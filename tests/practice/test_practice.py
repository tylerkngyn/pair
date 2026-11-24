import importlib.util
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.practice.catalog import PROBLEMS, RAW
from app.practice.database import Login, PracticeSession, User

spec = importlib.util.spec_from_file_location("practice_harness", Path(__file__).parents[2] / "static/practice/harness.py")
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def register(client, name="alice"):
    response = client.post("/api/auth/register", json={"username": name, "password": "good-password-123"})
    assert response.status_code == 201, response.text
    client.headers["x-csrf-token"] = response.json()["csrf"]
    return response


def start(client, **settings):
    response = client.post("/api/practice", json={"problem_ids": ["contains-duplicate"], **settings})
    assert response.status_code == 201, response.text
    return response.json()


def test_catalog_complete_and_unique():
    assert len(RAW["problems"]) == len(PROBLEMS) == 150
    assert len({p["number"] for p in PROBLEMS.values()}) == 150
    assert all(len(p["cases"]) >= 2 for p in PROBLEMS.values())


@pytest.mark.parametrize("pid", list(PROBLEMS))
def test_reference_and_starter_against_practice_suite(pid):
    problem = PROBLEMS[pid]
    result = harness.evaluate(problem, problem["reference"])
    assert result["passed"] == result["total"], result
    assert result["runtime_ms"] >= 0
    assert result["reference_ms"] >= 0
    starter = harness.evaluate(problem, problem["starter"])
    assert starter["passed"] < starter["total"], (pid, starter)


def test_harness_alternatives_errors_and_deep_copies():
    p = PROBLEMS["longest-palindromic-substring"]
    assert harness.matches(p, p["cases"][0], "aba")
    p = PROBLEMS["course-schedule-ii"]
    assert harness.matches(p, p["cases"][0], [1, 0, 2])
    assert not harness.matches(p, p["cases"][0], [2, 0, 1])
    p = PROBLEMS["clone-graph"]
    bad = harness.evaluate(p, "class Solution:\n def cloneGraph(self, node): return node")
    assert bad["passed"] < bad["total"]
    error = harness.evaluate(PROBLEMS["contains-duplicate"], "def broken(")
    assert error["passed"] == 0 and "SyntaxError" in error["error"]


def test_accounts_csrf_revocation_and_password_hash(client):
    assert client.get("/api/auth/me").status_code == 401
    response = register(client)
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=strict" in response.headers["set-cookie"]
    assert client.get("/api/auth/me").json()["username"] == "alice"
    assert client.post("/api/practice", json={}, headers={"x-csrf-token": "wrong"}).status_code == 403
    assert client.post("/api/practice", json={}, headers={"origin": "null"}).status_code == 403
    assert client.post("/api/auth/register", json={"username":"other", "password":"good-password-123"}, headers={"origin":"https://evil.example"}).status_code == 403
    with app.state.database.session() as db:
        user = db.scalar(select(User))
        assert user.password_hash.startswith("scrypt$") and "good-password" not in user.password_hash
        login = db.scalar(select(Login))
        assert login.token_hash != client.cookies["pair_session"]
    old_cookie = client.cookies["pair_session"]
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.set("pair_session", old_cookie)
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.clear()
    assert client.post("/api/auth/login", json={"username":"alice", "password":"wrong-password"}).status_code == 401
    assert client.post("/api/auth/login", json={"username":"ALICE", "password":"good-password-123"}).status_code == 200


def test_account_validation_and_throttle(client):
    assert client.post("/api/auth/register", json={"username":"bad name", "password":"good-password-123"}).status_code == 422
    register(client)
    assert client.post("/api/auth/register", json={"username":"alice", "password":"good-password-123"}).status_code == 409
    for _ in range(12):
        result = client.post("/api/auth/login", json={"username":"unknown", "password":"wrong-password"})
        assert result.status_code == 401
    assert client.post("/api/auth/login", json={"username":"unknown", "password":"wrong-password"}).status_code == 429


def test_submission_draft_revisions_finish_and_history(client):
    register(client)
    session = start(client)
    pid = "contains-duplicate"
    url = f"/api/practice/{session['id']}/work/{pid}"
    code = "class Solution:\n def containsDuplicate(self, nums): return len(set(nums)) != len(nums)"
    report = harness.evaluate(PROBLEMS[pid], code)
    saved = client.put(url, json={"code":code, "revision":0, "report":report})
    assert saved.status_code == 200, saved.text
    assert saved.json()["result"]["source"] == "browser_practice"
    assert client.put(url, json={"code":"stale", "revision":0}).status_code == 409
    session = client.get(f"/api/practice/{session['id']}").json()
    assert session["correctness"] == 100
    assert session["work"][pid]["code"] == code
    # Editing code invalidates the old result; it cannot score a different draft.
    assert client.put(url, json={"code":"changed", "revision":1}).json()["result"] is None
    assert client.get(f"/api/practice/{session['id']}").json()["correctness"] == 0
    bad = {**report, "total":999}
    assert client.put(url, json={"code":code, "revision":2, "report":bad}).status_code == 422
    assert client.put(url, json={"code":code, "revision":2, "report":report}).status_code == 200
    finished = client.post(f"/api/practice/{session['id']}/finish").json()
    assert finished["status"] == "completed" and finished["correctness"] == 100
    assert client.put(url, json={"code":code, "revision":3}).status_code == 409
    assert client.get("/api/practice").json()["sessions"][0]["id"] == session["id"]


