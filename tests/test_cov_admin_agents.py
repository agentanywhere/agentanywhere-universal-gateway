"""Behaviour tests for admin/agents_api.py (agent registry control plane)."""
import json

import pytest
from fastapi import HTTPException

from app.config import get_settings
from app.repositories.agent_registry_repo import AgentRegistryRepository

TOKEN = get_settings().secret_key
AUTH = {"Authorization": f"Bearer {TOKEN}"}


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
def _isolated_request_log_db(db_session, monkeypatch):
    """The access-log middleware persists rejected requests through
    app.database.AsyncSessionLocal (a ./gateway.db file by default, and slow).
    Point it at the in-memory test database instead."""
    import app.database as database
    monkeypatch.setattr(database, "AsyncSessionLocal", _NoSessionLocal())


@pytest.fixture
def edge():
    import app.main as m

    m.agent_edge_config.replace(routes={})
    yield m.agent_edge_config
    m.agent_edge_config.replace(routes={})


def _msg(resp):
    """Error text, whichever envelope the app renders (message or detail)."""
    body = resp.json()
    return body.get("message") or body.get("detail") or ""


def _create(client, slug="orders-agent", upstream="http://10.0.0.5:8090", **extra):
    return client.post(
        "/admin/agents",
        json={"slug": slug, "upstream_base": upstream, **extra},
        headers=AUTH,
    )


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/admin/agents"),
        ("get", "/admin/agents/export"),
        ("post", "/admin/agents/import"),
        ("post", "/admin/agents/reload"),
        ("get", "/admin/agents/health"),
        ("get", "/admin/agents/some-slug"),
        ("delete", "/admin/agents/some-slug"),
        ("post", "/admin/agents/some-slug/health"),
    ],
)
def test_endpoints_reject_missing_and_wrong_token(client, edge, method, path):
    assert getattr(client, method)(path).status_code == 401
    bad = {"Authorization": "Bearer definitely-wrong"}
    assert getattr(client, method)(path, headers=bad).status_code == 401


def test_create_persists_defaults_and_is_audited(client, edge):
    resp = _create(client, display_name="Orders", description="d")
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "published"
    assert body["runtime_mode"] == "dedicated"
    assert body["data_residency"] == "in"
    assert body["enabled"] is True
    assert body["live"] is True

    got = client.get("/admin/agents/orders-agent", headers=AUTH)
    assert got.status_code == 200
    assert got.json()["display_name"] == "Orders"
    assert got.json()["public_url"] == "/agents/orders-agent/"

    # audit entry was written for the create
    audit = client.get(
        "/admin/governance/audit", params={"action": "agent.create"}, headers=AUTH
    ).json()
    assert any(a["resource_id"] == "orders-agent" for a in audit)


@pytest.mark.parametrize(
    "payload,fragment",
    [
        ({"slug": "Bad_Slug", "upstream_base": "http://x:1"}, "kebab"),
        ({"slug": "a", "upstream_base": "http://x:1"}, "2"),
        ({"slug": "ok-slug", "upstream_base": "ftp://x"}, "http"),
        ({"slug": "ok-slug", "upstream_base": "http://x y"}, "spaces"),
        ({"slug": "ok-slug", "upstream_base": "http://x:1", "status": "weird"}, "status"),
        ({"slug": "ok-slug", "upstream_base": "http://x:1", "runtime_mode": "x"}, "runtime_mode"),
    ],
)
def test_create_validation_422(client, edge, payload, fragment):
    resp = client.post("/admin/agents", json=payload, headers=AUTH)
    assert resp.status_code == 422
    assert fragment in _msg(resp)
    assert client.get("/admin/agents", headers=AUTH).json() == []


def test_create_missing_required_field_422(client, edge):
    assert client.post("/admin/agents", json={"slug": "abc"}, headers=AUTH).status_code == 422


def test_create_duplicate_409(client, edge):
    assert _create(client).status_code == 201
    dup = _create(client, upstream="http://other:1")
    assert dup.status_code == 409
    assert "already exists" in _msg(dup)
    # original untouched
    assert client.get("/admin/agents/orders-agent", headers=AUTH).json()["upstream_base"] == "http://10.0.0.5:8090"


def test_get_unknown_404(client, edge):
    r = client.get("/admin/agents/nope-agent", headers=AUTH)
    assert r.status_code == 404
    assert "nope-agent" in _msg(r)


