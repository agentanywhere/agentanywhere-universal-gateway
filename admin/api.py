"""Admin API endpoints."""
from fastapi import APIRouter, Depends, HTTPException, status, Response, Body
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
from app.time_utils import utcnow
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import get_settings
from app.secure_compare import constant_time_equals
from app.models import APIKey, RouteConfig, RequestLog, HealthStatus, SettingsProfile, ProtocolSettings, ProtocolType
from app.routing import Router
from app.database import get_db
from app.repositories import APIKeyRepository, RouteRepository, LogRepository
from app.repositories.settings_profile_repo import SettingsProfileRepository
from app.ai_service import AIService
from app.sdk.generator import SDKService
from app.sdk.python_generator import PythonSDKGenerator
from app.sdk.javascript_generator import JavaScriptSDKGenerator
from app.sdk.go_generator import GoSDKGenerator
from app.webhooks import webhook_manager, WebhookEventType
from fastapi.responses import StreamingResponse
import zipfile
import io
from app.metrics import (
    request_count, request_duration, error_count,
    rate_limit_hits, circuit_breaker_state
)
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
import redis
import json
import httpx
import structlog
import time
import asyncio

# Initialize AI service
ai_service = AIService()

settings = get_settings()
router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBearer(auto_error=False)
logger = structlog.get_logger()

# Initialize SDK service
# Use gateway_url_base if configured, otherwise construct from host:port
if settings.gateway_url_base:
    gateway_url = settings.gateway_url_base.rstrip('/')
else:
    # Default to http, but could be https in production
    scheme = "https" if settings.environment == "production" and not settings.debug else "http"
    gateway_url = f"{scheme}://{settings.host}:{settings.port}"
sdk_service = SDKService(gateway_url=gateway_url)
sdk_service.register_generator("python", PythonSDKGenerator(gateway_url))
sdk_service.register_generator("javascript", JavaScriptSDKGenerator(gateway_url))
sdk_service.register_generator("go", GoSDKGenerator(gateway_url))


@router.get("/auth/config", response_model=Dict[str, Any])
async def admin_auth_config():
    """OIDC config the admin SPA needs for OIDC SSO PKCE login."""
    from app.ida_auth import public_config

    return public_config()


@router.post("/auth/login", response_model=Dict[str, Any])
async def admin_login(
    username: str = Body(...),
    password: str = Body(...)
):
    """Local administrator sign-in for the admin console.

    There is no built-in password. The password is the configured admin
    credential: ``ADMIN_TOKEN``, or ``SECRET_KEY`` when it is not the
    placeholder shipped in env.example. On success that same credential is
    returned as the bearer token the console sends on every admin call, so
    there is no second credential store that could drift out of step with
    :func:`verify_admin_token_value`.

    With neither credential configured, sign-in is impossible rather than
    open: this endpoint answers 401 to everything.
    """
    credential = _local_admin_credential()
    user_ok = constant_time_equals(username, settings.admin_username)
    pass_ok = constant_time_equals(password, credential)
    if credential and user_ok and pass_ok:
        return {
            "access_token": credential,
            "token_type": "bearer",
            "username": settings.admin_username,
        }

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid username or password"
    )


def _is_placeholder_secret(key: str) -> bool:
    """True when SECRET_KEY is still the value shipped in env.example."""
    return (not key) or key.startswith("change-me")


def _local_admin_credential() -> str:
    """The credential a local administrator signs in with, or "" if none is set.

    ``ADMIN_TOKEN`` wins. ``SECRET_KEY`` is accepted for single-operator
    deployments, but never while it is still the public placeholder.
    """
    if settings.admin_token:
        return settings.admin_token
    if not _is_placeholder_secret(settings.secret_key):
        return settings.secret_key
    return ""


def _secret_key_is_admin_credential() -> bool:
    """Whether SECRET_KEY may be presented as an admin credential.

    Only when it is not the placeholder, and only when ADMIN_TOKEN is empty,
    unless ADMIN_ACCEPT_SECRET_KEY=true restores the old behaviour of
    accepting both.
    """
    if _is_placeholder_secret(settings.secret_key):
        return False
    if not settings.admin_token:
        return True
    return bool(getattr(settings, "admin_accept_secret_key", False))


def verify_admin_token_value(token: Optional[str]) -> str:
    """Validate a raw admin token string (OIDC SSO, legacy admin token, or SECRET_KEY).

    Shared by the header-based dependency and by endpoints that must authenticate
    from a query parameter (e.g. Server-Sent Events, where the browser's
    ``EventSource`` cannot set an ``Authorization`` header).
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )

    # OIDC SSO (Keycloak-compatible) RS256 access token — primary in production.
    from app import ida_auth

    if ida_auth.enabled() and ida_auth.is_ida_token(token):
        ida_auth.verify_admin_token(token)
        return token

    # Explicit admin token. SECRET_KEY is only a break-glass fallback for
    # single-operator deployments that have not set ADMIN_TOKEN (or that opt
    # back in with ADMIN_ACCEPT_SECRET_KEY), and never while it is the shipped
    # placeholder, which is public. Both are compared in constant time, and a
    # credential containing non-ASCII bytes is simply wrong (401) rather than
    # an unhandled error.
    if settings.admin_token and constant_time_equals(token, settings.admin_token):
        return token
    if _secret_key_is_admin_credential() and constant_time_equals(
        token, settings.secret_key
    ):
        return token

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid admin token",
    )


def verify_admin_token(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    """Verify admin authentication token (OIDC SSO, legacy admin token, or SECRET_KEY)."""
    return verify_admin_token_value(credentials.credentials if credentials else None)


@router.get("/health", response_model=HealthStatus)
async def health_check():
    """Health check endpoint."""
    return HealthStatus(
        status="healthy",
        version=settings.app_version,
        services={}
    )


@router.get("/dashboard/stats")
async def get_dashboard_stats(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Get dashboard statistics for the last hour."""
    try:
        one_hour_ago = utcnow() - timedelta(hours=1)
        stats = await LogRepository.get_stats(db, start_date=one_hour_ago)

        total_requests = int(stats.get("total_requests") or 0)
        total_errors = int(stats.get("total_errors") or 0)
        avg_response_time = float(stats.get("avg_response_time") or 0.0)
        requests_per_minute = total_requests / 60.0 if total_requests else 0.0
        error_rate = (total_errors / total_requests) if total_requests else 0.0

        rate_limit_hits_count = 0
        try:
            redis_client = (
                redis.from_url(settings.rate_limit_redis_url)
                if settings.rate_limit_redis_url
                else None
            )
            if redis_client is not None:
                raw = redis_client.get("rate_limit_hits_total")
                if isinstance(raw, bytes):
                    rate_limit_hits_count = int(raw)
                elif isinstance(raw, (int, str)) and raw:
                    rate_limit_hits_count = int(raw)
        except Exception:
            rate_limit_hits_count = 0

        return {
            "total_requests": total_requests,
            "total_errors": total_errors,
            "avg_response_time": avg_response_time,
            "requests_per_minute": requests_per_minute,
            "rate_limit_hits": rate_limit_hits_count,
            "error_rate": error_rate,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching stats: {str(e)}"
        )


@router.get("/health/detailed")
async def get_detailed_health(admin_token: str = Depends(verify_admin_token)):
    """Get detailed health status of all services."""
    services = {}
    
    # Check Redis
    try:
        if settings.rate_limit_redis_url:
            redis_client = redis.from_url(settings.rate_limit_redis_url)
            redis_client.ping()
            services["redis"] = {"status": "healthy", "response_time_ms": 0}
        else:
            services["redis"] = {"status": "not_configured"}
    except Exception as e:
        services["redis"] = {"status": "unhealthy", "error": str(e)}
    
    # Check backends
    async with httpx.AsyncClient() as client:
        backends = {
            "mcp_backend": settings.mcp_backend_url,
            "a2a_backend": settings.a2a_backend_url,
        }
        
        for name, url in backends.items():
            try:
                start = utcnow()
                response = await client.get(f"{url}/health", timeout=5.0)
                elapsed = (utcnow() - start).total_seconds() * 1000
                services[name] = {
                    "status": "healthy" if response.status_code == 200 else "unhealthy",
                    "response_time_ms": elapsed
                }
            except Exception as e:
                services[name] = {"status": "unhealthy", "error": str(e)}
    
    return {
        "status": "healthy" if all(s.get("status") == "healthy" for s in services.values() if "status" in s) else "degraded",
        "services": services,
        "timestamp": utcnow().isoformat()
    }


