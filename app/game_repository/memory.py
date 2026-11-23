"""In-process GameRepository + a placeholder ScoreProvider.

Mirrors the per-room asyncio.Lock idiom RoomManager uses (see
app/room_manager.py's `Room.lock`) for the optimistic-concurrency
compare_and_set required by app/contracts/game.py's GameRepository Protocol.

This is a Stage-2 skeleton: no persistence across process restarts, no
pruning of stored command results (flagged as a known gap in
docs/protocol.md's "Open questions for sign-off", item 3), and no real
scoring (StubScoreProvider below is a placeholder, not a catalog lookup).
"""

import asyncio
from dataclasses import dataclass, field

from app.contracts.game import CommandResult, GameState, ScoreProvider, ScoreTier


@dataclass
class _RoomRecord:
    """Everything the repository tracks for one room, guarded by `lock`."""

    state: GameState | None = None
    # request_id -> CommandResult, for idempotent replay across restarts of
    # the same command (see docs/protocol.md "Atomic commit shape").
    results: dict[str, CommandResult] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class InMemoryGameRepository:
    """In-process implementation of the GameRepository Protocol.

    Each room gets its own asyncio.Lock (same idiom as RoomManager.Room.lock)
    so compare_and_set is atomic per room without serializing unrelated
    rooms against each other.
    """

    def __init__(self) -> None:
        self._rooms: dict[str, _RoomRecord] = {}

    def _record(self, room_id: str) -> _RoomRecord:
        return self._rooms.setdefault(room_id, _RoomRecord())

    async def load(self, room_id: str) -> GameState | None:
        record = self._rooms.get(room_id)
        if record is None or record.state is None:
            return None
        # Defensive copy: callers must not be able to corrupt "committed"
        # state by mutating what load() handed them (bypasses versioning
        # entirely otherwise). See docs/protocol.md's atomic-commit-shape
        # requirement.
        return record.state.model_copy(deep=True)

    async def compare_and_set(
        self,
        room_id: str,
        expected_version: int,
        next_state: GameState,
        command_result: CommandResult,
    ) -> bool:
        record = self._record(room_id)
        async with record.lock:
            # Idempotent replay: a previously-seen request_id must not
            # re-apply, even if the version check below would otherwise
            # pass. The caller is expected to check this before calling
            # GameEngine.apply, but we guard here too since this is where
            # the atomic write actually happens.
            if command_result.request_id in record.results:
                return False

            current_version = record.state.version if record.state is not None else 0
            if current_version != expected_version:
                return False

            # Defensive copies on the way in too: the caller's `next_state`/
            # `command_result` objects must not keep a live handle into our
            # internal storage either.
            record.state = next_state.model_copy(deep=True)
            record.results[command_result.request_id] = command_result.model_copy(deep=True)
            return True

    async def result_for(self, room_id: str, request_id: str) -> CommandResult | None:
        """Look up a previously-stored CommandResult for idempotent replay.

        Not part of the GameRepository Protocol itself (which only
        specifies load/compare_and_set), but callers (future
        engine/dispatcher wiring) need some way to detect "this request_id
        was already applied" before re-running GameEngine.apply; this is
        the obvious place for that lookup to live for the in-memory
        implementation.
        """
        record = self._rooms.get(room_id)
        if record is None:
            return None
        result = record.results.get(request_id)
        return result.model_copy(deep=True) if result is not None else None


class StubScoreProvider:
    """Placeholder ScoreProvider. NOT real scoring.

    tools/scoring isn't built yet; this always returns a fixed tier so
    engine/dispatcher wiring can be exercised end to end before the real
    catalog lookup exists. Replace with a real implementation before
    shipping actual gameplay.
    """

    def __init__(self, fixed_tier: ScoreTier = "Unscored") -> None:
        self._fixed_tier = fixed_tier

    def lookup(
        self, catalog_version: str, image_id: str, canonical_word: str,
    ) -> ScoreTier:
        return self._fixed_tier
