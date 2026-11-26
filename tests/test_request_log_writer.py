"""Request-log writes: inline (the default), batched with a bounded queue, or off."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.database as database
from app.db_models import RequestLogModel
from app.request_log_writer import RequestLogWriter, writer_from_settings


def _entry(i=0, status=200):
    return SimpleNamespace(
        request_id=f"r{i}", method="GET", path="/api/x", client_ip="10.0.0.1", status_code=status,
        response_time_ms=3.4, user_agent="t", error=None, protocol="http",
    )


@pytest.fixture
def session_factory(db_session, monkeypatch):
    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    return factory


async def _count(factory):
    async with factory() as s:
        return (await s.execute(select(func.count()).select_from(RequestLogModel))).scalar_one()


async def test_off_writes_nothing(session_factory):
    w = RequestLogWriter(mode="off")
    await w.start()
    w.submit(_entry(), None)
    await asyncio.sleep(0.05)
    await w.stop()
    assert await _count(session_factory) == 0 and w.written == 0


async def test_inline_writes_one_row_per_request(session_factory):
    w = RequestLogWriter(mode="inline")
    for i in range(5):
        w.submit(_entry(i), None)
    await asyncio.sleep(0.3)
    assert await _count(session_factory) == 5 and w.written == 5


async def test_batched_writes_in_batches_and_flushes_on_stop(session_factory):
    w = RequestLogWriter(mode="batched", batch_size=10, flush_interval=0.05, queue_size=1000)
    await w.start()
    for i in range(25):
        w.submit(_entry(i), None)
    for _ in range(100):                              # a slow runner can need more than a few flush intervals
        if w.written + w.failed >= 25:                # the writer's own count: reading the table here would hold its lock
            break
        await asyncio.sleep(0.05)
    assert await _count(session_factory) == 25
    for i in range(25, 33):
        w.submit(_entry(i), None)
    await w.stop()                                    # rows still queued are written on the way out
    assert await _count(session_factory) == 33 and w.written == 33 and w.dropped == 0


async def test_batched_queue_is_bounded_and_counts_drops(session_factory):
    w = RequestLogWriter(mode="batched", batch_size=5, flush_interval=5.0, queue_size=3)
    w._queue = asyncio.Queue(maxsize=3)               # writer not started: nothing drains
    for i in range(10):
        w.submit(_entry(i), None)
    assert w.dropped == 7 and w._queue.qsize() == 3


async def test_batched_survives_a_failing_database(monkeypatch):
    class Boom:
        def __call__(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(database, "AsyncSessionLocal", Boom())
    monkeypatch.setattr("app.request_log_writer._RETRY_DELAYS", (0.01, 0.01))
    w = RequestLogWriter(mode="batched", batch_size=2, flush_interval=0.02, queue_size=10)
    await w.start()
    for i in range(4):
        w.submit(_entry(i), None)
    await asyncio.sleep(0.3)
    await w.stop()
    assert w.failed == 4 and w.written == 0


def test_settings_map_to_a_writer_and_unknown_mode_falls_back():
    s = SimpleNamespace(request_log_mode="BATCHED", request_log_batch_size=7, request_log_flush_interval=0.5,
                        request_log_queue_size=9)
    w = writer_from_settings(s)
    assert (w.mode, w.batch_size, w.flush_interval, w.queue_size) == ("batched", 7, 0.5, 9)
    assert RequestLogWriter(mode="nonsense").mode == "inline"


def test_sqlite_pragmas_come_from_the_settings_and_ignore_bad_values():
    cfg = SimpleNamespace(sqlite_journal_mode="wal", sqlite_synchronous="normal", sqlite_busy_timeout_ms=7000)
    assert database.sqlite_pragmas(cfg) == [
        "PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA busy_timeout=7000"]
    bad = SimpleNamespace(sqlite_journal_mode="; DROP TABLE x", sqlite_synchronous="sometimes", sqlite_busy_timeout_ms=0)
    assert database.sqlite_pragmas(bad) == []
    assert database.sqlite_pragmas(SimpleNamespace(sqlite_journal_mode="", sqlite_synchronous="",
                                                   sqlite_busy_timeout_ms=0)) == []


def test_sqlite_pragmas_are_applied_to_new_connections(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine

    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    database.install_sqlite_pragmas(
        eng, ["PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL", "PRAGMA busy_timeout=7000"])

    async def read():
        async with eng.connect() as conn:
            return {n: (await conn.exec_driver_sql(f"PRAGMA {n}")).scalar()
                    for n in ("journal_mode", "synchronous", "busy_timeout")}

    got = asyncio.run(read())
    asyncio.run(eng.dispose())
    assert got == {"journal_mode": "wal", "synchronous": 1, "busy_timeout": 7000}


# ---- in-process API key cache ------------------------------------------------------------
def _local_auth(monkeypatch, ttl):
    import app.middleware.auth as auth

    monkeypatch.setattr(auth.settings, "api_key_local_cache_ttl_seconds", ttl, raising=False)
    monkeypatch.setattr(auth.settings, "redis_enabled", False, raising=False)
    return auth.AuthMiddleware()


def _db_key(**over):
    from datetime import datetime

    base = dict(id="k1", name="n", rate_limit_per_minute=5, rate_limit_per_hour=50, allowed_paths=[],
                expires_at=None, is_active=True, created_at=datetime(2026, 1, 1), key_metadata={})
    base.update(over)
    return SimpleNamespace(**base)


def test_local_key_cache_is_off_by_default(monkeypatch):
    mw = _local_auth(monkeypatch, 0)
    mw._cache_put("h", _db_key())
    assert mw._cache_get("h") is None and mw._local_cache == {}


def test_local_key_cache_serves_until_it_expires_or_is_invalidated(monkeypatch):
    import app.api_key_cache as akc
    import app.middleware.auth as auth

    mw = _local_auth(monkeypatch, 5)
    mw._cache_put("h1", _db_key())
    hit = mw._cache_get("h1")
    assert hit and hit["id"] == "k1" and hit["is_active"] is True
    akc.invalidate("h1")                                   # the admin API does this on revoke
    assert mw._cache_get("h1") is None
    mw._cache_put("h2", _db_key())
    real = auth.time.monotonic
    monkeypatch.setattr(auth.time, "monotonic", lambda: real() + 10)
    assert mw._cache_get("h2") is None and "h2" not in mw._local_cache


def test_local_key_cache_never_outlives_the_key(monkeypatch):
    from datetime import timedelta

    from app.time_utils import utcnow

    mw = _local_auth(monkeypatch, 300)
    mw._cache_put("h", _db_key(expires_at=utcnow() + timedelta(seconds=2)))
    expires_at = mw._local_cache["h"][0]
    import time
    assert expires_at - time.monotonic() <= 2.5


# ---- startup race and AI switch --------------------------------------------------------------
async def test_init_db_retries_when_another_worker_creates_the_tables_first(monkeypatch):
    calls = {"n": 0}

    class FakeConn:
        async def run_sync(self, fn):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("(sqlite3.OperationalError) table api_keys already exists")

    class FakeBegin:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(database.engine.__class__, "begin", lambda self: FakeBegin(), raising=False)
    await database.init_db()
    assert calls["n"] == 3


async def test_init_db_raises_other_errors(monkeypatch):
    class Boom:
        async def __aenter__(self):
            raise RuntimeError("disk I/O error")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(database.engine.__class__, "begin", lambda self: Boom(), raising=False)
    with pytest.raises(RuntimeError, match="disk I/O"):
        await database.init_db()


def test_ai_analytics_can_be_switched_off(client, monkeypatch):
    import app.main as main

    seen = []
    monkeypatch.setattr(main.ai_service, "process_request", lambda data: seen.append(data) or {})
    monkeypatch.setattr(main.settings, "ai_analytics_enabled", False)
    client.get("/health")
    assert seen == []
    monkeypatch.setattr(main.settings, "ai_analytics_enabled", True)
    client.get("/health")
    assert len(seen) >= 1


async def test_tenant_lookup_is_cached_only_when_the_local_ttl_is_on(monkeypatch):
    import app.middleware.auth as auth
    from app.models import APIKey
    from app.repositories import tenant_repo

    calls = {"n": 0}
    tenant = SimpleNamespace(tenant_id="t1", is_active=True)

    async def fake_lookup(db, api_key):
        calls["n"] += 1
        return tenant

    monkeypatch.setattr(tenant_repo.TenantRepository, "get_tenant_for_api_key", staticmethod(fake_lookup))
    monkeypatch.setattr(tenant_repo.TenantRepository, "to_dict", staticmethod(lambda t: {"tenant_id": t.tenant_id}))

    def request():
        return SimpleNamespace(
            headers={auth.settings.api_key_header: "sk-test"}, state=SimpleNamespace(), method="GET",
            url=SimpleNamespace(path="/api/x"), client=SimpleNamespace(host="10.0.0.1"),
        )

    for ttl, expected_calls in ((0, 3), (30, 1)):
        mw = _local_auth(monkeypatch, ttl)
        principal = APIKey(key="sk-test", name="n", rate_limit_per_minute=5, rate_limit_per_hour=50, is_active=True)

        async def fake_validate(api_key, db=None, _p=principal):
            return _p

        monkeypatch.setattr(mw, "_validate_api_key", fake_validate)
        calls["n"] = 0
        for _ in range(3):
            req = request()
            await mw.authenticate(req, db=object())
            assert req.state.tenant_id == "t1"
        assert calls["n"] == expected_calls


async def test_a_failed_batch_is_retried_and_then_written(session_factory, monkeypatch):
    monkeypatch.setattr("app.request_log_writer._RETRY_DELAYS", (0.01, 0.01))
    calls = {"n": 0}
    real = database.AsyncSessionLocal

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("database is locked")
        return real()

    monkeypatch.setattr(database, "AsyncSessionLocal", flaky)
    w = RequestLogWriter(mode="batched", batch_size=10, flush_interval=0.02, queue_size=100)
    await w.start()
    for i in range(3):
        w.submit(_entry(i), None)
    await w.stop()
    assert calls["n"] == 3 and w.written == 3 and w.failed == 0
    assert await _count(session_factory) == 3


async def test_stop_writes_the_batch_it_was_building_instead_of_dropping_it(session_factory):
    w = RequestLogWriter(mode="batched", batch_size=100, flush_interval=30.0, queue_size=100)
    await w.start()
    for i in range(7):
        w.submit(_entry(i), None)
    await asyncio.sleep(0.05)            # the writer has taken them off the queue and is waiting to fill the batch
    await w.stop()
    assert w.written == 7 and w.dropped == 0 and await _count(session_factory) == 7


async def test_stop_on_a_full_queue_falls_back_to_cancel_and_drain(session_factory):
    w = RequestLogWriter(mode="batched", batch_size=5, flush_interval=0.01, queue_size=3)
    await w.start()
    w._queue.put_nowait((_entry(0), None))
    w._queue.put_nowait((_entry(1), None))
    w._queue.put_nowait((_entry(2), None))
    await w.stop()
    assert await _count(session_factory) == 3


def test_defaults_avoid_a_database_commit_per_request():
    """Shipped defaults: batched request log, SQLite in WAL with NORMAL sync, key use throttled."""
    from app.config import Settings
    s = Settings(_env_file=None)
    assert s.request_log_mode == "batched"
    assert (s.sqlite_journal_mode, s.sqlite_synchronous, s.sqlite_busy_timeout_ms) == ("WAL", "NORMAL", 5000)
    assert s.api_key_last_used_interval_seconds == 60.0
    assert writer_from_settings(SimpleNamespace()).mode == "batched"
