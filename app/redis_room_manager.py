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
import secrets
import time
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect
from redis.asyncio import Redis

CHANNEL_PREFIX = "chan:"
ROOM_KEY_PREFIX = "room:"
ROOM_TTL_SECONDS = 3600  # Safety net only; rooms are deleted explicitly on empty.

# KEYS[1] = room hash key. ARGV[1] = max peers. ARGV[2] = peer id. ARGV[3] = handle.
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
return 'OK'
"""


@dataclass(eq=False)
class Peer:
    socket: WebSocket
    id: str = field(default_factory=lambda: secrets.token_hex(8))
    handle: str = ""
    room: str | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RedisRoomManager:
    """Drop-in replacement for RoomManager, backed by Redis for shared state."""

    MAX_PEERS = 2
    SEND_TIMEOUT = 5

    def __init__(self, redis_url: str | None = None, *, client: Redis | None = None) -> None:
        self._redis = client if client is not None else Redis.from_url(redis_url, decode_responses=True)
        self._join_script = self._redis.register_script(_JOIN_SCRIPT)
        self._local_peers: dict[str, list[Peer]] = {}
        self._listener_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._listener_task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        await self._redis.aclose()

    async def connect(self, socket: WebSocket) -> Peer:
        await socket.accept()
        return Peer(socket=socket)

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

    async def _publish(self, code: str, event: dict) -> None:
        await self._redis.publish(f"{CHANNEL_PREFIX}{code}", json.dumps(event))

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
        for _ in range(10):
            attempt = handle or f"Player-{secrets.randbelow(10000):04d}"
            result = await self._join_script(
                keys=[key], args=[self.MAX_PEERS, peer.id, attempt, ROOM_TTL_SECONDS],
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
        code = peer.room
        if code is None:
            return
        local = self._local_peers.get(code, [])
        if peer in local:
            local.remove(peer)
            if not local:
                self._local_peers.pop(code, None)
        removed = await self._redis.hdel(self._room_key(code), peer.id)
        peer.room = None
        if not removed:
            return
        await self._publish(code, {
            "type": "system", "payload": {"text": f"{peer.handle} left the room"},
        })
        remaining = await self._redis.hlen(self._room_key(code))
        if remaining:
            await self._publish(code, await self._room_state_event(code))
        else:
            await self._redis.delete(self._room_key(code))

    async def _listen(self) -> None:
        async with self._redis.pubsub() as pubsub:
            await pubsub.psubscribe(f"{CHANNEL_PREFIX}*")
            async for message in pubsub.listen():
                if message["type"] != "pmessage":
                    continue
                code = message["channel"].removeprefix(CHANNEL_PREFIX)
                peers = self._local_peers.get(code)
                if not peers:
                    continue
                try:
                    event = json.loads(message["data"])
                except ValueError:
                    continue
                failed = []
                for p in list(peers):
                    if not await self._send_raw(p, event):
                        failed.append(p)
                for p in failed:
                    await self.disconnect(p)
                    await self.close(p, 1011)
