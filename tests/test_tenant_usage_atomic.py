"""Tenant usage counters are atomic, and the quota check is cheap."""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.db_models import TenantUsageModel
from app.repositories.tenant_repo import TenantRepository
from app.tenant import TenantService


@pytest_asyncio.fixture
async def maker(tmp_path):
    # 200 writers share one file; a slow CI runner can need longer than the driver's 5 s lock wait.
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'usage.db'}", connect_args={"timeout": 60}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    mk = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with mk() as s:
        await TenantRepository.create(
            s, "t1", "T1", quota={"requests_per_minute": 3, "requests_per_hour": 5}
        )
    yield mk
    await engine.dispose()


async def _rows(maker, tenant="t1"):
    async with maker() as s:
        res = await s.execute(
            select(TenantUsageModel).where(TenantUsageModel.tenant_id == tenant)
        )
        return list(res.scalars().all())


def _count_statements(maker):
    stmts = []
    engine = maker.kw["bind"]

    def hook(conn, cursor, statement, *a):
        stmts.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", hook)
    return stmts, lambda: event.remove(engine.sync_engine, "before_cursor_execute", hook)


async def _record(maker):
    async with maker() as s:
        await TenantService().record_request(s, "t1", bytes_out=2)


async def test_concurrent_record_request_loses_nothing(maker):
    await asyncio.gather(*[_record(maker) for _ in range(200)])
    rows = await _rows(maker)
    assert len(rows) == 3  # one per window: no duplicates from the first-request race
    for r in rows:
        assert r.request_count == 200
        assert r.bytes_out == 400


async def test_first_request_race_creates_one_row_per_window(maker):
    await asyncio.gather(*[_record(maker) for _ in range(20)])
    by_window = {}
    for r in await _rows(maker):
        by_window.setdefault(r.window, []).append(r)
    assert {w: len(v) for w, v in by_window.items()} == {"minute": 1, "hour": 1, "day": 1}


async def test_errors_and_partial_windows(maker):
    async with maker() as s:
        await TenantRepository.increment_usage(s, "t1", windows=["minute"], error_count=1)
    async with maker() as s:
        # hour row does not exist yet, minute does: mixed update + insert
        await TenantRepository.increment_usage(
            s, "t1", windows=["minute", "hour"], request_count=2, bytes_in=7
        )
    rows = {r.window: r for r in await _rows(maker)}
    assert (rows["minute"].request_count, rows["minute"].error_count, rows["minute"].bytes_in) == (3, 1, 7)
    assert (rows["hour"].request_count, rows["hour"].bytes_in) == (2, 7)


async def test_record_request_statement_count(maker):
    await _record(maker)  # create the rows
    stmts, stop = _count_statements(maker)
    try:
        await _record(maker)
    finally:
        stop()
    assert len(stmts) == 1 and stmts[0].lstrip().upper().startswith("UPDATE")


async def test_quota_enforced_at_minute_limit(maker):
    svc = TenantService()
    for i in range(3):
        async with maker() as s:
            assert await svc.check_quota(s, "t1", "minute") is True
        await _record(maker)
    async with maker() as s:
        assert await svc.check_quota(s, "t1", "minute") is False
        assert await svc.check_quota(s, "t1", "hour") is True


async def test_quota_enforced_at_hour_limit_with_prime(maker):
    svc = TenantService()
    async with maker() as s:
        await TenantRepository.increment_usage(s, "t1", windows=["hour"], request_count=5)
    async with maker() as s:
        tenant = {"tenant_id": "t1", "is_active": True,
                  "quota": {"requests_per_minute": 3, "requests_per_hour": 5}}
        await svc.prime_quota(s, tenant)
        assert await svc.check_quota(s, "t1", "minute") is True
        assert await svc.check_quota(s, "t1", "hour") is False
        svc.clear_quota_prime(s)
    # at the limit minus one is still allowed
    async with maker() as s:
        await TenantRepository.update_quota(s, "t1", {"requests_per_hour": 6})
        tenant["quota"]["requests_per_hour"] = 6
        await svc.prime_quota(s, tenant)
        assert await svc.check_quota(s, "t1", "hour") is True


async def test_quota_check_uses_one_query_and_none_when_unlimited(maker):
    svc = TenantService()
    tenant = {"tenant_id": "t1", "is_active": True,
              "quota": {"requests_per_minute": 3, "requests_per_hour": 5}}
    stmts, stop = _count_statements(maker)
    try:
        async with maker() as s:
            await svc.prime_quota(s, tenant)
            await svc.check_quota(s, "t1", "minute")
            await svc.check_quota(s, "t1", "hour")
            svc.clear_quota_prime(s)
        assert len(stmts) == 1

        stmts.clear()
        free = {"tenant_id": "t1", "is_active": True, "quota": {"requests_per_minute": 0}}
        async with maker() as s:
            await svc.prime_quota(s, free)
            assert await svc.check_quota(s, "t1", "minute") is True
            assert await svc.check_quota(s, "t1", "hour") is True
            svc.clear_quota_prime(s)
        assert stmts == []
    finally:
        stop()


async def test_inactive_tenant_denied_with_prime(maker):
    svc = TenantService()
    async with maker() as s:
        await svc.prime_quota(s, {"tenant_id": "t1", "is_active": False, "quota": {}})
        assert await svc.check_quota(s, "t1", "minute") is False
        svc.clear_quota_prime(s)
