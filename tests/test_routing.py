"""Tests for routing logic."""
import pytest
from unittest.mock import Mock, AsyncMock, patch
from app.routing import Router, settings as routing_settings
from app.models import RouteConfig, ProtocolType
from app.config import get_settings
import httpx

settings = get_settings()


@pytest.fixture
def router():
    """Create router instance."""
    return Router()


def test_get_backend_url_mcp(router):
    """Test MCP backend URL routing."""
    url, protocol = router._get_backend_url("/mcp/query")
    assert url == settings.mcp_backend_url
    assert protocol == ProtocolType.MCP
    
    url, protocol = router._get_backend_url("/api/mcp/test")
    assert url == settings.mcp_backend_url
    assert protocol == ProtocolType.MCP


def test_get_backend_url_a2a(router):
    """Test A2A backend URL routing."""
    url, protocol = router._get_backend_url("/a2a/message")
    assert url == settings.a2a_backend_url
    assert protocol == ProtocolType.A2A
    
    url, protocol = router._get_backend_url("/api/a2a/test")
    assert url == settings.a2a_backend_url
    assert protocol == ProtocolType.A2A


def test_get_backend_url_ag_ui(router):
    with patch.object(routing_settings, "ag_ui_backend_url", "http://agui-backend:9000"):
        url, protocol = router._get_backend_url("/agui/events")
        assert url == "http://agui-backend:9000"
        assert protocol == ProtocolType.AG_UI


def test_get_backend_url_ag_ui_not_configured(router):
    with patch.object(routing_settings, "ag_ui_backend_url", None):
        with pytest.raises(ValueError, match="AG-UI backend URL not configured"):
            router._get_backend_url("/agui/x")


def test_get_backend_url_acp(router):
    with patch.object(routing_settings, "acp_backend_url", "http://acp:8800"):
        url, protocol = router._get_backend_url("/api/acp/runs")
        assert url == "http://acp:8800"
        assert protocol == ProtocolType.ACP


def test_get_backend_url_acp_not_configured(router):
    with patch.object(routing_settings, "acp_backend_url", None):
        with pytest.raises(ValueError, match="ACP backend URL not configured"):
            router._get_backend_url("/acp/ping")


def test_tier2_path_strip(router):
    with patch.object(routing_settings, "acp_backend_url", "http://acp:8800"):
        p = router._apply_tier2_path_strip("/acp/runs", ProtocolType.ACP)
        assert p == "/runs"
    with patch.object(routing_settings, "ag_ui_backend_url", "http://x:1"):
        p = router._apply_tier2_path_strip("/api/agui/v1/stream", ProtocolType.AG_UI)
        assert p == "/v1/stream"


def test_get_backend_url_http(router):
    """Test HTTP backend URL routing."""
    with patch.object(settings, 'http_backend_url', 'http://http-backend:8003'):
        url, protocol = router._get_backend_url("/api/users")
        assert url == settings.http_backend_url
        assert protocol == ProtocolType.HTTP


def test_get_backend_url_mqtt(router):
    """Test MQTT backend URL routing."""
    with patch.object(settings, 'mqtt_broker_url', 'mqtt://localhost:1883'):
        url, protocol = router._get_backend_url("/mqtt/publish")
        assert url == settings.mqtt_broker_url
        assert protocol == ProtocolType.MQTT
        
        url, protocol = router._get_backend_url("/api/mqtt/subscribe")
        assert url == settings.mqtt_broker_url
        assert protocol == ProtocolType.MQTT


def test_get_backend_url_mqtt_not_configured(router):
    """Test MQTT routing when not configured."""
    with patch.object(settings, 'mqtt_broker_url', None):
        with pytest.raises(ValueError, match="MQTT broker URL not configured"):
            router._get_backend_url("/mqtt/publish")


def test_get_backend_url_websocket(router):
    """Test WebSocket backend URL routing."""
    with patch.object(settings, 'websocket_backend_url', 'ws://localhost:8080'):
        url, protocol = router._get_backend_url("/ws/endpoint")
        assert url == settings.websocket_backend_url
        assert protocol == ProtocolType.WEBSOCKET
        
        url, protocol = router._get_backend_url("/websocket/chat")
        assert url == settings.websocket_backend_url
        assert protocol == ProtocolType.WEBSOCKET


def test_get_backend_url_websocket_not_configured(router):
    """Test WebSocket routing when not configured."""
    with patch.object(settings, 'websocket_backend_url', None):
        with pytest.raises(ValueError, match="WebSocket backend URL not configured"):
            router._get_backend_url("/ws/endpoint")


def test_get_backend_url_grpc(router):
    """Test gRPC backend URL routing."""
    with patch.object(settings, 'grpc_backend_url', 'localhost:50051'):
        url, protocol = router._get_backend_url("/grpc/call")
        assert url == settings.grpc_backend_url
        assert protocol == ProtocolType.GRPC
        
        url, protocol = router._get_backend_url("/api/grpc/service")
        assert url == settings.grpc_backend_url
        assert protocol == ProtocolType.GRPC


def test_get_backend_url_grpc_not_configured(router):
    """Test gRPC routing when not configured."""
    with patch.object(settings, 'grpc_backend_url', None):
        with pytest.raises(ValueError, match="gRPC backend URL not configured"):
            router._get_backend_url("/grpc/call")


