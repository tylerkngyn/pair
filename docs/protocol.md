# Cooperative image-word game — protocol spec (design only)

This document specifies the wire protocol and the concurrency/consistency
rules for the cooperative image-word game feature. It is a contract for
implementation, not an implementation: no scoring logic, engine behavior,
or UI is defined here. See `app/contracts/game.py` for the corresponding
Python interfaces (`GameEngine`, `GameRepository`, `RoomTransport`,
`ScoreProvider`).

It follows the existing chat protocol's conventions in `app/models.py`:
discriminated `{"type": ..., "payload": {...}}` events, one `Literal` per
event type, strict/extra-forbid models.

## Client commands

All client commands carry a `request_id` (client-generated, e.g. a UUID)
so retries after a dropped ack are idempotent server-side — see "Atomic
commit shape" below. `round_ready` and `guess` additionally carry
`round_id`, since they only make sense scoped to a specific round and a
stale/retried command for a round that has since ended must be rejected
rather than silently reapplied to whatever round is current.

### `game_start`

Sent by any peer in `lobby` phase to start the game.

```json
{
  "type": "game_start",
  "payload": {
    "request_id": "c7f1a2e0-8b1a-4e9a-9c3a-1f6b2e7d4a10"
  }
}
```

### `round_ready`

Sent by a peer once it has finished loading the current round's image and
is ready to play.

```json
{
  "type": "round_ready",
  "payload": {
    "request_id": "3b2e9f10-1234-4abc-8def-9a0b1c2d3e4f",
    "round_id": "r_000042"
  }
}
```

### `guess`

Sent by a peer submitting a word for the current round.

```json
{
  "type": "guess",
  "payload": {
    "request_id": "5d6e7f80-aabb-4ccd-8eef-001122334455",
    "round_id": "r_000042",
    "word": "lighthouse"
  }
}
```

### `game_sync`

Sent by a peer that needs a fresh authoritative snapshot (e.g. after
reconnect, or on suspicion of a missed event). No round scoping needed:
it always returns whatever is current.

```json
{
  "type": "game_sync",
  "payload": {
    "request_id": "9988aabb-ccdd-4eef-a001-223344556677"
  }
}
```

## Server events

### `game_state`

The full authoritative snapshot. Sent after every accepted command that
changes state, and in response to `game_sync`.

```json
{
  "type": "game_state",
  "payload": {
    "schema_version": 1,
    "version": 42,
    "room_id": "AB12CD",
    "phase": "playing",
    "leader_id": "peer_a1b2c3d4",
    "admission_order": ["peer_a1b2c3d4", "peer_e5f6a7b8"],
    "round_id": "r_000042",
    "image": {
      "image_id": "img_00912",
      "url": "https://cdn.example.com/images/img_00912.webp",
      "alt_text": "A red lighthouse on a rocky coast at sunset"
    },
    "server_deadline_ms": 1732390845123,
    "shared_score": 180,
    "accepted_words": [
      {"peer_id": "peer_a1b2c3d4", "canonical_word": "lighthouse", "tier": "Perfect"}
    ],
    "recap": null
  }
}
```

`recap`, when present (sent on the snapshot that follows a round ending),
carries the prior round's summary:

```json
"recap": {
  "round_id": "r_000041",
  "image": {
    "image_id": "img_00911",
    "url": "https://cdn.example.com/images/img_00911.webp",
    "alt_text": "A stack of pancakes with syrup"
  },
  "accepted_words": [
    {"peer_id": "peer_a1b2c3d4", "canonical_word": "pancakes", "tier": "Perfect"},
    {"peer_id": "peer_e5f6a7b8", "canonical_word": "breakfast", "tier": "Good"}
  ],
  "points_awarded": 25
}
```

### `guess_result`

Sent to the guessing peer only (not broadcast), telling them what their
own `guess` command did. The shared `game_state` broadcast that follows
(if the guess was accepted) is what tells the other peer.

```json
{
  "type": "guess_result",
  "payload": {
    "request_id": "5d6e7f80-aabb-4ccd-8eef-001122334455",
    "accepted": true,
    "reason": "",
    "tier": "Perfect",
    "points_delta": 25
  }
}
```

A rejected guess (e.g. stale `round_id`, duplicate word already used,
deadline passed) sets `accepted: false` and a human-readable `reason`,
with no `tier`/`points_delta`.

## Leader election

- `leader_id` is an explicit field in `GameState`, never derived
  client-side and never derived from Redis's `HGETALL` ordering, which is
  unspecified and does not reflect join order (`app/redis_room_manager.py`
  stores membership in a hash). The engine is the only writer of
  `leader_id`.
- Admission order is tracked explicitly as `GameState.admission_order`
  (append-only list of peer IDs, in the order each peer successfully
  joined the room), not re-derived from `RoomTransport.members()` at
  election time — `members()` reflects who's connected *right now*, which
  is exactly what makes it unsuitable as the ordering source: a
  reconnecting peer must not jump to the front just because it rejoined
  the transport's live set after others.
