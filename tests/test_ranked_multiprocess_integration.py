"""Real multi-process, real-Redis, real-network load test for ranked
mode's matchmaking and finalization — the double-finalize race
specifically, which tests/test_redis_ranked_queue.py and
tests/practice/test_ranked_redis_backend.py's fakeredis-based tests
cannot actually prove, since fakeredis runs in-process and single-threaded:
nothing in those tests is ever truly concurrent at the OS/network level.
This spins up two real uvicorn worker processes sharing one real Redis
(same infra tests/test_multiprocess_integration.py already uses for the
room manager), has both independently notice the same match's deadline
passed, and confirms the Elo update landed exactly once rather than
compounding.

Opt-in only, same as tests/test_multiprocess_integration.py: excluded from
the default `pytest -q` run, requires Docker or PAIR_TEST_REDIS_URL.
Run explicitly with: python -m pytest -m integration -v
"""

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_multiprocess_integration import REPO_ROOT, _free_port, _http_ok, _wait_for, real_redis_url  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture
def two_ranked_workers(real_redis_url):
    """Like test_multiprocess_integration.py's two_workers, but also
    points both processes at one shared, disposable SQLite file — ranked
    matchmaking needs the database both workers see, not just Redis, and
    this must never be the real var/pair.db a developer might be using
    locally.
    """
    ports = [_free_port(), _free_port()]
    state_dir = tempfile.mkdtemp(prefix="pair-ranked-integration-")
    db_path = Path(state_dir) / "pair.db"
    env = {**os.environ, "REDIS_URL": real_redis_url, "PAIR_STATE_DIR": state_dir}
    # tests/conftest.py's autouse isolated_database fixture sets DATABASE_URL
    # on THIS process for its own per-test isolation; Database() checks
    # DATABASE_URL before PAIR_STATE_DIR, so blindly inheriting it via
    # **os.environ would point both subprocesses at pytest's own tmp_path
    # db instead of the shared one these two workers actually need.
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    procs = []
    try:
        for port in ports:
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=REPO_ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            procs.append(proc)
        for port in ports:
            _wait_for(
                lambda port=port: _http_ok(f"http://127.0.0.1:{port}/health"),
                timeout=10, description=f"uvicorn on port {port} to become healthy",
            )
        yield ports, db_path
    finally:
        for proc in procs:
            proc.terminate()
        deadline = time.monotonic() + 5
        for proc in procs:
            try:
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
            if proc.returncode not in (0, None, -15, -9):
                output = proc.stdout.read() if proc.stdout else ""
                print(f"uvicorn worker exited with {proc.returncode}:\n{output}")
        shutil.rmtree(state_dir, ignore_errors=True)


FILTERS = {"categories": [], "difficulty": "mixed", "duration_minutes": 30, "problem_count": 1}


async def _register(client: httpx.AsyncClient, base_url: str, username: str) -> str:
    response = await client.post(f"{base_url}/api/auth/register", json={"username": username, "password": "good-password-123"})
    response.raise_for_status()
    return response.json()["csrf"]


