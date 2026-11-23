"""Contracts for the cooperative image-word game, SPEC ONLY.

This module defines the interfaces and wire-adjacent data shapes that a real
implementation of the game will be built against. It intentionally contains
no game logic, no scoring, no socket I/O, and no Redis calls — see the four
Protocols below for exactly where each kind of work is meant to live.

Design intent, summarized (see docs/protocol.md for the full writeup):

- GameEngine.apply is pure and synchronous: given a state, a command, and
  read-only context, it returns a Transition. It never touches a socket or
  Redis, so it can be unit tested with plain data and reused unchanged
  whether the caller is RoomManager (in-process) or RedisRoomManager
  (multi-worker).
- GameRepository is where atomicity lives. load/compare_and_set follow the
  same optimistic-concurrency shape as RedisRoomManager's Lua-scripted
  HSET-after-HLEN pattern in app/redis_room_manager.py: callers read a
  version, compute a next state off of it, and the store only commits if
  the version hasn't moved. CommandResult is persisted in the same atomic
  write as the state, so a retried request_id (same client, dropped ack,
  resent command) replays the stored result instead of re-running game
  logic and double-scoring.
- RoomTransport is the thin boundary to actual delivery. Both RoomManager
  and RedisRoomManager already draw this line (local dict of Peers vs.
  Redis hash + pub/sub) — this Protocol just names the two operations the
  game needs from whichever one is active: a stable, order-preserving peer
  list, and fan-out publish.
- ScoreProvider is read-only and deterministic for a given
  (catalog_version, image_id, canonical_word) triple, so the engine can call
  it freely inside apply() without that violating purity from the caller's
  point of view — same catalog version in, same answer out, no side effects.
"""

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field

from app.strict_model import StrictModel

# --------------------------------------------------------------------------
# Shared vocabulary
# --------------------------------------------------------------------------

Phase = Literal["lobby", "loading", "playing", "finished"]
ScoreTier = Literal["Perfect", "Great", "Good", "Unscored"]


class ImageMeta(StrictModel):
    """Everything a client needs to render the round's prompt image.

    image_id is the stable identifier ScoreProvider.lookup keys on; url is
    whatever the client fetches pixels from and may rotate independently
    (e.g. a signed URL) without changing image_id.
    """

    image_id: str
    url: str
    alt_text: str = ""


class AcceptedWord(StrictModel):
    """One canonical word a peer has already landed on for the current round."""

    peer_id: str
    canonical_word: str
    tier: ScoreTier


class RoundRecap(StrictModel):
    """Summary of a just-finished round, carried in the snapshot that follows it."""

    round_id: str
    image: ImageMeta
    accepted_words: list[AcceptedWord]
    points_awarded: int