def test_session_access_isolation(client):
    register(client)
    session = start(client)
    client.post("/api/auth/logout")
    register(client, "bob")
    prefix = f"/api/practice/{session['id']}"
    assert client.get(prefix).status_code == 404
    assert client.post(prefix + "/finish").status_code == 404
    assert client.put(prefix + "/work/contains-duplicate", json={"code":"x", "revision":0}).status_code == 404
    assert client.get("/api/practice").json() == {"sessions":[]}


def test_filters_counts_deadlines_and_overtime(client):
    register(client)
    assert client.post("/api/practice", json={"categories":["unknown"]}).status_code == 422
    assert client.post("/api/practice", json={"problem_count":6}).status_code == 422
    response = client.post("/api/practice", json={"categories":["Graphs"], "difficulty":"medium", "problem_count":2})
    assert response.status_code == 201, response.text
    assert all(p["category"] == "Graphs" and p["difficulty"] == "medium" for p in response.json()["problems"])
    session = start(client, allow_overtime=False)
    with app.state.database.session.begin() as db:
        db.get(PracticeSession, session["id"]).deadline = time.time() - 1
    prefix = f"/api/practice/{session['id']}"
    assert client.get(prefix).json()["status"] == "expired"
    assert client.put(prefix + "/work/contains-duplicate", json={"code":"x", "revision":0}).status_code == 409
    session = start(client, allow_overtime=True)
    with app.state.database.session.begin() as db:
        db.get(PracticeSession, session["id"]).deadline = time.time() - 1
    prefix = f"/api/practice/{session['id']}"
    assert client.get(prefix).json()["status"] == "overtime"
    assert client.put(prefix + "/work/contains-duplicate", json={"code":"x", "revision":0}).status_code == 200


def test_execution_boundary_and_security_headers(client):
    assert client.get("/").headers["content-security-policy"].startswith("default-src 'self'")
    policy = client.get("/assets/runner.html").headers["content-security-policy"]
    assert "sandbox allow-scripts" in policy and "allow-same-origin" not in policy
    assert "connect-src 'none'" in policy
    assert "cdn.jsdelivr.net" not in policy
    manifest = client.get("/assets/runtime/314.0.8/manifest.json").json()
    assert manifest["version"] == "314.0.8"
    assert len(manifest["files"]) == 5
    for file in manifest["files"]:
        response = client.get("/assets/runtime/314.0.8/" + file["name"])
        assert len(response.content) == file["bytes"]
        assert "immutable" in response.headers["cache-control"]
    assert client.get("/assets/runtime/314.0.8/pyodide.asm.wasm").headers["content-type"] == "application/wasm"
    register(client)
    session = start(client)
    # Code remains plain text in storage; a submission never runs in FastAPI.
    code = "raise Exception('MUST NEVER EXECUTE ON SERVER')"
    response = client.put(f"/api/practice/{session['id']}/work/contains-duplicate", json={"code":code, "revision":0})
    assert response.status_code == 200
    assert client.get("/api/practice").headers["cache-control"] == "no-store"


def test_accounts_and_drafts_survive_application_restart():
    with TestClient(app) as first:
        register(first)
        session = start(first)
        url = f"/api/practice/{session['id']}/work/contains-duplicate"
        assert first.put(url, json={"code":"# persistent draft", "revision":0}).status_code == 200
    # Same database, new lifecycle and cookie: persistence is independent of the
    # in-memory room manager and the original browser login.
    with TestClient(app) as restarted:
        login = restarted.post("/api/auth/login", json={"username":"alice", "password":"good-password-123"})
        assert login.status_code == 200
        restored = restarted.get(f"/api/practice/{session['id']}").json()
        assert restored["work"]["contains-duplicate"]["code"] == "# persistent draft"
        assert restored["work"]["contains-duplicate"]["revision"] == 1


def test_multi_problem_session_includes_unsubmitted_tests(client):
    register(client)
    session = start(client, problem_count=2, problem_ids=["contains-duplicate", "valid-anagram"])
    p = PROBLEMS["contains-duplicate"]
    report = harness.evaluate(p, p["reference"])
    response = client.put(f"/api/practice/{session['id']}/work/contains-duplicate", json={"code":p["reference"], "revision":0, "report":report})
    assert response.status_code == 200
    result = client.post(f"/api/practice/{session['id']}/finish").json()
    assert result["correctness"] == round(100 * len(p["cases"]) / (len(p["cases"]) + len(PROBLEMS["valid-anagram"]["cases"])), 1)
    assert result["submitted"] == 1
