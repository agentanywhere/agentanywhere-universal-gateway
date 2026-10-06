"""Auth/error responses are content-negotiated: JSON for APIs, HTML for browsers."""
import pytest


def _protected_path() -> str:
    # A path that is neither public, admin, oauth2, nor an agent-edge route, so
    # the auth middleware rejects it with 401 API-key-required.
    return "/some/protected/resource"


def test_api_client_gets_json(client):
    resp = client.get(_protected_path(), headers={"Accept": "application/json"})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert body["error"] == "AUTHENTICATION_ERROR"
    assert "message" in body


def test_default_client_gets_json(client):
    # curl / SDK default Accept: */* must stay on the JSON contract.
    resp = client.get(_protected_path(), headers={"Accept": "*/*"})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json()["error"] == "AUTHENTICATION_ERROR"


def test_browser_gets_html_page(client):
    accept = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    resp = client.get(_protected_path(), headers={"Accept": accept})
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert "<!doctype html>" in html.lower()
    # Human-readable, not a raw JSON blob.
    assert "Sign-in required" in html
    assert "401" in html
    # The machine JSON fields must not leak as the visible body.
    assert '{"error"' not in html


def test_xhr_html_accept_still_gets_json(client):
    # A fetch()/XHR that sets X-Requested-With should keep JSON even if it lists html.
    resp = client.get(
        _protected_path(),
        headers={"Accept": "text/html", "X-Requested-With": "XMLHttpRequest"},
    )
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("application/json")


def test_error_responses_have_nosniff(client):
    resp = client.get(_protected_path(), headers={"Accept": "application/json"})
    assert resp.headers.get("x-content-type-options") == "nosniff"


@pytest.fixture
def one_agent():
    """Make the agent edge active with a single known slug, then restore."""
    import app.main as m

    m.agent_edge_config.replace(routes={"real-agent": "http://10.0.0.10:9999"})
    yield
    m.agent_edge_config.replace(routes={})


def test_unknown_agent_slug_is_404_json(client, one_agent):
    resp = client.get("/agents/ghost-agent/", headers={"Accept": "application/json"})
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == "AGENT_NOT_FOUND"
    assert "ghost-agent" in body["message"]


def test_unknown_agent_slug_is_html_page_for_browser(client, one_agent):
    accept = "text/html,application/xhtml+xml,*/*;q=0.8"
    resp = client.get("/agents/ghost-agent/", headers={"Accept": accept})
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("text/html")
    assert "Agent not found" in resp.text
