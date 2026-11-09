"""Observability for AgentAnywhere Universal Gateway.

Single module covering the four pillars of production observability:

* **Metrics.** Rate / Errors / Duration histograms (Prometheus) with
  consistent labels (``method``, ``route``, ``status_class``, ``protocol``,
  ``tenant``). A small custom-metrics SDK lets plugins record their own
  counters / gauges / histograms.

* **Tracing.** A lightweight in-process tracer that records spans for
  request → auth → policy → transform → backend, keeps a ring buffer
  of recent traces visible at ``/admin/observability/traces``, and can
  optionally forward spans to an OpenTelemetry collector via OTLP-HTTP.

* **SLOs.** A rolling-window SLO tracker (availability + latency) with
  error-budget burn-rate, per-route or per-tenant.

* **Dependency health.** Async probes for the database, Redis, backend
  services, log exporters, and any plugin-registered probe. Aggregated
  into ``GET /health/detailed`` and ``GET /admin/observability/dependencies``.

The module is intentionally dependency-light: it only requires
``prometheus_client`` and ``httpx`` (already used elsewhere). OpenTelemetry
export is via raw OTLP/HTTP so we don't pull in the entire OTEL SDK
unless it's installed.
"""
from __future__ import annotations

import asyncio
import math
import os
import statistics
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import (
    Any,
    Awaitable,
    Callable,
    Deque,
    Dict,
    Iterable,
    List,
    Optional,
    Tuple,
)

import httpx
from prometheus_client import Counter, Gauge, Histogram

import structlog

logger = structlog.get_logger("observability")


# ---------------------------------------------------------------------------
# RED metrics (high-cardinality-safe)
# ---------------------------------------------------------------------------
# We expose Prometheus metrics with a fixed, low-cardinality label set.
# - ``status_class`` is the HTTP status class ("2xx", "3xx", …) — never the
#   raw status code, which would explode the cardinality.
# - ``route`` is the matched route template, not the exact URL.
#
# These coexist with ``app.metrics`` (which exposes the older,
# higher-cardinality counters used by some integration tests). Both are
# served on ``/metrics`` since they share the default registry.

red_request_count = Counter(
    "gateway_red_requests_total",
    "Total requests, labelled with low-cardinality dimensions for RED metrics.",
    ["method", "route", "status_class", "protocol", "tenant"],
)

red_request_duration = Histogram(
    "gateway_red_request_duration_seconds",
    "Request duration in seconds (RED metrics).",
    ["method", "route", "protocol", "tenant"],
    buckets=(
        0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5,
        1.0, 2.5, 5.0, 10.0,
    ),
)

red_error_count = Counter(
    "gateway_red_errors_total",
    "Total errored requests (5xx or exception).",
    ["method", "route", "protocol", "tenant", "error_class"],
)

dependency_up = Gauge(
    "gateway_dependency_up",
    "Dependency health: 1 healthy, 0 unhealthy, 0.5 degraded.",
    ["name", "kind"],
)

slo_burn_rate = Gauge(
    "gateway_slo_burn_rate",
    "SLO burn rate (1.0 = consuming budget at the target rate, 14.4 = exhausting in 1h for 30d budget).",
    ["slo"],
)

slo_availability = Gauge(
    "gateway_slo_availability",
    "Current rolling availability (0.0 - 1.0) for each registered SLO.",
    ["slo"],
)


# ---------------------------------------------------------------------------
# Custom metrics SDK
# ---------------------------------------------------------------------------