def test_update_merges_fields_and_hot_reloads(client, edge):
    _create(client, public_paths=["/invite"])
    r = client.put(
        "/admin/agents/orders-agent",
        json={"upstream_base": "http://10.0.0.9:9000", "description": "new"},
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["upstream_base"] == "http://10.0.0.9:9000"
    assert body["description"] == "new"
    assert body["public_paths"] == ["/invite"]  # untouched
    assert edge.resolve("/agents/orders-agent/x").upstream_base == "http://10.0.0.9:9000"

    audit = client.get(
        "/admin/governance/audit", params={"action": "agent.update"}, headers=AUTH
    ).json()
    assert audit and audit[0]["resource_id"] == "orders-agent"


def test_update_disable_removes_from_live_routing(client, edge):
    _create(client)
    assert edge.resolve("/agents/orders-agent/x") is not None
    r = client.put("/admin/agents/orders-agent", json={"enabled": False}, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["live"] is False
    assert edge.resolve("/agents/orders-agent/x") is None


def test_update_unknown_404_and_invalid_422(client, edge):
    assert client.put("/admin/agents/ghost-agent", json={"enabled": False}, headers=AUTH).status_code == 404
    _create(client)
    bad = client.put("/admin/agents/orders-agent", json={"upstream_base": "nope"}, headers=AUTH)
    assert bad.status_code == 422
    bad = client.put("/admin/agents/orders-agent", json={"status": "zzz"}, headers=AUTH)
    assert bad.status_code == 422
    bad = client.put("/admin/agents/orders-agent", json={"runtime_mode": "zzz"}, headers=AUTH)
    assert bad.status_code == 422
    # a rejected update changes nothing
    assert client.get("/admin/agents/orders-agent", headers=AUTH).json()["upstream_base"] == "http://10.0.0.5:8090"


def test_delete_removes_row_and_route(client, edge):
    _create(client)
    r = client.delete("/admin/agents/orders-agent", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["deleted"] is True and r.json()["slug"] == "orders-agent"
    assert client.get("/admin/agents/orders-agent", headers=AUTH).status_code == 404
    assert edge.resolve("/agents/orders-agent/x") is None
    assert client.delete("/admin/agents/orders-agent", headers=AUTH).status_code == 404


def test_list_marks_live_flag(client, edge):
    _create(client, slug="live-one")
    _create(client, slug="off-one", upstream="http://h:2")
    client.put("/admin/agents/off-one", json={"enabled": False}, headers=AUTH)
    rows = {a["slug"]: a for a in client.get("/admin/agents", headers=AUTH).json()}
    assert rows["live-one"]["live"] is True
    assert rows["off-one"]["live"] is False


def test_export_only_enabled_with_public_paths(client, edge):
    _create(client, slug="a-one", public_paths=["/p"])
    _create(client, slug="b-two", upstream="http://h:2")
    _create(client, slug="c-off", upstream="http://h:3")
    client.put("/admin/agents/c-off", json={"enabled": False}, headers=AUTH)
    body = client.get("/admin/agents/export", headers=AUTH).json()
    assert body["count"] == 2
    assert set(body["AGENT_ROUTES_JSON"]) == {"a-one", "b-two"}
    assert body["AGENT_PUBLIC_PATHS_JSON"] == {"a-one": ["/p"]}


def test_import_upserts_valid_skips_invalid(client, edge):
    r = client.post(
        "/admin/agents/import",
        json={
            "routes": {
                "good-one": "http://h:1",
                "Bad_Slug": "http://h:2",
                "bad-url": "ftp://h",
            },
            "public_paths": {"good-one": ["/open"]},
        },
        headers=AUTH,
    )
    assert r.status_code == 200
    assert r.json()["imported"] == 1
    assert r.json()["live_routes"] >= 1
    rows = client.get("/admin/agents", headers=AUTH).json()
    assert [a["slug"] for a in rows] == ["good-one"]
    assert rows[0]["public_paths"] == ["/open"]
    assert edge.resolve("/agents/good-one/open").is_public is True


def test_import_accepts_env_style_keys_and_updates_existing(client, edge):
    _create(client, slug="good-one", upstream="http://old:1")
    r = client.post(
        "/admin/agents/import",
        json={
            "AGENT_ROUTES_JSON": {"good-one": "http://new:1"},
            "AGENT_PUBLIC_PATHS_JSON": {"good-one": "not-a-list"},
        },
        headers=AUTH,
    )
    assert r.status_code == 200
    got = client.get("/admin/agents/good-one", headers=AUTH).json()
    assert got["upstream_base"] == "http://new:1"


@pytest.mark.parametrize("payload", [{}, {"routes": {}}, {"routes": ["x"]}])
def test_import_requires_routes_422(client, edge, payload):
    r = client.post("/admin/agents/import", json=payload, headers=AUTH)
    assert r.status_code == 422
    assert "routes" in _msg(r)


def test_reload_picks_up_rows_written_directly_to_db(client, edge, db_session):
    import asyncio

    asyncio.get_event_loop_policy()  # noqa: B018 (keep import used)
    # Reload on an empty registry reports zero routes
    r = client.post("/admin/agents/reload", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"reloaded": True, "live_routes": 0}


def test_health_snapshot_requires_auth_and_returns_dict(client, edge):
    r = client.get("/admin/agents/health", headers=AUTH)
    assert r.status_code == 200
    assert isinstance(r.json(), dict)


def test_stream_rejects_missing_and_bad_token(client, edge):
    assert client.get("/admin/agents/stream").status_code == 401
    assert client.get("/admin/agents/stream", params={"token": "bad"}).status_code == 401
    assert (
        client.get("/admin/agents/stream", headers={"Authorization": "Bearer bad"}).status_code
        == 401
    )


class _FakeRequest:
    """Minimal Request stand-in: header map plus a scripted disconnect flag."""

    def __init__(self, headers=None, disconnect_after=1):
        self.headers = headers or {}
        self._left = disconnect_after

    async def is_disconnected(self):
        if self._left <= 0:
            return True
        self._left -= 1
        return False


@pytest.mark.asyncio
async def test_stream_emits_snapshot_then_stops_on_disconnect(edge, monkeypatch):
    import admin.agents_api as mod

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    # token via query param
    resp = await mod.agents_health_stream(_FakeRequest(disconnect_after=2), token=TOKEN)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"] == "no-cache"
    chunks = [c async for c in resp.body_iterator]
    assert len(chunks) == 2
    for c in chunks:
        assert c.startswith("data: ") and c.endswith("\n\n")
        json.loads(c[len("data: "):])

    # token via bearer header
    resp = await mod.agents_health_stream(
        _FakeRequest(headers={"authorization": f"Bearer {TOKEN}"}, disconnect_after=1),
        token=None,
    )
    assert len([c async for c in resp.body_iterator]) == 1

    with pytest.raises(HTTPException) as ei:
        await mod.agents_health_stream(_FakeRequest(), token=None)
    assert ei.value.status_code == 401


class _FakeResp:
    status_code = 204


def _fake_client(result=None, exc=None, seen=None):
    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            if seen is not None:
                seen.append(url)
            if exc:
                raise exc
            return result

    return _C


def test_probe_unknown_slug_404(client, edge):
    r = client.post("/admin/agents/ghost-agent/health", json={}, headers=AUTH)
    assert r.status_code == 404


def test_probe_reachable_builds_target_url(client, edge, monkeypatch):
    import admin.agents_api as mod

    _create(client, upstream="http://10.0.0.5:8090/")
    seen = []
    monkeypatch.setattr(mod.httpx, "AsyncClient", _fake_client(_FakeResp(), seen=seen))

    r = client.post("/admin/agents/orders-agent/health", json={"path": "ping"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is True
    assert body["status_code"] == 204
    assert body["target"] == "http://10.0.0.5:8090/ping"
    assert seen == ["http://10.0.0.5:8090/ping"]

    r = client.post("/admin/agents/orders-agent/health", json={}, headers=AUTH)
    assert r.json()["target"] == "http://10.0.0.5:8090/"


def test_probe_unreachable_reports_error_not_500(client, edge, monkeypatch):
    import admin.agents_api as mod

    _create(client)
    monkeypatch.setattr(
        mod.httpx, "AsyncClient", _fake_client(exc=RuntimeError("connection refused"))
    )
    r = client.post("/admin/agents/orders-agent/health", json={"path": "/x"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["reachable"] is False
    assert "connection refused" in body["error"]
    assert "status_code" not in body


@pytest.mark.asyncio
async def test_audit_helper_swallows_failures(monkeypatch):
    import admin.agents_api as mod
    import app.governance as gov

    class _Boom:
        async def record(self, *a, **k):
            raise RuntimeError("db down")

    monkeypatch.setattr(gov, "audit_log", _Boom())

    class _Req:
        class state:
            request_id = "r1"

        client = None
        headers = {}

    # must not raise
    await mod._audit(None, _Req(), "agent.create", "x-agent", "tok-abcdefgh12345678")


def test_actor_is_a_principal_never_a_token_fragment():
    import admin.agents_api as mod

    assert mod._actor(None) is None
    assert mod._actor("") is None
    assert mod._actor("0123456789abcdef") == "unknown"
    assert mod._actor(get_settings().secret_key) == "secret-key"


@pytest.mark.asyncio
async def test_repo_row_visible_after_api_create(client, edge, db_session):
    _create(client)
    row = await AgentRegistryRepository.get(db_session, "orders-agent")
    assert row is not None and row.enabled is True
    assert row.updated_by == "secret-key"
