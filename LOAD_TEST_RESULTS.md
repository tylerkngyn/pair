# Load test & kill test results

Real numbers from `scripts/load_test.py` and `scripts/kill_test.py` — real
`uvicorn` worker processes, a real Dockerized `redis:7-alpine`, real
WebSocket clients, driven from one laptop (Apple Silicon Mac, local Docker
Desktop) on 2026-11-23. Not a production benchmark on production hardware —
treat absolute numbers as directional, the *shape* (1 worker caps out, 2 and
4 workers don't, at the same offered load) as the fact this exists to prove.

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

**Result: 100 seconds to full recovery**, close to the README's documented
worst case (`PRESENCE_TTL_SECONDS` + `REAP_INTERVAL_SECONDS` = 110s — Alice's
presence key had already been live a few seconds before the kill, so the
margin is in the right ballpark, not an exact match). Worker B's own reap sweep — not worker A, which is dead — notices
Alice's expired presence key, removes her from Redis, publishes the
departure so Bob's room goes back to `waiting`, and the room is immediately
rejoinable by a new peer. No manual intervention, no stuck room, no zombie
peer.

That number is a direct, tunable trade-off: lowering
`PRESENCE_TTL_SECONDS`/`REAP_INTERVAL_SECONDS` detects crashes faster at the
cost of false-positives against a merely backgrounded/throttled browser tab
(see the README's note on `setInterval` throttling) — the current defaults
were chosen to absorb that, not for speed.

Reproduce: `python scripts/kill_test.py` (takes ~110s — it waits out the
real timers, not an accelerated test double).

## Dashboard

`GET /dashboard` (added in `app/main.py`, `static/dashboard.html`) is a
plain polling page for `/metrics` + `/health` on whatever worker answers
that origin — counters, a live event log ("peer reaped", "reap sweep ran"),
and a connectivity indicator. It's deliberately not a metrics backend (see
the README's Observability section for why): same per-process, no-Prometheus
design as `/metrics` itself, just rendered instead of raw JSON.
