"""Unit tests for the AgentAnywhere agent-edge router (app.agent_routes)."""
from types import SimpleNamespace

import pytest

from app.agent_routes import AgentEdgeConfig, AgentRoute, parse_agent_path


# --------------------------------------------------------------------------
# parse_agent_path
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,expected",
    [
        ("/agents/orders-agent", ("orders-agent", "/")),
        ("/agents/orders-agent/", ("orders-agent", "/")),
        ("/agents/orders-agent/api/v1/health", ("orders-agent", "/api/v1/health")),
        ("/agents/support-agent/auth/callback", ("support-agent", "/auth/callback")),
        ("/agents/search-agent/ws/copilot", ("search-agent", "/ws/copilot")),
    ],
)
def test_parse_agent_path_ok(path, expected):
    assert parse_agent_path(path) == expected


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/agents",
        "/agents/",          # no slug
        "/api/mcp/foo",
        "/oauth2/token",
        "agents/orders-agent",  # missing leading slash
    ],
)
def test_parse_agent_path_none(path):
    assert parse_agent_path(path) is None


# --------------------------------------------------------------------------
# AgentEdgeConfig.resolve
# --------------------------------------------------------------------------
def _cfg():
    return AgentEdgeConfig(
        routes={
            "orders-agent": "http://10.0.0.10:8090/",  # trailing slash should be trimmed
            "support-agent": "http://10.0.0.10:8001",
            "search-agent": "http://10.0.0.10:8005",
        },
        public_paths={
            "orders-agent": ["/invite", "/api/v1/invites"],
        },
    )


def test_resolve_strips_prefix_and_trims_base():
    route = _cfg().resolve("/agents/orders-agent/api/v1/health")
    assert isinstance(route, AgentRoute)
    assert route.slug == "orders-agent"
    assert route.upstream_base == "http://10.0.0.10:8090"
    assert route.upstream_path == "/api/v1/health"
    assert route.upstream_url == "http://10.0.0.10:8090/api/v1/health"


def test_resolve_root_path():
    route = _cfg().resolve("/agents/support-agent")
    assert route.upstream_url == "http://10.0.0.10:8001/"


def test_resolve_unknown_slug_returns_none():
    assert _cfg().resolve("/agents/does-not-exist/foo") is None


def test_resolve_non_agent_path_returns_none():
    assert _cfg().resolve("/api/mcp/tools") is None


def test_audience_defaults_to_slug():
    assert _cfg().resolve("/agents/search-agent/x").audience == "search-agent"


def test_audience_override():
    cfg = AgentEdgeConfig(
        routes={"orders-agent": "http://u:1"},
        audience_override="agentanywhere",
    )
    assert cfg.resolve("/agents/orders-agent/x").audience == "agentanywhere"


# --------------------------------------------------------------------------
# public-path allowlist (candidate/invite bypasses SSO)
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path,is_public",
    [
        ("/agents/orders-agent/invite", True),
        ("/agents/orders-agent/invite/abc123", True),
        ("/agents/orders-agent/api/v1/invites/redeem", True),
        ("/agents/orders-agent/admin", False),       # gated
        ("/agents/orders-agent/invitations", False),  # must not match /invite by substring
    ],
)
def test_is_public_path(path, is_public):
    assert _cfg().resolve(path).is_public is is_public


def test_no_public_paths_for_other_slugs():
    assert _cfg().resolve("/agents/support-agent/invite").is_public is False


# --------------------------------------------------------------------------
# WebSocket upstream URI (http->ws, https->wss, prefix-stripped + query)
# --------------------------------------------------------------------------
def test_ws_upstream_uri_http_to_ws():
    route = _cfg().resolve("/agents/search-agent/ws/copilot")
    assert route.upstream_ws_base == "ws://10.0.0.10:8005"
    assert route.ws_upstream_uri() == "ws://10.0.0.10:8005/ws/copilot"
    assert route.ws_upstream_uri("token=abc") == "ws://10.0.0.10:8005/ws/copilot?token=abc"


def test_ws_upstream_uri_https_to_wss():
    cfg = AgentEdgeConfig(routes={"x": "https://host.example:443"})
    route = cfg.resolve("/agents/x/socket")
    assert route.ws_upstream_uri() == "wss://host.example:443/socket"


# --------------------------------------------------------------------------
# from_settings parsing (JSON env strings)
# --------------------------------------------------------------------------
def test_from_settings_parses_json():
    settings = SimpleNamespace(
        agent_routes_json='{"orders-agent":"http://10.0.0.10:8090","support-agent":"http://10.0.0.10:8001"}',
        agent_public_paths_json='{"orders-agent":["/invite"]}',
        ida_audience=None,
    )
    cfg = AgentEdgeConfig.from_settings(settings)
    assert cfg.slugs == ["orders-agent", "support-agent"]
    assert cfg.resolve("/agents/orders-agent/invite").is_public is True
    assert cfg.resolve("/agents/support-agent/x").upstream_base == "http://10.0.0.10:8001"


@pytest.mark.parametrize("bad", [None, "", "not-json", "[1,2,3]", "42"])
def test_from_settings_tolerates_bad_json(bad):
    settings = SimpleNamespace(
        agent_routes_json=bad,
        agent_public_paths_json=bad,
        ida_audience=None,
    )
    cfg = AgentEdgeConfig.from_settings(settings)
    assert cfg.slugs == []
    assert cfg.resolve("/agents/orders-agent/x") is None