@router.get("/api-keys", response_model=List[Dict[str, Any]])
async def list_api_keys(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """List all API keys.

    Each entry has the key's ``id`` (what the delete and toggle endpoints
    take) and a short ``key_prefix`` to recognise it by. The full key is only
    ever returned once, by ``POST /admin/api-keys``.
    """
    db_keys = await APIKeyRepository.list_all(db)
    return [APIKeyRepository.to_public_dict(key) for key in db_keys]


class APIKeyCreateRequest(BaseModel):
    """JSON body accepted by ``POST /admin/api-keys`` (query parameters work too)."""

    name: Optional[str] = None
    rate_limit_per_minute: Optional[int] = None
    rate_limit_per_hour: Optional[int] = None
    expires_at: Optional[str] = None
    expires_in_days: Optional[int] = None
    # A list, or a comma-separated string as in the query parameter.
    allowed_paths: Optional[Any] = None


@router.post("/api-keys", response_model=Dict[str, Any])
async def create_api_key(
    name: Optional[str] = None,
    rate_limit_per_minute: Optional[int] = None,
    rate_limit_per_hour: Optional[int] = None,
    expires_at: Optional[str] = None,
    expires_in_days: Optional[int] = None,
    allowed_paths: Optional[str] = None,
    body: Optional[APIKeyCreateRequest] = Body(None),
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Create a new API key.

    Parameters can be sent as query parameters or as a JSON body with the
    same names (a query parameter wins when both are given). ``name`` is
    required.

    Optional expiry can be given either as an absolute ISO-8601 timestamp
    (``expires_at``) or as a relative number of days from now
    (``expires_in_days``) — providing both is rejected. ``allowed_paths`` is
    an allow-list of path prefixes the key may access: a comma-separated
    string, or a JSON list in the body. Empty means every path.
    """
    if body is not None:
        name = name if name is not None else body.name
        rate_limit_per_minute = (
            rate_limit_per_minute if rate_limit_per_minute is not None else body.rate_limit_per_minute
        )
        rate_limit_per_hour = (
            rate_limit_per_hour if rate_limit_per_hour is not None else body.rate_limit_per_hour
        )
        expires_at = expires_at if expires_at is not None else body.expires_at
        expires_in_days = expires_in_days if expires_in_days is not None else body.expires_in_days
        if allowed_paths is None and body.allowed_paths is not None:
            if isinstance(body.allowed_paths, (list, tuple)):
                allowed_paths = ",".join(str(p) for p in body.allowed_paths)
            elif isinstance(body.allowed_paths, str):
                allowed_paths = body.allowed_paths
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="'allowed_paths' must be a list of paths or a comma-separated string.",
                )
    if not name or not str(name).strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'name' is required (query parameter or JSON body).",
        )
    rate_limit_per_minute = 60 if rate_limit_per_minute is None else rate_limit_per_minute
    rate_limit_per_hour = 1000 if rate_limit_per_hour is None else rate_limit_per_hour
    # --- Resolve / validate expiry -------------------------------------------
    if expires_at is not None and expires_in_days is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either 'expires_at' or 'expires_in_days', not both.",
        )

    expiry: Optional[datetime] = None
    if expires_in_days is not None:
        if expires_in_days <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="'expires_in_days' must be a positive integer.",
            )
        expiry = utcnow() + timedelta(days=expires_in_days)
    elif expires_at is not None:
        try:
            expiry = datetime.fromisoformat(expires_at)
        except (ValueError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'expires_at' is not a valid ISO-8601 datetime: {expires_at!r}",
            )
        # Compare naive-to-naive: drop tzinfo so a tz-aware input still validates.
        expiry_cmp = expiry.replace(tzinfo=None) if expiry.tzinfo else expiry
        if expiry_cmp <= utcnow():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="'expires_at' must be in the future.",
            )

    # --- Parse allowed paths --------------------------------------------------
    parsed_paths: Optional[List[str]] = None
    if allowed_paths is not None:
        parsed_paths = [p.strip() for p in allowed_paths.split(",") if p.strip()]

    key, db_key = await APIKeyRepository.create(
        db,
        name,
        rate_limit_per_minute,
        rate_limit_per_hour,
        allowed_paths=parsed_paths,
        expires_at=expiry,
    )
    
    # Trigger webhook for API key creation
    try:
        await webhook_manager.trigger_webhook(
            WebhookEventType.API_KEY_CREATED,
            {
                "api_key_id": db_key.id,
                "name": name,
                "rate_limit_per_minute": rate_limit_per_minute,
                "rate_limit_per_hour": rate_limit_per_hour,
            }
        )
        # Send email notification
        from app.notifications import notification_manager
        asyncio.create_task(
            notification_manager.send_api_key_alert(
                event_type="created",
                api_key_id=db_key.id,
                api_key_name=name,
                created_by=admin_token[:8] if admin_token else None
            )
        )
    except Exception:
        pass  # Don't fail if webhook fails
    
    return {
        "id": db_key.id,
        "name": db_key.name,
        "key": key,  # Only returned on creation; the gateway keeps only its hash
        "key_prefix": APIKeyRepository.display_prefix(key),
        "rate_limit_per_minute": db_key.rate_limit_per_minute,
        "rate_limit_per_hour": db_key.rate_limit_per_hour,
        "is_active": db_key.is_active,
        "created_at": db_key.created_at.isoformat() if db_key.created_at else None,
        "expires_at": db_key.expires_at.isoformat() if db_key.expires_at else None,
        "allowed_paths": db_key.allowed_paths or [],
    }


@router.delete("/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Revoke an API key."""
    success = await APIKeyRepository.delete(db, key_id)
    if not success:
        raise HTTPException(status_code=404, detail="API key not found")
    
    # Trigger webhook for API key revocation
    try:
        await webhook_manager.trigger_webhook(
            WebhookEventType.API_KEY_REVOKED,
            {"api_key_id": key_id}
        )
        # Send email notification
        from app.notifications import notification_manager
        asyncio.create_task(
            notification_manager.send_api_key_alert(
                event_type="revoked",
                api_key_id=key_id,
                api_key_name=None,
                created_by=None
            )
        )
    except Exception:
        pass
    
    return {"message": "API key deleted"}


@router.put("/api-keys/{key_id}/toggle")
async def toggle_api_key(
    key_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Toggle API key active status."""
    key = await APIKeyRepository.toggle_active(db, key_id)
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")
    
    return {
        "id": key.id,
        "name": key.name,
        "is_active": key.is_active
    }


class OpenAPIRegistrationRequest(BaseModel):
    """Payload for registering an OpenAPI document as MCP tools / A2A skills."""

    name: str
    openapi_document: Dict[str, Any]
    upstream_base_url: str
    description: Optional[str] = None
    path_prefix: str = ""
    expose_mcp_tools: bool = True
    expose_a2a_skills: bool = True


def _policy_priority(value: Any) -> int:
    """Policy priority from a request body: missing/null means the default of 100."""
    if value is None:
        return 100
    if isinstance(value, bool):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'priority' must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="'priority' must be an integer")


def _registration_to_dict(row) -> Dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "upstream_base_url": row.upstream_base_url,
        "path_prefix": row.path_prefix or "",
        "expose_mcp_tools": bool(row.expose_mcp_tools),
        "expose_a2a_skills": bool(row.expose_a2a_skills),
        "is_active": bool(row.is_active),
    }


@router.post("/registrations/openapi", response_model=Dict[str, Any])
async def create_openapi_registration(
    payload: OpenAPIRegistrationRequest,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Register an OpenAPI 3.x document so its operations are advertised as MCP
    tools and A2A skills, and (optionally) proxied to ``upstream_base_url``."""
    from app.tier1_openapi import validate_openapi_document
    from app.tier1_registry import tier1_registry
    from app.repositories.openapi_registration_repo import OpenAPIRegistrationRepository

    try:
        validate_openapi_document(payload.openapi_document)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    existing = await OpenAPIRegistrationRepository.get_active_by_name(db, payload.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An active registration named {payload.name!r} already exists.",
        )

    row = await OpenAPIRegistrationRepository.create(
        db,
        name=payload.name,
        description=payload.description,
        openapi_document=payload.openapi_document,
        upstream_base_url=payload.upstream_base_url,
        path_prefix=payload.path_prefix,
        expose_mcp_tools=payload.expose_mcp_tools,
        expose_a2a_skills=payload.expose_a2a_skills,
    )
    await tier1_registry.reload(db)
    return _registration_to_dict(row)


@router.get("/registrations/openapi", response_model=Dict[str, Any])
async def list_openapi_registrations(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """List all active OpenAPI registrations."""
    from app.tier1_registry import tier1_registry

    await tier1_registry.reload(db)
    regs = tier1_registry.list_registrations()
    return {"count": len(regs), "registrations": regs}


@router.get("/registrations/mcp-tools", response_model=Dict[str, Any])
async def list_registration_mcp_tools(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Return the aggregated MCP tool descriptors synthesized from all
    registrations that expose MCP tools."""
    from app.tier1_registry import tier1_registry

    await tier1_registry.reload(db)
    tools = tier1_registry.aggregated_mcp_tools()
    return {"count": len(tools), "tools": tools}


@router.delete("/registrations/openapi/{reg_id}", response_model=Dict[str, Any])
async def delete_openapi_registration(
    reg_id: str,
    hard: bool = False,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Delete a registration. ``hard=true`` removes the row; otherwise it is
    soft-deleted (marked inactive) so it can be audited."""
    from app.tier1_registry import tier1_registry
    from app.repositories.openapi_registration_repo import OpenAPIRegistrationRepository

    if hard:
        ok = await OpenAPIRegistrationRepository.delete_hard(db, reg_id)
    else:
        ok = await OpenAPIRegistrationRepository.delete_soft(db, reg_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Registration not found")
    await tier1_registry.reload(db)
    return {"message": "Registration deleted", "id": reg_id, "hard": hard}


@router.get("/routes", response_model=List[Dict[str, Any]])
async def list_routes(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """List all routes: statically configured ones plus registered (dynamic) ones."""
    routes = [
        {
            "path": "/mcp/*",
            "backend_url": settings.mcp_backend_url,
            "protocol": "mcp",
            "timeout": settings.default_timeout,
            "requires_auth": True,
        },
        {
            "path": "/a2a/*",
            "backend_url": settings.a2a_backend_url,
            "protocol": "a2a",
            "timeout": settings.default_timeout,
            "requires_auth": True,
        },
    ]
    
    # Add MQTT route if configured
    if settings.mqtt_broker_url:
        routes.append({
            "path": "/mqtt/*",
            "backend_url": settings.mqtt_broker_url,
            "protocol": "mqtt",
            "timeout": settings.default_timeout,
            "requires_auth": True,
        })
    
    # Add WebSocket route if configured
    if settings.websocket_backend_url:
        routes.append({
            "path": "/ws/*",
            "backend_url": settings.websocket_backend_url,
            "protocol": "websocket",
            "timeout": settings.default_timeout,
            "requires_auth": True,
        })
    
    # Add gRPC route if configured
    if settings.grpc_backend_url:
        routes.append({
            "path": "/grpc/*",
            "backend_url": settings.grpc_backend_url,
            "protocol": "grpc",
            "timeout": settings.default_timeout,
            "requires_auth": True,
        })
    
    # Add GraphQL route if configured
    if settings.graphql_backend_url:
        routes.append({
            "path": "/graphql/*, /api/graphql/*",
            "backend_url": settings.graphql_backend_url,
            "protocol": "graphql",
            "timeout": settings.default_timeout,
            "requires_auth": True,
        })
    
    # Add HTTP/REST route if configured
    if settings.http_backend_url:
        routes.append({
            "path": "/api/*, /rest/*, /http/*",
            "backend_url": settings.http_backend_url,
            "protocol": "http",
            "timeout": settings.default_timeout,
            "requires_auth": True,
        })
    else:
        # Add test HTTP endpoint (always available)
        routes.append({
            "path": "/test/http",
            "backend_url": gateway_url,
            "protocol": "http",
            "timeout": settings.default_timeout,
            "requires_auth": False,  # Test endpoint
        })
    
    # Add test endpoints for all protocols
    # Determine WebSocket URL scheme
    ws_scheme = "wss" if gateway_url.startswith("https") else "ws"
    ws_gateway_url = gateway_url.replace("http://", f"{ws_scheme}://").replace("https://", f"{ws_scheme}://")
    
    routes.extend([
        {
            "path": "/test/mcp",
            "backend_url": gateway_url,
            "protocol": "mcp",
            "timeout": settings.default_timeout,
            "requires_auth": False,
        },
        {
            "path": "/test/a2a",
            "backend_url": gateway_url,
            "protocol": "a2a",
            "timeout": settings.default_timeout,
            "requires_auth": False,
        },
        {
            "path": "/test/websocket",
            "backend_url": ws_gateway_url,
            "protocol": "websocket",
            "timeout": settings.default_timeout,
            "requires_auth": False,
        },
        {
            "path": "/test/grpc",
            "backend_url": gateway_url,
            "protocol": "grpc",
            "timeout": settings.default_timeout,
            "requires_auth": False,
        },
    ])

    # Routes registered via POST /admin/routes. These used to be invisible
    # here — the listing was built purely from settings, so a route you had
    # just created never appeared and looked like it had failed. Each entry is
    # tagged with `source` so operators can tell a persisted route from a
    # statically-configured one.
    for entry in routes:
        entry.setdefault("source", "static")
    try:
        db_routes = await RouteRepository.list_all(db, active_only=False)
        for r in db_routes:
            routes.append({
                "id": r.id,
                "path": r.path,
                "backend_url": r.backend_url,
                "protocol": r.protocol,
                "timeout": r.timeout,
                "rate_limit": r.rate_limit,
                "requires_auth": r.requires_auth,
                "rewrite_path": r.rewrite_path,
                "is_active": r.is_active,
                "source": "dynamic",
            })
    except Exception as exc:  # noqa: BLE001
        # Never let the dynamic listing break the static one.
        logger.warning("admin_list_dynamic_routes_failed", error=str(exc))

    return routes


@router.post("/routes", response_model=Dict[str, Any])
async def create_route(
    route: RouteConfig,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Create a new route.

    The route is persisted and then loaded into the live in-memory registry so
    it takes effect immediately in this worker; other replicas converge via the
    periodic refresh. Previously the row was written but never read by the
    proxy, so creating a route silently did nothing.
    """
    db_route = await RouteRepository.create(db, route)

    applied = False
    try:
        from app import route_registry
        await route_registry.refresh_now()
        applied = route_registry.match(route.path.rstrip("*") or route.path) is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("route_registry_refresh_after_create_failed", error=str(exc))

    return {
        "id": db_route.id,
        "path": db_route.path,
        "backend_url": db_route.backend_url,
        "protocol": db_route.protocol,
        "timeout": db_route.timeout,
        "requires_auth": db_route.requires_auth,
        # Tells the operator the route is actually live, not just stored.
        "active": applied,
    }


@router.get("/logs")
async def get_logs(
    limit: int = 100,
    offset: int = 0,
    status_code: Optional[int] = None,
    path: Optional[str] = None,
    level: Optional[str] = None,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Get request logs with filtering.

    ``level`` (``error`` for 5xx, ``warning`` for 4xx) is applied in the query, so a page is
    full and ``total`` counts every matching row. ``limit`` is capped so one call cannot ask
    for the whole table.
    """
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    db_logs, total = await LogRepository.list_logs(
        db, limit=limit, offset=offset, level=level, status_code=status_code, path=path
    )
    # Convert SQLAlchemy models to dicts for JSON serialization
    logs = [
        {
            "id": log.id,
            "request_id": log.request_id,
            "method": log.method,
            "path": log.path,
            "client_ip": log.client_ip,
            "api_key_id": log.api_key_id,
            "status_code": log.status_code,
            "response_time_ms": log.response_time_ms,
            "timestamp": log.timestamp.isoformat() if log.timestamp else None,
            "user_agent": log.user_agent,
            "error": log.error,
            "protocol": log.protocol,
        }
        for log in db_logs
    ]
    return {"logs": logs, "total": total}


@router.get("/ai/insights")
async def get_ai_insights(admin_token: str = Depends(verify_admin_token)):
    """Get comprehensive AI insights."""
    try:
        insights = ai_service.get_ai_insights()
        return insights
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching AI insights: {str(e)}"
        )


@router.get("/ai/anomalies")
async def get_anomalies(
    window_minutes: int = 60,
    admin_token: str = Depends(verify_admin_token)
):
    """Get anomaly detection summary."""
    try:
        summary = ai_service.anomaly_detector.get_anomaly_summary(window_minutes)
        return summary
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching anomalies: {str(e)}"
        )


@router.get("/ai/scaling-recommendations")
async def get_scaling_recommendations(admin_token: str = Depends(verify_admin_token)):
    """Get auto-scaling recommendations."""
    try:
        # Get current metrics (would come from actual system)
        current_metrics = {
            "requests_per_minute": 500,
            "avg_latency_ms": 150,
            "error_rate": 0.01,
            "current_instances": 2,
            "max_capacity_rpm": 1000,
            "cost_per_instance_per_hour": 0.1,
        }
        
        recommendation = ai_service.scaling_recommender.get_scaling_recommendation(current_metrics)
        return recommendation
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching scaling recommendations: {str(e)}"
        )


@router.get("/ai/cost-analysis")
async def get_cost_analysis(admin_token: str = Depends(verify_admin_token)):
    """Get cost optimization analysis."""
    try:
        analysis = ai_service.cost_optimizer.analyze_costs()
        return analysis
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching cost analysis: {str(e)}"
        )


@router.get("/ai/traffic-forecast")
async def get_traffic_forecast(
    hours_ahead: int = 24,
    admin_token: str = Depends(verify_admin_token)
):
    """Get traffic forecasting predictions."""
    try:
        forecast = ai_service.predictive_analytics.predict_traffic(hours_ahead=hours_ahead)
        return {"forecast": forecast, "hours_ahead": hours_ahead}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error fetching traffic forecast: {str(e)}"
        )


@router.get("/sdk/languages")
async def get_supported_languages(admin_token: str = Depends(verify_admin_token)):
    """Get list of supported SDK languages."""
    languages = ["python", "javascript", "go"]
    return {
        "languages": languages,
        "count": len(languages),
        "default": "python"
    }


@router.post("/sdk/generate/{language}")
async def generate_sdk(
    language: str,
    admin_token: str = Depends(verify_admin_token)
):
    """Generate SDK for a specific language."""
    if language not in ["python", "javascript", "go"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported language: {language}"
        )
    
    # Get routes data
    routes = [
        {"path": "/mcp/*", "method": "POST", "protocol": "mcp", "backend_url": gateway_url},
        {"path": "/a2a/*", "method": "POST", "protocol": "a2a", "backend_url": gateway_url},
        {"path": "/api/*", "method": "GET", "protocol": "http", "backend_url": gateway_url},
    ]
    
    sdk_files = sdk_service.generate_sdk(language, routes)
    
    # Create zip file
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for filename, content in sdk_files.items():
            zip_file.writestr(filename, content)
    
    zip_buffer.seek(0)
    
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={language}_sdk.zip"}
    )


@router.get("/sdk/preview/{language}")
async def preview_sdk(
    language: str,
    admin_token: str = Depends(verify_admin_token)
):
    """Preview SDK code for a specific language."""
    if language not in ["python", "javascript", "go"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported language: {language}"
        )
    
    routes = [
        {"path": "/mcp/*", "method": "POST", "protocol": "mcp", "backend_url": gateway_url},
        {"path": "/a2a/*", "method": "POST", "protocol": "a2a", "backend_url": gateway_url},
        {"path": "/api/*", "method": "GET", "protocol": "http", "backend_url": gateway_url},
    ]
    
    sdk_files = sdk_service.generate_sdk(language, routes)
    
    return {
        "language": language,
        "files": sdk_files,
        "file_count": len(sdk_files),
        "preview": list(sdk_files.keys())[0] if sdk_files else None
    }


@router.get("/config")
async def get_config(
    section: Optional[str] = None,
    redacted: bool = True,
    admin_token: str = Depends(verify_admin_token),
):
    """Return the current gateway configuration.

    By default, sensitive fields (secrets, API keys, credentialed URLs)
    are redacted. Pass ``?redacted=false`` to retrieve raw values
    (admin-only).

    Pass ``?section=<name>`` to fetch a single section (e.g. ``logging``).
    Available sections come from :mod:`app.config`.
    """
    from app.config import (
        SECTIONS as _SECTIONS,
        SAFE_RELOAD_FIELDS as _SAFE_RELOAD_FIELDS,
        get_settings as _get_settings,
    )

    s = _get_settings()
    sections = s.as_sections(redacted=redacted)
    if section:
        if section not in sections:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Unknown config section '{section}'. "
                f"Available: {sorted(_SECTIONS.keys())}",
            )
        return {section: sections[section]}
    return {
        "sections": sections,
        "safe_reload_fields": sorted(_SAFE_RELOAD_FIELDS),
    }


@router.get("/config/diff", response_model=Dict[str, Any])
async def get_config_diff(admin_token: str = Depends(verify_admin_token)):
    """Show fields where the current config differs from the defaults."""
    from app.config import get_settings as _get_settings

    diff = _get_settings().diff_from_defaults()
    return {
        "differs": [
            {"field": field, "default": old, "current": new}
            for field, (old, new) in diff.items()
        ],
        "count": len(diff),
    }


@router.get("/config/schema", response_model=Dict[str, Any])
async def get_config_schema(admin_token: str = Depends(verify_admin_token)):
    """Return the JSON Schema for the gateway configuration."""
    from app.config import Settings as _Settings

    return _Settings.model_json_schema()


@router.post("/config/validate", response_model=Dict[str, Any])
async def validate_config(
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
):
    """Dry-run validation of a partial config payload.

    Returns ``{"valid": True}`` or detailed pydantic-style errors.
    Does **not** apply the changes.
    """
    from pydantic import ValidationError as _PydValidationError
    from app.config import Settings as _Settings, get_settings as _get_settings

    try:
        merged = {**_get_settings().model_dump(), **payload}
        _Settings(**merged)
        return {"valid": True}
    except _PydValidationError as e:
        # Coerce error fields to JSON-serializable strings — pydantic
        # error contexts may carry exception instances which FastAPI's
        # serializer cannot handle.
        clean: List[Dict[str, Any]] = []
        for err in e.errors():
            entry = {
                "loc": [str(p) for p in err.get("loc", [])],
                "msg": err.get("msg"),
                "type": err.get("type"),
            }
            ctx = err.get("ctx") or {}
            if ctx:
                entry["ctx"] = {k: str(v) for k, v in ctx.items()}
            clean.append(entry)
        return {"valid": False, "errors": clean}


@router.post("/config/reload", response_model=Dict[str, Any])
async def reload_config(admin_token: str = Depends(verify_admin_token)):
    """Re-read settings from environment / CONFIG_FILE.

    This rebuilds the cached ``Settings`` instance. Subsystems that
    snapshot config at startup (database connections, exporters,
    workers) are *not* re-initialised — call the relevant admin
    endpoint or restart the process for those.
    """
    from app.config import reload_settings as _reload_settings

    new = _reload_settings()
    return {
        "reloaded": True,
        "environment": new.environment,
        "version": new.app_version,
    }


@router.put("/config", response_model=Dict[str, Any])
async def update_config(
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
):
    """Apply a hot-reloadable subset of config to the running gateway.

    Only fields in ``SAFE_RELOAD_FIELDS`` are accepted (logging level,
    sampling, alert thresholds, rate limits, circuit-breaker tuning,
    metrics toggle). All other fields require a process restart.

    Each change is recorded in an in-memory audit log (see
    ``GET /admin/config/audit``).
    """
    from app.config import (
        ConfigError as _ConfigError,
        apply_runtime_changes as _apply_runtime_changes,
    )

    try:
        result = _apply_runtime_changes(payload, actor=admin_token[-8:] if admin_token else None)
    except _ConfigError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )
    return result


