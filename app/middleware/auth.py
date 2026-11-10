"""Authentication middleware."""
import hashlib
import hmac
from typing import Optional
from app.time_utils import utcnow
from fastapi import Request, HTTPException, status, Depends
from app.exceptions import AuthenticationError
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import get_settings
from app.docs_links import docs_link
from app.secure_compare import constant_time_equals
from app.models import APIKey
from app.database import get_db
from app.repositories.api_key_repo import APIKeyRepository
from app.oauth2 import oauth2_manager
from app import api_key_cache
from app.client_ip import client_ip as _client_ip
from sqlalchemy.ext.asyncio import AsyncSession
import redis
import json
import time
import structlog

settings = get_settings()
security = HTTPBearer(auto_error=False)

logger = structlog.get_logger()


def _normalise_scope_path(path: str) -> Optional[str]:
    """Collapse repeated slashes and drop a trailing one.

    Returns None for a path with ``.`` or ``..`` segments: a scoped key never
    gets to rely on how a backend resolves them.
    """
    if not path.startswith("/"):
        path = "/" + path
    segments = [seg for seg in path.split("/") if seg != ""]
    if any(seg in (".", "..") for seg in segments):
        return None
    return "/" + "/".join(segments)


def path_is_allowed(path: str, patterns) -> bool:
    """Whether ``path`` falls under one of the key's ``allowed_paths``.

    Each pattern is a path prefix matched on whole segments: ``/mcp`` allows
    ``/mcp`` and ``/mcp/tools`` but not ``/mcpx``. A pattern ending in ``*``
    is a plain string prefix (``/api/v*`` allows ``/api/v1`` and ``/api/v2``);
    ``/*`` at the end is the same as no wildcard. ``/`` or ``*`` allows every
    path. An empty list allows every path (checked by the caller).
    Paths with ``.`` or ``..`` segments are refused for scoped keys.
    """
    normalised = _normalise_scope_path(path or "/")
    if normalised is None:
        return False
    for raw in patterns:
        pattern = (raw or "").strip()
        if not pattern:
            continue
        if pattern in ("*", "/", "/*"):
            return True
        if pattern.endswith("/*"):
            pattern = pattern[:-2]
        elif pattern.endswith("*"):
            prefix = _normalise_scope_path(pattern[:-1]) or ""
            # Keep a trailing slash the pattern had before the "*".
            if pattern[:-1].endswith("/") and prefix != "/":
                prefix += "/"
            if normalised.startswith(prefix):
                return True
            continue
        base = _normalise_scope_path(pattern)
        if base is None:
            continue
        if normalised == base or normalised.startswith(base + "/"):
            return True
    return False


