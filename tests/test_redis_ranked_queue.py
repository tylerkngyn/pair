"""Simulates two workers sharing one Redis, each with its own
RedisRankedQueue, to prove matchmaking (including widening on both join
and poll) is correct across workers without real multi-process infra —
same approach as tests/test_redis_room_manager.py.
"""

import asyncio

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis

from app.practice.redis_ranked_queue import RedisRankedQueue

FILTERS = {"categories": [], "difficulty": "mixed", "duration_minutes": 30, "problem_count": 1}
OTHER_FILTERS = {"categories": ["arrays"], "difficulty": "mixed", "duration_minutes": 30, "problem_count": 1}


@pytest.fixture
def two_workers():
    server = FakeServer()
    return (
        RedisRankedQueue(client=FakeRedis(server=server, decode_responses=True)),
        RedisRankedQueue(client=FakeRedis(server=server, decode_responses=True)),
    )


def run(body):
    asyncio.run(body())


def test_first_join_queues_second_join_matches(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        joined = await worker_a.join("alice", "alice", 1000, FILTERS)
        assert joined["status"] == "queued"

        matched = await worker_b.join("bob", "bob", 1000, FILTERS)
        assert matched["status"] == "matched"
        assert matched["opponent"]["user_id"] == "alice"
        assert matched["opponent"]["username"] == "alice"
        assert matched["opponent"]["rating"] == 1000

    run(scenario)


def test_double_join_rejected(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        await worker_a.join("alice", "alice", 1000, FILTERS)
        again = await worker_b.join("alice", "alice", 1000, FILTERS)
        assert again["status"] == "already_queued"

    run(scenario)


def test_different_filters_never_match(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        await worker_a.join("alice", "alice", 1000, FILTERS)
        result = await worker_b.join("bob", "bob", 1000, OTHER_FILTERS)
        assert result["status"] == "queued"

    run(scenario)


def test_rating_outside_base_window_does_not_match(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        await worker_a.join("alice", "alice", 1000, FILTERS)
        result = await worker_b.join("bob", "bob", 1200, FILTERS)
        assert result["status"] == "queued"

    run(scenario)


def test_fresh_arrival_matches_a_long_waiting_candidate(two_workers):
    """Mirrors the in-process regression: a fresh join's own window
    should widen based on how long the ALREADY-QUEUED candidate has
    waited, not shrink back to the base case.
    """
    worker_a, worker_b = two_workers

    async def scenario():
        await worker_a.join("alice", "alice", 1000, FILTERS)
        # Backdate alice's queued_at directly, simulating a long wait.
        await worker_a._redis.hset("rq_meta:alice", "queued_at", 0)

        matched = await worker_b.join("bob", "bob", 1200, FILTERS)
        assert matched["status"] == "matched"
        assert matched["opponent"]["user_id"] == "alice"

    run(scenario)


def test_poll_on_a_lone_queued_player_widens_and_matches(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        await worker_a.join("alice", "alice", 1000, FILTERS)
        await worker_a.join("bob", "bob", 1200, FILTERS)
        # Too far apart to have matched on bob's arrival; backdate both so
        # a poll's widened window (based on whichever waited longer) now
        # covers the 200-point gap.
        await worker_a._redis.hset("rq_meta:alice", "queued_at", 0)
        await worker_a._redis.hset("rq_meta:bob", "queued_at", 0)

        polled = await worker_b.poll("alice")
        assert polled["status"] == "matched"
        assert polled["opponent"]["user_id"] == "bob"

    run(scenario)


def test_poll_with_no_match_available_stays_queued(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        joined = await worker_a.join("alice", "alice", 1000, FILTERS)
        polled = await worker_b.poll("alice")
        assert polled["status"] == "queued"
        assert polled["filters"] == FILTERS
        assert polled["queued_at"] == joined["queued_at"]
        assert await worker_b.contains("alice")

    run(scenario)


def test_poll_when_never_queued_is_idle(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        assert await worker_a.poll("nobody") == {"status": "idle"}

    run(scenario)


def test_pending_mailbox_consumed_once_across_workers(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        await worker_a.mark_pending("alice", "match-123")

        first = await worker_b.poll("alice")
        assert first == {"status": "matched", "match_id": "match-123"}

        second = await worker_a.poll("alice")
        assert second == {"status": "idle"}

    run(scenario)


def test_leave_removes_a_queued_entry(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        await worker_a.join("alice", "alice", 1000, FILTERS)
        assert await worker_b.leave("alice") is True
        assert await worker_a.poll("alice") == {"status": "idle"}
        # A second leave finds nothing left to remove.
        assert await worker_b.leave("alice") is False

    run(scenario)


def test_leave_never_queued_reports_false(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        assert await worker_a.leave("nobody") is False

    run(scenario)


def test_matched_candidate_removed_so_a_third_join_cannot_also_match_it(two_workers):
    """Capacity/correctness analog to the room manager's duplicate-handle
    test: once alice is matched, she must be gone from the pool entirely,
    not matchable a second time by someone else's join() racing in.
    """
    worker_a, worker_b = two_workers

    async def scenario():
        await worker_a.join("alice", "alice", 1000, FILTERS)
        first_match = await worker_a.join("bob", "bob", 1000, FILTERS)
        assert first_match["status"] == "matched"

        second_match = await worker_b.join("carol", "carol", 1000, FILTERS)
        assert second_match["status"] == "queued"

    run(scenario)


def test_sweep_removes_a_ghost_member_whose_meta_expired(two_workers):
    """A crashed/vanished client's rq_meta hash can TTL out on its own,
    but nothing else ever removes the matching ZSET member — only an
    active sweep does, same role as the room manager's reap loop.
    """
    worker_a, worker_b = two_workers

    async def scenario():
        from app.practice.redis_ranked_queue import filters_key

        await worker_a.join("alice", "alice", 1000, FILTERS)
        # Simulate alice's meta having already expired without a clean
        # leave()/match, leaving her user_id stranded in the ZSET alone.
        await worker_a._redis.delete(worker_a._meta_key("alice"))

        await worker_b._sweep_once()

        remaining = await worker_a._redis.zrange(worker_a._queue_key(filters_key(FILTERS)), 0, -1)
        assert "alice" not in remaining

    run(scenario)


def test_sweep_leaves_live_entries_alone(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        await worker_a.join("alice", "alice", 1000, FILTERS)
        await worker_b._sweep_once()

        polled = await worker_a.poll("alice")
        assert polled["status"] == "queued"

    run(scenario)


def test_finalize_lock_is_exclusive_across_workers(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        first = await worker_a.acquire_finalize_lock("match-1", "worker-a")
        assert first is True

        second = await worker_b.acquire_finalize_lock("match-1", "worker-b")
        assert second is False

        # A different match is an entirely separate lock.
        third = await worker_b.acquire_finalize_lock("match-2", "worker-b")
        assert third is True

    run(scenario)
