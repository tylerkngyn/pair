"""Process-local presence and delivery; routes never access room storage.

A Redis implementation will need atomic shared membership and pub/sub delivery,
while actual WebSocket connections remain local to each worker.
"""

import asyncio
import secrets
import time
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect

from app.metrics import Metrics
from app.models import (
    ChatBroadcast, ChatPayload, ErrorEvent, ErrorPayload, ReactionBroadcast,
    ReactionPayload, RoomStateEvent, RoomStatePayload, ServerEvent,
    SystemEvent, SystemPayload, TypingBroadcast, TypingPayload,
)


@dataclass(eq=False)
class Peer:
    socket: WebSocket
    handle: str = ""
    room: str | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class Room:
    code: str
    peers: list[Peer] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RoomManager:
    MAX_PEERS = 2
    SEND_TIMEOUT = 5

    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}
        self.metrics = Metrics()
        self.metrics.set_rooms_active_getter(lambda: len(self._rooms))

    async def connect(self, socket: WebSocket) -> Peer:
        await socket.accept()
        return Peer(socket=socket)

    async def send(self, peer: Peer, event: ServerEvent) -> bool:
        """Serialize sends per socket; a slow/broken peer cannot stall forever."""
        try:
            async with peer.send_lock:
                await asyncio.wait_for(
                    peer.socket.send_json(event.model_dump()), self.SEND_TIMEOUT
                )
            return True
        except (WebSocketDisconnect, OSError, RuntimeError, TimeoutError):
            return False

    async def close(self, peer: Peer, code: int = 1000) -> None:
        try:
            async with peer.send_lock:
                await asyncio.wait_for(peer.socket.close(code=code), self.SEND_TIMEOUT)
        except (WebSocketDisconnect, OSError, RuntimeError, TimeoutError):
            pass

    async def error(self, peer: Peer, message: str, *, full: bool = False) -> bool:
        return await self.send(peer, ErrorEvent(payload=ErrorPayload(
            code="ROOM_FULL" if full else "INVALID_MSG", message=message,
        )))

    async def join(self, peer: Peer, code: str, handle: str) -> bool:
        if peer.room is not None:
            await self.error(peer, "Already joined; leave before joining another room")
            return False

        # A waiting join may hold a reference to a room removed by disconnect.
        # Retry under the current room's lock rather than reviving stale state.
        while True:
            room = self._rooms.setdefault(code, Room(code=code))
            async with room.lock:
                if self._rooms.get(code) is not room:
                    continue
                if len(room.peers) >= self.MAX_PEERS:
                    await self.error(peer, "Room is full", full=True)
                    return False
                names = {member.handle for member in room.peers}
                if not handle:
                    while not handle or handle in names:
                        handle = f"Player-{secrets.randbelow(10000):04d}"
                if handle in names:
                    await self.error(peer, "Display name already in use; choose another")
                    return False
                peer.handle, peer.room = handle, code
                room.peers.append(peer)
                self.metrics.record_join()
                await self._publish(room, SystemEvent(payload=SystemPayload(
                    text=f"{handle} joined the room",
                )))
                return peer.room is not None

    async def heartbeat(self, peer: Peer) -> None:
        """No-op here; only RedisRoomManager needs a liveness signal to reap."""

    async def chat(self, peer: Peer, text: str) -> None:
        code = peer.room
        room = self._rooms.get(code) if code is not None else None
        if room is None:
            await self.error(peer, "Join a room before sending messages")
            return
        async with room.lock:
            if peer not in room.peers:
                return
            await self._publish(room, ChatBroadcast(payload=ChatPayload(
                sender=peer.handle, text=text, timestamp=int(time.time()),
            )))

    async def typing(self, peer: Peer, is_typing: bool) -> None:
        code = peer.room
        room = self._rooms.get(code) if code is not None else None
        if room is None:
            return
        async with room.lock:
            if peer not in room.peers:
                return
            await self._publish(room, TypingBroadcast(payload=TypingPayload(
                sender=peer.handle, typing=is_typing,
            )))

    async def react(self, peer: Peer, emoji: str) -> None:
        code = peer.room
        room = self._rooms.get(code) if code is not None else None
        if room is None:
            return
        async with room.lock:
            if peer not in room.peers:
                return
            await self._publish(room, ReactionBroadcast(payload=ReactionPayload(
                sender=peer.handle, emoji=emoji,
            )))

    async def disconnect(self, peer: Peer) -> None:
        code = peer.room
        room = self._rooms.get(code) if code is not None else None
        if room is None:
            return
        async with room.lock:
            if peer not in room.peers:
                return
            room.peers.remove(peer)
            peer.room = None
            await self._publish(room, SystemEvent(payload=SystemPayload(
                text=f"{peer.handle} left the room",
            )))

    async def _publish(self, room: Room, event: ServerEvent) -> None:
        """Called under the room lock; prune failed sends and refresh presence.

        Failures during either event delivery or state delivery produce departure
        notices for survivors. Iterate rather than recursively acquiring a lock.
        """
        events: list[ServerEvent] = [event]
        refresh_state = not isinstance(event, (ChatBroadcast, TypingBroadcast, ReactionBroadcast))
        while room.peers:
            failed: list[Peer] = []
            for outgoing in events:
                peers = [peer for peer in room.peers if peer not in failed]
                delivered = await asyncio.gather(*(self.send(p, outgoing) for p in peers))
                failed.extend(p for p, ok in zip(peers, delivered) if not ok)
            if not failed:
                if not refresh_state:
                    return
                state = RoomStateEvent(payload=RoomStatePayload(
                    room=room.code,
                    status="paired" if len(room.peers) == self.MAX_PEERS else "waiting",
                    peers=[peer.handle for peer in room.peers],
                ))
                peers = list(room.peers)
                delivered = await asyncio.gather(*(self.send(p, state) for p in peers))
                failed = [p for p, ok in zip(peers, delivered) if not ok]
            if not failed:
                return
            refresh_state = True
            events = []
            for peer in failed:
                room.peers.remove(peer)
                peer.room = None
                events.append(SystemEvent(payload=SystemPayload(
                    text=f"{peer.handle} left the room",
                )))
            await asyncio.gather(*(self.close(peer, 1011) for peer in failed))

        # No await between finding and deleting the room; joins recheck identity.
        if self._rooms.get(room.code) is room:
            del self._rooms[room.code]
