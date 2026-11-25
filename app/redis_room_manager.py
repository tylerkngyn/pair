"""Multi-worker room manager: membership in Redis, delivery via Redis Pub/Sub.

A WebSocket connection cannot leave the worker process that accepted it, so
this manager never ships sockets around. Instead, membership (who is in
which room) lives in Redis as shared state, and every event is published to
a per-room channel; each worker's single listener task re-delivers incoming
events only to the local sockets it actually holds. Capacity and duplicate
handle checks run as a single Lua script so concurrent joins from different
workers cannot both succeed for the same slot.
"""

import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.observability import METRICS, log_event

logger = logging.getLogger(__name__)

# Backoff before retrying the pub/sub listener or a reap sweep after a Redis
# connection error, so a dropped connection is logged and retried rather than
# silently killing the background task forever (the single biggest risk of
# Redis being this deployment's one shared dependency — see README).
RECONNECT_BACKOFF_SECONDS = 2

CHANNEL_PREFIX = "chan:"
USER_CHANNEL_PREFIX = "user:"
ACK_KEY_PREFIX = "ack:"
ROOM_KEY_PREFIX = "room:"
PRESENCE_KEY_PREFIX = "presence:"
ROOM_TTL_SECONDS = 3600  # Safety net only; rooms are deleted explicitly on empty.

ACK_WAIT_SECONDS = float(os.environ.get("PAIR_ACK_WAIT_SECONDS", "0.5"))
ACK_POLL_INTERVAL_SECONDS = 0.02
ACK_TTL_SECONDS = 5  # Only needs to outlive the wait above; a generous margin.

# Overridable only for integration tests, to avoid waiting out the real
# ~2 minute worst case; production defaults are unchanged.
PRESENCE_TTL_SECONDS = int(os.environ.get("PAIR_PRESENCE_TTL_SECONDS", "90"))
# ^ Browsers throttle backgrounded tabs' setInterval, often to ~60s; must
#   comfortably outlive that, not just one missed 20s ping.
REAP_INTERVAL_SECONDS = int(os.environ.get("PAIR_REAP_INTERVAL_SECONDS", "20"))
# ^ Worst-case detection ~= PRESENCE_TTL + this, roughly two minutes by default.

# KEYS[1] = room hash key. KEYS[2] = presence key.
# ARGV[1] = max peers. ARGV[2] = peer id. ARGV[3] = handle.
# ARGV[4] = room ttl. ARGV[5] = presence ttl.
_JOIN_SCRIPT = """
local count = redis.call('HLEN', KEYS[1])
if count >= tonumber(ARGV[1]) then
  return 'FULL'
end
local existing = redis.call('HVALS', KEYS[1])
for i = 1, #existing do
  if existing[i] == ARGV[3] then
    return 'DUP'
  end
end
redis.call('HSET', KEYS[1], ARGV[2], ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
redis.call('SET', KEYS[2], 1, 'EX', ARGV[5])
return 'OK'
"""