class CustomMetrics:
    """Tiny dimensional metrics SDK for plugins and application code.

    Metrics are created lazily on first use and registered against the
    default Prometheus collector registry — so they show up on
    ``/metrics`` automatically.
    """

    def __init__(self) -> None:
        self._counters: Dict[str, Counter] = {}
        self._gauges: Dict[str, Gauge] = {}
        self._histograms: Dict[str, Histogram] = {}

    def counter(
        self,
        name: str,
        description: str = "",
        labels: Optional[List[str]] = None,
    ) -> Counter:
        key = name
        if key not in self._counters:
            self._counters[key] = Counter(
                name, description or name, labels or []
            )
        return self._counters[key]

    def gauge(
        self,
        name: str,
        description: str = "",
        labels: Optional[List[str]] = None,
    ) -> Gauge:
        if name not in self._gauges:
            self._gauges[name] = Gauge(name, description or name, labels or [])
        return self._gauges[name]

    def histogram(
        self,
        name: str,
        description: str = "",
        labels: Optional[List[str]] = None,
        buckets: Optional[Tuple[float, ...]] = None,
    ) -> Histogram:
        if name not in self._histograms:
            kwargs: Dict[str, Any] = {}
            if buckets is not None:
                kwargs["buckets"] = buckets
            self._histograms[name] = Histogram(
                name, description or name, labels or [], **kwargs
            )
        return self._histograms[name]

    def list_metrics(self) -> Dict[str, List[str]]:
        return {
            "counters": list(self._counters),
            "gauges": list(self._gauges),
            "histograms": list(self._histograms),
        }


custom_metrics = CustomMetrics()


# ---------------------------------------------------------------------------
# Lightweight tracing
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """A single tracing span."""

    trace_id: str
    span_id: str
    name: str
    started_at: float
    ended_at: Optional[float] = None
    parent_span_id: Optional[str] = None
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "OK"
    error: Optional[str] = None

    @property
    def duration_ms(self) -> Optional[float]:
        if self.ended_at is None:
            return None
        return round((self.ended_at - self.started_at) * 1000, 3)

    def add_event(self, name: str, **attrs: Any) -> None:
        self.events.append(
            {"name": name, "timestamp": time.time(), "attributes": attrs}
        )

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_error(self, exc: BaseException) -> None:
        self.status = "ERROR"
        self.error = f"{type(exc).__name__}: {exc}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error": self.error,
            "attributes": self.attributes,
            "events": self.events,
        }


@dataclass
class Trace:
    trace_id: str
    spans: List[Span] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    @property
    def root(self) -> Optional[Span]:
        return next((s for s in self.spans if s.parent_span_id is None), None)

    @property
    def duration_ms(self) -> Optional[float]:
        if not self.spans:
            return None
        end = max((s.ended_at or s.started_at for s in self.spans), default=None)
        if end is None:
            return None
        return round((end - self.started_at) * 1000, 3)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "span_count": len(self.spans),
            "spans": [s.to_dict() for s in self.spans],
        }


_current_span_var: ContextVar[Optional[Span]] = ContextVar("current_span", default=None)


