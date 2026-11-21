"""Integration-style tests for app.main: auth middleware, rate limiting, error
rendering, ops endpoints, protocol endpoints and the proxy handler.

Backends are replaced with in-process fakes (monkeypatching the Router and the
protocol handlers); nothing here opens a socket, talks to a live server, or
reads a .env file.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient

import app.main as main
from app.agent_routes import AgentEdgeConfig
from app.database import get_db
from app.exceptions import ValidationError
from app.main import app
from app.repositories.api_key_repo import APIKeyRepository
from app.tenant import TenantService, tenant_manager
from app.versioning import VersionUnavailable

HTML = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def gwdb(override_get_db):
    """TestClient backed by the in-memory test database; renders 500s instead of re-raising."""
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def gw():
    """Like ``gwdb`` but with no database session at all (faster; for tests that
    never present a stored API key). The auth layer treats a missing session
    like an unreachable database."""
    async def _no_db():
        yield None

    app.dependency_overrides[get_db] = _no_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def key(db_session):
    api_key, _ = await APIKeyRepository.create(
        db_session, "cov-key", rate_limit_per_minute=1000, rate_limit_per_hour=10000
    )
    return api_key


@pytest.fixture
def H(key):
    return {"X-API-Key": key}


@pytest.fixture(autouse=True)
def _isolated_database(monkeypatch):
    """Keep the app's own lifespan and background tasks off the shared on-disk
    database (other test processes use it; lock waits made every test slow)."""
    import app.database as dbmod
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(dbmod, "AsyncSessionLocal", async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))
    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "close_db", AsyncMock())
    monkeypatch.setattr(main, "get_db", main.get_db)
    yield


@pytest.fixture(autouse=True)
def _restore_handlers(monkeypatch):
    """Protocol handlers are module globals; make sure none leaks between tests."""
    for name in ("mqtt_handler", "grpc_handler", "graphql_handler", "websocket_handler"):
        monkeypatch.setattr(main, name, getattr(main, name), raising=True)


def backend(monkeypatch, response=None, exc=None):
    calls = []

    async def route_request(**kw):
        calls.append(kw)
        if exc is not None:
            raise exc
        return response

    monkeypatch.setattr(main.router, "route_request", route_request)
    return calls


def jresp(status=200, body=None, headers=None):
    return httpx.Response(status, json=body if body is not None else {"ok": True}, headers=headers)


# ====================================================================== helpers
def test_safe_key_label_never_leaks_key():
    assert main.safe_key_label(None) is None
    assert main.safe_key_label(SimpleNamespace(id="abc", key="sk-secret")) == "abc"
    label = main.safe_key_label(SimpleNamespace(id=None, key="sk-secret"))
    assert label.startswith("key-") and len(label) == 16 and "secret" not in label
    assert main.safe_key_label(SimpleNamespace(id=None, key=None)) is None


def test_rate_limit_metric_key_is_bounded():
    req = lambda p: SimpleNamespace(state=SimpleNamespace(**({} if p is None else {"api_key": p})))
    assert main._rate_limit_metric_key(req(None)) == "anonymous"
    assert main._rate_limit_metric_key(req(SimpleNamespace(id="kid"))) == "kid"
    assert main._rate_limit_metric_key(req(SimpleNamespace(id=None))) == "non-database-key"


def test_rejected_path_label_bounds_cardinality():
    assert main._rejected_path_label("/mcp/tools") == "/mcp"
    assert main._rejected_path_label("/agents/x/y") == "/agents"
    assert main._rejected_path_label("/random/scanner/path") == "/other"
    assert main._rejected_path_label("") == "/other"


def test_iter_request_db_honours_override(override_get_db):
    app.dependency_overrides[get_db] = override_get_db
    try:
        assert main._iter_request_db().__class__.__name__ == "async_generator"
    finally:
        app.dependency_overrides.clear()
    assert main._iter_request_db() is not None


# ====================================================================== error rendering
def _req(headers):
    return SimpleNamespace(headers={k.lower(): v for k, v in headers.items()})


def test_client_prefers_html_rules():
    assert main._client_prefers_html(_req({"accept": HTML})) is True
    assert main._client_prefers_html(_req({})) is False
    assert main._client_prefers_html(_req({"accept": "application/json, text/html"})) is False
    assert main._client_prefers_html(_req({"accept": "text/html, application/json"})) is True
    assert main._client_prefers_html(_req({"accept": "text/html", "x-requested-with": "XMLHttpRequest"})) is False


def test_error_title():
    assert main._error_title(404, "AGENT_NOT_FOUND") == "Agent not found"
    assert main._error_title(429, "X") == "Too many requests"
    assert main._error_title(418, "X") == "Error"


def test_error_html_escapes_and_renders_blocks(monkeypatch):
    monkeypatch.setattr(app, "docs_url", None)
    page = main._error_html(429, {
        "error": "RATE_LIMIT_EXCEEDED", "message": "<script>alert(1)</script>",
        "request_id": "rid<1>", "details": {"hint": "slow <b>down</b>", "retry_after": 30},
    })
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "rid&lt;1&gt;" in page and "slow &lt;b&gt;down&lt;/b&gt;" in page
    assert 'id="cd">30<' in page and "retry in" in page      # live countdown
    assert 'href="/docs"' not in page                          # docs not served -> no button
    monkeypatch.setattr(app, "docs_url", "/docs")
    assert 'href="/docs"' in main._error_html(500, {"error": "E", "message": "m"})
    # bad / missing inputs degrade gracefully
    page = main._error_html(429, {"details": "not-a-dict"})
    assert "retry in" not in page and "The request could not be completed." in page
    assert "retry in" not in main._error_html(429, {"details": {"retry_after": "soon"}})
    assert "retry in" not in main._error_html(404, {"details": {"retry_after": 5}})


def test_render_error_response_json_vs_html():
    r = main.render_error_response(_req({}), 400, {"error": "X", "message": "m"}, {"Retry-After": "5"})
    assert r.status_code == 400 and r.headers["content-type"].startswith("application/json")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["retry-after"] == "5"
    r = main.render_error_response(_req({"accept": HTML}), 403, {"error": "X", "message": "m"})
    assert r.status_code == 403 and r.headers["content-type"].startswith("text/html")


def test_401_json_carries_hint_and_request_id(gw):
    r = gw.get("/some/protected/path", headers={"X-Request-ID": "req-123"})
    assert r.status_code == 401
    body = r.json()
    assert body["error"] == "AUTHENTICATION_ERROR" and body["request_id"] == "req-123"
    assert "X-API-Key" in body["details"]["hint"]
    assert r.headers["X-Request-ID"] == "req-123"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_validation_error_is_enveloped_422(gw):
    r = gw.get("/oauth2/authorize")  # missing required query params
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "VALIDATION_ERROR" and body["message"] == "Request validation failed"
    locs = {tuple(e["location"]) for e in body["details"]["errors"]}
    assert ("query", "provider") in locs and ("query", "redirect_uri") in locs
    assert "request_id" in body and "timestamp" in body


def test_validation_error_as_html_for_browsers(gw):
    r = gw.get("/oauth2/authorize", headers={"Accept": HTML})
    assert r.status_code == 422 and r.headers["content-type"].startswith("text/html")


def test_unhandled_exception_hides_internals(gw, monkeypatch):
    monkeypatch.setattr(main.oauth2_manager, "list_providers",
                        MagicMock(side_effect=RuntimeError("secret internal detail")))
    r = gw.get("/oauth2/providers")
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "INTERNAL_ERROR"
    assert "secret internal detail" not in json.dumps(body)
    assert body["request_id"]


def test_log_handled_error_levels(monkeypatch):
    seen = []
    fake = SimpleNamespace(
        error=lambda *a, **k: seen.append(("error", k)),
        warning=lambda *a, **k: seen.append(("warning", k)),
        info=lambda *a, **k: seen.append(("info", k)),
    )
    monkeypatch.setattr(main, "logger", fake)
    boom = main.metrics.error_count
    main._log_handled_error(None, 500, "INTERNAL_ERROR", "m", "r1", exc=RuntimeError("x"), internal_error="detail")
    main._log_handled_error(None, 429, "RATE", "m", "r2")
    main._log_handled_error(SimpleNamespace(url=SimpleNamespace(path="/p"), method="GET"), 404, "NF", "m", "r3")
    assert [s[0] for s in seen] == ["error", "warning", "info"]
    assert seen[0][1]["internal_error"] == "detail" and seen[2][1]["http.request.path"] == "/p"
    # a broken metric must never break error handling
    monkeypatch.setattr(main.metrics, "error_count", None)
    main._log_handled_error(None, 503, None, "m", "r4")
    monkeypatch.setattr(main.metrics, "error_count", boom)


# ====================================================================== public paths & ops
def test_public_endpoints_need_no_key(gw):
    assert gw.get("/health").json()["status"] == "healthy"
    assert gw.get("/live").json()["status"] == "alive"
    assert gw.get("/.well-known/ag-ui.json").json()["name"] == "AG-UI"
    card = gw.get("/.well-known/agent-card.json")
    assert card.status_code == 200 and isinstance(card.json(), dict)
    assert gw.get("/oauth2/providers").status_code == 200


def test_health_reports_version(gw):
    body = gw.get("/health").json()
    assert body["version"] == main.settings.app_version and body["services"] == {}


def test_logging_middleware_headers_and_traceparent(gw):
    tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    r = gw.get("/health", headers={"traceparent": tp, "X-Request-ID": "abc"})
    assert r.headers["X-Request-ID"] == "abc"
    assert float(r.headers["X-Response-Time"]) >= 0
    echoed = r.headers["traceparent"].split("-")
    assert echoed[1] == "a" * 32          # same trace id
    assert echoed[2] != "b" * 16          # new span for this hop
    r2 = gw.get("/health")                 # no inbound ids: generated
    assert len(r2.headers["X-Request-ID"]) >= 32 and r2.headers["traceparent"].startswith("00-")


def test_ready_ok_and_not_ready(gw, monkeypatch):
    from app.observability import dependency_probes as dp

    ok = SimpleNamespace(status="healthy", message="", to_dict=lambda: {"name": "database", "status": "healthy"})
    monkeypatch.setattr(dp, "check", AsyncMock(return_value=ok))
    r = gw.get("/ready")
    assert r.status_code == 200 and r.json() == {"status": "ready", "database": {"name": "database", "status": "healthy"}}
    bad = SimpleNamespace(status="unhealthy", message="db down", to_dict=lambda: {})
    monkeypatch.setattr(dp, "check", AsyncMock(return_value=bad))
    r = gw.get("/ready")
    assert r.status_code == 503 and r.json() == {"status": "not_ready", "reason": "db down"}
    monkeypatch.setattr(dp, "check", AsyncMock(return_value=None))
    r = gw.get("/ready")
    assert r.status_code == 503 and r.json()["reason"] == "no probe registered"


def test_health_detailed_aggregates_and_503_on_unhealthy(gw, monkeypatch):
    from app.observability import dependency_probes as dp

    probe = SimpleNamespace(to_dict=lambda: {"name": "cache", "status": "x"})
    monkeypatch.setattr(dp, "check_all", AsyncMock(return_value=[probe]))
    monkeypatch.setattr(dp, "aggregate", lambda results: "degraded")
    r = gw.get("/health/detailed")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded" and body["dependencies"] == [{"name": "cache", "status": "x"}]
    assert body["version"] == main.settings.app_version and body["checked_at"].endswith("Z")
    monkeypatch.setattr(dp, "aggregate", lambda results: "unhealthy")
    assert gw.get("/health/detailed").status_code == 503


def test_metrics_endpoint_and_disabled_switch(gw, monkeypatch):
    gw.get("/health")
    r = gw.get(main.settings.metrics_path)
    assert r.status_code == 200 and "gateway_requests_total" in r.text
    monkeypatch.setattr(main.settings, "metrics_enabled", False)
    r = gw.get(main.settings.metrics_path)
    assert r.status_code == 404 and r.json()["message"] == "Metrics disabled"


def test_ops_endpoints_admin_gate_applies_to_json_envelope(gw, monkeypatch):
    import admin.api as admin_api
    monkeypatch.setattr(main.settings, "ops_endpoints_require_admin", True)
    monkeypatch.setattr(admin_api.settings, "admin_token", "ops-cov-admin-token")
    assert gw.get("/metrics").status_code == 401
    ok = gw.get("/metrics", headers={"Authorization": "Bearer ops-cov-admin-token"})
    assert ok.status_code == 200
    # an API key is not an admin credential
    assert gw.get("/metrics", headers={"X-API-Key": "ops-cov-admin-token"}).status_code == 401
    # health and liveness stay open
    assert gw.get("/health").status_code == 200


def test_docs_paths_404_when_debug_off_and_open_when_on(gw, monkeypatch):
    monkeypatch.setattr(app, "docs_url", None)
    monkeypatch.setattr(app, "openapi_url", None)
    for p in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
        r = gw.get(p)
        assert r.status_code == 404, p
        assert r.json()["error"] == "NOT_FOUND" and "DEBUG=true" in r.json()["message"]
    # With docs configured the middleware lets the request through without a key
    # (status then depends on whether FastAPI actually serves the path).
    monkeypatch.setattr(app, "docs_url", "/docs")
    assert gw.get("/docs").json().get("error") != "NOT_FOUND" if gw.get("/docs").headers.get(
        "content-type", "").startswith("application/json") else True


# ====================================================================== API-key auth
def test_options_preflight_bypasses_auth(gw):
    r = gw.options("/some/protected/path", headers={
        "Origin": "https://app.example.com", "Access-Control-Request-Method": "GET"})
    # The CORS layer answers preflights itself (400 for an origin that is not
    # allowed); what matters is that the API-key check never turns it into a 401.
    assert r.status_code in (200, 400)


def test_options_without_cors_headers_returns_gateway_preflight(gw):
    r = gw.options("/mcp/anything")
    assert r.status_code == 200
    assert r.headers["access-control-allow-methods"].startswith("GET, POST")


def test_missing_and_invalid_keys_rejected(gw):
    assert gw.get("/mcp/t").status_code == 401
    r = gw.get("/mcp/t", headers={"X-API-Key": "sk-invalid"})
    assert r.status_code == 401 and r.json()["error"] == "AUTHENTICATION_ERROR"
    assert "Invalid or inactive" in r.json()["message"]
    r = gw.get("/mcp/t", headers={"Authorization": "Bearer sk-invalid"})
    assert r.status_code == 401


def test_valid_key_via_x_api_key_and_authorization_bearer(gwdb, monkeypatch, key):
    calls = backend(monkeypatch, jresp(200, {"hello": "x"}))
    r1 = gwdb.get("/mcp/t", headers={"X-API-Key": key})
    r2 = gwdb.get("/mcp/t", headers={"Authorization": f"Bearer {key}"})
    assert r1.status_code == r2.status_code == 200
    assert r1.json() == {"hello": "x"}
    assert len(calls) == 2


def test_admin_and_oauth_prefixes_skip_api_key_check(gw):
    # /admin has its own auth: the gateway layer must not answer with its API-key 401.
    r = gw.get("/admin/does-not-exist")
    assert r.json().get("error") != "AUTHENTICATION_ERROR" or "API key required" not in r.json().get("message", "")
    assert gw.get("/oauth2/providers").status_code == 200


def test_break_glass_secret_key_authenticates(gw, monkeypatch):
    from app.middleware import auth as auth_mod
    monkeypatch.setattr(auth_mod.settings, "secret_key", "break-glass-operator-secret-0001")
    monkeypatch.setattr(auth_mod.settings, "allow_secret_key_api_key", True)
    backend(monkeypatch, jresp())
    r = gw.get("/mcp/t", headers={"X-API-Key": "break-glass-operator-secret-0001"})
    assert r.status_code == 200
    assert gw.get("/mcp/t", headers={"X-API-Key": "break-glass-operator-secret-0002"}).status_code == 401


@pytest_asyncio.fixture
async def scoped_key(db_session):
    k, _ = await APIKeyRepository.create(db_session, "scoped", allowed_paths=["/mcp"])
    return k


def test_scoped_key_enforced(gwdb, monkeypatch, scoped_key):
    backend(monkeypatch, jresp())
    assert gwdb.get("/mcp/ok", headers={"X-API-Key": scoped_key}).status_code == 200
    r = gwdb.get("/other/path", headers={"X-API-Key": scoped_key})
    assert r.status_code == 403
    body = r.json()
    assert body["error"] == "AUTHORIZATION_ERROR"
    assert body["details"]["allowed_paths"] == ["/mcp"]


def test_plugin_auth_provider_short_circuits_and_failure_falls_back(gw, monkeypatch):
    backend(monkeypatch, jresp())
    monkeypatch.setattr(main.plugin_registry, "authenticate", lambda ctx: {"user": "plug"})
    r = gw.get("/mcp/t")
    assert r.status_code == 200      # no key needed: a plugin authenticated the caller
    monkeypatch.setattr(main.plugin_registry, "authenticate", MagicMock(side_effect=RuntimeError("bad plugin")))
    assert gw.get("/mcp/t").status_code == 401   # provider crash must not open the door
    monkeypatch.setattr(main.plugin_registry, "authenticate", lambda ctx: None)
    assert gw.get("/mcp/t").status_code == 401
    monkeypatch.setattr(main.settings, "plugins_enabled", False)
    monkeypatch.setattr(main.plugin_registry, "authenticate", lambda ctx: {"user": "plug"})
    assert gw.get("/mcp/t").status_code == 401   # ignored when plugins are disabled


# ====================================================================== rate limiting
@pytest_asyncio.fixture
async def tiny_key(db_session):
    k, _ = await APIKeyRepository.create(db_session, "tiny", rate_limit_per_minute=2, rate_limit_per_hour=1000)
    return k


@pytest_asyncio.fixture
async def hourly_key(db_session):
    k, _ = await APIKeyRepository.create(db_session, "hourly", rate_limit_per_minute=1000, rate_limit_per_hour=1)
    return k


def test_per_minute_limit_returns_429_with_retry_after(gwdb, monkeypatch, tiny_key):
    from app.middleware import rate_limit as rl
    monkeypatch.setattr(rl.settings, "rate_limit_enabled", True)
    backend(monkeypatch, jresp())
    h = {"X-API-Key": tiny_key}
    assert gwdb.get("/mcp/t", headers=h).status_code == 200
    assert gwdb.get("/mcp/t", headers=h).status_code == 200
    r = gwdb.get("/mcp/t", headers=h)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "60"
    body = r.json()
    assert body["error"] == "RATE_LIMIT_EXCEEDED"
    assert r.headers["X-Request-ID"]            # rejected requests are still traced
    # the limit is per key: another key is unaffected
    assert gwdb.get("/mcp/t", headers={"X-API-Key": "bad"}).status_code == 401


def test_429_rendered_as_html_for_browsers(gwdb, monkeypatch, tiny_key):
    from app.middleware import rate_limit as rl
    monkeypatch.setattr(rl.settings, "rate_limit_enabled", True)
    backend(monkeypatch, jresp())
    h = {"X-API-Key": tiny_key, "Accept": HTML}
    gwdb.get("/mcp/t", headers=h)
    gwdb.get("/mcp/t", headers=h)
    r = gwdb.get("/mcp/t", headers=h)
    assert r.status_code == 429 and r.headers["content-type"].startswith("text/html")
    assert "Too many requests" in r.text and r.headers["Retry-After"] == "60"


def test_hourly_limit(gwdb, monkeypatch, hourly_key):
    from app.middleware import rate_limit as rl
    monkeypatch.setattr(rl.settings, "rate_limit_enabled", True)
    backend(monkeypatch, jresp())
    h = {"X-API-Key": hourly_key}
    assert gwdb.get("/mcp/t", headers=h).status_code == 200
    r = gwdb.get("/mcp/t", headers=h)
    assert r.status_code == 429 and r.headers["Retry-After"] == "3600"


def test_rate_limit_disabled_never_throttles(gwdb, monkeypatch, tiny_key):
    from app.middleware import rate_limit as rl
    monkeypatch.setattr(rl.settings, "rate_limit_enabled", False)
    backend(monkeypatch, jresp())
    for _ in range(5):
        assert gwdb.get("/mcp/t", headers={"X-API-Key": tiny_key}).status_code == 200


def test_429_counted_in_rate_limit_metric(gwdb, monkeypatch, tiny_key):
    from app.middleware import rate_limit as rl
    monkeypatch.setattr(rl.settings, "rate_limit_enabled", True)
    backend(monkeypatch, jresp())
    h = {"X-API-Key": tiny_key}
    for _ in range(3):
        gwdb.get("/mcp/t", headers=h)
    text = gwdb.get("/metrics").text
    assert "gateway_rate_limit_hits_total" in text


# ====================================================================== tenants
@pytest_asyncio.fixture
async def tenant_key(db_session):
    k, row = await APIKeyRepository.create(db_session, "tenant-key")
    svc = TenantService()
    await svc.create_tenant(db_session, "cov-gw-tenant", "Cov", quota={"requests_per_minute": 2, "requests_per_hour": 100})
    await svc.associate_api_key(db_session, "cov-gw-tenant", row.id, k)
    yield k
    await svc.delete(db_session, "cov-gw-tenant")


def test_tenant_resolved_header_and_quota_429(gwdb, monkeypatch, db_session, tenant_key):
    backend(monkeypatch, jresp())
    h = {"X-API-Key": tenant_key}
    r = gwdb.get("/mcp/t", headers=h)
    assert r.status_code == 200 and r.headers["X-Tenant-ID"] == "cov-gw-tenant"
    # usage is recorded by a background task; seed it deterministically instead
    loop = asyncio.new_event_loop()
    try:
        svc = TenantService()
        loop.run_until_complete(svc.record_request(db_session, "cov-gw-tenant"))
        loop.run_until_complete(svc.record_request(db_session, "cov-gw-tenant"))
    finally:
        loop.close()
    r = gwdb.get("/mcp/t", headers=h)
    assert r.status_code == 429
    body = r.json()
    assert body["error"] == "RATE_LIMIT_EXCEEDED"
    assert body["details"]["tenant_id"] == "cov-gw-tenant"
    assert body["details"]["quota"]["requests_per_minute"] == 2
    assert r.headers["Retry-After"] == "60"


def test_tenant_lookup_failure_does_not_block_request(gwdb, monkeypatch, tenant_key):
    import app.tenant as tenant_mod
    backend(monkeypatch, jresp())
    monkeypatch.setattr(tenant_mod.tenant_service, "get_tenant_for_api_key",
                        AsyncMock(side_effect=RuntimeError("tenant db down")))
    assert gwdb.get("/mcp/t", headers={"X-API-Key": tenant_key}).status_code == 200


def test_tenant_quota_message_when_hour_window_is_exhausted(gwdb, monkeypatch, tenant_key):
    import app.tenant as tenant_mod

    async def check(db, tid, window="minute", cost=1):
        return window != "hour"

    backend(monkeypatch, jresp())
    monkeypatch.setattr(tenant_mod.tenant_service, "check_quota", check)
    r = gwdb.get("/mcp/t", headers={"X-API-Key": tenant_key})
    assert r.status_code == 429 and r.headers["Retry-After"] == "3600"


# ====================================================================== legacy-auth fallback
def test_db_failure_falls_back_to_break_glass_key(monkeypatch):
    from app.middleware import auth as auth_mod

    async def broken_db():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = broken_db
    monkeypatch.setattr(auth_mod.settings, "secret_key", "break-glass-operator-secret-0003")
    monkeypatch.setattr(auth_mod.settings, "allow_secret_key_api_key", True)
    backend(monkeypatch, jresp())
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            assert c.get("/mcp/t", headers={"X-API-Key": "break-glass-operator-secret-0003"}).status_code == 200
            r = c.get("/mcp/t")
            assert r.status_code == 401 and r.json()["error"] == "AUTHENTICATION_ERROR"
            r = c.get("/mcp/t", headers={"X-API-Key": "wrong"})
            assert r.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_db_failure_then_auth_crash_is_internal_error(monkeypatch):
    async def broken_db():
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = broken_db
    monkeypatch.setattr(main.auth_middleware, "authenticate", AsyncMock(side_effect=RuntimeError("kaboom")))
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            r = c.get("/mcp/t", headers={"X-API-Key": "k"})
            assert r.status_code == 500
            assert "kaboom" not in r.text
    finally:
        app.dependency_overrides.clear()


# ------------------------------------------------- repeated query keys
@pytest.fixture
def sent(monkeypatch):
    """The real router with a fake backend; yields the query of each request sent."""
    from app.transformers import transformation_manager

    queries = []

    def answer(request):
        queries.append(request.url.query.decode())
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        main.router, "client", httpx.AsyncClient(transport=httpx.MockTransport(answer))
    )
    monkeypatch.setattr(transformation_manager, "rules_by_id", {})
    return queries


def test_a_repeated_query_key_reaches_the_backend_whole(gwdb, sent, H):
    assert gwdb.get("/mcp/t?tag=a&tag=b&one=1&tag=c", headers=H).status_code == 200
    assert sent == ["tag=a&tag=b&tag=c&one=1"]


def test_a_query_without_repeats_is_sent_as_before(gwdb, sent, H):
    gwdb.get("/mcp/t?one=1&blank=&two=a%20b", headers=H)
    assert sent == ["one=1&blank=&two=a%20b"]


def test_a_repeated_key_given_the_same_value_twice_keeps_both(gwdb, sent, H):
    gwdb.get("/mcp/t?tag=a&tag=a", headers=H)
    assert sent == ["tag=a&tag=a"]


def test_a_rule_that_sets_or_removes_a_repeated_key_wins(gwdb, sent, H):
    from app.transformers import TransformationRule, transformation_manager

    rule = TransformationRule.from_dict({
        "name": "query", "path_pattern": "**",
        "request_config": {"query_set": {"tag": "z"}, "query_remove": ["drop"]},
    })
    transformation_manager.rules_by_id[rule.id] = rule
    gwdb.get("/mcp/t?tag=a&tag=b&drop=1&drop=2&keep=x&keep=y", headers=H)
    assert sent == ["tag=z&keep=x&keep=y"]


def test_repeated_query_lists_only_keys_given_more_than_once():
    from starlette.datastructures import QueryParams
    from app.routing import _with_repeats, repeated_query

    repeated = repeated_query(QueryParams("a=1&b=2&a=3"))
    assert repeated == {"a": ["1", "3"]}
    assert _with_repeats({"a": "3", "b": "2"}, repeated) == {"a": ["1", "3"], "b": "2"}
    assert _with_repeats({"a": "9", "b": "2"}, repeated) == {"a": "9", "b": "2"}
    assert _with_repeats({"b": "2"}, repeated) == {"b": "2"}
    assert _with_repeats({"b": "2"}, None) == {"b": "2"}
    assert repeated_query({"a": "1"}) == {}

# ------------------------------------------------- escaped ?, # and control characters in a path
@pytest.fixture
def asked(monkeypatch):
    """The real router with a fake backend; yields the path and query of each request sent."""
    from app.transformers import transformation_manager

    seen = []

    def answer(request):
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(main.router, "client", httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    monkeypatch.setattr(transformation_manager, "rules_by_id", {})
    return seen


@pytest.mark.parametrize("path", ["/mcp/a%3Fadmin=1", "/mcp/x%3F%2F..%2F..%2Fadmin", "/mcp/a%23frag/b", "/mcp/a%09b", "/mcp/a%0Ab", "/mcp/a%7Fb",
                                  "/admin%3F/api-keys", "/health%23"])
def test_a_path_that_decodes_to_a_delimiter_is_refused_before_anything_reads_it(gwdb, asked, H, path):
    r = gwdb.get(path + "?x=1", headers={**H, "X-Request-ID": "rid-p"})
    assert r.status_code == 400
    assert r.json()["error"] == "INVALID_PATH" and r.json()["request_id"] == "rid-p"
    assert asked == []


def test_options_and_agents_are_refused_the_same(gw, asked, monkeypatch):
    monkeypatch.setattr(main, "agent_edge_config", AgentEdgeConfig({"demo": "http://10.0.0.5:9000"}))
    assert gw.get("/agents/demo/run%3Fadmin=1").status_code == 400
    assert gw.options("/mcp/a%3Fb").status_code == 400
    assert asked == []


def test_a_path_without_them_is_forwarded_as_before(gwdb, asked, H):
    for path in ("/mcp/a%20b", "/mcp/caf%C3%A9", "/mcp/~a", "/mcp/a%3Db"):
        assert gwdb.get(path, headers=H).status_code == 200
    assert asked == ["/mcp/a%20b", "/mcp/caf%C3%A9", "/mcp/~a", "/mcp/a=b"]


def test_breaks_out_of_the_path_names_only_those_characters():
    from app.main import _breaks_out_of_the_path

    assert not any(_breaks_out_of_the_path(p) for p in ("/a b/caf\u00e9/%41", "/a=b&c;d", "/"))
    assert all(_breaks_out_of_the_path(p) for p in ("/a?b", "/a#", "/a\tb", "/a\x00", "/a\x7f"))


# ------------------------------------------------- a body sent without its length
def test_a_chunked_body_goes_on_with_its_length_alone(gwdb, monkeypatch, H):
    """The caller's ``Transfer-Encoding`` is not passed on beside the length the gateway states."""
    from app.transformers import transformation_manager

    seen = []

    def answer(request):
        seen.append(({name.decode().lower(): value.decode() for name, value in request.headers.raw}, request.content))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(main.router, "client", httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    monkeypatch.setattr(transformation_manager, "rules_by_id", {})
    assert gwdb.post("/api/t", headers=H, content=iter([b"plain ", b"words"])).status_code == 200
    assert gwdb.post("/api/t", headers={**H, "Content-Type": "application/json"}, content=iter([b'{"a":', b" 1}"])).status_code == 200
    assert gwdb.get("/api/t", headers={**H, "Transfer-Encoding": "chunked"}).status_code == 200
    for headers, body in seen:
        assert "transfer-encoding" not in headers
    assert [(headers.get("content-length"), body) for headers, body in seen[:2]] == [("11", b"plain words"), ("8", b'{"a": 1}')]



# ------------------------------------------------- repeated header lines
@pytest.fixture
def lines(monkeypatch):
    """The real router with a fake backend; yields the header lines of each request sent."""
    from app.transformers import transformation_manager

    seen = []

    def answer(request):
        seen.append([(name.decode().lower(), value.decode()) for name, value in request.headers.raw])
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        main.router, "client", httpx.AsyncClient(transport=httpx.MockTransport(answer))
    )
    monkeypatch.setattr(transformation_manager, "rules_by_id", {})
    return seen