@router.get("/config/audit", response_model=List[Dict[str, Any]])
async def get_config_audit(
    limit: int = 100,
    admin_token: str = Depends(verify_admin_token),
):
    """Return the most recent runtime config changes."""
    from app.config import get_config_audit_log as _get_config_audit_log

    return _get_config_audit_log(limit=max(1, min(limit, 500)))


# ==================== Logging runtime controls ====================


@router.get("/logging", response_model=Dict[str, Any])
async def get_logging_config(admin_token: str = Depends(verify_admin_token)):
    """Return the current logging runtime configuration."""
    from app.logging_config import get_config as _get_logging_config

    cfg = _get_logging_config()
    return {
        "level": cfg.level,
        "format": cfg.format,
        "resolved_format": cfg.resolve_format(),
        "service_name": cfg.service_name,
        "service_version": cfg.service_version,
        "environment": cfg.environment,
        "redaction_enabled": cfg.redaction_enabled,
        "redact_keys": sorted(cfg.redact_keys),
        "redact_patterns": list(cfg.redact_patterns),
        "sample_rate": cfg.sample_rate,
        "sample_always_levels": sorted(cfg.sample_always_levels),
        "sample_always_status_ge": cfg.sample_always_status_ge,
        "logger_levels": dict(cfg.logger_levels),
        "include_caller": cfg.include_caller,
        "file_path": cfg.file_path,
        "audit_file_path": cfg.audit_file_path,
        "timestamp_format": cfg.timestamp_format,
    }


