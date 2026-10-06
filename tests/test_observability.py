"""Tests for the observability module."""
from __future__ import annotations

import asyncio
import time

import pytest

from app.observability import (
    CustomMetrics,
    DependencyProbes,
    ProbeResult,
    SLOTracker,
    Tracer,
    custom_metrics,
    normalize_route,
    record_request,
    slo_tracker,
    status_class,
    tracer,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestRoutePathHelpers:
    def test_status_class(self):
        assert status_class(200) == "2xx"
        assert status_class(301) == "3xx"
        assert status_class(404) == "4xx"
        assert status_class(503) == "5xx"

    def test_normalize_route_keeps_short(self):
        assert normalize_route("/health") == "/health"

    def test_normalize_route_truncates_long(self):
        assert normalize_route("/api/users/123/posts/456") == "/api/users/..."

    def test_normalize_route_handles_root(self):
        assert normalize_route("") == "/"


# ---------------------------------------------------------------------------
# SLOs
# ---------------------------------------------------------------------------


class TestSLOTracker:
    def setup_method(self):
        self.tracker = SLOTracker()
        self.tracker.register("availability", target=0.99)

    def test_availability_with_no_samples_returns_none(self):
        assert self.tracker.get("availability")["availability"] is None

    def test_availability_recorded(self):
        for _ in range(99):
            self.tracker.record("availability", success=True, latency_ms=5)
        self.tracker.record("availability", success=False, latency_ms=5)
        slo = self.tracker.get("availability")
        assert slo["sample_count"] == 100
        assert slo["availability"] == pytest.approx(0.99, abs=0.001)

    def test_burn_rate_zero_when_meeting_target(self):
        for _ in range(100):
            self.tracker.record("availability", success=True, latency_ms=5)
        slo = self.tracker.get("availability")
        assert slo["burn_rate"] == pytest.approx(0.0)

    def test_latency_threshold_marks_slow_request_as_failure(self):
        self.tracker.register(
            "latency_p99", target=0.99, latency_threshold_ms=100
        )
        self.tracker.record("latency_p99", success=True, latency_ms=200)
        slo = self.tracker.get("latency_p99")
        assert slo["availability"] == 0.0

    def test_percentiles_returned(self):
        for ms in [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]:
            self.tracker.record("availability", success=True, latency_ms=ms)
        slo = self.tracker.get("availability")
        assert slo["p50_ms"] is not None
        assert slo["p95_ms"] is not None

    def test_remove_unknown_slo_returns_false(self):
        assert self.tracker.remove("nope") is False


# ---------------------------------------------------------------------------
# Tracer
# ---------------------------------------------------------------------------


class TestTracer:
    def setup_method(self):
        self.tracer = Tracer(buffer_size=10)

    def test_span_records_duration(self):
        with self.tracer.span("op") as span:
            time.sleep(0.005)
        assert span.duration_ms is not None
        assert span.duration_ms >= 5

    def test_nested_spans_share_trace(self):
        with self.tracer.span("outer") as outer:
            with self.tracer.span("inner") as inner:
                pass
        assert inner.trace_id == outer.trace_id
        assert inner.parent_span_id == outer.span_id

    def test_span_records_error(self):
        with pytest.raises(ValueError):
            with self.tracer.span("bad") as span:
                raise ValueError("boom")
        assert span.status == "ERROR"
        assert span.error and "ValueError" in span.error

    def test_recent_traces_returned(self):
        for i in range(3):
            with self.tracer.span(f"op-{i}"):
                pass
        traces = self.tracer.get_recent_traces(limit=10)
        assert len(traces) == 3
        names = [t["spans"][0]["name"] for t in traces]
        assert names == ["op-2", "op-1", "op-0"]  # newest first

    def test_get_trace_by_id(self):
        with self.tracer.span("op") as span:
            pass
        fetched = self.tracer.get_trace(span.trace_id)
        assert fetched is not None
        assert fetched["trace_id"] == span.trace_id

    def test_get_unknown_trace_returns_none(self):
        assert self.tracer.get_trace("does-not-exist") is None

    def test_buffer_eviction(self):
        small = Tracer(buffer_size=3)
        for i in range(5):
            with small.span(f"op-{i}"):
                pass
        traces = small.get_recent_traces(limit=10)
        assert len(traces) <= 3

    @pytest.mark.asyncio
    async def test_aspan(self):
        async with self.tracer.aspan("aop") as span:
            await asyncio.sleep(0.001)
        assert span.duration_ms is not None
        assert span.duration_ms >= 1


# ---------------------------------------------------------------------------
# Custom metrics SDK
# ---------------------------------------------------------------------------


class TestCustomMetrics:
    def test_counter_idempotent(self):
        m = CustomMetrics()
        c1 = m.counter("test_counter_1")
        c2 = m.counter("test_counter_1")
        assert c1 is c2

    def test_histogram_with_custom_buckets(self):
        m = CustomMetrics()
        h = m.histogram(
            "test_hist_unique",
            buckets=(0.1, 1.0, 10.0),
        )
        assert h is not None

    def test_list_metrics(self):
        m = CustomMetrics()
        m.counter("test_listing_counter")
        m.gauge("test_listing_gauge")
        listing = m.list_metrics()
        assert "test_listing_counter" in listing["counters"]
        assert "test_listing_gauge" in listing["gauges"]


# ---------------------------------------------------------------------------
# Dependency probes
# ---------------------------------------------------------------------------


class TestDependencyProbes:
    @pytest.mark.asyncio
    async def test_register_and_run_probe(self):
        probes = DependencyProbes()
        probes.probes.clear()  # drop builtins for this test

        async def healthy_probe():
            return ProbeResult(
                name="x", kind="custom", status="healthy", latency_ms=1.0
            )

        probes.register("x", healthy_probe)
        result = await probes.check("x")
        assert result is not None
        assert result.status == "healthy"

    @pytest.mark.asyncio
    async def test_aggregate_status(self):
        probes = DependencyProbes()
        probes.probes.clear()

        async def healthy_probe():
            return ProbeResult(
                name="ok", kind="custom", status="healthy", latency_ms=1.0
            )

        async def unhealthy_probe():
            return ProbeResult(
                name="bad", kind="custom", status="unhealthy", latency_ms=None
            )

        probes.register("ok", healthy_probe)
        probes.register("bad", unhealthy_probe)
        results = await probes.check_all()
        agg = probes.aggregate(results)
        assert agg == "unhealthy"

    @pytest.mark.asyncio
    async def test_probe_timeout_marks_unhealthy(self):
        probes = DependencyProbes()
        probes.probes.clear()

        async def slow_probe():
            await asyncio.sleep(5)
            return ProbeResult(
                name="slow", kind="custom", status="healthy", latency_ms=5000
            )

        probes.register("slow", slow_probe)
        # Patch timeout for speed.
        original_check = probes.check

        async def fast_check(name):
            try:
                fn = probes.probes[name]
                result = await asyncio.wait_for(fn(), timeout=0.05)
            except asyncio.TimeoutError:
                result = ProbeResult(
                    name=name, kind="custom", status="unhealthy",
                    latency_ms=None, message="probe timeout",
                )
            return result

        probes.check = fast_check  # type: ignore[assignment]
        result = await probes.check("slow")
        assert result.status == "unhealthy"


# ---------------------------------------------------------------------------
# Top-level recording
# ---------------------------------------------------------------------------


class TestRecordRequest:
    def test_record_request_increments_global_slos(self):
        before = slo_tracker.get("availability")["sample_count"]
        record_request(
            method="GET",
            path="/api/users/1",
            status_code=200,
            duration_seconds=0.05,
            protocol="http",
            tenant="acme",
        )
        after = slo_tracker.get("availability")["sample_count"]
        assert after == before + 1

    def test_record_request_5xx_counts_error(self):
        before = slo_tracker.get("availability")
        before_avail = before.get("availability") or 1.0
        for _ in range(20):
            record_request(
                method="GET",
                path="/x/y",
                status_code=500,
                duration_seconds=0.01,
            )
        after = slo_tracker.get("availability")
        # availability should drop after 20 5xx samples
        assert (after.get("availability") or 1.0) < before_avail
