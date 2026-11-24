import asyncio
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app
from app.room_manager import Peer, RoomManager


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def expect(socket, event_type):
    event = socket.receive_json()
    assert event["type"] == event_type, event
    assert set(event) == {"type", "payload"}
    return event["payload"]


@contextmanager
def joined(client, handle="Alice", room="ABC123"):
    with client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "join", "payload": {"room": room, "handle": handle}})
        expect(socket, "system")
        state = expect(socket, "room_state")
        yield socket, state


def test_http(client):
    assert client.get("/health").json() == {"status": "ok"}
    page = client.get("/")
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert 'id="setup-form"' in page.text
    assert '<form id="join-form">' in client.get("/chat").text


def test_pair_chat_capacity_disconnect_and_rejoin(client):
    with joined(client, room=" abc123 ") as (alice, waiting):
        assert waiting == {"room": "ABC123", "status": "waiting", "peers": ["Alice"]}
        alice.send_json({"type": "chat", "payload": {"text": "while waiting"}})
        assert expect(alice, "chat")["text"] == "while waiting"
        with joined(client, "Bob") as (bob, paired):
            assert paired == {"room": "ABC123", "status": "paired", "peers": ["Alice", "Bob"]}
            assert expect(alice, "system")["text"] == "Bob joined the room"
            assert expect(alice, "room_state") == paired
            with client.websocket_connect("/ws") as third:
                third.send_json({"type": "join", "payload": {"room": "ABC123", "handle": "Eve"}})
                assert expect(third, "error") == {"code": "ROOM_FULL", "message": "Room is full"}
                with pytest.raises(WebSocketDisconnect) as closed:
                    third.receive_json()
                assert closed.value.code == 1008
            text = "<script>alert('plain text')</script> 🐙\nhello"
            alice.send_json({"type": "chat", "payload": {"text": text}})
            echo = expect(alice, "chat")
            assert echo == expect(bob, "chat")
            assert echo["sender"] == "Alice"
            assert echo["text"] == text
            assert isinstance(echo["timestamp"], int)
            bob.send_json({"type": "chat", "payload": {"text": "reply"}})
            assert expect(alice, "chat") == expect(bob, "chat")
        assert expect(alice, "system")["text"] == "Bob left the room"
        assert expect(alice, "room_state") == waiting
        with joined(client, "Charlie") as (_, state):
            assert state["status"] == "paired"
            expect(alice, "system")
            expect(alice, "room_state")
        expect(alice, "system")
        expect(alice, "room_state")
    # Waiting for a fresh HTTP response lets the endpoint finish its finally.
    client.get("/health")
    assert not app.state.rooms._rooms


@pytest.mark.parametrize("raw", [
    "not json", "{", "null", "[]", '"hello"',
    '{"type":"unknown","payload":{}}',
    '{"type":"chat"}',
    '{"type":"chat","payload":null}',
    '{"type":"chat","payload":{"text":42}}',
    '{"type":"chat","payload":{"text":""}}',
    '{"type":"chat","payload":{"text":"   "}}',
    '{"type":"chat","payload":{"text":"ok","extra":true}}',
    '{"type":"ping","payload":{},"extra":true}',
    '{"type":"ping","payload":{"extra":true}}',
    '{"type":"chat","payload":{"text":"' + "x" * 501 + '"}}',
    "x" * 8193,
])
def test_invalid_messages_do_not_break_socket(client, raw):
    with joined(client) as (socket, _):
        socket.send_text(raw)
        assert expect(socket, "error")["code"] == "INVALID_MSG"
        socket.send_json({"type": "ping", "payload": {}})
        socket.send_json({"type": "chat", "payload": {"text": "still connected"}})
        assert expect(socket, "chat")["text"] == "still connected"


def test_binary_frames_and_chat_before_join(client):
    with client.websocket_connect("/ws") as socket:
        socket.send_bytes(b"binary")
        assert expect(socket, "error")["code"] == "INVALID_MSG"
        socket.send_json({"type": "chat", "payload": {"text": "too soon"}})
        assert expect(socket, "error")["message"] == "Send a join event first"
        socket.send_json({"type": "join", "payload": {"room": "ABC123"}})
        event = expect(socket, "system")
        assert event["text"].startswith("Player-")
        assert expect(socket, "room_state")["peers"][0].startswith("Player-")


@pytest.mark.parametrize("payload", [
    {"room": "ABCDE"}, {"room": "ABC1234"}, {"room": "ＡＢＣ１２３"},
    {"room": "ABC!23"}, {"room": 123456},
    {"room": "ABC123", "handle": "x" * 33},
    {"room": "ABC123", "handle": "Ali\nce"},
])
def test_invalid_join_payload(client, payload):
    with client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "join", "payload": payload})
        assert expect(socket, "error")["code"] == "INVALID_MSG"
    assert not app.state.rooms._rooms


