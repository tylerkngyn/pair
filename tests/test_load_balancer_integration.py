"""Proves cross-worker delivery works through a real load balancer, not just
two manually-wired local processes (as in test_multiprocess_integration.py).

The Redis Pub/Sub design was specifically chosen so WebSocket connections
never need session affinity/sticky sessions: any worker can deliver any
room's events once it has a local peer in that room. This test is the one
place that claim is actually exercised against a real reverse proxy: two
real uvicorn processes sit behind a real nginx container doing plain
round-robin (explicitly NOT ip_hash/sticky), and two real WebSocket clients
connect through the proxy, get routed to different backends by nginx's
default round-robin, join the same room, and still chat correctly.

This does not test Render's actual production load balancer (a managed
black box, not something you can instantiate here) — see the README's
"Scaling beyond one instance in production" section for that distinction.

Opt-in only, same as test_multiprocess_integration.py: excluded from the
default `pytest -q` run, requires Docker. Skips cleanly without it.

Run explicitly with: python -m pytest -m integration -v
"""

import asyncio
import json
import os
import shutil
import socket as socketlib
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest
import websockets

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

# Mounted at /etc/nginx/conf.d/default.conf, which the base nginx:alpine
# image already includes from inside its own http{} block — this file must
# NOT wrap itself in events{}/http{} or nginx refuses to start.
NGINX_CONF_TEMPLATE = """\
upstream pair_backend {{
  server host.docker.internal:{port_a};
  server host.docker.internal:{port_b};
}}
server {{
  listen 80;
  location / {{
    proxy_pass http://pair_backend;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_read_timeout 3600s;
  }}
}}
"""


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


def _http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
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
    name = f"pair-test-redis-lb-{port}"
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


@pytest.fixture
def two_workers_behind_lb(real_redis_url):
    app_ports = [_free_port(), _free_port()]
    env = {**os.environ, "REDIS_URL": real_redis_url}
    procs = []
    nginx_name = None
    conf_file = None
    try:
        for port in app_ports:
            proc = subprocess.Popen(
                # Must bind 0.0.0.0, not 127.0.0.1: the nginx container
                # reaches it via host.docker.internal, which looks like a
                # different source address than loopback-only would accept.
                ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)],
                cwd=REPO_ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            procs.append(proc)
        for port in app_ports:
            _wait_for(
                lambda port=port: _http_ok(f"http://127.0.0.1:{port}/health"),
                timeout=10, description=f"uvicorn on port {port} to become healthy",
            )

        conf_file = tempfile.NamedTemporaryFile(mode="w", suffix=".conf", delete=False)
        conf_file.write(NGINX_CONF_TEMPLATE.format(port_a=app_ports[0], port_b=app_ports[1]))
        conf_file.close()

        lb_port = _free_port()
        nginx_name = f"pair-test-nginx-{lb_port}"
        try:
            subprocess.run(
                [
                    "docker", "run", "-d", "--rm", "--name", nginx_name,
                    "-p", f"{lb_port}:80",
                    "--add-host=host.docker.internal:host-gateway",
                    "-v", f"{conf_file.name}:/etc/nginx/conf.d/default.conf:ro",
                    "nginx:alpine",
                ],
                check=True, capture_output=True, timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            pytest.skip(f"Could not start nginx container: {exc}")

        _wait_for(
            lambda: _http_ok(f"http://127.0.0.1:{lb_port}/health"),
            timeout=15, description="nginx load balancer to become healthy",
        )
        yield lb_port
    finally:
        if nginx_name:
            subprocess.run(["docker", "rm", "-f", nginx_name], capture_output=True)
        if conf_file:
            os.unlink(conf_file.name)
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


def test_chat_works_across_round_robin_load_balanced_backends(two_workers_behind_lb):
    """Two WebSocket clients connect through one nginx URL. Plain round-robin
    (no ip_hash/sticky sessions configured) means nginx hands sequential
    connections to alternating backends, so these two peers land on
    different uvicorn processes — proving the no-sticky-sessions
    architecture actually works through a real reverse proxy, not just
    when a test manually dials two separate ports itself.
    """
    lb_port = two_workers_behind_lb

    async def scenario():
        url = f"ws://127.0.0.1:{lb_port}/ws"
        async with websockets.connect(url) as ws_a, websockets.connect(url) as ws_b:
            await send_join(ws_a, "LBTEST", "Alice")
            await drain_until(ws_a, "room_state", lambda p: p["status"] == "waiting")
            await send_join(ws_b, "LBTEST", "Bob")
            await drain_until(ws_a, "room_state", lambda p: p["status"] == "paired")
            await drain_until(ws_b, "room_state", lambda p: p["status"] == "paired")

            await send_chat(ws_a, "hi through the LB")
            payload = await drain_until(ws_b, "chat", lambda p: p["text"] == "hi through the LB")
            assert payload["sender"] == "Alice"

            await send_chat(ws_b, "reply through the LB")
            payload = await drain_until(ws_a, "chat", lambda p: p["text"] == "reply through the LB")
            assert payload["sender"] == "Bob"

    asyncio.run(scenario())
