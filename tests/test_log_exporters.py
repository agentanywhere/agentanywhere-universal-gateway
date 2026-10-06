"""Tests for log exporters and the batching worker."""
import asyncio
import gzip
import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.log_exporters import (
    DatadogExporter,
    ElasticsearchExporter,
    LogExportManager,
    LogExporter,
    SplunkExporter,
)


class _RecordingExporter(LogExporter):
    """Simple exporter that captures logs in-memory for assertions."""

    def __init__(self, name: str = "recorder", fail: bool = False):
        super().__init__(name)
        self.fail = fail
        self.batches: List[List[Dict[str, Any]]] = []

    async def export(self, logs):
        if self.fail:
            return False
        self.batches.append(list(logs))
        return True

    async def test_connection(self) -> bool:
        return not self.fail


@pytest.mark.asyncio
async def test_elasticsearch_exporter_calls_bulk():
    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post = AsyncMock(
            return_value=httpx.Response(
                200,
                request=httpx.Request("POST", "http://es/_bulk"),
            )
        )
        instance.aclose = AsyncMock()
        MockClient.return_value = instance

        exporter = ElasticsearchExporter("http://es")
        ok = await exporter.export([{"a": 1}, {"b": 2}])
        assert ok is True
        instance.post.assert_awaited_once()
        called_url = instance.post.await_args.args[0]
        assert called_url.endswith("/_bulk")


@pytest.mark.asyncio
async def test_splunk_exporter_posts_each_event():
    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post = AsyncMock(
            return_value=httpx.Response(
                200,
                request=httpx.Request("POST", "http://splunk/services/collector/event"),
            )
        )
        instance.aclose = AsyncMock()
        MockClient.return_value = instance

        exporter = SplunkExporter("http://splunk", token="tok", index="gateway")
        ok = await exporter.export([{"path": "/a"}, {"path": "/b"}])
        assert ok is True
        assert instance.post.await_count == 2


@pytest.mark.asyncio
async def test_datadog_exporter_posts_to_intake():
    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.post = AsyncMock(
            return_value=httpx.Response(
                202,
                request=httpx.Request(
                    "POST", "https://http-intake.logs.datadoghq.com/api/v2/logs"
                ),
            )
        )
        instance.aclose = AsyncMock()
        MockClient.return_value = instance

        exporter = DatadogExporter(
            api_key="dd-key",
            site="datadoghq.com",
            tags=["env:test"],
        )
        ok = await exporter.export([{"path": "/x", "status_code": 200}])
        assert ok is True
        instance.post.assert_awaited_once()
        kwargs = instance.post.await_args.kwargs
        headers = kwargs["headers"]
        assert headers["DD-API-KEY"] == "dd-key"
        events = kwargs["json"]
        assert len(events) == 1
        assert events[0]["service"] == "agentanywhere-gateway"
        assert events[0]["ddtags"] == "env:test"
        # ``message`` should be JSON-encoded original entry.
        assert json.loads(events[0]["message"]) == {"path": "/x", "status_code": 200}


@pytest.mark.asyncio
async def test_s3_exporter_uploads_gzipped_ndjson():
    pytest.importorskip("boto3", reason="boto3 not installed")

    boto_client = MagicMock()
    with patch("boto3.client", return_value=boto_client):
        from app.log_exporters import S3Exporter
        exporter = S3Exporter(bucket="test-bucket", prefix="logs/", region="us-east-1")
    assert exporter.enabled is True
    ok = await exporter.export([{"a": 1}, {"b": 2}])
    assert ok is True
    boto_client.put_object.assert_called_once()
    kwargs = boto_client.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "test-bucket"
    assert kwargs["Key"].startswith("logs/")
    assert kwargs["ContentEncoding"] == "gzip"
    decompressed = gzip.decompress(kwargs["Body"]).decode("utf-8").splitlines()
    assert json.loads(decompressed[0]) == {"a": 1}
    assert json.loads(decompressed[1]) == {"b": 2}


@pytest.mark.asyncio
async def test_s3_exporter_disabled_without_boto3():
    """When boto3 is missing, S3Exporter should mark itself disabled."""
    from app.log_exporters import S3Exporter

    # Force the import of boto3 to fail.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("boto3 missing for test")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=fake_import):
        exporter = S3Exporter(bucket="b")
    assert exporter.enabled is False
    assert await exporter.export([{"a": 1}]) is False


@pytest.mark.asyncio
async def test_log_export_manager_add_and_replace():
    manager = LogExportManager()
    a = _RecordingExporter("dup")
    b = _RecordingExporter("dup")
    manager.add_exporter(a)
    manager.add_exporter(b)
    # Adding two with the same name should leave only the latest one.
    assert manager.get_exporter("dup") is b
    assert len(manager.exporters) == 1


@pytest.mark.asyncio
async def test_log_export_manager_export_logs_routes_to_all():
    manager = LogExportManager()
    a = _RecordingExporter("a")
    b = _RecordingExporter("b", fail=True)
    manager.add_exporter(a)
    manager.add_exporter(b)
    result = await manager.export_logs([{"x": 1}])
    assert result == {"a": True, "b": False}
    assert a.batches == [[{"x": 1}]]


@pytest.mark.asyncio
async def test_log_export_manager_test_all_connections():
    manager = LogExportManager()
    manager.add_exporter(_RecordingExporter("ok"))
    manager.add_exporter(_RecordingExporter("bad", fail=True))
    results = await manager.test_all_connections()
    assert results == {"ok": True, "bad": False}


@pytest.mark.asyncio
async def test_batch_worker_drains_queue_on_stop():
    manager = LogExportManager(batch_size=10, flush_interval=0.05, max_queue_size=100)
    recorder = _RecordingExporter("rec")
    manager.add_exporter(recorder)
    await manager.start()
    try:
        for i in range(7):
            assert manager.enqueue({"i": i}) is True
        # Allow at least one flush cycle.
        await asyncio.sleep(0.2)
    finally:
        await manager.stop()

    flushed = sum(len(b) for b in recorder.batches)
    assert flushed == 7
