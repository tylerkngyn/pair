"""Load test: real uvicorn workers + real Dockerized Redis + real WebSocket
clients, re-run against the current codebase (this is the same idea as the
version deleted in commit dd9882e's pivot merge, rewritten against the
current /ws protocol and the current Prometheus-format /metrics endpoint).
Not a pytest test - a standalone CLI, since a full run takes tens of
seconds to minutes and isn't meant to run on every `pytest -q`.

Rooms cap at 2 peers (this app's whole chat/pairing product), so
"N clients" here means N/2 independent rooms, each a pair exchanging chat
messages, spread round-robin across however many worker ports are given.

Checks host load (uptime, vm.swapusage) before starting and refuses to run
if the host looks busy or low on free swap - this is a shared machine, not
a dedicated benchmark box.

Usage:
    python scripts/load_test.py --workers 1 --clients 2000 --messages 10
    python scripts/load_test.py --workers 2 --clients 2000 --messages 10
"""

import argparse
import asyncio
import json
import os
import re
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


def host_load_check(max_load_per_core: float = 1.5, min_free_swap_mb: int = 300) -> tuple[bool, str]:
    """Best-effort, macOS-specific (uptime + sysctl vm.swapusage). Returns
    (ok_to_proceed, human-readable reason). Not a hard guarantee on other
    platforms - if these commands aren't available, this allows the run
    rather than blocking it, since there's nothing reliable to check.
    """
    try:
        uptime_out = subprocess.run(["uptime"], capture_output=True, text=True, timeout=5).stdout
        load1 = float(re.search(r"load averages?:\s*([\d.]+)", uptime_out).group(1))
        ncpu = int(subprocess.run(["sysctl", "-n", "hw.ncpu"], capture_output=True, text=True, timeout=5).stdout.strip())
        per_core = load1 / ncpu
        swap_out = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True, text=True, timeout=5).stdout
        free_mb = float(re.search(r"free\s*=\s*([\d.]+)M", swap_out).group(1))
        reason = f"load1={load1} ncpu={ncpu} load_per_core={per_core:.2f} free_swap_mb={free_mb:.0f}"
        if per_core > max_load_per_core:
            return False, f"load too high to proceed fairly: {reason}"
        if free_mb < min_free_swap_mb:
            return False, f"too little free swap to proceed safely: {reason}"
        return True, reason
    except Exception as exc:
        return True, f"host load check unavailable ({exc}); proceeding without it"


def parse_prometheus_counter_or_gauge(text: str, name: str) -> float:
    match = re.search(rf"^{re.escape(name)} ([\d.eE+-]+)$", text, re.MULTILINE)
    return float(match.group(1)) if match else 0.0


class Cluster:
    def __init__(self, num_workers: int):
        self.num_workers = num_workers
        self.redis_name = None
        self.redis_url = None
        self.procs: list[subprocess.Popen] = []
        self.ports: list[int] = []

    def start(self) -> None:
        if not shutil.which("docker"):
            raise RuntimeError("Docker required for this load test")
        port = free_port()
        self.redis_name = f"pair-loadtest-redis-{port}"
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", self.redis_name, "-p", f"{port}:6379", "redis:7-alpine"],
            check=True, capture_output=True, timeout=30,
        )
        self.redis_url = f"redis://127.0.0.1:{port}/0"
        wait_for(lambda: redis_reachable(self.redis_url), 15, "redis to accept connections")

        env = {**os.environ, "REDIS_URL": self.redis_url}
        self.ports = [free_port() for _ in range(self.num_workers)]
        for p in self.ports:
            proc = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(p)],
                cwd=REPO_ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            self.procs.append(proc)
        for p in self.ports:
            wait_for(lambda p=p: http_ok(f"http://127.0.0.1:{p}/health"), 15, f"worker on {p} healthy")

    def stop(self) -> None:
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

    def all_health_ok(self) -> bool:
        return all(http_ok(f"http://127.0.0.1:{p}/health") for p in self.ports)

    def summed_metrics(self) -> dict:
        totals = {
            "ws_connections_active": 0.0, "messages_fanned_out_total": 0.0,
            "matches_finalized_total": 0.0, "redis_reconnects_total": 0.0,
        }
        for p in self.ports:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{p}/metrics", timeout=2) as response:
                    text = response.read().decode()
            except Exception:
                continue
            for key in totals:
                totals[key] += parse_prometheus_counter_or_gauge(text, f"pair_{key}")
        return totals


