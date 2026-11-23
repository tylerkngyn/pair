# Pair — two-peer room chat

A text-only FastAPI + native WebSocket MVP with one static HTML/CSS/JS frontend.
No frontend build, authentication, database, or message history.

## Run

Requires Python 3.11+.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Choose any six-character alphanumeric room code,
enter a name (or leave it blank), and join. In a second tab, enter the same code
and a different name. A new room is created automatically when needed.
Messages work while waiting, but are delivered only to peers currently present.
A third peer receives `ROOM_FULL` / `Room is full`, then close code `1008`.
Leave or refresh either tab; the survivor sees the departure and can pair again.

### Two devices on the same local network

```sh
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Find the server computer's LAN IP in network settings (e.g. `192.168.1.20`).
Open `http://192.168.1.20:8000` on both devices and enter the same code. Allow
port 8000 through the computer's firewall if prompted. Both devices must be on
the same network; guest Wi-Fi/client isolation may prevent connections. Browse
to the actual LAN IP, not `0.0.0.0`. HTTPS automatically uses `wss://`; an HTTPS
deployment needs a reverse proxy that forwards WebSocket upgrade requests.

### Devices that can't reach each other on LAN

Some networks (guest Wi-Fi, many offices/cafes) isolate client devices from
each other even on the same SSID, so the LAN IP approach above won't connect.
Two ways around it:

**Quick/temporary — tunnel the local server:**

```sh
brew install ngrok
ngrok config add-authtoken <your-authtoken>   # from the ngrok dashboard
uvicorn app.main:app --host 0.0.0.0 --port 8000 &
ngrok http 8000
```

Share the `https://*.ngrok-free.dev` URL `ngrok` prints; both peers open that
same URL and enter the same room code. On the free plan a tunnel session
lasts about two hours and gets a new random URL each run. Never commit or
share an authtoken in plain text.

**Permanent — deploy to Render:**

