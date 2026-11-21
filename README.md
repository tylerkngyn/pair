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

Open <http://127.0.0.1:8000>. Generate a code, enter a display name (or leave it
blank), and join. In a second tab, enter the same code and a different name.
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
{"type":"ping","payload":{}}
```

Room codes are six ASCII alphanumeric characters, normalized to uppercase.
Handles are optional, at most 32 characters, and default to `Player-1234`-style
random names. Duplicate handles in one room are rejected with `INVALID_MSG` so
sent and received messages remain unambiguous. Choose a different name to retry.
Chat text must contain 1–500 Unicode code points and cannot be all whitespace.
Text is preserved and rendered with `textContent`, never HTML.

Server events:

```json
{"type":"room_state","payload":{"room":"ABC123","status":"waiting","peers":["Alice"]}}
{"type":"room_state","payload":{"room":"ABC123","status":"paired","peers":["Alice","Bob"]}}
{"type":"chat","payload":{"sender":"Alice","text":"hello","timestamp":1700000000}}
{"type":"system","payload":{"text":"Bob joined the room"}}
{"type":"system","payload":{"text":"Bob left the room"}}
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
