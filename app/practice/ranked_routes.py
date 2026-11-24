"""Ranked 1v1 matchmaking and match orchestration.

Phase 1: a plain in-process queue (per the build checklist's "don't
over-build it now" — this gets replaced by the Redis sorted-set queue in
Phase 2). Matching happens synchronously inside POST /api/ranked/queue
against anyone already waiting. MATCH_FOUND/OPPONENT_PROGRESS/MATCH_RESULT
push over app.state.rooms's per-account delivery (RoomManager.send_to_user /
RedisRoomManager.send_to_user); GET /api/ranked/queue polling stays as a
fallback for a client whose socket was never identified (no valid cookie
at connect time, or no socket open at all).
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

from app.models import (
    MatchFoundEvent, MatchFoundPayload, MatchFoundProblem,
    MatchResultEvent, MatchResultPayload,
    OpponentProgressEvent, OpponentProgressPayload,
)
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


def _has_active_match(db, user_id: str) -> bool:
    """The DB, not _PENDING_MATCH, is the source of truth for "is this
    account already in a match" — _PENDING_MATCH is just a short-lived
    MATCH_FOUND notification mailbox and gets popped as soon as it's
    delivered (by a poll, or by a confirmed WebSocket push), so it must
    never double as the guard against re-queueing into a second match.
    """
    return db.scalar(
        select(RankedParticipant.match_id)
        .join(RankedMatch, RankedMatch.id == RankedParticipant.match_id)
        .where(RankedParticipant.user_id == user_id, RankedMatch.status == "active")
        .limit(1)
    ) is not None


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


def _find_opponent(incoming: dict):
    """Window widens with however long either side has waited, not just the
    candidate's own wait — a fresh arrival can still immediately match a
    long-waiting candidate (existing behavior), and a long-waiting poller
    re-scanning the queue (see queue_status below) gets the same courtesy
    even against a candidate who just arrived.
    """
    return next((
        candidate for candidate in _QUEUE
        if candidate["user_id"] != incoming["user_id"]
        and candidate["filters"] == incoming["filters"]
        # min(), not max(): the window should widen based on whichever side
        # has waited LONGER (the older/smaller timestamp), which gives the
        # larger elapsed time. Using max() picked the more-recently-queued
        # side instead, which shrank the window back toward the base case
        # on every fresh arrival regardless of how long the other side had
        # actually been waiting — the opposite of the "courtesy" described
        # above, just masked in testing by both sides usually being equally
        # fresh or equally backdated.
        and abs(candidate["rating"] - incoming["rating"]) <= _widen(time.time() - min(candidate["queued_at"], incoming["queued_at"]))
    ), None)


async def _match_and_push(request: Request, waiting: dict, incoming: dict, db):
    """Creates the match (caller holds _LOCK and an open transaction on
    `db`) and returns the info needed to push MATCH_FOUND once that
    transaction has committed and the lock is released.

    Removes both sides from _QUEUE by identity, not equality — `incoming`
    is sometimes a brand-new entry that was never queued (join_queue's own
    arrival) and sometimes an already-queued entry re-matching on a poll
    (queue_status), so both sides need an unconditional, identity-based
    removal rather than assuming only one of them is actually in _QUEUE.
    """
    _QUEUE[:] = [e for e in _QUEUE if e is not waiting and e is not incoming]
    match = _create_match(db, waiting, incoming)
    _PENDING_MATCH[waiting["user_id"]] = match.id
    _PENDING_MATCH[incoming["user_id"]] = match.id
    return {
        "match_id": match.id, "deadline": match.deadline,
        "problems": [MatchFoundProblem(id=pid, title=PROBLEMS[pid]["title"]) for pid in match.problem_ids],
    }


async def _push_match_found(request: Request, match_info: dict, a: dict, b: dict):
    # Pushed outside both the lock and the transaction: a dropped/slow
    # delivery should never hold either. _PENDING_MATCH is purely the
    # GET /api/ranked/queue poll fallback's mailbox at this point (the DB
    # is what guards against re-queueing, via _has_active_match above), so
    # a *confirmed* local delivery can safely drop it right away instead of
    # leaving a stale notification for a poll that will never need it.
    for side, opponent in ((a, b), (b, a)):
        delivered = await request.app.state.rooms.send_to_user(side["user_id"], MatchFoundEvent(payload=MatchFoundPayload(
            match_id=match_info["match_id"], opponent=opponent["username"], opponent_rating=opponent["rating"],
            deadline=match_info["deadline"], problems=match_info["problems"],
        )))
        if delivered:
            with _LOCK:
                _PENDING_MATCH.pop(side["user_id"], None)


@router.post("/queue", status_code=202)
async def join_queue(filters: QueueFilters, request: Request, identity=Depends(auth.authenticated_write)):
    user = identity[0]
    match_info = None
    opponent_entry = None
    incoming = None
    with _LOCK:
        if any(e["user_id"] == user.id for e in _QUEUE):
            raise HTTPException(409, "Already queued")
        with request.app.state.database.session.begin() as db:
            if _has_active_match(db, user.id):
                raise HTTPException(409, "Already in an active match")
            rating_row = _get_or_create_rating(db, user.id)
            incoming = {
                "user_id": user.id, "username": user.username, "rating": rating_row.rating,
                "filters": filters.model_dump(), "queued_at": time.time(),
            }
            opponent_entry = _find_opponent(incoming)
            if opponent_entry is None:
                _QUEUE.append(incoming)
                return {"status": "queued", "queued_at": incoming["queued_at"]}
            match_info = await _match_and_push(request, opponent_entry, incoming, db)
    await _push_match_found(request, match_info, incoming, opponent_entry)
    return {"status": "matched", "match_id": match_info["match_id"]}


@router.delete("/queue")
def leave_queue(request: Request, identity=Depends(auth.authenticated_write)):
    user_id = identity[0].id
    with _LOCK:
        with request.app.state.database.session() as db:
            if _has_active_match(db, user_id):
                raise HTTPException(409, "Already matched")
        before = len(_QUEUE)
        _QUEUE[:] = [e for e in _QUEUE if e["user_id"] != user_id]
        if len(_QUEUE) == before:
            raise HTTPException(404, "Not in queue")
        return {"status": "left"}


@router.get("/queue")
async def queue_status(request: Request, identity=Depends(auth.current_login)):
    user_id = identity[0].id
    match_info = None
    mine = None
    opponent_entry = None
    with _LOCK:
        match_id = _PENDING_MATCH.pop(user_id, None)
        if match_id:
            return {"status": "matched", "match_id": match_id}
        mine = next((e for e in _QUEUE if e["user_id"] == user_id), None)
        if mine is None:
            return {"status": "idle"}
        # Opportunistic widening: matching otherwise only runs inside
        # another account's POST /api/ranked/queue, so a lone queued player
        # would never benefit from their own window growing while they
        # just sit there. The client already polls this endpoint every few
        # seconds anyway (as the MATCH_FOUND poll fallback), so piggybacking
        # the retry here needs no new background task.
        opponent_entry = _find_opponent(mine)
        if opponent_entry is None:
            return {"status": "queued"}
        with request.app.state.database.session.begin() as db:
            match_info = await _match_and_push(request, opponent_entry, mine, db)
    await _push_match_found(request, match_info, mine, opponent_entry)
    return {"status": "matched", "match_id": match_info["match_id"]}


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

    Returns {user_id: MatchResultPayload-shaped dict} if this call is the
    one that finalized the match, else None (either still active, or
    already completed by an earlier call) — the caller uses this to know
    whether to push MATCH_RESULT, since it should fire exactly once.
    """
    if match.status != "active":
        return None
    participants = list(db.scalars(select(RankedParticipant).where(RankedParticipant.match_id == match.id)))
    if len(participants) != 2:
        return None
    work_by_user = {}
    for participant in participants:
        statuses, correctness = _problem_statuses_and_correctness(db, match, participant.user_id)
        work = list(db.scalars(select(RankedWork).where(RankedWork.match_id == match.id, RankedWork.user_id == participant.user_id)))
        last_submit = max((w.result["submitted_at"] for w in work if w.result), default=match.deadline)
        attempted = any(status != "not_started" for status in statuses.values())
        work_by_user[participant.user_id] = {"correctness": correctness, "elapsed": last_submit - match.started_at, "attempted": attempted}
    now = time.time()
    # A match finalizes once the deadline has passed. (Early "both sides
    # confirmed ready" finalization is deferred past Phase 1.)
    if now <= match.deadline:
        return None
    a, b = participants
    a_info, b_info = work_by_user[a.user_id], work_by_user[b.user_id]
    if not a_info["attempted"] and not b_info["attempted"]:
        # Double no-show: neither side ever submitted anything. Not a draw
        # — the plan says abandoning is a loss for the abandoning side, and
        # with no one left to call a winner there's no zero-sum pair to run
        # through elo_update. Each side instead takes the penalty it would
        # get for losing to an opponent of its own rating (-K/2, same shape
        # as a chess resignation), independently.
        outcome_a = outcome_b = "abandoned"
        new_a, _ = elo_update(a.rating_before, a.rating_before, 0, 0, 0.0)
        new_b, _ = elo_update(b.rating_before, b.rating_before, 0, 0, 0.0)
    elif not a_info["attempted"]:
        outcome_a, outcome_b = "abandoned", "win"
        new_a, new_b = elo_update(a.rating_before, b.rating_before, 0, 0, 0.0)
    elif not b_info["attempted"]:
        outcome_a, outcome_b = "win", "abandoned"
        new_a, new_b = elo_update(a.rating_before, b.rating_before, 0, 0, 1.0)
    else:
        outcome_a, score_a = match_outcome(a_info["correctness"], b_info["correctness"], a_info["elapsed"], b_info["elapsed"])
        outcome_b = {"win": "loss", "loss": "win", "draw": "draw"}[outcome_a]
        new_a, new_b = elo_update(a.rating_before, b.rating_before, 0, 0, score_a)
    results = {}
    for participant, opponent, outcome, new_rating, opponent_new_rating in (
        (a, b, outcome_a, new_a, new_b),
        (b, a, outcome_b, new_b, new_a),
    ):
        participant.outcome = outcome
        participant.rating_after = new_rating
        rating_row = _get_or_create_rating(db, participant.user_id)
        rating_row.rating = new_rating
        rating_row.matches_played += 1
        rating_row.updated_at = now
        results[participant.user_id] = {
            "outcome": outcome, "my_correctness": work_by_user[participant.user_id]["correctness"],
            "opponent_correctness": work_by_user[opponent.user_id]["correctness"],
            "rating_before": participant.rating_before, "rating_after": new_rating,
            "opponent_rating_after": opponent_new_rating,
        }
    match.status = "completed"
    match.finished_at = now
    db.flush()
    return results


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
async def save_ranked_work(match_id: str, problem_id: str, body: SaveWork, request: Request, identity=Depends(auth.authenticated_write)):
    user_id = identity[0].id
    progress_push = None
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
        if report:
            opponent = db.scalar(select(RankedParticipant).where(RankedParticipant.match_id == match_id, RankedParticipant.user_id != user_id))
            if opponent is not None:
                progress_push = (opponent.user_id, *_problem_statuses_and_correctness(db, match, user_id))
        response = {"revision": body.revision + 1, "result": result}
    if progress_push:
        opponent_id, statuses, correctness = progress_push
        await request.app.state.rooms.send_to_user(opponent_id, OpponentProgressEvent(payload=OpponentProgressPayload(
            problem_statuses=statuses, correctness=correctness,
        )))
    return response


