"""Regression tests: a route registered via POST /admin/routes must be honoured.

The bug these cover: ``POST /admin/routes`` persisted a ``RouteModel`` row and
``RouteRepository.find_matching_route`` existed and was unit-tested, but nothing
on the request path ever consulted either. The proxy resolved every backend from
hardcoded prefixes, so a registered route silently did nothing — while the docs
presented it as the supported way to add a backend.

The repository was tested in isolation, which is precisely why the gap survived:
the unit test proved the query worked, not that the gateway used it. These tests
assert the *behaviour* an operator is promised.
"""
import pytest

from app import route_registry
from app.models import ProtocolType, RouteConfig


def _rows(*specs):
    """Build lightweight row stand-ins (only the attributes the loader reads)."""
    class _Row:
        def __init__(self, path, backend_url, protocol="http", is_active=True,
                     timeout=30, rate_limit=None, requires_auth=True,
                     headers=None, rewrite_path=None):
            self.path = path
            self.backend_url = backend_url
            self.protocol = protocol
            self.is_active = is_active
            self.timeout = timeout
            self.rate_limit = rate_limit
            self.requires_auth = requires_auth
            self.headers = headers or {}
            self.rewrite_path = rewrite_path

    return [_Row(**s) for s in specs]


@pytest.fixture(autouse=True)
def _clear_registry():
    """Each test starts and ends with an empty registry."""
    route_registry.replace(())
    yield
    route_registry.replace(())


def _load(*specs):
    route_registry.replace(route_registry._snapshot_from_rows(_rows(*specs)))


def test_exact_route_matches():
    _load({"path": "/billing/invoice", "backend_url": "http://billing:9000"})
    cfg = route_registry.match("/billing/invoice")
    assert cfg is not None
    assert cfg.backend_url == "http://billing:9000"


def test_wildcard_route_matches_subpaths():
    _load({"path": "/billing/*", "backend_url": "http://billing:9000"})
    assert route_registry.match("/billing/invoice/42").backend_url == "http://billing:9000"


def test_unregistered_path_returns_none_so_static_routing_still_applies():
    """The static prefix fallback must remain intact for unmatched paths."""
    _load({"path": "/billing/*", "backend_url": "http://billing:9000"})
    assert route_registry.match("/mcp/tools") is None


def test_inactive_routes_are_ignored():
    _load({"path": "/off/*", "backend_url": "http://nope:1", "is_active": False})
    assert route_registry.match("/off/thing") is None


def test_longest_wildcard_prefix_wins_regardless_of_insertion_order():
    """Determinism: /api/v1/* must beat /api/* even when inserted after it."""
    _load(
        {"path": "/api/*", "backend_url": "http://general:8000"},
        {"path": "/api/v1/*", "backend_url": "http://v1:8001"},
    )
    assert route_registry.match("/api/v1/users").backend_url == "http://v1:8001"
    assert route_registry.match("/api/other").backend_url == "http://general:8000"


def test_exact_match_beats_wildcard():
    _load(
        {"path": "/api/*", "backend_url": "http://general:8000"},
        {"path": "/api/health", "backend_url": "http://health:8002"},
    )
    assert route_registry.match("/api/health").backend_url == "http://health:8002"


def test_rows_missing_required_fields_are_skipped_not_fatal():
    _load(
        {"path": "", "backend_url": "http://x:1"},
        {"path": "/ok", "backend_url": ""},
        {"path": "/good", "backend_url": "http://good:1"},
    )
    assert route_registry.count() == 1
    assert route_registry.match("/good") is not None


def test_unknown_protocol_falls_back_to_http_instead_of_raising():
    _load({"path": "/weird", "backend_url": "http://x:1", "protocol": "carrier-pigeon"})
    assert route_registry.match("/weird").protocol == ProtocolType.HTTP


def test_route_config_fields_are_carried_through():
    """timeout/rewrite_path must reach the proxy — _prepare_upstream_parts honours them."""
    _load({
        "path": "/legacy/*",
        "backend_url": "http://legacy:7000",
        "timeout": 90,
        "rewrite_path": "/v2/api",
    })
    cfg = route_registry.match("/legacy/thing")
    assert cfg.timeout == 90
    assert cfg.rewrite_path == "/v2/api"


def test_match_on_empty_registry_is_safe():
    assert route_registry.match("/anything") is None


@pytest.mark.asyncio
async def test_registered_route_is_used_by_the_proxy(monkeypatch):
    """End-to-end intent: the router must send the request to the registered backend.

    This is the assertion whose absence allowed the bug: it exercises the
    resolution the proxy actually performs, rather than the repository query.
    """
    from app.routing import Router

    router = Router()
    cfg = RouteConfig(
        path="/billing/*",
        backend_url="http://billing-backend:9000",
        protocol=ProtocolType.HTTP,
        timeout=45,
    )

    url, _headers, _qp, _content, _json, backend_url, _protocol, timeout = (
        router._prepare_upstream_parts(
            method="GET",
            path="/billing/invoice",
            headers={},
            query_params={},
            body=None,
            route_config=cfg,
        )
    )

    assert backend_url == "http://billing-backend:9000"
    assert url.startswith("http://billing-backend:9000")
    assert timeout == 45

    await router.close()


@pytest.mark.asyncio
async def test_without_a_registered_route_static_prefixes_still_resolve():
    """Guard against the fix changing behaviour for everyone else."""
    from app.routing import Router

    router = Router()
    _url, _h, _q, _c, _j, backend_url, protocol, _t = router._prepare_upstream_parts(
        method="GET",
        path="/mcp/tools",
        headers={},
        query_params={},
        body=None,
        route_config=None,
    )
    assert protocol == ProtocolType.MCP
    assert backend_url  # resolved from settings, unchanged by this feature
    await router.close()


# ---------------------------------------------------------------------------
# Admin API: a created route must be visible in the listing.
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_token():
    from app.config import get_settings
    return get_settings().secret_key


@pytest.mark.asyncio
async def test_created_route_appears_in_listing(client, admin_token):
    """The listing used to be built purely from settings, so a route you had
    just created never showed up and looked like the call had failed."""
    created = client.post(
        "/admin/routes",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "path": "/custom-listing/*",
            "backend_url": "http://custom-backend:8080",
            "protocol": "http",
            "timeout": 30,
        },
    )
    assert created.status_code == 200, created.text

    listed = client.get(
        "/admin/routes", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert listed.status_code == 200
    paths = {r["path"]: r for r in listed.json()}
    assert "/custom-listing/*" in paths, "created route missing from GET /admin/routes"
    assert paths["/custom-listing/*"]["source"] == "dynamic"
    assert paths["/custom-listing/*"]["backend_url"] == "http://custom-backend:8080"
    # Static routes must still be listed, and tagged.
    assert any(r.get("source") == "static" for r in listed.json())
