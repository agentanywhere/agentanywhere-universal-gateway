"""Where request-log rows are written, and how.

``REQUEST_LOG_MODE`` chooses:

* ``batched`` (default) rows go into a bounded in-memory queue and one background task writes
  them in batches, one transaction per batch. When the queue is full the newest row is
  dropped and counted, so a slow database cannot grow memory without limit or slow
  requests down. A crash can lose the rows still in the queue.
* ``inline``  one task per request opens its own session, inserts one row and commits.
  Nothing bounds the number of waiting tasks. The default before 0.9.2.
* ``off``     no rows are written (the dashboard's log views stay empty).
"""
from __future__ import annotations

import asyncio
from typing import Any, List, Optional, Tuple

import structlog

logger = structlog.get_logger("gateway")

MODES = ("inline", "batched", "off")

_STOP = object()                 # queued to ask the writer to finish what is ahead of it
_RETRY_DELAYS = (0.2, 0.5)       # seconds before the second and third attempt to write a batch
_STOP_TIMEOUT = 10.0             # most we wait for the queue to drain at shutdown


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
        """Write everything still queued, then stop.

        A stop marker goes in behind the queued rows and the writer finishes the batch it is
        building before it sees it, so nothing it already took off the queue is lost. If the
        queue is full or the database is too slow, the writer is cancelled after a timeout and
        what is left is counted as dropped.
        """
        if self._task is None:
            return
        task, self._task = self._task, None
        try:
            self._queue.put_nowait(_STOP)
            await asyncio.wait_for(asyncio.shield(task), timeout=_STOP_TIMEOUT)
        except (asyncio.QueueFull, asyncio.TimeoutError):
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
            if first is _STOP:
                return
            batch: List[Tuple[Any, Optional[str]]] = [first]
            stopping = False
            deadline = asyncio.get_running_loop().time() + self.flush_interval
            while len(batch) < self.batch_size:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if item is _STOP:
                    stopping = True
                    break
                batch.append(item)
            await self._write_batch(batch)
            if stopping:
                return

    async def _drain(self) -> None:
        if self._queue is None:
            return
        while not self._queue.empty():
            batch = []
            while len(batch) < self.batch_size and not self._queue.empty():
                item = self._queue.get_nowait()
                if item is not _STOP:
                    batch.append(item)
            if batch:
                await self._write_batch(batch)

    async def _write_batch(self, batch: List[Tuple[Any, Optional[str]]]) -> None:
        """Write one batch in one transaction; retry a couple of times if the database is busy."""
        error: Optional[Exception] = None
        for attempt, delay in enumerate((0.0, *_RETRY_DELAYS)):
            if delay:
                await asyncio.sleep(delay)
            try:
                from app.database import AsyncSessionLocal
                from app.repositories.log_repo import LogRepository

                async with AsyncSessionLocal() as session:
                    await LogRepository.create_many(session, batch)
                self.written += len(batch)
                return
            except Exception as exc:
                error = exc
        self.failed += len(batch)
        logger.warning("request_log_batch_failed", rows=len(batch), attempts=1 + len(_RETRY_DELAYS), error=str(error))


def writer_from_settings(settings: Any) -> RequestLogWriter:
    return RequestLogWriter(
        mode=str(getattr(settings, "request_log_mode", "batched")).lower(),
        batch_size=getattr(settings, "request_log_batch_size", 200),
        flush_interval=getattr(settings, "request_log_flush_interval", 0.25),
        queue_size=getattr(settings, "request_log_queue_size", 20000),
    )

