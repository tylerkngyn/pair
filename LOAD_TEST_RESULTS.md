# Load test & kill test results

Real numbers from `scripts/load_test.py` and `scripts/kill_test.py` — real
`uvicorn` worker processes, a real Dockerized `redis:7-alpine`, real
WebSocket clients, run on 2026-11-23. Not a production benchmark on
production hardware — treat absolute numbers as directional, the *shape*
(1 worker caps out, 2 and 4 workers don't, at the same offered load) as the
fact this exists to prove.

## Test environment

- MacBook Pro, Apple M3 Pro (11 cores: 5 performance + 6 efficiency), 18 GB RAM
- macOS 15.7.9, Python 3.13.11, Docker 29.7.2 (Docker Desktop)
- **The load generator and all app workers ran on this same machine**, sharing
  its CPU/RAM/network stack with each other and with Docker Desktop's own VM.
  That's a real confound: under load, the Python asyncio client driving
  hundreds of WebSocket connections competes for the same cores as the
  uvicorn worker(s) it's measuring, and all inter-process traffic (including
  "network" hops to Redis) is loopback, not a real network. Absolute latency
  and throughput numbers are this-machine-under-this-contention numbers, not
  a production ceiling — the load-bearing fact is the *relative* one (adding
  workers measurably moves the breaking point), not the absolute throughput.
  A real capacity benchmark would run the client and each worker on separate
  machines.

Raw JSON for every run is in `scripts/results/`.

