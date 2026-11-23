"""HTTP entry points and WebSocket protocol dispatch."""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from anyio import CancelScope
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import ValidationError

# app.models is imported first: it in turn imports app.contracts.game, and
# app.contracts.game imports StrictModel back from app.models, so whichever
# of these two modules starts loading first must be able to complete its
# own top-level class definitions before the other needs its names. Loading
# app.models first satisfies that (StrictModel is defined before app.models
# reaches its own `from app.contracts.game import ...` line).
from app.models import (
    ChatEvent, GameSyncEvent, GameStartEvent, GuessEvent, JoinEvent, PingEvent,
    ReactionEvent, RoundReadyEvent, TypingEvent, client_event_adapter,
)
from app.contracts.game import CommandContext, CommandResult, GameState, GameStateEvent, GuessResultEvent
from app.room_manager import RoomManager

INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"
DASHBOARD = Path(__file__).resolve().parent.parent / "static" / "dashboard.html"
MAX_FRAME_CHARS = 8192

# --- Game skeleton wiring: PLACEHOLDER, not real game logic -----------------
# No GameEngine/ScoreProvider implementation exists yet, and GameRepository
# implementations (app/game_repository/memory.py, redis.py) are being built
# separately. This block exists only to prove the four new command types
# (game_start/round_ready/guess/game_sync) can be validated and dispatched
# end-to-end and get a response back — it is NOT the real apply/commit path.
# _GAME_STATE_STORE stands in for a GameRepository's compare_and_set: a
# plain dict, no versioning/concurrency check, no idempotent replay via
# applied_request_ids. Whoever wires in the real GameEngine/ScoreProvider
# should delete this dict and _placeholder_apply entirely and replace the
# dispatch below with: load() -> engine.apply(state, command, context,
# scores) -> compare_and_set(...) -> publish events via RoomTransport.
_GAME_STATE_STORE: dict[str, GameState] = {}


def _placeholder_apply(peer, room_code: str, request_id: str) -> GameStateEvent:
    """No-op "transition": bump version (or create a fresh lobby state) and
    echo it back. Does not run GameEngine.apply, does not consult
    ScoreProvider, does not touch round_id/phase transitions for real."""
    context = CommandContext(peer_id=peer.handle, now_ms=int(time.time() * 1000), members=[peer.handle])
    state = _GAME_STATE_STORE.get(room_code)
    if state is None:
        state = GameState(
            schema_version=1, version=0, room_id=room_code,
            phase="lobby", leader_id=peer.handle, admission_order=[peer.handle],
        )
    next_state = state.model_copy(update={
        "version": state.version + 1,
        "applied_request_ids": {
            **state.applied_request_ids,
            request_id: CommandResult(request_id=request_id, accepted=True, reason="placeholder"),
        },
    })
    _GAME_STATE_STORE[room_code] = next_state  # stand-in for compare_and_set
    del context  # unused beyond demonstrating where it would feed engine.apply
    return GameStateEvent(payload=next_state)


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


@app.get("/metrics")
async def metrics():
    return app.state.rooms.metrics.as_dict()


@app.get("/dashboard", response_class=FileResponse)
async def dashboard():
    return FileResponse(DASHBOARD)


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
            elif isinstance(event, (GameStartEvent, RoundReadyEvent, GuessEvent, GameSyncEvent)):
                # PLACEHOLDER dispatch: proves routing only, see
                # _placeholder_apply above. Real wiring needs the actual
                # GameEngine/ScoreProvider/GameRepository.
                state_event = _placeholder_apply(peer, peer.room, event.payload.request_id)
                await manager.send(peer, state_event)
                if isinstance(event, GuessEvent):
                    await manager.send(peer, GuessResultEvent(payload=CommandResult(
                        request_id=event.payload.request_id, accepted=True,
                        reason="placeholder - no real scoring yet",
                    )))
    except (WebSocketDisconnect, OSError):
        pass
    except TimeoutError:
        await manager.error(peer, "Connection timed out; join again")
        await manager.close(peer, 1008)
    finally:
        # Finish presence cleanup even if the connection task is cancelled.
        with CancelScope(shield=True):
            await manager.disconnect(peer)
