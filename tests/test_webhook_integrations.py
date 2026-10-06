"""Tests for webhook integrations."""
import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from app.routing import Router
from app.ai_service import AIService
from app.webhooks import webhook_manager, WebhookEventType
from app.config import get_settings
import time

settings = get_settings()


@pytest.fixture
def router():
    """Create a router instance."""
    return Router()


@pytest.fixture
def ai_service():
    """Create an AI service instance."""
    return AIService()


@pytest.mark.asyncio
async def test_circuit_breaker_open_webhook(router):
    """Test that webhook is triggered when circuit breaker opens."""
    backend_url = "http://test-backend:8000"
    
    with patch.object(webhook_manager, 'trigger_webhook', new_callable=AsyncMock) as mock_trigger:
        # Simulate failures to open circuit breaker
        for i in range(settings.circuit_breaker_failure_threshold):
            router._record_failure(backend_url)
        
        # Wait a bit for async task to complete
        await asyncio.sleep(0.1)
        
        # Verify webhook was triggered
        assert mock_trigger.called
        call_args = mock_trigger.call_args
        assert call_args[0][0] == WebhookEventType.CIRCUIT_BREAKER_OPEN
        payload = call_args[0][1]
        assert payload["backend_url"] == backend_url
        assert payload["state"] == "open"
        assert payload["failure_count"] >= settings.circuit_breaker_failure_threshold


@pytest.mark.asyncio
async def test_circuit_breaker_close_webhook(router):
    """Test that webhook is triggered when circuit breaker closes."""
    backend_url = "http://test-backend:8000"
    
    # First open the circuit breaker
    for i in range(settings.circuit_breaker_failure_threshold):
        router._record_failure(backend_url)
    
    # Set state to half-open
    router.circuit_breakers[backend_url] = {
        "state": "half-open",
        "failure_count": 0,
        "opened_at": 0
    }
    
    with patch.object(webhook_manager, 'trigger_webhook', new_callable=AsyncMock) as mock_trigger:
        # Record success to close circuit breaker
        router._record_success(backend_url)
        
        # Wait a bit for async task to complete
        await asyncio.sleep(0.1)
        
        # Verify webhook was triggered
        assert mock_trigger.called
        call_args = mock_trigger.call_args
        assert call_args[0][0] == WebhookEventType.CIRCUIT_BREAKER_CLOSED
        payload = call_args[0][1]
        assert payload["backend_url"] == backend_url
        assert payload["state"] == "closed"
        assert payload["previous_state"] == "half-open"


@pytest.mark.asyncio
async def test_anomaly_detection_webhook(ai_service):
    """Test that webhook is triggered when anomaly is detected."""
    import asyncio
    
    # Prepare request data that might trigger an anomaly
    request_data = {
        "request_id": "test-123",
        "method": "GET",
        "path": "/test/anomaly",
        "status_code": 500,  # High error status
        "response_time_ms": 5000,  # Very slow response
        "client_ip": "127.0.0.1",
        "timestamp": "2024-01-01T00:00:00",
        "client_id": "test-client"
    }
    
    with patch.object(webhook_manager, 'trigger_webhook', new_callable=AsyncMock) as mock_trigger:
        # Process request through AI service
        result = ai_service.process_request(request_data)
        
        # Wait a bit for async task to complete
        await asyncio.sleep(0.1)
        
        # If anomaly was detected, webhook should be triggered
        if result.get("is_anomaly"):
            assert mock_trigger.called
            call_args = mock_trigger.call_args
            assert call_args[0][0] == WebhookEventType.ANOMALY_DETECTED
            payload = call_args[0][1]
            assert payload["endpoint"] == "/test/anomaly"
            assert "anomaly_score" in payload
            assert payload["request_id"] == "test-123"


@pytest.mark.asyncio
async def test_webhook_event_types_listed():
    """Test that all webhook event types are available."""
    from admin.api import list_webhook_events
    from fastapi import Depends
    
    # Mock the admin token verification
    with patch('admin.api.verify_admin_token', return_value="admin-token"):
        events = await list_webhook_events(admin_token="admin-token")
        
        # Verify all expected event types are present
        assert WebhookEventType.CIRCUIT_BREAKER_OPEN in events
        assert WebhookEventType.CIRCUIT_BREAKER_CLOSED in events
        assert WebhookEventType.ANOMALY_DETECTED in events
        assert WebhookEventType.RATE_LIMIT_EXCEEDED in events
        assert WebhookEventType.API_KEY_CREATED in events
        assert WebhookEventType.API_KEY_REVOKED in events

