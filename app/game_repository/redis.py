"""Redis-backed GameRepository, SKELETON implementation.

Follows the same key-naming spirit and Lua check-and-write pattern as
app/redis_room_manager.py's `_JOIN_SCRIPT`: a single script does the
version check and the state + command-result write together, so the two
can never land as separate round trips (see docs/protocol.md, "Atomic
commit shape").

Keys:
  game:<room_id>         -- a Redis HASH with fields "version" (str int)
                             and "state" (GameState, model_dump_json()).
  game_results:<room_id> -- a Redis HASH mapping request_id ->
                             CommandResult.model_dump_json(), for
                             idempotent replay.

Both keys get the same TTL (refreshed on every write), analogous to
ROOM_TTL_SECONDS in app/redis_room_manager.py: this is a game in
progress, not permanent data, so an abandoned room's game state should
eventually fall out of Redis rather than leak forever.
"""

from redis.asyncio import Redis

from app.contracts.game import CommandResult, GameState

GAME_KEY_PREFIX = "game:"
GAME_RESULTS_KEY_PREFIX = "game_results:"

# Safety net only, mirroring ROOM_TTL_SECONDS in app/redis_room_manager.py;
# a game in progress refreshes this on every accepted command, so a live
# room never actually hits it.
GAME_TTL_SECONDS = 3600

# KEYS[1] = game:<room_id> hash (fields: version, state).
# KEYS[2] = game_results:<room_id> hash (request_id -> CommandResult json).
# ARGV[1] = expected_version (string form of an int; missing KEYS[1] counts as "0").
# ARGV[2] = next_state_json (GameState.model_dump_json()).
# ARGV[3] = next_version (string form of next_state.version).
# ARGV[4] = request_id.
# ARGV[5] = command_result_json (CommandResult.model_dump_json()).
# ARGV[6] = ttl seconds.
#
# Returns a 2-element array:
#   {"OK", next_version}        -- committed.
#   {"REPLAY", stored_result}   -- request_id already had a recorded
#                                   result; nothing was (re)written, the
#                                   caller should treat the command as
#                                   already-applied and use stored_result.
#   {"CONFLICT", current_version} -- expected_version didn't match; caller
#                                     must reload and retry.
_COMPARE_AND_SET_SCRIPT = """
local existing_result = redis.call('HGET', KEYS[2], ARGV[4])
if existing_result then
  return {'REPLAY', existing_result}
end
local current_version = redis.call('HGET', KEYS[1], 'version')
if current_version == false then
  current_version = '0'
end
if current_version ~= ARGV[1] then
  return {'CONFLICT', current_version}
end
redis.call('HSET', KEYS[1], 'version', ARGV[3], 'state', ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[6])
redis.call('HSET', KEYS[2], ARGV[4], ARGV[5])
redis.call('EXPIRE', KEYS[2], ARGV[6])
return {'OK', ARGV[3]}
"""


class RedisGameRepository:
    """GameRepository backed by Redis, following RedisRoomManager's Lua
    check-and-write pattern for atomicity.

    Matches the shape app/game_repository/memory.py's InMemoryGameRepository
    settled on: compare_and_set returns False both on a genuine version
    conflict AND when request_id was already applied (so the bool alone
    only tells the caller "not committed by this call, don't publish
    events for it"), and a separate `result_for(room_id, request_id)`
    lookup is how a caller distinguishes replay-and-resend from a bare
    conflict-and-retry, and fetches the actual stored CommandResult to
    resend to the client.
    """

    def __init__(self, redis_url: str | None = None, *, client: Redis | None = None) -> None:
        self._redis = client if client is not None else Redis.from_url(redis_url, decode_responses=True)
        self._compare_and_set_script = self._redis.register_script(_COMPARE_AND_SET_SCRIPT)

    def _game_key(self, room_id: str) -> str:
        return f"{GAME_KEY_PREFIX}{room_id}"

    def _results_key(self, room_id: str) -> str:
        return f"{GAME_RESULTS_KEY_PREFIX}{room_id}"

    async def load(self, room_id: str) -> GameState | None:
        state_json = await self._redis.hget(self._game_key(room_id), "state")
        if state_json is None:
            return None
        return GameState.model_validate_json(state_json)

    async def compare_and_set(
        self,
        room_id: str,
        expected_version: int,
        next_state: GameState,
        command_result: CommandResult,
    ) -> bool:
        """Returns True only if this call actually committed next_state.
        Returns False both when request_id had already been applied
        (replay — see result_for) and on a genuine version conflict (the
        caller lost the race and must reload + retry); the caller tells
        the two apart via result_for, same as the in-memory adapter.
        """
        status, _ = await self._compare_and_set_script(
            keys=[self._game_key(room_id), self._results_key(room_id)],
            args=[
                str(expected_version),
                next_state.model_dump_json(),
                str(next_state.version),
                command_result.request_id,
                command_result.model_dump_json(),
                GAME_TTL_SECONDS,
            ],
        )
        return status == "OK"

    async def result_for(self, room_id: str, request_id: str) -> CommandResult | None:
        """Look up a previously-recorded CommandResult for request_id,
        without attempting any write. Used for idempotent replay: after
        compare_and_set returns True for a request_id the caller didn't
        just originate the result for, this is how it fetches the actual
        stored result to resend.
        """
        result_json = await self._redis.hget(self._results_key(room_id), request_id)
        if result_json is None:
            return None
        return CommandResult.model_validate_json(result_json)
