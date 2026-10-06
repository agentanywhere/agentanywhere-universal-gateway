"""Authentication middleware."""
import hashlib
import hmac
from typing import Optional
from app.time_utils import utcnow
from fastapi import Request, HTTPException, status, Depends
from app.exceptions import AuthenticationError
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import get_settings
from app.secure_compare import constant_time_equals
from app.models import APIKey
from app.database import get_db
from app.repositories.api_key_repo import APIKeyRepository
from app.oauth2 import oauth2_manager
from sqlalchemy.ext.asyncio import AsyncSession
import redis
import json

settings = get_settings()
security = HTTPBearer(auto_error=False)


class AuthMiddleware:
    """Authentication middleware for API key and JWT validation."""
    
    def __init__(self):
        self.redis_client = None
        if settings.redis_enabled:
            try:
                self.redis_client = redis.from_url(settings.redis_url, decode_responses=True)
            except Exception:
                pass  # Redis optional
    
    def _get_api_key_from_header(self, request: Request) -> Optional[str]:
        """Extract API key from request headers."""
        api_key = request.headers.get(settings.api_key_header)
        if not api_key:
            api_key = request.headers.get("Authorization", "").replace("Bearer ", "")
        return api_key if api_key else None

    def _get_bearer_token(self, request: Request) -> Optional[str]:
        """Extract a Bearer token from the Authorization header."""
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip() or None
        return None

    async def _validate_oauth2_token(self, request: Request) -> Optional[APIKey]:
        """If a Bearer token matches a valid OAuth2 access token, build an
        APIKey-shaped principal so downstream code (rate limiting, logs)
        works uniformly.
        """
        token = self._get_bearer_token(request)
        if not token:
            return None
        stored = await oauth2_manager.verify_access_token(token)
        if not stored:
            return None
        return APIKey(
            key=f"oauth2:{stored.token_id}",
            name=f"oauth2:{stored.provider}",
            rate_limit_per_minute=settings.rate_limit_per_minute,
            rate_limit_per_hour=settings.rate_limit_per_hour,
            is_active=True,
            metadata={
                "auth_type": "oauth2",
                "provider": stored.provider,
                "scope": stored.scope,
                "expires_at": stored.expires_at.isoformat(),
            },
        )
    
    async def _validate_api_key(self, api_key: str, db: Optional[AsyncSession] = None) -> Optional[APIKey]:
        """Validate API key."""
        if not api_key:
            return None
        
        # Check Redis cache first
        if self.redis_client:
            try:
                cached = self.redis_client.get(f"api_key:{api_key}")
                if cached:
                    key_data = json.loads(cached)
                    return APIKey(**key_data)
            except Exception:
                pass
        
        # Query database if available
        if db:
            try:
                db_key = await APIKeyRepository.get_by_key(db, api_key)
                if db_key:
                    # Check if expired
                    if db_key.expires_at and db_key.expires_at < utcnow():
                        return None
                    
                    # Check if active
                    if not db_key.is_active:
                        return None
                    
                    # Update last used
                    await APIKeyRepository.update_last_used(db, db_key.id)
                    
                    # Cache in Redis
                    if self.redis_client:
                        try:
                            key_data = {
                                # Must be the same value a database read
                                # yields (see APIKeyRepository.to_pydantic).
                                # This used to be the key's hash, which gave a
                                # cached key a different identity from an
                                # uncached one: tenant lookups hashed a hash
                                # and found nothing, so tenant quotas stopped
                                # being enforced, and the rate limiter counted
                                # one caller under two names.
                                "key": db_key.key,
                                "name": db_key.name,
                                "rate_limit_per_minute": db_key.rate_limit_per_minute,
                                "rate_limit_per_hour": db_key.rate_limit_per_hour,
                                "allowed_paths": db_key.allowed_paths or [],
                                "is_active": db_key.is_active,
                            }
                            self.redis_client.setex(
                                f"api_key:{api_key}",
                                300,  # 5 minute cache
                                json.dumps(key_data)
                            )
                        except Exception:
                            pass
                    
                    return APIKeyRepository.to_pydantic(db_key)
            except Exception:
                pass  # Fall through to legacy validation
        
        # Break-glass bootstrap key: accept the configured SECRET_KEY as an API
        # key ONLY when it has been changed from the shipped default. This lets a
        # fresh install authenticate before any DB key exists, without the old
        # behaviour of trusting *any* value beginning with "sk-" (which allowed
        # `X-API-Key: sk-anything` to authenticate). A default/unconfigured
        # SECRET_KEY grants nothing.
        if (
            settings.secret_key
            and not settings.secret_key.startswith("change-me")
            and constant_time_equals(api_key, settings.secret_key)
        ):
            return APIKey(
                key=api_key,
                name="default",
                rate_limit_per_minute=settings.rate_limit_per_minute,
                rate_limit_per_hour=settings.rate_limit_per_hour,
                is_active=True
            )

        return None
    
    async def authenticate(self, request: Request, db: Optional[AsyncSession] = None) -> Optional[APIKey]:
        """Authenticate request and return API key object if valid.

        Auth precedence:
          1. ``X-API-Key`` header / matching Bearer token (API key DB).
          2. Bearer token issued via OAuth2 (validated against the
             gateway's local token cache).
        """
        api_key = self._get_api_key_from_header(request)

        if not api_key:
            # No API key — fall back to OAuth2 bearer-token auth.
            return await self._validate_oauth2_token(request)

        validated_key = await self._validate_api_key(api_key, db)
        if not validated_key or not validated_key.is_active:
            # API key didn't match — try OAuth2 bearer fallback before
            # giving up.
            oauth_principal = await self._validate_oauth2_token(request)
            if oauth_principal:
                return oauth_principal

            request_id = getattr(request.state, "request_id", None)
            raise AuthenticationError(
                message="Invalid or inactive API key. Please check your API key and try again.",
                details={
                    "hint": "Ensure your API key is correct and active. You can check your API keys in the admin console.",
                    "documentation": "/docs#authentication"
                },
                request_id=request_id
            )

        # Attach tenant association (best effort; don't fail auth)
        if db is not None and hasattr(request, "state"):
            try:
                from app.repositories.tenant_repo import TenantRepository
                tenant = await TenantRepository.get_tenant_for_api_key(db, api_key)
                if tenant and tenant.is_active:
                    request.state.tenant_id = tenant.tenant_id
                    request.state.tenant = TenantRepository.to_dict(tenant)
            except Exception:
                pass

        # Governance: IP allow/deny + policy engine.
        await self._enforce_governance(request, validated_key)

        return validated_key

    async def _enforce_governance(
        self, request: Request, principal: APIKey
    ) -> None:
        """Apply IP rules + policy engine to a successfully-authenticated request.

        Failures here raise an :class:`AuthorizationError` (HTTP 403)
        with a helpful detail. The functions are no-ops when the
        governance engines have no rules / policies loaded.
        """
        try:
            from app.exceptions import AuthorizationError
            from app.governance import ip_rule_engine, policy_engine
        except Exception:
            return

        client_ip = (
            request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "")
        )
        api_key_id = getattr(principal, "key", None)
        tenant_id = getattr(request.state, "tenant_id", None)

        allowed, matched = ip_rule_engine.evaluate(
            client_ip, tenant=tenant_id, api_key_id=api_key_id
        )
        if not allowed:
            raise AuthorizationError(
                message="Request blocked by IP rule",
                details={
                    "ip": client_ip,
                    "rule_id": getattr(matched, "id", None),
                    "scope": getattr(matched, "scope", None),
                },
                request_id=getattr(request.state, "request_id", None),
            )

        decision = policy_engine.evaluate(
            {
                "path": request.url.path,
                "method": request.method,
                "client_ip": client_ip,
                "tenant": tenant_id or "",
                "headers": dict(request.headers),
            }
        )
        if not decision.allowed:
            raise AuthorizationError(
                message=decision.reason or "Request blocked by policy",
                details={"policy_id": decision.policy_id},
                request_id=getattr(request.state, "request_id", None),
            )
        if decision.rate_limit:
            request.state.policy_rate_limit = decision.rate_limit
            request.state.policy_id = decision.policy_id
    
    def verify_hmac_signature(self, request: Request, secret: str) -> bool:
        """Verify HMAC signature for signed requests."""
        signature = request.headers.get("X-Signature")
        if not signature:
            return False
        
        # Get request body
        body = b""
        if hasattr(request, "_body"):
            body = request._body
        else:
            # For streaming requests, this would need to be handled differently
            pass
        
        # Create expected signature
        expected_signature = hmac.new(
            secret.encode(),
            body,
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(signature, expected_signature)

