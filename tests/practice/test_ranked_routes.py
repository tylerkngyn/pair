import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.practice import ranked_routes


@pytest.fixture(autouse=True)
def clear_queue_state():
    ranked_routes._QUEUE.clear()
    ranked_routes._PENDING_MATCH.clear()
    yield
    ranked_routes._QUEUE.clear()
    ranked_routes._PENDING_MATCH.clear()


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


class Identity:
    def __init__(self, cookie, csrf):
        self.cookie = cookie
        self.csrf = csrf


def register(client, name) -> Identity:
    client.cookies.clear()
    response = client.post("/api/auth/register", json={"username": name, "password": "good-password-123"})
    assert response.status_code == 201, response.text
    return Identity(client.cookies.get("pair_session", domain="testserver.local"), response.json()["csrf"])


def as_(client, identity: Identity):
    client.cookies.clear()
    client.cookies.set("pair_session", identity.cookie, domain="testserver.local")
    client.headers["x-csrf-token"] = identity.csrf
    return client


FILTERS = {"categories": [], "difficulty": "mixed", "duration_minutes": 30, "problem_count": 1}


def test_queue_then_match_then_poll(client):
    alice = register(client, "alice")
    as_(client, alice)
    queued = client.post("/api/ranked/queue", json=FILTERS)
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"

    bob = register(client, "bob")
    as_(client, bob)
    matched = client.post("/api/ranked/queue", json=FILTERS)
    assert matched.status_code == 202
    assert matched.json()["status"] == "matched"
    match_id = matched.json()["match_id"]

    as_(client, alice)
    poll = client.get("/api/ranked/queue")
    assert poll.json() == {"status": "matched", "match_id": match_id}
    # Consumed by the first poll; a second poll finds nothing pending.
    assert client.get("/api/ranked/queue").json() == {"status": "idle"}


def test_double_queue_rejected(client):
    alice = register(client, "alice")
    as_(client, alice)
    client.post("/api/ranked/queue", json=FILTERS)
    assert client.post("/api/ranked/queue", json=FILTERS).status_code == 409


def test_leave_queue(client):
    alice = register(client, "alice")
    as_(client, alice)
    client.post("/api/ranked/queue", json=FILTERS)
    assert client.delete("/api/ranked/queue").status_code == 200
    assert client.get("/api/ranked/queue").json() == {"status": "idle"}
    assert client.delete("/api/ranked/queue").status_code == 404


def test_rating_defaults_and_tier(client):
    alice = register(client, "alice")
    as_(client, alice)
    response = client.get("/api/ranked/rating")
    assert response.json() == {"rating": 1000, "matches_played": 0, "tier": "Gold"}


def _match_two_players(client):
    alice = register(client, "alice")
    as_(client, alice)
    client.post("/api/ranked/queue", json=FILTERS)
    bob = register(client, "bob")
    as_(client, bob)
    match_id = client.post("/api/ranked/queue", json=FILTERS).json()["match_id"]
    return alice, bob, match_id


def _submit(client, identity, match_id, problem_id, passed, total):
    as_(client, identity)
    report = {
        "passed": passed, "total": total,
        "cases": [{"passed": i < passed} for i in range(total)],
    }
    return client.put(f"/api/ranked/matches/{match_id}/work/{problem_id}", json={"code": "x", "revision": 0, "report": report})


def test_submission_plausibility_floor_rejects_instant_pass(client):
    alice, bob, match_id = _match_two_players(client)
    problem_id = client.get(f"/api/ranked/matches/{match_id}").json()["problems"][0]["id"]
    from app.practice.catalog import PROBLEMS
    total = len(PROBLEMS[problem_id]["cases"])
    response = _submit(client, alice, match_id, problem_id, total, total)
    assert response.status_code == 422
    assert "implausibly fast" in response.json()["detail"]


def test_finalize_after_deadline_awards_winner(client, monkeypatch):
    monkeypatch.setattr(ranked_routes, "SUBMISSION_PLAUSIBILITY_FLOOR_SECONDS", 0)
    alice, bob, match_id = _match_two_players(client)
    from app.practice.catalog import PROBLEMS
    problem_id = client.get(f"/api/ranked/matches/{match_id}").json()["problems"][0]["id"]
    total = len(PROBLEMS[problem_id]["cases"])

    assert _submit(client, alice, match_id, problem_id, total, total).status_code == 200
    assert _submit(client, bob, match_id, problem_id, 0, total).status_code == 200

    match = ranked_routes.RankedMatch
    with client.app.state.database.session.begin() as db:
        row = db.get(match, match_id)
        row.deadline = time.time() - 1

    as_(client, alice)
    result = client.get(f"/api/ranked/matches/{match_id}").json()
    assert result["status"] == "completed"
    assert result["my_outcome"] == "win"
    assert result["my_rating_after"] == 1016
    assert result["opponent_outcome"] == "loss"
    assert result["opponent_rating_after"] == 984


