"""Session orchestration and storage; Python execution stays in the browser."""
import random
import secrets
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from app.practice import auth
from app.practice.catalog import PROBLEMS, summaries
from app.practice.database import PracticeSession, Work

router = APIRouter(prefix="/api")
router.add_api_route("/auth/register", auth.register, methods=["POST"], status_code=201)
router.add_api_route("/auth/login", auth.login, methods=["POST"])
router.add_api_route("/auth/logout", auth.logout, methods=["POST"])
router.add_api_route("/auth/me", auth.me, methods=["GET"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Settings(StrictModel):
    categories: list[str] = Field(default_factory=list, max_length=20)
    difficulty: Literal["mixed", "easy", "medium", "hard"] = "mixed"
    duration_minutes: int = Field(default=45, ge=5, le=180)
    problem_count: int = Field(default=1, ge=1, le=5)
    allow_overtime: bool = True
    problem_ids: list[str] = Field(default_factory=list, max_length=5)


class CaseResult(StrictModel):
    passed: bool
    actual: str | None = Field(default=None, max_length=1000)
    expected: str | None = Field(default=None, max_length=1000)
    error: str | None = Field(default=None, max_length=3000)


class Report(StrictModel):
    passed: int = Field(ge=0, le=100)
    total: int = Field(ge=1, le=100)
    runtime_ms: float | None = Field(default=None, ge=0, le=300000)
    reference_ms: float | None = Field(default=None, ge=0, le=300000)
    cases: list[CaseResult] = Field(max_length=100)
    stdout: str = Field(default="", max_length=8000)
    error: str | None = Field(default=None, max_length=3000)


class SaveWork(StrictModel):
    code: str = Field(max_length=50000)
    revision: int = Field(ge=0)
    report: Report | None = None


def owned(db, session_id, user_id):
    session = db.get(PracticeSession, session_id)
    if session is None or session.user_id != user_id:
        raise HTTPException(404, "Session not found")
    return session


def state(session):
    if session.finished_at is not None:
        return "completed"
    if time.time() > session.deadline:
        return "overtime" if session.settings["allow_overtime"] else "expired"
    return "active"


def serialize(db, session, detail=False):
    work = list(db.scalars(select(Work).where(Work.session_id == session.id)))
    expected = sum(len(PROBLEMS[pid]["cases"]) for pid in session.problem_ids)
    passed = sum(w.result["passed"] for w in work if w.result)
    result = {
        "id": session.id, "started_at": session.started_at, "deadline": session.deadline,
        "finished_at": session.finished_at, "status": state(session), "settings": session.settings,
        "problems": [{k: PROBLEMS[pid][k] for k in ("id", "title", "category", "difficulty")} for pid in session.problem_ids],
        "correctness": round(100 * passed / expected, 1),
        "submitted": sum(w.result is not None for w in work), "result_source": "browser_practice",
        "server_now": time.time(),
    }
    if detail:
        result["work"] = {w.problem_id: {"code": w.code, "revision": w.revision, "result": w.result} for w in work}
    return result


@router.get("/catalog")
def catalog():
    return {"problems": summaries(), "execution": "browser_python", "test_visibility": "inspectable"}


@router.get("/problems/{problem_id}")
def problem(problem_id: str, identity=Depends(auth.current_login)):
    if problem_id not in PROBLEMS:
        raise HTTPException(404, "Problem not found")
    return PROBLEMS[problem_id]


@router.post("/practice", status_code=201)
def start(settings: Settings, request: Request, identity=Depends(auth.authenticated_write)):
    categories = {p["category"] for p in PROBLEMS.values()}
    if set(settings.categories) - categories:
        raise HTTPException(422, "Unknown problem category")
    available = [p for p in PROBLEMS.values() if (not settings.categories or p["category"] in settings.categories) and (settings.difficulty == "mixed" or p["difficulty"] == settings.difficulty)]
    if settings.problem_ids:
        ids = settings.problem_ids
        allowed = {p["id"] for p in available}
        if len(ids) != settings.problem_count or len(set(ids)) != len(ids) or not set(ids) <= allowed:
            raise HTTPException(422, "Selected problems must be distinct and match the filters and problem count")
    else:
        if len(available) < settings.problem_count:
            raise HTTPException(422, "Not enough problems match these filters. Choose fewer problems or broader filters.")
        ids = [p["id"] for p in random.SystemRandom().sample(available, settings.problem_count)]
    now = time.time()
    with request.app.state.database.session.begin() as db:
        session = PracticeSession(id=secrets.token_hex(16), user_id=identity[0].id, started_at=now, deadline=now + settings.duration_minutes * 60, settings=settings.model_dump(), problem_ids=ids)
        db.add(session)
        db.flush()
        for pid in ids:
            db.add(Work(session_id=session.id, problem_id=pid, code=PROBLEMS[pid]["starter"], revision=0))
        db.flush()
        return serialize(db, session, True)


@router.get("/practice")
def history(request: Request, identity=Depends(auth.current_login)):
    with request.app.state.database.session() as db:
        sessions = db.scalars(select(PracticeSession).where(PracticeSession.user_id == identity[0].id).order_by(PracticeSession.started_at.desc()).limit(30))
        return {"sessions": [serialize(db, s) for s in sessions]}


@router.get("/practice/{session_id}")
def get_session(session_id: str, request: Request, identity=Depends(auth.current_login)):
    with request.app.state.database.session() as db:
        return serialize(db, owned(db, session_id, identity[0].id), True)


@router.put("/practice/{session_id}/work/{problem_id}")
def save_work(session_id: str, problem_id: str, body: SaveWork, request: Request, identity=Depends(auth.authenticated_write)):
    with request.app.state.database.session.begin() as db:
        session = owned(db, session_id, identity[0].id)
        if problem_id not in session.problem_ids:
            raise HTTPException(404, "Problem is not in this session")
        if state(session) in ("completed", "expired"):
            raise HTTPException(409, "This session has ended")
        work = db.get(Work, (session_id, problem_id))
        report = body.report
        result = work.result if work.code == body.code else None
        if report:
            count = len(PROBLEMS[problem_id]["cases"])
            if report.total != count or report.passed > count or (not report.error and len(report.cases) != count) or report.passed != sum(c.passed for c in report.cases):
                raise HTTPException(422, "Result does not match this problem's practice suite")
            if report.passed != count and (report.runtime_ms is not None or report.reference_ms is not None):
                raise HTTPException(422, "Efficiency comparison requires all tests to pass")
            result = report.model_dump() | {"source": "browser_practice", "submitted_at": time.time()}
        changed = db.execute(update(Work).where(Work.session_id == session_id, Work.problem_id == problem_id, Work.revision == body.revision).values(code=body.code, result=result, revision=Work.revision + 1))
        if changed.rowcount != 1:
            raise HTTPException(409, "This draft changed in another tab. Reload before saving.")
        return {"revision": body.revision + 1, "result": result}


@router.post("/practice/{session_id}/finish")
def finish(session_id: str, request: Request, identity=Depends(auth.authenticated_write)):
    with request.app.state.database.session.begin() as db:
        session = owned(db, session_id, identity[0].id)
        if session.finished_at is None:
            session.finished_at = time.time()
        db.flush()
        return serialize(db, session, True)