@router.put("/logging", response_model=Dict[str, Any])
async def update_logging_config(
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
):
    """Update the logging runtime configuration in place.

    Accepted keys: ``level``, ``format``, ``sample_rate``,
    ``redaction_enabled``, ``redact_keys`` (list), ``redact_patterns``
    (list), ``logger_levels`` (mapping), ``include_caller`` (bool).
    """
    from app.logging_config import update_config as _update_logging
    from app.logging_config import setup_logging as _setup_logging

    allowed = {
        "level", "format", "sample_rate", "redaction_enabled",
        "redact_keys", "redact_patterns", "logger_levels",
        "include_caller", "sample_always_status_ge", "timestamp_format",
    }
    invalid = [k for k in payload if k not in allowed]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown logging fields: {invalid}. Allowed: {sorted(allowed)}",
        )

    if "sample_rate" in payload:
        rate = payload["sample_rate"]
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0.0 <= rate <= 1.0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="sample_rate must be a number between 0.0 and 1.0",
            )
        payload["sample_rate"] = float(rate)

    try:
        # Coerce sets where appropriate.
        if "redact_keys" in payload:
            payload["redact_keys"] = set(payload["redact_keys"])
        cfg = _update_logging(**payload)
    except (TypeError, ValueError) as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    # Re-setup so format / handler / processor changes take effect.
    if any(k in payload for k in ("format", "level", "include_caller")):
        _setup_logging()

    return {
        "level": cfg.level,
        "format": cfg.format,
        "sample_rate": cfg.sample_rate,
        "redaction_enabled": cfg.redaction_enabled,
        "include_caller": cfg.include_caller,
        "logger_levels": dict(cfg.logger_levels),
    }


@router.post("/logging/test", response_model=Dict[str, Any])
async def emit_test_log(
    payload: Optional[Dict[str, Any]] = Body(None),
    admin_token: str = Depends(verify_admin_token),
):
    """Emit a test log line at the chosen level.

    Body: ``{"level": "info", "message": "...", "extra": {...}}``.
    """
    from app.logging_config import get_logger as _get_logger

    payload = payload or {}
    level = (payload.get("level") or "info").lower()
    message = payload.get("message", "admin_test_log")
    extra = payload.get("extra") or {}

    log = _get_logger("admin")
    fn = getattr(log, level, log.info)
    fn(message, source="admin_test", **extra)
    return {"emitted": True, "level": level, "message": message}


@router.get("/settings/profiles", response_model=List[Dict[str, Any]])
async def list_settings_profiles(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """List all settings profiles."""
    profiles = await SettingsProfileRepository.list_all(db)
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "protocol_settings": p.protocol_settings,
            "global_settings": p.global_settings,
            "is_default": p.is_default,
            "created_at": p.created_at.isoformat() if p.created_at else None,
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
        }
        for p in profiles
    ]