def test_history_lists_opponent_and_outcome(client, monkeypatch):
    monkeypatch.setattr(ranked_routes, "SUBMISSION_PLAUSIBILITY_FLOOR_SECONDS", 0)
    alice, bob, match_id = _match_two_players(client)
    from app.practice.catalog import PROBLEMS
    problem_id = client.get(f"/api/ranked/matches/{match_id}").json()["problems"][0]["id"]
    total = len(PROBLEMS[problem_id]["cases"])
    _submit(client, alice, match_id, problem_id, total, total)
    _submit(client, bob, match_id, problem_id, 0, total)
    with client.app.state.database.session.begin() as db:
        row = db.get(ranked_routes.RankedMatch, match_id)
        row.deadline = time.time() - 1
    as_(client, alice)
    client.get(f"/api/ranked/matches/{match_id}")  # triggers finalization

    history = client.get("/api/ranked/history").json()["matches"]
    assert len(history) == 1
    assert history[0]["opponent"] == "bob"
    assert history[0]["outcome"] == "win"


def test_match_found_pushed_over_websocket_to_both_sides(client):
    """End-to-end over the real cookie-identified /ws path: a queued
    account's open socket receives MATCH_FOUND the moment the opponent's
    POST /api/ranked/queue matches it, with no polling involved.
    """
    alice = register(client, "alice")
    as_(client, alice)
    with client.websocket_connect("/ws") as alice_ws:
        client.post("/api/ranked/queue", json=FILTERS)

        bob = register(client, "bob")
        as_(client, bob)
        with client.websocket_connect("/ws") as bob_ws:
            matched = client.post("/api/ranked/queue", json=FILTERS)
            match_id = matched.json()["match_id"]

            bob_event = bob_ws.receive_json()
            assert bob_event["type"] == "match_found"
            assert bob_event["payload"]["match_id"] == match_id
            assert bob_event["payload"]["opponent"] == "alice"

        alice_event = alice_ws.receive_json()
        assert alice_event["type"] == "match_found"
        assert alice_event["payload"]["match_id"] == match_id
        assert alice_event["payload"]["opponent"] == "bob"

    # Consumed by delivery, not left pending for the poll fallback.
    as_(client, alice)
    assert client.get("/api/ranked/queue").json() == {"status": "idle"}


def test_opponent_progress_and_match_result_pushed_over_websocket(client, monkeypatch):
    monkeypatch.setattr(ranked_routes, "SUBMISSION_PLAUSIBILITY_FLOOR_SECONDS", 0)
    from app.practice.catalog import PROBLEMS

    alice = register(client, "alice")
    as_(client, alice)
    with client.websocket_connect("/ws") as alice_ws:
        client.post("/api/ranked/queue", json=FILTERS)
        bob = register(client, "bob")
        as_(client, bob)
        with client.websocket_connect("/ws") as bob_ws:
            match_id = client.post("/api/ranked/queue", json=FILTERS).json()["match_id"]
            bob_ws.receive_json()  # match_found
            alice_ws.receive_json()  # match_found

            problem_id = client.get(f"/api/ranked/matches/{match_id}").json()["problems"][0]["id"]
            total = len(PROBLEMS[problem_id]["cases"])

            as_(client, bob)
            _submit(client, bob, match_id, problem_id, total, total)
            progress = alice_ws.receive_json()
            assert progress["type"] == "opponent_progress"
            assert progress["payload"]["correctness"] == 100.0
            assert progress["payload"]["problem_statuses"][problem_id] == "passed"

            with client.app.state.database.session.begin() as db:
                row = db.get(ranked_routes.RankedMatch, match_id)
                row.deadline = time.time() - 1

            as_(client, alice)
            result = client.get(f"/api/ranked/matches/{match_id}").json()
            assert result["my_outcome"] == "loss"

            alice_result = alice_ws.receive_json()
            assert alice_result["type"] == "match_result"
            assert alice_result["payload"]["outcome"] == "loss"

            bob_result = bob_ws.receive_json()
            assert bob_result["type"] == "match_result"
            assert bob_result["payload"]["outcome"] == "win"
