"""Simulates two Uvicorn workers sharing one Redis, each with its own
RedisRoomManager and its own local sockets, to prove cross-worker delivery
and capacity/handle checks are correct without real multi-process infra.
"""

import asyncio

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis

from app.redis_room_manager import Peer, RedisRoomManager


class FakeSocket:
    def __init__(self):
        self.events = []
        self.closed = None

    async def accept(self):
        pass

    async def send_json(self, event):
        self.events.append(event)

    async def close(self, code=1000):
        self.closed = code


@pytest.fixture
def two_workers():
    server = FakeServer()
    return (
        RedisRoomManager(client=FakeRedis(server=server, decode_responses=True)),
        RedisRoomManager(client=FakeRedis(server=server, decode_responses=True)),
    )


async def settle():
    # Let the pub/sub listener tasks process queued messages.
    await asyncio.sleep(0.05)


def run_with_workers(worker_a, worker_b, body):
    """Starts both managers' listener tasks in the same event loop as `body`."""

    async def wrapper():
        await worker_a.start()
        await worker_b.start()
        try:
            await body()
        finally:
            await worker_a.stop()
            await worker_b.stop()

    asyncio.run(wrapper())


def test_cross_worker_join_and_chat(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        bob = Peer(socket=FakeSocket())
        assert await worker_a.join(alice, "ABC123", "Alice")
        await settle()
        assert await worker_b.join(bob, "ABC123", "Bob")
        await settle()

        assert any(e["type"] == "system" and "Bob joined" in e["payload"]["text"] for e in alice.socket.events)
        assert any(e["type"] == "room_state" and e["payload"]["status"] == "paired" for e in alice.socket.events)
        assert any(e["type"] == "room_state" and e["payload"]["status"] == "paired" for e in bob.socket.events)

        await worker_a.chat(alice, "hello from worker A")
        await settle()
        assert any(e["type"] == "chat" and e["payload"]["text"] == "hello from worker A" for e in bob.socket.events)
        assert any(e["type"] == "chat" and e["payload"]["text"] == "hello from worker A" for e in alice.socket.events)

    run_with_workers(worker_a, worker_b, scenario)


def test_cross_worker_capacity_and_duplicate_handle(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        bob = Peer(socket=FakeSocket())
        assert await worker_a.join(alice, "ABC123", "Alice")
        assert await worker_b.join(bob, "ABC123", "Bob")

        third = Peer(socket=FakeSocket())
        assert not await worker_a.join(third, "ABC123", "Eve")
        assert any(e["payload"].get("code") == "ROOM_FULL" for e in third.socket.events)

        # A fresh room on a different code must still accept a first peer.
        fresh = Peer(socket=FakeSocket())
        assert await worker_b.join(fresh, "XYZ789", "Alice")
        assert fresh.room == "XYZ789"

        dup = Peer(socket=FakeSocket())
        assert not await worker_a.join(dup, "XYZ789", "Alice")
        assert any("already in use" in e["payload"].get("message", "") for e in dup.socket.events)

    run_with_workers(worker_a, worker_b, scenario)


def test_cross_worker_disconnect_notifies_other_worker(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        bob = Peer(socket=FakeSocket())
        await worker_a.join(alice, "ABC123", "Alice")
        await worker_b.join(bob, "ABC123", "Bob")
        await settle()

        await worker_a.disconnect(alice)
        await settle()

        assert any(e["type"] == "system" and "Alice left" in e["payload"]["text"] for e in bob.socket.events)
        assert any(
            e["type"] == "room_state" and e["payload"]["status"] == "waiting" and e["payload"]["peers"] == ["Bob"]
            for e in bob.socket.events
        )

    run_with_workers(worker_a, worker_b, scenario)
