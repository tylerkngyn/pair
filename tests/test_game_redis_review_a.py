"""Adversarial review tests for RedisGameRepository, checking it against
app/contracts/game.py and docs/protocol.md's "Atomic commit shape" /
leader-election / deadline sections. Pattern follows tests/test_game_redis.py.
"""

import asyncio

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis

from app.contracts.game import CommandResult, GameState
from app.game_repository.redis import RedisGameRepository


def make_state(room_id: str, version: int, **kwargs) -> GameState:
    defaults = dict(
        schema_version=1,
        version=version,
        room_id=room_id,
        phase="lobby",
        leader_id="peer_a",
        admission_order=["peer_a"],
    )
    defaults.update(kwargs)
    return GameState(**defaults)


@pytest.fixture
def repo():
    server = FakeServer()
    return RedisGameRepository(client=FakeRedis(server=server, decode_responses=True))


def test_missing_key_treated_as_version_0_first_write(repo):
    """A brand-new room has no game: key at all. The script must treat
    HGET returning false (missing field/key) as current_version == '0',
    so the very first compare_and_set(room_id, 0, ...) succeeds."""

    async def scenario():
        assert await repo.load("FRESH1") is None
        next_state = make_state("FRESH1", 1)
        result = CommandResult(request_id="req-1", accepted=True)

        committed = await repo.compare_and_set("FRESH1", 0, next_state, result)

        assert committed is True
        loaded = await repo.load("FRESH1")
        assert loaded == next_state

    asyncio.run(scenario())


def test_genuine_conflict_under_concurrent_writers(repo):
    """Two 'workers' both load version 0, race to commit version 1. One
    must win, the other must be rejected as a genuine conflict (distinct
    request_ids, so this is not idempotent replay)."""

    async def scenario():
        room_id = "RACE01"
        winner_state = make_state(room_id, 1, leader_id="peer_a")
        winner_result = CommandResult(request_id="req-winner", accepted=True)
        loser_state = make_state(room_id, 1, leader_id="peer_b")
        loser_result = CommandResult(request_id="req-loser", accepted=True)

        # Simulate both having read expected_version=0 concurrently;
        # whichever compare_and_set call reaches Redis first wins.
        first = await repo.compare_and_set(room_id, 0, winner_state, winner_result)
        second = await repo.compare_and_set(room_id, 0, loser_state, loser_result)

        assert first is True
        assert second is False

        # The loser's request_id must NOT have a stored result (it's a
        # real conflict, not a replay) -- this is how a caller in
        # compare_and_set's contract tells conflict apart from replay.
        assert await repo.result_for(room_id, "req-loser") is None
        # And the committed state must be the winner's, untouched by the loser.
        loaded = await repo.load(room_id)
        assert loaded == winner_state

    asyncio.run(scenario())


def test_ttl_is_set_on_both_hashes_after_write(repo):
    """docs/protocol.md + the module docstring say both game:<room_id> and
    game_results:<room_id> get the same TTL, refreshed on every write."""

    async def scenario():
        room_id = "TTL0001"
        next_state = make_state(room_id, 1)
        result = CommandResult(request_id="req-1", accepted=True)

        assert await repo.compare_and_set(room_id, 0, next_state, result) is True

        game_ttl = await repo._redis.ttl(repo._game_key(room_id))
        results_ttl = await repo._redis.ttl(repo._results_key(room_id))

        # ttl() returns -1 if the key exists but has no TTL, -2 if the
        # key doesn't exist. Either hash lacking a TTL is a real bug:
        # an abandoned room's game state would never fall out of Redis.
        assert game_ttl > 0, f"game: hash has no TTL set (ttl={game_ttl})"
        assert results_ttl > 0, f"game_results: hash has no TTL set (ttl={results_ttl})"

    asyncio.run(scenario())


def test_result_for_reads_back_stored_command_result_after_commit(repo):
    """result_for must deserialize exactly what was committed, including
    non-default fields (tier/points_delta/reason), matching the
    CommandResult persisted in the same atomic write as the state."""

    async def scenario():
        room_id = "RESULT1"
        next_state = make_state(room_id, 1)
        result = CommandResult(
            request_id="req-guess-1",
            accepted=True,
            reason="",
            tier="Great",
            points_delta=15,
        )

        committed = await repo.compare_and_set(room_id, 0, next_state, result)
        assert committed is True

        fetched = await repo.result_for(room_id, "req-guess-1")
        assert fetched is not None
        assert fetched == result
        assert fetched.tier == "Great"
        assert fetched.points_delta == 15

    asyncio.run(scenario())
