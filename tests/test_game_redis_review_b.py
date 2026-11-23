"""Adversarial review tests for RedisGameRepository vs its contract/docs,
and vs InMemoryGameRepository's shape. Follows tests/test_game_redis.py's
fakeredis + asyncio.run() pattern.
"""

import asyncio

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis

from app.contracts.game import (
    AcceptedWord,
    CommandResult,
    GameState,
    ImageMeta,
    RoundRecap,
)
from app.game_repository.memory import InMemoryGameRepository
from app.game_repository.redis import RedisGameRepository


def make_rich_state(room_id: str, version: int) -> GameState:
    """A state exercising every nested field: accepted_words, recap, image."""
    image = ImageMeta(image_id="img_1", url="https://cdn.example.com/x.webp", alt_text="a \"quoted\" café")
    return GameState(
        schema_version=1,
        version=version,
        room_id=room_id,
        phase="playing",
        leader_id="peer_a",
        admission_order=["peer_a", "peer_b"],
        round_id="r_1",
        image=image,
        server_deadline_ms=1234567890,
        shared_score=50,
        accepted_words=[
            AcceptedWord(peer_id="peer_a", canonical_word="lighthouse", tier="Perfect"),
            AcceptedWord(peer_id="peer_b", canonical_word="coast", tier="Good"),
        ],
        used_words=["lighthouse", "coast"],
        recap=RoundRecap(
            round_id="r_0",
            image=image,
            accepted_words=[AcceptedWord(peer_id="peer_a", canonical_word="pancakes", tier="Perfect")],
            points_awarded=25,
        ),
    )


@pytest.fixture
def redis_repo():
    server = FakeServer()
    return RedisGameRepository(client=FakeRedis(server=server, decode_responses=True))


@pytest.fixture
def memory_repo():
    return InMemoryGameRepository()


def test_round_trip_preserves_nested_fields(redis_repo):
    """GameState with nested accepted_words/recap/image must survive
    model_dump_json -> Redis -> model_validate_json byte-for-byte equal."""
    async def scenario():
        state = make_rich_state("ROOM1", 1)
        result = CommandResult(request_id="req-1", accepted=True, tier="Perfect", points_delta=25)
        assert await redis_repo.compare_and_set("ROOM1", 0, state, result) is True

        loaded = await redis_repo.load("ROOM1")
        assert loaded == state
        assert loaded.recap.accepted_words[0].canonical_word == "pancakes"
        assert loaded.image.alt_text == 'a "quoted" café'
        assert loaded.accepted_words[1].tier == "Good"

    asyncio.run(scenario())


def test_replay_vs_conflict_distinguishable_via_return_value(redis_repo):
    """compare_and_set collapses REPLAY and CONFLICT to the same `False`
    by design (per the repo's own docstring) -- confirm that both
    scenarios actually reach Python as False, and that the caller's only
    way to tell them apart (result_for) behaves correctly for each."""
    async def scenario():
        state_v1 = make_rich_state("ROOM2", 1)
        result_v1 = CommandResult(request_id="req-1", accepted=True)
        assert await redis_repo.compare_and_set("ROOM2", 0, state_v1, result_v1) is True

        # REPLAY case: same request_id resent.
        replay_attempt_state = make_rich_state("ROOM2", 2)
        replay_committed = await redis_repo.compare_and_set(
            "ROOM2", 1, replay_attempt_state, CommandResult(request_id="req-1", accepted=False)
        )
        assert replay_committed is False
        assert await redis_repo.result_for("ROOM2", "req-1") == result_v1  # original preserved

        # CONFLICT case: new request_id but stale expected_version.
        conflict_attempt_state = make_rich_state("ROOM2", 2)
        conflict_committed = await redis_repo.compare_and_set(
            "ROOM2", 0, conflict_attempt_state, CommandResult(request_id="req-2", accepted=True)
        )
        assert conflict_committed is False
        # Since it genuinely conflicted (not a replay), no result should
        # have been recorded for req-2 at all.
        assert await redis_repo.result_for("ROOM2", "req-2") is None

    asyncio.run(scenario())


@pytest.mark.parametrize("scenario_name", ["conflict", "replay", "success"])
def test_redis_and_memory_adapters_agree_on_bool_shape(redis_repo, memory_repo, scenario_name):
    """Both adapters must return the same True/False for equivalent
    sequences, per memory.py's/redis.py's shared docstring claim."""
    async def scenario():
        for repo in (redis_repo, memory_repo):
            state_v1 = make_rich_state("ROOMX", 1)
            result_v1 = CommandResult(request_id="req-1", accepted=True)
            first = await repo.compare_and_set("ROOMX", 0, state_v1, result_v1)
            assert first is True

            if scenario_name == "success":
                state_v2 = make_rich_state("ROOMX", 2)
                result_v2 = CommandResult(request_id="req-2", accepted=True)
                second = await repo.compare_and_set("ROOMX", 1, state_v2, result_v2)
                assert second is True
            elif scenario_name == "replay":
                state_v2_attempt = make_rich_state("ROOMX", 2)
                second = await repo.compare_and_set("ROOMX", 1, state_v2_attempt, result_v1)
                assert second is False
            elif scenario_name == "conflict":
                state_v2_attempt = make_rich_state("ROOMX", 2)
                result_v2 = CommandResult(request_id="req-2", accepted=True)
                # Wrong expected_version (0 instead of 1).
                second = await repo.compare_and_set("ROOMX", 0, state_v2_attempt, result_v2)
                assert second is False

    asyncio.run(scenario())


def test_same_request_id_across_different_rooms_is_not_a_collision(redis_repo, memory_repo):
    """A request_id is only meaningful scoped to a room (docs/protocol.md's
    idempotent-replay design is per-command, and commands are always sent
    to one room). Reusing the same request_id in a different room_id must
    NOT be treated as an already-applied replay -- it should commit
    normally in the second room."""
    async def scenario():
        for repo in (redis_repo, memory_repo):
            state_a = make_rich_state("ROOM_A", 1)
            result = CommandResult(request_id="shared-req-id", accepted=True, tier="Perfect", points_delta=10)
            committed_a = await repo.compare_and_set("ROOM_A", 0, state_a, result)
            assert committed_a is True

            # Same request_id, different room, fresh room (expected_version=0).
            state_b = make_rich_state("ROOM_B", 1)
            result_b = CommandResult(request_id="shared-req-id", accepted=True, tier="Good", points_delta=5)
            committed_b = await repo.compare_and_set("ROOM_B", 0, state_b, result_b)
            assert committed_b is True, (
                f"{type(repo).__name__}: request_id reuse across different room_ids "
                "was incorrectly treated as a collision/replay"
            )

            # Each room's result_for must return its own result, not bleed over.
            assert await repo.result_for("ROOM_A", "shared-req-id") == result
            assert await repo.result_for("ROOM_B", "shared-req-id") == result_b

    asyncio.run(scenario())
