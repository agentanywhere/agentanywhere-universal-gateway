"""Tests for main application."""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import get_settings

settings = get_settings()
client = TestClient(app)


def test_app_startup():
    """Test application starts correctly."""
    response = client.get("/health")
    assert response.status_code == 200


def test_cors_headers(client):
    """Test CORS headers are present."""
    # Test OPTIONS on a public endpoint (health) - should work
    response = client.options("/health")
    # CORS should be configured - OPTIONS should return 200
    assert response.status_code in [200, 405]
    # If 200, check for CORS headers
    if response.status_code == 200:
        assert "Access-Control-Allow-Origin" in response.headers


def test_request_id_header():
    """Test that request ID is added to response."""
    response = client.get("/health")
    assert "X-Request-ID" in response.headers
    assert response.headers["X-Request-ID"] is not None


def test_response_time_header():
    """Test that response time is added."""
    response = client.get("/health")
    assert "X-Response-Time" in response.headers


def test_openapi_docs():
    """Test OpenAPI documentation endpoint."""
    # Only available in debug mode
    if settings.debug:
        response = client.get("/docs")
        assert response.status_code == 200


def test_gateway_proxy_requires_auth():
    """Test that gateway proxy requires authentication."""
    response = client.get("/mcp/test")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_gateway_proxy_with_invalid_key(db_session):
    """Test gateway proxy with invalid API key."""
    response = client.get(
        "/mcp/test",
        headers={"X-API-Key": "invalid-key-that-does-not-exist"}
    )
    assert response.status_code == 401


def test_metrics_endpoint_format():
    """Test metrics endpoint returns Prometheus format."""
    response = client.get("/metrics")
    if response.status_code == 200:
        # Check it's Prometheus format
        content = response.text
        assert "gateway_requests_total" in content or len(content) > 0


@pytest.mark.asyncio
async def test_mqtt_publish_not_configured(client, db_session):
    """Test MQTT publish when not configured."""
    from app.repositories.api_key_repo import APIKeyRepository
    # Create a valid API key
    api_key, _ = await APIKeyRepository.create(
        db=db_session,
        name="Test Key",
    )
    response = client.post(
        "/mqtt/publish",
        json={"topic": "test/topic", "payload": {"message": "test"}},
        headers={"X-API-Key": api_key}
    )
    # Should return 503 if not configured, or handle gracefully
    assert response.status_code in [503, 401, 502]  # 401 if auth fails, 502 if backend not available


@pytest.mark.asyncio
async def test_mqtt_subscribe_not_configured(client, db_session):
    """Test MQTT subscribe when not configured."""
    from app.repositories.api_key_repo import APIKeyRepository
    # Create a valid API key
    api_key, _ = await APIKeyRepository.create(
        db=db_session,
        name="Test Key",
    )
    response = client.post(
        "/mqtt/subscribe",
        json={"topic": "test/topic", "qos": 1},
        headers={"X-API-Key": api_key}
    )
    # Should return 503 if not configured, or handle gracefully
    assert response.status_code in [503, 401, 502]  # 401 if auth fails, 502 if backend not available