async def run_pair(room_code: str, port_a: int, port_b: int, messages: int,
                    connect_latencies: list, msg_latencies: list, failures: list, timeouts: list) -> None:
    url_a = f"ws://127.0.0.1:{port_a}/ws"
    url_b = f"ws://127.0.0.1:{port_b}/ws"
    t0 = time.monotonic()
    try:
        async with websockets.connect(url_a, close_timeout=1) as ws_a, \
                   websockets.connect(url_b, close_timeout=1) as ws_b:
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
    except TimeoutError as exc:
        timeouts.append(f"{type(exc).__name__}: {exc}")
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")


async def main_async(num_workers: int, num_clients: int, messages: int, concurrency_ramp: int) -> dict:
    ok, reason = host_load_check()
    print(f"Host load check: {reason}")
    if not ok:
        raise SystemExit(f"Aborting: {reason}")

    cluster = Cluster(num_workers)
    print(f"Starting {num_workers} worker(s) + Redis...")
    cluster.start()
    try:
        num_rooms = num_clients // 2
        connect_latencies, msg_latencies, failures, timeouts = [], [], [], []
        sem = asyncio.Semaphore(concurrency_ramp)

        async def bounded_run(i):
            # Abort mid-run, not just before starting, if the host degrades
            # while this is in flight.
            ok, reason = host_load_check(max_load_per_core=2.5, min_free_swap_mb=150)
            if not ok:
                failures.append(f"HostOverloaded: {reason}")
                return
            async with sem:
                port_a = cluster.ports[i % len(cluster.ports)]
                port_b = cluster.ports[(i + 1) % len(cluster.ports)]
                await run_pair(f"R{i:05d}", port_a, port_b, messages, connect_latencies, msg_latencies, failures, timeouts)

        start = time.monotonic()
        tasks = [asyncio.create_task(bounded_run(i)) for i in range(num_rooms)]
        await asyncio.gather(*tasks)
        elapsed = time.monotonic() - start

        health_after = cluster.all_health_ok()
        total_messages = len(msg_latencies)
        sorted_msg = sorted(msg_latencies)

        def pct(data, p):
            if not data:
                return None
            idx = min(len(data) - 1, max(0, int(len(data) * p) - 1))
            return round(data[idx] * 1000, 2)

        result = {
            "workers": num_workers,
            "clients": num_clients,
            "rooms": num_rooms,
            "messages_per_pair": messages,
            "elapsed_seconds": round(elapsed, 2),
            "successful_pairs": len(connect_latencies),
            "timed_out_pairs": len(timeouts),
            "other_failed_pairs": len(failures),
            "connect_success_rate": round(len(connect_latencies) / num_rooms, 4) if num_rooms else None,
            "total_messages_delivered": total_messages,
            "throughput_msgs_per_sec": round(total_messages / elapsed, 1) if elapsed > 0 else 0,
            "message_latency_ms": {"p50": pct(sorted_msg, 0.50), "p95": pct(sorted_msg, 0.95), "p99": pct(sorted_msg, 0.99)},
            "health_responsive_after_run": health_after,
            "sample_timeouts": timeouts[:3],
            "sample_other_failures": failures[:3],
            "server_metrics": cluster.summed_metrics(),
        }
        print(json.dumps(result, indent=2))
        return result
    finally:
        cluster.stop()


def main() -> None:
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
