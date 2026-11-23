"""Tests for RedisGameRepository, using fakeredis (same pattern as
tests/test_redis_room_manager.py: a FakeServer-backed FakeRedis client, no
real Redis needed, and asyncio.run() wrapping each scenario since this repo
has no pytest-asyncio installed).
"""

import asyncio

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis

from app.contracts.game import CommandResult, GameState
from app.game_repository.redis import RedisGameRepository


def make_state(room_id: str, version: int) -> GameState:
    return GameState(
        schema_version=1,
        version=version,
        room_id=room_id,
        phase="lobby",
        leader_id="peer_a",
        admission_order=["peer_a"],
    )


@pytest.fixture
def repo():
    server = FakeServer()
    return RedisGameRepository(client=FakeRedis(server=server, decode_responses=True))


def test_load_unknown_room_returns_none(repo):
    async def scenario():
        assert await repo.load("NOPE") is None

    asyncio.run(scenario())


def test_compare_and_set_commits_fresh_room(repo):
    async def scenario():
        next_state = make_state("ABC123", 1)
        result = CommandResult(request_id="req-1", accepted=True)

        committed = await repo.compare_and_set("ABC123", 0, next_state, result)

        assert committed is True
        loaded = await repo.load("ABC123")
        assert loaded == next_state
        assert await repo.result_for("ABC123", "req-1") == result

    asyncio.run(scenario())


def test_compare_and_set_rejects_stale_version(repo):
    async def scenario():
        first_state = make_state("ABC123", 1)
        first_result = CommandResult(request_id="req-1", accepted=True)
        assert await repo.compare_and_set("ABC123", 0, first_state, first_result) is True

        # Someone else's write already bumped the room to version 1; a
        # second caller still expecting version 0 must be rejected, not
        # overwrite.
        stale_next_state = make_state("ABC123", 1)
        stale_result = CommandResult(request_id="req-2", accepted=True)
        committed = await repo.compare_and_set("ABC123", 0, stale_next_state, stale_result)

        assert committed is False
        # The conflicting write must not have landed.
        loaded = await repo.load("ABC123")
        assert loaded == first_state
        assert await repo.result_for("ABC123", "req-2") is None

    asyncio.run(scenario())


def test_compare_and_set_replays_idempotent_request_id(repo):
    async def scenario():
        state_v1 = make_state("ABC123", 1)
        result_v1 = CommandResult(request_id="req-1", accepted=True, tier="Perfect", points_delta=25)
        assert await repo.compare_and_set("ABC123", 0, state_v1, result_v1) is True

        # A retried command with the same request_id, even with a
        # (hypothetically different) next_state/command_result, must not
        # re-apply: compare_and_set returns False, and the caller fetches
        # the original result via result_for.
        state_v2_attempt = make_state("ABC123", 2)
        different_result = CommandResult(request_id="req-1", accepted=False, reason="should not land")
        committed = await repo.compare_and_set("ABC123", 1, state_v2_attempt, different_result)

        assert committed is False
        assert await repo.result_for("ABC123", "req-1") == result_v1
        # State must still be the one from the original application, not
        # bumped again and not overwritten by the retried call's (different)
        # result.
        loaded = await repo.load("ABC123")
        assert loaded == state_v1

    asyncio.run(scenario())


def test_result_for_unknown_request_id_returns_none(repo):
    async def scenario():
        state = make_state("ABC123", 1)
        result = CommandResult(request_id="req-1", accepted=True)
        await repo.compare_and_set("ABC123", 0, state, result)

        assert await repo.result_for("ABC123", "never-sent") is None

    asyncio.run(scenario())
