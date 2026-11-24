"""Real multi-process, real-Redis, real-network integration tests.

tests/test_redis_room_manager.py proves RedisRoomManager's algorithm is
correct (membership, capacity, pub/sub fan-out, reaping) by running two
instances in one process sharing one fakeredis server and fake sockets.
It never proves this works across actual separate OS processes, a real
Redis server over a real network connection, or real WebSocket/TCP
connections — that's what this file is for.

Opt-in only: excluded from the default `pytest -q` run by pytest.ini's
`addopts = -m "not integration"`. Requires either a reachable Docker daemon
(a disposable `redis:7-alpine` container is started and removed per module)
or an externally provided `PAIR_TEST_REDIS_URL` pointing at a real Redis.
Neither available -> every test here is skipped, never errored.

Run explicitly with: python -m pytest -m integration -v
"""

import asyncio
import json
import os
import shutil
import socket as socketlib
import subprocess
import time
from pathlib import Path

import pytest
import websockets

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    sock = socketlib.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for(predicate, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    pytest.fail(f"Timed out waiting for {description}")


def _redis_reachable(url: str) -> bool:
    import redis as sync_redis
    try:
        sync_redis.Redis.from_url(url, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


@pytest.fixture(scope="module")
def real_redis_url():
    external = os.environ.get("PAIR_TEST_REDIS_URL")
    if external:
        yield external
        return

    if not shutil.which("docker"):
        pytest.skip("Docker not available and PAIR_TEST_REDIS_URL not set")

    port = _free_port()
    name = f"pair-test-redis-{port}"
    try:
        subprocess.run(
            ["docker", "run", "-d", "--rm", "--name", name, "-p", f"{port}:6379", "redis:7-alpine"],
            check=True, capture_output=True, timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        pytest.skip(f"Docker daemon unreachable or failed to start redis container: {exc}")

    url = f"redis://127.0.0.1:{port}/0"
    try:
        _wait_for(lambda: _redis_reachable(url), timeout=15, description="redis container to accept connections")
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def _http_ok(url: str) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


@pytest.fixture
def two_workers(real_redis_url):
    ports = [_free_port(), _free_port()]
    env = {
        **os.environ,
        "REDIS_URL": real_redis_url,
        # Shrunk for the crash/reap scenario; defaults in app code are unchanged.
        "PAIR_PRESENCE_TTL_SECONDS": "3",
        "PAIR_REAP_INTERVAL_SECONDS": "1",
    }
    procs = []
    try:
        for port in ports:
            proc = subprocess.Popen(
                ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                cwd=REPO_ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            procs.append(proc)
        for port in ports:
            _wait_for(
                lambda port=port: _http_ok(f"http://127.0.0.1:{port}/health"),
                timeout=10, description=f"uvicorn on port {port} to become healthy",
            )
        yield ports
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


async def send_join(ws, room: str, handle: str) -> None:
    await ws.send(json.dumps({"type": "join", "payload": {"room": room, "handle": handle}}))


async def send_chat(ws, text: str) -> None:
    await ws.send(json.dumps({"type": "chat", "payload": {"text": text}}))


async def drain_until(ws, event_type: str, predicate, timeout: float = 5) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            pytest.fail(f"Timed out waiting for a {event_type!r} event matching predicate")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        event = json.loads(raw)
        if event.get("type") == event_type and predicate(event.get("payload", {})):
            return event["payload"]


def test_cross_process_chat_delivery(two_workers):
    port_a, port_b = two_workers

    async def scenario():
        async with websockets.connect(f"ws://127.0.0.1:{port_a}/ws") as ws_a, \
                   websockets.connect(f"ws://127.0.0.1:{port_b}/ws") as ws_b:
            await send_join(ws_a, "ROOM01", "Alice")
            await drain_until(ws_a, "room_state", lambda p: p["status"] == "waiting")
            await send_join(ws_b, "ROOM01", "Bob")
            await drain_until(ws_a, "room_state", lambda p: p["status"] == "paired")
            await drain_until(ws_b, "room_state", lambda p: p["status"] == "paired")

            await send_chat(ws_a, "hi from A")
            payload = await drain_until(ws_b, "chat", lambda p: p["text"] == "hi from A")
            assert payload["sender"] == "Alice"

            await send_chat(ws_b, "hi from B")
            payload = await drain_until(ws_a, "chat", lambda p: p["text"] == "hi from B")
            assert payload["sender"] == "Bob"

    asyncio.run(scenario())


def test_crash_is_reaped_across_processes(two_workers):
    port_a, port_b = two_workers

    async def scenario():
        ws_a = await websockets.connect(f"ws://127.0.0.1:{port_a}/ws")
        async with websockets.connect(f"ws://127.0.0.1:{port_b}/ws") as ws_b:
            await send_join(ws_a, "ROOM02", "Alice")
            await drain_until(ws_a, "room_state", lambda p: p["status"] == "waiting")
            await send_join(ws_b, "ROOM02", "Bob")
            await drain_until(ws_a, "room_state", lambda p: p["status"] == "paired")
            await drain_until(ws_b, "room_state", lambda p: p["status"] == "paired")

            # Abort the raw TCP transport: no WebSocket close frame, no clean
            # disconnect on the server. Only the TTL+reap sweep on process B
            # should ever notice Alice is gone.
            ws_a.transport.abort()

            payload = await drain_until(ws_b, "system", lambda p: "left the room" in p["text"], timeout=10)
            assert "Alice" in payload["text"]
            payload = await drain_until(
                ws_b, "room_state", lambda p: p["status"] == "waiting" and p["peers"] == ["Bob"], timeout=5,
            )
            assert payload["peers"] == ["Bob"]

    asyncio.run(scenario())