@router.get("/settings/profiles/{profile_id}", response_model=Dict[str, Any])
async def get_settings_profile(
    profile_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Get a specific settings profile."""
    profile = await SettingsProfileRepository.get_by_id(db, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    
    return {
        "id": profile.id,
        "name": profile.name,
        "description": profile.description,
        "protocol_settings": profile.protocol_settings,
        "global_settings": profile.global_settings,
        "is_default": profile.is_default,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }


@router.post("/settings/profiles", response_model=Dict[str, Any])
async def create_settings_profile(
    profile: SettingsProfile,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Create a new settings profile."""
    created = await SettingsProfileRepository.create(db, profile)
    
    return {
        "id": created.id,
        "name": created.name,
        "description": created.description,
        "protocol_settings": created.protocol_settings,
        "global_settings": created.global_settings,
        "is_default": created.is_default,
        "created_at": created.created_at.isoformat() if created.created_at else None,
        "updated_at": created.updated_at.isoformat() if created.updated_at else None,
    }


@router.put("/settings/profiles/{profile_id}", response_model=Dict[str, Any])
async def update_settings_profile(
    profile_id: str,
    profile: SettingsProfile,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Update a settings profile."""
    existing = await SettingsProfileRepository.get_by_id(db, profile_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Profile not found")
    
    # Check if name change conflicts
    if profile.name != existing.name:
        name_check = await SettingsProfileRepository.get_by_name(db, profile.name)
        if name_check:
            raise HTTPException(status_code=400, detail="Profile with this name already exists")
    
    profile.id = profile_id
    updated = await SettingsProfileRepository.update(db, profile_id, profile)
    
    return {
        "id": updated.id,
        "name": updated.name,
        "description": updated.description,
        "protocol_settings": updated.protocol_settings,
        "global_settings": updated.global_settings,
        "is_default": updated.is_default,
        "created_at": updated.created_at.isoformat() if updated.created_at else None,
        "updated_at": updated.updated_at.isoformat() if updated.updated_at else None,
    }


@router.delete("/settings/profiles/{profile_id}")
async def delete_settings_profile(
    profile_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Delete a settings profile."""
    success = await SettingsProfileRepository.delete(db, profile_id)
    if not success:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"message": "Profile deleted"}


@router.post("/settings/profiles/{profile_id}/set-default")
async def set_default_profile(
    profile_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Set a profile as the default."""
    profile = await SettingsProfileRepository.set_default(db, profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    return {"message": "Profile set as default", "profile_id": profile.id}


@router.get("/settings/protocols", response_model=List[Dict[str, Any]])
async def get_supported_protocols(
    admin_token: str = Depends(verify_admin_token)
):
    """Get list of supported protocols with their default settings."""
    protocols = [
        {
            "type": "mcp",
            "name": "MCP (Model Context Protocol)",
            "description": "AI/ML model interactions",
            "default_settings": {
                "rate_limit_per_minute": settings.protocol_mcp_rate_limit_per_minute,
                "rate_limit_per_hour": settings.protocol_mcp_rate_limit_per_hour,
                "timeout": settings.default_timeout,
                "circuit_breaker_enabled": settings.circuit_breaker_enabled,
            }
        },
        {
            "type": "a2a",
            "name": "A2A (Agent-to-Agent)",
            "description": "Agent communication protocol",
            "default_settings": {
                "rate_limit_per_minute": settings.protocol_a2a_rate_limit_per_minute,
                "rate_limit_per_hour": settings.protocol_a2a_rate_limit_per_hour,
                "timeout": settings.default_timeout,
                "circuit_breaker_enabled": settings.circuit_breaker_enabled,
            }
        },
        {
            "type": "http",
            "name": "HTTP/REST",
            "description": "Standard RESTful APIs",
            "default_settings": {
                "rate_limit_per_minute": settings.protocol_http_rate_limit_per_minute,
                "rate_limit_per_hour": settings.protocol_http_rate_limit_per_hour,
                "timeout": settings.default_timeout,
                "circuit_breaker_enabled": settings.circuit_breaker_enabled,
                "compression_enabled": True,
            }
        },
        {
            "type": "mqtt",
            "name": "MQTT",
            "description": "IoT messaging and pub/sub",
            "default_settings": {
                "rate_limit_per_minute": settings.protocol_mqtt_rate_limit_per_minute,
                "rate_limit_per_hour": settings.protocol_mqtt_rate_limit_per_hour,
                "timeout": settings.default_timeout,
                "circuit_breaker_enabled": False,
                "qos_level": 1,
                "keep_alive": settings.protocol_mqtt_keep_alive,
            }
        },
        {
            "type": "websocket",
            "name": "WebSocket",
            "description": "Real-time bidirectional communication",
            "default_settings": {
                "rate_limit_per_minute": settings.protocol_websocket_rate_limit_per_minute,
                "rate_limit_per_hour": settings.protocol_websocket_rate_limit_per_hour,
                "timeout": settings.default_timeout,
                "circuit_breaker_enabled": False,
                "max_connections": settings.protocol_websocket_max_connections,
                "keep_alive": settings.protocol_websocket_keep_alive,
            }
        },
        {
            "type": "grpc",
            "name": "gRPC",
            "description": "High-performance RPC with protocol buffers",
            "default_settings": {
                "rate_limit_per_minute": settings.protocol_grpc_rate_limit_per_minute,
                "rate_limit_per_hour": settings.protocol_grpc_rate_limit_per_hour,
                "timeout": settings.default_timeout,
                "circuit_breaker_enabled": settings.circuit_breaker_enabled,
                "compression_enabled": True,
            }
        },
    ]
    return protocols


# ==================== Webhook Management ====================

class WebhookCreateRequest(BaseModel):
    """JSON body of ``POST /admin/webhooks``.

    ``headers`` and ``filters`` were always read from the body; ``url``,
    ``event_type`` (or a list in ``events``) and ``secret`` may now be sent
    there too instead of as query parameters.
    """

    url: Optional[str] = None
    event_type: Optional[str] = None
    events: Optional[List[str]] = None
    secret: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    filters: Optional[Dict[str, Any]] = None


def _known_webhook_events() -> List[str]:
    return [
        value for name, value in vars(WebhookEventType).items()
        if not name.startswith("_") and isinstance(value, str)
    ]


@router.post("/webhooks", response_model=Dict[str, Any])
async def create_webhook(
    event_type: Optional[str] = None,
    url: Optional[str] = None,
    secret: Optional[str] = None,
    body: Optional[WebhookCreateRequest] = Body(None),
    admin_token: str = Depends(verify_admin_token)
):
    """Register a webhook for event notifications.

    ``url`` and ``event_type`` can be query parameters or body fields; the
    body may list several events in ``events``, and one webhook is
    registered per event.
    """
    body = body or WebhookCreateRequest()
    url = url or body.url
    secret = secret if secret is not None else body.secret
    events: List[str] = []
    if event_type:
        events.append(event_type)
    if body.event_type:
        events.append(body.event_type)
    events.extend(body.events or [])
    events = list(dict.fromkeys(e for e in events if e))
    if not url or not events:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'url' and 'event_type' (or 'events') are required.",
        )
    # Unknown names are accepted as before (a plugin may fire its own
    # events), but reported, since the built-in ones never fire them.
    known = _known_webhook_events()
    unknown = [e for e in events if e not in known]
    ids = []
    for event in events:
        ids.append(await webhook_manager.register_webhook(
            event_type=event,
            url=url,
            secret=secret,
            headers=body.headers,
            filters=body.filters,
        ))
    return {
        "id": ids[0],
        "ids": ids,
        "event_type": events[0],
        "events": events,
        "url": url,
        "status": "registered",
        **({"unknown_events": unknown} if unknown else {}),
    }


@router.get("/webhooks", response_model=List[Dict[str, Any]])
async def list_webhooks(
    event_type: Optional[str] = None,
    admin_token: str = Depends(verify_admin_token)
):
    """List all registered webhooks.

    ``?event_type=`` lists only the webhooks for that event. Each row's
    ``event_type`` is the event that webhook was registered for.
    """
    if event_type:
        rows = [(event_type, w) for w in webhook_manager.list_webhooks(event_type)]
    else:
        rows = [
            (event, w)
            for event, hooks in webhook_manager.webhooks.items()
            for w in hooks
        ]
    # Remove sensitive data
    return [
        {
            "id": w["id"],
            "event_type": event,
            "url": w["url"],
            "enabled": w.get("enabled", True),
            "created_at": w.get("created_at"),
            "failure_count": w.get("failure_count", 0),
            "last_success": w.get("last_success"),
            "last_failure": w.get("last_failure"),
        }
        for event, w in rows
    ]


@router.delete("/webhooks/{webhook_id}")
async def delete_webhook(
    webhook_id: str,
    event_type: Optional[str] = None,
    admin_token: str = Depends(verify_admin_token)
):
    """Unregister a webhook. ``event_type`` is optional: without it the
    webhook is looked up under every event."""
    if event_type is None:
        event_type = next(
            (
                ev for ev, hooks in webhook_manager.webhooks.items()
                if any(w.get("id") == webhook_id for w in hooks)
            ),
            None,
        )
        if event_type is None:
            raise HTTPException(status_code=404, detail="Webhook not found")
    success = await webhook_manager.unregister_webhook(event_type, webhook_id)
    if not success:
        raise HTTPException(status_code=404, detail="Webhook not found")
    return {"message": "Webhook unregistered", "webhook_id": webhook_id}


@router.get("/webhooks/events", response_model=List[str])
async def list_webhook_events(admin_token: str = Depends(verify_admin_token)):
    """List all available webhook event types."""
    return [
        WebhookEventType.RATE_LIMIT_EXCEEDED,
        WebhookEventType.REQUEST_ERROR,
        WebhookEventType.CIRCUIT_BREAKER_OPEN,
        WebhookEventType.CIRCUIT_BREAKER_CLOSED,
        WebhookEventType.ANOMALY_DETECTED,
        WebhookEventType.API_KEY_CREATED,
        WebhookEventType.API_KEY_REVOKED,
        WebhookEventType.HIGH_ERROR_RATE,
        WebhookEventType.TRAFFIC_SPIKE,
    ]


# ==================== Request/Response Transformation ====================

@router.post("/transformations", response_model=Dict[str, Any])
async def create_transformation(
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
):
    """Create a declarative transformation rule.

    Body example:

        {
          "name": "add-tracking",
          "path_pattern": "/api/*",
          "methods": ["GET", "POST"],
          "request_config": {
            "headers_set": {"X-Trace-Id": "abc"},
            "headers_remove": ["X-Internal"],
            "query_set": {"v": "2"},
            "body_set": {"meta.source": "gateway"},
            "path_rewrite": {"pattern": "^/api/", "replacement": "/v2/api/"}
          },
          "response_config": {
            "headers_set": {"X-Powered-By": "MCP-Gateway"},
            "status_map": {"500": 502}
          }
        }
    """
    from app.transformers import header_problem, transformation_manager

    if not payload.get("name"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'name' is required",
        )
    if not payload.get("request_config") and not payload.get("response_config"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one of 'request_config' or 'response_config' is required",
        )

    # A header that cannot be written would fail every request the rule applies to.
    problem = header_problem(payload)
    if problem:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)

    rule = transformation_manager.register_from_dict(payload)
    return rule.to_dict()


@router.get("/transformations", response_model=List[Dict[str, Any]])
async def list_transformations(admin_token: str = Depends(verify_admin_token)):
    """List all transformation rules."""
    from app.transformers import transformation_manager

    return [r.to_dict() for r in transformation_manager.list_rules()]


@router.get("/transformations/{rule_id}", response_model=Dict[str, Any])
async def get_transformation(
    rule_id: str,
    admin_token: str = Depends(verify_admin_token),
):
    """Get a single transformation rule by id."""
    from app.transformers import transformation_manager

    rule = transformation_manager.get_rule(rule_id)
    if not rule:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transformation rule '{rule_id}' not found",
        )
    return rule.to_dict()


@router.delete("/transformations/{rule_id}")
async def delete_transformation(
    rule_id: str,
    admin_token: str = Depends(verify_admin_token),
):
    """Delete a transformation rule."""
    from app.transformers import transformation_manager

    if not transformation_manager.remove_rule(rule_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transformation rule '{rule_id}' not found",
        )
    return {"message": f"Transformation rule '{rule_id}' deleted"}


# ==================== API Versioning ====================

@router.post("/versions", response_model=Dict[str, Any])
async def create_version(
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
):
    """Register an API version.

    Body fields: version (required), status, deprecated_date, sunset_date,
    base_path, default, backend_url.
    """
    from app.versioning import version_manager, VersionStatus

    version = payload.get("version")
    if not version:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'version' is required",
        )

    status_str = (payload.get("status") or "active").upper()
    status_enum = (
        VersionStatus[status_str]
        if status_str in VersionStatus.__members__
        else VersionStatus.ACTIVE
    )

    def _parse_dt(val):
        if not val:
            return None
        try:
            return datetime.fromisoformat(val)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date format: {val}. Use ISO 8601.",
            )

    api_version = version_manager.register_version(
        version=str(version),
        status=status_enum,
        deprecated_date=_parse_dt(payload.get("deprecated_date")),
        sunset_date=_parse_dt(payload.get("sunset_date")),
        base_path=payload.get("base_path"),
        default=bool(payload.get("default", False)),
        backend_url=payload.get("backend_url"),
    )

    return api_version.to_dict()


@router.get("/versions", response_model=List[Dict[str, Any]])
async def list_versions(admin_token: str = Depends(verify_admin_token)):
    """List all API versions."""
    from app.versioning import version_manager

    return [v.to_dict() for v in version_manager.list_versions()]


@router.get("/versions/config", response_model=Dict[str, Any])
async def get_version_config(admin_token: str = Depends(verify_admin_token)):
    """Get version-management config (default version, prefix stripping)."""
    from app.versioning import version_manager

    return {
        "default_version": version_manager.default_version,
        "strip_prefix_on_proxy": version_manager.strip_prefix_on_proxy,
    }


@router.put("/versions/config", response_model=Dict[str, Any])
async def update_version_config(
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
):
    """Update version-management config."""
    from app.versioning import version_manager

    if "strip_prefix_on_proxy" in payload:
        version_manager.strip_prefix_on_proxy = bool(payload["strip_prefix_on_proxy"])
    if "default_version" in payload:
        v = payload["default_version"]
        if v and v not in version_manager.versions:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Version '{v}' is not registered",
            )
        version_manager.default_version = v
        if v:
            for ver in version_manager.versions.values():
                ver.default = ver.version == v

    return {
        "default_version": version_manager.default_version,
        "strip_prefix_on_proxy": version_manager.strip_prefix_on_proxy,
    }


@router.get("/versions/{version}", response_model=Dict[str, Any])
async def get_version(
    version: str,
    admin_token: str = Depends(verify_admin_token),
):
    """Get a single API version by name."""
    from app.versioning import version_manager

    v = version_manager.get_version(version)
    if not v:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version '{version}' not found",
        )
    return v.to_dict()


@router.post("/versions/{version}/deprecate")
async def deprecate_version(
    version: str,
    sunset_date: Optional[str] = Body(None),
    admin_token: str = Depends(verify_admin_token),
):
    """Deprecate an API version. Optionally schedule a sunset date."""
    from app.versioning import version_manager

    if version not in version_manager.versions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version '{version}' not found",
        )

    sunset_dt = None
    if sunset_date:
        try:
            sunset_dt = datetime.fromisoformat(sunset_date)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid date format: {sunset_date}. Use ISO 8601.",
            )

    version_manager.deprecate_version(version, sunset_dt)
    return version_manager.get_version(version).to_dict()


@router.delete("/versions/{version}")
async def remove_version(
    version: str,
    hard: bool = False,
    admin_token: str = Depends(verify_admin_token),
):
    """Remove an API version.

    By default this transitions the version to ``REMOVED`` status (which
    causes 410 Gone responses). Pass ``?hard=true`` to delete it entirely.
    """
    from app.versioning import version_manager

    if version not in version_manager.versions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version '{version}' not found",
        )

    if hard:
        del version_manager.versions[version]
        if version_manager.default_version == version:
            version_manager.default_version = next(iter(version_manager.versions), None)
        return {"message": f"Version '{version}' permanently deleted"}

    version_manager.remove_version(version)
    return version_manager.get_version(version).to_dict()


# ==================== OAuth2 Management ====================

@router.post("/oauth2/providers", response_model=Dict[str, Any])
async def register_oauth2_provider(
    name: str = Body(...),
    client_id: str = Body(...),
    client_secret: str = Body(...),
    authorization_endpoint: str = Body(...),
    token_endpoint: str = Body(...),
    userinfo_endpoint: Optional[str] = Body(None),
    introspection_endpoint: Optional[str] = Body(None),
    revocation_endpoint: Optional[str] = Body(None),
    scopes: Optional[List[str]] = Body(None),
    admin_token: str = Depends(verify_admin_token),
):
    """Register an OAuth2 provider.

    Optional ``introspection_endpoint`` (RFC 7662) and
    ``revocation_endpoint`` (RFC 7009) URLs are honored when set.
    """
    from app.oauth2 import oauth2_manager

    oauth2_manager.register_provider(
        name=name,
        client_id=client_id,
        client_secret=client_secret,
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        userinfo_endpoint=userinfo_endpoint,
        introspection_endpoint=introspection_endpoint,
        revocation_endpoint=revocation_endpoint,
        scopes=scopes,
    )

    return {
        "name": name,
        "client_id": client_id,
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
        "userinfo_endpoint": userinfo_endpoint,
        "introspection_endpoint": introspection_endpoint,
        "revocation_endpoint": revocation_endpoint,
        "scopes": scopes,
        "status": "registered",
    }


@router.get("/oauth2/providers", response_model=List[Dict[str, Any]])
async def list_oauth2_providers(admin_token: str = Depends(verify_admin_token)):
    """List all registered OAuth2 providers."""
    from app.oauth2 import oauth2_manager

    providers = []
    for name in oauth2_manager.list_providers():
        client = oauth2_manager.clients[name]
        providers.append({
            "name": name,
            "client_id": client.client_id,
            "authorization_endpoint": client.authorization_endpoint,
            "token_endpoint": client.token_endpoint,
            "userinfo_endpoint": client.userinfo_endpoint,
            "introspection_endpoint": client.introspection_endpoint,
            "revocation_endpoint": client.revocation_endpoint,
            "scopes": client.scopes,
        })

    return providers


@router.delete("/oauth2/providers/{provider_name}")
async def delete_oauth2_provider(
    provider_name: str,
    admin_token: str = Depends(verify_admin_token),
):
    """Unregister an OAuth2 provider."""
    from app.oauth2 import oauth2_manager

    if not oauth2_manager.remove_provider(provider_name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"OAuth2 provider '{provider_name}' not found",
        )

    return {"message": f"OAuth2 provider '{provider_name}' unregistered"}


@router.get("/oauth2/tokens", response_model=List[Dict[str, Any]])
async def list_oauth2_tokens(admin_token: str = Depends(verify_admin_token)):
    """List active OAuth2 tokens issued/cached by the gateway.

    Returns metadata only — never the access or refresh tokens themselves.
    """
    from app.oauth2 import oauth2_manager

    out = []
    for tid, stored in oauth2_manager.tokens.items():
        out.append({
            "token_id": tid,
            "provider": stored.provider,
            "token_type": stored.token_type,
            "scope": stored.scope,
            "expires_at": stored.expires_at.isoformat(),
            "expired": stored.is_expired(),
            "has_refresh_token": stored.refresh_token is not None,
        })
    return out


@router.delete("/oauth2/tokens/{token_id}")
async def revoke_oauth2_token(
    token_id: str,
    admin_token: str = Depends(verify_admin_token),
):
    """Revoke a locally cached OAuth2 token by id."""
    from app.oauth2 import oauth2_manager

    if token_id not in oauth2_manager.tokens:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Token '{token_id}' not found",
        )
    oauth2_manager._remove_token(token_id)
    return {"message": f"Token '{token_id}' revoked"}


# ==================== Multi-Tenant Management ====================

@router.post("/tenants", response_model=Dict[str, Any])
async def create_tenant(
    tenant_id: str = Body(...),
    name: str = Body(...),
    description: Optional[str] = Body(None),
    config: Optional[Dict[str, Any]] = Body(None),
    quota: Optional[Dict[str, Any]] = Body(None),
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db)
):
    """Create a new tenant."""
    from app.tenant import tenant_service

    try:
        return await tenant_service.create_tenant(
            db,
            tenant_id=tenant_id,
            name=name,
            description=description,
            config=config,
            quota=quota,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


@router.get("/tenants", response_model=List[Dict[str, Any]])
async def list_tenants(
    active_only: bool = False,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """List all tenants."""
    from app.tenant import tenant_service

    return await tenant_service.list_tenants(db, active_only=active_only)


@router.get("/tenants/{tenant_id}", response_model=Dict[str, Any])
async def get_tenant(
    tenant_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Get a tenant by ID."""
    from app.tenant import tenant_service

    tenant = await tenant_service.get_tenant(db, tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found"
        )
    return tenant


@router.put("/tenants/{tenant_id}/quota", response_model=Dict[str, Any])
async def update_tenant_quota(
    tenant_id: str,
    quota: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Update tenant quota."""
    from app.tenant import tenant_service

    tenant = await tenant_service.update_quota(db, tenant_id, quota)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found"
        )
    return tenant


@router.put("/tenants/{tenant_id}/config", response_model=Dict[str, Any])
async def update_tenant_config(
    tenant_id: str,
    config: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Update tenant config."""
    from app.tenant import tenant_service

    tenant = await tenant_service.update_config(db, tenant_id, config)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found"
        )
    return tenant


@router.post("/tenants/{tenant_id}/associate-api-key", response_model=Dict[str, Any])
async def associate_api_key_with_tenant(
    tenant_id: str,
    api_key_id: str = Body(...),
    api_key: str = Body(...),
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Associate an API key with a tenant."""
    from app.tenant import tenant_service

    success = await tenant_service.associate_api_key(db, tenant_id, api_key_id, api_key)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found"
        )
    return {
        "tenant_id": tenant_id,
        "api_key_id": api_key_id,
        "api_key": api_key[:8] + "...",
        "message": "API key associated",
    }


@router.delete("/tenants/{tenant_id}/api-keys/{api_key_id}")
async def disassociate_api_key_from_tenant(
    tenant_id: str,
    api_key_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Disassociate an API key from a tenant."""
    from app.tenant import tenant_service

    ok = await tenant_service.disassociate_api_key(db, api_key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Association not found")
    return {"message": "API key disassociated", "tenant_id": tenant_id, "api_key_id": api_key_id}


@router.get("/tenants/{tenant_id}/usage", response_model=Dict[str, Any])
async def get_tenant_usage(
    tenant_id: str,
    window: str = "hour",
    lookback: int = 24,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Get usage counters and history for a tenant."""
    from app.tenant import tenant_service

    if window not in {"minute", "hour", "day"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="window must be one of minute|hour|day",
        )
    tenant = await tenant_service.get_tenant(db, tenant_id)
    if not tenant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found",
        )
    usage = await tenant_service.usage(db, tenant_id, window=window, lookback=lookback)
    return {"tenant_id": tenant_id, **usage}


@router.post("/tenants/{tenant_id}/activate", response_model=Dict[str, Any])
async def activate_tenant(
    tenant_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Activate a tenant."""
    from app.tenant import tenant_service

    tenant = await tenant_service.set_active(db, tenant_id, True)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
    return tenant


@router.post("/tenants/{tenant_id}/deactivate", response_model=Dict[str, Any])
async def deactivate_tenant_endpoint(
    tenant_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate a tenant (without deleting)."""
    from app.tenant import tenant_service

    tenant = await tenant_service.set_active(db, tenant_id, False)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_id}' not found")
    return tenant


@router.delete("/tenants/{tenant_id}")
async def delete_tenant(
    tenant_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Delete a tenant and cascade its API key associations."""
    from app.tenant import tenant_service

    ok = await tenant_service.delete(db, tenant_id)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Tenant '{tenant_id}' not found",
        )
    return {"message": f"Tenant '{tenant_id}' deleted"}


# ==================== Log Export Management ====================

@router.post("/log-exporters/elasticsearch", response_model=Dict[str, Any])
async def configure_elasticsearch_exporter(
    url: str = Body(...),
    index: str = Body("gateway-logs"),
    admin_token: str = Depends(verify_admin_token)
):
    """Configure Elasticsearch log exporter."""
    from app.log_exporters import log_export_manager, ElasticsearchExporter

    exporter = ElasticsearchExporter(url=url, index=index)
    log_export_manager.add_exporter(exporter)

    return {
        "exporter": "elasticsearch",
        "url": url,
        "index": index,
        "status": "configured"
    }


@router.post("/log-exporters/splunk", response_model=Dict[str, Any])
async def configure_splunk_exporter(
    url: str = Body(...),
    token: str = Body(...),
    index: str = Body("gateway"),
    admin_token: str = Depends(verify_admin_token)
):
    """Configure Splunk log exporter."""
    from app.log_exporters import log_export_manager, SplunkExporter

    exporter = SplunkExporter(url=url, token=token, index=index)
    log_export_manager.add_exporter(exporter)

    return {
        "exporter": "splunk",
        "url": url,
        "index": index,
        "status": "configured"
    }


@router.post("/log-exporters/s3", response_model=Dict[str, Any])
async def configure_s3_exporter(
    bucket: str = Body(...),
    prefix: str = Body("gateway-logs/"),
    region: str = Body("us-east-1"),
    access_key: Optional[str] = Body(None),
    secret_key: Optional[str] = Body(None),
    admin_token: str = Depends(verify_admin_token)
):
    """Configure AWS S3 log exporter."""
    from app.log_exporters import log_export_manager, S3Exporter

    exporter = S3Exporter(
        bucket=bucket,
        prefix=prefix,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
    )
    if not exporter.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="boto3 is required for S3 log export. Install boto3.",
        )
    log_export_manager.add_exporter(exporter)
    return {
        "exporter": "s3",
        "bucket": bucket,
        "prefix": prefix,
        "region": region,
        "status": "configured",
    }


@router.post("/log-exporters/cloudwatch", response_model=Dict[str, Any])
async def configure_cloudwatch_exporter(
    log_group: str = Body(...),
    log_stream: str = Body(...),
    region: str = Body("us-east-1"),
    access_key: Optional[str] = Body(None),
    secret_key: Optional[str] = Body(None),
    admin_token: str = Depends(verify_admin_token)
):
    """Configure AWS CloudWatch Logs exporter."""
    from app.log_exporters import log_export_manager, CloudWatchExporter

    exporter = CloudWatchExporter(
        log_group=log_group,
        log_stream=log_stream,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
    )
    if not exporter.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="boto3 is required for CloudWatch log export. Install boto3.",
        )
    log_export_manager.add_exporter(exporter)
    return {
        "exporter": "cloudwatch",
        "log_group": log_group,
        "log_stream": log_stream,
        "region": region,
        "status": "configured",
    }


@router.post("/log-exporters/datadog", response_model=Dict[str, Any])
async def configure_datadog_exporter(
    api_key: str = Body(...),
    site: str = Body("datadoghq.com"),
    service: str = Body("agentanywhere-gateway"),
    source: str = Body("python"),
    tags: Optional[List[str]] = Body(None),
    admin_token: str = Depends(verify_admin_token)
):
    """Configure Datadog log exporter."""
    from app.log_exporters import log_export_manager, DatadogExporter

    exporter = DatadogExporter(
        api_key=api_key,
        site=site,
        service=service,
        source=source,
        tags=tags or [],
    )
    log_export_manager.add_exporter(exporter)
    return {
        "exporter": "datadog",
        "site": site,
        "service": service,
        "status": "configured",
    }


@router.get("/log-exporters", response_model=List[Dict[str, Any]])
async def list_log_exporters(admin_token: str = Depends(verify_admin_token)):
    """List all configured log exporters."""
    from app.log_exporters import log_export_manager

    return log_export_manager.list_exporters()


@router.delete("/log-exporters/{exporter_name}")
async def remove_log_exporter(
    exporter_name: str,
    admin_token: str = Depends(verify_admin_token)
):
    """Remove a log exporter."""
    from app.log_exporters import log_export_manager

    log_export_manager.remove_exporter(exporter_name)

    return {"message": f"Log exporter '{exporter_name}' removed"}


@router.post("/log-exporters/test/{exporter_name}")
async def test_log_exporter(
    exporter_name: str,
    admin_token: str = Depends(verify_admin_token)
):
    """Test connection to a log exporter."""
    from app.log_exporters import log_export_manager

    exporter = log_export_manager.get_exporter(exporter_name)
    if not exporter:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Exporter '{exporter_name}' not found"
        )

    is_connected = await exporter.test_connection()

    return {
        "exporter": exporter_name,
        "connected": is_connected
    }


@router.post("/log-exporters/worker/start", response_model=Dict[str, Any])
async def start_log_export_worker(admin_token: str = Depends(verify_admin_token)):
    """Start the asynchronous log export batching worker."""
    from app.log_exporters import log_export_manager

    await log_export_manager.start()
    return {
        "status": "running",
        "batch_size": log_export_manager.batch_size,
        "flush_interval": log_export_manager.flush_interval,
    }


@router.post("/log-exporters/worker/stop", response_model=Dict[str, Any])
async def stop_log_export_worker(admin_token: str = Depends(verify_admin_token)):
    """Stop the asynchronous log export batching worker."""
    from app.log_exporters import log_export_manager

    await log_export_manager.stop()
    return {"status": "stopped"}


@router.get("/log-exporters/worker/status", response_model=Dict[str, Any])
async def log_export_worker_status(admin_token: str = Depends(verify_admin_token)):
    """Return the current state of the log export worker."""
    from app.log_exporters import log_export_manager

    return {
        "running": log_export_manager.is_running(),
        "batch_size": log_export_manager.batch_size,
        "flush_interval": log_export_manager.flush_interval,
        "max_queue_size": log_export_manager.max_queue_size,
        "exporters": [e.name for e in log_export_manager.exporters],
    }


# ==================== Email Notification Configuration ====================

@router.post("/notifications/email/configure", response_model=Dict[str, Any])
async def configure_email_notifications(
    smtp_host: str = Body(...),
    smtp_port: int = Body(587),
    smtp_user: Optional[str] = Body(None),
    smtp_password: Optional[str] = Body(None),
    smtp_use_tls: bool = Body(True),
    from_email: Optional[str] = Body(None),
    admin_token: str = Depends(verify_admin_token)
):
    """Configure email notification service."""
    from app.notifications import notification_manager
    
    notification_manager.configure_email(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        smtp_use_tls=smtp_use_tls,
        from_email=from_email
    )
    
    return {
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "status": "configured"
    }


@router.post("/notifications/email/recipients", response_model=Dict[str, Any])
async def set_email_recipients(
    emails: List[str] = Body(...),
    admin_token: str = Depends(verify_admin_token)
):
    """Set email recipients for alerts."""
    from app.notifications import notification_manager
    
    notification_manager.set_alert_recipients(emails)
    
    return {
        "recipients": emails,
        "count": len(emails),
        "status": "updated"
    }


@router.post("/notifications/email/test", response_model=Dict[str, Any])
async def test_email_notifications(
    admin_token: str = Depends(verify_admin_token)
):
    """Test email notification service."""
    from app.notifications import notification_manager
    
    if not notification_manager.email_service:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email service not configured"
        )
    
    is_connected = await notification_manager.email_service.test_connection()
    
    return {
        "service": "email",
        "connected": is_connected
    }


@router.get("/notifications/alert-thresholds", response_model=Dict[str, Any])
async def get_alert_thresholds(
    admin_token: str = Depends(verify_admin_token)
):
    """Get current alert thresholds configuration."""
    from app.notifications import notification_manager
    
    return {
        "thresholds": notification_manager.get_alert_thresholds(),
        "email_rate_limit_per_hour": notification_manager.email_rate_limit_per_hour,
        "status": "success"
    }


@router.post("/notifications/alert-thresholds", response_model=Dict[str, Any])
async def update_alert_thresholds(
    rate_limit_enabled: Optional[bool] = Body(None),
    circuit_breaker_enabled: Optional[bool] = Body(None),
    anomaly_score_threshold: Optional[float] = Body(None),
    error_rate_threshold: Optional[float] = Body(None),
    traffic_spike_threshold: Optional[float] = Body(None),
    min_interval_seconds: Optional[int] = Body(None),
    email_rate_limit_per_hour: Optional[int] = Body(None),
    admin_token: str = Depends(verify_admin_token)
):
    """Update alert thresholds configuration."""
    from app.notifications import notification_manager
    
    # Validate thresholds
    if anomaly_score_threshold is not None and (anomaly_score_threshold < 0 or anomaly_score_threshold > 1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="anomaly_score_threshold must be between 0.0 and 1.0"
        )
    
    if error_rate_threshold is not None and (error_rate_threshold < 0 or error_rate_threshold > 1):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="error_rate_threshold must be between 0.0 and 1.0"
        )
    
    if traffic_spike_threshold is not None and traffic_spike_threshold < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="traffic_spike_threshold must be >= 0"
        )
    
    if min_interval_seconds is not None and min_interval_seconds < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="min_interval_seconds must be >= 0"
        )
    
    if email_rate_limit_per_hour is not None and email_rate_limit_per_hour < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="email_rate_limit_per_hour must be >= 1"
        )
    
    # Update thresholds
    notification_manager.update_alert_thresholds(
        rate_limit_enabled=rate_limit_enabled,
        circuit_breaker_enabled=circuit_breaker_enabled,
        anomaly_score_threshold=anomaly_score_threshold,
        error_rate_threshold=error_rate_threshold,
        traffic_spike_threshold=traffic_spike_threshold,
        min_interval_seconds=min_interval_seconds
    )
    
    if email_rate_limit_per_hour is not None:
        notification_manager.email_rate_limit_per_hour = email_rate_limit_per_hour
    
    return {
        "thresholds": notification_manager.get_alert_thresholds(),
        "email_rate_limit_per_hour": notification_manager.email_rate_limit_per_hour,
        "status": "updated"
    }


