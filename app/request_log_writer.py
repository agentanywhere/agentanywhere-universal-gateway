"""Where request-log rows are written, and how.

``REQUEST_LOG_MODE`` chooses:

* ``inline``  (default, the previous behaviour) one task per request opens its own
  session, inserts one row and commits. Nothing bounds the number of waiting tasks.
* ``batched`` rows go into a bounded in-memory queue and one background task writes
  them in batches, one transaction per batch. When the queue is full the newest row is
  dropped and counted, so a slow database cannot grow memory without limit or slow
  requests down. A crash can lose the rows still in the queue.
* ``off``     no rows are written (the dashboard's log views stay empty).
"""
from __future__ import annotations

import asyncio
from typing import Any, List, Optional, Tuple

import structlog

logger = structlog.get_logger("gateway")

MODES = ("inline", "batched", "off")


class RequestLogWriter:
    def __init__(
        self,
        mode: str = "inline",
        batch_size: int = 200,
        flush_interval: float = 0.25,
        queue_size: int = 20000,
    ) -> None:
        self.mode = mode if mode in MODES else "inline"
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.01, float(flush_interval))
        self.queue_size = max(1, int(queue_size))
        self.written = 0
        self.dropped = 0
        self.failed = 0
        self._queue: Optional[asyncio.Queue] = None
        self._task: Optional[asyncio.Task] = None

    # -- called from the request path -------------------------------------------------
    def submit(self, entry: Any, api_key_id: Optional[str]) -> None:
        """Hand one finished request to the writer. Never blocks and never raises."""
        if self.mode == "off":
            return
        if self.mode == "batched" and self._queue is not None:
            try:
                self._queue.put_nowait((entry, api_key_id))
            except asyncio.QueueFull:
                self.dropped += 1
                if self.dropped == 1 or self.dropped % 1000 == 0:
                    logger.warning("request_log_queue_full", dropped=self.dropped, queue_size=self.queue_size)
            return
        asyncio.create_task(self._write_one(entry, api_key_id))

    # -- inline -----------------------------------------------------------------------
    async def _write_one(self, entry: Any, api_key_id: Optional[str]) -> None:
        try:
            from app.database import AsyncSessionLocal
            from app.repositories.log_repo import LogRepository

            async with AsyncSessionLocal() as session:
                await LogRepository.create(session, entry, api_key_id=api_key_id)
            self.written += 1
        except Exception as exc:
            self.failed += 1
            logger.debug("request_log_persist_failed", error=str(exc))

    # -- batched ----------------------------------------------------------------------
    async def start(self) -> None:
        if self.mode == "batched" and self._task is None:
            self._queue = asyncio.Queue(maxsize=self.queue_size)
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Write what is still queued, then stop."""
        if self._task is None:
            return
        task, self._task = self._task, None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await self._drain()
        self._queue = None

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            first = await self._queue.get()
            batch: List[Tuple[Any, Optional[str]]] = [first]
            deadline = asyncio.get_running_loop().time() + self.flush_interval
            while len(batch) < self.batch_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
                except asyncio.TimeoutError:
                    break
            await self._write_batch(batch)

    async def _drain(self) -> None:
        if self._queue is None:
            return
        while not self._queue.empty():
            batch = []
            while len(batch) < self.batch_size and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            await self._write_batch(batch)

    async def _write_batch(self, batch: List[Tuple[Any, Optional[str]]]) -> None:
        try:
            from app.database import AsyncSessionLocal
            from app.repositories.log_repo import LogRepository

            async with AsyncSessionLocal() as session:
                await LogRepository.create_many(session, batch)
            self.written += len(batch)
        except Exception as exc:
            self.failed += len(batch)
            logger.warning("request_log_batch_failed", rows=len(batch), error=str(exc))


def writer_from_settings(settings: Any) -> RequestLogWriter:
    return RequestLogWriter(
        mode=str(getattr(settings, "request_log_mode", "inline")).lower(),
        batch_size=getattr(settings, "request_log_batch_size", 200),
        flush_interval=getattr(settings, "request_log_flush_interval", 0.25),
        queue_size=getattr(settings, "request_log_queue_size", 20000),
    )

