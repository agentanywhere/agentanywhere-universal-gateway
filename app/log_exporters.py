"""Log export to external systems.

Provides exporters for Elasticsearch, Splunk, AWS CloudWatch, AWS S3 and
Datadog, plus a batching worker that buffers log entries and flushes
them in the background.
"""
from typing import Dict, Any, List, Optional, Callable, Awaitable
from datetime import datetime
from app.time_utils import utcnow
import asyncio
import gzip
import io
import json
import os
import uuid

import httpx

from app.config import get_settings
import structlog

logger = structlog.get_logger()
settings = get_settings()


class LogExporter:
    """Base class for log exporters."""

    def __init__(self, name: str):
        self.name = name
        self.enabled = True

    async def export(self, logs: List[Dict[str, Any]]) -> bool:
        """Export logs. Returns True if successful."""
        raise NotImplementedError

    async def test_connection(self) -> bool:
        """Test connection to external system."""
        raise NotImplementedError

    async def close(self) -> None:
        """Optional close hook for subclasses with resources to release."""
        return None

    def describe(self) -> Dict[str, Any]:
        """Return a JSON-serialisable description of the exporter."""
        return {"name": self.name, "enabled": self.enabled}


class ElasticsearchExporter(LogExporter):
    """Export logs to Elasticsearch."""

    def __init__(self, url: str, index: str = "gateway-logs"):
        super().__init__("elasticsearch")
        self.url = url.rstrip('/')
        self.index = index
        self.client = httpx.AsyncClient(timeout=10.0)

    async def export(self, logs: List[Dict[str, Any]]) -> bool:
        if not logs:
            return True
        try:
            bulk_data = []
            for log in logs:
                bulk_data.append(json.dumps({"index": {"_index": self.index}}))
                bulk_data.append(json.dumps(log, default=str))
            bulk_body = "\n".join(bulk_data) + "\n"

            response = await self.client.post(
                f"{self.url}/_bulk",
                content=bulk_body,
                headers={"Content-Type": "application/x-ndjson"},
            )
            response.raise_for_status()
            logger.info("logs_exported", exporter=self.name, count=len(logs))
            return True
        except Exception as exc:
            logger.error("log_export_failed", exporter=self.name, error=str(exc))
            return False

    async def test_connection(self) -> bool:
        try:
            response = await self.client.get(f"{self.url}/_cluster/health")
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        await self.client.aclose()

    def describe(self) -> Dict[str, Any]:
        return {**super().describe(), "url": self.url, "index": self.index}


