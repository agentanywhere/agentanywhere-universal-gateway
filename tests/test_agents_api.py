"""End-to-end tests for the /admin/agents control-plane API.

Drives the real FastAPI app via TestClient so router registration, admin-token
gating, validation, DB persistence, live hot-reload into the proxy, and
export/import are all exercised together.
"""
import pytest

from app.config import get_settings

# The admin API accepts the configured key (ADMIN_TOKEN, or a non-placeholder
# SECRET_KEY). tests/conftest.py sets a real SECRET_KEY before app.config loads.
TOKEN = get_settings().secret_key
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def edge():
    """Reset the process-global agent-edge config around each test."""
    import app.main as m

    m.agent_edge_config.replace(routes={})
    yield m.agent_edge_config
    m.agent_edge_config.replace(routes={})


def test_requires_admin_token(client, edge):
    assert client.get("/admin/agents").status_code == 401


def test_create_lists_and_goes_live(client, edge):
    resp = client.post(
        "/admin/agents",
        json={
            "slug": "demo-agent",
            "upstream_base": "http://10.0.0.10:8099",
            "public_paths": ["/invite"],
            "display_name": "Demo Agent",
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["slug"] == "demo-agent"
    assert body["live"] is True
    assert body["public_url"] == "/agents/demo-agent/"

    # The live proxy resolves it immediately — no restart.
    route = edge.resolve("/agents/demo-agent/api/health")
    assert route is not None
    assert route.upstream_base == "http://10.0.0.10:8099"
    assert route.upstream_path == "/api/health"
    assert edge.resolve("/agents/demo-agent/invite").is_public is True

    listing = client.get("/admin/agents", headers=AUTH).json()
    assert any(a["slug"] == "demo-agent" for a in listing)


def test_validation_and_conflicts(client, edge):
    client.post(
        "/admin/agents",
        json={"slug": "dup-agent", "upstream_base": "http://x:1"},
        headers=AUTH,
    )
    # Duplicate slug -> 409
    assert (
        client.post(
            "/admin/agents",
            json={"slug": "dup-agent", "upstream_base": "http://x:2"},
            headers=AUTH,
        ).status_code
        == 409
    )
    # Bad slug -> 422
    assert (
        client.post(
            "/admin/agents",
            json={"slug": "Bad_Slug", "upstream_base": "http://x:1"},
            headers=AUTH,
        ).status_code
        == 422
    )
    # Bad upstream -> 422
    assert (
        client.post(
            "/admin/agents",
            json={"slug": "ok-slug", "upstream_base": "ftp://nope"},
            headers=AUTH,
        ).status_code
        == 422
    )


def test_update_disable_removes_from_live_routing(client, edge):
    client.post(
        "/admin/agents",
        json={"slug": "toggle-agent", "upstream_base": "http://10.0.0.10:8101"},
        headers=AUTH,
    )
    assert edge.resolve("/agents/toggle-agent/x") is not None

    resp = client.put("/admin/agents/toggle-agent", json={"enabled": False}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    # Disabled -> no longer routable.
    assert edge.resolve("/agents/toggle-agent/x") is None


def test_export_import_roundtrip(client, edge):
    client.post(
        "/admin/agents",
        json={"slug": "exported-agent", "upstream_base": "http://10.0.0.10:8102", "public_paths": ["/p"]},
        headers=AUTH,
    )
    exported = client.get("/admin/agents/export", headers=AUTH).json()
    assert exported["AGENT_ROUTES_JSON"]["exported-agent"] == "http://10.0.0.10:8102"
    assert exported["AGENT_PUBLIC_PATHS_JSON"]["exported-agent"] == ["/p"]

    resp = client.post(
        "/admin/agents/import",
        json={"routes": {"imported-agent": "http://10.0.0.10:8103"}},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1
    assert edge.resolve("/agents/imported-agent/") is not None


def test_delete_and_404(client, edge):
    client.post(
        "/admin/agents",
        json={"slug": "gone-agent", "upstream_base": "http://10.0.0.10:8104"},
        headers=AUTH,
    )
    assert client.delete("/admin/agents/gone-agent", headers=AUTH).status_code == 200
    assert edge.resolve("/agents/gone-agent/x") is None
    assert client.get("/admin/agents/gone-agent", headers=AUTH).status_code == 404
    assert client.delete("/admin/agents/gone-agent", headers=AUTH).status_code == 404
