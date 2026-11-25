import json
import logging

from fastapi.testclient import TestClient

from app.main import app
from app.observability import METRICS, Metrics, log_event


def test_metrics_endpoint_serves_prometheus_text_format():
    with TestClient(app) as client:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        body = response.text
        for name in (
            "pair_ws_connections_active", "pair_messages_fanned_out_total",
            "pair_fanout_latency_seconds", "pair_ranked_queue_depth",
            "pair_matches_finalized_total", "pair_finalize_race_prevented_total",
            "pair_redis_reconnects_total",
        ):
            assert f"# TYPE {name}" in body, f"{name} missing from /metrics output"


def test_metrics_endpoint_reflects_real_websocket_activity():
    with TestClient(app) as client:
        before = client.get("/metrics").text
        before_active = _gauge_value(before, "pair_ws_connections_active")

        with client.websocket_connect("/ws") as socket:
            during = client.get("/metrics").text
            assert _gauge_value(during, "pair_ws_connections_active") == before_active + 1

            socket.send_json({"type": "join", "payload": {"room": "OBS001", "handle": "A"}})
            socket.receive_json()  # system: joined
            socket.receive_json()  # room_state

        after = client.get("/metrics").text
        assert _gauge_value(after, "pair_ws_connections_active") == before_active


def _gauge_value(body: str, name: str) -> float:
    for line in body.splitlines():
        if line.startswith(f"{name} "):
            return float(line.split()[1])
    raise AssertionError(f"{name} not found in metrics body")


def test_counter_and_gauge_and_histogram_render_valid_prometheus_syntax():
    m = Metrics()
    m.messages_fanned_out_total.inc(3)
    m.ws_connections_active.set(2)
    m.fanout_latency_seconds.observe(0.002)
    m.fanout_latency_seconds.observe(0.3)
    text = m.render()

    assert "pair_messages_fanned_out_total 3.0" in text
    assert "pair_ws_connections_active 2" in text
    # Cumulative: an observation of 0.3s must count in every bucket >= 0.3,
    # and the smallest (0.0005s) bucket must NOT count either observation.
    assert 'pair_fanout_latency_seconds_bucket{le="0.0005"} 0' in text
    assert 'pair_fanout_latency_seconds_bucket{le="0.5"} 2' in text
    assert 'pair_fanout_latency_seconds_bucket{le="+Inf"} 2' in text
    assert "pair_fanout_latency_seconds_count 2" in text


def test_log_event_emits_one_json_line_with_event_name_and_fields(caplog):
    with caplog.at_level(logging.INFO, logger="pair.events"):
        log_event("test_event", match_id="abc123", outcome="win")

    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].message)
    assert payload["event"] == "test_event"
    assert payload["match_id"] == "abc123"
    assert payload["outcome"] == "win"
    assert "ts" in payload


def test_global_metrics_singleton_is_shared_across_imports():
    # Both app.main and app.room_manager import the same METRICS instance -
    # a regression here (e.g. two separate Metrics() objects) would mean
    # /metrics silently stops reflecting what the room managers record.
    from app.room_manager import METRICS as room_manager_metrics
    assert room_manager_metrics is METRICS
