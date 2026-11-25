"""Two-seat unranked lobbies. Database CAS operations coordinate all workers."""
import secrets
import string
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.practice import auth
from app.practice.database import Lobby, LobbySeat, Rating, User
from app.practice.ranked_routes import QueueFilters, RANKED_WARMUP_SECONDS, _create_match, _has_active_match, _pick_problems
from app.practice.routes import StrictModel

router = APIRouter(prefix="/api/lobbies")
LEASE_SECONDS = 120


class Ready(StrictModel):
    ready: bool


class CreateLobby(QueueFilters):
    is_private: bool = False


def cleanup(db):
    now = time.time()
    for lobby in db.scalars(select(Lobby).where(Lobby.status == "waiting")):
        host = db.get(LobbySeat, lobby.host_id)
        guest = db.get(LobbySeat, lobby.guest_id) if lobby.guest_id else None
        if host is None or host.last_seen < now - LEASE_SECONDS:
            alive = select(LobbySeat.user_id).where(LobbySeat.user_id == lobby.host_id, LobbySeat.last_seen >= now - LEASE_SECONDS).exists()
            changed = db.execute(update(Lobby).where(Lobby.code == lobby.code, Lobby.status == "waiting", ~alive).values(status="closed"))
            if changed.rowcount:
                db.execute(delete(LobbySeat).where(LobbySeat.code == lobby.code))
        elif lobby.guest_id and (guest is None or guest.last_seen < now - LEASE_SECONDS):
            guest_id = lobby.guest_id
            alive = select(LobbySeat.user_id).where(LobbySeat.user_id == guest_id, LobbySeat.last_seen >= now - LEASE_SECONDS).exists()
            changed = db.execute(update(Lobby).where(Lobby.code == lobby.code, Lobby.status == "waiting", Lobby.guest_id == guest_id, ~alive).values(guest_id=None, guest_ready=0))
            if changed.rowcount:
                db.execute(delete(LobbySeat).where(LobbySeat.user_id == guest_id, LobbySeat.code == lobby.code))
    db.flush()


def info(db, lobby, user_id):
    host, guest = db.get(User, lobby.host_id), db.get(User, lobby.guest_id) if lobby.guest_id else None
    return {"code": lobby.code, "host": host.username, "guest": guest.username if guest else None,
            "is_host": user_id == lobby.host_id, "settings": lobby.settings, "status": lobby.status,
            "is_private": lobby.is_private,
            "host_ready": bool(lobby.host_ready), "guest_ready": bool(lobby.guest_ready),
            "match_id": lobby.match_id, "created_at": lobby.created_at}


def member(db, code, user_id):
    lobby = db.get(Lobby, code.upper())
    if lobby is None or user_id not in (lobby.host_id, lobby.guest_id):
        raise HTTPException(404, "Lobby not found")
    return lobby


async def available(request, db, user_id):
    if _has_active_match(db, user_id):
        raise HTTPException(409, "Finish your current match first")
    if db.get(LobbySeat, user_id):
        raise HTTPException(409, "Leave your current lobby first")
    queue = getattr(request.app.state, "ranked_queue", None)
    if queue:
        queued = await queue.contains(user_id)
    else:
        from app.practice.ranked_routes import _QUEUE
        queued = any(p["user_id"] == user_id for p in _QUEUE)
    if queued:
        raise HTTPException(409, "Cancel ranked matchmaking first")


@router.get("")
def browse(request: Request, identity=Depends(auth.current_login)):
    with request.app.state.database.session.begin() as db:
        cleanup(db)
        return {"lobbies": [info(db, lobby, identity[0].id) for lobby in db.scalars(
            select(Lobby).where(Lobby.status == "waiting", Lobby.guest_id.is_(None), Lobby.is_private.is_(False)).order_by(Lobby.created_at.desc()).limit(30))]}


@router.get("/current")
def current(request: Request, identity=Depends(auth.current_login)):
    with request.app.state.database.session.begin() as db:
        cleanup(db)
        seat = db.get(LobbySeat, identity[0].id)
        if not seat:
            return {"lobby": None}
        seat.last_seen = time.time()
        return {"lobby": info(db, db.get(Lobby, seat.code), identity[0].id)}


@router.post("", status_code=201)
async def create(filters: CreateLobby, request: Request, identity=Depends(auth.authenticated_write)):
    _pick_problems(filters)  # Validate before reserving a seat.
    try:
        with request.app.state.database.session.begin() as db:
            cleanup(db)
            await available(request, db, identity[0].id)
            code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            while db.get(Lobby, code):
                code = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            lobby = Lobby(code=code, host_id=identity[0].id, settings=filters.model_dump(exclude={"is_private"}), is_private=filters.is_private, status="waiting", created_at=time.time(), host_ready=1, guest_ready=0)
            db.add(lobby); db.flush()
            db.add(LobbySeat(user_id=identity[0].id, code=code, last_seen=time.time()))
            db.flush()
            return info(db, lobby, identity[0].id)
    except IntegrityError:
        raise HTTPException(409, "Already in a lobby; refresh to resume it") from None


