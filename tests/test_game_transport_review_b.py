"""Adversarial review of the game command/event dispatcher wiring in
app/main.py and app/models.py against docs/protocol.md and
app/contracts/game.py.

This is a review, not a fix: it documents actual behavior of the
placeholder wiring (including bugs/quirks), and does not assert the
"correct" eventual GameEngine/GameRepository behavior where the
placeholder intentionally differs from it.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import _GAME_STATE_STORE, app
from app.contracts.game import GuessCommand
from tests.test_chat import expect, joined


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# 1. Is _GAME_STATE_STORE correctly scoped per room_id?
# ---------------------------------------------------------------------------

def test_game_state_store_is_scoped_per_room(client):
    """Two different rooms sending game commands must not see each other's
    state: each room_code is an independent dict key, and _placeholder_apply
    looks up/creates state only under the caller's own peer.room. This
    confirms isolation holds for the placeholder as written."""
    with joined(client, room="ROOMAA") as (socket_a, _):
        socket_a.send_json({"type": "game_start", "payload": {"request_id": "a1"}})
        state_a = expect(socket_a, "game_state")
        assert state_a["room_id"] == "ROOMAA"
        assert state_a["version"] == 1

    with joined(client, handle="Bob", room="ROOMBB") as (socket_b, _):
        socket_b.send_json({"type": "game_start", "payload": {"request_id": "b1"}})
        state_b = expect(socket_b, "game_state")
        # ROOMBB must start fresh at version 1, not inherit ROOMAA's version.
        assert state_b["room_id"] == "ROOMBB"
        assert state_b["version"] == 1
        assert state_b["leader_id"] != state_a["leader_id"]

    # Independently, poking the module-level store confirms the two rooms
    # are genuinely separate dict entries with independent version counters.
    assert _GAME_STATE_STORE["ROOMAA"].version == 1
    assert _GAME_STATE_STORE["ROOMBB"].version == 1


# ---------------------------------------------------------------------------
# 2. Disconnect + new peer joining same room code afterward: stale state?
# ---------------------------------------------------------------------------

def test_stale_game_state_survives_disconnect_and_rejoin_by_new_peer(client):
    """_GAME_STATE_STORE is keyed only by room_code (a plain module-level
    dict, never cleared on disconnect/room-cleanup), so if the SAME room
    code is reused after the original peer disconnects, a brand-new peer
    that joins that code inherits the old game's state and version counter
    rather than starting a fresh lobby at version 0. This documents that
    actual behavior; it is explicitly a known placeholder gap, not something
    this test asserts should remain this way."""
    room = "STALE1"
    with joined(client, handle="Alice", room=room) as (socket, _):
        socket.send_json({"type": "game_start", "payload": {"request_id": "r1"}})
        first_state = expect(socket, "game_state")
        assert first_state["version"] == 1
        assert first_state["leader_id"] == "Alice"
    # socket context exits -> disconnect happens (room_manager reaps peer).

    with joined(client, handle="Bob", room=room) as (socket2, _):
        socket2.send_json({"type": "game_sync", "payload": {"request_id": "r2"}})
        second_state = expect(socket2, "game_sync" if False else "game_state")
        # Document actual (buggy-looking) behavior: version keeps climbing
        # from the dead peer's game, and leader_id still says "Alice" even
        # though Alice is gone and Bob is the only member now.
        assert second_state["version"] == 2
        assert second_state["leader_id"] == "Alice"
        assert second_state["room_id"] == room


# ---------------------------------------------------------------------------
# 3. Game command Pydantic models reject extra/unknown fields.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    '{"type":"game_start","payload":{"request_id":"r1","unexpected":1}}',
    '{"type":"round_ready","payload":{"request_id":"r1","round_id":"r_1","bogus":"x"}}',
    '{"type":"guess","payload":{"request_id":"r1","round_id":"r_1","word":"cat","extra":true}}',
    '{"type":"game_sync","payload":{"request_id":"r1","nope":null}}',
])
def test_all_four_game_commands_reject_extra_fields(client, raw):
    """StrictModel sets extra='forbid'; GameStartCommand, RoundReadyCommand,
    GuessCommand, and GameSyncCommand all inherit from it (app/contracts/
    game.py), so an unknown field on any of the four must be rejected with
    the same INVALID_MSG error chat events get for unknown fields, not
    silently ignored."""
    with joined(client) as (socket, _):
        socket.send_text(raw)
        assert expect(socket, "error")["code"] == "INVALID_MSG"
        socket.send_json({"type": "chat", "payload": {"text": "still connected"}})
        assert expect(socket, "chat")["text"] == "still connected"


def test_guess_command_direct_model_rejects_extra_field():
    """Direct Pydantic-level confirmation (bypassing the socket layer) that
    GuessCommand has extra='forbid' wired through StrictModel, matching
    ChatInput's behavior."""
    with pytest.raises(Exception):
        GuessCommand(request_id="r1", round_id="r_1", word="cat", extra_field="nope")


# ---------------------------------------------------------------------------
# 4. guess missing its required `word` field is rejected with the same
#    error shape as other malformed payloads.
# ---------------------------------------------------------------------------

def test_guess_missing_word_rejected_same_shape_as_other_malformed(client):
    with joined(client) as (socket, _):
        socket.send_text('{"type":"guess","payload":{"request_id":"r1","round_id":"round_1"}}')
        error = expect(socket, "error")
        assert set(error) == {"code", "message"}
        assert error["code"] == "INVALID_MSG"

        # Compare directly against a different kind of malformed payload
        # (chat with blank text) to confirm the error envelope shape matches
        # exactly, not just the code.
        socket.send_text('{"type":"chat","payload":{"text":""}}')
        error2 = expect(socket, "error")
        assert set(error2) == set(error)
        assert error2["code"] == error["code"]

        # Connection still usable afterward either way.
        socket.send_json({"type": "ping", "payload": {}})
        socket.send_json({"type": "chat", "payload": {"text": "ok"}})
        assert expect(socket, "chat")["text"] == "ok"
