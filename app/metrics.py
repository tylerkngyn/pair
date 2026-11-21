"""Minimal per-process counters, exposed as plain JSON via GET /metrics.

Deliberately not Prometheus-format: nothing in this deployment scrapes
Prometheus, and this is a 2-peer MVP, not a service that warrants a metrics
backend. These numbers are per-process (each worker reports only what it has
seen) — consistent with the rest of the app's design, where only Redis room
membership is actually shared state. Cross-worker aggregation, if ever
needed, would be a small Redis-backed counter follow-up, not built here.
"""

from dataclasses import dataclass, field


@dataclass
class Metrics:
    peers_connected_total: int = 0
    reap_sweeps_total: int = 0
    peers_reaped_total: int = 0
    _rooms_active_getter: list = field(default_factory=list, repr=False)

    def set_rooms_active_getter(self, getter) -> None:
        """getter() -> int, called lazily so it always reflects live state."""
        self._rooms_active_getter[:] = [getter]

    @property
    def rooms_active(self) -> int:
        return self._rooms_active_getter[0]() if self._rooms_active_getter else 0

    def record_join(self) -> None:
        self.peers_connected_total += 1

    def record_reap_sweep(self) -> None:
        self.reap_sweeps_total += 1

    def record_peer_reaped(self) -> None:
        self.peers_reaped_total += 1

    def as_dict(self) -> dict:
        return {
            "rooms_active": self.rooms_active,
            "peers_connected_total": self.peers_connected_total,
            "reap_sweeps_total": self.reap_sweeps_total,
            "peers_reaped_total": self.peers_reaped_total,
        }
