"""Kill test: two real uvicorn workers behind a shared real Redis, one peer on
each. SIGKILL the worker holding peer A mid-session (no close handshake, no
chance to run its own cleanup) and measure wall-clock time until the
survivor on the *other* worker sees "left the room" / room_state back to
waiting. This is the number that turns "the room recovers from a crashed
worker" into a fact instead of a claim.

Not a pytest test (tests/test_multiprocess_integration.py already proves
this functionally with a simulated TTL for speed) — this script uses the
real production PRESENCE_TTL_SECONDS/REAP_INTERVAL_SECONDS defaults, so the
recovery time it reports is the real one, not an accelerated test value.

Usage:
    python scripts/kill_test.py                               # production defaults: TTL=90s, reap=20s
    python scripts/kill_test.py --ttl 20 --reap-interval 5     # short-TTL tradeoff comparison

A shorter TTL/reap-interval detects a real crash faster, at the cost of
false-positive evictions against a merely backgrounded/throttled browser
tab (browsers commonly throttle backgrounded setInterval to ~60s — see the
README) — this script only measures the "detects a real crash faster" half
of that tradeoff; it does not simulate a backgrounded-but-alive tab.
"""

import argparse
import asyncio
import json
import os
import shutil
import socket as socketlib
import subprocess
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
    """Mimics the real browser's 20s ping heartbeat (README) so the server's
    60s idle-socket timeout never fires while we're just waiting to observe
    the crash/reap behavior."""
    try:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"type": "ping", "payload": {}}))
    except (asyncio.CancelledError, websockets.exceptions.ConnectionClosed):
        pass


async def scenario(port_a, port_b, proc_a):
    async with websockets.connect(f"ws://127.0.0.1:{port_a}/ws") as ws_a, \
               websockets.connect(f"ws://127.0.0.1:{port_b}/ws") as ws_b:
        hb_task = asyncio.create_task(heartbeat(ws_b))
        await ws_a.send(json.dumps({"type": "join", "payload": {"room": "KILL01", "handle": "Alice"}}))
        await ws_b.send(json.dumps({"type": "join", "payload": {"room": "KILL01", "handle": "Bob"}}))
        await drain_until(ws_a, "room_state", lambda p: p["status"] == "paired")
        await drain_until(ws_b, "room_state", lambda p: p["status"] == "paired")
        print("Paired. Chat works:")
        await ws_a.send(json.dumps({"type": "chat", "payload": {"text": "hello before kill"}}))
        payload = await drain_until(ws_b, "chat", lambda p: p["text"] == "hello before kill")
        print(f"  Bob received: {payload['text']!r} from {payload['sender']}")

        print(f"\nSIGKILL-ing worker A (pid {proc_a.pid}, port {port_a}) mid-session...")
        kill_time = time.monotonic()
        proc_a.kill()
        proc_a.wait(timeout=5)

        print("Waiting for Bob (on surviving worker B) to see Alice leave...")
        payload = await drain_until(ws_b, "system", lambda p: "left the room" in p["text"], timeout=180)
        detect_time = time.monotonic()
        print(f"  system: {payload['text']!r}  ->  {detect_time - kill_time:.1f}s after kill -9")

        payload = await drain_until(ws_b, "room_state", lambda p: p["status"] == "waiting", timeout=5)
        recover_time = time.monotonic()
        print(f"  room_state back to waiting, peers={payload['peers']}  ->  {recover_time - kill_time:.1f}s after kill -9")

        print("\nRoom still usable: Bob can be rejoined by a new peer.")
        async with websockets.connect(f"ws://127.0.0.1:{port_b}/ws") as ws_c:
            await ws_c.send(json.dumps({"type": "join", "payload": {"room": "KILL01", "handle": "Carol"}}))
            await drain_until(ws_c, "room_state", lambda p: p["status"] == "paired")
            print("  Carol joined and re-paired with Bob successfully.")

        hb_task.cancel()
        return detect_time - kill_time, recover_time - kill_time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ttl", type=int, default=90, help="PAIR_PRESENCE_TTL_SECONDS (production default: 90)")
    parser.add_argument("--reap-interval", type=int, default=20, help="PAIR_REAP_INTERVAL_SECONDS (production default: 20)")
    args = parser.parse_args()

    if not shutil.which("docker"):
        raise SystemExit("Docker required for this test")

    redis_port = free_port()
    redis_name = f"pair-killtest-redis-{redis_port}"
    print(f"Starting Redis container on port {redis_port}...")
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", redis_name, "-p", f"{redis_port}:6379", "redis:7-alpine"],
        check=True, capture_output=True, timeout=30,
    )
    redis_url = f"redis://127.0.0.1:{redis_port}/0"
    wait_for(lambda: redis_reachable(redis_url), 15, "redis ready")

    env = {
        **os.environ,
        "REDIS_URL": redis_url,
        "PAIR_PRESENCE_TTL_SECONDS": str(args.ttl),
        "PAIR_REAP_INTERVAL_SECONDS": str(args.reap_interval),
    }
    port_a, port_b = free_port(), free_port()
    print(f"Starting worker A on {port_a}, worker B on {port_b} "
          f"(PRESENCE_TTL={args.ttl}s, REAP_INTERVAL={args.reap_interval}s)...")
    proc_a = subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port_a)],
        cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    proc_b = subprocess.Popen(
        ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port_b)],
        cwd=REPO_ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_for(lambda: http_ok(f"http://127.0.0.1:{port_a}/health"), 10, "worker A healthy")
        wait_for(lambda: http_ok(f"http://127.0.0.1:{port_b}/health"), 10, "worker B healthy")

        detect_s, recover_s = asyncio.run(scenario(port_a, port_b, proc_a))
        print(f"\nRESULT: detect={detect_s:.1f}s  full_recovery={recover_s:.1f}s  "
              f"(TTL={args.ttl}s, reap interval={args.reap_interval}s)")
    finally:
        for proc in (proc_a, proc_b):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
        subprocess.run(["docker", "rm", "-f", redis_name], capture_output=True)


if __name__ == "__main__":
    main()