def _of(sent_lines, name):
    return [value for line, value in sent_lines if line == name]


def _get(client, path, headers):
    """A GET with its header lines as given. (The test client itself joins the lines of a
    repeated header into one before the app sees them.)"""
    async def ask():
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as direct:
            return await direct.get(path, headers=headers)

    return client.portal.call(ask)


def test_a_header_given_on_two_lines_reaches_the_backend_whole(gwdb, lines, H):
    given = [*H.items(), ("X-Tag", "a"), ("X-Other", "o"), ("x-tag", "b"), ("X-TAG", "c")]
    assert _get(gwdb, "/mcp/t", given).status_code == 200
    assert _of(lines[0], "x-tag") == ["a", "b", "c"]
    assert _of(lines[0], "x-other") == ["o"]


def test_a_header_given_once_is_sent_as_before(gwdb, lines, H):
    _get(gwdb, "/mcp/t", [*H.items(), ("X-Tag", "a, b")])
    assert _of(lines[0], "x-tag") == ["a, b"]


def test_the_same_value_on_two_lines_keeps_both(gwdb, lines, H):
    _get(gwdb, "/mcp/t", [*H.items(), ("X-Tag", "a"), ("X-Tag", "a")])
    assert _of(lines[0], "x-tag") == ["a", "a"]


