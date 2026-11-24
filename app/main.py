"""HTTP entry points and WebSocket protocol dispatch."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from anyio import CancelScope
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from app.models import ChatEvent, JoinEvent, PingEvent, ReactionEvent, TypingEvent, client_event_adapter
from app.room_manager import RoomManager
from app.practice import auth
from app.practice.database import Database
from app.practice.ranked_routes import router as ranked_router
from app.practice.routes import router as practice_router

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"
MAX_FRAME_CHARS = 8192


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.database = Database()
    # REDIS_URL stays unset for solo chat and single-instance deploys; a
    # WebSocket connection cannot leave the worker process that accepted it,
    # so this only matters once there is more than one worker/instance, which
    # ranked mode's shared-room matches are the first feature to need. See
    # the Obsidian plan's "Distributed backend for ranked mode" section.
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        from app.practice.redis_ranked_queue import RedisRankedQueue
        from app.redis_room_manager import RedisRoomManager
        manager = RedisRoomManager(redis_url)
        await manager.start()
        app.state.rooms = manager
        ranked_queue = RedisRankedQueue(redis_url)
        await ranked_queue.start()
        app.state.ranked_queue = ranked_queue
        try:
            yield
        finally:
            await manager.stop()
            await ranked_queue.stop()
            app.state.database.close()
        return
    app.state.rooms = RoomManager()
    try:
        yield
    finally:
        app.state.database.close()


app = FastAPI(title="Pair / interview practice", lifespan=lifespan)
app.include_router(practice_router)
app.include_router(ranked_router)
app.mount("/assets", StaticFiles(directory=INDEX.parent / "practice"), name="assets")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if request.url.path.startswith("/assets/runtime/314.0.8/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.url.path in ("/", "/assets/app.js", "/assets/runtime-loader.js", "/assets/runner.html", "/assets/harness.py"):
        response.headers["Cache-Control"] = "no-cache"
    if request.url.path == "/assets/runner.html":
        response.headers["Content-Security-Policy"] = "default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' blob:; worker-src blob:; connect-src 'none'; frame-ancestors 'self'; sandbox allow-scripts"
    elif request.url.path == "/":
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    return response


@app.get("/", response_class=FileResponse)
async def index():
    return FileResponse(INDEX)


@app.get("/chat", response_class=FileResponse)
async def legacy_chat():
    return FileResponse(INDEX.with_name("chat.html"))


@app.get("/ranked", response_class=FileResponse)
async def ranked_test_page():
    """Manual test page for ranked mode: not part of the product UI, just
    a thin client over the real /api/ranked/* + /ws contract so the backend
    can be exercised by hand before Tyler builds the real frontend.
    """
    return FileResponse(INDEX.with_name("ranked.html"))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.websocket("/ws")
async def websocket_endpoint(socket: WebSocket):
    manager: RoomManager = socket.app.state.rooms
    peer = await manager.connect(socket)
    # Identifies the socket to its account, if any, so it can be reached by
    # user_id alone before joining any room — e.g. a queued ranked player
    # waiting for MATCH_FOUND. Best-effort: an absent/expired cookie just
    # leaves the peer anonymous, same as plain chat today.
    user, _login = auth.user_for_token(socket.app.state.database, socket.cookies.get(auth.COOKIE, ""))
    if user is not None:
        await manager.identify(peer, user.id)
    try:
        while True:
            # Browser pings keep an idle socket alive, joined or not — a
            # queued ranked player has no room yet but still needs to
            # survive the idle timeout while waiting for a match.
            frame = await asyncio.wait_for(socket.receive(), 60 if (peer.room or peer.user_id) else 15)
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
            elif isinstance(event, PingEvent):
                # One-way; the protocol has no pong event. Also refreshes
                # Redis-side presence so a crashed worker's peers can be
                # reaped. Allowed with no room joined (a queued ranked
                # player has none yet) unlike the other event types below.
                await manager.heartbeat(peer)
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
    except (WebSocketDisconnect, OSError):
        pass
    except TimeoutError:
        await manager.error(peer, "Connection timed out; join again")
        await manager.close(peer, 1008)
    finally:
        # Finish presence cleanup even if the connection task is cancelled.
        with CancelScope(shield=True):
            await manager.disconnect(peer)
