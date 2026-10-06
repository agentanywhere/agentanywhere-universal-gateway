"""Webhook support for event notifications."""
import asyncio
import httpx
from typing import List, Dict, Any, Optional
from app.time_utils import utcnow
from app.config import get_settings
import structlog

logger = structlog.get_logger()
settings = get_settings()


class WebhookManager:
    """Manages webhook subscriptions and event delivery."""
    
    def __init__(self):
        self.webhooks: Dict[str, List[Dict[str, Any]]] = {}
        self.client = httpx.AsyncClient(timeout=10.0)
        self.retry_attempts = 3
        self.retry_delay = 1.0
    
    async def register_webhook(
        self,
        event_type: str,
        url: str,
        secret: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        filters: Optional[Dict[str, Any]] = None
    ) -> str:
        """Register a webhook for an event type."""
        webhook_id = f"{event_type}_{utcnow().timestamp()}"
        
        webhook = {
            "id": webhook_id,
            "url": url,
            "secret": secret,
            "headers": headers or {},
            "filters": filters or {},
            "enabled": True,
            "created_at": utcnow().isoformat(),
            "failure_count": 0,
            "last_success": None,
            "last_failure": None,
        }
        
        if event_type not in self.webhooks:
            self.webhooks[event_type] = []
        
        self.webhooks[event_type].append(webhook)
        
        logger.info("webhook_registered", event_type=event_type, webhook_id=webhook_id, url=url)
        return webhook_id
    
    async def unregister_webhook(self, event_type: str, webhook_id: str) -> bool:
        """Unregister a webhook."""
        if event_type not in self.webhooks:
            return False
        
        self.webhooks[event_type] = [
            w for w in self.webhooks[event_type] if w["id"] != webhook_id
        ]
        
        logger.info("webhook_unregistered", event_type=event_type, webhook_id=webhook_id)
        return True
    
    async def trigger_webhook(
        self,
        event_type: str,
        payload: Dict[str, Any],
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Trigger webhooks for an event type."""
        if event_type not in self.webhooks:
            return []
        
        results = []
        
        for webhook in self.webhooks[event_type]:
            if not webhook.get("enabled", True):
                continue
            
            # Apply filters
            if filters and not self._matches_filters(webhook.get("filters", {}), filters):
                continue
            
            # Prepare payload
            webhook_payload = {
                "event_type": event_type,
                "timestamp": utcnow().isoformat(),
                "data": payload,
            }
            
            # Add signature if secret is provided
            if webhook.get("secret"):
                import hmac
                import hashlib
                import json
                signature = hmac.new(
                    webhook["secret"].encode(),
                    json.dumps(webhook_payload, sort_keys=True).encode(),
                    hashlib.sha256
                ).hexdigest()
                webhook_payload["signature"] = signature
            
            # Send webhook
            result = await self._send_webhook(webhook, webhook_payload)
            results.append(result)
        
        return results
    
    async def _send_webhook(
        self,
        webhook: Dict[str, Any],
        payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Send webhook with retry logic."""
        url = webhook["url"]
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "MCP-A2A-Gateway/1.0",
            **webhook.get("headers", {}),
        }
        
        for attempt in range(self.retry_attempts):
            try:
                response = await self.client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=10.0
                )
                
                if response.status_code < 400:
                    webhook["failure_count"] = 0
                    webhook["last_success"] = utcnow().isoformat()
                    logger.info(
                        "webhook_delivered",
                        webhook_id=webhook["id"],
                        url=url,
                        status_code=response.status_code
                    )
                    return {
                        "webhook_id": webhook["id"],
                        "status": "success",
                        "status_code": response.status_code,
                        "attempt": attempt + 1,
                    }
                else:
                    raise httpx.HTTPStatusError(
                        f"HTTP {response.status_code}",
                        request=response.request,
                        response=response
                    )
            
            except Exception as e:
                webhook["failure_count"] = webhook.get("failure_count", 0) + 1
                webhook["last_failure"] = utcnow().isoformat()
                
                if attempt < self.retry_attempts - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                
                logger.error(
                    "webhook_failed",
                    webhook_id=webhook["id"],
                    url=url,
                    error=str(e),
                    attempt=attempt + 1
                )
                
                # Disable webhook after too many failures
                if webhook["failure_count"] >= 10:
                    webhook["enabled"] = False
                    logger.warning(
                        "webhook_disabled",
                        webhook_id=webhook["id"],
                        url=url,
                        failure_count=webhook["failure_count"]
                    )
                
                return {
                    "webhook_id": webhook["id"],
                    "status": "failed",
                    "error": str(e),
                    "attempt": attempt + 1,
                }
        
        return {
            "webhook_id": webhook["id"],
            "status": "failed",
            "error": "Max retries exceeded",
        }
    
    def _matches_filters(self, webhook_filters: Dict[str, Any], event_filters: Dict[str, Any]) -> bool:
        """Check if event matches webhook filters."""
        if not webhook_filters:
            return True
        
        for key, value in webhook_filters.items():
            if key not in event_filters:
                return False
            if event_filters[key] != value:
                return False
        
        return True
    
    def list_webhooks(self, event_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """List all webhooks, optionally filtered by event type."""
        if event_type:
            return self.webhooks.get(event_type, [])
        
        all_webhooks = []
        for webhooks in self.webhooks.values():
            all_webhooks.extend(webhooks)
        
        return all_webhooks
    
    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()


# Global webhook manager instance
webhook_manager = WebhookManager()

# Event types
class WebhookEventType:
    """Webhook event types."""
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    REQUEST_ERROR = "request_error"
    CIRCUIT_BREAKER_OPEN = "circuit_breaker_open"
    CIRCUIT_BREAKER_CLOSED = "circuit_breaker_closed"
    ANOMALY_DETECTED = "anomaly_detected"
    API_KEY_CREATED = "api_key_created"
    API_KEY_REVOKED = "api_key_revoked"
    HIGH_ERROR_RATE = "high_error_rate"
    TRAFFIC_SPIKE = "traffic_spike"


