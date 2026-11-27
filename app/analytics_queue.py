"""Run the analytics engine off the request path.

``AI_ANALYTICS_MODE`` chooses:

* ``background`` (default) the request's facts go into a bounded in-memory queue and one
  background task hands them to the analytics engine in batches. A response is never
  held up by scoring. When the queue is full the oldest entry is dropped and counted.
  The engine's ``process_batch`` is used when it has one (the built-in engine scores a
  whole batch with one model call); otherwise ``process_request`` per entry.
* ``inline``  the engine runs in the request path, before the response is sent, and an
  anomaly adds ``X-Anomaly-Detected`` and ``X-Anomaly-Score`` to that response. The
  behaviour before 0.9.2.
"""
from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Deque, Dict, List, Optional

import structlog

logger = structlog.get_logger("gateway")

MODES = ("background", "inline")


class AnalyticsQueue:
    def __init__(self, engine: Any, mode: str = "background", batch_size: int = 500,
                 flush_interval: float = 0.25, queue_size: int = 10000) -> None:
        self.engine = engine
        self.mode = mode if mode in MODES else "background"
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.01, float(flush_interval))
        self.queue: Deque[Dict[str, Any]] = deque(maxlen=max(1, int(queue_size)))
        self.processed = 0
        self.dropped = 0
        self.failed = 0
        self._task: Optional[asyncio.Task] = None
        self._stopping = False

    @property
    def inline(self) -> bool:
        return self.mode == "inline"

    def submit(self, request_data: Dict[str, Any]) -> None:
        """Queue one request's facts for scoring (``background`` mode)."""
        if len(self.queue) == self.queue.maxlen:
            self.dropped += 1          # deque drops the oldest entry on append
        self.queue.append(request_data)

    async def start(self) -> None:
        if self.inline or self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="analytics-queue")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping = True
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except Exception:
            self._task.cancel()
        self._task = None
        self.drain()                    # whatever arrived during shutdown

    async def _run(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.flush_interval)
            self.drain()
        self.drain()

    def drain(self) -> int:
        """Hand everything queued to the engine, a batch at a time. Returns entries processed."""
        done = 0
        while self.queue:
            batch: List[Dict[str, Any]] = []
            while self.queue and len(batch) < self.batch_size:
                batch.append(self.queue.popleft())
            try:
                process_batch = getattr(self.engine, "process_batch", None)
                if callable(process_batch):
                    process_batch(batch)
                else:
                    for item in batch:
                        self.engine.process_request(item)
                self.processed += len(batch)
                done += len(batch)
            except Exception as exc:  # scoring must never take the gateway down
                self.failed += len(batch)
                logger.warning("analytics_batch_failed", entries=len(batch), error=str(exc))
        return done


def queue_from_settings(engine: Any, settings: Any) -> AnalyticsQueue:
    return AnalyticsQueue(
        engine,
        mode=str(getattr(settings, "ai_analytics_mode", "background")).lower(),
        batch_size=getattr(settings, "ai_analytics_batch_size", 500),
        flush_interval=getattr(settings, "ai_analytics_flush_interval", 0.25),
        queue_size=getattr(settings, "ai_analytics_queue_size", 10000),
    )
