"""Phase 2: Redis-backed ranked matchmaking queue.

The Phase 1 queue in ranked_routes.py (_QUEUE, a plain Python list behind
a threading.Lock) only ever sees requests handled by its own process. Once
ranked mode runs on more than one worker/instance, a POST from one worker
and a POST from another must still be able to match each other, so the
queue itself has to become shared state - this module is that shared
state, structured the same way app/redis_room_manager.py's room membership
is: real work happens in Lua scripts so two workers racing the same
request (two joins that could match each other) can't both "win" the same
match, the same reason _JOIN_SCRIPT exists over there.

Data model, per filters-equal group (a distinct ZSET/meta-hash per
group, since matching only ever considers identical filters, same as
Phase 1):
  rq:<filters_hash>        ZSET  member=user_id, score=rating
  rq_meta:<user_id>        HASH  username, rating, filters (json), queued_at,
                                  filters_hash
A user_id's presence in `rq_meta:<id>` is itself "am I queued" - no
separate index needed.

Why a sweep is needed at all, given every key above already carries a
TTL: Redis expires a whole key, never an individual ZSET member. If a
queued account's `rq_meta:<id>` hash TTLs out without an explicit
leave()/match (its owner crashed, or the browser was just closed), its
user_id is still sitting in `rq:<filters_hash>` as a ghost member - both
matching scripts already skip it safely (their `#meta > 0` check), but
nothing ever ZREMs it, so it lingers (and gets rescanned on every
match attempt in that filter group) until the whole ZSET key's own TTL
finally expires. start()/_sweep_once() below is that ZREM, the same
role app/redis_room_manager.py's reap loop plays for room membership.
"""

import asyncio
import hashlib
import json
import os
import time

from redis.asyncio import Redis

QUEUE_KEY_PREFIX = "rq:"
META_KEY_PREFIX = "rq_meta:"
PENDING_KEY_PREFIX = "rq_pending:"
FINALIZE_LOCK_PREFIX = "match_lock:"
ENTRY_TTL_SECONDS = 600  # Safety net only; explicit leave/match removes entries immediately.
PENDING_TTL_SECONDS = 300  # How long a MATCH_FOUND notice waits for a poll if the WS push never confirms.
FINALIZE_LOCK_TTL_SECONDS = 10  # Generous for one Elo update + two Rating writes; see acquire_finalize_lock.

# Overridable only for tests, same convention as app/redis_room_manager.py's
# PAIR_PRESENCE_TTL_SECONDS/PAIR_REAP_INTERVAL_SECONDS.
SWEEP_INTERVAL_SECONDS = int(os.environ.get("PAIR_QUEUE_SWEEP_INTERVAL_SECONDS", "30"))

MIN_WINDOW = 50
WINDOW_STEP = 25
WINDOW_GROWTH_SECONDS = 5
MAX_WINDOW = 400

# KEYS[1] = this filter-group's ZSET. KEYS[2] = the joining user's own meta hash.
# ARGV[1] = user_id. ARGV[2] = rating. ARGV[3] = now. ARGV[4] = username.
# ARGV[5] = filters (json). ARGV[6] = filters_hash. ARGV[7] = meta key prefix.
# ARGV[8] = entry ttl. ARGV[9..12] = min_window, window_step, window_growth, max_window.
#
# Matches against whoever already in the group is within the rating window
# widened by THEIR OWN wait (ARGV[3] - candidate's queued_at) - a fresh
# arrival should still be able to immediately match a long-waiting
# candidate. Candidates come back from ZRANGEBYSCORE in rating order, not
# wait-time order; the first one within window wins, same tie-break the
# in-process version uses (whichever `next()` finds first).
_JOIN_SCRIPT = """
if redis.call('EXISTS', KEYS[2]) == 1 then
  return cjson.encode({status = 'already_queued'})
end
local rating = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local max_window = tonumber(ARGV[12])
local candidates = redis.call('ZRANGEBYSCORE', KEYS[1], rating - max_window, rating + max_window)
for _, candidate_id in ipairs(candidates) do
  local meta_key = ARGV[7] .. candidate_id
  local meta = redis.call('HGETALL', meta_key)
  if #meta > 0 then
    local fields = {}
    for i = 1, #meta, 2 do fields[meta[i]] = meta[i + 1] end
    local waited = now - tonumber(fields['queued_at'])
    local window = math.min(max_window, tonumber(ARGV[9]) + tonumber(ARGV[10]) * math.floor(waited / tonumber(ARGV[11])))
    if math.abs(tonumber(fields['rating']) - rating) <= window then
      redis.call('ZREM', KEYS[1], candidate_id)
      redis.call('DEL', meta_key)
      return cjson.encode({status = 'matched', opponent_id = candidate_id, opponent = fields})
    end
  end
end
redis.call('ZADD', KEYS[1], rating, ARGV[1])
redis.call('HSET', KEYS[2], 'username', ARGV[4], 'rating', rating, 'filters', ARGV[5], 'queued_at', now, 'filters_hash', ARGV[6])
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[8]))
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[8]))
return cjson.encode({status = 'queued', queued_at = now})
"""

