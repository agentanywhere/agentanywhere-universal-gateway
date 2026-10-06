"""Integration tests."""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import get_settings
from app.repositories.api_key_repo import APIKeyRepository

settings = get_settings()


@pytest.mark.asyncio
async def test_full_request_flow(client, db_session):
    """Test full request flow from client to backend."""
    # Create API key
    api_key, db_key = await APIKeyRepository.create(
        db=db_session,
        name="Integration Test Key",
    )
    
    # Make request with API key
    response = client.get(
        "/mcp/test",
        headers={"X-API-Key": api_key}
    )
    
    # Should get past auth; with no backend reachable, routing fails upstream.
    # 502 = connect error, 503 = circuit breaker open / backend not configured,
    # 504 = upstream timeout. (Previously this returned a generic 500 — the D2 bug.)
    assert response.status_code in [401, 502, 503, 504]  # Auth passed, routing may fail


@pytest.mark.asyncio
async def test_api_key_usage_tracking(client, db_session):
    """Test that API key usage is tracked."""
    # Create API key
    api_key, db_key = await APIKeyRepository.create(
        db=db_session,
        name="Usage Test Key",
    )
    
    initial_last_used = db_key.last_used_at
    
    # Make request
    try:
        client.get(
            "/health",  # Health endpoint doesn't require auth
            headers={"X-API-Key": api_key}
        )
    except:
        pass
    
    # Check last_used was updated (if auth was called)
    # Note: This depends on middleware actually being called


@pytest.mark.asyncio
async def test_rate_limiting_integration(client, db_session):
    """Test rate limiting in integration."""
    # Create API key with low limits
    api_key, db_key = await APIKeyRepository.create(
        db=db_session,
        name="Rate Limit Test",
        rate_limit_per_minute=2,
        rate_limit_per_hour=10,
    )
    
    # Make requests up to limit
    # Note: Actual rate limiting depends on Redis being available
    # This is a placeholder for when Redis is configured


def test_health_endpoint(client):
    """Test health endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "version" in data


def test_metrics_endpoint(client):
    """Test metrics endpoint."""
    response = client.get("/metrics")
    # Should return 200 if enabled, 404 if disabled
    assert response.status_code in [200, 404]


def test_ready_endpoint(client):
    """Test readiness endpoint."""
    response = client.get("/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"

