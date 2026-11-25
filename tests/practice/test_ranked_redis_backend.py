"""Proves the Redis matchmaking backend works through the real HTTP app,
not just as a standalone module (tests/test_redis_ranked_queue.py already
covers the matching logic itself in isolation). app.state.ranked_queue is
set directly here rather than via REDIS_URL, since the lifespan branch
that does that is a two-line wire-up with nothing left to prove once this
passes against the same app.state attribute it sets.
"""
import asyncio
import time

import pytest
from fakeredis import FakeServer
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.practice import ranked_routes
from app.practice.database import RankedMatch, RankedParticipant
from app.practice.redis_ranked_queue import RedisRankedQueue

FILTERS = {"categories": [], "difficulty": "mixed", "duration_minutes": 30, "problem_count": 1}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(ranked_routes, 'RANKED_WARMUP_SECONDS', 0)
    with TestClient(app) as c:
        # Exposed so a test can build a second RedisRankedQueue against the
        # same backing store, simulating a second worker racing the first.
        c.ranked_queue_server = FakeServer()
        c.app.state.ranked_queue = RedisRankedQueue(client=FakeRedis(server=c.ranked_queue_server, decode_responses=True))
        try:
            yield c
        finally:
            del c.app.state.ranked_queue


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


def test_queue_then_match_over_redis_backend(client):
    alice = register(client, "alice")
    as_(client, alice)
    queued = client.post("/api/ranked/queue", json=FILTERS)
    assert queued.status_code == 202
    assert queued.json()["status"] == "queued"

    bob = register(client, "bob")
    as_(client, bob)
    matched = client.post("/api/ranked/queue", json=FILTERS)
    assert matched.json()["status"] == "matched"
    match_id = matched.json()["match_id"]

    as_(client, alice)
    poll = client.get("/api/ranked/queue")
    assert poll.json() == {"status": "matched", "match_id": match_id}
    assert client.get("/api/ranked/queue").json() == {"status": "idle"}

    detail = client.get(f"/api/ranked/matches/{match_id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == match_id


def test_double_join_rejected_over_redis_backend(client):
    alice = register(client, "alice")
    as_(client, alice)
    client.post("/api/ranked/queue", json=FILTERS)
    assert client.post("/api/ranked/queue", json=FILTERS).status_code == 409


def test_redis_match_uses_shared_future_start(client, monkeypatch):
    monkeypatch.setattr(ranked_routes, 'RANKED_WARMUP_SECONDS', 7)
    alice = register(client, 'alice'); as_(client, alice)
    client.post('/api/ranked/queue', json=FILTERS)
    bob = register(client, 'bob'); as_(client, bob)
    mid = client.post('/api/ranked/queue', json=FILTERS).json()['match_id']
    first = client.get(f'/api/ranked/matches/{mid}').json()
    as_(client, alice)
    second = client.get(f'/api/ranked/matches/{mid}').json()
    assert first['started_at'] == second['started_at'] > second['server_now']
    assert first['deadline'] - first['started_at'] == FILTERS['duration_minutes'] * 60
    assert second['intro']['starts_at'] is None
    as_(client, alice).post(f'/api/ranked/matches/{mid}/ready')
    as_(client, bob)
    scheduled = client.post(f'/api/ranked/matches/{mid}/ready').json()
    assert scheduled['intro']['ready_count'] == 2
    assert scheduled['intro']['starts_at'] == scheduled['started_at'] > scheduled['server_now']


def test_leave_over_redis_backend(client):
    alice = register(client, "alice")
    as_(client, alice)
    client.post("/api/ranked/queue", json=FILTERS)
    assert client.delete("/api/ranked/queue").status_code == 200
    assert client.get("/api/ranked/queue").json() == {"status": "idle"}
    assert client.delete("/api/ranked/queue").status_code == 404


def _user_id(client, identity: Identity) -> str:
    """Reads the account's id straight from its own login row, without
    touching the shared client's active cookies/csrf header — callers may
    be mid-test with a different identity set as "current".
    """
    from app.practice.auth import token_hash
    from app.practice.database import Login
    with client.app.state.database.session() as db:
        return db.get(Login, token_hash(identity.cookie)).user_id


def test_poll_widens_and_matches_over_redis_backend(client):
    import asyncio

    from app.practice.redis_ranked_queue import filters_key

    alice = register(client, "alice")
    as_(client, alice)
    client.post("/api/ranked/queue", json=FILTERS)
    alice_id = _user_id(client, alice)

    # Push alice's own queued entry's rating outside bob's default ±50
    # window, bypassing the HTTP layer (which has no way to set a custom
    # starting rating) — same trick the in-process test uses.
    rq = client.app.state.ranked_queue
    group = filters_key(FILTERS)

    async def mutate(coro_calls):
        for call in coro_calls:
            await call

    asyncio.run(mutate([
        rq._redis.zadd(rq._queue_key(group), {alice_id: 1200}),
        rq._redis.hset(rq._meta_key(alice_id), "rating", 1200),
    ]))

    bob = register(client, "bob")
    as_(client, bob)
    assert client.post("/api/ranked/queue", json=FILTERS).json()["status"] == "queued"
    bob_id = _user_id(client, bob)

    # Backdate both sides' queued_at so a poll's widened window (based on
    # whichever waited longer) now covers the 200-point gap.
    asyncio.run(mutate([
        rq._redis.hset(rq._meta_key(alice_id), "queued_at", 0),
        rq._redis.hset(rq._meta_key(bob_id), "queued_at", 0),
    ]))

    as_(client, alice)
    polled = client.get("/api/ranked/queue")
    assert polled.json()["status"] == "matched"


def test_match_found_pushed_over_websocket_with_redis_backend(client):
    """Same end-to-end shape as the in-process WS test, over the Redis
    matchmaking path - proves MATCH_FOUND delivery and the Redis
    pending-mailbox pop-on-delivery both work through the real app.
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

        alice_event = alice_ws.receive_json()
        assert alice_event["type"] == "match_found"
        assert alice_event["payload"]["match_id"] == match_id

    # Delivered, so the Redis pending mailbox should already be consumed.
    as_(client, alice)
    assert client.get("/api/ranked/queue").json() == {"status": "idle"}


def _matched_pair_past_deadline(client):
    alice = register(client, "alice")
    as_(client, alice)
    client.post("/api/ranked/queue", json=FILTERS)
    bob = register(client, "bob")
    as_(client, bob)
    match_id = client.post("/api/ranked/queue", json=FILTERS).json()["match_id"]
    with client.app.state.database.session.begin() as db:
        db.get(RankedMatch, match_id).deadline = time.time() - 1
    return alice, bob, match_id


def test_losing_the_finalize_lock_race_never_mutates_anything(client):
    """The double-finalize race the Obsidian plan calls out: two workers
    both notice the deadline passed for the same match near-simultaneously.
    Simulated here by having a second worker win the lock a moment
    earlier (through its own queue client against the same backing
    Redis) and confirming the real finalize call then does nothing at
    all — not even the Elo computation runs, let alone a DB write.
    """
    _alice, _bob, match_id = _matched_pair_past_deadline(client)

    other_worker_queue = RedisRankedQueue(client=FakeRedis(server=client.ranked_queue_server, decode_responses=True))
    won = asyncio.run(other_worker_queue.acquire_finalize_lock(match_id, "other-worker"))
    assert won is True

    with client.app.state.database.session.begin() as db:
        match = db.get(RankedMatch, match_id)
        result = asyncio.run(ranked_routes._finalize_if_ready(db, match, client.app.state.ranked_queue))
    assert result is None

    with client.app.state.database.session() as db:
        match = db.get(RankedMatch, match_id)
        assert match.status == "active"
        assert match.finished_at is None
        participants = db.scalars(select(RankedParticipant).where(RankedParticipant.match_id == match_id)).all()
        assert all(p.outcome is None and p.rating_after is None for p in participants)


def test_finalize_still_works_normally_with_the_lock_in_place(client):
    """Confirms the lock is additive, not a regression: a single worker
    with no contention still finalizes exactly as it did before the lock
    existed.
    """
    alice, _bob, match_id = _matched_pair_past_deadline(client)

    as_(client, alice)
    result = client.get(f"/api/ranked/matches/{match_id}")
    assert result.status_code == 200
    assert result.json()["status"] == "completed"
