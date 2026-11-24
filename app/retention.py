"""Retention for tables that grow with traffic.

Two tables would otherwise grow without bound:

* ``request_logs``: nearly every request writes a row. Rows older than
  ``REQUEST_LOG_RETENTION_DAYS`` are deleted. The default, 0, keeps them
  forever, so an upgrade never deletes logs nobody asked to delete.
* ``tenant_usage`` per-minute windows: one row per tenant per minute, needed
  only for the current minute's quota check. Rows older than a day are
  deleted; hourly and daily windows are kept.

The governance audit log and token usage rollups are never pruned here.
Pruning runs at start-up and then every ``RETENTION_INTERVAL_HOURS``. SQLite
reuses the freed pages; the file itself does not shrink without a VACUUM.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional

import structlog
from sqlalchemy import delete

from app.config import get_settings
from app.db_models import RequestLogModel, TenantUsageModel
from app.time_utils import utcnow

logger = structlog.get_logger()

MINUTE_USAGE_RETENTION = timedelta(days=1)


async def prune_once(db, now: Optional[datetime] = None) -> Dict[str, int]:
    """Delete expired rows; returns the number deleted per table."""
    now = now or utcnow()
    settings = get_settings()
    deleted = {"request_logs": 0, "tenant_usage_minute": 0}

    days = settings.request_log_retention_days
    if days and days > 0:
        result = await db.execute(
            delete(RequestLogModel).where(RequestLogModel.timestamp < now - timedelta(days=days))
        )
        deleted["request_logs"] = result.rowcount or 0

    result = await db.execute(
        delete(TenantUsageModel)
        .where(TenantUsageModel.window == "minute")
        .where(TenantUsageModel.window_start < now - MINUTE_USAGE_RETENTION)
    )
    deleted["tenant_usage_minute"] = result.rowcount or 0

    await db.commit()
    return deleted


async def retention_loop(session_factory=None) -> None:
    """Prune at start-up, then on an interval, until cancelled."""
    if session_factory is None:
        from app.database import AsyncSessionLocal as session_factory  # noqa: N813
    while True:
        try:
            async with session_factory() as db:
                counts = await prune_once(db)
            logger.info(
                "retention_pruned",
                request_log_retention_days=get_settings().request_log_retention_days,
                **counts,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("retention_prune_failed", error=str(exc))
        await asyncio.sleep(max(1, get_settings().retention_interval_hours) * 3600)
