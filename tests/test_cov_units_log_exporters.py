"""Unit tests for app.log_exporters (all network/AWS calls mocked)."""
import asyncio
import gzip
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app import log_exporters as le

LOGS = [{"timestamp": "2024-01-01T00:00:00", "client_ip": "1.2.3.4", "path": "/a"},
        {"timestamp": 1700000000.0, "path": "/b"}]


def _resp(status=200):
    r = MagicMock()
    r.status_code = status
    if status >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError("x", request=MagicMock(), response=r)
    else:
        r.raise_for_status.return_value = None
    return r


# ---------------- base ----------------
async def test_base_exporter_contract():
    b = le.LogExporter("x")
    with pytest.raises(NotImplementedError):
        await b.export([])
    with pytest.raises(NotImplementedError):
        await b.test_connection()
    assert await b.close() is None
    assert b.describe() == {"name": "x", "enabled": True}


# ---------------- elasticsearch ----------------
async def test_es_export_success_and_bulk_format():
    e = le.ElasticsearchExporter("http://es:9200/", index="idx")
    e.client = MagicMock(post=AsyncMock(return_value=_resp()))
    assert await e.export(LOGS) is True
    args, kw = e.client.post.call_args
    assert args[0] == "http://es:9200/_bulk"
    lines = kw["content"].strip().split("\n")
    assert len(lines) == 4 and json.loads(lines[0]) == {"index": {"_index": "idx"}}
    assert e.describe()["index"] == "idx"


async def test_es_empty_failure_and_connection():
    e = le.ElasticsearchExporter("http://es:9200")
    e.client = MagicMock(post=AsyncMock(return_value=_resp(500)), get=AsyncMock(return_value=_resp(200)),
                         aclose=AsyncMock())
    assert await e.export([]) is True
    e.client.post.assert_not_called()
    assert await e.export(LOGS) is False
    assert await e.test_connection() is True
    e.client.get = AsyncMock(side_effect=httpx.ConnectError("down"))
    assert await e.test_connection() is False
    await e.close()
    e.client.aclose.assert_awaited()


# ---------------- splunk ----------------
async def test_splunk_export_paths():
    s = le.SplunkExporter("http://splunk:8088/", "tok", index="ix")
    s.client = MagicMock(post=AsyncMock(return_value=_resp()), get=AsyncMock(return_value=_resp(200)),
                         aclose=AsyncMock())
    assert await s.export([]) is True
    assert await s.export(LOGS) is True
    assert s.client.post.await_count == 2
    kw = s.client.post.call_args_list[0].kwargs
    assert kw["headers"]["Authorization"] == "Splunk tok"
    assert kw["json"]["host"] == "1.2.3.4" and kw["json"]["index"] == "ix"
    assert s.client.post.call_args_list[1].kwargs["json"]["host"] == "unknown"
    s.client.post = AsyncMock(return_value=_resp(403))
    assert await s.export(LOGS) is False
    assert await s.test_connection() is True
    s.client.get = AsyncMock(side_effect=RuntimeError)
    assert await s.test_connection() is False
    await s.close()
    assert s.describe()["url"] == "http://splunk:8088"


# ---------------- datadog ----------------
async def test_datadog_export_and_format():
    d = le.DatadogExporter("key", site="datadoghq.eu", tags=["a:1", "b:2"])
    assert d.intake_url == "https://http-intake.logs.datadoghq.eu/api/v2/logs"
    d.client = MagicMock(post=AsyncMock(return_value=_resp(202)), aclose=AsyncMock())
    ev = d._format_event({"path": "/x", "message": "m"})
    assert ev["ddtags"] == "a:1,b:2" and ev["host"] == "agentanywhere-gateway"
    assert "message" in ev and json.loads(ev["message"])["path"] == "/x"
    assert le.DatadogExporter("k")._format_event({})["ddtags"] is None
    assert await d.export([]) is True
    assert await d.export(LOGS) is True
    assert d.client.post.call_args.kwargs["headers"]["DD-API-KEY"] == "key"
    assert await d.test_connection() is True
    d.client.post = AsyncMock(return_value=_resp(401))
    assert await d.test_connection() is False
    assert await d.export(LOGS) is False
    d.client.post = AsyncMock(side_effect=httpx.TimeoutException("t"))
    assert await d.test_connection() is False
    assert await d.export(LOGS) is False
    await d.close()
    assert d.describe()["service"] == "agentanywhere-gateway"
    assert le.DatadogExporter("k", intake_url="http://custom").intake_url == "http://custom"


