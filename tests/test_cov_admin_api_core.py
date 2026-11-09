"""Behaviour tests for admin/api.py: auth, keys, registrations, routes, logs, AI,
SDK, config and logging endpoints."""
from datetime import timedelta

import pytest

import admin.api as admin_api
from app.config import get_settings
from app.models import RequestLog
from app.repositories.api_key_repo import APIKeyRepository
from app.repositories.log_repo import LogRepository
from app.time_utils import utcnow

SECRET = get_settings().secret_key
AUTH = {"Authorization": f"Bearer {SECRET}"}


class _NoSessionLocal:
    """Stand-in for app.database.AsyncSessionLocal. The access-log middleware persists
    rejected/handled requests through it as a fire-and-forget task; the real factory
    writes ./gateway.db, and a factory sharing the test connection races the test's own
    queries. Failing fast makes the middleware skip persistence (it swallows the error)."""

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        raise RuntimeError("request-log persistence disabled in tests")

    async def __aexit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _isolated(db_session, monkeypatch):
    """Keep middleware log persistence and the live route table off real state."""
    import app.database as database
    from app import route_registry
    monkeypatch.setattr(database, "AsyncSessionLocal", _NoSessionLocal())
    monkeypatch.setattr(route_registry, "_ROUTES", ())


def _msg(resp):
    body = resp.json()
    return body.get("message") or body.get("detail") or ""


# ------------------------------------------------------------------ auth
def test_auth_config_returns_oidc_shape(client):
    r = client.get("/admin/auth/config")
    assert r.status_code == 200
    assert set(r.json()) == {"enabled", "authority", "clientId"}


def test_login_success_wrong_user_wrong_password(client):
    ok = client.post("/admin/auth/login", json={"username": "admin", "password": SECRET})
    assert ok.status_code == 200
    assert ok.json() == {"access_token": SECRET, "token_type": "bearer", "username": "admin"}
    assert client.post("/admin/auth/login", json={"username": "x", "password": SECRET}).status_code == 401
    assert client.post("/admin/auth/login", json={"username": "admin", "password": "no"}).status_code == 401
    assert client.post("/admin/auth/login", json={"username": "admin"}).status_code == 422


def test_login_impossible_when_no_credential_configured(client, monkeypatch):
    monkeypatch.setattr(admin_api.settings, "admin_token", "")
    monkeypatch.setattr(admin_api.settings, "secret_key", "change-me-placeholder")
    r = client.post("/admin/auth/login", json={"username": "admin", "password": "change-me-placeholder"})
    assert r.status_code == 401
    assert admin_api._local_admin_credential() == ""
    assert admin_api._secret_key_is_admin_credential() is False
    # and the placeholder secret does not authenticate admin calls either
    assert client.get("/admin/api-keys", headers={"Authorization": "Bearer change-me-placeholder"}).status_code == 401


def test_admin_token_and_secret_key_acceptance_matrix(client, monkeypatch):
    monkeypatch.setattr(admin_api.settings, "admin_token", "dedicated-admin-token-xyz")
    # SECRET_KEY is no longer accepted once ADMIN_TOKEN is set...
    assert client.get("/admin/api-keys", headers=AUTH).status_code == 401
    assert client.get("/admin/api-keys", headers={"Authorization": "Bearer dedicated-admin-token-xyz"}).status_code == 200
    # ...unless explicitly opted back in.
    monkeypatch.setattr(admin_api.settings, "admin_accept_secret_key", True, raising=False)
    assert client.get("/admin/api-keys", headers=AUTH).status_code == 200


def test_verify_token_value_requires_token():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        admin_api.verify_admin_token_value(None)
    assert ei.value.status_code == 401
    with pytest.raises(HTTPException):
        admin_api.verify_admin_token(None)


def test_non_ascii_credential_is_401_not_500():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        admin_api.verify_admin_token_value("café-wrong")
    assert ei.value.status_code == 401


