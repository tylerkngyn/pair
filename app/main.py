"""HTTP entry points and WebSocket protocol dispatch."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from anyio import CancelScope
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import ValidationError

from app.models import (
    ChatEvent, JoinEvent, PingEvent, ReactionEvent, TypingEvent, client_event_adapter,
)
from app.room_manager import RoomManager

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"
MAX_FRAME_CHARS = 8192


@asynccontextmanager
async def lifespan(app: FastAPI):
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        from app.redis_room_manager import RedisRoomManager
        manager = RedisRoomManager(redis_url)
        await manager.start()
        app.state.rooms = manager
        try:
            yield
        finally:
            await manager.stop()
        return
    app.state.rooms = RoomManager()
    yield


app = FastAPI(title="Pair / room chat", lifespan=lifespan)


@app.get("/", response_class=FileResponse)
async def index():
    return FileResponse(INDEX)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(socket: WebSocket):
    manager: RoomManager = socket.app.state.rooms
    peer = await manager.connect(socket)
    try:
        while True:
            # Browser pings keep joined sockets alive, including an idle chat.
            frame = await asyncio.wait_for(socket.receive(), 60 if peer.room else 15)
            if frame["type"] == "websocket.disconnect":
                break
            raw = frame.get("text")
            if raw is None or len(raw) > MAX_FRAME_CHARS:
                if not await manager.error(peer, "Expected a JSON text frame up to 8192 characters"):
                    break
                continue
            try:
                event = client_event_adapter.validate_json(raw)
            except ValidationError:
                if not await manager.error(peer, "Invalid event: use join, chat (1–500 characters), or ping with the required payload"):
                    break
                continue
            if isinstance(event, JoinEvent):
                if peer.room is not None:
                    await manager.error(peer, "Already joined; leave before joining another room")
                elif not await manager.join(peer, event.payload.room, event.payload.handle):
                    # End this attempt so the UI can retry with a fresh socket.
                    await manager.close(peer, 1008)
                    break
            elif peer.room is None:
                await manager.error(peer, "Send a join event first")
            elif isinstance(event, ChatEvent):
                await manager.chat(peer, event.payload.text)
                if peer.room is None:
                    break
            elif isinstance(event, TypingEvent):
                await manager.typing(peer, event.payload.typing)
            elif isinstance(event, ReactionEvent):
                await manager.react(peer, event.payload.emoji)
            elif isinstance(event, PingEvent):
                # One-way; the protocol has no pong event. Also refreshes
                # Redis-side presence so a crashed worker's peers can be reaped.
                await manager.heartbeat(peer)
    except (WebSocketDisconnect, OSError):
        pass
    except TimeoutError:
        await manager.error(peer, "Connection timed out; join again")
        await manager.close(peer, 1008)
    finally:
        # Finish presence cleanup even if the connection task is cancelled.
        with CancelScope(shield=True):
            await manager.disconnect(peer)