# ---------------- boto3 based ----------------
@pytest.fixture
def fake_boto(monkeypatch):
    client = MagicMock()
    boto = types.SimpleNamespace(client=MagicMock(return_value=client))
    monkeypatch.setitem(sys.modules, "boto3", boto)
    return boto, client


async def test_cloudwatch_export(fake_boto):
    boto, client = fake_boto
    client.create_log_group.side_effect = Exception("exists")
    client.put_log_events.return_value = {"nextSequenceToken": "t2"}
    cw = le.CloudWatchExporter("g", "s", region="eu-west-1", access_key="a", secret_key="b")
    assert cw.enabled
    assert boto.client.call_args.kwargs["aws_access_key_id"] == "a"
    assert await cw.export([]) is True
    assert await cw.export(LOGS + [{"x": 1}]) is True
    assert cw._sequence_token == "t2"
    assert await cw.export(LOGS) is True
    assert client.put_log_events.call_args.kwargs["sequenceToken"] == "t2"
    client.put_log_events.side_effect = RuntimeError("throttle")
    assert await cw.export(LOGS) is False
    assert await cw.test_connection() is True
    client.describe_log_groups.side_effect = RuntimeError
    assert await cw.test_connection() is False
    assert cw.describe()["region"] == "eu-west-1"


async def test_cloudwatch_unavailable(monkeypatch):
    boto = types.SimpleNamespace(client=MagicMock(side_effect=RuntimeError("no creds")))
    monkeypatch.setitem(sys.modules, "boto3", boto)
    cw = le.CloudWatchExporter("g", "s")
    assert cw.enabled is False
    assert await cw.export(LOGS) is False
    assert await cw.test_connection() is False
    cw._ensure_stream()  # no client: no-op


async def test_s3_export(fake_boto):
    boto, client = fake_boto
    s3 = le.S3Exporter("bkt", prefix="logs", access_key="a", secret_key="b")
    assert s3.prefix == "logs/"
    assert le.S3Exporter("bkt", prefix="").prefix == ""
    assert await s3.export([]) is True
    assert await s3.export(LOGS) is True
    kw = client.put_object.call_args.kwargs
    assert kw["Bucket"] == "bkt" and kw["Key"].startswith("logs/") and kw["Key"].endswith(".json.gz")
    lines = gzip.decompress(kw["Body"]).decode().split("\n")
    assert [json.loads(x)["path"] for x in lines] == ["/a", "/b"]
    client.put_object.side_effect = RuntimeError("denied")
    assert await s3.export(LOGS) is False
    assert await s3.test_connection() is True
    client.head_bucket.side_effect = RuntimeError
    assert await s3.test_connection() is False
    assert s3.describe()["bucket"] == "bkt"


async def test_s3_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=MagicMock(side_effect=RuntimeError)))
    s3 = le.S3Exporter("b")
    assert s3.enabled is False
    assert await s3.export(LOGS) is False
    assert await s3.test_connection() is False


# ---------------- manager ----------------
class _Fake(le.LogExporter):
    def __init__(self, name, result=True, exc=None):
        super().__init__(name)
        self.result, self.exc, self.got, self.closed = result, exc, [], False

    async def export(self, logs):
        if self.exc:
            raise self.exc
        self.got.append(logs)
        return self.result

    async def test_connection(self):
        if self.exc:
            raise self.exc
        return self.result

    async def close(self):
        self.closed = True
        if self.exc:
            raise self.exc


async def test_manager_registry_and_export():
    m = le.LogExportManager()
    a, b, c = _Fake("a"), _Fake("b", exc=RuntimeError("boom")), _Fake("c")
    c.enabled = False
    for x in (a, b, c):
        m.add_exporter(x)
    m.add_exporter(_Fake("a", result=False))  # replaces
    assert len(m.exporters) == 3
    assert m.get_exporter("zzz") is None
    assert {d["name"] for d in m.list_exporters()} == {"a", "b", "c"}
    assert await m.export_logs(LOGS) == {"a": False, "b": False}
    res = await m.test_all_connections()
    assert res["b"] is False and res["c"] is True
    m.remove_exporter("nope")
    m.remove_exporter("b")  # running loop: schedules close (which raises, swallowed)
    await asyncio.sleep(0.01)
    assert b.closed and m.get_exporter("b") is None


