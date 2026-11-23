"""Independent adversarial review of InMemoryGameRepository / StubScoreProvider.

Run: .venv/bin/python -m pytest tests/test_game_repository_memory_review_b.py -v
"""

import asyncio

from app.contracts.game import CommandResult, GameState, ScoreProvider
from app.game_repository.memory import InMemoryGameRepository, StubScoreProvider


def make_state(version: int, room_id: str = "ROOM1") -> GameState:
    return GameState(
        schema_version=1,
        version=version,
        room_id=room_id,
        phase="lobby",
        leader_id="peer_a",
        admission_order=["peer_a"],
    )


def make_result(request_id: str = "req1") -> CommandResult:
    return CommandResult(request_id=request_id, accepted=True)


def test_load_never_read_room_returns_none():
    asyncio.run(_test_load_never_read_room_returns_none())


async def _test_load_never_read_room_returns_none():
    repo = InMemoryGameRepository()
    result = await repo.load("NEVER_SEEN")
    assert result is None


def test_load_after_write_returns_gamestate_type():
    asyncio.run(_test_load_after_write_returns_gamestate_type())


async def _test_load_after_write_returns_gamestate_type():
    repo = InMemoryGameRepository()
    state = make_state(0)
    ok = await repo.compare_and_set("ROOM1", 0, state, make_result())
    assert ok
    loaded = await repo.load("ROOM1")
    assert isinstance(loaded, GameState)
    assert loaded.version == 0


def test_compare_and_set_aliases_next_state_no_defensive_copy():
    asyncio.run(_test_compare_and_set_aliases_next_state_no_defensive_copy())


async def _test_compare_and_set_aliases_next_state_no_defensive_copy():
    """If the caller mutates next_state after a successful commit, does the
    repository's stored/returned state change too? GameState is a pydantic
    StrictModel; if the repo stores the same object reference rather than a
    copy, mutating the caller's object leaks into the "committed" state.
    """
    repo = InMemoryGameRepository()
    state = make_state(0)
    ok = await repo.compare_and_set("ROOM1", 0, state, make_result())
    assert ok

    # Mutate the object the caller still holds a reference to.
    state.shared_score = 99999

    loaded = await repo.load("ROOM1")
    # If this fails, the repo is storing a live alias to the caller's
    # object rather than an independent copy -- caller-side mutation after
    # commit silently corrupts "committed" state.
    assert loaded.shared_score != 99999, (
        "compare_and_set stored a reference to next_state; caller mutation "
        "after commit leaked into the stored state"
    )


def test_compare_and_set_aliases_command_result_no_defensive_copy():
    asyncio.run(_test_compare_and_set_aliases_command_result_no_defensive_copy())


async def _test_compare_and_set_aliases_command_result_no_defensive_copy():
    repo = InMemoryGameRepository()
    state = make_state(0)
    result = make_result("reqX")
    ok = await repo.compare_and_set("ROOM1", 0, state, result)
    assert ok

    result.points_delta = 123456

    stored = await repo.result_for("ROOM1", "reqX")
    assert stored.points_delta != 123456, (
        "compare_and_set stored a reference to command_result; caller "
        "mutation after commit leaked into the stored CommandResult"
    )


def test_load_returns_same_object_mutation_leaks_into_store():
    asyncio.run(_test_load_returns_same_object_mutation_leaks_into_store())


async def _test_load_returns_same_object_mutation_leaks_into_store():
    """Even without the caller-side mutation above: does load() hand back a
    live reference to the internal record's state, letting a caller mutate
    the repository's internal state through the returned object?
    """
    repo = InMemoryGameRepository()
    state = make_state(0)
    await repo.compare_and_set("ROOM1", 0, state, make_result())

    loaded_once = await repo.load("ROOM1")
    loaded_once.shared_score = 777777

    loaded_twice = await repo.load("ROOM1")
    assert loaded_twice.shared_score != 777777, (
        "load() returns a live reference into repository internals; "
        "mutating the returned GameState corrupts the stored state for "
        "every future load() of this room"
    )


def test_compare_and_set_negative_expected_version_on_fresh_room():
    asyncio.run(_test_compare_and_set_negative_expected_version_on_fresh_room())


async def _test_compare_and_set_negative_expected_version_on_fresh_room():
    """Fresh room has implicit current_version == 0. A negative
    expected_version should never match and must be rejected."""
    repo = InMemoryGameRepository()
    state = make_state(1)
    ok = await repo.compare_and_set("FRESH", -1, state, make_result("r1"))
    assert ok is False
    assert await repo.load("FRESH") is None


def test_compare_and_set_expected_version_far_ahead_of_actual():
    asyncio.run(_test_compare_and_set_expected_version_far_ahead_of_actual())


async def _test_compare_and_set_expected_version_far_ahead_of_actual():
    repo = InMemoryGameRepository()
    state0 = make_state(0)
    await repo.compare_and_set("ROOM1", 0, state0, make_result("r1"))

    # actual stored version is 0; claim expected_version absurdly ahead
    state_far = make_state(999999)
    ok = await repo.compare_and_set("ROOM1", 999999, state_far, make_result("r2"))
    assert ok is False
    loaded = await repo.load("ROOM1")
    assert loaded.version == 0  # unchanged


def test_compare_and_set_negative_expected_version_against_nonzero_actual():
    asyncio.run(_test_compare_and_set_negative_expected_version_against_nonzero_actual())


async def _test_compare_and_set_negative_expected_version_against_nonzero_actual():
    repo = InMemoryGameRepository()
    state0 = make_state(5)
    # Force a stored state with version 5 by going through the normal path
    # is awkward (expected_version must match current 0 first), so instead
    # verify that once version is nonzero, a negative expected_version still
    # never spuriously matches.
    await repo.compare_and_set("ROOM1", 0, state0, make_result("seed"))
    ok = await repo.compare_and_set("ROOM1", -5, make_state(6), make_result("r3"))
    assert ok is False


def test_stub_score_provider_satisfies_scoreprovider_protocol_runtime_check():
    stub = StubScoreProvider()
    assert isinstance(stub, ScoreProvider), (
        "StubScoreProvider does not structurally satisfy the "
        "@runtime_checkable ScoreProvider Protocol via isinstance()"
    )


def test_stub_score_provider_lookup_signature_matches_protocol_call_shape():
    stub = StubScoreProvider()
    # Exercise exactly the call shape the Protocol documents.
    tier = stub.lookup("v1", "img_1", "lighthouse")
    assert tier in ("Perfect", "Great", "Good", "Unscored")


def test_stub_score_provider_class_itself_is_not_an_instance_check_bypass():
    """isinstance() on a runtime_checkable Protocol only checks method
    *names* exist, not signatures. Confirm a trivial object with a
    differently-shaped `lookup` would ALSO pass isinstance, showing the
    Protocol check is weaker than it looks -- documenting this is not a bug
    in memory.py, but testers should not over-trust isinstance(..., ScoreProvider).
    """

    class FakeWrongSignature:
        def lookup(self):  # wrong arity entirely
            return "Unscored"

    assert isinstance(FakeWrongSignature(), ScoreProvider)