# KEYS[1] = this filter-group's ZSET. KEYS[2] = the polling user's own meta hash.
# ARGV[1] = user_id. ARGV[2] = now. ARGV[3] = meta key prefix.
# ARGV[4..7] = min_window, window_step, window_growth, max_window.
#
# Unlike _JOIN_SCRIPT, the widening here uses the OLDER of the two
# queued_at timestamps (own vs candidate) - a long-waiting poller gets
# credit for its own wait against a candidate who just arrived, not just
# the reverse. See ranked_routes.py's _find_opponent for the in-process
# equivalent and why min(), not max(), is the correct choice here.
_POLL_SCRIPT = """
local own = redis.call('HGETALL', KEYS[2])
if #own == 0 then
  return cjson.encode({status = 'idle'})
end
local own_fields = {}
for i = 1, #own, 2 do own_fields[own[i]] = own[i + 1] end
local own_rating = tonumber(own_fields['rating'])
local own_queued_at = tonumber(own_fields['queued_at'])
local now = tonumber(ARGV[2])
local max_window = tonumber(ARGV[7])
local candidates = redis.call('ZRANGEBYSCORE', KEYS[1], own_rating - max_window, own_rating + max_window)
for _, candidate_id in ipairs(candidates) do
  if candidate_id ~= ARGV[1] then
    local meta_key = ARGV[3] .. candidate_id
    local meta = redis.call('HGETALL', meta_key)
    if #meta > 0 then
      local fields = {}
      for i = 1, #meta, 2 do fields[meta[i]] = meta[i + 1] end
      local candidate_queued_at = tonumber(fields['queued_at'])
      local oldest = own_queued_at
      if candidate_queued_at < oldest then oldest = candidate_queued_at end
      local waited = now - oldest
      local window = math.min(max_window, tonumber(ARGV[4]) + tonumber(ARGV[5]) * math.floor(waited / tonumber(ARGV[6])))
      if math.abs(tonumber(fields['rating']) - own_rating) <= window then
        redis.call('ZREM', KEYS[1], candidate_id, ARGV[1])
        redis.call('DEL', meta_key, KEYS[2])
        return cjson.encode({status = 'matched', opponent_id = candidate_id, opponent = fields, mine = own_fields})
      end
    end
  end
end
return cjson.encode({status = 'queued'})
"""


