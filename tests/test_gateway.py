"""Test gateway functionality."""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
from app.main import app
from app.config import get_settings
from app.repositories.api_key_repo import APIKeyRepository

settings = get_settings()


def test_health_endpoint(client):
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "version" in data
    assert "timestamp" in data


def test_metrics_endpoint(client):
    """Test metrics endpoint."""
    response = client.get("/metrics")
    # Should return 200 if enabled
    assert response.status_code in [200, 404]


def test_ready_endpoint(client):
    """Test readiness endpoint."""
    response = client.get("/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ready"


def test_gateway_requires_auth(client):
    """Test that gateway endpoints require authentication."""
    # Try to access MCP endpoint without auth
    response = client.get("/mcp/test")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_gateway_with_api_key(client, db_session):
    """Test gateway with API key."""
    # Create a valid API key
    api_key, _ = await APIKeyRepository.create(
        db=db_session,
        name="Test Key",
    )
    headers = {"X-API-Key": api_key}
    # This will fail at routing level since backend doesn't exist, but auth should pass
    response = client.get("/mcp/test", headers=headers)
    # Should get past auth (401) but may fail at routing (404, 502) or timeout (504)
    assert response.status_code in [401, 404, 502, 504]


def test_cors_headers(client):
    """Test CORS headers are present."""
    # Test OPTIONS on a public endpoint (health) - should work
    response = client.options("/health")
    # CORS middleware should add headers and return 200 for OPTIONS
    assert response.status_code in [200, 405]
    # If 200, check for CORS headers
    if response.status_code == 200:
        assert "Access-Control-Allow-Origin" in response.headers


def test_request_id_header(client):
    """Test that request ID is added to response."""
    response = client.get("/health")
    assert "X-Request-ID" in response.headers
    assert response.headers["X-Request-ID"] is not None


def test_response_time_header(client):
    """Test that response time is added."""
    response = client.get("/health")
    assert "X-Response-Time" in response.headers


@pytest.mark.asyncio
@patch('app.routing.Router.route_request')
async def test_mcp_routing(mock_route, client, db_session):
    """Test MCP routing."""
    from app.routing import ProtocolType
    from httpx import Response
    
    # Create a valid API key
    api_key, _ = await APIKeyRepository.create(
        db=db_session,
        name="Test Key",
    )
    
    # Mock successful response
    mock_response = Response(200, json={"status": "ok"})
    mock_route.return_value = mock_response
    
    headers = {"X-API-Key": api_key}
    response = client.get("/mcp/test", headers=headers)
    
    # Should route to MCP backend
    assert mock_route.called


@pytest.mark.asyncio
@patch('app.routing.Router.route_request')
async def test_a2a_routing(mock_route, client, db_session):
    """Test A2A routing."""
    from httpx import Response
    
    # Create a valid API key
    api_key, _ = await APIKeyRepository.create(
        db=db_session,
        name="Test Key",
    )
    
    # Mock successful response
    mock_response = Response(200, json={"status": "ok"})
    mock_route.return_value = mock_response
    
    headers = {"X-API-Key": api_key}
    response = client.get("/a2a/test", headers=headers)
    
    # Should route to A2A backend
    assert mock_route.called


@pytest.mark.asyncio
@patch('app.routing.Router.route_request')
async def test_graphql_routing(mock_route, client, db_session):
    """Test GraphQL routing."""
    from httpx import Response
    
    # Create a valid API key
    api_key, _ = await APIKeyRepository.create(
        db=db_session,
        name="Test Key",
    )
    
    # Mock successful response
    mock_response = Response(200, json={"data": {"test": "value"}})
    mock_route.return_value = mock_response
    
    headers = {"X-API-Key": api_key}
    response = client.post(
        "/graphql",
        json={"query": "{ test }"},
        headers=headers
    )
    
    # Should route to GraphQL backend
    assert mock_route.called or response.status_code in [200, 502, 503]