# ==================== Slack Notification Configuration ====================

@router.post("/notifications/slack/configure", response_model=Dict[str, Any])
async def configure_slack_notifications(
    webhook_url: str = Body(...),
    default_channel: Optional[str] = Body(None),
    username: str = Body("AgentAnywhere Gateway"),
    rate_limit_per_hour: Optional[int] = Body(None),
    admin_token: str = Depends(verify_admin_token),
):
    """Configure the Slack incoming-webhook notification channel."""
    from app.notifications import notification_manager

    notification_manager.configure_slack(
        webhook_url=webhook_url,
        default_channel=default_channel,
        username=username,
    )
    if rate_limit_per_hour is not None:
        if rate_limit_per_hour < 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="rate_limit_per_hour must be >= 1",
            )
        notification_manager.slack_rate_limit_per_hour = rate_limit_per_hour

    return {
        "status": "configured",
        "default_channel": default_channel,
        "username": username,
        "rate_limit_per_hour": notification_manager.slack_rate_limit_per_hour,
    }


@router.get("/notifications/slack", response_model=Dict[str, Any])
async def get_slack_notification_status(
    admin_token: str = Depends(verify_admin_token),
):
    """Return current Slack notification configuration."""
    from app.notifications import notification_manager

    if not notification_manager.slack_service:
        return {"configured": False}
    service = notification_manager.slack_service
    return {
        "configured": True,
        "default_channel": service.default_channel,
        "username": service.username,
        "rate_limit_per_hour": notification_manager.slack_rate_limit_per_hour,
    }