def filters_key(filters: dict) -> str:
    """A stable hash of the filters dict, used both as the ZSET key suffix
    and stored in meta for a sweep to find which ZSET a stale entry
    belongs to. json.dumps with sort_keys makes this independent of
    dict insertion order.
    """
    canonical = json.dumps(filters, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class RedisRankedQueue:
    def __init__(self, redis_url: str | None = None, *, client: Redis | None = None) -> None:
        self._redis = client if client is not None else Redis.from_url(redis_url, decode_responses=True)
        self._join_script = self._redis.register_script(_JOIN_SCRIPT)
        self._poll_script = self._redis.register_script(_POLL_SCRIPT)
        self._sweep_task: asyncio.Task | None = None

    async def start(self) -> None:
        self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await self._sweep_once()

    async def _sweep_once(self) -> None:
        """Removes ghost ZSET members — see the module docstring for why
        these can exist despite every key carrying a TTL. Scans every
        filter group, not just ones this call happens to know about,
        since a crashed worker's own bookkeeping (if it even had any) is
        gone — only a sweep from any still-running process can notice.
        """
        async for key in self._redis.scan_iter(match=f"{QUEUE_KEY_PREFIX}*"):
            members = await self._redis.zrange(key, 0, -1)
            if not members:
                continue
            # One pipelined round trip for every member's meta-existence
            # check, instead of one EXISTS per member, so a sweep's cost
            # doesn't scale with total queued accounts times a round trip —
            # same reasoning as the room manager's reap sweep.
            pipeline = self._redis.pipeline(transaction=False)
            for user_id in members:
                pipeline.exists(self._meta_key(user_id))
            alive = await pipeline.execute()
            ghosts = [user_id for user_id, is_alive in zip(members, alive) if not is_alive]
            if ghosts:
                await self._redis.zrem(key, *ghosts)

    def _queue_key(self, group: str) -> str:
        return f"{QUEUE_KEY_PREFIX}{group}"

    def _meta_key(self, user_id: str) -> str:
        return f"{META_KEY_PREFIX}{user_id}"

    async def join(self, user_id: str, username: str, rating: int, filters: dict) -> dict:
        """Returns one of:
        {"status": "already_queued"}
        {"status": "queued", "queued_at": float}
        {"status": "matched", "opponent": {user_id, username, rating, filters, queued_at}}
        """
        group = filters_key(filters)
        now = time.time()
        raw = await self._join_script(
            keys=[self._queue_key(group), self._meta_key(user_id)],
            args=[
                user_id, rating, now, username, json.dumps(filters), group, META_KEY_PREFIX,
                ENTRY_TTL_SECONDS, MIN_WINDOW, WINDOW_STEP, WINDOW_GROWTH_SECONDS, MAX_WINDOW,
            ],
        )
        result = json.loads(raw)
        if result["status"] == "matched":
            result["opponent"] = _decode_opponent(result.pop("opponent_id"), result["opponent"])
        return result

    async def mark_pending(self, user_id: str, match_id: str) -> None:
        """Called by the HTTP layer right after it creates a match from
        either join()'s or poll()'s "matched" result, for BOTH sides —
        the side whose own join()/poll() call just returned "matched"
        already has the match_id synchronously and doesn't strictly need
        this, but marking it anyway keeps pop_pending()'s "WS delivered,
        so drop the mailbox" logic (ranked_routes.py's _push_match_found
        equivalent) uniform for both sides instead of special-casing one.
        The side that was sitting in the queue when the OTHER side's call
        matched them has no other way to find out except this or a
        successful WS push.
        """
        await self._redis.set(f"{PENDING_KEY_PREFIX}{user_id}", match_id, ex=PENDING_TTL_SECONDS)

    async def pop_pending(self, user_id: str) -> str | None:
        """Consumes (deletes) a pending match notice if one exists."""
        key = f"{PENDING_KEY_PREFIX}{user_id}"
        match_id = await self._redis.get(key)
        if match_id is not None:
            await self._redis.delete(key)
        return match_id

    async def poll(self, user_id: str) -> dict:
        """Returns {"status": "idle"} (not queued at all), {"status":
        "queued"}, {"status": "matched", "match_id": str} (a pending
        notice from someone else's join()/poll() already matching this
        account), or {"status": "matched", "opponent": {...}} (this poll
        itself just found a match — same shape join() returns).

        Unlike join(), the caller has no reason to already know this
        account's filters — that's exactly what a poll is for — so this
        reads the stored meta hash first to find which filter group's
        ZSET to even look in, then runs the matching script against that
        group. (A small race is possible between that read and the script
        run — the entry could be matched by someone else's join() in
        between — but the script's own HGETALL-is-empty check on KEYS[2]
        handles that the same as any other now-stale poll would.)
        """
        pending = await self.pop_pending(user_id)
        if pending:
            return {"status": "matched", "match_id": pending}
        own = await self._redis.hgetall(self._meta_key(user_id))
        if not own:
            return {"status": "idle"}
        group = own["filters_hash"]
        raw = await self._poll_script(
            keys=[self._queue_key(group), self._meta_key(user_id)],
            args=[user_id, time.time(), META_KEY_PREFIX, MIN_WINDOW, WINDOW_STEP, WINDOW_GROWTH_SECONDS, MAX_WINDOW],
        )
        result = json.loads(raw)
        if result["status"] == "matched":
            result["opponent"] = _decode_opponent(result.pop("opponent_id"), result["opponent"])
            # "mine" is this account's own entry as it stood in the queue —
            # the script already deleted it as part of the match, so this
            # is the caller's only remaining copy of it. Needed to build
            # the match (same shape as the dict join()'s caller already
            # has for itself locally, but a poller doesn't have one).
            result["mine"] = _decode_opponent(user_id, result["mine"])
        return result

    async def leave(self, user_id: str) -> bool:
        """True if the user was actually queued; False if there was
        nothing to remove (mirrors the in-process version's 404).
        """
        own = await self._redis.hgetall(self._meta_key(user_id))
        if not own:
            return False
        await self._redis.delete(self._meta_key(user_id))
        await self._redis.zrem(self._queue_key(own["filters_hash"]), user_id)
        return True

    async def acquire_finalize_lock(self, match_id: str, worker_id: str) -> bool:
        """First line of defense against two workers both running Elo
        finalization for the same match at the same moment — e.g. both
        notice the shared deadline passed within the same instant on a
        GET /api/ranked/matches/{id} each happened to receive. True means
        this call won the lock and should proceed; False means another
        worker already holds it (or just finished and the lock hasn't
        expired yet), so this caller must not finalize.

        Deliberately NOT the only guard: _finalize_if_ready's DB-side
        `status == "active"` check is a second, independent safety net in
        ranked_routes.py, in case this lock is ever misused or expires
        mid-operation (NX EX 10 is generous for how fast one Elo update +
        two Rating writes should take, but not infinite).
        """
        return bool(await self._redis.set(f"{FINALIZE_LOCK_PREFIX}{match_id}", worker_id, nx=True, ex=FINALIZE_LOCK_TTL_SECONDS))


def _decode_opponent(user_id: str, fields: dict) -> dict:
    return {
        "user_id": user_id, "username": fields["username"], "rating": int(float(fields["rating"])),
        "filters": json.loads(fields["filters"]), "queued_at": float(fields["queued_at"]),
    }
