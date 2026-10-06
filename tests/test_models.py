"""Tests for Pydantic models."""
import pytest
from datetime import datetime
from app.models import (
    APIKey,
    RouteConfig,
    RequestLog,
    HealthStatus,
    ErrorResponse,
    ProtocolType,
)


def test_api_key_model():
    """Test APIKey model."""
    key = APIKey(
        key="sk-test123",
        name="Test Key",
        rate_limit_per_minute=60,
        rate_limit_per_hour=1000,
    )
    
    assert key.key == "sk-test123"
    assert key.name == "Test Key"
    assert key.is_active is True
    assert key.created_at is not None


def test_route_config_model():
    """Test RouteConfig model."""
    route = RouteConfig(
        path="/test/*",
        backend_url="http://backend:8000",
        protocol=ProtocolType.HTTP,
        timeout=30,
    )
    
    assert route.path == "/test/*"
    assert route.backend_url == "http://backend:8000"
    assert route.protocol == ProtocolType.HTTP
    assert route.requires_auth is True  # Default


def test_request_log_model():
    """Test RequestLog model."""
    log = RequestLog(
        request_id="req-123",
        method="GET",
        path="/test",
        client_ip="127.0.0.1",
        status_code=200,
        response_time_ms=50.5,
    )
    
    assert log.request_id == "req-123"
    assert log.status_code == 200
    assert log.response_time_ms == 50.5


def test_health_status_model():
    """Test HealthStatus model."""
    health = HealthStatus(
        status="healthy",
        version="1.0.0",
        services={"mcp": "healthy", "a2a": "healthy"}
    )
    
    assert health.status == "healthy"
    assert health.version == "1.0.0"
    assert "mcp" in health.services


def test_error_response_model():
    """Test ErrorResponse model."""
    error = ErrorResponse(
        error="TestError",
        message="Test error message",
        request_id="req-123"
    )
    
    assert error.error == "TestError"
    assert error.message == "Test error message"
    assert error.request_id == "req-123"


def test_protocol_type_enum():
    """Test ProtocolType enum."""
    assert ProtocolType.MCP.value == "mcp"
    assert ProtocolType.A2A.value == "a2a"
    assert ProtocolType.AG_UI.value == "ag_ui"
    assert ProtocolType.ACP.value == "acp"
    assert ProtocolType.HTTP.value == "http"
    assert ProtocolType.MQTT.value == "mqtt"
    assert ProtocolType.WEBSOCKET.value == "websocket"
    assert ProtocolType.GRPC.value == "grpc"

