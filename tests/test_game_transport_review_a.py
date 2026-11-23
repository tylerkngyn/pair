"""Independent adversarial review of the game command/event dispatcher wiring
(app/main.py + app/models.py) against docs/protocol.md.

These tests do not fix anything; they only probe for crashes/hangs/
cross-talk at the WebSocket transport layer, following the helpers already
established in tests/test_chat.py and tests/test_game_transport.py.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_chat import expect, joined


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def test_rapid_fire_game_commands_on_one_connection_never_crash_or_hang(client):
    with joined(client, room="RAPIDX") as (socket, _):
        # Fire all four game command types back-to-back, no reads in between,
        # to try to provoke a dropped/duplicated response or a hang.
        socket.send_json({"type": "game_start", "payload": {"request_id": "r1"}})
        socket.send_json({
            "type": "round_ready",
            "payload": {"request_id": "r2", "round_id": "round_1"},
        })
        socket.send_json({
            "type": "guess",
            "payload": {"request_id": "r3", "round_id": "round_1", "word": "fish"},
        })
        socket.send_json({"type": "game_sync", "payload": {"request_id": "r4"}})
        socket.send_json({
            "type": "guess",
            "payload": {"request_id": "r5", "round_id": "round_1", "word": "bird"},
        })

        # game_start -> game_state, round_ready -> game_state,
        # guess -> game_state + guess_result, game_sync -> game_state,
        # guess -> game_state + guess_result => 7 events total.
        received = [socket.receive_json() for _ in range(7)]
        types = [e["type"] for e in received]
        assert types == [
            "game_state", "game_state", "game_state", "guess_result",
            "game_state", "game_state", "guess_result",
        ], types

        # Connection is still alive and ordering/state is sane afterward.
        socket.send_json({"type": "game_sync", "payload": {"request_id": "r6"}})
        final_state = expect(socket, "game_state")
        assert final_state["version"] == 6


def test_chat_typing_reaction_unaffected_by_interleaved_game_commands(client):
    with joined(client, room="MIXED1") as (alice, _):
        with joined(client, "Bob", "MIXED1") as (bob, _):
            expect(alice, "system")
            expect(alice, "room_state")

            # Chat works before any game command.
            alice.send_json({"type": "chat", "payload": {"text": "hi before game"}})
            assert expect(alice, "chat") == expect(bob, "chat")

            # Alice sends a game command; only Alice should see its events.
            alice.send_json({"type": "game_start", "payload": {"request_id": "g1"}})
            expect(alice, "game_state")

            # Chat, typing, and reaction still work fully, on both peers,
            # with no leaked game_state/guess_result on Bob's side.
            alice.send_json({"type": "chat", "payload": {"text": "hi after game"}})
            assert expect(alice, "chat") == expect(bob, "chat")

            alice.send_json({"type": "typing", "payload": {"typing": True}})
            for socket in (alice, bob):
                assert expect(socket, "typing") == {"sender": "Alice", "typing": True}

            bob.send_json({"type": "reaction", "payload": {"emoji": "👍"}})
            for socket in (alice, bob):
                assert expect(socket, "reaction") == {"sender": "Bob", "emoji": "👍"}

            # Interleave again: game command, then immediately chat, on the
            # same connection, back to back with no intervening read.
            alice.send_json({"type": "round_ready", "payload": {"request_id": "g2", "round_id": "round_1"}})
            alice.send_json({"type": "chat", "payload": {"text": "interleaved"}})
            expect(alice, "game_state")
            assert expect(alice, "chat") == expect(bob, "chat")


@pytest.mark.parametrize("word", ["", "x" * 65, "x" * 10_000, " ", "🐙" * 64])
def test_guess_with_empty_or_overlong_word_does_not_crash_the_connection(client, word):
    with joined(client, room="GUESSL") as (socket, _):
        socket.send_json({
            "type": "guess",
            "payload": {"request_id": "r1", "round_id": "round_1", "word": word},
        })
        # Whatever happens (validation rejection, or placeholder apply),
        # the connection must survive and keep answering afterward.
        event = socket.receive_json()
        assert event["type"] in ("error", "game_state"), event
        if event["type"] == "error":
            assert event["payload"]["code"] == "INVALID_MSG"
        else:
            # Accepted: a guess_result must follow, and it must not crash.
            result = expect(socket, "guess_result")
            assert result["request_id"] == "r1"

        socket.send_json({"type": "chat", "payload": {"text": "still alive"}})
        assert expect(socket, "chat")["text"] == "still alive"


def test_guess_with_exactly_max_length_word_boundary(client):
    with joined(client) as (socket, _):
        word = "x" * 64  # GuessCommand's documented max_length
        socket.send_json({
            "type": "guess",
            "payload": {"request_id": "r1", "round_id": "round_1", "word": word},
        })
        expect(socket, "game_state")
        result = expect(socket, "guess_result")
        assert result["request_id"] == "r1"


def test_one_peer_sending_game_commands_does_not_affect_the_other_peer(client):
    with joined(client, room="TWOPER") as (alice, _):
        with joined(client, "Bob", "TWOPER") as (bob, _):
            expect(alice, "system")
            expect(alice, "room_state")

            # Only Alice plays the game.
            alice.send_json({"type": "game_start", "payload": {"request_id": "g1"}})
            alice_state = expect(alice, "game_state")
            alice.send_json({
                "type": "guess",
                "payload": {"request_id": "g2", "round_id": "round_1", "word": "owl"},
            })
            expect(alice, "game_state")
            expect(alice, "guess_result")

            # Bob never sent a game command and must have received nothing
            # of the sort; Bob's own chat/typing/reaction keep working.
            bob.send_json({"type": "chat", "payload": {"text": "bob chats"}})
            assert expect(alice, "chat") == expect(bob, "chat")

            bob.send_json({"type": "typing", "payload": {"typing": True}})
            for socket in (alice, bob):
                assert expect(socket, "typing") == {"sender": "Bob", "typing": True}

            # Bob can independently issue his own game_sync and gets a
            # consistent, shared room-level game_state back.
            bob.send_json({"type": "game_sync", "payload": {"request_id": "b1"}})
            bob_state = expect(bob, "game_state")
            assert bob_state["room_id"] == alice_state["room_id"]
            assert bob_state["version"] >= alice_state["version"]
