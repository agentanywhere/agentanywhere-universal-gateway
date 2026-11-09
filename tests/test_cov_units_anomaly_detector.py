"""Unit tests for app.ai.anomaly_detector (real sklearn, tiny deterministic data)."""
from datetime import timedelta
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.ai.anomaly_detector import AnomalyDetector
from app.time_utils import utcnow


def _req(i=0, **kw):
    d = {"response_time_ms": 100 + (i % 5), "status_code": 200, "request_size": 500,
         "path": "/ok", "timestamp": utcnow()}
    d.update(kw)
    return d


def _filled(n=20, window=20):
    det = AnomalyDetector(window_size=window)
    for i in range(n):
        det.add_request(_req(i))
    return det


def test_extract_features_defaults_and_values():
    det = AnomalyDetector()
    f = det._extract_features({})
    assert f.shape == (1, 7) and f.tolist() == [[0, 200, 0, 0, 0, 0, 0]]
    f = det._extract_features({"response_time_ms": 5, "error_rate": 0.5})
    assert f[0][0] == 5 and f[0][6] == 0.5


def test_derived_features():
    det = AnomalyDetector()
    assert det._calculate_derived_features([])["requests_per_minute"] == 0
    now = utcnow()
    reqs = [
        {"timestamp": now, "status_code": 500, "response_time_ms": 100},
        {"timestamp": now, "status_code": 200, "response_time_ms": 300},
        {"timestamp": now - timedelta(minutes=5), "status_code": 500, "response_time_ms": 9999},
        {"status_code": 500},  # no timestamp -> ignored
    ]
    d = det._calculate_derived_features(reqs)
    assert d == {"requests_per_minute": 2, "error_rate": 0.5, "avg_response_time": 200}
    old = det._calculate_derived_features([{"timestamp": now - timedelta(hours=1)}])
    assert old == {"requests_per_minute": 0, "error_rate": 0, "avg_response_time": 0}


def test_add_request_enriches_and_parses_iso():
    det = AnomalyDetector(window_size=5)
    det.add_request({"timestamp": "2024-01-03T10:00:00", "path": "/x"})
    stored = det.request_history[-1]
    assert stored["hour_of_day"] == 10 and stored["day_of_week"] == 2  # Wednesday
    assert len(det.feature_history) == 1
    det.add_request({"path": "/no-ts"})  # defaults timestamp to now
    assert det.request_history[-1]["timestamp"] is not None
    for _ in range(20):
        det.add_request({})
    assert len(det.feature_history) == 5 and len(det.request_history) == 10  # bounded deques


def test_add_request_bad_timestamp_raises():
    with pytest.raises(ValueError):
        AnomalyDetector().add_request({"timestamp": "yesterday-ish"})


def test_train_insufficient_then_success():
    det = AnomalyDetector(window_size=10)
    for i in range(5):
        det.add_request(_req(i))
    assert det.train() is False and det.is_trained is False
    for i in range(10):
        det.add_request(_req(i))
    assert det.train() is True and det.is_trained is True


def test_detect_untrained_paths():
    det = AnomalyDetector(window_size=10)
    assert det.detect_anomaly(_req()) == (False, 0.0)  # too little data, no auto-train


def test_detect_auto_trains_and_flags_outlier():
    det = _filled(30, window=30)
    assert det.is_trained is False
    normal = det.detect_anomaly(_req(1))
    assert det.is_trained is True
    assert normal[0] in (True, False) and -1.0 <= normal[1] <= 1.0
    outlier = det.detect_anomaly({"response_time_ms": 10_000_000, "status_code": 599,
                                  "request_size": 10**9, "hour_of_day": 3, "day_of_week": 6,
                                  "requests_per_minute": 10**6, "error_rate": 1, "path": "/evil"})
    assert outlier[0] is True and 0 < outlier[1] <= 1.0


def test_detect_auto_train_failure_paths(monkeypatch):
    det = _filled(15, window=15)
    monkeypatch.setattr(det, "train", lambda: False)
    assert det.detect_anomaly(_req()) == (False, 0.0)
    monkeypatch.setattr(det, "train", MagicMock(side_effect=RuntimeError("fit failed")))
    assert det.detect_anomaly(_req()) == (False, 0.0)


def test_detect_inner_guards(monkeypatch):
    det = _filled(12, window=12)
    det.train()
    # model blows up -> swallowed
    monkeypatch.setattr(det.model, "predict", MagicMock(side_effect=RuntimeError("x")))
    assert det.detect_anomaly(_req()) == (False, 0.0)
    # empty predictions / scores
    monkeypatch.setattr(det.model, "predict", lambda X: np.array([]))
    monkeypatch.setattr(det.model, "score_samples", lambda X: np.array([0.1]))
    assert det.detect_anomaly(_req()) == (False, 0.0)
    monkeypatch.setattr(det.model, "predict", lambda X: np.array([1]))
    monkeypatch.setattr(det.model, "score_samples", lambda X: np.array([]))
    assert det.detect_anomaly(_req()) == (False, 0.0)
    # scaler "fitted" check
    det.scaler = MagicMock(spec=[])  # no mean_ attribute
    assert det.detect_anomaly(_req()) == (False, 0.0)
    det.scaler = MagicMock(mean_=None)
    assert det.detect_anomaly(_req()) == (False, 0.0)
    # invalid scaled features
    det.scaler = MagicMock(mean_=np.zeros(7), transform=lambda f: np.empty((0, 7)))
    assert det.detect_anomaly(_req()) == (False, 0.0)


def test_detect_score_normalised_and_clamped(monkeypatch):
    det = _filled(12, window=12)
    det.train()
    monkeypatch.setattr(det.model, "predict", lambda X: np.array([-1]))
    monkeypatch.setattr(det.model, "score_samples", lambda X: np.array([-50.0]))
    r = det.detect_anomaly(_req())
    assert r[0] is True and r[1] == 1.0 and type(r[1]) is float
    monkeypatch.setattr(det.model, "predict", lambda X: np.array([1]))
    monkeypatch.setattr(det.model, "score_samples", lambda X: np.array([50.0]))
    assert det.detect_anomaly(_req()) == (False, -1.0)


def test_anomaly_summary_empty():
    s = AnomalyDetector().get_anomaly_summary()
    assert s == {"total_requests": 0, "anomalies": 0, "anomaly_rate": 0.0, "top_anomalous_paths": []}
    det = AnomalyDetector()
    det.add_request(_req(timestamp=utcnow() - timedelta(hours=5)))
    assert det.get_anomaly_summary(window_minutes=60)["total_requests"] == 0


def test_anomaly_summary_groups_by_path(monkeypatch):
    det = AnomalyDetector(window_size=10)
    for i in range(6):
        det.add_request(_req(i, path="/a" if i < 4 else "/b"))
    flags = iter([(True, 0.9), (True, 0.8), (False, -0.5), (True, 0.7), (False, 0), (True, 0.6)])
    monkeypatch.setattr(det, "detect_anomaly", lambda r: next(flags))
    s = det.get_anomaly_summary()
    assert s["total_requests"] == 6 and s["anomalies"] == 4
    assert s["anomaly_rate"] == pytest.approx(4 / 6)
    assert s["top_anomalous_paths"][0] == {"path": "/a", "count": 3}
    assert {"path": "/b", "count": 1} in s["top_anomalous_paths"]
    assert len(s["recent_anomalies"]) == 4 and s["recent_anomalies"][0]["timestamp"]