def test_get_backend_url_graphql(router):
    """Test GraphQL backend URL routing."""
    with patch.object(settings, 'graphql_backend_url', 'http://localhost:4000'):
        url, protocol = router._get_backend_url("/graphql")
        assert url == settings.graphql_backend_url
        assert protocol == ProtocolType.GRAPHQL
        
        url, protocol = router._get_backend_url("/api/graphql/query")
        assert url == settings.graphql_backend_url
        assert protocol == ProtocolType.GRAPHQL


def test_get_backend_url_graphql_not_configured(router):
    """Test GraphQL routing when not configured."""
    with patch.object(settings, 'graphql_backend_url', None):
        with pytest.raises(ValueError, match="GraphQL backend URL not configured"):
            router._get_backend_url("/graphql")


def test_get_backend_url_default(router):
    """Test default routing."""
    # Without HTTP backend, defaults to MCP
    with patch.object(settings, 'http_backend_url', None):
        url, protocol = router._get_backend_url("/unknown/path")
        assert url == settings.mcp_backend_url
        assert protocol == ProtocolType.MCP


def test_check_circuit_breaker_closed(router):
    """Test circuit breaker in closed state."""
    backend_url = "http://test-backend:8000"
    result = router._check_circuit_breaker(backend_url)
    assert result is True


def test_check_circuit_breaker_open(router):
    """Test circuit breaker in open state."""
    import time
    backend_url = "http://test-backend:8000"
    
    # Open circuit breaker
    router.circuit_breakers[backend_url] = {
        "state": "open",
        "opened_at": time.time() - 10,  # Opened 10 seconds ago
        "failure_count": 5,
    }
    
    with patch.object(settings, 'circuit_breaker_timeout', 5):
        result = router._check_circuit_breaker(backend_url)
        # Should be half-open after timeout
        assert result is True


def test_check_circuit_breaker_recently_opened(router):
    """Test circuit breaker recently opened."""
    import time
    backend_url = "http://test-backend:8000"
    
    # Open circuit breaker recently
    router.circuit_breakers[backend_url] = {
        "state": "open",
        "opened_at": time.time(),  # Just opened
        "failure_count": 5,
    }
    
    with patch.object(settings, 'circuit_breaker_timeout', 60):
        result = router._check_circuit_breaker(backend_url)
        assert result is False  # Should be blocked


def test_record_success(router):
    """Test recording successful request."""
    backend_url = "http://test-backend:8000"
    
    # Set to half-open
    router.circuit_breakers[backend_url] = {
        "state": "half-open",
        "failure_count": 3,
    }
    
    router._record_success(backend_url)
    
    cb = router.circuit_breakers[backend_url]
    assert cb["state"] == "closed"
    assert cb["failure_count"] == 0


def test_record_failure(router):
    """Test recording failed request."""
    import time
    backend_url = "http://test-backend:8000"
    
    # Record multiple failures
    for i in range(5):
        router._record_failure(backend_url)
    
    cb = router.circuit_breakers[backend_url]
    assert cb["failure_count"] == 5
    assert cb["state"] == "open"


@pytest.mark.asyncio
async def test_route_request_success(router):
    """Test successful request routing."""
    with patch.object(router.client, 'request', new_callable=AsyncMock) as mock_request:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {"Content-Type": "application/json"}
        mock_response.aread = AsyncMock(return_value=b'{"result": "ok"}')
        mock_request.return_value = mock_response
        
        response = await router.route_request(
            method="GET",
            path="/test",
            headers={},
            query_params={},
        )
        
        assert response.status_code == 200
        mock_request.assert_called_once()


@pytest.mark.asyncio
async def test_route_request_with_route_config(router):
    """Test routing with custom route config."""
    route_config = RouteConfig(
        path="/custom/*",
        backend_url="http://custom-backend:8000",
        protocol=ProtocolType.HTTP,
        timeout=60,
        rewrite_path="/rewritten",
    )
    
    with patch.object(router.client, 'request', new_callable=AsyncMock) as mock_request:
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.headers = {}
        mock_response.aread = AsyncMock(return_value=b'{}')
        mock_request.return_value = mock_response
        
        response = await router.route_request(
            method="GET",
            path="/custom/test",
            headers={},
            query_params={},
            route_config=route_config,
        )
        
        # Check that rewrite_path was used
        call_args = mock_request.call_args
        assert "custom-backend:8000" in call_args[1]["url"]
        assert "/rewritten" in call_args[1]["url"]


@pytest.mark.asyncio
async def test_route_request_circuit_breaker_open(router):
    """Test routing when circuit breaker is open."""
    import time
    backend_url = settings.mcp_backend_url
    
    # Open circuit breaker
    router.circuit_breakers[backend_url] = {
        "state": "open",
        "opened_at": time.time(),
        "failure_count": 5,
    }
    
    with patch.object(settings, 'circuit_breaker_timeout', 60):
        with pytest.raises(httpx.HTTPStatusError):
            await router.route_request(
                method="GET",
                path="/mcp/test",
                headers={},
                query_params={},
            )


@pytest.mark.asyncio
async def test_route_request_failure_records(router):
    """Test that failures are recorded."""
    backend_url = settings.mcp_backend_url
    
    with patch.object(router.client, 'request', new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = Exception("Connection error")
        
        with pytest.raises(Exception):
            await router.route_request(
                method="GET",
                path="/mcp/test",
                headers={},
                query_params={},
            )
        
        # Check failure was recorded
        cb = router.circuit_breakers.get(backend_url, {})
        assert cb.get("failure_count", 0) > 0

