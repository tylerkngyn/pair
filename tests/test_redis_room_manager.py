"""Simulates two Uvicorn workers sharing one Redis, each with its own
RedisRoomManager and its own local sockets, to prove cross-worker delivery
and capacity/handle checks are correct without real multi-process infra.
"""

import asyncio

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis
from redis.exceptions import RedisError

import app.redis_room_manager as redis_room_manager
from app.models import SystemEvent, SystemPayload
from app.redis_room_manager import Peer, RedisRoomManager


@pytest.fixture(autouse=True)
def fast_ack_wait(monkeypatch):
    # The real default (0.5s) is sized for production network latency; here
    # it would just slow down every "delivery correctly failed" test for no
    # benefit, since fakeredis has no real latency to wait out.
    monkeypatch.setattr(redis_room_manager, "ACK_WAIT_SECONDS", 0.05)


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
        await settle()  # Let both listener tasks finish subscribing before body() publishes anything.
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


def test_join_sets_presence_key(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        assert await worker_a.join(alice, "ABC123", "Alice")
        assert await worker_a._redis.exists(worker_a._presence_key("ABC123", alice.id))

    run_with_workers(worker_a, worker_b, scenario)


def test_heartbeat_refreshes_presence_ttl(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        await worker_a.join(alice, "ABC123", "Alice")
        await worker_a.heartbeat(alice)
        ttl = await worker_a._redis.ttl(worker_a._presence_key("ABC123", alice.id))
        assert ttl > 0

    run_with_workers(worker_a, worker_b, scenario)


def test_reap_is_noop_while_presence_alive(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        bob = Peer(socket=FakeSocket())
        await worker_a.join(alice, "ABC123", "Alice")
        await worker_b.join(bob, "ABC123", "Bob")
        await settle()
        alice.socket.events.clear()
        bob.socket.events.clear()

        await worker_b._reap_once()
        await settle()

        assert await worker_a._redis.hlen(worker_a._room_key("ABC123")) == 2
        assert alice.socket.events == []
        assert bob.socket.events == []

    run_with_workers(worker_a, worker_b, scenario)


def test_reap_removes_crashed_peer_and_frees_capacity(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        bob = Peer(socket=FakeSocket())
        await worker_a.join(alice, "ABC123", "Alice")
        await worker_b.join(bob, "ABC123", "Bob")
        await settle()

        # Simulate worker_a crashing: its peer's presence lapses, but no
        # clean disconnect() ever runs for it.
        await worker_a._redis.delete(worker_a._presence_key("ABC123", alice.id))

        await worker_b._reap_once()
        await settle()

        assert await worker_a._redis.hlen(worker_a._room_key("ABC123")) == 1
        assert any(e["type"] == "system" and "Alice left" in e["payload"]["text"] for e in bob.socket.events)
        assert any(
            e["type"] == "room_state" and e["payload"]["status"] == "waiting" and e["payload"]["peers"] == ["Bob"]
            for e in bob.socket.events
        )

        third = Peer(socket=FakeSocket())
        assert await worker_b.join(third, "ABC123", "Eve")

    run_with_workers(worker_a, worker_b, scenario)


def test_reap_deletes_room_when_last_peer_stale(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        await worker_a.join(alice, "ABC123", "Alice")
        await worker_a._redis.delete(worker_a._presence_key("ABC123", alice.id))

        await worker_b._reap_once()

        assert not await worker_a._redis.exists(worker_a._room_key("ABC123"))

    run_with_workers(worker_a, worker_b, scenario)


def test_reap_and_disconnect_race_is_idempotent(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        bob = Peer(socket=FakeSocket())
        await worker_a.join(alice, "ABC123", "Alice")
        await worker_b.join(bob, "ABC123", "Bob")
        await settle()
        bob.socket.events.clear()

        await worker_a._redis.delete(worker_a._presence_key("ABC123", alice.id))
        await worker_a.disconnect(alice)
        await settle()
        await worker_b._reap_once()
        await settle()

        left_events = [e for e in bob.socket.events if e["type"] == "system" and "Alice left" in e["payload"]["text"]]
        assert len(left_events) == 1

    run_with_workers(worker_a, worker_b, scenario)


def test_reap_of_locally_held_peer_closes_its_socket(two_workers):
    """A sweep can land on a peer the sweeping worker still holds locally
    (a stuck process, not a true crash on another worker). That peer must
    stop receiving events and its socket must close, or it becomes a
    zombie: still live and chatting while Redis has forgotten it, letting
    a third peer join a room that already has two live sockets.
    """
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        bob = Peer(socket=FakeSocket())
        await worker_a.join(alice, "ABC123", "Alice")
        await worker_b.join(bob, "ABC123", "Bob")
        await settle()

        # Simulate alice's presence lapsing without disconnect() ever running,
        # even though worker_a still holds her socket locally.
        await worker_a._redis.delete(worker_a._presence_key("ABC123", alice.id))
        await worker_a._reap_once()
        await settle()

        assert alice.id not in [p.id for p in worker_a._local_peers.get("ABC123", [])]
        assert alice.socket.closed == 1011
        assert alice.room is None

        # Capacity is genuinely free: a third peer can join.
        third = Peer(socket=FakeSocket())
        assert await worker_b.join(third, "ABC123", "Eve")

        # The zombie must not go on receiving room events.
        events_before = len(alice.socket.events)
        await worker_b.chat(bob, "hello after reap")
        await settle()
        assert len(alice.socket.events) == events_before

    run_with_workers(worker_a, worker_b, scenario)


def test_reap_evicts_peer_held_by_a_different_worker(two_workers):
    """The sweeping worker only ever sees Redis membership directly; the
    actual live peer may be held locally by a DIFFERENT worker than the
    one whose sweep notices the lapsed presence key. That other worker
    must still be told to drop and close it, or it becomes a zombie: live
    and chatting on worker_a while worker_b's sweep has already told Redis
    (and everyone else) that it's gone.
    """
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        bob = Peer(socket=FakeSocket())
        await worker_a.join(alice, "ABC123", "Alice")
        await worker_b.join(bob, "ABC123", "Bob")
        await settle()

        await worker_a._redis.delete(worker_a._presence_key("ABC123", alice.id))
        # worker_b's sweep notices and removes alice from Redis, even though
        # alice's actual socket is held locally by worker_a, not worker_b.
        await worker_b._reap_once()
        await settle()

        assert alice.id not in [p.id for p in worker_a._local_peers.get("ABC123", [])]
        assert alice.socket.closed == 1011
        assert alice.room is None

        third = Peer(socket=FakeSocket())
        assert await worker_a.join(third, "ABC123", "Eve")

        events_before = len(alice.socket.events)
        await worker_b.chat(bob, "hello after cross-worker reap")
        await settle()
        assert len(alice.socket.events) == events_before

    run_with_workers(worker_a, worker_b, scenario)


def test_heartbeat_extends_room_ttl(two_workers):
    """A long-lived, active, paired room must not expire out from under two
    still-connected peers just because no third join has happened recently
    to refresh the room hash's own safety-net TTL.
    """
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        await worker_a.join(alice, "ABC123", "Alice")

        await worker_a._redis.expire(worker_a._room_key("ABC123"), 5)
        await worker_a.heartbeat(alice)

        ttl = await worker_a._redis.ttl(worker_a._room_key("ABC123"))
        assert ttl > 5

    run_with_workers(worker_a, worker_b, scenario)


def test_send_to_user_reaches_account_on_a_different_worker(two_workers):
    """The case ranked matchmaking needs: a queued account has no room yet,
    may be held by a different worker than the one handling the match, and
    must still receive a push addressed to its user_id alone.
    """
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        await worker_a.identify(alice, "user-alice")

        delivered = await worker_b.send_to_user("user-alice", SystemEvent(payload=SystemPayload(text="match found")))
        await settle()

        assert delivered
        assert any(e["type"] == "system" and e["payload"]["text"] == "match found" for e in alice.socket.events)

    run_with_workers(worker_a, worker_b, scenario)


def test_send_to_user_unknown_account_reports_undelivered(two_workers):
    """No subscriber anywhere ever sets the ack key, so this must report
    False rather than the old always-True behavior — a caller treating the
    return value as a real delivery confirmation (ranked_routes.py's
    _PENDING_MATCH cleanup) would otherwise wrongly believe it arrived.
    """
    worker_a, worker_b = two_workers

    async def scenario():
        delivered = await worker_a.send_to_user("nobody-queued", SystemEvent(payload=SystemPayload(text="match found")))
        assert delivered is False

    run_with_workers(worker_a, worker_b, scenario)


def test_send_to_user_failed_local_send_reports_undelivered(two_workers):
    """The target account exists on another worker, but its socket send
    actually fails there — the ack must only be set on a *successful*
    _send_raw, not merely on finding a local peer to attempt delivery to.
    """
    worker_a, worker_b = two_workers

    class BrokenSocket(FakeSocket):
        async def send_json(self, event):
            raise RuntimeError("socket is closed")

    async def scenario():
        alice = Peer(socket=BrokenSocket())
        await worker_a.identify(alice, "user-alice")

        delivered = await worker_b.send_to_user("user-alice", SystemEvent(payload=SystemPayload(text="match found")))
        assert delivered is False

    run_with_workers(worker_a, worker_b, scenario)


def test_disconnect_clears_user_identity(two_workers):
    worker_a, worker_b = two_workers

    async def scenario():
        alice = Peer(socket=FakeSocket())
        await worker_a.identify(alice, "user-alice")
        await worker_a.disconnect(alice)

        delivered = await worker_b.send_to_user("user-alice", SystemEvent(payload=SystemPayload(text="match found")))
        await settle()

        assert delivered is False
        assert alice.socket.events == []

    run_with_workers(worker_a, worker_b, scenario)


class _DroppedConnection:
    """An async context manager standing in for self._redis.pubsub() that
    simulates a Redis connection failing right as the listener tries to
    (re)subscribe - the exact moment _listen's own psubscribe call would
    raise RedisError on a real dropped connection.
    """

    async def __aenter__(self):
        raise RedisError("simulated Redis connection drop")

    async def __aexit__(self, *exc_info):
        pass


def test_listener_survives_a_dropped_connection_and_resumes(two_workers, monkeypatch):
    """Regression test for the listener task silently dying on any Redis
    hiccup. Before the RECONNECT_BACKOFF_SECONDS/RedisError retry existed,
    this exact scenario (pubsub() raising once, then working again) left
    worker_a's listener task dead forever - alice, held locally on
    worker_a, would never receive anything again, with nothing anywhere
    signaling it. Confirmed to fail without the fix: see the commit that
    added the try/except for the before/after run.
    """
    worker_a, worker_b = two_workers
    monkeypatch.setattr(redis_room_manager, "RECONNECT_BACKOFF_SECONDS", 0.05)

    real_pubsub = worker_a._redis.pubsub
    calls = {"count": 0}

    def flaky_pubsub(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return _DroppedConnection()
        return real_pubsub(*args, **kwargs)

    monkeypatch.setattr(worker_a._redis, "pubsub", flaky_pubsub)

    async def scenario():
        alice = Peer(socket=FakeSocket())
        await worker_a.identify(alice, "user-alice")
        # Long enough for the first (failing) subscribe attempt, the
        # RECONNECT_BACKOFF_SECONDS sleep, and the second (successful) one.
        await asyncio.sleep(0.3)

        delivered = await worker_b.send_to_user("user-alice", SystemEvent(payload=SystemPayload(text="hello")))
        assert delivered is True
        assert calls["count"] >= 2
        assert any(e["type"] == "system" and e["payload"]["text"] == "hello" for e in alice.socket.events)

    run_with_workers(worker_a, worker_b, scenario)


def test_reap_loop_survives_a_redis_error_and_keeps_reaping(two_workers, monkeypatch):
    """Regression test for the reap loop dying the same way the listener
    could: one _reap_once() call raising RedisError must not stop the
    REAP_INTERVAL_SECONDS loop from trying again next tick.
    """
    worker_a, worker_b = two_workers
    monkeypatch.setattr(redis_room_manager, "REAP_INTERVAL_SECONDS", 0.05)

    real_reap_once = worker_a._reap_once
    calls = {"count": 0}

    async def flaky_reap_once():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RedisError("simulated Redis error during a reap sweep")
        await real_reap_once()

    monkeypatch.setattr(worker_a, "_reap_once", flaky_reap_once)

    async def scenario():
        # Two sweeps' worth of waiting: the first raises and is caught: the
        # loop must still be alive to run a second one.
        await asyncio.sleep(0.2)
        assert calls["count"] >= 2
        assert not worker_a._reap_task.done()

    run_with_workers(worker_a, worker_b, scenario)