def test_cross_process_matchmaking_and_exactly_once_finalization(two_ranked_workers):
    """Two accounts queue on two different real processes, get matched
    across them, and both processes independently hit the deadline-passed
    path for the same match at nearly the same moment — the actual
    double-finalize race, not a simulated one. The Elo update must land
    exactly once: ratings end at the normal post-match values (1016/984
    for a first match between two 1000-rated accounts), not some other
    value a double-apply would produce.
    """
    ports, db_path = two_ranked_workers
    base_a, base_b = f"http://127.0.0.1:{ports[0]}", f"http://127.0.0.1:{ports[1]}"

    async def scenario():
        async with httpx.AsyncClient(cookies=httpx.Cookies()) as alice, httpx.AsyncClient(cookies=httpx.Cookies()) as bob:
            alice_csrf = await _register(alice, base_a, "alice")
            bob_csrf = await _register(bob, base_b, "bob")

            queued = await alice.post(f"{base_a}/api/ranked/queue", json=FILTERS, headers={"x-csrf-token": alice_csrf})
            assert queued.json()["status"] == "queued"

            matched = await bob.post(f"{base_b}/api/ranked/queue", json=FILTERS, headers={"x-csrf-token": bob_csrf})
            assert matched.json()["status"] == "matched", matched.text
            match_id = matched.json()["match_id"]

            # Alice may have learned about the match via her own poll
            # rather than a WS push in this real-process run; either way
            # she must agree on the match_id before we race finalization.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                polled = await alice.get(f"{base_a}/api/ranked/queue", headers={"x-csrf-token": alice_csrf})
                if polled.json().get("match_id") == match_id:
                    break
                await asyncio.sleep(0.2)
            else:
                pytest.fail("Alice's process never learned about the match")

            # Both browser clients finish loading/showing versus before the
            # server schedules the shared start, regardless of worker affinity.
            ready = await asyncio.gather(
                alice.post(f"{base_a}/api/ranked/matches/{match_id}/ready", headers={"x-csrf-token":alice_csrf}),
                bob.post(f"{base_b}/api/ranked/matches/{match_id}/ready", headers={"x-csrf-token":bob_csrf}),
            )
            assert all(response.status_code == 200 for response in ready)

            # Submit a real, correct, all-passing solution for alice so
            # there's an actual winner (not just a double-abandonment,
            # which the fakeredis-based test already covers in depth) —
            # this exercises the win/loss Elo path specifically.
            problems = (await alice.get(f"{base_a}/api/ranked/matches/{match_id}", headers={"x-csrf-token": alice_csrf})).json()["problems"]
            problem_id = problems[0]["id"]
            catalog = (await alice.get(f"{base_a}/api/catalog")).json()
            total = next(p["test_count"] for p in catalog["problems"] if p["id"] == problem_id)
            report = {"passed": total, "total": total, "cases": [{"passed": True}] * total}
            submit_deadline = time.monotonic() + 25
            while time.monotonic() < submit_deadline:
                submitted = await alice.put(
                    f"{base_a}/api/ranked/matches/{match_id}/work/{problem_id}",
                    json={"code": "x", "revision": 0, "report": report},
                    headers={"x-csrf-token": alice_csrf},
                )
                if submitted.status_code == 200:
                    break
                await asyncio.sleep(1)  # Warmup countdown, then the 10s submission floor.
            else:
                pytest.fail(f"Alice's submission never succeeded: {submitted.status_code} {submitted.text}")

            # Force the shared deadline into the past directly in the one
            # database file both processes point at (DATABASE_URL above).
            from app.practice.database import Database, RankedMatch
            db = Database(url=f"sqlite:///{db_path}")
            with db.session.begin() as session:
                session.get(RankedMatch, match_id).deadline = time.time() - 1
            db.close()

            # The actual race: both processes independently read the
            # match, see the deadline has passed, and both race for the
            # finalize lock within the same instant. Whichever loses the
            # race may still report "active" in THIS particular response
            # if its own read happened just before the winner's commit —
            # that's ordinary eventual consistency between two
            # near-simultaneous reads, not a bug; the real property this
            # test cares about is that the Elo update itself landed
            # exactly once, checked via the final ratings below.
            result_a, result_b = await asyncio.gather(
                alice.get(f"{base_a}/api/ranked/matches/{match_id}", headers={"x-csrf-token": alice_csrf}),
                bob.get(f"{base_b}/api/ranked/matches/{match_id}", headers={"x-csrf-token": bob_csrf}),
            )
            assert result_a.status_code == 200 and result_b.status_code == 200

            # Confirm it did actually finalize (eventually) rather than
            # both sides having silently lost / neither ever landing.
            finalized_deadline = time.monotonic() + 5
            while time.monotonic() < finalized_deadline:
                check = await alice.get(f"{base_a}/api/ranked/matches/{match_id}", headers={"x-csrf-token": alice_csrf})
                if check.json()["status"] == "completed":
                    break
                await asyncio.sleep(0.2)
            else:
                pytest.fail("Match never finalized at all")

            alice_rating = (await alice.get(f"{base_a}/api/ranked/rating", headers={"x-csrf-token": alice_csrf})).json()
            bob_rating = (await bob.get(f"{base_b}/api/ranked/rating", headers={"x-csrf-token": bob_csrf})).json()
            assert alice_rating["rating"] == 1016, "alice's winning rating was applied more than once"
            assert bob_rating["rating"] == 984, "bob's losing rating was applied more than once"
            assert alice_rating["matches_played"] == 1
            assert bob_rating["matches_played"] == 1

    asyncio.run(scenario())
