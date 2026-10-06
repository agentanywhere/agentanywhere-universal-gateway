"""Integration tests against a *running* gateway container/server.

These are true end-to-end tests: they require a live gateway reachable at
``GATEWAY_URL`` (default ``http://localhost:8000``). When no server is
reachable — e.g. a plain ``pytest`` run on a laptop or in unit-test CI — the
whole module is skipped rather than failing, so the default test run stays
green. To run them, start the gateway (``docker compose up`` or
``mcp-gateway serve``) and re-run pytest, optionally selecting the marker::

    pytest -m integration

Set ``REQUIRE_INTEGRATION=1`` to turn the skip into a hard failure (useful in
a dedicated integration CI stage where the server must be up).
"""
import os

import pytest
import requests

BASE_URL = os.getenv("GATEWAY_URL", "http://localhost:8000")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "admin-test-token-12345678901234567890")

# Mark every test in this module as an integration test so it can be selected
# or excluded with ``pytest -m "integration"`` / ``pytest -m "not integration"``.
pytestmark = pytest.mark.integration


def _server_reachable(url: str) -> bool:
    try:
        requests.get(f"{url}/health", timeout=2)
        return True
    except requests.RequestException:
        return False


# Skip the entire module at collection time when no live gateway is reachable,
# unless REQUIRE_INTEGRATION=1 forces the tests to run (and fail) in a dedicated
# integration CI stage where the server is expected to be up.
if os.getenv("REQUIRE_INTEGRATION") != "1" and not _server_reachable(BASE_URL):
    pytest.skip(
        f"No gateway reachable at {BASE_URL}. Start the server "
        f"(docker compose up / mcp-gateway serve) or set GATEWAY_URL to run "
        f"these integration tests.",
        allow_module_level=True,
    )


@pytest.fixture
def gateway_url():
    """Get gateway URL."""
    return BASE_URL


@pytest.fixture
def admin_headers():
    """Get admin headers."""
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


class TestContainerHealth:
    """Test container health endpoints."""
    
    def test_health_endpoint(self, gateway_url):
        """Test health endpoint."""
        response = requests.get(f"{gateway_url}/health", timeout=5)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "version" in data
    
    def test_ready_endpoint(self, gateway_url):
        """Test ready endpoint."""
        response = requests.get(f"{gateway_url}/ready", timeout=5)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"
    
    def test_metrics_endpoint(self, gateway_url):
        """Test metrics endpoint."""
        response = requests.get(f"{gateway_url}/metrics", timeout=5)
        assert response.status_code == 200
        assert "gateway_requests_total" in response.text or "requests_total" in response.text


class TestContainerAdminAPI:
    """Test admin API endpoints against container."""
    
    def test_admin_health(self, gateway_url, admin_headers):
        """Test admin health endpoint."""
        response = requests.get(f"{gateway_url}/admin/health", headers=admin_headers, timeout=5)
        assert response.status_code == 200
    
    def test_list_api_keys(self, gateway_url, admin_headers):
        """Test listing API keys."""
        response = requests.get(f"{gateway_url}/admin/api-keys", headers=admin_headers, timeout=5)
        assert response.status_code == 200
        assert isinstance(response.json(), list)
    
    def test_list_routes(self, gateway_url, admin_headers):
        """Test listing routes."""
        response = requests.get(f"{gateway_url}/admin/routes", headers=admin_headers, timeout=5)
        assert response.status_code == 200
        assert isinstance(response.json(), list)
    
    def test_list_tenants(self, gateway_url, admin_headers):
        """Test listing tenants."""
        response = requests.get(f"{gateway_url}/admin/tenants", headers=admin_headers, timeout=5)
        assert response.status_code == 200
        assert isinstance(response.json(), list)
    
    def test_list_oauth2_providers(self, gateway_url, admin_headers):
        """Test listing OAuth2 providers."""
        response = requests.get(f"{gateway_url}/admin/oauth2/providers", headers=admin_headers, timeout=5)
        assert response.status_code == 200
        assert isinstance(response.json(), list)
    
    def test_list_log_exporters(self, gateway_url, admin_headers):
        """Test listing log exporters."""
        response = requests.get(f"{gateway_url}/admin/log-exporters", headers=admin_headers, timeout=5)
        assert response.status_code == 200
        assert isinstance(response.json(), list)


class TestContainerOAuth2:
    """Test OAuth2 endpoints."""
    
    def test_oauth2_authorize_endpoint_exists(self, gateway_url):
        """Test OAuth2 authorize endpoint exists."""
        # Should return 400/422 (missing params) or 401 (auth required), not 404
        response = requests.get(f"{gateway_url}/oauth2/authorize", timeout=5)
        assert response.status_code in [400, 401, 422]  # Bad request, unauthorized, or validation error


class TestContainerGraphQL:
    """Test GraphQL endpoint."""
    
    def test_graphql_endpoint_exists(self, gateway_url):
        """Test GraphQL endpoint exists."""
        # Should return 400 (missing query) not 404
        response = requests.post(f"{gateway_url}/graphql", json={}, timeout=5)
        assert response.status_code in [400, 401, 503]  # Bad request, unauthorized, or not configured

