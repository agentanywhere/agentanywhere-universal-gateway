"""Rate limiting middleware."""
import time
import threading
from typing import Dict, Optional, Tuple
from fastapi import Request, HTTPException, status
from app.exceptions import RateLimitError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from app.config import get_settings
from app.models import APIKey
from app.api_key_cache import hash_key
from app.client_ip import client_ip
import redis
import asyncio

settings = get_settings()


class RateLimiter:
    """Rate limiting using slowapi and Redis."""
    
    def __init__(self):
        self.redis_client = None
        if settings.rate_limit_redis_url:
            try:
                self.redis_client = redis.from_url(
                    settings.rate_limit_redis_url,
                    decode_responses=True,
                    socket_connect_timeout=settings.redis_socket_timeout,
                    socket_timeout=settings.redis_socket_timeout,
                )
            except Exception:
                pass

        # In-memory fixed-window counters used when Redis is not configured, so
        # rate limiting works out of the box on a single instance. Keyed by
        # "<identity>:<window>" -> (window_expiry_epoch, count).
        self._memory_counters: Dict[str, Tuple[float, int]] = {}
        self._memory_lock = threading.Lock()

        # Initialize slowapi limiter
        self.limiter = Limiter(
            key_func=get_remote_address,
            storage_uri=settings.rate_limit_redis_url if settings.rate_limit_redis_url else "memory://",
            default_limits=[f"{settings.rate_limit_per_minute}/minute"]
        )
    
    def get_key_func(self, api_key: Optional[APIKey] = None):
        """Get key function for rate limiting based on API key or IP."""
        def key_func(request: Request):
            if api_key:
                return f"api_key:{api_key.key}"
            return get_remote_address(request)
        return key_func
    
    def check_rate_limit(self, request: Request, api_key: Optional[APIKey] = None) -> bool:
        """Check if request is within rate limits."""
        if not settings.rate_limit_enabled:
            return True
        
        if api_key:
            # Use API key specific limits
            limit_per_min = api_key.rate_limit_per_minute
            limit_per_hour = api_key.rate_limit_per_hour
        else:
            # Use default limits
            limit_per_min = settings.rate_limit_per_minute
            limit_per_hour = settings.rate_limit_per_hour
        
        # Check per-minute limit
        if not self._check_limit(request, api_key, limit_per_min, 60):
            self._notify_exceeded(request, api_key, limit_per_min, "minute")
            # NOTE: this raise must live at the `if` scope, NOT inside a
            # notification `except`. Previously it was mis-indented, so in
            # async production (where create_task does not throw) it was dead
            # code and the per-minute limit was never enforced.
            request_id = getattr(request.state, "request_id", None)
            raise RateLimitError(
                limit=limit_per_min,
                window="minute",
                retry_after=60,  # seconds until the next window, at most
                request_id=request_id
            )

        # Check per-hour limit
        if not self._check_limit(request, api_key, limit_per_hour, 3600):
            self._notify_exceeded(request, api_key, limit_per_hour, "hour")
            request_id = getattr(request.state, "request_id", None)
            raise RateLimitError(
                limit=limit_per_hour,
                window="hour",
                retry_after=3600,
                request_id=request_id
            )

        return True

    def _notify_exceeded(
        self, request: Request, api_key: Optional[APIKey], limit: int, window: str
    ) -> None:
        """Fire the ``rate_limit_exceeded`` webhook and the email/Slack alert.

        Both run as background tasks so the 429 is not delayed; the alert
        side applies ALERT_THRESHOLD_RATE_LIMIT and its own interval. This
        used to read ``api_key.id`` from a principal that had no ``id``, so
        the AttributeError was swallowed and nothing was ever sent.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # called outside the event loop (unit tests): nothing to schedule
        api_key_id = getattr(api_key, "id", None) if api_key else None
        try:
            ip = client_ip(request) or get_remote_address(request)
        except Exception:
            ip = None
        path = str(getattr(getattr(request, "url", None), "path", ""))
        payload = {
            "api_key_id": api_key_id,
            "api_key_name": getattr(api_key, "name", None) if api_key else None,
            "client_ip": ip,
            "limit": limit,
            "window": window,
            "path": path,
        }
        try:
            from app.webhooks import webhook_manager, WebhookEventType

            loop.create_task(
                webhook_manager.trigger_webhook(WebhookEventType.RATE_LIMIT_EXCEEDED, payload)
            )
        except Exception:
            pass  # Don't fail the request if the webhook cannot be scheduled
        try:
            from app.notifications import notification_manager

            loop.create_task(notification_manager.send_rate_limit_alert(
                api_key_id=api_key_id,
                client_ip=ip or "",
                limit=limit,
                window=window,
                path=path,
            ))
        except Exception:
            pass  # Don't fail the request if the alert cannot be scheduled

    def _identity(self, request: Request, api_key: Optional[APIKey]) -> str:
        # Counters are named after a hash of the key, so Redis never holds a
        # usable credential in its key names.
        if api_key:
            return f"key:{hash_key(api_key.key)[:32]}"
        return client_ip(request) or get_remote_address(request)

    def _check_limit(self, request: Request, api_key: Optional[APIKey], limit: int, window: int) -> bool:
        """Return True if the request is within `limit` for the given window.

        Uses Redis when configured; otherwise an in-process fixed-window counter
        so limiting still works on a single instance (previously this path
        silently allowed every request).
        """
        identity = self._identity(request, api_key)
        key = f"rate_limit:{identity}:{window}"

        if not self.redis_client:
            return self._check_limit_memory(key, limit, window)

        try:
            current = self.redis_client.get(key)
            if current and int(current) >= limit:
                return False

            pipe = self.redis_client.pipeline()
            pipe.incr(key)
            pipe.expire(key, window)
            pipe.execute()
            return True
        except Exception:
            # Redis is unreachable — fall back to the in-memory limiter rather
            # than failing open, so a Redis outage does not disable limiting.
            return self._check_limit_memory(key, limit, window)

    def _check_limit_memory(self, key: str, limit: int, window: int) -> bool:
        """In-process fixed-window counter. Thread-safe."""
        now = time.time()
        with self._memory_lock:
            expiry, count = self._memory_counters.get(key, (0.0, 0))
            if now >= expiry:
                # Window elapsed (or first hit) — start a fresh window.
                expiry, count = now + window, 0
            if count >= limit:
                self._memory_counters[key] = (expiry, count)
                return False
            self._memory_counters[key] = (expiry, count + 1)
            return True

