"""In-process WebSocket fan-out latency benchmark: no real sockets, no
subprocess, no Docker — drives RoomManager directly in this process, the
same FakeSocket pattern tests/test_redis_room_manager.py already uses.
Measures the cost structured JSON logging + the hand-rolled /metrics
counters add to the hot fan-out path (app/room_manager.py's _publish),
by toggling app.observability.DISABLED between reps and comparing latency.

Usage:
    python scripts/ws_latency_bench.py --clients 100 --messages 20 --reps 5
"""

import argparse
import asyncio
import statistics
import time

import app.observability as observability
from app.room_manager import Peer, RoomManager


class FakeSocket:
    """Records the wall-clock time each event is "sent" so the caller can
    pair a chat message's send time with its delivery time on the other
    peer's socket.
    """

    def __init__(self) -> None:
        self.received_at: list[float] = []

    async def accept(self) -> None:
        pass

    async def send_json(self, event: dict) -> None:
        if event.get("type") == "chat":
            self.received_at.append(time.perf_counter())

    async def close(self, code: int = 1000) -> None:
        pass


def percentiles(data_ms: list[float]) -> dict:
    if not data_ms:
        return {"p50": None, "p95": None, "p99": None}
    s = sorted(data_ms)
    def pct(p):
        idx = min(len(s) - 1, max(0, int(len(s) * p) - 1))
        return round(s[idx], 3)
    return {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99)}


async def run_once(num_clients: int, messages: int) -> list[float]:
    """One rep: num_clients/2 rooms, each pair exchanging `messages` chat
    messages A->B, timing each round trip. Returns latencies in ms.
    """
    manager = RoomManager()
    latencies_ms: list[float] = []

    async def run_pair(room_code: str):
        a = Peer(socket=FakeSocket())
        b = Peer(socket=FakeSocket())
        await manager.join(a, room_code, "A")
        await manager.join(b, room_code, "B")
        for i in range(messages):
            send_ts = time.perf_counter()
            await manager.chat(a, f"msg-{i}")
            recv_ts = b.socket.received_at[-1]
            latencies_ms.append((recv_ts - send_ts) * 1000)
        await manager.disconnect(a)
        await manager.disconnect(b)

    num_rooms = num_clients // 2
    await asyncio.gather(*(run_pair(f"R{i:05d}") for i in range(num_rooms)))
    return latencies_ms


async def run_reps(num_clients: int, messages: int, reps: int) -> dict:
    per_rep = []
    for _ in range(reps):
        latencies = await run_once(num_clients, messages)
        per_rep.append(percentiles(latencies))
    return {
        "p50": {
            "values": [r["p50"] for r in per_rep],
            "median": round(statistics.median(r["p50"] for r in per_rep), 3),
        },
        "p95": {
            "values": [r["p95"] for r in per_rep],
            "median": round(statistics.median(r["p95"] for r in per_rep), 3),
        },
        "p99": {
            "values": [r["p99"] for r in per_rep],
            "median": round(statistics.median(r["p99"] for r in per_rep), 3),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=100)
    parser.add_argument("--messages", type=int, default=20, help="chat messages per pair per rep")
    parser.add_argument("--reps", type=int, default=5)
    args = parser.parse_args()

    print(f"clients={args.clients} rooms={args.clients // 2} messages_per_pair={args.messages} reps={args.reps}\n")

    observability.DISABLED = False
    enabled = asyncio.run(run_reps(args.clients, args.messages, args.reps))
    print("Instrumentation ENABLED (structured logging + /metrics):")
    for k, v in enabled.items():
        print(f"  {k}: median={v['median']}ms  per-rep={v['values']}")

    observability.DISABLED = True
    disabled = asyncio.run(run_reps(args.clients, args.messages, args.reps))
    observability.DISABLED = False
    print("\nInstrumentation DISABLED (PAIR_DISABLE_OBSERVABILITY equivalent):")
    for k, v in disabled.items():
        print(f"  {k}: median={v['median']}ms  per-rep={v['values']}")

    print("\nDelta (enabled - disabled), median of medians:")
    for k in ("p50", "p95", "p99"):
        print(f"  {k}: {round(enabled[k]['median'] - disabled[k]['median'], 3)}ms")


if __name__ == "__main__":
    main()