def test_public_admin_health(client):
    r = client.get("/admin/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"
    assert r.json()["version"] == get_settings().app_version


# ------------------------------------------------------------------ dashboard / health
@pytest.mark.asyncio
async def test_dashboard_stats_aggregates_recent_logs(client, db_session, monkeypatch):
    monkeypatch.setattr(admin_api.settings, "rate_limit_redis_url", "")
    for i, code in enumerate([200, 200, 500, 404]):
        await LogRepository.create(
            db_session,
            RequestLog(request_id=f"r{i}", method="GET", path="/x", client_ip="1.1.1.1",
                       status_code=code, response_time_ms=100.0),
        )
    r = client.get("/admin/dashboard/stats", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["total_requests"] == 4
    assert body["total_errors"] == 2
    assert body["error_rate"] == 0.5
    assert body["avg_response_time"] == 100.0
    assert body["requests_per_minute"] == pytest.approx(4 / 60.0)
    assert body["rate_limit_hits"] == 0


def test_dashboard_stats_empty_and_redis_counter(client, monkeypatch):
    class _Redis:
        def get(self, key):
            assert key == "rate_limit_hits_total"
            return b"7"

    monkeypatch.setattr(admin_api.settings, "rate_limit_redis_url", "redis://fake:6379/0")
    monkeypatch.setattr(admin_api.redis, "from_url", lambda url: _Redis())
    body = client.get("/admin/dashboard/stats", headers=AUTH).json()
    assert body["total_requests"] == 0 and body["error_rate"] == 0.0
    assert body["rate_limit_hits"] == 7

    class _RedisStr(_Redis):
        def get(self, key):
            return "9"

    monkeypatch.setattr(admin_api.redis, "from_url", lambda url: _RedisStr())
    assert client.get("/admin/dashboard/stats", headers=AUTH).json()["rate_limit_hits"] == 9

    def _boom(url):
        raise ConnectionError("down")

    monkeypatch.setattr(admin_api.redis, "from_url", _boom)
    r = client.get("/admin/dashboard/stats", headers=AUTH)
    assert r.status_code == 200 and r.json()["rate_limit_hits"] == 0


def test_dashboard_stats_500_when_repository_fails(client, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(admin_api.LogRepository, "get_stats", _boom)
    r = client.get("/admin/dashboard/stats", headers=AUTH)
    assert r.status_code == 500
    assert "db exploded" in _msg(r)


class _Resp:
    def __init__(self, code):
        self.status_code = code


def _http_client_factory(codes):
    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, timeout=None):
            code = codes[url.split("/health")[0]]
            if isinstance(code, Exception):
                raise code
            return _Resp(code)

    return lambda *a, **k: _C()


def test_detailed_health_degraded_and_healthy(client, monkeypatch):
    s = admin_api.settings
    monkeypatch.setattr(s, "rate_limit_redis_url", "")
    monkeypatch.setattr(s, "mcp_backend_url", "http://mcp.test")
    monkeypatch.setattr(s, "a2a_backend_url", "http://a2a.test")

    monkeypatch.setattr(
        admin_api.httpx, "AsyncClient",
        _http_client_factory({"http://mcp.test": 200, "http://a2a.test": RuntimeError("refused")}),
    )
    body = client.get("/admin/health/detailed", headers=AUTH).json()
    assert body["status"] == "degraded"
    assert body["services"]["redis"] == {"status": "not_configured"}
    assert body["services"]["mcp_backend"]["status"] == "healthy"
    assert body["services"]["a2a_backend"] == {"status": "unhealthy", "error": "refused"}

    monkeypatch.setattr(
        admin_api.httpx, "AsyncClient",
        _http_client_factory({"http://mcp.test": 200, "http://a2a.test": 503}),
    )
    assert client.get("/admin/health/detailed", headers=AUTH).json()["services"]["a2a_backend"]["status"] == "unhealthy"

    monkeypatch.setattr(
        admin_api.httpx, "AsyncClient",
        _http_client_factory({"http://mcp.test": 200, "http://a2a.test": 200}),
    )
    # redis "not_configured" is not "healthy", so the aggregate stays degraded
    assert client.get("/admin/health/detailed", headers=AUTH).json()["status"] == "degraded"


def test_detailed_health_redis_branches(client, monkeypatch):
    s = admin_api.settings
    monkeypatch.setattr(s, "mcp_backend_url", "http://mcp.test")
    monkeypatch.setattr(s, "a2a_backend_url", "http://a2a.test")
    monkeypatch.setattr(
        admin_api.httpx, "AsyncClient",
        _http_client_factory({"http://mcp.test": 200, "http://a2a.test": 200}),
    )
    monkeypatch.setattr(s, "rate_limit_redis_url", "redis://fake:6379/0")

    class _Ok:
        def ping(self):
            return True

    monkeypatch.setattr(admin_api.redis, "from_url", lambda url: _Ok())
    body = client.get("/admin/health/detailed", headers=AUTH).json()
    assert body["services"]["redis"]["status"] == "healthy"
    assert body["status"] == "healthy"

    class _Bad:
        def ping(self):
            raise ConnectionError("no redis")

    monkeypatch.setattr(admin_api.redis, "from_url", lambda url: _Bad())
    body = client.get("/admin/health/detailed", headers=AUTH).json()
    assert body["services"]["redis"] == {"status": "unhealthy", "error": "no redis"}
    assert body["status"] == "degraded"


# ------------------------------------------------------------------ api keys
def _post_key(client, **kw):
    return client.post("/admin/api-keys", headers=AUTH, **kw)


def test_create_key_json_body_and_defaults(client):
    r = _post_key(client, json={"name": "svc", "allowed_paths": ["/a", "/b"]})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "svc"
    assert body["rate_limit_per_minute"] == 60 and body["rate_limit_per_hour"] == 1000
    assert body["allowed_paths"] == ["/a", "/b"]
    assert body["expires_at"] is None and body["is_active"] is True
    assert body["key"].startswith("sk-") and body["key_prefix"]

    listed = client.get("/admin/api-keys", headers=AUTH).json()
    assert [k["id"] for k in listed] == [body["id"]]
    assert "key" not in listed[0]  # secret shown only at creation


def test_create_key_query_wins_over_body_and_csv_paths(client):
    r = _post_key(
        client,
        params={"name": "from-query", "allowed_paths": " /x , ,/y "},
        json={"name": "from-body", "rate_limit_per_minute": 5, "allowed_paths": ["/ignored"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "from-query"
    assert body["rate_limit_per_minute"] == 5
    assert body["allowed_paths"] == ["/x", "/y"]


def test_create_key_body_csv_string_paths(client):
    body = _post_key(client, json={"name": "k", "allowed_paths": "/p,/q"}).json()
    assert body["allowed_paths"] == ["/p", "/q"]


def test_create_key_body_bad_allowed_paths_400(client):
    r = _post_key(client, json={"name": "k", "allowed_paths": {"a": 1}})
    assert r.status_code == 400
    assert "allowed_paths" in _msg(r)


@pytest.mark.parametrize("payload", [{}, {"name": ""}, {"name": "   "}])
def test_create_key_requires_name_422(client, payload):
    r = _post_key(client, json=payload)
    assert r.status_code == 422
    assert "name" in _msg(r)


def test_create_key_expiry_rules(client):
    ok = _post_key(client, params={"name": "e", "expires_in_days": 3}).json()
    assert ok["expires_at"] is not None

    future = (utcnow() + timedelta(days=2)).isoformat()
    ok2 = _post_key(client, params={"name": "e2", "expires_at": future})
    assert ok2.status_code == 200 and ok2.json()["expires_at"].startswith(future[:10])

    tz_future = (utcnow() + timedelta(days=2)).isoformat() + "+00:00"
    assert _post_key(client, params={"name": "e3", "expires_at": tz_future}).status_code == 200

    both = _post_key(client, params={"name": "e", "expires_in_days": 1, "expires_at": future})
    assert both.status_code == 400 and "not both" in _msg(both)

    for days in (0, -4):
        r = _post_key(client, params={"name": "e", "expires_in_days": days})
        assert r.status_code == 400 and "positive" in _msg(r)

    bad = _post_key(client, params={"name": "e", "expires_at": "tomorrow-ish"})
    assert bad.status_code == 400 and "ISO-8601" in _msg(bad)

    past = _post_key(client, params={"name": "e", "expires_at": "2000-01-01T00:00:00"})
    assert past.status_code == 400 and "future" in _msg(past)

    names = [k["name"] for k in client.get("/admin/api-keys", headers=AUTH).json()]
    assert sorted(names) == ["e", "e2", "e3"]  # rejected requests created nothing


@pytest.mark.asyncio
async def test_revoke_and_toggle_api_key(client, db_session):
    _, row = await APIKeyRepository.create(db_session, name="victim")
    assert row.is_active is True

    t = client.put(f"/admin/api-keys/{row.id}/toggle", headers=AUTH)
    assert t.status_code == 200
    assert t.json() == {"id": row.id, "name": "victim", "is_active": False}
    assert client.put(f"/admin/api-keys/{row.id}/toggle", headers=AUTH).json()["is_active"] is True

    assert client.delete(f"/admin/api-keys/{row.id}", headers=AUTH).status_code == 200
    assert client.get("/admin/api-keys", headers=AUTH).json() == []
    assert client.delete(f"/admin/api-keys/{row.id}", headers=AUTH).status_code == 404
    assert client.put(f"/admin/api-keys/{row.id}/toggle", headers=AUTH).status_code == 404


def test_create_key_survives_webhook_failure(client, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("hook down")

    monkeypatch.setattr(admin_api.webhook_manager, "trigger_webhook", _boom)
    assert _post_key(client, json={"name": "still-works"}).status_code == 200


def test_create_key_triggers_webhook_with_details(client, monkeypatch):
    calls = []

    async def _trigger(event, payload):
        calls.append((event, payload))

    monkeypatch.setattr(admin_api.webhook_manager, "trigger_webhook", _trigger)
    body = _post_key(client, json={"name": "hooked", "rate_limit_per_hour": 77}).json()
    assert calls and calls[0][0] == admin_api.WebhookEventType.API_KEY_CREATED
    assert calls[0][1]["api_key_id"] == body["id"]
    assert calls[0][1]["rate_limit_per_hour"] == 77


# ------------------------------------------------------------------ openapi registrations
OPENAPI_DOC = {
    "openapi": "3.0.0",
    "info": {"title": "t", "version": "1"},
    "paths": {
        "/pets": {"get": {"operationId": "listPets", "summary": "List pets",
                          "responses": {"200": {"description": "ok"}}}}
    },
}


def _reg(name="pets", doc=None, **kw):
    return {"name": name, "openapi_document": doc or OPENAPI_DOC,
            "upstream_base_url": "http://pets.test", **kw}


def test_registration_lifecycle(client):
    r = client.post("/admin/registrations/openapi", json=_reg(description="d", path_prefix="/p"), headers=AUTH)
    assert r.status_code == 200
    reg = r.json()
    assert reg["name"] == "pets" and reg["path_prefix"] == "/p"
    assert reg["is_active"] is True and reg["expose_mcp_tools"] is True

    listing = client.get("/admin/registrations/openapi", headers=AUTH).json()
    assert listing["count"] == 1

    tools = client.get("/admin/registrations/mcp-tools", headers=AUTH).json()
    assert tools["count"] == len(tools["tools"]) >= 1

    dup = client.post("/admin/registrations/openapi", json=_reg(), headers=AUTH)
    assert dup.status_code == 409 and "already exists" in _msg(dup)

    soft = client.delete(f"/admin/registrations/openapi/{reg['id']}", headers=AUTH)
    assert soft.status_code == 200 and soft.json()["hard"] is False
    assert client.get("/admin/registrations/openapi", headers=AUTH).json()["count"] == 0
    again = client.post("/admin/registrations/openapi", json=_reg("pets2"), headers=AUTH).json()
    hard = client.delete(f"/admin/registrations/openapi/{again['id']}", params={"hard": True}, headers=AUTH)
    assert hard.status_code == 200 and hard.json()["hard"] is True
    assert client.delete(f"/admin/registrations/openapi/{again['id']}", params={"hard": True}, headers=AUTH).status_code == 404
    assert client.delete("/admin/registrations/openapi/ghost", headers=AUTH).status_code == 404


def test_name_reusable_after_soft_delete(client):
    first = client.post("/admin/registrations/openapi", json=_reg(), headers=AUTH).json()
    client.delete(f"/admin/registrations/openapi/{first['id']}", headers=AUTH)
    again = client.post("/admin/registrations/openapi", json=_reg(), headers=AUTH)
    assert again.status_code == 200


@pytest.mark.parametrize(
    "doc,fragment",
    [
        ({"paths": {}}, "openapi"),
        ({"openapi": "2.0", "paths": {}}, "3.x"),
        ({"openapi": "3.0.0"}, "paths"),
    ],
)
def test_registration_rejects_invalid_openapi_400(client, doc, fragment):
    r = client.post("/admin/registrations/openapi", json=_reg(doc=doc), headers=AUTH)
    assert r.status_code == 400
    assert fragment in _msg(r)


def test_registration_missing_fields_422(client):
    assert client.post("/admin/registrations/openapi", json={"name": "x"}, headers=AUTH).status_code == 422


# ------------------------------------------------------------------ routes
def test_list_routes_static_and_optional_backends(client, monkeypatch):
    s = admin_api.settings
    monkeypatch.setattr(s, "mqtt_broker_url", "mqtt://m:1883")
    monkeypatch.setattr(s, "websocket_backend_url", "ws://w:1")
    monkeypatch.setattr(s, "grpc_backend_url", "g:50051")
    monkeypatch.setattr(s, "graphql_backend_url", "http://gq")
    monkeypatch.setattr(s, "http_backend_url", "http://h")
    routes = client.get("/admin/routes", headers=AUTH).json()
    by_path = {r["path"]: r for r in routes}
    for p in ["/mcp/*", "/a2a/*", "/mqtt/*", "/ws/*", "/grpc/*", "/api/*, /rest/*, /http/*",
              "/test/mcp", "/test/a2a", "/test/websocket", "/test/grpc"]:
        assert p in by_path
    assert "/test/http" not in by_path
    assert by_path["/mqtt/*"]["protocol"] == "mqtt"
    assert by_path["/test/mcp"]["requires_auth"] is False
    assert by_path["/test/websocket"]["backend_url"].startswith(("ws://", "wss://"))
    assert all(r["source"] == "static" for r in routes)


def test_list_routes_defaults_include_test_http(client, monkeypatch):
    s = admin_api.settings
    for attr in ["mqtt_broker_url", "websocket_backend_url", "grpc_backend_url",
                 "graphql_backend_url", "http_backend_url"]:
        monkeypatch.setattr(s, attr, None)
    paths = [r["path"] for r in client.get("/admin/routes", headers=AUTH).json()]
    assert "/test/http" in paths
    assert "/mqtt/*" not in paths and "/ws/*" not in paths


def test_created_route_is_persisted_listed_and_live(client, db_session, monkeypatch):
    # The live route table reloads from the DB, so give it the test database.
    import app.database as database
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    monkeypatch.setattr(
        database,
        "AsyncSessionLocal",
        async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False),
    )
    r = client.post(
        "/admin/routes",
        json={"path": "/svc-x/*", "backend_url": "http://svc-x:9", "protocol": "http", "timeout": 7},
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is True and body["timeout"] == 7 and body["requires_auth"] is True

    dyn = [x for x in client.get("/admin/routes", headers=AUTH).json() if x["source"] == "dynamic"]
    assert [d["path"] for d in dyn] == ["/svc-x/*"]
    assert dyn[0]["id"] == body["id"]


def test_create_route_validation_422(client):
    assert client.post("/admin/routes", json={"path": "/x"}, headers=AUTH).status_code == 422


def test_create_route_reports_inactive_when_registry_refresh_fails(client, monkeypatch):
    from app import route_registry

    async def _boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(route_registry, "refresh_now", _boom)
    r = client.post(
        "/admin/routes",
        json={"path": "/svc-y/*", "backend_url": "http://svc-y:9", "protocol": "http"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["active"] is False


def test_list_routes_survives_db_failure(client, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(admin_api.RouteRepository, "list_all", _boom)
    r = client.get("/admin/routes", headers=AUTH)
    assert r.status_code == 200
    assert r.json()  # static routes still listed
    assert all(x["source"] == "static" for x in r.json())


# ------------------------------------------------------------------ logs
@pytest.mark.asyncio
async def test_logs_level_filters(client, db_session):
    for i, code in enumerate([200, 404, 500, 503]):
        await LogRepository.create(
            db_session,
            RequestLog(request_id=f"id{i}", method="GET", path=f"/p{i}", client_ip="1.1.1.1",
                       status_code=code, response_time_ms=1.0, user_agent="ua"),
        )
    errors = client.get("/admin/logs", params={"level": "error"}, headers=AUTH).json()
    assert sorted(l["status_code"] for l in errors["logs"]) == [500, 503]
    assert errors["total"] == 2
    warns = client.get("/admin/logs", params={"level": "warning"}, headers=AUTH).json()
    assert [l["status_code"] for l in warns["logs"]] == [404]
    plain = client.get("/admin/logs", params={"status_code": 200}, headers=AUTH).json()
    assert plain["total"] == 1 and plain["logs"][0]["path"] == "/p0"
    assert plain["logs"][0]["timestamp"] and plain["logs"][0]["user_agent"] == "ua"
    paged = client.get("/admin/logs", params={"limit": 2, "offset": 0}, headers=AUTH).json()
    assert len(paged["logs"]) == 2 and paged["total"] == 4


# ------------------------------------------------------------------ AI endpoints
@pytest.mark.parametrize(
    "path,attr_chain",
    [
        ("/admin/ai/insights", ("get_ai_insights",)),
        ("/admin/ai/anomalies", ("anomaly_detector", "get_anomaly_summary")),
        ("/admin/ai/scaling-recommendations", ("scaling_recommender", "get_scaling_recommendation")),
        ("/admin/ai/cost-analysis", ("cost_optimizer", "analyze_costs")),
        ("/admin/ai/traffic-forecast", ("predictive_analytics", "predict_traffic")),
    ],
)
def test_ai_endpoints_ok_and_500_on_failure(client, monkeypatch, path, attr_chain):
    ok = client.get(path, headers=AUTH)
    assert ok.status_code == 200

    target = admin_api.ai_service
    for a in attr_chain[:-1]:
        target = getattr(target, a)

    def _boom(*a, **k):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(target, attr_chain[-1], _boom)
    bad = client.get(path, headers=AUTH)
    assert bad.status_code == 500
    assert "model unavailable" in _msg(bad)


def test_traffic_forecast_echoes_hours_ahead(client, monkeypatch):
    monkeypatch.setattr(admin_api.ai_service.predictive_analytics, "predict_traffic",
                        lambda hours_ahead: [hours_ahead])
    body = client.get("/admin/ai/traffic-forecast", params={"hours_ahead": 3}, headers=AUTH).json()
    assert body == {"forecast": [3], "hours_ahead": 3}


# ------------------------------------------------------------------ SDK
def test_sdk_languages_and_unsupported(client):
    body = client.get("/admin/sdk/languages", headers=AUTH).json()
    assert body == {"languages": ["python", "javascript", "go"], "count": 3, "default": "python"}
    assert client.post("/admin/sdk/generate/cobol", headers=AUTH).status_code == 400
    assert client.get("/admin/sdk/preview/cobol", headers=AUTH).status_code == 400


@pytest.mark.parametrize("lang", ["python", "javascript", "go"])
def test_sdk_generate_zip_and_preview(client, lang):
    import io
    import zipfile

    z = client.post(f"/admin/sdk/generate/{lang}", headers=AUTH)
    assert z.status_code == 200
    assert z.headers["content-type"] == "application/zip"
    assert f"{lang}_sdk.zip" in z.headers["content-disposition"]
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert names

    p = client.get(f"/admin/sdk/preview/{lang}", headers=AUTH).json()
    assert p["language"] == lang
    assert p["file_count"] == len(p["files"]) > 0
    assert p["preview"] in p["files"]
    assert sorted(p["files"]) == sorted(names)


# ------------------------------------------------------------------ config
def test_get_config_sections_redaction_and_unknown(client):
    full = client.get("/admin/config", headers=AUTH).json()
    assert full["sections"] and isinstance(full["safe_reload_fields"], list)
    name = next(iter(full["sections"]))
    one = client.get("/admin/config", params={"section": name}, headers=AUTH).json()
    assert list(one) == [name] and one[name] == full["sections"][name]
    miss = client.get("/admin/config", params={"section": "nope"}, headers=AUTH)
    assert miss.status_code == 404 and "nope" in _msg(miss)

    redacted = client.get("/admin/config", headers=AUTH).text
    raw = client.get("/admin/config", params={"redacted": "false"}, headers=AUTH).text
    assert SECRET not in redacted
    assert SECRET in raw


def test_config_diff_schema_validate(client):
    diff = client.get("/admin/config/diff", headers=AUTH).json()
    assert diff["count"] == len(diff["differs"])
    schema = client.get("/admin/config/schema", headers=AUTH).json()
    assert "properties" in schema

    assert client.post("/admin/config/validate", json={}, headers=AUTH).json() == {"valid": True}
    bad = client.post("/admin/config/validate", json={"port": "not-an-int"}, headers=AUTH).json()
    assert bad["valid"] is False
    assert bad["errors"][0]["loc"] == ["port"]
    assert bad["errors"][0]["msg"]


def test_config_reload_and_runtime_update_and_audit(client):
    r = client.post("/admin/config/reload", headers=AUTH).json()
    assert r["reloaded"] is True and r["version"]

    bad = client.put("/admin/config", json={"secret_key": "nope-nope-nope-nope-nope-nope-1"}, headers=AUTH)
    assert bad.status_code == 400

    audit = client.get("/admin/config/audit", params={"limit": 0}, headers=AUTH)
    assert audit.status_code == 200 and isinstance(audit.json(), list)
    assert client.get("/admin/config/audit", params={"limit": 100000}, headers=AUTH).status_code == 200


def test_config_update_applies_safe_field(client):
    from app.config import SAFE_RELOAD_FIELDS, get_settings as gs

    field = "log_level" if "log_level" in SAFE_RELOAD_FIELDS else sorted(SAFE_RELOAD_FIELDS)[0]
    current = getattr(gs(), field)
    r = client.put("/admin/config", json={field: current}, headers=AUTH)
    assert r.status_code == 200


# ------------------------------------------------------------------ logging
@pytest.fixture
def restore_logging():
    from app.logging_config import get_config

    cfg = get_config()
    snapshot = (cfg.level, cfg.sample_rate, cfg.include_caller, set(cfg.redact_keys),
                dict(cfg.logger_levels), cfg.redaction_enabled)
    yield
    from app.logging_config import update_config

    update_config(level=snapshot[0], sample_rate=snapshot[1], include_caller=snapshot[2],
                  redact_keys=snapshot[3], logger_levels=snapshot[4], redaction_enabled=snapshot[5])


def test_logging_get_and_update(client, restore_logging):
    cfg = client.get("/admin/logging", headers=AUTH).json()
    assert {"level", "resolved_format", "redact_keys", "sample_rate", "logger_levels"} <= set(cfg)

    r = client.put(
        "/admin/logging",
        json={"sample_rate": 0.5, "redact_keys": ["foo", "bar"], "level": "DEBUG"},
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["sample_rate"] == 0.5
    assert client.get("/admin/logging", headers=AUTH).json()["redact_keys"] == ["bar", "foo"]


def test_logging_update_rejects_unknown_and_bad_values_400(client, restore_logging):
    bad = client.put("/admin/logging", json={"colour": "red"}, headers=AUTH)
    assert bad.status_code == 400 and "colour" in _msg(bad)


def test_logging_update_rejects_non_numeric_sample_rate(client, restore_logging):
    bad = client.put("/admin/logging", json={"sample_rate": "lots"}, headers=AUTH)
    assert bad.status_code == 400


def test_logging_test_endpoint_emits(client):
    r = client.post("/admin/logging/test", json={"level": "WARNING", "message": "hello", "extra": {"a": 1}}, headers=AUTH)
    assert r.json() == {"emitted": True, "level": "warning", "message": "hello"}
    d = client.post("/admin/logging/test", headers=AUTH).json()
    assert d == {"emitted": True, "level": "info", "message": "admin_test_log"}
    odd = client.post("/admin/logging/test", json={"level": "nonsense"}, headers=AUTH).json()
    assert odd["emitted"] is True