def test_remove_exporter_without_loop():
    m = le.LogExportManager()
    m.add_exporter(_Fake("a"))
    m.remove_exporter("a")
    assert m.exporters == []


async def test_manager_worker_batches_and_shutdown_drain():
    m = le.LogExportManager(batch_size=2, flush_interval=0.05, max_queue_size=3)
    ex = _Fake("a")
    m.add_exporter(ex)
    assert m.enqueue({"i": 0}) is False  # not running
    await m.start()
    assert m.is_running()
    await m.start()  # idempotent
    for i in range(3):
        assert m.enqueue({"i": i}) is True
    assert m.enqueue({"i": 99}) is False  # full (worker may have drained; tolerate)
    await asyncio.sleep(0.2)
    assert sum(len(b) for b in ex.got) >= 3
    assert all(len(b) <= 2 for b in ex.got)
    await m.stop()
    assert not m.is_running() and ex.closed


async def test_manager_stop_drains_remaining():
    m = le.LogExportManager(batch_size=10, flush_interval=5.0)
    ex = _Fake("a")
    m.add_exporter(ex)
    await m.start()
    await asyncio.sleep(0)  # worker waits on queue
    m._stop_event.set()
    m._queue.put_nowait({"late": 1})
    await asyncio.wait_for(m._worker_task, 2)
    flat = [x for b in ex.got for x in b]
    assert {"late": 1} in flat
    await m.stop()


async def test_manager_drain_batch_timeout():
    m = le.LogExportManager(flush_interval=0.01)
    m._queue = asyncio.Queue()
    assert await m._drain_batch() == []


async def test_manager_stop_timeout_cancels(monkeypatch):
    m = le.LogExportManager()

    async def forever():
        await asyncio.sleep(100)

    m._worker_task = asyncio.create_task(forever())
    m._stop_event = asyncio.Event()

    async def fake_wait_for(aw, timeout):
        aw.cancel() if False else None
        raise asyncio.TimeoutError

    monkeypatch.setattr(le.asyncio, "wait_for", fake_wait_for)
    t = m._worker_task
    await m.stop()
    monkeypatch.undo()
    await asyncio.sleep(0)
    assert t.cancelled() or t.cancelling() > 0 or t.done()


async def test_worker_cancel_returns_quietly():
    m = le.LogExportManager(flush_interval=10)
    await m.start()
    await asyncio.sleep(0)
    m._worker_task.cancel()
    await asyncio.sleep(0.01)
    assert not m.is_running()


# ---------------- configure_from_settings ----------------
def test_configure_from_settings(monkeypatch, fake_boto):
    m = le.LogExportManager()
    S = types.SimpleNamespace
    monkeypatch.setattr(le, "settings", S(log_export_enabled=False))
    le.configure_from_settings(m)
    assert m.exporters == []
    monkeypatch.setattr(le, "settings", S(
        log_export_enabled=True,
        elasticsearch_enabled=True, elasticsearch_url="http://es", elasticsearch_index="i",
        splunk_enabled=True, splunk_url="http://sp", splunk_token="t", splunk_index="g",
        s3_log_export_enabled=True, s3_log_bucket="b", s3_log_prefix="p/", s3_log_region="us-east-1",
        aws_access_key_id=None, aws_secret_access_key=None,
        datadog_logs_enabled=True, datadog_api_key="k", datadog_tags="a:1, ,b:2",
        cloudwatch_logs_enabled=True, cloudwatch_log_group="g", cloudwatch_log_stream="s",
    ))
    le.configure_from_settings(m)
    assert {e.name for e in m.exporters} == {"elasticsearch", "splunk", "s3", "datadog", "cloudwatch"}
    assert m.get_exporter("datadog").tags == ["a:1", "b:2"]
    # enabled flags set but required values missing -> nothing added
    m2 = le.LogExportManager()
    monkeypatch.setattr(le, "settings", S(log_export_enabled=True, elasticsearch_enabled=True,
                                          elasticsearch_url="", splunk_enabled=True, splunk_url="x", splunk_token=""))
    le.configure_from_settings(m2)
    assert m2.exporters == []
