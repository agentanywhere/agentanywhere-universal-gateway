"""SECRET_KEY as a data-plane API key: allowed by default, but always visible.

Release one of two: ALLOW_SECRET_KEY_API_KEY defaults to true, and every proxy
request authenticated with SECRET_KEY is logged as a warning, counted and
written to the governance audit log, so real use can be measured before the
default is switched off.
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app import metrics
from app.config import get_settings
from app.db_models import GovernanceAuditLogModel
from app.middleware import auth as auth_module
from app.repositories.api_key_repo import APIKeyRepository


@pytest.fixture(autouse=True)
def _fresh_break_glass_windows():
    import app.main as main_mod

    main_mod.auth_middleware.__dict__.pop("_break_glass_windows", None)
    yield
    main_mod.auth_middleware.__dict__.pop("_break_glass_windows", None)


@pytest.fixture
def proxied(monkeypatch):
    import app.main as main_mod

    monkeypatch.setattr(main_mod.router, "route_request",
                        AsyncMock(return_value=httpx.Response(200, json={"ok": True})))


@pytest.fixture
async def file_db(tmp_path):
    """A file-backed database so concurrent writers get real, separate sessions."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.database import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def audit_db(file_db, monkeypatch):
    """Break-glass audit rows go to their own database, with a fresh audit log.

    The gateway writes them from a background task on the test client's own
    event loop, so they must not share the test's session.
    """
    import app.governance as governance

    monkeypatch.setattr(governance, "audit_log", governance.AuditLog())
    monkeypatch.setattr(auth_module, "_audit_session", file_db)
    return file_db


async def _audit_rows(sessions, expect=1):
    rows = []
    for _ in range(100):  # the audit write runs in a background task
        async with sessions() as db:
            rows = (await db.execute(
                select(GovernanceAuditLogModel).where(GovernanceAuditLogModel.action == "auth.secret_key_as_api_key")
            )).scalars().all()
        if len(rows) >= expect:
            return rows
        await asyncio.sleep(0.02)
    return rows


def test_default_is_off():
    assert type(get_settings()).model_fields["allow_secret_key_api_key"].default is False


@pytest.fixture
def break_glass_on(monkeypatch):
    monkeypatch.setattr(auth_module.settings, "allow_secret_key_api_key", True)


@pytest.mark.asyncio
async def test_use_is_allowed_counted_and_audited(break_glass_on, client, proxied, audit_db):
    secret = get_settings().secret_key
    before = metrics.secret_key_api_key_uses._value.get()

    with patch.object(auth_module.logger, "warning") as warn:
        r = client.get("/api/v1/break-glass-probe", headers={"X-API-Key": secret, "User-Agent": "probe/1"})
    assert r.status_code == 200

    assert metrics.secret_key_api_key_uses._value.get() == before + 1
    warned = [c for c in warn.call_args_list if c.args and c.args[0] == "secret_key_used_as_api_key"]
    assert warned and warned[0].kwargs["path"] == "/api/v1/break-glass-probe"
    assert secret not in json.dumps(warned[0].kwargs, default=str)

    rows = await _audit_rows(audit_db)
    assert len(rows) == 1
    row = rows[0]
    assert row.actor == "secret-key" and row.resource_id == "/api/v1/break-glass-probe"
    assert row.user_agent == "probe/1" and row.outcome == "success"
    serialised = json.dumps({c.name: getattr(row, c.key) for c in GovernanceAuditLogModel.__table__.columns}, default=str)
    assert secret not in serialised


@pytest.mark.asyncio
async def test_switched_off_it_is_refused(client, proxied, audit_db, monkeypatch):
    monkeypatch.setattr(auth_module.settings, "allow_secret_key_api_key", False)
    r = client.get("/api/v1/break-glass-probe", headers={"X-API-Key": get_settings().secret_key})
    assert r.status_code == 401
    assert await _audit_rows(audit_db, expect=99) == []  # waits out the poll, then expects none


@pytest.mark.asyncio
async def test_real_api_keys_are_not_audited_as_break_glass(client, proxied, audit_db, db_session):
    raw, _ = await APIKeyRepository.create(db_session, name="normal key")
    before = metrics.secret_key_api_key_uses._value.get()
    assert client.get("/api/v1/break-glass-probe", headers={"X-API-Key": raw}).status_code == 200
    assert metrics.secret_key_api_key_uses._value.get() == before
    assert await _audit_rows(audit_db, expect=99) == []  # waits out the poll, then expects none


# --------------------------------------------------------------------------- chain integrity

@pytest.mark.asyncio
async def test_concurrent_audit_writes_keep_one_chain(file_db):
    """20 writers, each with its own session, at once: one valid chain of 20."""
    from app.governance import AuditEntry, AuditLog

    log = AuditLog()

    async def write(i):
        async with file_db() as db:
            await log.record(db, AuditEntry(action="auth.secret_key_as_api_key", actor="secret-key",
                                            resource_id=f"/p/{i}"))

    await asyncio.gather(*(write(i) for i in range(20)))
    async with file_db() as db:
        result = await log.verify_chain(db)
    assert result == {"valid": True, "checked": 20, "broken_at": None}


@pytest.mark.asyncio
async def test_concurrent_break_glass_requests_keep_one_chain(file_db, monkeypatch):
    """The real break-glass path: 20 requests in flight, 20 rows, chain valid."""
    from types import SimpleNamespace

    import app.governance as governance
    import app.main as main_mod

    monkeypatch.setattr(governance, "audit_log", governance.AuditLog())
    monkeypatch.setattr(auth_module, "_audit_session", file_db)

    def request(i):
        return SimpleNamespace(
            method="GET", url=SimpleNamespace(path=f"/api/v1/p{i}"),
            headers={"user-agent": "load/1"}, client=SimpleNamespace(host="10.0.0.9"),
            state=SimpleNamespace(request_id=f"r{i}"),
        )

    for i in range(20):
        main_mod.auth_middleware._record_break_glass_use(request(i))
    assert len(auth_module._background_tasks) == 20  # held until done
    await asyncio.gather(*list(auth_module._background_tasks))
    assert not auth_module._background_tasks

    async with file_db() as db:
        result = await governance.audit_log.verify_chain(db)
    assert result == {"valid": True, "checked": 20, "broken_at": None}


@pytest.mark.asyncio
async def test_one_audit_row_per_client_and_path_per_minute(break_glass_on, client, proxied, audit_db, monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(auth_module, "_now", lambda: clock["t"])
    secret = get_settings().secret_key
    before = metrics.secret_key_api_key_uses._value.get()

    for _ in range(5):
        assert client.get("/api/v1/chatty", headers={"X-API-Key": secret}).status_code == 200
    rows = await _audit_rows(audit_db)
    assert len(rows) == 1 and rows[0].audit_metadata["uses_since_last_entry"] == 0
    assert metrics.secret_key_api_key_uses._value.get() == before + 5  # every use counted

    clock["t"] += 61
    assert client.get("/api/v1/chatty", headers={"X-API-Key": secret}).status_code == 200
    rows = await _audit_rows(audit_db, expect=2)
    assert len(rows) == 2
    assert sorted(r.audit_metadata["uses_since_last_entry"] for r in rows) == [0, 4]
