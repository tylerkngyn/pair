"""Ranked 1v1 matchmaking and match orchestration.

Two matchmaking backends, chosen per request via request.app.state.ranked_queue
(set in app/main.py's lifespan exactly like app.state.rooms is, behind the
same REDIS_URL switch):

- Default: the Phase 1 in-process queue below (_QUEUE/_PENDING_MATCH, a
  plain list/dict behind a threading.Lock). Single-process only — two
  workers each have their own copy and can never match each other's
  waiting players.
- Phase 2: app/practice/redis_ranked_queue.py's RedisRankedQueue, shared
  state in Redis with the actual matching logic in Lua scripts (same
  reason app/redis_room_manager.py's room joins use one) so two workers
  racing the same pair of requests can't both claim the same match.

Match creation and finalization (_create_match, _finalize_if_ready) are
backend-agnostic — they only ever see plain dicts/DB rows, never the queue
implementation. MATCH_FOUND/OPPONENT_PROGRESS/MATCH_RESULT push over
app.state.rooms's per-account delivery (RoomManager.send_to_user /
RedisRoomManager.send_to_user) either way; GET /api/ranked/queue polling
stays as a fallback for a client whose socket was never identified.
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
# Identifies this process as the holder of a finalize lock, so acquiring
# it twice from the SAME worker (e.g. two requests racing in-process,
# which the DB status guard already handles) isn't confused with two
# different workers racing — not load-bearing for correctness, just a
# readable value in Redis over a meaningless token.
_WORKER_ID = secrets.token_hex(8)
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


def _redis_queue(request: Request):
    """None by default (Phase 1, in-process); set in app/main.py's
    lifespan alongside app.state.rooms when REDIS_URL is configured.
    """
    return getattr(request.app.state, "ranked_queue", None)


def _match_info(match: RankedMatch) -> dict:
    return {
        "match_id": match.id, "deadline": match.deadline,
        "problems": [MatchFoundProblem(id=pid, title=PROBLEMS[pid]["title"]) for pid in match.problem_ids],
    }


async def _match_and_push(request: Request, waiting: dict, incoming: dict, db):
    """In-process path only. Removes both sides from _QUEUE by identity,
    not equality — `incoming` is sometimes a brand-new entry that was
    never queued (join_queue's own arrival) and sometimes an
    already-queued entry re-matching on a poll (queue_status), so both
    sides need an unconditional, identity-based removal rather than
    assuming only one of them is actually in _QUEUE.
    """
    _QUEUE[:] = [e for e in _QUEUE if e is not waiting and e is not incoming]
    match = _create_match(db, waiting, incoming)
    _PENDING_MATCH[waiting["user_id"]] = match.id
    _PENDING_MATCH[incoming["user_id"]] = match.id
    return _match_info(match)


async def _push_match_found(request: Request, match_info: dict, a: dict, b: dict, redis_queue=None):
    # Pushed outside both the lock/queue-backend call and the DB
    # transaction: a dropped/slow delivery should never hold either. The
    # pending-match mailbox (whichever backend's) is popped only on a
    # *confirmed* delivery, so an unconfirmed push leaves it for the poll
    # fallback instead of losing the notification.
    for side, opponent in ((a, b), (b, a)):
        delivered = await request.app.state.rooms.send_to_user(side["user_id"], MatchFoundEvent(payload=MatchFoundPayload(
            match_id=match_info["match_id"], opponent=opponent["username"], opponent_rating=opponent["rating"],
            deadline=match_info["deadline"], problems=match_info["problems"],
        )))
        if delivered:
            if redis_queue is not None:
                await redis_queue.pop_pending(side["user_id"])
            else:
                with _LOCK:
                    _PENDING_MATCH.pop(side["user_id"], None)


@router.post("/queue", status_code=202)
async def join_queue(filters: QueueFilters, request: Request, identity=Depends(auth.authenticated_write)):
    user = identity[0]
    redis_queue = _redis_queue(request)
    if redis_queue is not None:
        return await _join_redis(request, redis_queue, user, filters)
    return await _join_in_process(request, user, filters)


async def _join_in_process(request: Request, user, filters: QueueFilters):
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


async def _join_redis(request: Request, redis_queue, user, filters: QueueFilters):
    with request.app.state.database.session.begin() as db:
        if _has_active_match(db, user.id):
            raise HTTPException(409, "Already in an active match")
        rating_row = _get_or_create_rating(db, user.id)
        incoming = {
            "user_id": user.id, "username": user.username, "rating": rating_row.rating,
            "filters": filters.model_dump(), "queued_at": time.time(),
        }
        result = await redis_queue.join(user.id, user.username, rating_row.rating, incoming["filters"])
        if result["status"] == "already_queued":
            raise HTTPException(409, "Already queued")
        if result["status"] == "queued":
            return {"status": "queued", "queued_at": result["queued_at"]}
        opponent = result["opponent"]
        match_info = _match_info(_create_match(db, opponent, incoming))
    await redis_queue.mark_pending(opponent["user_id"], match_info["match_id"])
    await redis_queue.mark_pending(user.id, match_info["match_id"])
    await _push_match_found(request, match_info, incoming, opponent, redis_queue)
    return {"status": "matched", "match_id": match_info["match_id"]}


@router.delete("/queue")
async def leave_queue(request: Request, identity=Depends(auth.authenticated_write)):
    user_id = identity[0].id
    redis_queue = _redis_queue(request)
    if redis_queue is not None:
        with request.app.state.database.session() as db:
            if _has_active_match(db, user_id):
                raise HTTPException(409, "Already matched")
        if not await redis_queue.leave(user_id):
            raise HTTPException(404, "Not in queue")
        return {"status": "left"}
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
    redis_queue = _redis_queue(request)
    if redis_queue is not None:
        return await _poll_redis(request, redis_queue, user_id)
    return await _poll_in_process(request, user_id)


async def _poll_in_process(request: Request, user_id: str):
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


async def _poll_redis(request: Request, redis_queue, user_id: str):
    result = await redis_queue.poll(user_id)
    if result["status"] in ("idle", "queued"):
        return result
    if "match_id" in result:  # Pending-mailbox hit; a match already exists.
        return result
    # The poll itself just found a match via widening — same as join()'s
    # match case, except the "incoming" side (this account) came from the
    # script's own "mine" field rather than being freshly built here.
    opponent, mine = result["opponent"], result["mine"]
    with request.app.state.database.session.begin() as db:
        match_info = _match_info(_create_match(db, opponent, mine))
    await redis_queue.mark_pending(opponent["user_id"], match_info["match_id"])
    await redis_queue.mark_pending(user_id, match_info["match_id"])
    await _push_match_found(request, match_info, mine, opponent, redis_queue)
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


async def _finalize_if_ready(db, match: RankedMatch, redis_queue=None):
    """Elo update is idempotent via the active->completed status guard, so a
    second caller racing this on another request is a no-op, not a double
    payout. That guard alone is Phase 1's whole story (single process, so
    it's the only race that can happen); once more than one worker can
    reach this for the same match, the gap between "read status == active"
    and "write status = completed" is wide enough (a full Elo computation
    plus a DB write) for two workers to both pass the read before either
    writes. `redis_queue.acquire_finalize_lock`, when a Redis backend is
    configured, closes that gap — only the worker that wins the lock
    proceeds; the status guard remains the second, independent safety net
    for exactly the reason its own docstring there gives.

    Returns {user_id: MatchResultPayload-shaped dict} if this call is the
    one that finalized the match, else None (still active, already
    completed by an earlier call, or lost the finalize-lock race to
    another worker) — the caller uses this to know whether to push
    MATCH_RESULT, since it should fire exactly once.
    """
    if match.status != "active":
        return None
    # Checked before the lock and the expensive per-problem queries below:
    # no reason to take a 10-second lock, or do any of that work, for a
    # match that isn't actually ready yet. (Early "both sides confirmed
    # ready" finalization is deferred past Phase 1 — only the deadline
    # passing finalizes a match.)
    if time.time() <= match.deadline:
        return None
    if redis_queue is not None and not await redis_queue.acquire_finalize_lock(match.id, _WORKER_ID):
        return None
    # The actual DB-side transition guard: an atomic conditional UPDATE,
    # not an ORM attribute set applied later. Claiming the transition
    # BEFORE computing or writing any Elo/Rating changes means a losing
    # racer (the finalize lock failed to prevent it, or expired mid-flight)
    # bails out here with nothing mutated yet, rather than needing to
    # discard already-staged ORM changes on a late "actually, no" — which
    # a plain `match.status = "completed"` at the end of this function
    # can't do, since by then the result would already be half-built.
    now = time.time()
    claimed = db.execute(
        update(RankedMatch).where(RankedMatch.id == match.id, RankedMatch.status == "active")
        .values(status="completed", finished_at=now)
    )
    if claimed.rowcount != 1:
        return None
    match.status, match.finished_at = "completed", now
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
        results = await _finalize_if_ready(db, match, _redis_queue(request))
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