@dataclass(eq=False)
class Peer:
    socket: WebSocket
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    handle: str = ""
    room: str | None = None
    user_id: str | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RedisRoomManager:
    """Drop-in replacement for RoomManager, backed by Redis for shared state."""

    MAX_PEERS = 2
    SEND_TIMEOUT = 5

    def __init__(self, redis_url: str | None = None, *, client: Redis | None = None) -> None:
        self._redis = client if client is not None else Redis.from_url(redis_url, decode_responses=True)
        self._join_script = self._redis.register_script(_JOIN_SCRIPT)
        self._local_peers: dict[str, list[Peer]] = {}
        # Routing only, not shared membership: which account maps to which
        # locally-held socket. Never written to Redis — a stale entry here
        # just means delivery misses until the peer reconnects and
        # re-identifies, same failure mode as a dropped Pub/Sub message.
        self._local_users: dict[str, Peer] = {}
        self._listener_task: asyncio.Task | None = None
        self._reap_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._listener_task = asyncio.create_task(self._listen())
        self._reap_task = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        for task in (self._listener_task, self._reap_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        await self._redis.aclose()

    async def connect(self, socket: WebSocket) -> Peer:
        await socket.accept()
        peer = Peer(socket=socket)
        METRICS.ws_connections_active.inc()
        log_event("ws_connect", peer_id=peer.id)
        return peer

    async def _send_raw(self, peer: Peer, event: dict) -> bool:
        try:
            async with peer.send_lock:
                await asyncio.wait_for(
                    peer.socket.send_json(event), self.SEND_TIMEOUT
                )
            return True
        except (WebSocketDisconnect, OSError, RuntimeError, TimeoutError):
            return False

    async def send(self, peer: Peer, event) -> bool:
        return await self._send_raw(peer, event.model_dump())

    async def close(self, peer: Peer, code: int = 1000) -> None:
        try:
            async with peer.send_lock:
                await asyncio.wait_for(peer.socket.close(code=code), self.SEND_TIMEOUT)
        except (WebSocketDisconnect, OSError, RuntimeError, TimeoutError):
            pass

    async def error(self, peer: Peer, message: str, *, full: bool = False) -> bool:
        return await self._send_raw(peer, {
            "type": "error",
            "payload": {"code": "ROOM_FULL" if full else "INVALID_MSG", "message": message},
        })

    def _room_key(self, code: str) -> str:
        return f"{ROOM_KEY_PREFIX}{code}"

    def _presence_key(self, code: str, peer_id: str) -> str:
        return f"{PRESENCE_KEY_PREFIX}{code}:{peer_id}"

    async def _publish(self, code: str, event: dict) -> None:
        await self._redis.publish(f"{CHANNEL_PREFIX}{code}", json.dumps(event))

    async def identify(self, peer: Peer, user_id: str) -> None:
        """Associate a peer with an account so it can be reached by user_id
        alone, before (or without) ever joining a room — e.g. a queued
        ranked player waiting for MATCH_FOUND, which may be handled by a
        different worker than the one that eventually finds the match.
        """
        peer.user_id = user_id
        self._local_users[user_id] = peer

    async def send_to_user(self, user_id: str, event) -> bool:
        """Publishes to every worker; only the one holding this user_id
        locally (if any) actually delivers it. Unlike room events, this
        is point-to-point — no membership state, no eviction notice.

        Confirms real delivery across workers via a one-shot ack key: the
        envelope carries a nonce, whichever worker's _listen successfully
        delivers it locally sets `ack:<nonce>` in Redis, and this polls for
        that key up to ACK_WAIT_SECONDS before giving up. A publish with no
        subscriber anywhere, or a target worker that fails to actually
        write to the socket, correctly reports False — callers (e.g.
        ranked_routes.py's _PENDING_MATCH cleanup) can treat a True return
        here exactly like RoomManager's.
        """
        nonce = secrets.token_hex(8)
        envelope = {"nonce": nonce, "event": event.model_dump()}
        await self._redis.publish(f"{USER_CHANNEL_PREFIX}{user_id}", json.dumps(envelope))
        ack_key = f"{ACK_KEY_PREFIX}{nonce}"
        deadline = time.monotonic() + ACK_WAIT_SECONDS
        while time.monotonic() < deadline:
            if await self._redis.exists(ack_key):
                await self._redis.delete(ack_key)
                return True
            await asyncio.sleep(ACK_POLL_INTERVAL_SECONDS)
        await self._redis.delete(ack_key)  # Covers a late ack arriving after we already gave up.
        return False

    async def _room_state_event(self, code: str) -> dict:
        handles = list((await self._redis.hgetall(self._room_key(code))).values())
        return {
            "type": "room_state",
            "payload": {
                "room": code,
                "status": "paired" if len(handles) == self.MAX_PEERS else "waiting",
                "peers": handles,
            },
        }

    async def join(self, peer: Peer, code: str, handle: str) -> bool:
        if peer.room is not None:
            await self.error(peer, "Already joined; leave before joining another room")
            return False

        key = self._room_key(code)
        presence_key = self._presence_key(code, peer.id)
        for _ in range(10):
            attempt = handle or f"Player-{secrets.randbelow(10000):04d}"
            result = await self._join_script(
                keys=[key, presence_key],
                args=[self.MAX_PEERS, peer.id, attempt, ROOM_TTL_SECONDS, PRESENCE_TTL_SECONDS],
            )
            if result == "FULL":
                await self.error(peer, "Room is full", full=True)
                return False
            if result == "DUP":
                if handle:
                    await self.error(peer, "Display name already in use; choose another")
                    return False
                continue  # Random name collided; the script already retried nothing, so loop.
            peer.handle, peer.room = attempt, code
            self._local_peers.setdefault(code, []).append(peer)
            await self._publish(code, {
                "type": "system", "payload": {"text": f"{attempt} joined the room"},
            })
            await self._publish(code, await self._room_state_event(code))
            return True
        await self.error(peer, "Could not assign a display name; try again")
        return False

    async def heartbeat(self, peer: Peer) -> None:
        if peer.room is None:
            return
        await self._redis.set(
            self._presence_key(peer.room, peer.id), 1, ex=PRESENCE_TTL_SECONDS,
        )
        # Also extend the room hash's own safety-net TTL, or an active,
        # long-lived paired session would still get wiped after ROOM_TTL_SECONDS
        # regardless of how recently either peer pinged.
        await self._redis.expire(self._room_key(peer.room), ROOM_TTL_SECONDS)

    async def chat(self, peer: Peer, text: str) -> None:
        if peer.room is None:
            await self.error(peer, "Join a room before sending messages")
            return
        await self._publish(peer.room, {
            "type": "chat",
            "payload": {"sender": peer.handle, "text": text, "timestamp": int(time.time())},
        })

    async def typing(self, peer: Peer, is_typing: bool) -> None:
        if peer.room is None:
            return
        await self._publish(peer.room, {
            "type": "typing", "payload": {"sender": peer.handle, "typing": is_typing},
        })

    async def react(self, peer: Peer, emoji: str) -> None:
        if peer.room is None:
            return
        await self._publish(peer.room, {
            "type": "reaction", "payload": {"sender": peer.handle, "emoji": emoji},
        })

    async def disconnect(self, peer: Peer) -> None:
        """A peer's own clean disconnect. The caller owns that peer's socket
        lifecycle already (it's closing or already closed), so this only
        drops bookkeeping and Redis membership — it never re-closes the
        socket itself, unlike _remove_peer used by reap/eviction below.
        """
        METRICS.ws_connections_active.dec()
        log_event("ws_disconnect", peer_id=peer.id, room=peer.room)
        if peer.user_id is not None and self._local_users.get(peer.user_id) is peer:
            del self._local_users[peer.user_id]
        code = peer.room
        if code is None:
            return
        peer.room = None
        self._drop_local(code, peer.id)
        await self._remove_from_redis(code, peer.id, peer.handle)

    def _drop_local(self, code: str, peer_id: str) -> Peer | None:
        """Remove a peer from this worker's own bookkeeping, if held locally."""
        local = self._local_peers.get(code, [])
        match = next((p for p in local if p.id == peer_id), None)
        if match is not None:
            local.remove(match)
            if not local:
                self._local_peers.pop(code, None)
        return match

    async def _remove_from_redis(self, code: str, peer_id: str, handle: str) -> None:
        """Drop a peer from Redis room membership and notify the room.

        HDEL returning 0 means another caller (a disconnect racing a reap
        sweep, or two sweeps) already removed this peer, so the rest is a
        no-op — whoever's HDEL actually removed it owns the notifications.
        """
        key = self._room_key(code)
        removed = await self._redis.hdel(key, peer_id)
        await self._redis.delete(self._presence_key(code, peer_id))
        if not removed:
            return
        # Tells every worker (including ones that still hold this peer
        # locally, e.g. it's live on a *different* worker than the one
        # that just removed it) to drop it — see _handle_evict.
        await self._publish(code, {"type": "_evict", "payload": {"peer_id": peer_id}})
        await self._publish(code, {
            "type": "system", "payload": {"text": f"{handle} left the room"},
        })
        remaining = await self._redis.hlen(key)
        if remaining:
            await self._publish(code, await self._room_state_event(code))
        else:
            await self._redis.delete(key)

    async def _remove_peer(self, code: str, peer_id: str, handle: str) -> None:
        """Used by reap sweeps: removes from Redis AND closes a locally-held
        peer's socket, since (unlike disconnect()) nothing else owns that
        socket's lifecycle — its presence lapsed with no clean disconnect
        ever running, e.g. the process got stuck.
        """
        stale_local = self._drop_local(code, peer_id)
        await self._remove_from_redis(code, peer_id, handle)
        if stale_local is not None:
            stale_local.room = None
            await self.close(stale_local, 1011)

    async def _handle_evict(self, code: str, peer_id: str) -> None:
        """Another worker's disconnect/reap already removed this peer from
        Redis. If *this* worker still holds it locally (it's live here,
        just owned by a different worker than the one that removed it),
        drop and close it so it can never keep chatting after Redis has
        forgotten it.
        """
        stale_local = self._drop_local(code, peer_id)
        if stale_local is not None:
            stale_local.room = None
            await self.close(stale_local, 1011)

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(REAP_INTERVAL_SECONDS)
            try:
                await self._reap_once()
            except RedisError:
                METRICS.redis_reconnects_total.inc()
                log_event("redis_reconnect", source="reap_loop")
                logger.warning("Reap sweep failed; Redis unreachable, will retry next interval", exc_info=True)

    async def _reap_once(self) -> None:
        """Remove peers whose presence key lapsed, e.g. from a crashed worker.

        Scans the whole room keyspace, not just locally-held rooms: a
        crashed worker's own bookkeeping is gone, so only another worker's
        sweep can notice and clean up its peers.
        """
        async for key in self._redis.scan_iter(match=f"{ROOM_KEY_PREFIX}*"):
            code = key.removeprefix(ROOM_KEY_PREFIX)
            members = await self._redis.hgetall(key)
            if not members:
                continue
            # One pipelined round trip for every member's presence check,
            # instead of one EXISTS per peer, so a sweep's cost doesn't
            # scale with total members across all rooms times a round trip.
            pipeline = self._redis.pipeline(transaction=False)
            for peer_id in members:
                pipeline.exists(self._presence_key(code, peer_id))
            alive = await pipeline.execute()
            for (peer_id, handle), is_alive in zip(members.items(), alive):
                if not is_alive:
                    await self._remove_peer(code, peer_id, handle)

    async def _listen(self) -> None:
        """Runs for the manager's lifetime. A dropped Redis connection would
        otherwise raise out of `pubsub.listen()` and silently kill this
        background task forever — every local peer would stop receiving any
        room/user events with no signal anywhere. Log it and resubscribe
        instead.
        """
        while True:
            try:
                async with self._redis.pubsub() as pubsub:
                    await pubsub.psubscribe(f"{CHANNEL_PREFIX}*")
                    await pubsub.psubscribe(f"{USER_CHANNEL_PREFIX}*")
                    async for message in pubsub.listen():
                        await self._handle_message(message)
            except RedisError:
                METRICS.redis_reconnects_total.inc()
                log_event("redis_reconnect", source="listener")
                logger.warning(
                    "Pub/sub listener lost its Redis connection; reconnecting in %ss",
                    RECONNECT_BACKOFF_SECONDS, exc_info=True,
                )
                await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)

    async def _handle_message(self, message: dict) -> None:
        if message["type"] != "pmessage":
            return
        channel = message["channel"]
        try:
            event = json.loads(message["data"])
        except ValueError:
            return
        if channel.startswith(USER_CHANNEL_PREFIX):
            peer = self._local_users.get(channel.removeprefix(USER_CHANNEL_PREFIX))
            if peer is not None and await self._send_raw(peer, event["event"]):
                await self._redis.set(f"{ACK_KEY_PREFIX}{event['nonce']}", 1, ex=ACK_TTL_SECONDS)
                METRICS.messages_fanned_out_total.inc()
            return
        code = channel.removeprefix(CHANNEL_PREFIX)
        if event.get("type") == "_evict":
            await self._handle_evict(code, event["payload"]["peer_id"])
            return
        peers = self._local_peers.get(code)
        if not peers:
            return
        start = time.perf_counter()
        log_event("message_fanout", room=code, event_type=event.get("type"), recipients=len(peers))
        failed = []
        for p in list(peers):
            if not await self._send_raw(p, event):
                failed.append(p)
        METRICS.messages_fanned_out_total.inc(len(peers) - len(failed))
        METRICS.fanout_latency_seconds.observe(time.perf_counter() - start)
        for p in failed:
            await self.disconnect(p)
            await self.close(p, 1011)
