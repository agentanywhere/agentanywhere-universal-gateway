"""Performance smoke test against a real local stack. Run with::

    pytest -m performance tests/perf

It starts the sample backend and a gateway, so it is skipped in a normal run.
The limits are deliberately loose: they catch a broken or stalled gateway, not
a few milliseconds. Tighten them with PERF_MAX_P95_MS and PERF_MIN_RPS on a
dedicated runner. For real numbers use ``python -m perf.suite``.
"""
import os

import pytest

from perf.suite import run_suite

pytestmark = pytest.mark.performance

MAX_P95_MS = float(os.getenv("PERF_MAX_P95_MS", "10000"))
MIN_RPS = float(os.getenv("PERF_MIN_RPS", "1"))


@pytest.fixture(scope="module")
def run():
    out = run_suite(concurrency=8, duration=3.0, warmup=1.0)
    return {r.name: r for r in out["results"]}, out


def test_every_scenario_served_requests_without_errors(run):
    results, _ = run
    for name, r in results.items():
        assert r.requests > 0, name
        assert r.errors == 0, f"{name}: {r.status_counts}"


def test_proxied_requests_succeed_through_the_gateway(run):
    results, _ = run
    assert set(results["proxy GET /api/users"].status_counts) == {"200"}
    assert set(results["proxy POST /api/users"].status_counts) <= {"200", "201"}


def test_unauthenticated_requests_are_rejected_not_proxied(run):
    results, _ = run
    assert set(results["auth rejected"].status_counts) == {"401"}


def test_latency_and_throughput_stay_within_loose_limits(run):
    results, _ = run
    for name in ("gateway /health", "proxy GET /api/users", "proxy POST /api/users"):
        r = results[name]
        assert r.p95_ms <= MAX_P95_MS, f"{name}: p95 {r.p95_ms} ms"
        assert r.rps >= MIN_RPS, f"{name}: {r.rps} req/s"


def test_environment_is_recorded_for_reproducibility(run):
    _, out = run
    env = out["environment"]
    assert env["python"] and env["platform"] and env["cpu_count"] and env["concurrency"] == 8