def test_duplicate_names_and_repeated_join(client):
    with joined(client) as (alice, _):
        with client.websocket_connect("/ws") as duplicate:
            duplicate.send_json({"type": "join", "payload": {"room": "ABC123", "handle": "Alice"}})
            assert "already in use" in expect(duplicate, "error")["message"]
            with pytest.raises(WebSocketDisconnect):
                duplicate.receive_json()
        alice.send_json({"type": "join", "payload": {"room": "XYZ789"}})
        assert expect(alice, "error")["code"] == "INVALID_MSG"
        assert "XYZ789" not in app.state.rooms._rooms
        alice.send_json({"type": "chat", "payload": {"text": "🐙" * 500}})
        assert len(expect(alice, "chat")["text"]) == 500


def test_typing_and_reaction_broadcast(client):
    with joined(client, room=" abc123 ") as (alice, _):
        with joined(client, "Bob") as (bob, _):
            expect(alice, "system")
            expect(alice, "room_state")

            alice.send_json({"type": "typing", "payload": {"typing": True}})
            for socket in (alice, bob):
                assert expect(socket, "typing") == {"sender": "Alice", "typing": True}

            alice.send_json({"type": "typing", "payload": {"typing": False}})
            for socket in (alice, bob):
                assert expect(socket, "typing") == {"sender": "Alice", "typing": False}

            bob.send_json({"type": "reaction", "payload": {"emoji": "👍"}})
            for socket in (alice, bob):
                assert expect(socket, "reaction") == {"sender": "Bob", "emoji": "👍"}


@pytest.mark.parametrize("raw", [
    '{"type":"typing","payload":{"typing":"yes"}}',
    '{"type":"typing","payload":{}}',
    '{"type":"reaction","payload":{"emoji":"🔥"}}',
    '{"type":"reaction","payload":{"emoji":""}}',
    '{"type":"reaction","payload":{}}',
])
def test_invalid_typing_and_reaction_payload(client, raw):
    with joined(client) as (socket, _):
        socket.send_text(raw)
        assert expect(socket, "error")["code"] == "INVALID_MSG"


def test_typing_and_reaction_require_join():
    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "typing", "payload": {"typing": True}})
        assert expect(socket, "error")["message"] == "Send a join event first"
        socket.send_json({"type": "reaction", "payload": {"emoji": "👍"}})
        assert expect(socket, "error")["message"] == "Send a join event first"


def test_ping_before_join_does_not_error():
    """A queued ranked player has no room yet but still needs to ping to
    survive the idle timeout while waiting for a match.
    """
    with TestClient(app) as client, client.websocket_connect("/ws") as socket:
        socket.send_json({"type": "ping", "payload": {}})
        socket.send_json({"type": "join", "payload": {"room": "ABC123", "handle": "Alice"}})
        assert expect(socket, "system")["text"] == "Alice joined the room"


def test_rooms_are_isolated(client):
    with joined(client) as (alice, _), joined(client, "Bob", "XYZ789") as (bob, state):
        assert state["peers"] == ["Bob"]
        alice.send_json({"type": "chat", "payload": {"text": "only Alice"}})
        expect(alice, "chat")
        bob.send_json({"type": "chat", "payload": {"text": "only Bob"}})
        assert expect(bob, "chat")["text"] == "only Bob"


class FakeSocket:
    def __init__(self):
        self.events = []
        self.fail = False

    async def send_json(self, event):
        if self.fail:
            raise OSError("disconnected transport")
        self.events.append(event)

    async def close(self, code=1000):
        pass


def test_concurrent_capacity_and_failed_delivery_cleanup():
    async def scenario():
        manager = RoomManager()
        peers = [Peer(socket=FakeSocket()) for _ in range(3)]
        accepted = await asyncio.gather(*(
            manager.join(peer, "ABC123", f"Player-{i}") for i, peer in enumerate(peers)
        ))
        assert accepted.count(True) == 2
        assert len(manager._rooms["ABC123"].peers) == 2
        alice, bob = [p for p in peers if p.room]
        bob.socket.fail = True
        await manager.chat(alice, "test failed delivery")
        assert bob.room is None
        assert manager._rooms["ABC123"].peers == [alice]
        assert alice.socket.events[-2]["payload"]["text"] == f"{bob.handle} left the room"
        assert alice.socket.events[-1]["payload"]["status"] == "waiting"
        await manager.disconnect(bob)  # Idempotent after send failure.
        await manager.disconnect(alice)
        await manager.disconnect(alice)
        assert not manager._rooms

    asyncio.run(scenario())


def test_slow_connection_cannot_keep_a_room_occupied():
    class SlowSocket(FakeSocket):
        async def send_json(self, event):
            await asyncio.Event().wait()

    async def scenario():
        manager = RoomManager()
        manager.SEND_TIMEOUT = 0.01
        slow = Peer(socket=SlowSocket())
        assert not await manager.join(slow, "ABC123", "Slow")
        assert slow.room is None
        assert not manager._rooms
        replacement = Peer(socket=FakeSocket())
        assert await manager.join(replacement, "ABC123", "Replacement")
        await manager.disconnect(replacement)
        assert not manager._rooms

    asyncio.run(scenario())
