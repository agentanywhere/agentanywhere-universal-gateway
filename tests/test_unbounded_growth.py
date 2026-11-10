"""Bounded growth: tracer spans, metric path labels, rate-limiter memory."""
import time
from unittest.mock import patch

import pytest

from app import observability as obs
from app.middleware.rate_limit import RateLimiter


def test_spans_per_trace_are_capped():
    t = obs.Tracer(buffer_size=10)
    for _ in range(5000):
        with t.span("req", trace_id="a" * 32):
            with t.span("inner"):
                pass
    trace = t.get_trace("a" * 32)
    assert trace["span_count"] == obs.MAX_SPANS_PER_TRACE
    assert len(trace["spans"]) == obs.MAX_SPANS_PER_TRACE
    assert trace["dropped_spans"] == 10000 - obs.MAX_SPANS_PER_TRACE
    assert len(t._traces) == 1


def test_trace_below_cap_has_unchanged_shape():
    t = obs.Tracer(buffer_size=10)
    with t.span("x", trace_id="b" * 32):
        pass
    assert set(t.get_trace("b" * 32)) == {"trace_id", "started_at", "duration_ms", "span_count", "spans"}


@pytest.fixture
def fresh_labels():
    obs._metric_path_labels.clear()
    yield
    obs._metric_path_labels.clear()


def test_metric_label_known_routes_unchanged(fresh_labels):
    for p in ["/", "/health", "/api/users", "/api/v1/users", "/mcp/tools", "/metrics"]:
        assert obs.metric_path_label(p) == p


def test_metric_label_ids_collapse(fresh_labels):
    labels = {obs.metric_path_label(f"/api/users/{i}/orders") for i in range(1000)}
    assert labels == {"/api/users/:id/orders"}
    assert obs.metric_path_label("/api/items/123e4567-e89b-42d3-a456-426614174000") == "/api/items/:id"
    assert obs.metric_path_label("/a/deadbeefcafe1234") == "/a/:id"
    assert obs.metric_path_label("/a/b/c/d/e/f") == "/a/b/c/d/..."


def test_metric_label_overflow_bucket(fresh_labels):
    out = {obs.metric_path_label(f"/scan{'x' * (i % 7)}/{chr(97 + i % 26)}{chr(97 + i // 26 % 26)}{chr(97 + i // 676)}") for i in range(3000)}
    assert "other" in out
    assert len(out) <= obs.METRIC_PATH_MAX_LABELS + 1
    # an already-seen label keeps working after overflow
    first = next(iter(obs._metric_path_labels))
    assert obs.metric_path_label(first) == first


def test_metrics_endpoint_still_prometheus_text():
    from fastapi.testclient import TestClient
    from app import main

    with patch.object(main, "_require_ops_admin", lambda r: None), \
            patch.object(main.settings, "metrics_enabled", True):
        r = TestClient(main.app).get(main.settings.metrics_path)
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "# HELP" in r.text


def test_rate_limiter_prunes_expired_keeps_active():
    rl = RateLimiter()
    rl.redis_client = None
    with patch("app.middleware.rate_limit.time.time", return_value=1000.0):
        for i in range(2000):
            assert rl._check_limit_memory(f"old{i}", 5, 60)
        assert rl._check_limit_memory("live", 3, 600)
    with patch("app.middleware.rate_limit.time.time", return_value=1100.0):
        assert rl._check_limit_memory("live", 3, 600)
        assert len(rl._memory_counters) == 1
        assert rl._check_limit_memory("live", 3, 600)
        assert not rl._check_limit_memory("live", 3, 600)  # limit unchanged
        # an expired key restarts its window as before
        assert rl._check_limit_memory("old1", 5, 60)


def test_rate_limiter_limits_unchanged_without_sweep():
    rl = RateLimiter()
    for _ in range(5):
        assert rl._check_limit_memory("k", 5, 60)
    assert not rl._check_limit_memory("k", 5, 60)