This repo includes `render.yaml`. In the
[Render dashboard](https://dashboard.render.com), create a new Blueprint,
point it at this repository, and Render provisions a free web service from
that file, builds with `pip install -r requirements.txt`, and runs
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`. You get a permanent
`https://pair-room-chat.onrender.com`-style URL with TLS and `wss://`
handled for you — no expiring link, no local machine needing to stay on.

`numInstances: 1` in `render.yaml` is required, not cosmetic: room state
lives in process memory (see Extension boundary below), so a second
instance would never see rooms created on the first. Render's free plan
spins the service down after inactivity and takes a few seconds to wake on
the next request.

A shared URL (ngrok or Render) is reachable by anyone who has the link,
same as any room code; don't post it publicly.

## Layout

```text
app/
  main.py          HTTP routes and WebSocket validation/dispatch
  models.py        Strict Pydantic input/output envelopes
  room_manager.py  Room capacity, membership, ordered delivery, cleanup
static/
  index.html       Complete responsive UI with inline CSS/JS
  dashboard.html   Live /metrics + /health polling dashboard (GET /dashboard)
scripts/
  load_test.py     Concurrency/latency/throughput load test, see LOAD_TEST_RESULTS.md
  kill_test.py     Worker-crash recovery-time test, see LOAD_TEST_RESULTS.md
tests/
  test_chat.py     Protocol and lifecycle integration tests
requirements.txt
requirements-dev.txt
```

`GET /` serves the UI; `GET /health` returns `{"status":"ok"}`; `GET /dashboard`
serves a small live-metrics page; `WS /ws` handles the protocol. Routes do
not manipulate room storage. A per-room lock
serializes joins, chats, and departures; per-socket locks serialize delivery.
Failed/slow sends are bounded, and failed peers are removed with departure
notifications. Empty rooms are deleted. The browser retains at most 300 log
entries and clears them when leaving/rejoining; the backend stores no messages.

## Protocol

Every event is `{"type":"...","payload":{...}}`. Unknown event types, extra
fields, incorrect types, binary frames, and malformed JSON receive `INVALID_MSG`
without crashing the handler. Invalid chat payloads leave the connection usable.
The first meaningful event must be `join`. A socket joins only one room.

Client events:

```json
{"type":"join","payload":{"room":"ABC123","handle":"Alice"}}
{"type":"chat","payload":{"text":"hello"}}
{"type":"typing","payload":{"typing":true}}
{"type":"reaction","payload":{"emoji":"👍"}}
{"type":"ping","payload":{}}
```

Room codes are six ASCII alphanumeric characters, normalized to uppercase.
Handles are optional, at most 32 characters, and default to `Player-1234`-style
random names. Duplicate handles in one room are rejected with `INVALID_MSG` so
sent and received messages remain unambiguous. Choose a different name to retry.
Chat text must contain 1–500 Unicode code points and cannot be all whitespace.
Text is preserved and rendered with `textContent`, never HTML.

`typing` broadcasts the sender's in-progress state to the room; the client
sends `true` while composing and `false` on send, idle, or clearing the
field. `reaction` broadcasts a one-off emoji to the room and must be one of
`👍 ❤️ 😂 😮 👏`; other values are rejected with `INVALID_MSG`. Both require
having joined a room first and never trigger a `room_state` refresh.

Server events:

```json
{"type":"room_state","payload":{"room":"ABC123","status":"waiting","peers":["Alice"]}}
{"type":"room_state","payload":{"room":"ABC123","status":"paired","peers":["Alice","Bob"]}}
{"type":"chat","payload":{"sender":"Alice","text":"hello","timestamp":1700000000}}
{"type":"system","payload":{"text":"Bob joined the room"}}
{"type":"system","payload":{"text":"Bob left the room"}}
{"type":"typing","payload":{"sender":"Alice","typing":true}}
{"type":"reaction","payload":{"sender":"Alice","emoji":"👍"}}
{"type":"error","payload":{"code":"ROOM_FULL","message":"Room is full"}}
{"type":"error","payload":{"code":"INVALID_MSG","message":"..."}}
```

Chats echo to the sender as confirmation and label their handle with `(you)`.
The light/dark toggle remembers your choice in the browser when storage is available.
Timestamps are server-generated Unix seconds. On departure, the wire status
returns to `waiting`; the UI updates the capacity and logs the departure.
Browser heartbeats
send `ping` every 20 seconds, with no protocol `pong`. The server closes sockets
that send nothing for 60 seconds (15 seconds before joining). Physical network
loss detection depends on WebSocket transport timeouts; clean closes notify
survivors as soon as the server receives the disconnect.

## Tests

```sh
pip install -r requirements-dev.txt
python -m pytest -q
```

## Extension boundary

By default, run **one Uvicorn worker**: all presence is in process memory and
disappears on restart. In-memory WebSocket presence is process-local, as
described in the
[FastAPI WebSocket documentation](https://fastapi.tiangolo.com/advanced/websockets/).
The protocol uses [Pydantic discriminated unions](https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions)
so new lobby/game events can be added explicitly.

To grow this MVP, add event schemas and dispatch handlers; keep membership and
delivery behind `RoomManager`. Room codes are sharing conveniences, not
authentication or access control.

### Running multiple workers (Redis-backed)

Set `REDIS_URL` (e.g. `redis://localhost:6379/0`) and the app switches to
`app/redis_room_manager.RedisRoomManager` instead of the in-process
`RoomManager`. This makes it safe to run more than one Uvicorn
worker/instance pointed at the same Redis:

- **Membership** (who is in which room) lives in Redis, checked and updated
  atomically by a Lua script, so capacity and duplicate-handle checks are
  correct even when two workers race to join the same room.
- **Delivery** happens over Redis Pub/Sub: a worker never has another
  worker's actual `WebSocket` object, so instead it publishes every event to
  a per-room channel, and each worker's own listener task forwards incoming
  events only to the local sockets it holds.
- A peer that disconnects is removed from Redis and the departure is
  published so peers on *other* workers see it too.
- **Crash recovery**: each peer's connection carries a Redis presence key
  (`presence:{room}:{peer_id}`, 90s TTL) refreshed on every browser `ping`.
  The TTL is set well above the 20s ping interval because backgrounded
  browser tabs commonly throttle `setInterval` to roughly once a minute —
  a routine case, not a crash, and the margin must absorb it or a merely
  backgrounded peer gets reaped out from under a still-open socket. Each
  worker also runs a periodic sweep (every ~20s) across the whole room
  keyspace and removes any peer whose presence key lapsed — not just peers
  it holds locally, since a *crashed* worker's own bookkeeping is gone and
  only another worker's sweep can notice. Whichever worker's sweep or
  disconnect actually removes a peer from Redis also publishes an internal
  eviction signal on the room's channel, so if that peer is really live
  and held locally by a *different* worker (not the crashed/sweeping one),
  that worker drops it and closes its socket too — otherwise it would keep
  chatting as a zombie even after Redis and every other peer have
  forgotten it. Worst-case detection is roughly the TTL plus the sweep
  interval (around two minutes), during which a room with a crashed peer
  can still look full to a new joiner. Every `ping`-triggered heartbeat
  also refreshes the room hash's own safety-net TTL, not just the peer's
  presence key, so a long-lived active room never expires out from under
  two still-connected peers.

This is still MVP-grade: no message history, no auth beyond the room code.
`tests/test_redis_room_manager.py` exercises all of the above — including
simulated crashes — by running two `RedisRoomManager` instances against one
shared [fakeredis](https://github.com/cunla/fakeredis-py) server to prove
cross-worker join/chat/disconnect/reap behavior without real multi-process
infra.

### Real multi-process integration tests

`tests/test_redis_room_manager.py` proves the algorithm is correct, but
runs entirely in one process against a fake Redis and fake sockets. It
can't catch a bug that only shows up across real separate OS processes, a
real Redis connection over the network, or real WebSocket/TCP round trips.
`tests/test_multiprocess_integration.py` closes that gap: it launches two
actual `uvicorn app.main:app` processes on different ports, points both at
one real Redis (a disposable `redis:7-alpine` Docker container, or an
externally provided `PAIR_TEST_REDIS_URL`), and drives them with real
WebSocket clients to prove cross-process chat delivery and that a crashed
peer (its TCP connection aborted with no close handshake) actually gets
reaped by the *other* process within a few seconds.

This suite is opt-in — excluded from the default `pytest -q` run via
`pytest.ini`'s `addopts = -m "not integration"`, since it needs Docker (or
a real reachable Redis) and takes noticeably longer than the rest of the
suite. Run it explicitly with:

```sh
python -m pytest -m integration -v
```

Without Docker running and without `PAIR_TEST_REDIS_URL` set, every test in
this file skips cleanly rather than erroring. Two env vars exist purely to
make the crash/reap scenario testable in seconds instead of the real ~2
minute worst case — `PAIR_PRESENCE_TTL_SECONDS` and
`PAIR_REAP_INTERVAL_SECONDS` override the constants of the same name in
`app/redis_room_manager.py`; production behavior is unchanged unless they
are explicitly set.

`tests/test_load_balancer_integration.py` closes a related but separate
gap: the Redis Pub/Sub design was specifically chosen so WebSocket
connections never need session affinity ("sticky sessions") — any worker
can deliver any room's events once it has a local peer in that room. That
claim had never been tested against an actual reverse proxy, only two
manually-dialed local ports. This test puts two real `uvicorn` processes
behind a real `nginx:alpine` container doing plain round-robin (no
`ip_hash`), connects two real WebSocket clients through the one proxy URL,
and proves they land on different backends yet still chat correctly. Same
opt-in/skip rules as above. It does **not** test Render's actual production
load balancer — that's a managed black box you can't instantiate locally —
see "Scaling beyond one instance in production" below for that distinction.

### Observability

`GET /metrics` returns a small JSON object of per-process counters:
`rooms_active`, `peers_connected_total`, `reap_sweeps_total`, and
`peers_reaped_total`. These are deliberately plain counters in a
`dataclass` (`app/metrics.py`), not Prometheus-format output — nothing in
this deployment scrapes Prometheus, and a 2-peer ephemeral chat MVP doesn't
warrant a metrics backend, alerting, or an APM. Each worker reports only
what it has personally seen; there's no cross-worker aggregation, matching
the rest of this app's design where only Redis room membership is actually
shared state. If cluster-wide totals were ever needed, the natural
follow-up is a small Redis-backed `INCR` per counter — not built now, since
nothing currently needs it.

`GET /dashboard` renders those same two endpoints (`/metrics` + `/health`)
as a small auto-refreshing page instead of raw JSON — counters, a live event
log for joins/reaps/sweeps, and a connectivity indicator. Same per-process
scope as `/metrics` itself: it shows whichever worker answers that request,
not a cluster total.

### Load testing and crash recovery, with real numbers

`scripts/load_test.py` and `scripts/kill_test.py` spin up real `uvicorn`
worker processes against a real Dockerized Redis and drive them with real
WebSocket clients (same pattern as the integration tests above, but as
standalone CLIs rather than pytest tests, since a full run takes
seconds-to-minutes). Full results, methodology, and reproduction commands
are in [`LOAD_TEST_RESULTS.md`](./LOAD_TEST_RESULTS.md). Headline findings
from a run on 2026-11-23:

- **1,000 concurrent clients (500 rooms) breaks a single worker** — most
  pairs time out and the process stops answering `/health`; the *same*
  1,000-client load runs clean on 2 workers, and 2,000 clients (which breaks
  2 workers) runs clean on 4. That's the measured fact behind "horizontally
  scalable," not just the Redis Pub/Sub design.
- **Worker crash recovery takes ~100 seconds** with production defaults
  (`SIGKILL` on one worker mid-session; the surviving peer's room returns to
  `waiting` and is immediately rejoinable), matching the documented
  `PRESENCE_TTL_SECONDS` + `REAP_INTERVAL_SECONDS` worst case almost exactly.

### Known gaps that are not closeable in this repo

Two things were considered for distributed-system hardening and
deliberately left undone, because building fake code to "solve" them would
be actively misleading:

- **Redis is a single point of failure.** `RedisRoomManager` takes one
  `redis_url` — there is no Sentinel/cluster-aware client wiring, and
  `fakeredis` cannot simulate real primary/replica failover, so a test that
  claimed to prove HA would prove nothing. Real resilience here needs a
  Sentinel quorum (or a managed Redis product with failover) and
  Sentinel-aware client code — both outside what this codebase or its
  current (free-tier) hosting can provide. What *is* in place: the Redis
  Pub/Sub listener and reap loop now log a warning and retry/reconnect
  automatically on a dropped Redis connection, rather than silently
  stalling delivery forever (see `RECONNECT_BACKOFF_SECONDS` in
  `app/redis_room_manager.py`) — that's an honest improvement to this
  single Redis's failure visibility, not a fix for it being a SPOF.
- **No horizontal autoscaling.** `render.yaml` runs `plan: free`,
  `numInstances: 1`. Render's free tier does not support `numInstances`
  greater than 1, CPU/memory-based autoscaling, or rolling deploys across
  multiple instances at any configuration — that functionality exists only
  on Render's paid "Standard" tier and above. There is nothing to build in
  this repo to close this gap; it is a hosting-plan upgrade, not a code
  change. The app's code already supports running N instances correctly
  (that's what the Redis work above is for), and `GET /health` is already
  a ready-to-use health-check endpoint the moment a higher tier is in use.

#### Scaling beyond one instance in production

Once (and only if) this project moves to a Render plan that supports it,
scaling out is a `render.yaml` config change, not a code change:

```yaml
services:
  - type: web
    name: pair-room-chat
    plan: standard        # numInstances > 1 requires a paid plan
    numInstances: 2        # or a {min, max} range, per Render's current schema
    envVars:
      - key: REDIS_URL
        value: <your Redis instance URL>
```

Render fronts all instances with its own managed load balancer
automatically — there is no user-configurable LB to install or test
against on Render itself; `tests/test_load_balancer_integration.py` above
is a stand-in proving the underlying no-sticky-sessions architecture is
sound, not a claim that it was tested against Render's specific LB.