def test_a_rule_that_changes_a_repeated_header_wins(gwdb, lines, H):
    from app.transformers import TransformationRule, transformation_manager

    rule = TransformationRule.from_dict({
        "name": "headers", "path_pattern": "**",
        "request_config": {
            "headers_set": {"x-set": "z"}, "headers_remove": ["X-Drop"],
            "headers_rename": {"X-Old": "X-New"},
        },
    })
    transformation_manager.rules_by_id[rule.id] = rule
    given = [*H.items(), *[(name, value) for name in ("X-Set", "X-Drop", "X-Old", "X-Keep") for value in ("1", "2")]]
    _get(gwdb, "/mcp/t", given)
    assert _of(lines[0], "x-set") == ["z"]
    assert _of(lines[0], "x-drop") == []
    assert _of(lines[0], "x-old") == []
    assert _of(lines[0], "x-new") == ["1"]  # a rule sees one value: the first line's
    assert _of(lines[0], "x-keep") == ["1", "2"]


def test_the_gateways_own_headers_are_written_once(gwdb, lines, H):
    trace = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    given = [*H.items(), ("traceparent", trace), ("traceparent", trace), ("X-Gateway-Note", "a"), ("X-Gateway-Note", "b")]
    _get(gwdb, "/mcp/t", given)
    assert len(_of(lines[0], "traceparent")) == 1
    assert _of(lines[0], "x-gateway-note") == []


