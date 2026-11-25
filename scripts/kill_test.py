"""Kill test: two real uvicorn workers behind a shared real Redis, one peer
on each. SIGKILL the worker holding peer A mid-session (no close handshake,
no chance to run its own cleanup) and measure wall-clock time until the
survivor on the *other* worker sees "left the room" / room_state back to
waiting. This is the number that turns "the room recovers from a crashed
worker" into a fact instead of a claim - re-run against the current
codebase (same idea as the version deleted in commit dd9882e's pivot
merge; rewritten to support --reps and report median/min-max).

Not a pytest test (tests/test_multiprocess_integration.py already proves
recovery functionally with a simulated TTL for speed) - this script uses
the real production PRESENCE_TTL_SECONDS/REAP_INTERVAL_SECONDS defaults
read directly from app.redis_room_manager, so the recovery time it
reports is the real one, not an accelerated test value. Each rep takes
roughly PRESENCE_TTL_SECONDS + REAP_INTERVAL_SECONDS wall-clock time to
wait out the real timers - this is slow on purpose.

Usage:
    python scripts/kill_test.py --reps 3
"""

import argparse
import asyncio
import json
import os
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
sys.path.insert(0, str(REPO_ROOT))
from app.redis_room_manager import PRESENCE_TTL_SECONDS, REAP_INTERVAL_SECONDS  # noqa: E402


def free_port() -> int:
    sock = socketlib.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_for(predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for {description}")


def http_ok(url):
    try:
        with urllib.request.urlopen(url, timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def redis_reachable(url):
    import redis as sync_redis
    try:
        sync_redis.Redis.from_url(url, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


async def drain_until(ws, event_type, predicate, timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"waiting for {event_type}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        event = json.loads(raw)
        if event.get("type") == event_type and predicate(event.get("payload", {})):
            return event["payload"]


async def heartbeat(ws, interval=15):
    """Mimics the real browser's ping heartbeat so the server's 60s
    idle-socket timeout never fires while we're just waiting to observe
    the crash/reap behavior."""
    try:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"type": "ping", "payload": {}}))
    except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
        pass


async def scenario(room_code: str, port_a: int, port_b: int, proc_a: subprocess.Popen):
    async with websockets.connect(f"ws://127.0.0.1:{port_a}/ws", close_timeout=1) as ws_a, \
               websockets.connect(f"ws://127.0.0.1:{port_b}/ws", close_timeout=1) as ws_b:
        hb_task = asyncio.create_task(heartbeat(ws_b))
        await ws_a.send(json.dumps({"type": "join", "payload": {"room": room_code, "handle": "Alice"}}))
        await ws_b.send(json.dumps({"type": "join", "payload": {"room": room_code, "handle": "Bob"}}))
        await drain_until(ws_a, "room_state", lambda p: p["status"] == "paired")
        await drain_until(ws_b, "room_state", lambda p: p["status"] == "paired")
        await ws_a.send(json.dumps({"type": "chat", "payload": {"text": "hello before kill"}}))
        await drain_until(ws_b, "chat", lambda p: p["text"] == "hello before kill")

        kill_time = time.monotonic()
        proc_a.kill()
        proc_a.wait(timeout=5)

        payload = await drain_until(ws_b, "system", lambda p: "left the room" in p["text"], timeout=180)
        detect_time = time.monotonic()
        assert "Alice" in payload["text"]

        payload = await drain_until(ws_b, "room_state", lambda p: p["status"] == "waiting", timeout=5)
        recover_time = time.monotonic()
        assert payload["peers"] == ["Bob"]

        async with websockets.connect(f"ws://127.0.0.1:{port_b}/ws", close_timeout=1) as ws_c:
            await ws_c.send(json.dumps({"type": "join", "payload": {"room": room_code, "handle": "Carol"}}))
            await drain_until(ws_c, "room_state", lambda p: p["status"] == "paired")

        hb_task.cancel()
        return detect_time - kill_time, recover_time - kill_time


def run_one_rep(rep_index: int) -> dict:
    if not shutil.which("docker"):
        raise SystemExit("Docker required for this test")

    redis_port = free_port()
    redis_name = f"pair-killtest-redis-{redis_port}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", redis_name, "-p", f"{redis_port}:6379", "redis:7-alpine"],
        check=True, capture_output=True, timeout=30,
    )
    redis_url = f"redis://127.0.0.1:{redis_port}/0"
    wait_for(lambda: redis_reachable(redis_url), 15, "redis ready")

    env = {**os.environ, "REDIS_URL": redis_url}
    port_a, port_b = free_port(), free_port()
    proc_a = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port_a)],
        cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc_b = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port_b)],
        cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: http_ok(f"http://127.0.0.1:{port_a}/health"), 10, "worker A healthy")
        wait_for(lambda: http_ok(f"http://127.0.0.1:{port_b}/health"), 10, "worker B healthy")

        detect_s, recover_s = asyncio.run(scenario(f"KILL{rep_index:02d}", port_a, port_b, proc_a))
        return {"detect_seconds": round(detect_s, 1), "recover_seconds": round(recover_s, 1)}
    finally:
        for proc in (proc_a, proc_b):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        subprocess.run(["docker", "rm", "-f", redis_name], capture_output=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=3)
    args = parser.parse_args()

    print(f"PRESENCE_TTL_SECONDS={PRESENCE_TTL_SECONDS} REAP_INTERVAL_SECONDS={REAP_INTERVAL_SECONDS} "
          f"(worst-case bound = {PRESENCE_TTL_SECONDS + REAP_INTERVAL_SECONDS}s)")

    reps = []
    for i in range(args.reps):
        print(f"\n=== rep {i + 1}/{args.reps} ===")
        result = run_one_rep(i)
        print(json.dumps(result))
        reps.append(result)

    detects = [r["detect_seconds"] for r in reps]
    recovers = [r["recover_seconds"] for r in reps]
    summary = {
        "presence_ttl_seconds": PRESENCE_TTL_SECONDS,
        "reap_interval_seconds": REAP_INTERVAL_SECONDS,
        "reps": reps,
        "detect_seconds": {"median": statistics.median(detects), "min": min(detects), "max": max(detects)},
        "recover_seconds": {"median": statistics.median(recovers), "min": min(recovers), "max": max(recovers)},
    }
    print("\n=== summary ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
