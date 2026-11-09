"""Regression tests for the launch-hardening fixes:

* D2 — an unreachable backend must surface as 502 BACKEND_ERROR, not a generic
  500 INTERNAL_ERROR.
* D4 — tenant quota enforcement must actually raise 429 (previously the
  RateLimitError was constructed with the wrong arguments and silently swallowed).
"""
import httpx
import pytest
from unittest.mock import AsyncMock, patch

from app.repositories.api_key_repo import APIKeyRepository


@pytest.mark.asyncio
@patch("app.routing.Router.route_request")
async def test_backend_connect_error_maps_to_502(mock_route, client, db_session):
    """D2: httpx.ConnectError from the router -> 502 BACKEND_ERROR envelope."""
    api_key, _ = await APIKeyRepository.create(db=db_session, name="D2 Key")
    mock_route.side_effect = httpx.ConnectError("All connection attempts failed")

    resp = client.get("/mcp/test", headers={"X-API-Key": api_key})

    assert resp.status_code == 502
    body = resp.json()
    assert body["error"] == "BACKEND_ERROR"
    assert "request_id" in body


@pytest.mark.asyncio
@patch("app.routing.Router.route_request")
async def test_backend_timeout_maps_to_502(mock_route, client, db_session):
    """D2: a read timeout is also an upstream failure -> 502, not 500."""
    api_key, _ = await APIKeyRepository.create(db=db_session, name="D2 Timeout Key")
    mock_route.side_effect = httpx.ReadTimeout("timed out")

    resp = client.get("/mcp/test", headers={"X-API-Key": api_key})

    assert resp.status_code == 502
    assert resp.json()["error"] == "BACKEND_ERROR"


@pytest.mark.asyncio
@patch("app.routing.Router.route_request")
async def test_tenant_over_quota_returns_429(mock_route, client, db_session):
    """D4: an over-quota tenant is rejected with 429 (the construction bug that
    disabled this previously would have let the request through)."""
    api_key, _ = await APIKeyRepository.create(db=db_session, name="D4 Key")
    # If auth/routing were reached, this would 200 — proving the quota gate is
    # what stops the request.
    mock_route.return_value = httpx.Response(200, json={"status": "ok"})

    tenant = {
        "tenant_id": "acme",
        "is_active": True,
        "quota": {"requests_per_minute": 10, "requests_per_hour": 100},
    }

    # authenticate() resolves the tenant once (through the repository) and the quota gate reuses
    # it, so the tenant is supplied at the repository.
    from types import SimpleNamespace

    tenant_row = SimpleNamespace(**tenant)
    with patch("app.repositories.tenant_repo.TenantRepository.get_tenant_for_api_key",
               AsyncMock(return_value=tenant_row)), \
         patch("app.repositories.tenant_repo.TenantRepository.to_dict", staticmethod(lambda t: tenant)), \
         patch("app.tenant.tenant_service") as ts:
        ts.prime_quota = AsyncMock()
        ts.clear_quota_prime = lambda db: None
        # Minute window is exhausted; hour window is fine.
        ts.check_quota = AsyncMock(side_effect=[False, True])

        resp = client.get("/mcp/test", headers={"X-API-Key": api_key})

    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "RATE_LIMIT_EXCEEDED"
    assert body["details"]["tenant_id"] == "acme"