def test_a_header_about_the_connection_stays_on_one_line(gwdb, lines, H):
    _get(gwdb, "/mcp/t", [*H.items(), ("TE", "trailers"), ("TE", "gzip")])
    assert _of(lines[0], "te") == ["trailers"]


def test_repeated_headers_lists_only_names_given_more_than_once():
    from starlette.datastructures import Headers
    from app.routing import _with_repeated_lines, repeated_headers

    came = Headers(raw=[(b"x-tag", b"a"), (b"x-other", b"o"), (b"x-tag", b"b"), (b"host", b"one"), (b"host", b"two")])
    first, last = repeated_headers(came), repeated_headers(came, held=-1)
    assert first == {"x-tag": ("a", ["a", "b"])}
    assert last == {"x-tag": ("b", ["a", "b"])}
    assert _with_repeated_lines({"x-tag": "a", "x-other": "o"}, first) == [("x-tag", "a"), ("x-tag", "b"), ("x-other", "o")]
    assert _with_repeated_lines({"x-tag": "b"}, last) == [("x-tag", "a"), ("x-tag", "b")]
    # Changed, renamed in another case, or gone: what is there is what is sent.
    assert _with_repeated_lines({"x-tag": "z"}, first) == {"x-tag": "z"}
    assert _with_repeated_lines({"X-Tag": "a"}, first) == {"X-Tag": "a"}
    assert _with_repeated_lines({"x-other": "o"}, first) == {"x-other": "o"}
    assert _with_repeated_lines({"x-other": "o"}, None) == {"x-other": "o"}
    assert repeated_headers({"x-tag": "a"}) == {}