class Tracer:
    """In-process tracer with optional OTLP/HTTP export.

    Stores up to ``buffer_size`` recent traces in a ring buffer for the
    admin UI / API. Set ``otlp_endpoint`` and ``enabled=True`` to also
    forward spans to an OpenTelemetry collector.
    """

    def __init__(self, buffer_size: int = 200) -> None:
        self.buffer_size = buffer_size
        self._traces: Dict[str, Trace] = {}
        self._order: Deque[str] = deque(maxlen=buffer_size)
        self.enabled: bool = False
        self.otlp_endpoint: Optional[str] = None
        self.otlp_headers: Dict[str, str] = {}
        self.service_name: str = "agentanywhere-gateway"

    # ----- configuration --------------------------------------------------

    def configure(
        self,
        *,
        enabled: Optional[bool] = None,
        otlp_endpoint: Optional[str] = None,
        otlp_headers: Optional[Dict[str, str]] = None,
        service_name: Optional[str] = None,
        buffer_size: Optional[int] = None,
    ) -> None:
        if enabled is not None:
            self.enabled = bool(enabled)
        if otlp_endpoint is not None:
            self.otlp_endpoint = otlp_endpoint or None
        if otlp_headers is not None:
            self.otlp_headers = dict(otlp_headers)
        if service_name is not None:
            self.service_name = service_name
        if buffer_size is not None and buffer_size != self.buffer_size:
            self.buffer_size = buffer_size
            new_q: Deque[str] = deque(self._order, maxlen=buffer_size)
            self._order = new_q

    def describe(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "otlp_endpoint": self.otlp_endpoint,
            "service_name": self.service_name,
            "buffer_size": self.buffer_size,
            "trace_count": len(self._traces),
        }

    # ----- spans ----------------------------------------------------------

    def _get_current_span(self) -> Optional[Span]:
        return _current_span_var.get()

    def start_span(
        self,
        name: str,
        *,
        trace_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Span:
        parent = self._get_current_span()
        if not trace_id:
            trace_id = parent.trace_id if parent else uuid.uuid4().hex
        span = Span(
            trace_id=trace_id,
            span_id=uuid.uuid4().hex[:16],
            name=name,
            started_at=time.time(),
            parent_span_id=parent.span_id if parent else None,
            attributes=dict(attributes or {}),
        )
        trace = self._traces.get(trace_id)
        if not trace:
            trace = Trace(trace_id=trace_id, started_at=span.started_at)
            self._traces[trace_id] = trace
            self._order.append(trace_id)
            # Evict trace at the head if the deque was full.
            if len(self._traces) > self.buffer_size:
                evict_id = next(iter(self._traces))
                if evict_id != trace_id:
                    self._traces.pop(evict_id, None)
        trace.spans.append(span)
        return span

    def end_span(self, span: Span) -> None:
        if span.ended_at is None:
            span.ended_at = time.time()
        if self.enabled and self.otlp_endpoint:
            asyncio.get_event_loop().call_soon_threadsafe(
                lambda: asyncio.ensure_future(self._export_span(span))
            ) if asyncio.get_event_loop().is_running() else None  # noqa: E501

    @contextmanager
    def span(
        self,
        name: str,
        *,
        trace_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ):
        span = self.start_span(name, trace_id=trace_id, attributes=attributes)
        token = _current_span_var.set(span)
        try:
            yield span
        except BaseException as exc:
            span.set_error(exc)
            raise
        finally:
            self.end_span(span)
            _current_span_var.reset(token)

    @asynccontextmanager
    async def aspan(
        self,
        name: str,
        *,
        trace_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ):
        span = self.start_span(name, trace_id=trace_id, attributes=attributes)
        token = _current_span_var.set(span)
        try:
            yield span
        except BaseException as exc:
            span.set_error(exc)
            raise
        finally:
            self.end_span(span)
            _current_span_var.reset(token)

    # ----- read API -------------------------------------------------------

    def get_recent_traces(self, limit: int = 50) -> List[Dict[str, Any]]:
        ids = list(self._order)[-limit:]
        return [self._traces[tid].to_dict() for tid in reversed(ids) if tid in self._traces]

    def get_trace(self, trace_id: str) -> Optional[Dict[str, Any]]:
        trace = self._traces.get(trace_id)
        return trace.to_dict() if trace else None

    def clear(self) -> None:
        self._traces.clear()
        self._order.clear()

    # ----- OTLP export ----------------------------------------------------

    async def _export_span(self, span: Span) -> None:
        if not self.enabled or not self.otlp_endpoint:
            return
        # OTLP/HTTP JSON envelope (subset — collectors are lenient).
        payload = {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {
                                "key": "service.name",
                                "value": {"stringValue": self.service_name},
                            }
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "agentanywhere-gateway"},
                            "spans": [
                                {
                                    "traceId": span.trace_id,
                                    "spanId": span.span_id,
                                    "parentSpanId": span.parent_span_id or "",
                                    "name": span.name,
                                    "startTimeUnixNano": int(span.started_at * 1e9),
                                    "endTimeUnixNano": int(
                                        (span.ended_at or span.started_at) * 1e9
                                    ),
                                    "attributes": [
                                        {
                                            "key": k,
                                            "value": {"stringValue": str(v)},
                                        }
                                        for k, v in span.attributes.items()
                                    ],
                                    "status": {
                                        "code": 2 if span.status == "ERROR" else 1,
                                        "message": span.error or "",
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    self.otlp_endpoint,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        **self.otlp_headers,
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("otlp_export_failed", error=str(exc))


tracer = Tracer()


# ---------------------------------------------------------------------------
# SLO tracker
# ---------------------------------------------------------------------------


@dataclass
class SLOSample:
    timestamp: float
    success: bool
    latency_ms: float


@dataclass
class SLO:
    """A Service Level Objective (availability + latency)."""

    name: str
    target: float = 0.999  # e.g. 99.9% available
    latency_threshold_ms: Optional[float] = None  # success := latency <= threshold
    rolling_window_seconds: int = 3600  # 1h sliding window for live burn rate
    budget_window_seconds: int = 30 * 24 * 3600  # 30d budget window
    samples: Deque[SLOSample] = field(default_factory=lambda: deque(maxlen=100_000))
    # One-second buckets [second, total, good] covering the rolling window, with running
    # sums, so the live availability and burn rate cost O(1) per request instead of a
    # scan of every stored sample.
    _buckets: Deque[List[float]] = field(default_factory=deque, repr=False)
    _win_total: int = field(default=0, repr=False)
    _win_good: int = field(default=0, repr=False)

    def record(self, success: bool, latency_ms: float) -> None:
        # Combine HTTP success with latency-target success when configured.
        if self.latency_threshold_ms is not None and latency_ms > self.latency_threshold_ms:
            success = False
        now = time.time()
        sample = SLOSample(timestamp=now, success=success, latency_ms=latency_ms)
        self.samples.append(sample)
        second = int(now)
        if self._buckets and self._buckets[-1][0] == second:
            bucket = self._buckets[-1]
        else:
            bucket = [second, 0, 0]
            self._buckets.append(bucket)
        bucket[1] += 1
        self._win_total += 1
        if success:
            bucket[2] += 1
            self._win_good += 1
        self._expire(now)

    def _expire(self, now: float) -> None:
        cutoff = int(now - self.rolling_window_seconds)
        while self._buckets and self._buckets[0][0] < cutoff:
            _, total, good = self._buckets.popleft()
            self._win_total -= total
            self._win_good -= good

    def _recent(self, window_seconds: int) -> List[SLOSample]:
        cutoff = time.time() - window_seconds
        # samples are time-ordered (append-only), so we can pop from the
        # left until the cutoff. But we must NOT mutate during reads.
        return [s for s in self.samples if s.timestamp >= cutoff]

    def availability(self, window_seconds: Optional[int] = None) -> Optional[float]:
        window = window_seconds or self.rolling_window_seconds
        if window == self.rolling_window_seconds:
            self._expire(time.time())
            return (self._win_good / self._win_total) if self._win_total else None
        recent = self._recent(window)
        if not recent:
            return None
        successes = sum(1 for s in recent if s.success)
        return successes / len(recent)

    def latency_percentile(
        self,
        p: float,
        window_seconds: Optional[int] = None,
    ) -> Optional[float]:
        window = window_seconds or self.rolling_window_seconds
        recent = sorted(s.latency_ms for s in self._recent(window))
        if not recent:
            return None
        if p <= 0:
            return recent[0]
        if p >= 1:
            return recent[-1]
        idx = min(len(recent) - 1, int(math.ceil(p * len(recent)) - 1))
        return recent[idx]

    def burn_rate(self) -> Optional[float]:
        """Return the live error-budget burn rate.

        Burn rate of ``1.0`` means the budget will be consumed exactly in
        ``budget_window_seconds``; ``14.4`` means it will be consumed in
        1 hour for a 30-day budget (a common short-window page threshold).
        """
        avail = self.availability()
        if avail is None:
            return None
        target_failure = 1.0 - self.target
        actual_failure = 1.0 - avail
        if target_failure <= 0:
            return float("inf") if actual_failure > 0 else 0.0
        return actual_failure / target_failure

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "target": self.target,
            "latency_threshold_ms": self.latency_threshold_ms,
            "rolling_window_seconds": self.rolling_window_seconds,
            "budget_window_seconds": self.budget_window_seconds,
            "sample_count": len(self.samples),
            "availability": self.availability(),
            "burn_rate": self.burn_rate(),
            "p50_ms": self.latency_percentile(0.5),
            "p95_ms": self.latency_percentile(0.95),
            "p99_ms": self.latency_percentile(0.99),
        }


class SLOTracker:
    """Manages a set of SLOs."""

    def __init__(self) -> None:
        self.slos: Dict[str, SLO] = {}

    def register(
        self,
        name: str,
        *,
        target: float = 0.999,
        latency_threshold_ms: Optional[float] = None,
        rolling_window_seconds: int = 3600,
        budget_window_seconds: int = 30 * 24 * 3600,
    ) -> SLO:
        slo = SLO(
            name=name,
            target=target,
            latency_threshold_ms=latency_threshold_ms,
            rolling_window_seconds=rolling_window_seconds,
            budget_window_seconds=budget_window_seconds,
        )
        self.slos[name] = slo
        logger.info("slo_registered", name=name, target=target)
        return slo

    def remove(self, name: str) -> bool:
        return self.slos.pop(name, None) is not None

    def record(self, name: str, success: bool, latency_ms: float) -> None:
        slo = self.slos.get(name)
        if slo:
            slo.record(success, latency_ms)
            avail = slo.availability()
            burn = slo.burn_rate()
            if avail is not None:
                slo_availability.labels(slo=name).set(avail)
            if burn is not None and not math.isinf(burn):
                slo_burn_rate.labels(slo=name).set(burn)

    def record_global(self, success: bool, latency_ms: float) -> None:
        """Record against every registered SLO."""
        for name in list(self.slos.keys()):
            self.record(name, success, latency_ms)

    def list_slos(self) -> List[Dict[str, Any]]:
        return [slo.to_dict() for slo in self.slos.values()]

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        slo = self.slos.get(name)
        return slo.to_dict() if slo else None


slo_tracker = SLOTracker()
# Sensible defaults — admins can override / add more via the API.
slo_tracker.register("availability", target=0.999)
slo_tracker.register("latency_p99", target=0.99, latency_threshold_ms=500.0)


# ---------------------------------------------------------------------------
# Dependency probes
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    name: str
    kind: str  # "database" | "cache" | "backend" | "exporter" | "custom"
    status: str  # "healthy" | "degraded" | "unhealthy"
    latency_ms: Optional[float]
    message: Optional[str] = None
    last_checked: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "message": self.message,
            "last_checked": datetime.fromtimestamp(self.last_checked, timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        }


ProbeFn = Callable[[], Awaitable[ProbeResult]]


class DependencyProbes:
    """Registry of async health probes.

    Built-ins probe the database, Redis (if enabled), backend services
    (if configured), and any registered log exporter. Plugins / app
    code can add their own probes via :meth:`register`.
    """

    def __init__(self) -> None:
        self.probes: Dict[str, ProbeFn] = {}
        self._last_results: Dict[str, ProbeResult] = {}
        self._register_builtins()

    # ----- registration ---------------------------------------------------

    def register(self, name: str, fn: ProbeFn) -> None:
        self.probes[name] = fn

    def remove(self, name: str) -> None:
        self.probes.pop(name, None)
        self._last_results.pop(name, None)

    def list_probes(self) -> List[str]:
        return list(self.probes.keys())

    # ----- execution ------------------------------------------------------

    async def check(self, name: str) -> Optional[ProbeResult]:
        fn = self.probes.get(name)
        if not fn:
            return None
        try:
            result = await asyncio.wait_for(fn(), timeout=3.0)
        except asyncio.TimeoutError:
            result = ProbeResult(
                name=name, kind="custom", status="unhealthy",
                latency_ms=None, message="probe timeout (>3s)",
            )
        except Exception as exc:  # noqa: BLE001
            result = ProbeResult(
                name=name, kind="custom", status="unhealthy",
                latency_ms=None, message=str(exc),
            )
        self._last_results[name] = result
        dependency_up.labels(name=name, kind=result.kind).set(
            {"healthy": 1.0, "degraded": 0.5, "unhealthy": 0.0}.get(result.status, 0.0)
        )
        return result

    async def check_all(self) -> List[ProbeResult]:
        results = await asyncio.gather(
            *(self.check(name) for name in list(self.probes.keys())),
            return_exceptions=False,
        )
        return [r for r in results if r is not None]

    def aggregate(self, results: Iterable[ProbeResult]) -> str:
        """Aggregate per-probe status into a single overall status."""
        statuses = [r.status for r in results]
        if any(s == "unhealthy" for s in statuses):
            return "unhealthy"
        if any(s == "degraded" for s in statuses):
            return "degraded"
        if statuses:
            return "healthy"
        return "unknown"

    # ----- built-ins ------------------------------------------------------

    def _register_builtins(self) -> None:
        self.register("database", self._probe_database)
        self.register("redis", self._probe_redis)
        self.register("backends", self._probe_backends)
        self.register("log_exporters", self._probe_log_exporters)

    async def _probe_database(self) -> ProbeResult:
        start = time.perf_counter()
        try:
            from app.database import AsyncSessionLocal
            from sqlalchemy import text

            async with AsyncSessionLocal() as session:
                await session.execute(text("SELECT 1"))
            elapsed = (time.perf_counter() - start) * 1000
            status = "healthy" if elapsed < 250 else "degraded"
            return ProbeResult(
                name="database", kind="database", status=status,
                latency_ms=round(elapsed, 2),
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                name="database", kind="database", status="unhealthy",
                latency_ms=None, message=str(exc),
            )

    async def _probe_redis(self) -> ProbeResult:
        try:
            from app.config import get_settings
            settings = get_settings()
            if not getattr(settings, "redis_enabled", False):
                return ProbeResult(
                    name="redis", kind="cache", status="healthy",
                    latency_ms=None, message="disabled",
                )
            import redis  # noqa: WPS433
            client = redis.from_url(settings.redis_url, socket_timeout=2.0)
            start = time.perf_counter()
            client.ping()
            elapsed = (time.perf_counter() - start) * 1000
            return ProbeResult(
                name="redis", kind="cache",
                status="healthy" if elapsed < 200 else "degraded",
                latency_ms=round(elapsed, 2),
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                name="redis", kind="cache", status="degraded",
                latency_ms=None, message=str(exc),
            )

    async def _probe_backends(self) -> ProbeResult:
        from app.config import get_settings

        settings = get_settings()
        urls = [
            ("mcp", settings.mcp_backend_url),
            ("a2a", settings.a2a_backend_url),
            ("http", settings.http_backend_url),
        ]
        urls = [(name, url) for name, url in urls if url]
        if not urls:
            return ProbeResult(
                name="backends", kind="backend", status="healthy",
                latency_ms=None, message="none configured",
            )
        started = time.perf_counter()
        good = 0
        async with httpx.AsyncClient(timeout=2.0) as client:
            for name, url in urls:
                try:
                    response = await client.get(url.rstrip("/") + "/health")
                    if response.status_code < 500:
                        good += 1
                except Exception:
                    pass
        elapsed = (time.perf_counter() - started) * 1000
        if good == len(urls):
            status = "healthy"
        elif good > 0:
            status = "degraded"
        else:
            status = "unhealthy"
        return ProbeResult(
            name="backends", kind="backend", status=status,
            latency_ms=round(elapsed, 2),
            message=f"{good}/{len(urls)} reachable",
        )

    async def _probe_log_exporters(self) -> ProbeResult:
        try:
            from app.log_exporters import log_export_manager
            if not log_export_manager.exporters:
                return ProbeResult(
                    name="log_exporters", kind="exporter", status="healthy",
                    latency_ms=None, message="none configured",
                )
            results = await log_export_manager.test_all_connections()
            ok = sum(1 for v in results.values() if v)
            total = len(results)
            if ok == total:
                return ProbeResult(
                    name="log_exporters", kind="exporter", status="healthy",
                    latency_ms=None, message=f"{ok}/{total} reachable",
                )
            status = "degraded" if ok > 0 else "unhealthy"
            return ProbeResult(
                name="log_exporters", kind="exporter", status=status,
                latency_ms=None, message=f"{ok}/{total} reachable",
            )
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                name="log_exporters", kind="exporter", status="degraded",
                latency_ms=None, message=str(exc),
            )


dependency_probes = DependencyProbes()


# ---------------------------------------------------------------------------
# Top-level recording helpers
# ---------------------------------------------------------------------------


def status_class(status_code: int) -> str:
    return f"{status_code // 100}xx"


def normalize_route(path: str) -> str:
    """Reduce a request path to a low-cardinality route template.

    We keep the first two path segments (``/api/users``) and replace the
    rest with ``/...`` to avoid label explosions on URLs with embedded
    ids or filter parameters.
    """
    if not path:
        return "/"
    parts = path.split("/")
    kept = [p for p in parts[:3]]
    if len(parts) > 3:
        kept.append("...")
    return "/".join(kept) or "/"


def record_request(
    *,
    method: str,
    path: str,
    status_code: int,
    duration_seconds: float,
    protocol: str = "http",
    tenant: Optional[str] = None,
    error: Optional[BaseException] = None,
) -> None:
    """Record a request against the RED metrics + SLOs.

    Called from the request middleware (`app.main`); also safe to call
    from plugin code.
    """
    route = normalize_route(path)
    tenant_label = tenant or "_"
    cls = status_class(status_code)
    red_request_count.labels(
        method=method, route=route, status_class=cls,
        protocol=protocol, tenant=tenant_label,
    ).inc()
    red_request_duration.labels(
        method=method, route=route, protocol=protocol, tenant=tenant_label,
    ).observe(duration_seconds)
    if status_code >= 500 or error is not None:
        red_error_count.labels(
            method=method, route=route, protocol=protocol,
            tenant=tenant_label,
            error_class=type(error).__name__ if error else f"http_{status_code}",
        ).inc()
    success = status_code < 500 and error is None
    slo_tracker.record_global(success=success, latency_ms=duration_seconds * 1000.0)


# ---------------------------------------------------------------------------
# Settings application
# ---------------------------------------------------------------------------


def _env_with_settings_fallback() -> Dict[str, str]:
    """The OTEL_* values from the process environment, falling back to the
    gateway's Settings (which also reads ``.env``).

    Reading ``os.environ`` alone meant values written in ``.env`` were ignored
    whenever uvicorn was started directly, because nothing exports ``.env``
    into the environment.
    """
    merged: Dict[str, str] = {}
    try:
        from app.config import get_settings

        s = get_settings()
        merged = {
            "OTEL_TRACING_ENABLED": "true" if s.otel_tracing_enabled else "false",
            "OTEL_EXPORTER_OTLP_ENDPOINT": s.otel_exporter_otlp_endpoint or "",
            "OTEL_EXPORTER_OTLP_HEADERS": s.otel_exporter_otlp_headers or "",
            "OTEL_SERVICE_NAME": s.otel_service_name or "",
        }
    except Exception:  # noqa: BLE001 - settings are optional here
        merged = {}
    for name in (
        "OTEL_TRACING_ENABLED",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_SERVICE_NAME",
    ):
        if name in os.environ:
            merged[name] = os.environ[name]
    return merged


def configure_from_env(env: Optional[Dict[str, str]] = None) -> None:
    """Configure the tracer from OTEL_* settings (idempotent).

    With no ``env`` given, the process environment wins and ``.env`` (through
    Settings) fills in the rest.
    """
    env = env if env is not None else _env_with_settings_fallback()
    enabled = env.get("OTEL_TRACING_ENABLED", "false").lower() in {"1", "true", "yes"}
    endpoint = env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None
    headers_raw = env.get("OTEL_EXPORTER_OTLP_HEADERS") or ""
    headers = {}
    for pair in headers_raw.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            headers[k.strip()] = v.strip()
    service = env.get("OTEL_SERVICE_NAME") or "agentanywhere-gateway"
    tracer.configure(
        enabled=enabled,
        otlp_endpoint=endpoint,
        otlp_headers=headers,
        service_name=service,
    )


configure_from_env()


__all__ = [
    "CustomMetrics",
    "DependencyProbes",
    "ProbeResult",
    "SLO",
    "SLOTracker",
    "Span",
    "Trace",
    "Tracer",
    "configure_from_env",
    "custom_metrics",
    "dependency_probes",
    "normalize_route",
    "record_request",
    "slo_tracker",
    "status_class",
    "tracer",
]