Rooms cap at 2 peers (this app's whole product), so "N clients" below means
N/2 independent rooms, each pair exchanging 10 chat messages, connected
directly to worker ports round-robin (no load balancer in front — that
claim is proven separately in `tests/test_load_balancer_integration.py`;
this isolates how the app + Redis itself scales with worker count).

## Concurrency, latency, and throughput vs. worker count

| Workers | Clients (rooms) | Result | Connect p95 | Msg p95 | Msg p99 | Throughput |
|---|---|---|---|---|---|---|
| 1 | 200 (100) | ✅ clean | 119 ms | 19 ms | 21 ms | 4,035 msg/s |
| 1 | 600 (300) | ✅ clean | 182 ms | 38 ms | 56 ms | 4,031 msg/s |
| 1 | **1000 (500)** | ❌ **degrades** | 318 ms (for the 300 that fully paired) | — | — | 32 msg/s; 377 pairs timed out (paired then stalled, or never paired); `/metrics` and `/health` stopped responding |
| 2 | 1000 (500) | ✅ clean | 230 ms | 61 ms | 127 ms | 5,134 msg/s |
| 2 | **2000 (1000)** | ❌ **degrades** | 251 ms (for the 645 that fully paired) | — | — | 74 msg/s; 434 pairs timed out (paired then stalled, or never paired) |
| 4 | 1000 (500) | ✅ clean | 248 ms | 35 ms | 85 ms | 6,437 msg/s |
| 4 | 2000 (1000) | ✅ clean | 189 ms | 37 ms | 56 ms | 6,632 msg/s |

**The headline fact:** 1,000 concurrent clients (500 rooms) breaks a single
worker — most new pairs time out, and the worker stops answering `/health`
and `/metrics` entirely, because everything (WebSocket I/O, Pydantic
validation, per-room locking) runs on one process's single asyncio event
loop. The *identical* 1,000-client load against 2 workers runs clean with
room to spare, and against 4 workers is clean and visibly faster. The same
pattern repeats one level up: 2,000 clients breaks 2 workers the same way
1,000 broke 1 worker, but 4 workers handle 2,000 clients cleanly. Splitting
connections across Redis-backed workers is what makes this application
"horizontally scalable" — not a design claim, a measured breaking point that
moves with worker count.

Message delivery latency itself (p95: 19–61 ms, p99: 21–127 ms depending on
load) stays low throughout every *successful* run; the failure mode under overload is
connection/message timeouts and an unresponsive process, not creeping
latency — consistent with one saturated asyncio loop rather than, e.g., a
slow dependency.

Reproduce:
```sh
python scripts/load_test.py --workers 1 --clients 1000 --messages 10 --concurrency-ramp 300
python scripts/load_test.py --workers 2 --clients 1000 --messages 10 --concurrency-ramp 300
python scripts/load_test.py --workers 4 --clients 2000 --messages 10 --concurrency-ramp 300
```

## Kill test: worker crash recovery

`scripts/kill_test.py` pairs Alice (worker A) and Bob (worker B) behind one
shared Redis, confirms chat works, then `SIGKILL`s worker A's process
outright — no close handshake, no chance for its own cleanup code to run,
simulating a real crash rather than a graceful shutdown. It uses the
*production* defaults (`PRESENCE_TTL_SECONDS=90`, `REAP_INTERVAL_SECONDS=20`),
not the shortened constants the pytest integration suite uses to run fast.

```
Paired. Chat works:
  Bob received: 'hello before kill' from Alice

SIGKILL-ing worker A mid-session...
Waiting for Bob (on surviving worker B) to see Alice leave...
  system: 'Alice left the room'          ->  100.0s after kill -9
  room_state back to waiting, peers=['Bob'] ->  100.0s after kill -9

Room still usable: Carol joined and re-paired with Bob successfully.

RESULT: detect=100.0s  full_recovery=100.0s
```

**Result: 100 seconds to full recovery.** Worker B's own reap sweep — not
worker A, which is dead — notices Alice's expired presence key, removes her
from Redis, publishes the departure so Bob's room goes back to `waiting`,
and the room is immediately rejoinable by a new peer. No manual
intervention, no stuck room, no zombie peer. (Why exactly 100s, not the
README's theoretical 110s ceiling, is explained below.)

### Why 100 seconds, and the tradeoff against going faster

100s isn't an accident of the test, it's close to this app's own stated
worst case (`PRESENCE_TTL_SECONDS` + `REAP_INTERVAL_SECONDS` = 90 + 20 =
110s) — and the typical case actually lands a bit under that ceiling.
Alice's presence key starts its 90s countdown the moment she joins, and the
sweep that notices it has expired lands somewhere in its next 20s cycle, not
necessarily at the full 20s mark — so the expected detection time is closer
to `TTL + reap_interval/2` (≈100s) than the strict `TTL + reap_interval`
upper bound (110s). That matches what was measured.

**That 90s/20s choice is a direct, tunable tradeoff, not an oversight.**
A shorter TTL detects a real crash faster — but the TTL isn't free-floating,
it's refreshed by the same browser `ping` heartbeat that keeps a tab alive,
and browsers throttle backgrounded tabs' `setInterval` to roughly once a
minute (see the README). A TTL shorter than that throttled interval would
start evicting peers who are still connected and still coming back, just on
a backgrounded tab — a false eviction, not a crash. 90s was chosen
specifically to comfortably outlive that throttling; it's slow by design,
not by neglect.

To make that tradeoff concrete, `scripts/kill_test.py` now accepts
`--ttl`/`--reap-interval` overrides (wired to the same
`PAIR_PRESENCE_TTL_SECONDS`/`PAIR_REAP_INTERVAL_SECONDS` env vars the app
already reads) so the faster-but-riskier end of that tradeoff is a measured
number too, not just a claim:

| TTL | Reap interval | Measured recovery | Worst-case ceiling (TTL + interval) |
|---|---|---|---|
| 90s (production default) | 20s | **100.0s** | 110s |
| 20s | 5s | **24.8s** | 25s |

Dropping to a 20s TTL / 5s sweep cuts recovery from 100s to ~25s — a real,
4x improvement in crash-detection speed — but at a cost this test doesn't
measure directly: a 20s TTL is *shorter* than the ~60s throttle interval
browsers commonly apply to backgrounded tabs, so in production that setting
would start reaping peers who are merely backgrounded, not crashed, turning
"fast recovery" into "spurious disconnects." The 90s/20s production default
exists specifically to stay above that throttling floor; the 20s/5s number
above is what you'd get if you *didn't* need to protect against backgrounded
tabs — e.g. a server-to-server presence check with its own dedicated
heartbeat — not a drop-in replacement for this app's current default.

Reproduce:
```sh
python scripts/kill_test.py                           # production: TTL=90s, reap=20s -> ~100s
python scripts/kill_test.py --ttl 20 --reap-interval 5 # faster, riskier -> ~25s
```
Each run takes roughly its own worst-case ceiling in wall-clock time — it
waits out the real timers, not an accelerated test double.

## Dashboard

`GET /dashboard` (added in `app/main.py`, `static/dashboard.html`) is a
plain polling page for `/metrics` + `/health` on whatever worker answers
that origin — counters, a live event log ("peer reaped", "reap sweep ran"),
and a connectivity indicator. It's deliberately not a metrics backend (see
the README's Observability section for why): same per-process, no-Prometheus
design as `/metrics` itself, just rendered instead of raw JSON.