class AuthMiddleware:
    """Authentication middleware for API key and JWT validation."""
    
    def __init__(self):
        self.redis_client = None
        if settings.redis_enabled:
            try:
                self.redis_client = redis.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_connect_timeout=settings.redis_socket_timeout,
                    socket_timeout=settings.redis_socket_timeout,
                )
            except Exception:
                pass  # Redis optional
        # After a failed Redis call, skip the cache until this monotonic time
        # so an absent Redis costs one short timeout, not one per request.
        self._redis_down = (None, 0.0)  # (client, retry-at)
        # In-process cache of validated keys, used only when Redis is not (or is no longer)
        # available. Off by default: another worker's change to a key is not seen here until
        # the entry expires, so a key can outlive its revocation by up to this many seconds.
        self._local_ttl = float(getattr(settings, "api_key_local_cache_ttl_seconds", 0) or 0)
        self._local_cache: dict = {}
        self._tenant_cache: dict = {}   # key hash -> (expires, tenant dict or None), same TTL
        api_key_cache.register(self)
    
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
    
    REDIS_RETRY_SECONDS = 30.0

    def _redis_available(self) -> bool:
        client, retry_at = self._redis_down
        return bool(self.redis_client) and (
            client is not self.redis_client or time.monotonic() >= retry_at
        )

    def _redis_failed(self) -> None:
        self._redis_down = (self.redis_client, time.monotonic() + self.REDIS_RETRY_SECONDS)

    def _cache_get(self, key_hash: str) -> Optional[dict]:
        if not self._redis_available():
            if self._local_ttl > 0:
                entry = self._local_cache.get(key_hash)
                if entry is not None:
                    if entry[0] > time.monotonic():
                        return entry[1]
                    self._local_cache.pop(key_hash, None)
            return None
        try:
            cached = self.redis_client.get(api_key_cache.cache_key_for_hash(key_hash))
            return json.loads(cached) if cached else None
        except redis.RedisError:
            self._redis_failed()
            return None
        except Exception:
            return None

    def _cache_put(self, key_hash: str, db_key) -> None:
        use_local = not self._redis_available()
        if use_local and self._local_ttl <= 0:
            return
        try:
            ttl = api_key_cache.CACHE_TTL_SECONDS
            if use_local:
                ttl = self._local_ttl
            if db_key.expires_at:
                remaining = int((db_key.expires_at - utcnow()).total_seconds())
                ttl = max(1, min(ttl, remaining))
            key_data = {
                # The key itself is never written to Redis: the entry is
                # found by the key's hash, and on a hit the principal is
                # rebuilt with the key the caller presented, so a cached key
                # has the same identity as an uncached one (the rate limiter
                # and tenant lookup depend on that).
                "id": db_key.id,
                "name": db_key.name,
                "rate_limit_per_minute": db_key.rate_limit_per_minute,
                "rate_limit_per_hour": db_key.rate_limit_per_hour,
                "allowed_paths": db_key.allowed_paths or [],
                "metadata": APIKeyRepository.public_metadata(db_key),
                "created_at": db_key.created_at.isoformat() if db_key.created_at else None,
                "expires_at": db_key.expires_at.isoformat() if db_key.expires_at else None,
                "is_active": db_key.is_active,
            }
            if use_local:
                if len(self._local_cache) >= 10_000:
                    self._local_cache.clear()
                self._local_cache[key_hash] = (time.monotonic() + ttl, key_data)
                return
            self.redis_client.setex(
                api_key_cache.cache_key_for_hash(key_hash), ttl, json.dumps(key_data)
            )
        except redis.RedisError:
            self._redis_failed()
        except Exception:
            pass

    async def _validate_api_key(self, api_key: str, db: Optional[AsyncSession] = None) -> Optional[APIKey]:
        """Validate API key."""
        if not api_key:
            return None

        key_hash = api_key_cache.hash_key(api_key)

        # Check Redis cache first. Entries are dropped when a key is deleted,
        # disabled or changed (see app.api_key_cache), and never outlive the
        # key's expiry; the checks below are a second line of defence.
        cached = self._cache_get(key_hash)
        if cached:
            try:
                principal = APIKey(key=api_key, **{k: v for k, v in cached.items() if v is not None})
                if principal.is_active and not (
                    principal.expires_at and principal.expires_at < utcnow()
                ):
                    return principal
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

                    # Built before the write: a failed commit expires the row.
                    principal = APIKeyRepository.to_pydantic(db_key, presented_key=api_key)

                    # Update last used; a row from before hashed storage also
                    # loses its plaintext copy here (see APIKeyRepository). This is
                    # bookkeeping: failing to record it (SQLite "database is locked"
                    # under write load, say) must not reject a key just validated.
                    try:
                        await APIKeyRepository.mark_used(db, db_key)
                    except Exception as exc:
                        logger.warning("api_key_mark_used_failed", error=str(exc))
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                        return principal

                    self._cache_put(key_hash, db_key)

                    return APIKeyRepository.to_pydantic(db_key, presented_key=api_key)
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
                    "documentation": docs_link("authentication")
                },
                request_id=request_id
            )

        # Attach tenant association (best effort; don't fail auth)
        if db is not None and hasattr(request, "state"):
            try:
                from app.repositories.tenant_repo import TenantRepository
                tenant_info = None
                cached_at = None
                if self._local_ttl > 0:
                    cached_at = api_key_cache.hash_key(api_key)
                    entry = self._tenant_cache.get(cached_at)
                    if entry is not None and entry[0] > time.monotonic():
                        tenant_info = entry[1]
                        cached_at = False                       # served from the cache
                if cached_at is not False:
                    tenant = await TenantRepository.get_tenant_for_api_key(db, api_key)
                    if tenant and tenant.is_active:
                        tenant_info = TenantRepository.to_dict(tenant)
                    if cached_at:
                        if len(self._tenant_cache) >= 10_000:
                            self._tenant_cache.clear()
                        self._tenant_cache[cached_at] = (time.monotonic() + self._local_ttl, tenant_info)
                request.state.tenant_lookup_done = True
                if tenant_info:
                    request.state.tenant_id = tenant_info.get("tenant_id")
                    request.state.tenant = tenant_info
            except Exception:
                pass

        # Path scope: a key created with allowed_paths may only reach those.
        self._enforce_allowed_paths(request, validated_key)

        # Governance: IP allow/deny + policy engine.
        await self._enforce_governance(request, validated_key)

        return validated_key

    def _enforce_allowed_paths(self, request: Request, principal: APIKey) -> None:
        """Reject a request outside the key's ``allowed_paths`` with 403."""
        patterns = list(getattr(principal, "allowed_paths", None) or [])
        if not patterns:
            return
        path = getattr(getattr(request, "url", None), "path", "") or ""
        if path_is_allowed(path, patterns):
            return
        from app.exceptions import AuthorizationError

        raise AuthorizationError(
            message="This API key is not allowed to access this path",
            details={
                "path": path,
                "allowed_paths": patterns,
                "hint": "The key was created with allowed_paths. Use a key whose scope covers this path.",
            },
            request_id=getattr(getattr(request, "state", None), "request_id", None),
        )

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

        client_ip = _client_ip(request) or ""
        # api_key-scoped IP rules store the key's database id, never the
        # plaintext key. Principals without an id (OAuth2 / break-glass)
        # are simply not subject to key-scoped rules.
        api_key_id = getattr(principal, "id", None)
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

