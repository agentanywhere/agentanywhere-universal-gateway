"""SLO availability and burn rate are kept as running sums, not recomputed from every sample."""
import time

import pytest

from app import observability as obs
from app.observability import SLO


def test_availability_matches_the_samples():
    slo = SLO(name="t", target=0.9)
    for ok in [True] * 8 + [False] * 2:
        slo.record(ok, 10.0)
    assert slo.availability() == pytest.approx(0.8)
    assert slo.burn_rate() == pytest.approx((1 - 0.8) / (1 - 0.9))


def test_no_samples_means_no_availability():
    assert SLO(name="t").availability() is None
    assert SLO(name="t").burn_rate() is None


def test_latency_threshold_counts_slow_requests_as_failures():
    slo = SLO(name="t", target=0.99, latency_threshold_ms=100.0)
    slo.record(True, 50.0)
    slo.record(True, 500.0)
    assert slo.availability() == pytest.approx(0.5)


def test_old_buckets_leave_the_rolling_window(monkeypatch):
    slo = SLO(name="t", rolling_window_seconds=60)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(obs.time, "time", lambda: clock["now"])
    for _ in range(10):
        slo.record(False, 1.0)
    clock["now"] += 30
    for _ in range(10):
        slo.record(True, 1.0)
    assert slo.availability() == pytest.approx(0.5)
    clock["now"] += 45                      # the failures are now 75 s old, the successes 45 s
    assert slo.availability() == pytest.approx(1.0)
    clock["now"] += 100                     # everything has expired
    assert slo.availability() is None
    assert len(slo._buckets) == 0 and slo._win_total == 0 and slo._win_good == 0


def test_other_windows_still_scan_the_samples(monkeypatch):
    slo = SLO(name="t", rolling_window_seconds=3600)
    clock = {"now": 2_000_000.0}
    monkeypatch.setattr(obs.time, "time", lambda: clock["now"])
    slo.record(False, 1.0)
    clock["now"] += 120
    slo.record(True, 1.0)
    assert slo.availability(window_seconds=60) == pytest.approx(1.0)
    assert slo.availability(window_seconds=3600) == pytest.approx(0.5)


def test_recording_cost_does_not_grow_with_the_number_of_samples():
    small, large = SLO(name="s"), SLO(name="l")
    for _ in range(50_000):
        large.record(True, 1.0)

    def cost(slo: SLO) -> float:
        start = time.perf_counter()
        for _ in range(2_000):
            slo.record(True, 1.0)
            slo.burn_rate()
        return time.perf_counter() - start

    # With a scan per request the loaded SLO is roughly 1000 times slower; allow a wide margin.
    assert cost(large) < cost(small) * 20 + 0.05


def test_tracker_record_updates_gauges_without_scanning():
    tracker = obs.SLOTracker()
    tracker.register("avail", target=0.999)
    for _ in range(100):
        tracker.record("avail", True, 5.0)
    tracker.record("avail", False, 5.0)
    info = tracker.get("avail")
    assert info["sample_count"] == 101
    assert info["availability"] == pytest.approx(100 / 101)