@router.post("/notifications/slack/test", response_model=Dict[str, Any])
async def test_slack_notifications(
    admin_token: str = Depends(verify_admin_token),
):
    """Send a Slack ping to verify the webhook works."""
    from app.notifications import notification_manager

    if not notification_manager.slack_service:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Slack service not configured",
        )
    is_connected = await notification_manager.slack_service.test_connection()
    return {"service": "slack", "connected": is_connected}


@router.delete("/notifications/slack")
async def remove_slack_notifications(
    admin_token: str = Depends(verify_admin_token),
):
    """Disable Slack notifications by clearing the configured webhook."""
    from app.notifications import notification_manager

    if notification_manager.slack_service is not None:
        try:
            await notification_manager.slack_service.close()
        except Exception:
            pass
    notification_manager.slack_service = None
    return {"status": "removed"}


# ==================== Plugins ====================


@router.get("/plugins", response_model=List[Dict[str, Any]])
async def list_plugins(admin_token: str = Depends(verify_admin_token)):
    """List installed plugins with their metadata, status, and call stats."""
    from app.plugins import plugin_registry
    return plugin_registry.to_dict()


@router.get("/plugins/{name}", response_model=Dict[str, Any])
async def get_plugin(name: str, admin_token: str = Depends(verify_admin_token)):
    """Return details for a single plugin."""
    from app.plugins import plugin_registry
    plugin = plugin_registry.get(name)
    if plugin is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Plugin '{name}' not found")
    return {
        **plugin.metadata.to_dict(),
        "enabled": plugin.enabled,
        "stats": plugin_registry.stats(name),
        "health": plugin.health(),
        "config": plugin.config,
    }


@router.post("/plugins/{name}/enable")
async def enable_plugin(name: str, admin_token: str = Depends(verify_admin_token)):
    """Enable a plugin."""
    from app.plugins import plugin_registry
    if not plugin_registry.set_enabled(name, True):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Plugin '{name}' not found")
    return {"plugin": name, "enabled": True}


@router.post("/plugins/{name}/disable")
async def disable_plugin(name: str, admin_token: str = Depends(verify_admin_token)):
    """Disable a plugin without unloading it."""
    from app.plugins import plugin_registry
    if not plugin_registry.set_enabled(name, False):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Plugin '{name}' not found")
    return {"plugin": name, "enabled": False}


@router.delete("/plugins/{name}")
async def unregister_plugin(name: str, admin_token: str = Depends(verify_admin_token)):
    """Unregister a plugin (calls teardown). Reload the gateway to bring it back."""
    from app.plugins import plugin_registry
    if not plugin_registry.unregister(name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Plugin '{name}' not found")
    return {"plugin": name, "status": "unregistered"}


@router.post("/plugins/reload", response_model=Dict[str, Any])
async def reload_plugins(
    plugin_paths: Optional[List[str]] = Body(None),
    plugins_disabled: Optional[List[str]] = Body(None),
    plugin_configs: Optional[Dict[str, Dict[str, Any]]] = Body(None),
    admin_token: str = Depends(verify_admin_token),
):
    """Discover and reload plugins from the configured sources.

    Optional body fields override the gateway settings for this reload.
    """
    from app.plugins import plugin_registry, load_plugins
    plugin_registry.clear()
    result = load_plugins(
        plugin_paths=plugin_paths if plugin_paths is not None else settings.plugin_paths,
        disabled=plugins_disabled if plugins_disabled is not None else settings.plugins_disabled,
        plugin_configs=plugin_configs,
    )
    return {
        "reloaded": True,
        **result.to_dict(),
        "registered": [p.name for p in plugin_registry.list()],
    }


# ============================================================================
# Observability
# ============================================================================


@router.get("/observability/dependencies", response_model=Dict[str, Any])
async def get_observability_dependencies(
    admin_token: str = Depends(verify_admin_token),
):
    """Run every registered dependency probe and return aggregated status."""
    from app.observability import dependency_probes

    results = await dependency_probes.check_all()
    aggregate = dependency_probes.aggregate(results)
    return {
        "status": aggregate,
        "dependencies": [r.to_dict() for r in results],
    }


@router.get("/observability/slos", response_model=List[Dict[str, Any]])
async def list_slos(admin_token: str = Depends(verify_admin_token)):
    """List all configured SLOs with current availability + burn rate."""
    from app.observability import slo_tracker

    return slo_tracker.list_slos()


@router.get("/observability/slos/{name}", response_model=Dict[str, Any])
async def get_slo(name: str, admin_token: str = Depends(verify_admin_token)):
    """Get a single SLO by name."""
    from app.observability import slo_tracker

    slo = slo_tracker.get(name)
    if not slo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SLO '{name}' not found",
        )
    return slo


@router.post("/observability/slos", response_model=Dict[str, Any])
async def create_slo(
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
):
    """Register a new SLO.

    Body: ``{"name": str, "target": float, "latency_threshold_ms": float?,
    "rolling_window_seconds": int?, "budget_window_seconds": int?}``.
    """
    from app.observability import slo_tracker

    name = payload.get("name")
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'name' is required",
        )
    slo = slo_tracker.register(
        name=name,
        target=float(payload.get("target", 0.999)),
        latency_threshold_ms=payload.get("latency_threshold_ms"),
        rolling_window_seconds=int(payload.get("rolling_window_seconds", 3600)),
        budget_window_seconds=int(payload.get("budget_window_seconds", 30 * 24 * 3600)),
    )
    return slo.to_dict()


