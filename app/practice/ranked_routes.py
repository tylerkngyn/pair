"""Ranked 1v1 matchmaking and match orchestration.

Phase 1: a plain in-process queue (per the build checklist's "don't
over-build it now" — this gets replaced by the Redis sorted-set queue in
Phase 2). Matching happens synchronously inside POST /api/ranked/queue
against anyone already waiting; a side that was already queued learns
about the match by polling GET /api/ranked/queue, since this phase has no
per-account WebSocket delivery yet to push MATCH_FOUND unprompted.
"""
import random
import secrets
import threading
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import Field
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.practice import auth
from app.practice.auth import token_hash
from app.practice.catalog import PROBLEMS
from app.practice.database import RateLimit, Rating, RankedMatch, RankedParticipant, RankedWork, User
from app.practice.ranked import elo_update, match_outcome, tier
from app.practice.routes import SaveWork, StrictModel

router = APIRouter(prefix="/api/ranked")

# Phase 1 in-process matchmaking state. Single-process only; Phase 2 moves
# this to a Redis sorted set plus a Lua matching script so every worker
# sees every waiting player.
_LOCK = threading.Lock()
_QUEUE: list[dict] = []  # [{user_id, username, filters, rating, queued_at}]
_PENDING_MATCH: dict[str, str] = {}  # user_id -> match_id, consumed by one poll

MIN_WINDOW = 50
WINDOW_STEP = 25
WINDOW_GROWTH_SECONDS = 5
MAX_WINDOW = 400
SUBMISSION_PLAUSIBILITY_FLOOR_SECONDS = 10


class QueueFilters(StrictModel):
    categories: list[str] = Field(default_factory=list, max_length=20)
    difficulty: Literal["mixed", "easy", "medium", "hard"] = "mixed"
    duration_minutes: int = Field(default=30, ge=5, le=180)
    problem_count: int = Field(default=1, ge=1, le=5)


