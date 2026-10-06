"""Tests for webhook functionality."""
import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from app.webhooks import WebhookManager, WebhookEventType
import httpx


@pytest.mark.asyncio
async def test_register_webhook():
    """Test webhook registration."""
    manager = WebhookManager()
    try:
        webhook_id = await manager.register_webhook(
            event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
            url="https://example.com/webhook",
            secret="test-secret"
        )
        
        assert webhook_id is not None
        assert webhook_id.startswith("rate_limit_exceeded_")
        
        webhooks = manager.list_webhooks(WebhookEventType.RATE_LIMIT_EXCEEDED)
        assert len(webhooks) == 1
        assert webhooks[0]["id"] == webhook_id
        assert webhooks[0]["url"] == "https://example.com/webhook"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_unregister_webhook():
    """Test webhook unregistration."""
    manager = WebhookManager()
    try:
        webhook_id = await manager.register_webhook(
            event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
            url="https://example.com/webhook"
        )
        
        success = await manager.unregister_webhook(
            WebhookEventType.RATE_LIMIT_EXCEEDED,
            webhook_id
        )
        
        assert success is True
        
        webhooks = manager.list_webhooks(WebhookEventType.RATE_LIMIT_EXCEEDED)
        assert len(webhooks) == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_trigger_webhook_success():
    """Test successful webhook trigger."""
    manager = WebhookManager()
    try:
        with patch.object(manager.client, 'post') as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_post.return_value = mock_response
            
            await manager.register_webhook(
                event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
                url="https://example.com/webhook"
            )
            
            results = await manager.trigger_webhook(
                WebhookEventType.RATE_LIMIT_EXCEEDED,
                {"test": "data"}
            )
            
            assert len(results) == 1
            assert results[0]["status"] == "success"
            assert results[0]["status_code"] == 200
            mock_post.assert_called_once()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_webhook_with_secret():
    """Test webhook with HMAC signature."""
    manager = WebhookManager()
    try:
        with patch.object(manager.client, 'post') as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_post.return_value = mock_response
            
            await manager.register_webhook(
                event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
                url="https://example.com/webhook",
                secret="test-secret"
            )
            
            results = await manager.trigger_webhook(
                WebhookEventType.RATE_LIMIT_EXCEEDED,
                {"test": "data"}
            )
            
            assert len(results) == 1
            # Check that signature was added
            call_args = mock_post.call_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert payload is not None
            assert "signature" in payload
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_webhook_retry_on_failure():
    """Test webhook retry logic on failure."""
    manager = WebhookManager()
    try:
        with patch.object(manager.client, 'post') as mock_post:
            # First two calls fail, third succeeds
            mock_post.side_effect = [
                httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock(status_code=500)),
                httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock(status_code=500)),
                MagicMock(status_code=200)
            ]
            
            await manager.register_webhook(
                event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
                url="https://example.com/webhook"
            )
            
            results = await manager.trigger_webhook(
                WebhookEventType.RATE_LIMIT_EXCEEDED,
                {"test": "data"}
            )
            
            assert len(results) == 1
            assert results[0]["status"] == "success"
            assert results[0]["attempt"] == 3
            assert mock_post.call_count == 3
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_webhook_filtering():
    """Test webhook filtering."""
    manager = WebhookManager()
    try:
        with patch.object(manager.client, 'post') as mock_post:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_post.return_value = mock_response
            
            # Register webhook with filter
            await manager.register_webhook(
                event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
                url="https://example.com/webhook1",
                filters={"api_key_id": "key123"}
            )
            
            # Register webhook without filter
            await manager.register_webhook(
                event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
                url="https://example.com/webhook2"
            )
            
            # Trigger with matching filter
            results = await manager.trigger_webhook(
                WebhookEventType.RATE_LIMIT_EXCEEDED,
                {"test": "data"},
                filters={"api_key_id": "key123"}
            )
            
            # Should trigger both (one matches filter, one has no filter)
            assert len(results) == 2
            
            # Reset mock
            mock_post.reset_mock()
            
            # Trigger with non-matching filter
            results = await manager.trigger_webhook(
                WebhookEventType.RATE_LIMIT_EXCEEDED,
                {"test": "data"},
                filters={"api_key_id": "key456"}
            )
            
            # Should only trigger the one without filter
            assert len(results) == 1
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_webhook_auto_disable_after_failures():
    """Test webhook auto-disable after too many failures."""
    manager = WebhookManager()
    try:
        with patch.object(manager.client, 'post') as mock_post:
            mock_post.side_effect = httpx.HTTPStatusError(
                "500", request=MagicMock(), response=MagicMock(status_code=500)
            )
            
            webhook_id = await manager.register_webhook(
                event_type=WebhookEventType.RATE_LIMIT_EXCEEDED,
                url="https://example.com/webhook"
            )
            
            # Trigger 10 times to exceed failure threshold
            for _ in range(10):
                await manager.trigger_webhook(
                    WebhookEventType.RATE_LIMIT_EXCEEDED,
                    {"test": "data"}
                )
            
            # Check webhook is disabled
            webhooks = manager.list_webhooks(WebhookEventType.RATE_LIMIT_EXCEEDED)
            assert len(webhooks) == 1
            assert webhooks[0]["enabled"] is False
            assert webhooks[0]["failure_count"] >= 10
    finally:
        await manager.close()

