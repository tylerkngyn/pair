"""Structured event logging and a minimal, hand-rolled Prometheus text
exporter — no prometheus_client dependency, since this app's metric
surface is small enough that a ~60-line exporter covers it without
pulling in a library whose registry/multiprocess machinery this
single-process-per-worker app doesn't need.

Metrics here are per-process, same as the earlier app/metrics.py this
replaces/extends: each worker reports only what it itself has seen (how
many local connections it holds, how many events it fanned out), not a
cluster-wide total. Consistent with the rest of the app, where only Redis
room/queue membership is actually shared state — see docs/architecture.md's
Observability section for why cluster-wide aggregation isn't built here.
"""

import json
import logging
import time

logger = logging.getLogger("pair.events")


def log_event(event: str, **fields) -> None:
    """One JSON line per call, to the "pair.events" logger. `fields` should
    include whatever ID makes the event traceable - peer_id, user_id,
    match_id, room, etc. - per the call site's own docstring/comment.
    """
    logger.info(json.dumps({"event": event, "ts": round(time.time(), 6), **fields}))


class _Counter:
    def __init__(self, name: str, help_text: str):
        self.name = name
        self.help_text = help_text
        self.value = 0.0

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount

    def render(self) -> str:
        return f"# HELP {self.name} {self.help_text}\n# TYPE {self.name} counter\n{self.name} {self.value}\n"


class _Gauge:
    def __init__(self, name: str, help_text: str):
        self.name = name
        self.help_text = help_text
        self.value = 0.0

    def set(self, value: float) -> None:
        self.value = value

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        self.value -= amount

    def render(self) -> str:
        return f"# HELP {self.name} {self.help_text}\n# TYPE {self.name} gauge\n{self.name} {self.value}\n"


class _Histogram:
    """Cumulative buckets, matching Prometheus's own exposition format:
    each bucket's count includes every observation at or below it, so
    `_bucket{le="+Inf"}` always equals the total observation count.
    """

    def __init__(self, name: str, help_text: str, buckets: list[float]):
        self.name = name
        self.help_text = help_text
        self.buckets = sorted(buckets)
        self.bucket_counts = [0] * len(self.buckets)
        self.sum = 0.0
        self.count = 0

    def observe(self, value: float) -> None:
        self.sum += value
        self.count += 1
        for i, upper_bound in enumerate(self.buckets):
            if value <= upper_bound:
                self.bucket_counts[i] += 1

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        for upper_bound, bucket_count in zip(self.buckets, self.bucket_counts):
            lines.append(f'{self.name}_bucket{{le="{upper_bound}"}} {bucket_count}')
        lines.append(f'{self.name}_bucket{{le="+Inf"}} {self.count}')
        lines.append(f"{self.name}_sum {self.sum}")
        lines.append(f"{self.name}_count {self.count}")
        return "\n".join(lines) + "\n"


class Metrics:
    def __init__(self):
        self.ws_connections_active = _Gauge(
            "pair_ws_connections_active", "WebSocket connections this worker currently holds locally",
        )
        self.messages_fanned_out_total = _Counter(
            "pair_messages_fanned_out_total", "Room/user events delivered to a locally-held peer",
        )
        self.fanout_latency_seconds = _Histogram(
            "pair_fanout_latency_seconds", "Time to fan one event out to its locally-held peer(s)",
            buckets=[0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
        )
        self.ranked_queue_depth = _Gauge(
            "pair_ranked_queue_depth",
            # In-process backend only — see docs/architecture.md's
            # Observability section for why the Redis-backed queue's depth
            # isn't tracked here (it would need an extra async Redis round
            # trip on every /metrics scrape to sum every filter group's
            # ZCARD; not done to keep /metrics a cheap, synchronous read).
            "Accounts currently waiting in the in-process ranked matchmaking queue",
        )
        self.matches_finalized_total = _Counter(
            "pair_matches_finalized_total", "Ranked matches that completed Elo finalization",
        )
        self.finalize_race_prevented_total = _Counter(
            "pair_finalize_race_prevented_total",
            "Finalize attempts correctly turned away because another request/worker already won "
            "(the Redis lock or the DB status=active guard)",
        )
        self.redis_reconnects_total = _Counter(
            "pair_redis_reconnects_total",
            "Times the Redis pub/sub listener or reap loop recovered from a dropped connection",
        )

    def render(self) -> str:
        return "\n".join((
            self.ws_connections_active.render(),
            self.messages_fanned_out_total.render(),
            self.fanout_latency_seconds.render(),
            self.ranked_queue_depth.render(),
            self.matches_finalized_total.render(),
            self.finalize_race_prevented_total.render(),
            self.redis_reconnects_total.render(),
        ))


METRICS = Metrics()
