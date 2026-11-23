"""Tests for the in-memory GameRepository skeleton.

No async test plugin is installed in this repo (see tests/test_chat.py's
convention), so each async scenario is wrapped in a plain sync test via
asyncio.run, rather than pytest.mark.asyncio.

A trivial fake GameEngine (echoes the command back as a no-op transition)
would be defined inline here if needed to exercise the repository end to
end; real game logic is out of scope for this module (see
app/contracts/game.py).
"""

import asyncio

from app.contracts.game import CommandResult, GameState
from app.game_repository.memory import InMemoryGameRepository, StubScoreProvider

ROOM_ID = "AB12CD"


def make_state(version: int, room_id: str = ROOM_ID) -> GameState:
    return GameState(
        schema_version=1,
        version=version,
        room_id=room_id,
        phase="lobby",
        leader_id="peer_1",
        admission_order=["peer_1"],
    )


def test_load_unknown_room_returns_none() -> None:
    async def scenario() -> None:
        repo = InMemoryGameRepository()
        assert await repo.load("NOPE00") is None

    asyncio.run(scenario())


def test_compare_and_set_succeeds_and_persists() -> None:
    async def scenario() -> None:
        repo = InMemoryGameRepository()
        next_state = make_state(version=1)
        result = CommandResult(request_id="req-1", accepted=True)

        ok = await repo.compare_and_set(
            ROOM_ID, expected_version=0, next_state=next_state, command_result=result,
        )

        assert ok is True
        loaded = await repo.load(ROOM_ID)
        assert loaded is not None
        assert loaded.version == 1
        assert loaded.phase == "lobby"

    asyncio.run(scenario())


def test_compare_and_set_rejects_stale_version() -> None:
    async def scenario() -> None:
        repo = InMemoryGameRepository()
        await repo.compare_and_set(
            ROOM_ID, expected_version=0, next_state=make_state(version=1),
            command_result=CommandResult(request_id="req-1", accepted=True),
        )

        # Caller computed next_state off a stale expected_version (0 again,
        # but the room is already at version 1) -> rejected, state unchanged.
        stale_next = make_state(version=2)
        ok = await repo.compare_and_set(
            ROOM_ID, expected_version=0, next_state=stale_next,
            command_result=CommandResult(request_id="req-2", accepted=True),
        )

        assert ok is False
        loaded = await repo.load(ROOM_ID)
        assert loaded is not None
        assert loaded.version == 1

    asyncio.run(scenario())


def test_idempotent_replay_of_repeated_request_id() -> None:
    async def scenario() -> None:
        repo = InMemoryGameRepository()
        first_result = CommandResult(request_id="req-1", accepted=True, points_delta=10)
        ok = await repo.compare_and_set(
            ROOM_ID, expected_version=0, next_state=make_state(version=1),
            command_result=first_result,
        )
        assert ok is True

        # Same request_id resent (e.g. dropped ack) with a different
        # candidate write: must not re-apply (no second write), and the
        # originally stored result must still be retrievable for replay.
        replay_ok = await repo.compare_and_set(
            ROOM_ID, expected_version=1, next_state=make_state(version=2),
            command_result=CommandResult(request_id="req-1", accepted=True, points_delta=999),
        )

        assert replay_ok is False
        loaded = await repo.load(ROOM_ID)
        assert loaded is not None
        assert loaded.version == 1  # unchanged; the bogus re-write did not land

        stored = await repo.result_for(ROOM_ID, "req-1")
        assert stored is not None
        assert stored.points_delta == 10

    asyncio.run(scenario())


def test_stub_score_provider_returns_fixed_tier() -> None:
    provider = StubScoreProvider()
    assert provider.lookup("v1", "img_1", "lighthouse") == "Unscored"
    assert provider.lookup("v1", "img_1", "anything") == "Unscored"

    custom = StubScoreProvider(fixed_tier="Good")
    assert custom.lookup("v1", "img_1", "lighthouse") == "Good"
