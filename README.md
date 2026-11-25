[![CI](https://github.com/tylerkngyn/tylerjunze/actions/workflows/ci.yml/badge.svg)](https://github.com/tylerkngyn/tylerjunze/actions/workflows/ci.yml)

# Pair

Pair is a Python interview practice workspace: solo, timed practice sessions against a NeetCode 150-based catalog, plus a ranked 1v1 matchmaking mode with Elo ratings. Code runs entirely in the browser via a sandboxed Pyodide runtime, while FastAPI handles accounts, problem selection, session/match state, and a WebSocket layer for ranked delivery that can run single-worker in-process or Redis-backed across multiple workers.

TODO: screenshot

## Features

| Feature | Status |
| --- | --- |
| Solo timed practice (NeetCode 150 catalog, draft autosave, history) | Complete |
| In-browser Python execution (Pyodide, sandboxed iframe + worker) | Complete |
| Ranked 1v1 matchmaking with Elo ratings | HTTP API complete; ranked WebSocket (`MATCH_FOUND`/`OPPONENT_PROGRESS`/`MATCH_RESULT`) implemented, falls back to polling when delivery can't be confirmed |
| Redis multi-worker mode (room delivery + ranked queue) | Complete, falls back to in-process single-worker behavior when `REDIS_URL` is unset |
| Structured JSON event logging | Complete (6 event types) |
| `/metrics` Prometheus endpoint | Complete (7 metrics) |
| Two-player (non-ranked) interviews, AI feedback, speech, readability scoring | Not started |
| Live production deployment | Not done — no Render deploy of this state is claimed |

## Architecture

```
Browser (workspace, editor, Pyodide sandbox)
        |  HTTPS JSON + session cookie / WebSocket
        v
   FastAPI (app/main.py)
        |-- accounts, CSRF, practice sessions  --> SQLite (./var/pair.db) or PostgreSQL (DATABASE_URL)
        |-- ranked queue + match HTTP API (app/practice/ranked_routes.py)
        |-- room/user delivery (app/room_manager.py, app/redis_room_manager.py)
        |-- observability: structured logs + /metrics (app/observability.py)
        v
   Redis (optional, REDIS_URL) — ranked queue (app/practice/redis_ranked_queue.py),
   cross-worker room/user Pub/Sub channels, finalize lock
```

**Ranked match flow:** a client `POST`s `/api/ranked/queue` with its practice settings. If an eligible waiting opponent exists (or is found by widening the wait window), a match is created immediately and a `MATCH_FOUND` event is pushed over WebSocket to both sides (falling back to polling `GET /api/ranked/queue` if delivery can't be confirmed, which also opportunistically retries matching for the still-queued caller). Each side works independently, submitting through `PUT /api/ranked/matches/{id}/work/{problem_id}`, which pushes `OPPONENT_PROGRESS` to the other player. `GET /api/ranked/matches/{id}` is the only place finalization is checked: once the deadline passes, it computes the Elo update and atomically flips the match to `completed` (guarded by a DB status check and, when Redis is configured, a per-match Redis lock to prevent a double finalization race across two workers), then pushes `MATCH_RESULT` to both clients. Without `REDIS_URL`, queueing and room delivery are in-process and only visible to a single worker; with it set, `RedisRankedQueue` and `RedisRoomManager` make the same behavior work across multiple workers, with no code change required on either side.

## Tech stack

| Layer | Technology | Verified from |
| --- | --- | --- |
| Web framework | FastAPI (`>=0.115,<1`) | `requirements.txt` |
| Validation | Pydantic (`>=2.10,<3`) | `requirements.txt` |
| ASGI server | Uvicorn (`>=0.34,<1`) | `requirements.txt` |
| ORM | SQLAlchemy (`>=2.0,<3`) | `requirements.txt` |
| Database (local) | SQLite (`./var/pair.db`) | `app/practice/database.py` |
| Database (optional) | PostgreSQL via psycopg (`>=3.2,<4`) — supported but not tested against a live instance | `requirements.txt`, `.env.example`, `render.yaml` (not provisioned there) |
| Distributed transport | Redis (`>=5,<6`), optional via `REDIS_URL` | `requirements.txt`, `app/redis_room_manager.py`, `app/practice/redis_ranked_queue.py` |
| In-browser execution | Pyodide (bundled runtime assets, version 314.0.8) | `static/practice/runtime/314.0.8/`, `static/practice/runtime-loader.js` |
| Frontend | Plain HTML/CSS/JS, no framework or build step | `static/` |
| Tests | pytest (`>=8,<10`), httpx, fakeredis, websockets | `requirements-dev.txt` |

## Quick start

Requires Python 3.13.

```bash
git clone https://github.com/tylerkngyn/tylerjunze.git
cd tylerjunze
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
```

**Run with SQLite (default, no external services):**

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open <http://127.0.0.1:8000>. Data is stored in `./var/pair.db`.

**Run with Redis (multi-worker mode):**

```bash
docker run -d --rm --name pair-redis -p 6379:6379 redis:7-alpine
REDIS_URL=redis://127.0.0.1:6379 uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2
```

Both commands above were run against a clean clone during this writeup; the SQLite path was confirmed with a live `/health` check (`{"status":"ok"}`).

## Testing

Default suite (no external services required):

```bash
pytest -q
```

268 passed, 3 deselected, confirmed on a clean clone (deselected tests are the integration ones below, excluded by `pytest.ini`'s `addopts = -m "not integration"`).

Integration tests (require a Docker daemon, or `PAIR_TEST_REDIS_URL` pointing at a real Redis instance — they spin up a disposable `redis:7-alpine` container and real multi-process `uvicorn` workers):

```bash
pytest -m integration -v
```

This covers `tests/test_multiprocess_integration.py` and `tests/test_ranked_multiprocess_integration.py`. They were not run for this writeup — no Docker daemon was available in this environment, so their pass/fail status here is unverified.

## Benchmarks

From `scripts/load_test.py` (real `uvicorn` workers, real Dockerized Redis, real WebSocket clients), results checked into `scripts/results/`, 3 reps per configuration:

| Workers | Clients | Connect success rate | p50 (ms) | p95 (ms) | p99 (ms) |
| --- | --- | --- | --- | --- | --- |
| 1 | 500 | 1.0 | ~22.7 | ~51.6 | ~59.9 |
| 1 | 1000 | 0.8 (all 3 reps) | ~22.9 | ~60.2 | ~115.7 |
| 2 | 500 | 1.0 | ~13.5 | ~30.5 | ~36.9 |
| 2 | 1000 | 1.0 | ~13.8 | ~38.6 | ~58.0 |

Commands used:

```bash
python scripts/load_test.py --workers 1 --clients 500 --messages 10 --out scripts/results/w1_500_rep1.json
python scripts/load_test.py --workers 2 --clients 1000 --messages 10 --out scripts/results/w2_1000_rep1.json
python scripts/kill_test.py --reps 3
```

**Limitations:**
- All runs are on localhost, one shared machine, with synthetic (not real-browser) clients.
- Only 3 reps per configuration; results vary with machine load.
- The checked-in `scripts/results/w1_1000_*.json` files all show a 0.8 connect success rate for 1 worker / 1000 clients; an earlier claim of a 60% result on a separate run could not be found in any committed result file or in `scripts/results/matrix_run.log`, so it is left out of this table.

## Observability

**Prometheus metrics** (`/metrics`, from `app/observability.py`):

| Metric | Type | Measures |
| --- | --- | --- |
| `pair_ws_connections_active` | gauge | WebSocket connections this worker currently holds locally |
| `pair_messages_fanned_out_total` | counter | Room/user events delivered to a locally-held peer |
| `pair_fanout_latency_seconds` | histogram | Time to fan one event out to its locally-held peer(s) |
| `pair_ranked_queue_depth` | gauge | Accounts waiting in the queue — in-process backend only; reads 0 under Redis, not an empty queue |
| `pair_matches_finalized_total` | counter | Ranked matches that completed Elo finalization |
| `pair_finalize_race_prevented_total` | counter | Finalize attempts correctly turned away by the Redis lock or DB status guard |
| `pair_redis_reconnects_total` | counter | Times the Redis pub/sub listener or reap loop recovered from a dropped connection |

**Structured log events** (JSON lines via `log_event`, one `pair.events` logger call per event, from `app/observability.py`):

- `ws_connect` / `ws_disconnect` — `peer_id`, `room`
- `message_fanout` — `room`, `event_type`, `recipients` (once per fan-out call, not per recipient)
- `ranked_queue_join` — `user_id`, `backend`, `status`, `match_id` when matched, `via="poll"` when found by a widen-on-poll
- `ranked_finalize` — `match_id`, plus `lock=`/`db_guard=`/`result=`

## Known limitations and what's next

- No live production deployment of this code's state has been made; `render.yaml` provisions only the web service, not Redis or Postgres.
- `pair_ranked_queue_depth` does not reflect queue depth under the Redis-backed queue (documented limitation, not a bug).
- The ranked WebSocket path falls back to HTTP polling whenever delivery can't be confirmed (unidentified socket, no socket open, or any push under `RedisRoomManager`, since it can't confirm delivery).
- Integration tests requiring Docker/Redis were not run as part of this writeup and their current pass/fail status is unverified here.
- Two-player non-ranked interviews, AI feedback, speech input, and readability scoring are not built.

## Credits

Based on `git shortlog -sn` and `git log --follow` on the files each person's commits touch, confirmed against `docs/architecture.md`'s own ownership table:

- **Junze (Junny05)** — Redis delivery layer (`app/redis_room_manager.py`, `app/practice/redis_ranked_queue.py`), ranked backend (`app/practice/ranked_routes.py`), observability (`app/observability.py`), load-test tooling (`scripts/`), core room manager and HTTP composition (`app/room_manager.py`, `app/main.py`).
- **Tyler** — practice product and content (`app/practice/routes.py`, `app/practice/catalog.py`, `app/practice/exercises.py`, `app/practice/auth.py`), Pyodide sandbox (`static/practice/runner.html`, `harness.py`, `runtime-loader.js`), frontend (`static/index.html`, `static/practice/app.js`, `styles.css`).

---

**Could not be verified and left out of this README:** a demo/screenshot (no image assets exist in the repo), exact integration-test pass/fail status (no Docker daemon available here), and a previously mentioned 60% connect-success-rate data point for the 1-worker/1000-client load test (only 0.8 appears in the checked-in results and log).