def _widen(waited_seconds: float) -> int:
    return min(MAX_WINDOW, MIN_WINDOW + WINDOW_STEP * int(waited_seconds // WINDOW_GROWTH_SECONDS))


def _get_or_create_rating(db, user_id: str) -> Rating:
    rating = db.get(Rating, user_id)
    if rating is None:
        rating = Rating(user_id=user_id, rating=1000, matches_played=0, updated_at=time.time())
        db.add(rating)
        db.flush()
    return rating


def _pick_problems(filters: QueueFilters) -> list[str]:
    available = [
        p for p in PROBLEMS.values()
        if (not filters.categories or p["category"] in filters.categories)
        and (filters.difficulty == "mixed" or p["difficulty"] == filters.difficulty)
    ]
    if len(available) < filters.problem_count:
        raise HTTPException(422, "Not enough problems match these filters. Choose fewer problems or broader filters.")
    return [p["id"] for p in random.SystemRandom().sample(available, filters.problem_count)]


def _create_match(db, waiting: dict, incoming: dict) -> RankedMatch:
    now = time.time()
    problem_ids = _pick_problems(QueueFilters(**incoming["filters"]))
    match = RankedMatch(
        id=secrets.token_hex(16), problem_ids=problem_ids, started_at=now,
        deadline=now + incoming["filters"]["duration_minutes"] * 60, status="active",
    )
    db.add(match)
    db.flush()
    for entry in (waiting, incoming):
        db.add(RankedParticipant(match_id=match.id, user_id=entry["user_id"], rating_before=entry["rating"]))
        for pid in problem_ids:
            starter = PROBLEMS[pid]["starter"]
            db.add(RankedWork(match_id=match.id, problem_id=pid, user_id=entry["user_id"], code=starter, revision=0))
    db.flush()
    return match


@router.post("/queue", status_code=202)
def join_queue(filters: QueueFilters, request: Request, identity=Depends(auth.authenticated_write)):
    user = identity[0]
    with _LOCK:
        if any(e["user_id"] == user.id for e in _QUEUE) or user.id in _PENDING_MATCH:
            raise HTTPException(409, "Already queued or matched")
        with request.app.state.database.session.begin() as db:
            rating_row = _get_or_create_rating(db, user.id)
            incoming = {
                "user_id": user.id, "username": user.username, "rating": rating_row.rating,
                "filters": filters.model_dump(), "queued_at": time.time(),
            }
            for candidate in _QUEUE:
                if candidate["filters"] != incoming["filters"]:
                    continue
                window = _widen(time.time() - candidate["queued_at"])
                if abs(candidate["rating"] - incoming["rating"]) <= window:
                    _QUEUE.remove(candidate)
                    match = _create_match(db, candidate, incoming)
                    _PENDING_MATCH[candidate["user_id"]] = match.id
                    _PENDING_MATCH[user.id] = match.id
                    return {"status": "matched", "match_id": match.id}
            _QUEUE.append(incoming)
            return {"status": "queued", "queued_at": incoming["queued_at"]}


@router.delete("/queue")
def leave_queue(identity=Depends(auth.authenticated_write)):
    user_id = identity[0].id
    with _LOCK:
        if user_id in _PENDING_MATCH:
            raise HTTPException(409, "Already matched")
        before = len(_QUEUE)
        _QUEUE[:] = [e for e in _QUEUE if e["user_id"] != user_id]
        if len(_QUEUE) == before:
            raise HTTPException(404, "Not in queue")
        return {"status": "left"}


@router.get("/queue")
def queue_status(identity=Depends(auth.current_login)):
    user_id = identity[0].id
    with _LOCK:
        match_id = _PENDING_MATCH.pop(user_id, None)
        if match_id:
            return {"status": "matched", "match_id": match_id}
        if any(e["user_id"] == user_id for e in _QUEUE):
            return {"status": "queued"}
        return {"status": "idle"}


@router.get("/rating")
def rating(request: Request, identity=Depends(auth.current_login)):
    with request.app.state.database.session.begin() as db:
        row = _get_or_create_rating(db, identity[0].id)
        return {"rating": row.rating, "matches_played": row.matches_played, "tier": tier(row.rating)}


def _owned_participant(db, match_id, user_id):
    participant = db.get(RankedParticipant, (match_id, user_id))
    if participant is None:
        raise HTTPException(404, "Match not found")
    return participant


def _finalize_if_ready(db, match: RankedMatch):
    """Elo update is idempotent via the active->completed status guard, so a
    second caller racing this on another request is a no-op, not a double
    payout. The real exactly-once guarantee across *workers* is a Phase 2
    concern (a Redis finalization lock); single-process Phase 1 only needs
    this status check.
    """
    if match.status != "active":
        return
    participants = list(db.scalars(select(RankedParticipant).where(RankedParticipant.match_id == match.id)))
    if len(participants) != 2:
        return
    expected = sum(len(PROBLEMS[pid]["cases"]) for pid in match.problem_ids)
    work_by_user = {}
    for participant in participants:
        work = list(db.scalars(select(RankedWork).where(RankedWork.match_id == match.id, RankedWork.user_id == participant.user_id)))
        passed = sum(w.result["passed"] for w in work if w.result)
        last_submit = max((w.result["submitted_at"] for w in work if w.result), default=match.deadline)
        work_by_user[participant.user_id] = {
            "correctness": round(100 * passed / expected, 1) if expected else 0.0,
            "elapsed": last_submit - match.started_at,
        }
    now = time.time()
    # A match finalizes once the deadline has passed. (Early "both sides
    # confirmed ready" finalization is deferred past Phase 1.)
    if now <= match.deadline:
        return
    a, b = participants
    outcome_a, score_a = match_outcome(
        work_by_user[a.user_id]["correctness"], work_by_user[b.user_id]["correctness"],
        work_by_user[a.user_id]["elapsed"], work_by_user[b.user_id]["elapsed"],
    )
    new_a, new_b = elo_update(a.rating_before, b.rating_before, 0, 0, score_a)
    for participant, outcome, new_rating in ((a, outcome_a, new_a), (b, {"win": "loss", "loss": "win", "draw": "draw"}[outcome_a], new_b)):
        participant.outcome = outcome
        participant.rating_after = new_rating
        rating_row = _get_or_create_rating(db, participant.user_id)
        rating_row.rating = new_rating
        rating_row.matches_played += 1
        rating_row.updated_at = now
    match.status = "completed"
    match.finished_at = now
    db.flush()


def _ranked_submission_throttle(request, user_id):
    """Separate from auth.throttle's login-attempt limits: this caps ranked
    *submissions* specifically, since a ranked rating is a stake that raises
    the incentive to hammer the endpoint in ways solo practice never gave
    a reason to.
    """
    now = time.time()
    with request.app.state.database.session.begin() as db:
        db.execute(delete(RateLimit).where(RateLimit.reset_at < now))
        key = token_hash("ranked_submit:" + user_id)
        insert = sqlite_insert if db.bind.dialect.name == "sqlite" else postgres_insert
        statement = insert(RateLimit).values(key=key, count=1, reset_at=now + 60)
        statement = statement.on_conflict_do_update(index_elements=[RateLimit.key], set_={"count": RateLimit.count + 1}).returning(RateLimit.count)
        if db.execute(statement).scalar_one() > 20:
            raise HTTPException(429, "Too many submissions. Slow down.")


@router.put("/matches/{match_id}/work/{problem_id}")
def save_ranked_work(match_id: str, problem_id: str, body: SaveWork, request: Request, identity=Depends(auth.authenticated_write)):
    user_id = identity[0].id
    with request.app.state.database.session.begin() as db:
        match = db.get(RankedMatch, match_id)
        if match is None:
            raise HTTPException(404, "Match not found")
        _owned_participant(db, match_id, user_id)
        if problem_id not in match.problem_ids:
            raise HTTPException(404, "Problem is not in this match")
        if match.status != "active" or time.time() > match.deadline:
            raise HTTPException(409, "This match has ended")
        work = db.get(RankedWork, (match_id, problem_id, user_id))
        report = body.report
        result = work.result if work.code == body.code else None
        if report:
            _ranked_submission_throttle(request, user_id)
            count = len(PROBLEMS[problem_id]["cases"])
            if report.total != count or report.passed > count or (not report.error and len(report.cases) != count) or report.passed != sum(c.passed for c in report.cases):
                raise HTTPException(422, "Result does not match this problem's practice suite")
            # Plausibility floor: a pass reported faster than any human could
            # plausibly run the suite is treated as a precomputed/replayed
            # answer, not evidence of skill — see the Obsidian plan's
            # anti-cheat section. This never proves a submission is genuine;
            # it only rejects the cheapest replay attempts.
            elapsed = time.time() - match.started_at
            if report.passed == count and elapsed < SUBMISSION_PLAUSIBILITY_FLOOR_SECONDS:
                raise HTTPException(422, "Submitted implausibly fast after the match started")
            result = report.model_dump() | {"source": "browser_practice", "submitted_at": time.time()}
        changed = db.execute(update(RankedWork).where(
            RankedWork.match_id == match_id, RankedWork.problem_id == problem_id,
            RankedWork.user_id == user_id, RankedWork.revision == body.revision,
        ).values(code=body.code, result=result, revision=RankedWork.revision + 1))
        if changed.rowcount != 1:
            raise HTTPException(409, "This draft changed in another tab. Reload before saving.")
        return {"revision": body.revision + 1, "result": result}


@router.get("/matches/{match_id}")
def get_match(match_id: str, request: Request, identity=Depends(auth.current_login)):
    with request.app.state.database.session.begin() as db:
        match = db.get(RankedMatch, match_id)
        if match is None:
            raise HTTPException(404, "Match not found")
        participant = _owned_participant(db, match_id, identity[0].id)
        _finalize_if_ready(db, match)
        opponent = db.scalar(select(RankedParticipant).where(RankedParticipant.match_id == match_id, RankedParticipant.user_id != identity[0].id))
        return {
            "id": match.id, "status": match.status, "deadline": match.deadline, "finished_at": match.finished_at,
            "problems": [{k: PROBLEMS[pid][k] for k in ("id", "title", "category", "difficulty")} for pid in match.problem_ids],
            "my_outcome": participant.outcome, "my_rating_after": participant.rating_after,
            "opponent_outcome": opponent.outcome if opponent else None,
            "opponent_rating_after": opponent.rating_after if opponent else None,
        }


@router.get("/history")
def history(request: Request, identity=Depends(auth.current_login)):
    with request.app.state.database.session() as db:
        mine = list(db.scalars(
            select(RankedParticipant).where(RankedParticipant.user_id == identity[0].id)
            .order_by(RankedParticipant.match_id.desc()).limit(30)
        ))
        matches = []
        for participant in mine:
            opponent = db.scalar(select(RankedParticipant).where(RankedParticipant.match_id == participant.match_id, RankedParticipant.user_id != identity[0].id))
            opponent_user = db.get(User, opponent.user_id) if opponent else None
            match = db.get(RankedMatch, participant.match_id)
            matches.append({
                "id": match.id, "started_at": match.started_at, "finished_at": match.finished_at,
                "opponent": opponent_user.username if opponent_user else None,
                "outcome": participant.outcome, "rating_before": participant.rating_before, "rating_after": participant.rating_after,
            })
        return {"matches": matches}
