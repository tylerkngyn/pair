"""WebSocket-level tests for the game command/event skeleton wiring.

These only prove that the four new client commands (game_start,
round_ready, guess, game_sync) validate and dispatch through app/main.py's
existing WebSocket loop without crashing the connection, and that malformed
game payloads are rejected the same way malformed chat payloads already
are. There is no real GameEngine/ScoreProvider yet (see app/main.py's
_placeholder_apply), so none of this asserts real round/scoring behavior.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from tests.test_chat import expect, joined


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def test_game_sync_is_accepted_and_gets_a_response(client):
    with joined(client, room="GAMESY") as (socket, _):
        socket.send_json({"type": "game_sync", "payload": {"request_id": "r1"}})
        state = expect(socket, "game_state")
        assert state["room_id"] == "GAMESY"
        assert state["version"] >= 1
        # Connection still alive and usable afterward.
        socket.send_json({"type": "chat", "payload": {"text": "still here"}})
        assert expect(socket, "chat")["text"] == "still here"


def test_game_start_round_ready_get_game_state_responses(client):
    with joined(client, room="GAMEAB") as (socket, _):
        socket.send_json({"type": "game_start", "payload": {"request_id": "r1"}})
        before = expect(socket, "game_state")["version"]
        socket.send_json({
            "type": "round_ready",
            "payload": {"request_id": "r2", "round_id": "round_1"},
        })
        after = expect(socket, "game_state")["version"]
        assert after == before + 1


def test_guess_gets_game_state_then_guess_result(client):
    with joined(client) as (socket, _):
        socket.send_json({
            "type": "guess",
            "payload": {"request_id": "r1", "round_id": "round_1", "word": "lighthouse"},
        })
        expect(socket, "game_state")
        result = expect(socket, "guess_result")
        assert result["request_id"] == "r1"
        assert result["accepted"] is True


def test_game_command_before_join_is_rejected_like_chat(client):
    with client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "game_sync", "payload": {"request_id": "r1"}})
        assert expect(socket, "error")["message"] == "Send a join event first"


@pytest.mark.parametrize("raw", [
    '{"type":"game_sync"}',
    '{"type":"game_sync","payload":null}',
    '{"type":"game_sync","payload":{}}',
    '{"type":"game_sync","payload":{"request_id":42}}',
    '{"type":"game_sync","payload":{"request_id":"r1","extra":true}}',
    '{"type":"guess","payload":{"request_id":"r1","round_id":"round_1"}}',
    '{"type":"guess","payload":{"request_id":"r1","round_id":"round_1","word":""}}',
    '{"type":"round_ready","payload":{"request_id":"r1"}}',
])
def test_malformed_game_payloads_are_rejected_like_malformed_chat(client, raw):
    with joined(client) as (socket, _):
        socket.send_text(raw)
        assert expect(socket, "error")["code"] == "INVALID_MSG"
        # Connection survives, same as the malformed-chat case.
        socket.send_json({"type": "ping", "payload": {}})
        socket.send_json({"type": "chat", "payload": {"text": "still connected"}})
        assert expect(socket, "chat")["text"] == "still connected"