class SplunkExporter(LogExporter):
    """Export logs to Splunk."""

    def __init__(self, url: str, token: str, index: str = "gateway", verify_ssl: bool = True):
        super().__init__("splunk")
        self.url = url.rstrip('/')
        self.token = token
        self.index = index
        self.client = httpx.AsyncClient(timeout=10.0, verify=verify_ssl)

    async def export(self, logs: List[Dict[str, Any]]) -> bool:
        if not logs:
            return True
        try:
            for log in logs:
                event = {
                    "time": log.get("timestamp", utcnow().timestamp()),
                    "host": log.get("client_ip", "unknown"),
                    "source": "agentanywhere-gateway",
                    "sourcetype": "gateway:access",
                    "index": self.index,
                    "event": log,
                }
                response = await self.client.post(
                    f"{self.url}/services/collector/event",
                    json=event,
                    headers={
                        "Authorization": f"Splunk {self.token}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
            logger.info("logs_exported", exporter=self.name, count=len(logs))
            return True
        except Exception as exc:
            logger.error("log_export_failed", exporter=self.name, error=str(exc))
            return False

    async def test_connection(self) -> bool:
        try:
            response = await self.client.get(
                f"{self.url}/services/collector/health",
                headers={"Authorization": f"Splunk {self.token}"},
            )
            return response.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        await self.client.aclose()

    def describe(self) -> Dict[str, Any]:
        return {**super().describe(), "url": self.url, "index": self.index}


class CloudWatchExporter(LogExporter):
    """Export logs to AWS CloudWatch Logs.

    boto3 is imported lazily so the gateway does not require AWS
    dependencies unless this exporter is enabled.
    """

    def __init__(
        self,
        log_group: str,
        log_stream: str,
        region: str = "us-east-1",
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        super().__init__("cloudwatch")
        self.log_group = log_group
        self.log_stream = log_stream
        self.region = region
        self._client = None
        self._sequence_token: Optional[str] = None
        try:
            import boto3  # noqa: WPS433

            kwargs: Dict[str, Any] = {"region_name": region}
            if access_key and secret_key:
                kwargs["aws_access_key_id"] = access_key
                kwargs["aws_secret_access_key"] = secret_key
            self._client = boto3.client("logs", **kwargs)
            self._ensure_stream()
            self.enabled = True
        except Exception as exc:  # pragma: no cover - boto3 may be absent in tests
            logger.warning(
                "cloudwatch_exporter_unavailable",
                error=str(exc),
                hint="Install boto3 to enable CloudWatch log export",
            )
            self.enabled = False

    def _ensure_stream(self) -> None:
        if not self._client:
            return
        try:
            self._client.create_log_group(logGroupName=self.log_group)
        except Exception:
            pass
        try:
            self._client.create_log_stream(
                logGroupName=self.log_group, logStreamName=self.log_stream
            )
        except Exception:
            pass

    async def export(self, logs: List[Dict[str, Any]]) -> bool:
        if not logs:
            return True
        if not self._client or not self.enabled:
            return False

        def _put_events() -> bool:
            assert self._client is not None
            events = [
                {
                    "timestamp": int(
                        (
                            datetime.fromisoformat(log["timestamp"]).timestamp()
                            if isinstance(log.get("timestamp"), str)
                            else (log.get("timestamp") or utcnow().timestamp())
                        )
                        * 1000
                    ),
                    "message": json.dumps(log, default=str),
                }
                for log in logs
            ]
            kwargs: Dict[str, Any] = {
                "logGroupName": self.log_group,
                "logStreamName": self.log_stream,
                "logEvents": events,
            }
            if self._sequence_token:
                kwargs["sequenceToken"] = self._sequence_token
            response = self._client.put_log_events(**kwargs)
            self._sequence_token = response.get("nextSequenceToken")
            return True

        try:
            await asyncio.to_thread(_put_events)
            logger.info("logs_exported", exporter=self.name, count=len(logs))
            return True
        except Exception as exc:
            logger.error("log_export_failed", exporter=self.name, error=str(exc))
            return False

    async def test_connection(self) -> bool:
        if not self._client:
            return False
        try:
            await asyncio.to_thread(self._client.describe_log_groups, limit=1)
            return True
        except Exception:
            return False

    def describe(self) -> Dict[str, Any]:
        return {
            **super().describe(),
            "log_group": self.log_group,
            "log_stream": self.log_stream,
            "region": self.region,
        }


class S3Exporter(LogExporter):
    """Export logs to AWS S3 as gzipped NDJSON objects."""

    def __init__(
        self,
        bucket: str,
        prefix: str = "gateway-logs/",
        region: str = "us-east-1",
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
    ):
        super().__init__("s3")
        self.bucket = bucket
        self.prefix = prefix.rstrip("/") + "/" if prefix else ""
        self.region = region
        self._client = None
        try:
            import boto3  # noqa: WPS433

            kwargs: Dict[str, Any] = {"region_name": region}
            if access_key and secret_key:
                kwargs["aws_access_key_id"] = access_key
                kwargs["aws_secret_access_key"] = secret_key
            self._client = boto3.client("s3", **kwargs)
            self.enabled = True
        except Exception as exc:  # pragma: no cover
            logger.warning(
                "s3_exporter_unavailable",
                error=str(exc),
                hint="Install boto3 to enable S3 log export",
            )
            self.enabled = False

    def _build_key(self) -> str:
        now = utcnow()
        return (
            f"{self.prefix}{now.strftime('%Y/%m/%d')}/"
            f"gateway-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json.gz"
        )

    async def export(self, logs: List[Dict[str, Any]]) -> bool:
        if not logs:
            return True
        if not self._client or not self.enabled:
            return False

        ndjson = "\n".join(json.dumps(log, default=str) for log in logs).encode("utf-8")
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb") as gz:
            gz.write(ndjson)
        body = buffer.getvalue()
        key = self._build_key()

        def _upload() -> bool:
            assert self._client is not None
            self._client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                ContentEncoding="gzip",
            )
            return True

        try:
            await asyncio.to_thread(_upload)
            logger.info(
                "logs_exported",
                exporter=self.name,
                count=len(logs),
                key=key,
                bytes=len(body),
            )
            return True
        except Exception as exc:
            logger.error("log_export_failed", exporter=self.name, error=str(exc))
            return False

    async def test_connection(self) -> bool:
        if not self._client:
            return False
        try:
            await asyncio.to_thread(self._client.head_bucket, Bucket=self.bucket)
            return True
        except Exception:
            return False

    def describe(self) -> Dict[str, Any]:
        return {
            **super().describe(),
            "bucket": self.bucket,
            "prefix": self.prefix,
            "region": self.region,
        }


class DatadogExporter(LogExporter):
    """Export logs to Datadog Logs intake."""

    DEFAULT_INTAKE = "https://http-intake.logs.datadoghq.com/api/v2/logs"

    def __init__(
        self,
        api_key: str,
        site: str = "datadoghq.com",
        service: str = "agentanywhere-gateway",
        source: str = "python",
        intake_url: Optional[str] = None,
        tags: Optional[List[str]] = None,
    ):
        super().__init__("datadog")
        self.api_key = api_key
        self.site = site
        self.service = service
        self.source = source
        self.tags = tags or []
        self.intake_url = intake_url or f"https://http-intake.logs.{site}/api/v2/logs"
        self.client = httpx.AsyncClient(timeout=10.0)

    def _format_event(self, log: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "ddsource": self.source,
            "ddtags": ",".join(self.tags) if self.tags else None,
            "service": self.service,
            "host": log.get("client_ip") or "agentanywhere-gateway",
            "message": json.dumps(log, default=str),
            **{k: v for k, v in log.items() if k not in {"message"}},
        }

    async def export(self, logs: List[Dict[str, Any]]) -> bool:
        if not logs:
            return True
        try:
            payload = [self._format_event(log) for log in logs]
            response = await self.client.post(
                self.intake_url,
                json=payload,
                headers={
                    "DD-API-KEY": self.api_key,
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            logger.info("logs_exported", exporter=self.name, count=len(logs))
            return True
        except Exception as exc:
            logger.error("log_export_failed", exporter=self.name, error=str(exc))
            return False

    async def test_connection(self) -> bool:
        try:
            response = await self.client.post(
                self.intake_url,
                json=[],
                headers={"DD-API-KEY": self.api_key, "Content-Type": "application/json"},
            )
            return response.status_code in (200, 202)
        except Exception:
            return False

    async def close(self) -> None:
        await self.client.aclose()

    def describe(self) -> Dict[str, Any]:
        return {
            **super().describe(),
            "site": self.site,
            "service": self.service,
            "intake_url": self.intake_url,
        }


class LogExportManager:
    """Manages log exports with optional batching worker."""

    def __init__(
        self,
        batch_size: int = 100,
        flush_interval: float = 5.0,
        max_queue_size: int = 10000,
    ):
        self.exporters: List[LogExporter] = []
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.max_queue_size = max_queue_size
        self._queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None

    # ---------- registry ----------

    def add_exporter(self, exporter: LogExporter) -> None:
        # Replace any existing exporter with the same name
        self.exporters = [e for e in self.exporters if e.name != exporter.name]
        self.exporters.append(exporter)
        logger.info("log_exporter_added", exporter=exporter.name)

    def get_exporter(self, name: str) -> Optional[LogExporter]:
        return next((e for e in self.exporters if e.name == name), None)

    def remove_exporter(self, name: str) -> None:
        exporter = self.get_exporter(name)
        if not exporter:
            return
        self.exporters = [e for e in self.exporters if e.name != name]
        logger.info("log_exporter_removed", exporter=name)

        async def _close() -> None:
            try:
                await exporter.close()
            except Exception:
                pass

        try:
            asyncio.get_running_loop().create_task(_close())
        except RuntimeError:
            pass

    def list_exporters(self) -> List[Dict[str, Any]]:
        return [exporter.describe() for exporter in self.exporters]

    # ---------- export pipeline ----------

    async def export_logs(self, logs: List[Dict[str, Any]]) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        for exporter in self.exporters:
            if not exporter.enabled:
                continue
            try:
                success = await exporter.export(logs)
                results[exporter.name] = success
            except Exception as exc:
                logger.error("log_export_error", exporter=exporter.name, error=str(exc))
                results[exporter.name] = False
        return results

    async def test_all_connections(self) -> Dict[str, bool]:
        results: Dict[str, bool] = {}
        for exporter in self.exporters:
            try:
                results[exporter.name] = await exporter.test_connection()
            except Exception:
                results[exporter.name] = False
        return results

    # ---------- background batching ----------

    def is_running(self) -> bool:
        return self._worker_task is not None and not self._worker_task.done()

    async def start(self) -> None:
        if self.is_running():
            return
        self._queue = asyncio.Queue(maxsize=self.max_queue_size)
        self._stop_event = asyncio.Event()
        self._worker_task = asyncio.create_task(self._worker())
        logger.info(
            "log_export_worker_started",
            batch_size=self.batch_size,
            flush_interval=self.flush_interval,
        )

    async def stop(self) -> None:
        if self._stop_event:
            self._stop_event.set()
        if self._worker_task:
            try:
                await asyncio.wait_for(self._worker_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
        self._worker_task = None
        for exporter in list(self.exporters):
            try:
                await exporter.close()
            except Exception:
                pass

    def enqueue(self, log_entry: Dict[str, Any]) -> bool:
        """Add a log entry to the in-memory buffer.

        Returns False if the buffer is full or the worker is not running.
        """
        if not self._queue or not self.is_running():
            return False
        try:
            self._queue.put_nowait(log_entry)
            return True
        except asyncio.QueueFull:
            logger.warning("log_export_queue_full")
            return False

    async def _drain_batch(self) -> List[Dict[str, Any]]:
        assert self._queue is not None
        batch: List[Dict[str, Any]] = []
        try:
            first = await asyncio.wait_for(
                self._queue.get(), timeout=self.flush_interval
            )
            batch.append(first)
        except asyncio.TimeoutError:
            return batch
        while len(batch) < self.batch_size:
            try:
                batch.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return batch

    async def _worker(self) -> None:
        assert self._queue is not None
        assert self._stop_event is not None
        try:
            while not self._stop_event.is_set():
                batch = await self._drain_batch()
                if not batch:
                    continue
                await self.export_logs(batch)
            # Drain remaining items on shutdown
            remaining: List[Dict[str, Any]] = []
            while not self._queue.empty():
                try:
                    remaining.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            if remaining:
                await self.export_logs(remaining)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("log_export_worker_error", error=str(exc))


def configure_from_settings(manager: "LogExportManager") -> None:
    """Wire up exporters from environment-driven settings."""
    if not getattr(settings, "log_export_enabled", False):
        return

    if getattr(settings, "elasticsearch_enabled", False) and settings.elasticsearch_url:
        manager.add_exporter(
            ElasticsearchExporter(
                url=settings.elasticsearch_url,
                index=settings.elasticsearch_index,
            )
        )

    if (
        getattr(settings, "splunk_enabled", False)
        and settings.splunk_url
        and settings.splunk_token
    ):
        manager.add_exporter(
            SplunkExporter(
                url=settings.splunk_url,
                token=settings.splunk_token,
                index=settings.splunk_index,
            )
        )

    if getattr(settings, "s3_log_export_enabled", False) and getattr(settings, "s3_log_bucket", None):
        manager.add_exporter(
            S3Exporter(
                bucket=settings.s3_log_bucket,
                prefix=getattr(settings, "s3_log_prefix", "gateway-logs/"),
                region=getattr(settings, "s3_log_region", "us-east-1"),
                access_key=getattr(settings, "aws_access_key_id", None),
                secret_key=getattr(settings, "aws_secret_access_key", None),
            )
        )

    if getattr(settings, "datadog_logs_enabled", False) and getattr(
        settings, "datadog_api_key", None
    ):
        manager.add_exporter(
            DatadogExporter(
                api_key=settings.datadog_api_key,
                site=getattr(settings, "datadog_site", "datadoghq.com"),
                service=getattr(settings, "datadog_service", "agentanywhere-gateway"),
                source=getattr(settings, "datadog_source", "python"),
                tags=[
                    tag.strip()
                    for tag in getattr(settings, "datadog_tags", "").split(",")
                    if tag.strip()
                ],
            )
        )

    if (
        getattr(settings, "cloudwatch_logs_enabled", False)
        and getattr(settings, "cloudwatch_log_group", None)
        and getattr(settings, "cloudwatch_log_stream", None)
    ):
        manager.add_exporter(
            CloudWatchExporter(
                log_group=settings.cloudwatch_log_group,
                log_stream=settings.cloudwatch_log_stream,
                region=getattr(settings, "cloudwatch_region", "us-east-1"),
                access_key=getattr(settings, "aws_access_key_id", None),
                secret_key=getattr(settings, "aws_secret_access_key", None),
            )
        )


# Global log export manager
log_export_manager = LogExportManager(
    batch_size=int(os.getenv("LOG_EXPORT_BATCH_SIZE", "100")),
    flush_interval=float(os.getenv("LOG_EXPORT_FLUSH_INTERVAL", "5.0")),
    max_queue_size=int(os.getenv("LOG_EXPORT_MAX_QUEUE_SIZE", "10000")),
)
