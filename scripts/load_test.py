"""Load test: real uvicorn workers + real Dockerized Redis + real WebSocket
clients, used to produce the concurrency/latency/throughput numbers in
LOAD_TEST_RESULTS.md. Not a pytest test — a standalone CLI, since a full run
takes tens of seconds to minutes and isn't meant to run on every `pytest -q`.

Rooms cap at 2 peers (this app's whole product is a 2-peer pairing), so
"N clients" here means N/2 independent rooms, each a pair exchanging chat
messages, spread round-robin across however many worker ports are given.
That's the realistic load shape for this app: many small rooms, not one
giant one.

Usage:
    python scripts/load_test.py --workers 1 --clients 2000 --messages 20
    python scripts/load_test.py --workers 4 --clients 2000 --messages 20

Starts its own Redis container and its own N uvicorn worker processes,
connects directly to each worker's port (no load balancer — the load
balancer's job is proven separately in tests/test_load_balancer_integration.py;
this script isolates how the app+Redis itself scales with worker count).
"""

import argparse
import asyncio
import json
import shutil
import socket as socketlib
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import websockets

REPO_ROOT = Path(__file__).resolve().parent.parent


def free_port() -> int:
    sock = socketlib.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_for(predicate, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {description}")


def http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


def redis_reachable(url: str) -> bool:
    import redis as sync_redis
    try:
        sync_redis.Redis.from_url(url, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


class Cluster:
    def __init__(self, num_workers: int):
        self.num_workers = num_workers
        self.redis_name = None
        self.redis_url = None
        self.procs = []
        self.ports = []

    def start(self):
        if not shutil.which("docker"):
            raise RuntimeError("Docker required for this load test")
        port = free_port()
        self.redis_name = f"pair-loadtest-redis-{port}"
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", self.redis_name,
             "-p", f"{port}:6379", "redis:7-alpine"],
            check=True, capture_output=True, timeout=30,
        )
        self.redis_url = f"redis://127.0.0.1:{port}/0"
        wait_for(lambda: redis_reachable(self.redis_url), 15, "redis to accept connections")

        env = {**__import__("os").environ, "REDIS_URL": self.redis_url}
        self.ports = [free_port() for _ in range(self.num_workers)]
        for p in self.ports:
            proc = subprocess.Popen(
                ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(p)],
                cwd=REPO_ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            self.procs.append(proc)
        for p in self.ports:
            wait_for(lambda p=p: http_ok(f"http://127.0.0.1:{p}/health"), 15, f"worker on {p} healthy")

    def stop(self):
        for proc in self.procs:
            proc.terminate()
        deadline = time.monotonic() + 5
        for proc in self.procs:
            try:
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if self.redis_name:
            subprocess.run(["docker", "rm", "-f", self.redis_name], capture_output=True)

    def metrics(self):
        total = {"rooms_active": 0, "peers_connected_total": 0, "reap_sweeps_total": 0, "peers_reaped_total": 0}
        for p in self.ports:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{p}/metrics", timeout=2) as r:
                    data = json.loads(r.read())
                for k in total:
                    total[k] += data.get(k, 0)
            except Exception:
                pass
        return total


async def run_pair(room_code: str, port_a: int, port_b: int, messages: int,
                    connect_latencies: list, msg_latencies: list, failures: list):
    url_a = f"ws://127.0.0.1:{port_a}/ws"
    url_b = f"ws://127.0.0.1:{port_b}/ws"
    t0 = time.monotonic()
    try:
        async with websockets.connect(url_a) as ws_a, websockets.connect(url_b) as ws_b:
            await ws_a.send(json.dumps({"type": "join", "payload": {"room": room_code, "handle": "A"}}))
            await ws_b.send(json.dumps({"type": "join", "payload": {"room": room_code, "handle": "B"}}))

            async def drain_until(ws, event_type, predicate, timeout=10):
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"waiting for {event_type}")
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    event = json.loads(raw)
                    if event.get("type") == event_type and predicate(event.get("payload", {})):
                        return event["payload"]

            await drain_until(ws_a, "room_state", lambda p: p["status"] == "paired")
            await drain_until(ws_b, "room_state", lambda p: p["status"] == "paired")
            connect_latencies.append(time.monotonic() - t0)

            for i in range(messages):
                send_ts = time.monotonic()
                text = f"msg-{i}-{send_ts}"
                await ws_a.send(json.dumps({"type": "chat", "payload": {"text": text}}))
                await drain_until(ws_b, "chat", lambda p, text=text: p["text"] == text, timeout=10)
                msg_latencies.append(time.monotonic() - send_ts)
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")


async def main_async(num_workers: int, num_clients: int, messages: int, concurrency_ramp: int):
    cluster = Cluster(num_workers)
    print(f"Starting {num_workers} worker(s) + Redis...")
    cluster.start()
    try:
        num_rooms = num_clients // 2
        connect_latencies, msg_latencies, failures = [], [], []
        tasks = []
        sem = asyncio.Semaphore(concurrency_ramp)

        async def bounded_run(i):
            async with sem:
                port_a = cluster.ports[i % len(cluster.ports)]
                port_b = cluster.ports[(i + 1) % len(cluster.ports)]
                await run_pair(f"R{i:05d}", port_a, port_b, messages, connect_latencies, msg_latencies, failures)

        start = time.monotonic()
        for i in range(num_rooms):
            tasks.append(asyncio.create_task(bounded_run(i)))
        await asyncio.gather(*tasks)
        elapsed = time.monotonic() - start

        total_messages = len(msg_latencies)
        result = {
            "workers": num_workers,
            "clients": num_clients,
            "rooms": num_rooms,
            "messages_per_pair": messages,
            "elapsed_seconds": round(elapsed, 2),
            "successful_pairs": len(connect_latencies),
            "failed_pairs": len(failures),
            "total_messages_delivered": total_messages,
            "throughput_msgs_per_sec": round(total_messages / elapsed, 1) if elapsed > 0 else 0,
            "connect_latency_ms": {
                "p50": round(statistics.median(connect_latencies) * 1000, 1) if connect_latencies else None,
                "p95": round(sorted(connect_latencies)[int(len(connect_latencies) * 0.95) - 1] * 1000, 1) if connect_latencies else None,
                "max": round(max(connect_latencies) * 1000, 1) if connect_latencies else None,
            },
            "message_latency_ms": {
                "p50": round(statistics.median(msg_latencies) * 1000, 2) if msg_latencies else None,
                "p95": round(sorted(msg_latencies)[int(len(msg_latencies) * 0.95) - 1] * 1000, 2) if msg_latencies else None,
                "p99": round(sorted(msg_latencies)[int(len(msg_latencies) * 0.99) - 1] * 1000, 2) if msg_latencies else None,
                "max": round(max(msg_latencies) * 1000, 2) if msg_latencies else None,
            },
            "sample_failures": failures[:5],
            "server_metrics": cluster.metrics(),
        }
        print(json.dumps(result, indent=2))
        return result
    finally:
        cluster.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--clients", type=int, default=200)
    parser.add_argument("--messages", type=int, default=10, help="chat messages per pair")
    parser.add_argument("--concurrency-ramp", type=int, default=200, help="rooms connecting at once")
    parser.add_argument("--out", type=str, default=None, help="write JSON result to this file")
    args = parser.parse_args()

    result = asyncio.run(main_async(args.workers, args.clients, args.messages, args.concurrency_ramp))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