@router.delete("/observability/slos/{name}")
async def delete_slo(name: str, admin_token: str = Depends(verify_admin_token)):
    from app.observability import slo_tracker

    if not slo_tracker.remove(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"SLO '{name}' not found",
        )
    return {"deleted": name}


@router.get("/observability/traces", response_model=List[Dict[str, Any]])
async def list_traces(
    limit: int = 50,
    admin_token: str = Depends(verify_admin_token),
):
    """Return recent traces from the in-process ring buffer."""
    from app.observability import tracer

    return tracer.get_recent_traces(limit=max(1, min(limit, 500)))


@router.get("/observability/traces/{trace_id}", response_model=Dict[str, Any])
async def get_trace(
    trace_id: str,
    admin_token: str = Depends(verify_admin_token),
):
    from app.observability import tracer

    trace = tracer.get_trace(trace_id)
    if not trace:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trace '{trace_id}' not found",
        )
    return trace


@router.get("/observability/tracer", response_model=Dict[str, Any])
async def get_tracer_config(admin_token: str = Depends(verify_admin_token)):
    from app.observability import tracer

    return tracer.describe()


@router.put("/observability/tracer", response_model=Dict[str, Any])
async def update_tracer_config(
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
):
    """Configure the tracer at runtime (enabled, OTLP endpoint, headers)."""
    from app.observability import tracer

    tracer.configure(
        enabled=payload.get("enabled"),
        otlp_endpoint=payload.get("otlp_endpoint"),
        otlp_headers=payload.get("otlp_headers"),
        service_name=payload.get("service_name"),
        buffer_size=payload.get("buffer_size"),
    )
    return tracer.describe()


@router.get("/observability/metrics", response_model=Dict[str, List[str]])
async def list_custom_metrics(admin_token: str = Depends(verify_admin_token)):
    """List the names of every metric registered via the custom metrics SDK."""
    from app.observability import custom_metrics

    return custom_metrics.list_metrics()


@router.get("/observability/dashboard", response_model=Dict[str, Any])
async def observability_dashboard(admin_token: str = Depends(verify_admin_token)):
    """Combined live snapshot for an observability dashboard.

    Includes dependency status, all SLOs, tracer state, and counts of
    custom metrics.
    """
    from app.observability import (
        custom_metrics,
        dependency_probes,
        slo_tracker,
        tracer,
    )

    deps = await dependency_probes.check_all()
    return {
        "dependencies": {
            "status": dependency_probes.aggregate(deps),
            "results": [d.to_dict() for d in deps],
        },
        "slos": slo_tracker.list_slos(),
        "tracer": tracer.describe(),
        "custom_metrics": custom_metrics.list_metrics(),
    }


# ============================================================================
# Governance
# ============================================================================


@router.get("/governance/audit", response_model=List[Dict[str, Any]])
async def list_audit_log(
    limit: int = 100,
    offset: int = 0,
    action: Optional[str] = None,
    actor: Optional[str] = None,
    resource_type: Optional[str] = None,
    outcome: Optional[str] = None,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """List recent governance audit entries (with optional filters)."""
    from app.governance import audit_log

    return await audit_log.list(
        db,
        limit=limit,
        offset=offset,
        action=action,
        actor=actor,
        resource_type=resource_type,
        outcome=outcome,
    )


@router.get("/governance/audit/verify", response_model=Dict[str, Any])
async def verify_audit_chain(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Walk the hash chain forward and confirm every entry is intact."""
    from app.governance import audit_log

    return await audit_log.verify_chain(db)


@router.get("/governance/roles", response_model=List[Dict[str, Any]])
async def get_admin_roles(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """List configured admin role assignments.

    Roles are stored but not enforced yet: every valid admin credential can
    call every admin endpoint.
    """
    from app.governance import list_roles

    return await list_roles(db)


@router.post("/governance/roles", response_model=Dict[str, Any])
async def assign_admin_role(
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Map an admin token to a role (stored, not enforced yet; see above).

    Body: ``{"token": str, "name": str, "role": "super_admin"|"operator"|"viewer"|"auditor"}``.
    """
    from app.governance import Role, assign_role, audit_log, AuditEntry

    token = payload.get("token")
    name = payload.get("name")
    role_str = payload.get("role")
    if not token or not name or not role_str:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'token', 'name' and 'role' are required",
        )
    try:
        role = Role(role_str)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role '{role_str}'. Allowed: {[r.value for r in Role]}",
        )
    row = await assign_role(db, token=token, name=name, role=role)
    await audit_log.record(
        db,
        AuditEntry(
            action="rbac.role.assign",
            actor=admin_token[-8:],
            resource_type="admin_role",
            resource_id=row.id,
            after={"name": name, "role": role.value},
        ),
    )
    return {
        "id": row.id,
        "name": row.name,
        "role": row.role,
        "token_fingerprint": row.token_hash[:12],
        "is_active": row.is_active,
    }


@router.delete("/governance/roles/{role_id}")
async def revoke_admin_role(
    role_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    from app.governance import revoke_role, audit_log, AuditEntry

    revoked = await revoke_role(db, role_id)
    if not revoked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Role '{role_id}' not found",
        )
    await audit_log.record(
        db,
        AuditEntry(
            action="rbac.role.revoke",
            actor=admin_token[-8:],
            resource_type="admin_role",
            resource_id=role_id,
        ),
    )
    return {"revoked": role_id}


@router.get("/governance/policies", response_model=List[Dict[str, Any]])
async def list_governance_policies(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    from app.governance import list_policies

    return await list_policies(db)


@router.post("/governance/policies", response_model=Dict[str, Any])
async def create_governance_policy(
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Create a declarative policy.

    Body example::

        {
          "name": "deny-admin-from-public",
          "priority": 10,
          "conditions": {"path": "/admin/*", "cidrs": ["0.0.0.0/0"]},
          "action": {"type": "deny", "reason": "admin not reachable from public"}
        }
    """
    from app.governance import audit_log, AuditEntry, create_policy

    if not payload.get("name") or not payload.get("conditions") or not payload.get("action"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'name', 'conditions' and 'action' are required",
        )
    policy = await create_policy(
        db,
        name=payload["name"],
        description=payload.get("description"),
        priority=_policy_priority(payload.get("priority")),
        enabled=bool(payload.get("enabled", True)),
        conditions=payload["conditions"],
        action=payload["action"],
    )
    await audit_log.record(
        db,
        AuditEntry(
            action="policy.create",
            actor=admin_token[-8:],
            resource_type="policy",
            resource_id=policy.id,
            after=policy.to_dict(),
        ),
    )
    return policy.to_dict()


@router.put("/governance/policies/{policy_id}", response_model=Dict[str, Any])
async def update_governance_policy(
    policy_id: str,
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    from app.governance import audit_log, AuditEntry, update_policy

    allowed = {"name", "description", "priority", "enabled", "conditions", "action"}
    changes = {k: v for k, v in payload.items() if k in allowed}
    if not changes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"No updatable fields. Allowed: {sorted(allowed)}",
        )
    if "priority" in changes:
        changes["priority"] = _policy_priority(changes["priority"])
    updated = await update_policy(db, policy_id, **changes)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy '{policy_id}' not found",
        )
    await audit_log.record(
        db,
        AuditEntry(
            action="policy.update",
            actor=admin_token[-8:],
            resource_type="policy",
            resource_id=policy_id,
            after=updated.to_dict(),
        ),
    )
    return updated.to_dict()


@router.delete("/governance/policies/{policy_id}")
async def delete_governance_policy(
    policy_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    from app.governance import audit_log, AuditEntry, delete_policy

    deleted = await delete_policy(db, policy_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Policy '{policy_id}' not found",
        )
    await audit_log.record(
        db,
        AuditEntry(
            action="policy.delete",
            actor=admin_token[-8:],
            resource_type="policy",
            resource_id=policy_id,
        ),
    )
    return {"deleted": policy_id}


@router.get("/governance/ip-rules", response_model=List[Dict[str, Any]])
async def list_governance_ip_rules(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    from app.governance import list_ip_rules

    return await list_ip_rules(db)


@router.post("/governance/ip-rules", response_model=Dict[str, Any])
async def create_governance_ip_rule(
    payload: Dict[str, Any] = Body(...),
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Create an IP allow / deny rule.

    Body: ``{"cidr": str, "action": "allow"|"deny", "scope": "global"|"tenant"|"api_key", "scope_id": str?, "description": str?}``.
    """
    from app.governance import audit_log, AuditEntry, create_ip_rule

    try:
        rule = await create_ip_rule(
            db,
            cidr=payload["cidr"],
            action=payload["action"],
            scope=payload.get("scope", "global"),
            scope_id=payload.get("scope_id"),
            description=payload.get("description"),
            enabled=bool(payload.get("enabled", True)),
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    await audit_log.record(
        db,
        AuditEntry(
            action="ip_rule.create",
            actor=admin_token[-8:],
            resource_type="ip_rule",
            resource_id=rule.id,
            after=rule.to_dict(),
        ),
    )
    return rule.to_dict()


@router.delete("/governance/ip-rules/{rule_id}")
async def delete_governance_ip_rule(
    rule_id: str,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    from app.governance import audit_log, AuditEntry, delete_ip_rule

    deleted = await delete_ip_rule(db, rule_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"IP rule '{rule_id}' not found",
        )
    await audit_log.record(
        db,
        AuditEntry(
            action="ip_rule.delete",
            actor=admin_token[-8:],
            resource_type="ip_rule",
            resource_id=rule_id,
        ),
    )
    return {"deleted": rule_id}


@router.get("/governance/api-keys/expiring", response_model=List[Dict[str, Any]])
async def list_expiring_api_keys(
    within_days: int = 7,
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """List API keys whose expiry falls within ``within_days`` days."""
    from app.governance import expiring_api_keys

    return await expiring_api_keys(db, within_days=max(1, min(within_days, 365)))


@router.post("/governance/api-keys/sweep-expired", response_model=Dict[str, Any])
async def sweep_expired_api_keys(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Deactivate any API key whose expiry has passed."""
    from app.governance import audit_log, AuditEntry, auto_deactivate_expired_keys

    count = await auto_deactivate_expired_keys(db)
    await audit_log.record(
        db,
        AuditEntry(
            action="api_keys.sweep_expired",
            actor=admin_token[-8:],
            outcome="success",
            metadata={"deactivated_count": count},
        ),
    )
    return {"deactivated_count": count}


@router.post("/governance/reload", response_model=Dict[str, Any])
async def reload_governance_caches(
    admin_token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
):
    """Reload the policy + IP-rule caches from the database."""
    from app.governance import policy_engine, ip_rule_engine

    await policy_engine.reload(db)
    await ip_rule_engine.reload(db)
    return {
        "policies": len(policy_engine.list_policies()),
        "ip_rules": len(ip_rule_engine.list_rules()),
    }