class GameState(StrictModel):
    """The full authoritative game state for one room, as GameRepository stores it.

    schema_version guards against a deployed engine reading a state shape it
    no longer understands (e.g. mid-rollout across workers); version is the
    optimistic-concurrency counter compare_and_set operates on, distinct
    from schema_version and bumped on every accepted command.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int
    version: int
    room_id: str
    phase: Phase
    leader_id: str
    admission_order: list[str]
    round_id: str | None = None
    image: ImageMeta | None = None
    server_deadline_ms: int | None = None
    shared_score: int = 0
    accepted_words: list[AcceptedWord] = Field(default_factory=list)
    used_words: list[str] = Field(default_factory=list)
    recap: RoundRecap | None = None
    # request_id -> CommandResult, for idempotent replay. Bounded/pruned by
    # the repository implementation, not by the engine.
    applied_request_ids: dict[str, "CommandResult"] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Commands (client -> engine)
# --------------------------------------------------------------------------

class CommandContext(StrictModel):
    """Read-only facts the engine needs that are not part of GameState itself.

    Supplied by the caller (RoomManager/RedisRoomManager glue), never
    mutated by the engine.
    """

    peer_id: str
    now_ms: int
    members: list[str]  # stable peer IDs, in admission order; see RoomTransport.members


class GameStartCommand(StrictModel):
    type: Literal["game_start"] = "game_start"
    request_id: str


class RoundReadyCommand(StrictModel):
    type: Literal["round_ready"] = "round_ready"
    request_id: str
    round_id: str


class GuessCommand(StrictModel):
    type: Literal["guess"] = "guess"
    request_id: str
    round_id: str
    word: str = Field(min_length=1, max_length=64)


class GameSyncCommand(StrictModel):
    type: Literal["game_sync"] = "game_sync"
    request_id: str


Command = GameStartCommand | RoundReadyCommand | GuessCommand | GameSyncCommand


# --------------------------------------------------------------------------
# Results and transitions (engine -> caller)
# --------------------------------------------------------------------------

class CommandResult(StrictModel):
    """What a command produced, stored alongside the state it produced.

    Persisting this (keyed by request_id, inside GameState) is what lets a
    retried request replay the same result instead of re-scoring: see
    GameRepository.compare_and_set.
    """

    request_id: str
    accepted: bool
    reason: str = ""
    tier: ScoreTier | None = None
    points_delta: int = 0


class Transition(StrictModel):
    """The pure output of GameEngine.apply: a candidate next state plus the
    result to persist with it, and the events to publish if the caller's
    compare_and_set of (next_state, command_result) actually commits.

    The caller is responsible for the compare_and_set race: if it loses
    (version moved under it), it must re-load, and typically re-apply,
    rather than publish events for a state that was never committed.
    """

    next_state: GameState
    command_result: CommandResult
    events: list["GameEvent"]


# --------------------------------------------------------------------------
# Server events (engine/caller -> clients), published via RoomTransport
# --------------------------------------------------------------------------

class GameStateEvent(StrictModel):
    type: Literal["game_state"] = "game_state"
    payload: GameState


class GuessResultEvent(StrictModel):
    type: Literal["guess_result"] = "guess_result"
    payload: CommandResult


GameEvent = GameStateEvent | GuessResultEvent


# --------------------------------------------------------------------------
# GameEngine: pure, synchronous game logic
# --------------------------------------------------------------------------

@runtime_checkable
class GameEngine(Protocol):
    """Pure game-rule logic. No I/O: no socket sends, no Redis calls, no
    clock reads beyond what CommandContext.now_ms supplies. Implementations
    must be safe to call from either RoomManager or RedisRoomManager, and
    safe to call twice with the same (state, command) and get the same
    Transition back (modulo CommandContext.now_ms-driven deadline checks).
    """

    def apply(
        self,
        state: GameState,
        command: Command,
        context: CommandContext,
        scores: "ScoreProvider",
    ) -> Transition:
        """Compute the next state for one command.

        Must not mutate `state` in place; return a new GameState via
        next_state. `scores` is provided (rather than imported/instantiated
        internally) so apply() stays a pure function of its arguments and
        easy to test with a fake ScoreProvider.
        """
        ...


# --------------------------------------------------------------------------
# GameRepository: versioned, atomic, idempotent persistence
# --------------------------------------------------------------------------

@runtime_checkable
class GameRepository(Protocol):
    """Storage for GameState, with optimistic concurrency.

    Mirrors the shape RedisRoomManager already uses for room membership
    (read, compute, Lua-scripted conditional write) so a Redis-backed
    implementation can reuse that pattern directly; an in-process
    implementation can use a plain asyncio.Lock per room instead.
    """

    async def load(self, room_id: str) -> GameState | None:
        """Return the current state, or None if no game has started."""
        ...

    async def compare_and_set(
        self,
        room_id: str,
        expected_version: int,
        next_state: GameState,
        command_result: CommandResult,
    ) -> bool:
        """Atomically commit next_state (and record command_result under its
        request_id) iff the stored state's version still equals
        expected_version. Returns whether the commit happened.

        The state write and the command_result write MUST be atomic together
        (single transaction / single Lua script), not two sequential calls —
        otherwise a crash between them could persist a state whose
        request_id never gets recorded, defeating idempotent replay on retry.
        """
        ...


# --------------------------------------------------------------------------
# RoomTransport: delivery boundary (already split this way in RoomManager /
# RedisRoomManager; this just names the two operations the game needs)
# --------------------------------------------------------------------------

@runtime_checkable
class RoomTransport(Protocol):
    async def members(self, room_id: str) -> list[str]:
        """Stable peer IDs currently in the room, in explicit admission
        order (first-joined first). This is the ordering leader-election
        uses; it must not depend on Redis HGETALL's incidental ordering,
        which is unspecified and may not reflect join order. See
        docs/protocol.md for why admission order is tracked separately
        (GameState.admission_order) rather than derived from the transport
        at election time.
        """
        ...

    async def publish(self, room_id: str, event: "GameEvent") -> None:
        """Fan out an event to every peer currently in the room, local or
        remote-worker, the same way RedisRoomManager._publish already does
        for chat/typing/reaction events.
        """
        ...


# --------------------------------------------------------------------------
# ScoreProvider: read-only, deterministic lookup
# --------------------------------------------------------------------------

@runtime_checkable
class ScoreProvider(Protocol):
    def lookup(
        self, catalog_version: str, image_id: str, canonical_word: str,
    ) -> ScoreTier:
        """Deterministic, side-effect-free: the same three inputs always
        return the same tier for the lifetime of catalog_version. Returns
        "Unscored" if canonical_word has no entry for this image under this
        catalog version, rather than raising, so GameEngine.apply never
        needs to handle a lookup failure as an error path.
        """
        ...


@dataclass(frozen=True)
class ScoreEntry:
    """Convenience shape a concrete ScoreProvider's catalog data might use;
    not required by the Protocol above."""

    canonical_word: str
    tier: ScoreTier