- The leader is the earliest peer in `admission_order` that is still a
  current room member. On a leader's departure (disconnect, not just a
  dropped command), the engine reassigns `leader_id` to the next-earliest
  remaining member in `admission_order`, and that reassignment is itself
  committed through the normal `compare_and_set` path (so it's versioned
  and broadcast like any other state change) — not inferred by clients
  from a departure notice.

## Phase machine

```
lobby -> loading -> playing -> loading -> playing -> ... -> finished
                     \_____________ scoped per round ____________/
```

- `lobby`: no round in progress; waiting for `game_start`. Only phase a
  fresh room starts in.
- `loading`: a round's image has been assigned (`round_id`/`image` set)
  but one or both peers haven't yet sent `round_ready` for that
  `round_id`. The engine does not accept `guess` in this phase.
- `playing`: both peers have signaled `round_ready` for the current round;
  `server_deadline_ms` is set and guesses are accepted until the deadline
  or until the round's win condition is met.
- Each `loading -> playing` cycle is scoped to one `round_id`; `playing`
  transitions back to a new `loading` (new `round_id`, new `image`) for
  the next round, or to `finished` if the game's round limit is reached.
- `finished`: not terminal. The room loops back to `lobby` in-place (same
  room, same `room_id`, same `admission_order`/`leader_id` carried over)
  rather than requiring a fresh room, to avoid the latency of
  reconnecting/rejoining for a rematch. The leader can issue `game_start`
  again from `lobby` to begin a new game in the same room. `version`
  keeps incrementing across the rematch; it is not reset.

## Atomic commit shape

Every accepted command produces one atomic write containing all of:

1. the next `GameState` (including the bumped `version` and any
   `used_words`/`accepted_words` changes), and
2. the `CommandResult` for that command's `request_id`.

This mirrors the join-capacity check in `app/redis_room_manager.py`
(`_JOIN_SCRIPT`), which checks-and-writes room membership as one Lua
script so two concurrent joins can't both win the same slot. Here, the
same atomicity requirement exists for a different reason: **idempotent
replay**. A client that doesn't receive its `guess_result` ack (dropped
connection, timeout, retry logic) will resend the same command with the
same `request_id`. Because the command's result was committed in the same
write as the state change it caused, `GameRepository` can recognize a
previously-seen `request_id` and replay the stored `CommandResult`
(and, if useful, resend the already-current `game_state`) instead of
calling `GameEngine.apply` again — which would otherwise re-score the
guess and double-count `shared_score` or re-append a duplicate entry to
`accepted_words`.

Concretely: `GameRepository.compare_and_set(room_id, expected_version,
next_state, command_result)` must write `next_state` and record
`command_result` under its `request_id` together, succeeding only if the
room's stored version still equals `expected_version` — never as two
separate round trips where one could succeed without the other.

## Deadlines live in shared state

`server_deadline_ms` is a field on `GameState`, not a timer local to any
one worker process or client. This follows directly from the rest of the
app's design: `RedisRoomManager` already treats "who's in the room" as
shared state that every worker reads, never something a single worker's
local memory owns. A round deadline is the same kind of fact — if it were
a local `asyncio` timer on whichever worker handled `round_ready`, a
second worker (handling the other peer's socket, per
`app/redis_room_manager.py`'s per-worker local-peer model) would have no
way to know when the round should end, and a crashed worker would lose
the deadline entirely.

Advancement past a deadline (round timing out, moving `playing ->
loading`) is itself a command outcome, not a bare clock check: a worker
that observes `now_ms >= server_deadline_ms` must still go through
`GameEngine.apply` with the current `(round_id, version)` and commit via
`compare_and_set`, so a race between a time-based advance and a
last-second accepted `guess` resolves the same way an ordinary
version-conflict retry would — whichever write lands first with the
expected version wins, and the loser reloads and reconciles rather than
silently overwriting.

## Open questions for sign-off

Resolved: rematch loops back to `lobby` in the same room (see Phase
machine above). Remaining items below are deferred — the current build
goal is the skeleton (contracts + wiring), not full scoring/catalog
behavior, so these don't block Stage 2 implementation.

1. **Scoring catalog versioning.** `ScoreProvider.lookup` takes a
   `catalog_version` — who pins which version a given game uses (room
   creation time? a deploy-wide constant?), and what happens if the
   catalog updates mid-game?
2. **Image sourcing/CDN.** `ImageMeta.url` is treated as opaque and
   possibly rotating independently of `image_id`; where images actually
   come from (static set vs. generated) is out of scope here but affects
   whether `url` ever needs re-signing mid-round.
3. **`applied_request_ids` retention.** `GameState.applied_request_ids`
   needs pruning (by age or count) in any real `GameRepository`
   implementation — unbounded growth isn't addressed by this spec.
4. **Guess normalization.** Whether `word` -> `canonical_word` mapping
   (casing, stemming, synonyms) happens client-side, in `GameEngine`, or
   in `ScoreProvider` isn't specified; this spec only fixes that
   `GameEngine.apply` receives the raw submitted word and is responsible
   for resolving it to whatever `ScoreProvider.lookup` expects.
5. **Deadline duration and extension rules** (e.g. does a correct early
   guess shorten the round for the other peer) are gameplay decisions, not
   fixed here.
