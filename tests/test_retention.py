"""Retention: old request logs and per-minute usage rows are pruned; nothing else."""
from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.db_models import GovernanceAuditLogModel, RequestLogModel, TenantModel, TenantUsageModel
from app.retention import prune_once
from app.time_utils import utcnow


async def _count(db, model, *where):
    q = select(func.count()).select_from(model)
    for w in where:
        q = q.where(w)
    return int((await db.execute(q)).scalar())


def _log(i, age_days):
    return RequestLogModel(
        request_id=f"r{i}", method="GET", path="/x", client_ip="1.2.3.4",
        status_code=200, response_time_ms=1.0, timestamp=utcnow() - timedelta(days=age_days),
    )


@pytest.mark.asyncio
async def test_prunes_request_logs_past_the_retention_window(db_session):
    db_session.add_all([_log(1, 45), _log(2, 31), _log(3, 29), _log(4, 0)])
    await db_session.commit()
    with patch.object(get_settings(), "request_log_retention_days", 30):
        counts = await prune_once(db_session)
    assert counts["request_logs"] == 2
    assert await _count(db_session, RequestLogModel) == 2


@pytest.mark.asyncio
async def test_zero_keeps_request_logs_forever(db_session):
    db_session.add_all([_log(1, 400), _log(2, 1)])
    await db_session.commit()
    with patch.object(get_settings(), "request_log_retention_days", 0):
        counts = await prune_once(db_session)
    assert counts["request_logs"] == 0
    assert await _count(db_session, RequestLogModel) == 2


@pytest.mark.asyncio
async def test_only_old_minute_usage_rows_go(db_session):
    db_session.add(TenantModel(tenant_id="t", name="T", quota={}, is_active=True))
    now = utcnow()
    for window, age in (("minute", timedelta(days=2)), ("minute", timedelta(minutes=5)),
                        ("hour", timedelta(days=30)), ("day", timedelta(days=300))):
        db_session.add(TenantUsageModel(tenant_id="t", window=window, window_start=now - age, request_count=1))
    db_session.add(GovernanceAuditLogModel(action="x", outcome="success", entry_hash="h",
                                           timestamp=now - timedelta(days=900)))
    await db_session.commit()

    counts = await prune_once(db_session)
    assert counts["tenant_usage_minute"] == 1
    assert await _count(db_session, TenantUsageModel, TenantUsageModel.window == "minute") == 1
    assert await _count(db_session, TenantUsageModel, TenantUsageModel.window != "minute") == 2
    assert await _count(db_session, GovernanceAuditLogModel) == 1  # the audit log is never pruned


def test_defaults():
    s = get_settings()
    assert type(s).model_fields["request_log_retention_days"].default == 0
    assert type(s).model_fields["retention_interval_hours"].default == 24