def _problem_statuses_and_correctness(db, match: RankedMatch, user_id: str):
    work = list(db.scalars(select(RankedWork).where(RankedWork.match_id == match.id, RankedWork.user_id == user_id)))
    statuses = {}
    for w in work:
        if w.result and w.result["passed"] == len(PROBLEMS[w.problem_id]["cases"]):
            statuses[w.problem_id] = "passed"
        elif w.result:
            statuses[w.problem_id] = "attempted"
        else:
            statuses[w.problem_id] = "not_started"
    expected = sum(len(PROBLEMS[pid]["cases"]) for pid in match.problem_ids)
    passed = sum(w.result["passed"] for w in work if w.result)
    correctness = round(100 * passed / expected, 1) if expected else 0.0
    return statuses, correctness


@router.get("/matches/{match_id}")
async def get_match(match_id: str, request: Request, identity=Depends(auth.current_login)):
    with request.app.state.database.session.begin() as db:
        match = db.get(RankedMatch, match_id)
        if match is None:
            raise HTTPException(404, "Match not found")
        participant = _owned_participant(db, match_id, identity[0].id)
        results = _finalize_if_ready(db, match)
        opponent = db.scalar(select(RankedParticipant).where(RankedParticipant.match_id == match_id, RankedParticipant.user_id != identity[0].id))
        response = {
            "id": match.id, "status": match.status, "deadline": match.deadline, "finished_at": match.finished_at,
            "problems": [{k: PROBLEMS[pid][k] for k in ("id", "title", "category", "difficulty")} for pid in match.problem_ids],
            "my_outcome": participant.outcome, "my_rating_after": participant.rating_after,
            "opponent_outcome": opponent.outcome if opponent else None,
            "opponent_rating_after": opponent.rating_after if opponent else None,
        }
    if results:
        for user_id, payload in results.items():
            await request.app.state.rooms.send_to_user(user_id, MatchResultEvent(payload=MatchResultPayload(**payload)))
    return response


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
