"""AI_ANALYTICS_MODE=background: scoring runs in batches, off the request path."""
import asyncio
from datetime import timedelta

import pytest

from app.ai.anomaly_detector import AnomalyDetector
from app.analytics_queue import AnalyticsQueue, queue_from_settings
from app.config import Settings
from app.time_utils import utcnow


class OnlyPerRequest:
    def __init__(self):
        self.seen = []

    def process_request(self, data):
        self.seen.append(data)
        return {}


class Batched(OnlyPerRequest):
    def __init__(self):
        super().__init__()
        self.batches = []

    def process_batch(self, batch):
        self.batches.append(list(batch))


def test_drain_uses_process_batch_when_the_engine_has_one():
    engine = Batched()
    q = AnalyticsQueue(engine, batch_size=2)
    for i in range(5):
        q.submit({"i": i})
    assert q.drain() == 5
    assert [len(b) for b in engine.batches] == [2, 2, 1] and engine.seen == []
    assert q.processed == 5


def test_drain_falls_back_to_process_request_for_your_own_adapter():
    engine = OnlyPerRequest()
    q = AnalyticsQueue(engine)
    q.submit({"i": 1})
    q.submit({"i": 2})
    q.drain()
    assert [d["i"] for d in engine.seen] == [1, 2]


def test_a_full_queue_drops_the_oldest_and_counts_it():
    q = AnalyticsQueue(OnlyPerRequest(), queue_size=3)
    for i in range(5):
        q.submit({"i": i})
    assert [d["i"] for d in q.queue] == [2, 3, 4] and q.dropped == 2


def test_an_engine_failure_is_counted_and_does_not_raise():
    class Broken:
        def process_batch(self, batch):
            raise RuntimeError("model")

    q = AnalyticsQueue(Broken())
    q.submit({})
    q.drain()
    assert q.failed == 1 and q.processed == 0


async def test_the_background_task_drains_and_stop_flushes_the_rest():
    engine = Batched()
    q = AnalyticsQueue(engine, flush_interval=0.01)
    await q.start()
    q.submit({"i": 1})
    await asyncio.sleep(0.1)
    assert q.processed == 1
    q.submit({"i": 2})
    await q.stop()
    assert q.processed == 2


async def test_inline_mode_starts_no_task():
    q = AnalyticsQueue(OnlyPerRequest(), mode="inline")
    await q.start()
    assert q._task is None and q.inline


def test_defaults_and_unknown_modes():
    s = Settings(_env_file=None)
    assert (s.ai_analytics_mode, s.ai_analytics_batch_size, s.ai_analytics_queue_size) == ("background", 500, 10000)
    assert queue_from_settings(OnlyPerRequest(), s).mode == "background"
    assert AnalyticsQueue(OnlyPerRequest(), mode="nonsense").mode == "background"


def _request(i):
    return {"path": "/api/x", "status_code": 200 if i % 7 else 500, "response_time_ms": 5 + (i % 11) * 3,
            "timestamp": (utcnow() - timedelta(seconds=i)).isoformat()}


def test_batch_scoring_gives_the_same_answers_as_scoring_one_by_one():
    detector = AnomalyDetector(contamination=0.1, window_size=50)
    for i in range(60):
        detector.add_request(_request(i))
    detector.train()
    batch = [_request(i) for i in range(200, 240)] + [{"path": "/api/x", "status_code": 503, "response_time_ms": 90000}]
    one_by_one = [detector.detect_anomaly(r) for r in batch]
    together = detector.detect_anomalies(batch)
    assert [a for a, _ in together] == [a for a, _ in one_by_one]
    assert [round(s, 9) for _, s in together] == [round(s, 9) for _, s in one_by_one]
    assert together[-1][0] is True               # the slow 503 stands out


def test_batch_scoring_before_training_reports_nothing():
    detector = AnomalyDetector(window_size=50)
    assert detector.detect_anomalies([_request(1), _request(2)]) == [(False, 0.0), (False, 0.0)]
    assert detector.detect_anomalies([]) == []


def test_ai_service_process_batch_returns_one_result_per_request():
    from app.ai_service import AIService
    svc = AIService()
    out = svc.process_batch([{"path": "/api/x", "status_code": 200, "response_time_ms": 4, "client_id": "k1"}] * 3)
    assert len(out) == 3 and all("is_anomaly" in r for r in out)