@router.post("/{code}/join")
async def join(code: str, request: Request, identity=Depends(auth.authenticated_write)):
    try:
        with request.app.state.database.session.begin() as db:
            cleanup(db)
            await available(request, db, identity[0].id)
            changed = db.execute(update(Lobby).where(Lobby.code == code.upper(), Lobby.status == "waiting", Lobby.guest_id.is_(None), Lobby.host_id != identity[0].id).values(guest_id=identity[0].id, guest_ready=0))
            if changed.rowcount != 1:
                raise HTTPException(409, "Lobby is full, closed, or no longer available")
            db.add(LobbySeat(user_id=identity[0].id, code=code.upper(), last_seen=time.time()))
            db.flush()
            return info(db, db.get(Lobby, code.upper()), identity[0].id)
    except IntegrityError:
        raise HTTPException(409, "Already in another lobby; refresh to resume it") from None


@router.get("/{code}")
def get_lobby(code: str, request: Request, identity=Depends(auth.current_login)):
    with request.app.state.database.session.begin() as db:
        cleanup(db)
        lobby = member(db, code, identity[0].id)
        seat = db.get(LobbySeat, identity[0].id)
        if seat:
            seat.last_seen = time.time()
        return info(db, lobby, identity[0].id)


@router.put("/{code}/ready")
def ready(code: str, body: Ready, request: Request, identity=Depends(auth.authenticated_write)):
    with request.app.state.database.session.begin() as db:
        cleanup(db)
        lobby = member(db, code, identity[0].id)
        if lobby.status != "waiting":
            raise HTTPException(409, "Lobby has already started or closed")
        is_host = lobby.host_id == identity[0].id
        changed = db.execute(update(Lobby).where(Lobby.code == lobby.code, Lobby.status == "waiting",
            (Lobby.host_id if is_host else Lobby.guest_id) == identity[0].id).values(**{"host_ready" if is_host else "guest_ready": int(body.ready)}))
        if changed.rowcount != 1:
            raise HTTPException(409, "Lobby changed; refresh and try again")
        db.flush()
        return info(db, lobby, identity[0].id)


@router.post("/{code}/start")
def start(code: str, request: Request, identity=Depends(auth.authenticated_write)):
    with request.app.state.database.session.begin() as db:
        cleanup(db)
        lobby = member(db, code, identity[0].id)
        if lobby.host_id != identity[0].id:
            raise HTTPException(403, "Only the host can start")
        if lobby.status == "started":
            return info(db, lobby, identity[0].id)
        changed = db.execute(update(Lobby).where(Lobby.code == lobby.code, Lobby.status == "waiting", Lobby.guest_id.is_not(None), Lobby.host_ready == 1, Lobby.guest_ready == 1).values(status="started"))
        if changed.rowcount != 1:
            raise HTTPException(409, "Both players must be present and ready")
        entries = []
        for uid in (lobby.host_id, lobby.guest_id):
            if _has_active_match(db, uid):
                raise HTTPException(409, "A player is already in a match")
            user, rating = db.get(User, uid), db.get(Rating, uid)
            entries.append({"user_id": uid, "username": user.username, "rating": rating.rating if rating else 1000, "filters": lobby.settings})
        lobby.match_id = _create_match(db, *entries, warmup_seconds=RANKED_WARMUP_SECONDS).id
        db.flush()
        return info(db, lobby, identity[0].id)


@router.delete("/{code}")
def leave(code: str, request: Request, identity=Depends(auth.authenticated_write)):
    with request.app.state.database.session.begin() as db:
        lobby = member(db, code, identity[0].id)
        if lobby.status == "started":
            raise HTTPException(409, "Match already started; return to the match")
        if lobby.host_id == identity[0].id:
            changed = db.execute(update(Lobby).where(Lobby.code == lobby.code, Lobby.status == "waiting").values(status="closed"))
            if changed.rowcount != 1:
                raise HTTPException(409, "Match already started; return to the match")
            db.execute(delete(LobbySeat).where(LobbySeat.code == lobby.code))
        else:
            changed = db.execute(update(Lobby).where(Lobby.code == lobby.code, Lobby.status == "waiting", Lobby.guest_id == identity[0].id).values(guest_id=None, guest_ready=0))
            if changed.rowcount != 1:
                raise HTTPException(409, "Match already started; return to the match")
            db.execute(delete(LobbySeat).where(LobbySeat.user_id == identity[0].id))
        return {"status": "left"}
