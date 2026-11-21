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
tests/
  test_chat.py     Protocol and lifecycle integration tests
requirements.txt
requirements-dev.txt
```

`GET /` serves the UI; `GET /health` returns `{"status":"ok"}`; `WS /ws`
handles the protocol. Routes do not manipulate room storage. A per-room lock
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

Chats echo to the sender as confirmation. Timestamps are server-generated Unix
seconds. On departure, the wire status returns to `waiting`; the UI shows
`Peer disconnected · Waiting for peer` until a new peer joins. Browser heartbeats
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

Run **one Uvicorn worker**: all presence is in process memory and disappears on
restart. In-memory WebSocket presence is process-local, as described in the
[FastAPI WebSocket documentation](https://fastapi.tiangolo.com/advanced/websockets/).
The protocol uses [Pydantic discriminated unions](https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions)
so new lobby/game events can be added explicitly.

To grow this MVP, add event schemas and dispatch handlers; keep membership and
delivery behind `RoomManager`. A distributed Redis replacement will need atomic
capacity/membership operations, expiring presence, and pub/sub to each worker's
local sockets. WebSocket objects themselves cannot be put in Redis. No Redis or
game/scoring behavior is included here. Room codes are sharing conveniences,
not authentication or access control.
