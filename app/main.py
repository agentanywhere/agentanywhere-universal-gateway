"""Main FastAPI application."""
import os
import time
import uuid
import json
import asyncio
from types import SimpleNamespace
import httpx
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional
from app.time_utils import utcnow
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, HTMLResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from app.config import cors_middleware_options, get_settings
from app.banner import announce_startup, banner_facts
from app.logging_config import setup_logging

# First thing the process does, ahead of the heavier imports below: show the
# banner (so a slow start is not silent) and configure logging (so the lines the
# later imports log already use the configured format and redaction).
announce_startup(get_settings())
setup_logging()

from app.docs_links import docs_link
from app.client_ip import client_ip as _client_ip
from app.models import HealthStatus, ErrorResponse, GatewayRequest, ProtocolType, RouteConfig
from app.exceptions import (
    GatewayException, AuthenticationError, ValidationError,
    RateLimitError, BackendError, ServiceUnavailableError,
    NotFoundError, InternalServerError
)
from app.middleware import AuthMiddleware, RateLimiter
from app.routing import Router, repeated_query
from app.logging_config import (
    setup_logging,
    bind_request_context,
    clear_request_context,
    parse_traceparent,
    new_trace_context,
    format_traceparent,
)
from app import metrics
from app.observability import (
    record_request as obs_record_request,
    tracer as obs_tracer,
    normalize_route as obs_normalize_route,
    metric_path_label as obs_metric_path_label,
)
from app.database import init_db, close_db, get_db
from app.protocols import MQTTHandler, WebSocketHandler, GRPCHandler
from app.protocols.graphql_handler import GraphQLHandler
from app.log_exporters import log_export_manager
from app.oauth2 import oauth2_manager
from app.versioning import VersionUnavailable
from app.tenant import tenant_manager
from app.notifications import notification_manager
from app.plugins import plugin_registry, load_plugins, PluginContext
from admin.api import router as admin_router, verify_admin_token_value
from fastapi import WebSocket, WebSocketDisconnect
import structlog

settings = get_settings()
logger = setup_logging()
from app.adapters import build_adapters, json_response_class  # noqa: E402
adapters = build_adapters(settings)
request_log_writer = adapters.request_log
auth_middleware = AuthMiddleware()
rate_limiter = RateLimiter()
router = Router(client=adapters.upstream)

# Agent edge: slug -> upstream map for `/agents/<slug>/...` routing.
from app.agent_routes import AgentEdgeConfig
agent_edge_config = AgentEdgeConfig.from_settings(settings)
ai_service = adapters.analytics

# Initialize protocol handlers
mqtt_handler = None
if settings.mqtt_broker_url:
    mqtt_handler = MQTTHandler(settings.mqtt_broker_url)
    mqtt_handler.connect()

websocket_handler = None
if settings.websocket_backend_url:
    websocket_handler = WebSocketHandler(settings.websocket_backend_url)

grpc_handler = None
if settings.grpc_backend_url:
    grpc_handler = GRPCHandler(settings.grpc_backend_url)

graphql_handler = None
if settings.graphql_backend_url:
    graphql_handler = GraphQLHandler(settings.graphql_backend_url)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("gateway_starting", version=settings.app_version)
    # Initialize database
    await init_db()
    logger.info("database_initialized")
    await request_log_writer.start()
    logger.info("request_log_writer_started", mode=request_log_writer.mode)

    try:
        from app.database import AsyncSessionLocal
        from app.tier1_registry import tier1_registry

        async with AsyncSessionLocal() as session:
            await tier1_registry.reload(session)
        logger.info(
            "tier1_openapi_registrations_loaded",
            count=len(tier1_registry.list_registrations()),
        )
    except Exception as tier1_exc:  # noqa: BLE001
        logger.warning("tier1_registry_load_failed", error=str(tier1_exc))

    # Hydrate governance caches (policies + IP rules) so the auth
    # middleware sees them on the first request.
    try:
        from app.database import AsyncSessionLocal
        from app.governance import policy_engine, ip_rule_engine

        async with AsyncSessionLocal() as session:
            await policy_engine.reload(session)
            await ip_rule_engine.reload(session)
        logger.info(
            "governance_loaded",
            policies=len(policy_engine.list_policies()),
            ip_rules=len(ip_rule_engine.list_rules()),
        )
    except Exception as gov_exc:  # noqa: BLE001
        logger.warning("governance_load_failed", error=str(gov_exc))

    # Configure observability tracer from env (idempotent).
    try:
        from app.observability import configure_from_env as _obs_env
        _obs_env()
    except Exception as obs_exc:  # noqa: BLE001
        logger.debug("observability_configure_failed", error=str(obs_exc))
    
    # Configure email notifications from settings
    if settings.email_notifications_enabled and settings.smtp_host:
        notification_manager.configure_email(
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            smtp_user=settings.smtp_user,
            smtp_password=settings.smtp_password,
            smtp_use_tls=settings.smtp_use_tls,
            from_email=settings.smtp_from_email
        )
        if settings.email_alert_recipients:
            notification_manager.set_alert_recipients_from_string(settings.email_alert_recipients)
        logger.info("email_notifications_configured")

    # Configure Slack notifications from settings
    if settings.slack_notifications_enabled and settings.slack_webhook_url:
        notification_manager.configure_slack(
            webhook_url=settings.slack_webhook_url,
            default_channel=settings.slack_channel,
            username=settings.slack_username,
        )
        logger.info("slack_notifications_configured")

    # Bootstrap log exporters from settings + start batching worker
    try:
        from app.log_exporters import configure_from_settings
        configure_from_settings(log_export_manager)
        if settings.log_export_enabled and log_export_manager.exporters:
            await log_export_manager.start()
            logger.info(
                "log_exporters_bootstrapped",
                exporters=[e.name for e in log_export_manager.exporters],
            )
    except Exception as e:
        logger.warning("log_exporter_bootstrap_failed", error=str(e))

    # Hydrate the in-memory tenant registry from the database (best-effort)
    try:
        from app.tenant import tenant_manager as _mgr, tenant_service as _svc
        async for db_session in get_db():
            tenants = await _svc.list_tenants(db_session)
            for t in tenants:
                _mgr.create_tenant(
                    tenant_id=t["tenant_id"],
                    name=t["name"],
                    config=t.get("config"),
                    quota=t.get("quota"),
                    description=t.get("description"),
                )
            logger.info("tenants_hydrated", count=len(tenants))
            break
    except Exception as e:
        logger.warning("tenant_hydration_failed", error=str(e))

    # Bootstrap plugin system
    if settings.plugins_enabled:
        try:
            plugin_configs = {}
            if settings.plugin_configs_json:
                try:
                    plugin_configs = json.loads(settings.plugin_configs_json)
                    if not isinstance(plugin_configs, dict):
                        raise ValueError("plugin_configs_json must be a JSON object")
                except Exception as exc:
                    logger.warning("plugin_configs_parse_failed", error=str(exc))
                    plugin_configs = {}
            result = load_plugins(
                plugin_paths=settings.plugin_paths,
                disabled=settings.plugins_disabled,
                plugin_configs=plugin_configs,
            )
            logger.info(
                "plugins_loaded",
                loaded=result.loaded,
                skipped=result.skipped,
                errors=len(result.errors),
            )
            plugin_registry.run_hook("startup")
        except Exception as exc:
            logger.warning("plugin_bootstrap_failed", error=str(exc))

    # Agent-edge registry: seed from env on first boot, load into the live
    # config, and start a periodic refresh so replicas converge on admin edits.
    try:
        from app import agent_registry
        await agent_registry.bootstrap(agent_edge_config, settings)
        app.state.agent_registry_task = asyncio.create_task(
            agent_registry.refresh_loop(agent_edge_config)
        )
        logger.info("agent_registry_ready", agents=len(agent_edge_config.snapshot()["routes"]))
    except Exception as reg_exc:  # noqa: BLE001
        logger.warning("agent_registry_init_failed", error=str(reg_exc))

    # Dynamic route registry: load routes registered via POST /admin/routes so
    # they are actually honoured by the proxy, and refresh periodically so
    # replicas converge on admin edits. Before this, registered routes were
    # persisted but never read at request time.
    if settings.dynamic_routes_enabled:
        try:
            from app import route_registry
            await route_registry.bootstrap()
            app.state.route_registry_task = asyncio.create_task(
                route_registry.refresh_loop(settings.dynamic_routes_refresh_seconds)
            )
            logger.info("route_registry_ready", routes=route_registry.count())
        except Exception as rr_exc:  # noqa: BLE001
            logger.warning("route_registry_init_failed", error=str(rr_exc))

    # Live agent health/traffic monitor for the console Mission Control board.
    try:
        from app.agent_monitor import agent_monitor
        app.state.agent_monitor_task = asyncio.create_task(
            agent_monitor.run(agent_edge_config)
        )
        logger.info("agent_monitor_started")
    except Exception as mon_exc:  # noqa: BLE001
        logger.warning("agent_monitor_init_failed", error=str(mon_exc))

    # Initialize protocol handlers
    global mqtt_handler, grpc_handler, graphql_handler
    if mqtt_handler:
        # MQTT handler connects on demand, no need to start here
        logger.info("mqtt_handler_available")
    if grpc_handler:
        grpc_handler.get_channel()
        logger.info("grpc_handler_initialized")
    if graphql_handler:
        logger.info("graphql_handler_initialized")

    logger.info("gateway_ready", **banner_facts(settings))

    yield
    
    logger.info("gateway_shutting_down")
    for _task_attr in ("agent_registry_task", "agent_monitor_task"):
        _task = getattr(app.state, _task_attr, None)
        if _task is not None:
            _task.cancel()
            try:
                await _task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    if settings.plugins_enabled:
        try:
            plugin_registry.run_hook("shutdown")
            plugin_registry.clear()
        except Exception as exc:
            logger.warning("plugin_shutdown_failed", error=str(exc))
    await router.close()
    try:
        await log_export_manager.stop()
    except Exception as e:
        logger.warning("log_export_stop_failed", error=str(e))
    try:
        if notification_manager.slack_service is not None:
            await notification_manager.slack_service.close()
    except Exception:
        pass
    if mqtt_handler:
        # MQTT handler cleanup - disconnect if connected
        try:
            mqtt_handler.disconnect()
        except Exception:
            pass
    if grpc_handler:
        grpc_handler.close()
    if graphql_handler:
        await graphql_handler.close()
    await request_log_writer.stop()
    logger.info(
        "request_log_writer_stopped",
        written=request_log_writer.written, dropped=request_log_writer.dropped, failed=request_log_writer.failed,
    )
    await close_db()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    default_response_class=json_response_class(adapters.json_codec),
    # The interactive docs and the schema describe every route, admin ones
    # included, so they are served only with DEBUG=true. Without it the
    # gateway answers 404 for these paths itself (see DOCS_PATHS below)
    # instead of proxying them to a backend.
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)

DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"})

def safe_key_label(api_key) -> Optional[str]:
    """A label for an API key that is safe to log: its id, or, for a key that has no id
    (the break-glass SECRET_KEY), a short one-way hash. Never the key itself."""
    if api_key is None:
        return None
    key_id = getattr(api_key, "id", None)
    if key_id:
        return str(key_id)
    secret = getattr(api_key, "key", None)
    if secret:
        import hashlib
        return "key-" + hashlib.sha256(str(secret).encode("utf-8")).hexdigest()[:12]
    return None


def _iter_request_db():
    """Yield a DB session for use inside the HTTP/WebSocket middleware.

    The auth middleware runs outside FastAPI's dependency-injection, so it calls
    this instead of ``get_db()`` directly. When a ``get_db`` dependency override
    is registered (as the test suite does), honor it so the middleware shares the
    same session the request handlers use; otherwise fall back to the real
    ``get_db``. In production no override is set, so this is a no-op wrapper.
    """
    dep = app.dependency_overrides.get(get_db, get_db)
    return dep()


# Include admin router
app.include_router(admin_router)

# Agent-edge control plane (DB-backed registry CRUD + hot reload).
from admin.agents_api import router as agents_admin_router
app.include_router(agents_admin_router)

# CORS middleware. A "*" origin turns credentials off (with a warning):
# browsers reject the wildcard-plus-credentials combination, and Starlette
# would otherwise echo any Origin back with credentials allowed.
_cors_options, _cors_warning = cors_middleware_options(settings)
if _cors_warning:
    logger.warning("cors_wildcard_credentials_disabled", message=_cors_warning)
app.add_middleware(CORSMiddleware, **_cors_options)

# --------------------------------------------------------------------------
# Error rendering: content-negotiated so humans in a browser get a readable
# page instead of a raw JSON blob, while API clients keep the exact JSON body.
# --------------------------------------------------------------------------
import html as _html


def _client_prefers_html(request: Request) -> bool:
    """True when the caller looks like a browser navigation (wants HTML).

    We only switch to the HTML page when ``text/html`` is *preferred* over JSON
    in the Accept header, and the request is not an obvious API/XHR/fetch call.
    This keeps programmatic clients (curl default ``*/*``, SDKs, ``fetch`` with
    ``Accept: application/json``) on the machine-readable JSON contract.
    """
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" not in accept:
        return False
    # An explicit application/json earlier in the list means "give me JSON".
    if "application/json" in accept and accept.index("application/json") < accept.index("text/html"):
        return False
    # XHR / fetch hints.
    if request.headers.get("x-requested-with", "").lower() == "xmlhttprequest":
        return False
    return True


_STATUS_TITLES = {
    400: "Bad request",
    401: "Sign-in required",
    403: "Access denied",
    404: "Not found",
    429: "Too many requests",
    500: "Something went wrong",
    502: "Upstream error",
    503: "Service unavailable",
}

# Per-status accent hue (H,S,L base) so the page colour matches the error class:
# blue = auth, slate = missing, amber = throttled, red = server/upstream.
_STATUS_ACCENTS = {
    400: "38 92% 50%",   # amber
    401: "217 91% 60%",  # blue
    403: "217 91% 60%",  # blue
    404: "215 20% 55%",  # slate
    429: "38 92% 50%",   # amber
    500: "0 84% 60%",    # red
    502: "0 84% 60%",    # red
    503: "0 84% 60%",    # red
}


def _error_title(status_code: int, code: str) -> str:
    if code == "AGENT_NOT_FOUND":
        return "Agent not found"
    return _STATUS_TITLES.get(status_code, "Error")


def _error_html(status_code: int, data: Dict[str, Any]) -> str:
    """Render a clean, self-contained, theme-adaptive branded error page."""
    code = data.get("error") or "ERROR"
    title = _error_title(status_code, code)
    message = str(data.get("message") or "The request could not be completed.")
    details = data.get("details") or {}
    if not isinstance(details, dict):
        details = {}
    hint = details.get("hint")
    retry_after = details.get("retry_after")
    request_id = data.get("request_id")
    accent = _STATUS_ACCENTS.get(status_code, "217 91% 60%")

    esc = _html.escape
    hint_block = f'<p class="hint">{esc(str(hint))}</p>' if hint else ""
    # /docs only exists when DEBUG=true; otherwise the button would lead to a 404.
    docs_button = '<a class="btn ghost" href="/docs">API docs</a>' if app.docs_url else ""
    rid_block = (
        f'<p class="rid">Request ID <code id="rid">{esc(str(request_id))}</code>'
        f'<button id="copy" type="button" aria-label="Copy request ID">Copy</button></p>'
        if request_id
        else ""
    )
    # For throttling, offer a live countdown + retry instead of a dead end.
    countdown_block = ""
    countdown_js = ""
    try:
        ra = int(retry_after) if retry_after is not None else 0
    except (TypeError, ValueError):
        ra = 0
    if status_code == 429 and ra > 0:
        countdown_block = f'<p class="hint">You can retry in <b id="cd">{ra}</b>s.</p>'
        countdown_js = (
            "var n=%d,cd=document.getElementById('cd');"
            "var t=setInterval(function(){n-=1;if(cd)cd.textContent=n;"
            "if(n<=0){clearInterval(t);location.reload();}},1000);" % ra
        )
    copy_js = (
        "var b=document.getElementById('copy');if(b){b.addEventListener('click',function(){"
        "var r=document.getElementById('rid');navigator.clipboard&&navigator.clipboard.writeText(r.textContent)"
        ".then(function(){b.textContent='Copied';setTimeout(function(){b.textContent='Copy';},1500);});});}"
    )
    script = (
        f"<script>{copy_js}{countdown_js}</script>" if (request_id or countdown_js) else ""
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="robots" content="noindex" />
<title>{status_code} · {esc(title)} · AgentAnywhere</title>
<style>
  :root {{
    color-scheme: light dark;
    --accent: {accent};
    --bg1:#f8fafc; --bg2:#e2e8f0; --card:#ffffff; --border:15 23 42;
    --fg:#0f172a; --muted:#475569; --soft:#64748b;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg1:#1e293b; --bg2:#0f172a; --card:rgba(15,23,42,.6); --border:148 163 184;
             --fg:#f8fafc; --muted:#cbd5e1; --soft:#94a3b8; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    background:radial-gradient(1200px 600px at 50% -10%, var(--bg1), var(--bg2)); color:var(--fg); padding:24px;
  }}
  .card {{
    width:100%; max-width:460px; background:var(--card); border:1px solid rgb(var(--border)/.18);
    border-radius:20px; padding:40px 36px; box-shadow:0 24px 60px rgba(0,0,0,.28); backdrop-filter:blur(12px);
  }}
  .badge {{
    display:inline-flex; align-items:center; gap:8px; font-size:12px; font-weight:600; letter-spacing:.08em;
    text-transform:uppercase; color:hsl(var(--accent)); background:hsl(var(--accent)/.12);
    border:1px solid hsl(var(--accent)/.28); padding:6px 12px; border-radius:999px;
  }}
  h1 {{ font-size:56px; line-height:1; margin:22px 0 6px; font-weight:700; letter-spacing:-.02em; color:hsl(var(--accent)); }}
  h2 {{ font-size:20px; margin:0 0 12px; font-weight:600; color:var(--fg); }}
  p {{ margin:0 0 10px; font-size:15px; line-height:1.55; color:var(--muted); }}
  .hint {{ font-size:14px; color:var(--soft); background:rgb(var(--border)/.08); border-radius:10px; padding:12px 14px; }}
  .rid {{ margin-top:18px; font-size:12px; color:var(--soft); display:flex; align-items:center; gap:8px; }}
  code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--fg); }}
  #copy {{ font:inherit; font-size:11px; cursor:pointer; border:1px solid rgb(var(--border)/.3);
           background:transparent; color:var(--soft); border-radius:6px; padding:2px 8px; }}
  #copy:hover {{ color:var(--fg); }}
  .actions {{ margin-top:24px; display:flex; gap:10px; flex-wrap:wrap; }}
  a.btn {{ text-decoration:none; font-size:14px; font-weight:600; padding:10px 16px; border-radius:10px;
           background:hsl(var(--accent)); color:#fff; transition:filter .15s; }}
  a.btn:hover {{ filter:brightness(1.08); }}
  a.btn.ghost {{ background:transparent; color:hsl(var(--accent)); border:1px solid rgb(var(--border)/.28); }}
</style>
</head>
<body>
  <main class="card" role="alert">
    <span class="badge">{esc(str(code))}</span>
    <h1>{status_code}</h1>
    <h2>{esc(title)}</h2>
    <p>{esc(message)}</p>
    {hint_block}
    {countdown_block}
    <div class="actions">
      <a class="btn" href="/">Go home</a>
      {docs_button}
    </div>
    {rid_block}
  </main>
  {script}
</body>
</html>"""


def render_error_response(
    request: Request,
    status_code: int,
    response_data: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
) -> Response:
    """Return an error either as a branded HTML page (browsers) or JSON (APIs)."""
    out_headers = dict(headers or {})
    out_headers.setdefault("X-Content-Type-Options", "nosniff")
    if _client_prefers_html(request):
        out_headers["Content-Type"] = "text/html; charset=utf-8"
        return HTMLResponse(
            status_code=status_code,
            content=_error_html(status_code, response_data),
            headers=out_headers,
        )
    return JSONResponse(status_code=status_code, content=response_data, headers=out_headers)


# --------------------------------------------------------------------------
# Error logging: one structured line per handled error, with a severity that
# matches the status class. Client errors (4xx) are expected and logged at
# info/warning; server errors (5xx) are logged at error *with a stack trace*
# and counted in the error metric. This keeps logs signal-rich instead of
# spamming a breadcrumb per handler branch.
# --------------------------------------------------------------------------
def _log_handled_error(
    request: Optional[Request],
    status_code: int,
    error_code: Optional[str],
    message: Optional[str],
    request_id: Optional[str],
    exc: Optional[BaseException] = None,
    internal_error: Optional[str] = None,
    protocol: str = "gateway",
) -> None:
    fields: Dict[str, Any] = {
        "http.status_code": status_code,
        "error.code": error_code,
        "error.message": message,
        "http.request.path": request.url.path if request else None,
        "http.request.method": request.method if request else None,
        "request_id": request_id,
    }
    if internal_error:
        fields["internal_error"] = internal_error

    if status_code >= 500:
        # exc_info attaches the traceback so 5xx are debuggable; structlog
        # accepts an exception instance directly.
        logger.error("gateway_error_response", exc_info=exc or True, **fields)
        if settings.metrics_enabled:
            try:
                metrics.error_count.labels(
                    error_type=error_code or (type(exc).__name__ if exc else "unknown"),
                    protocol=protocol,
                ).inc()
            except Exception:  # noqa: BLE001 - metrics must never break error handling
                pass
    elif status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        logger.warning("gateway_error_response", **fields)
    else:
        logger.info("gateway_error_response", **fields)


# Global exception handlers (must be registered before routes)
@app.exception_handler(GatewayException)
async def gateway_exception_handler(request: Request, exc: GatewayException):
    """Handle custom gateway exceptions with a consistent JSON/HTML envelope."""
    request_id = exc.request_id or getattr(request.state, "request_id", None)

    # exc.detail is already the structured dict (set in GatewayException.__init__).
    if isinstance(exc.detail, dict):
        response_data = exc.detail.copy()
    else:
        response_data = {
            "error": exc.error_code,
            "message": exc.message,
            "request_id": request_id,
        }

    if not response_data.get("request_id"):
        response_data["request_id"] = request_id
    response_data.setdefault("timestamp", utcnow().isoformat())
    if exc.details and "details" not in response_data:
        response_data["details"] = exc.details

    headers = {}
    if exc.retry_after:
        headers["Retry-After"] = str(exc.retry_after)

    _log_handled_error(
        request,
        exc.status_code,
        response_data.get("error"),
        response_data.get("message"),
        response_data.get("request_id"),
        exc=exc,
        internal_error=getattr(exc, "log_error", None),
    )
    return render_error_response(request, exc.status_code, response_data, headers)


# Register HTTPException handler using Starlette's HTTPException
# This ensures it matches all HTTPException types (FastAPI uses Starlette's internally)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Handle FastAPI/Starlette HTTP exceptions with the gateway error envelope."""
    request_id = getattr(request.state, "request_id", None)

    # GatewayException subclasses HTTPException, so FastAPI may route it here.
    # Detect it (by type or by its attributes) and keep the structured envelope.
    is_gateway = isinstance(exc, GatewayException) or (
        hasattr(exc, "error_code") and hasattr(exc, "message") and hasattr(exc, "details")
    )

    if is_gateway:
        response_data = {
            "error": exc.error_code,
            "message": exc.message,
            "request_id": exc.request_id or request_id,
            "timestamp": utcnow().isoformat(),
        }
        if exc.details:
            response_data["details"] = exc.details

        headers = {}
        if getattr(exc, "retry_after", None):
            headers["Retry-After"] = str(exc.retry_after)

        _log_handled_error(
            request,
            exc.status_code,
            response_data["error"],
            response_data["message"],
            response_data["request_id"],
            exc=exc,
            internal_error=getattr(exc, "log_error", None),
        )
        return render_error_response(request, exc.status_code, response_data, headers)

    # Plain HTTPException: normalise both dict and string details into the envelope.
    if isinstance(exc.detail, dict):
        response_data = exc.detail.copy()
        response_data.setdefault("error", "HTTP_ERROR")
    else:
        response_data = {"error": "HTTP_ERROR", "message": str(exc.detail)}
    response_data.setdefault("request_id", request_id)
    response_data.setdefault("timestamp", utcnow().isoformat())

    # Preserve response headers set on the exception (e.g. WWW-Authenticate, Allow).
    headers = dict(getattr(exc, "headers", None) or {})

    _log_handled_error(
        request,
        exc.status_code,
        response_data.get("error"),
        response_data.get("message"),
        response_data.get("request_id"),
    )
    return render_error_response(request, exc.status_code, response_data, headers)


async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Map FastAPI request-validation (422) errors into the gateway envelope."""
    request_id = getattr(request.state, "request_id", None)
    errors = [
        {
            "location": list(err.get("loc", [])),
            "message": err.get("msg"),
            "type": err.get("type"),
        }
        for err in exc.errors()
    ]
    response_data = {
        "error": "VALIDATION_ERROR",
        "message": "Request validation failed",
        "request_id": request_id,
        "timestamp": utcnow().isoformat(),
        "details": {"errors": errors},
    }
    _log_handled_error(
        request,
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "VALIDATION_ERROR",
        "Request validation failed",
        request_id,
    )
    return render_error_response(
        request, status.HTTP_422_UNPROCESSABLE_ENTITY, response_data
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Handle unexpected exceptions."""
    request_id = getattr(request.state, "request_id", None)
    
    # Log the full error for debugging
    logger.exception(
        "unhandled_exception",
        request_id=request_id,
        path=request.url.path if request else None,
        error_type=type(exc).__name__,
        error=str(exc)
    )
    
    # Don't expose internal error details to users
    response_data = {
        "error": "INTERNAL_ERROR",
        "message": "An unexpected error occurred. Please try again later.",
        "request_id": request_id,
        "timestamp": utcnow().isoformat(),
    }
    
    if settings.metrics_enabled:
        metrics.error_count.labels(
            error_type=type(exc).__name__,
            protocol="unknown"
        ).inc()
    
    return render_error_response(request, status.HTTP_500_INTERNAL_SERVER_ERROR, response_data)

# Explicitly register exception handlers to override FastAPI defaults
# Must use Starlette's HTTPException for proper matching
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(GatewayException, gateway_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)

async def logging_middleware(request: Request, call_next):
    """Structured access logging, metrics, and W3C trace propagation.

    For every request we:

    1. Honour an incoming ``traceparent`` header (W3C Trace Context) or
       generate a fresh trace + span id.
    2. Bind ``request_id``, ``trace_id``, ``span_id`` and ``client_ip``
       to the per-request context vars so every log line emitted within
       this request carries them automatically.
    3. Emit a single structured access-log line with stable keys (status,
       duration_ms, method, path, protocol, ...).
    4. Echo ``X-Request-ID`` and ``traceparent`` back to the client.
    """
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    parsed = parse_traceparent(request.headers.get("traceparent"))
    if parsed:
        trace_id, parent_span_id, _flags = parsed
        # New span id for this hop, but we keep the same trace.
        _, span_id, flags = new_trace_context()
    else:
        trace_id, span_id, flags = new_trace_context()
        parent_span_id = None

    request.state.request_id = request_id
    request.state.trace_id = trace_id
    request.state.span_id = span_id
    request.state.parent_span_id = parent_span_id

    client_ip = _client_ip(request)

    bind_request_context(
        request_id=request_id,
        trace_id=trace_id,
        span_id=span_id,
        client_ip=client_ip,
    )

    # Resolve API version up-front so we can echo the relevant headers
    # back on the response. This is best-effort — unknown versions or
    # non-versioned paths just produce no headers (and a `410 Gone`
    # response is raised by the proxy layer if the version is removed).
    try:
        from app.versioning import version_manager, VersionUnavailable

        api_version, _ = version_manager.resolve(
            request.url.path, dict(request.headers)
        )
        if api_version:
            request.state.api_version = api_version
    except Exception:
        api_version = None

    start_time = time.time()

    # Open a parent span for this request. The tracer is always available
    # (in-process ring buffer); ``enabled=True`` adds OTLP export.
    route_template = obs_normalize_route(request.url.path)
    span_ctx = obs_tracer.aspan(
        f"{request.method} {route_template}",
        trace_id=trace_id,
        attributes={
            "http.method": request.method,
            "http.target": request.url.path,
            "http.route": route_template,
            "http.query": str(request.url.query),
            "client.ip": client_ip or "",
            "user_agent": request.headers.get("user-agent", ""),
        },
    )

    try:
        async with span_ctx as request_span:
            request.state.observability_span = request_span
            response = await call_next(request)
            request_span.set_attribute("http.status_code", response.status_code)
        process_time = time.time() - start_time
        # Set by the auth middleware when it answered the request itself.
        auth_rejected = bool(getattr(request.state, "auth_rejected", False))
        metric_path = _rejected_path_label(request.url.path) if auth_rejected else obs_metric_path_label(request.url.path)

        # Process through AI service
        try:
            if auth_rejected or not settings.ai_analytics_enabled:
                raise _SkipAI()
            request_data = {
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "response_time_ms": process_time * 1000,
                "client_ip": request.client.host if request.client else None,
                "api_key": getattr(request.state, "api_key", None),
                "timestamp": utcnow().isoformat(),
            }
            if hasattr(request.state, "api_key") and request.state.api_key:
                request_data["client_id"] = safe_key_label(request.state.api_key)
            
            ai_result = ai_service.process_request(request_data)
            
            # Add AI insights to response headers
            if ai_result.get("is_anomaly"):
                response.headers["X-Anomaly-Detected"] = "true"
                response.headers["X-Anomaly-Score"] = str(ai_result.get("anomaly_score", 0))
        except _SkipAI:
            pass
        except Exception as e:
            logger.warning("ai_processing_error", error=str(e))
        
        # Bind tenant + api-key id to log context (best effort).
        try:
            api_key_obj = getattr(request.state, "api_key", None)
            if api_key_obj is not None:
                key_id = safe_key_label(api_key_obj)
                if key_id:
                    bind_request_context(api_key_id=key_id)
            tenant_state = getattr(request.state, "tenant_id", None)
            if tenant_state:
                bind_request_context(tenant_id=str(tenant_state))
        except Exception:
            pass

        # Structured access log line — stable schema for downstream tools.
        logger.info(
            "http_access",
            **{
                "http.request.method": request.method,
                "http.request.path": request.url.path,
                "http.request.query": str(request.url.query),
                "http.response.status_code": response.status_code,
                "http.response.bytes": int(response.headers.get("content-length", 0) or 0),
                "http.duration_ms": round(process_time * 1000, 3),
                "user_agent.original": request.headers.get("user-agent"),
                "http.referrer": request.headers.get("referer"),
            },
        )

        # Observability: feed RED metrics + SLO tracker.
        try:
            tenant_id_metric = getattr(request.state, "tenant_id", None)
            obs_record_request(
                method=request.method,
                path=metric_path,
                status_code=response.status_code,
                duration_seconds=process_time,
                protocol="http",
                tenant=tenant_id_metric,
            )
        except Exception as obs_exc:  # noqa: BLE001
            logger.debug("observability_record_failed", error=str(obs_exc))
        
        # Record metrics
        if settings.metrics_enabled:
            # Determine protocol from path
            if "/mcp" in request.url.path:
                protocol = "mcp"
            elif "/a2a" in request.url.path:
                protocol = "a2a"
            elif "/agui" in request.url.path:
                protocol = "ag_ui"
            elif "/acp" in request.url.path:
                protocol = "acp"
            elif "/mqtt" in request.url.path:
                protocol = "mqtt"
            elif "/ws" in request.url.path or "/websocket" in request.url.path:
                protocol = "websocket"
            elif "/grpc" in request.url.path:
                protocol = "grpc"
            elif any(request.url.path.startswith(p) for p in ["/api", "/rest", "/http"]):
                protocol = "http"
            else:
                protocol = "http"  # Default to http
            
            metrics.request_count.labels(
                method=request.method,
                path=metric_path,
                status_code=response.status_code,
                protocol=protocol
            ).inc()
            metrics.request_duration.labels(
                method=request.method,
                path=metric_path,
                protocol=protocol
            ).observe(process_time)
            if auth_rejected and response.status_code == 429:
                try:
                    metrics.rate_limit_hits.labels(
                        client_id=_rate_limit_metric_key(request),
                    ).inc()
                except Exception:  # noqa: BLE001
                    pass
        
        # Add response headers
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{process_time:.3f}"
        response.headers["traceparent"] = format_traceparent(trace_id, span_id, flags)

        # Echo API version info so clients can see deprecation / sunset.
        api_version_obj = getattr(request.state, "api_version", None)
        if api_version_obj is not None:
            response.headers["X-API-Version"] = api_version_obj.version
            warning = api_version_obj.get_warning_header()
            if warning:
                response.headers["Warning"] = warning
            sunset = api_version_obj.get_sunset_header()
            if sunset:
                response.headers["Sunset"] = sunset
            if api_version_obj.deprecated_date is not None:
                response.headers["Deprecation"] = "true"

        # Tenant headers (when resolved)
        tenant = getattr(request.state, "tenant", None)
        if tenant is not None:
            tenant_id_value = (
                tenant.get("tenant_id") if isinstance(tenant, dict) else getattr(tenant, "tenant_id", None)
            )
            if tenant_id_value:
                response.headers["X-Tenant-ID"] = tenant_id_value
            quota = getattr(request.state, "tenant_quota", None) or {}
            if quota.get("minute_limit"):
                response.headers["X-Tenant-Quota-Minute"] = (
                    f"{quota.get('minute_used', 0)}/{quota.get('minute_limit', 0)}"
                )
            if quota.get("hour_limit"):
                response.headers["X-Tenant-Quota-Hour"] = (
                    f"{quota.get('hour_used', 0)}/{quota.get('hour_limit', 0)}"
                )
        
        # Record tenant usage (best-effort, async)
        tenant_id = getattr(request.state, "tenant_id", None)
        # A request the auth layer refused (e.g. over the tenant quota) is not
        # usage; counting it would keep a throttled tenant throttled.
        if tenant_id and not auth_rejected:
            try:
                from app.tenant import tenant_service
                from app.database import AsyncSessionLocal

                is_error = response.status_code >= 400
                content_length = int(response.headers.get("content-length", 0) or 0)

                async def _record_tenant_usage(t_id, err, bytes_out):
                    try:
                        async with AsyncSessionLocal() as session:
                            await tenant_service.record_request(
                                session,
                                t_id,
                                is_error=err,
                                bytes_out=bytes_out,
                            )
                    except Exception as exc:
                        logger.debug("tenant_usage_record_failed", error=str(exc))

                asyncio.create_task(_record_tenant_usage(tenant_id, is_error, content_length))
            except Exception as exc:
                logger.debug("tenant_usage_dispatch_failed", error=str(exc))

        # Export logs to external systems
        if log_export_manager.exporters:
            log_entry = {
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": process_time * 1000,
                "client_ip": request.client.host if request.client else None,
                "timestamp": utcnow().isoformat(),
                "user_agent": request.headers.get("user-agent"),
                "api_key_id": getattr(request.state.api_key, "id", None) if hasattr(request.state, "api_key") and request.state.api_key else None,
                "tenant_id": (
                    tenant.get("tenant_id") if isinstance(tenant, dict)
                    else getattr(tenant, "tenant_id", None) if tenant else None
                ),
            }
            # Determine protocol
            if "/mcp" in request.url.path:
                log_entry["protocol"] = "mcp"
            elif "/a2a" in request.url.path:
                log_entry["protocol"] = "a2a"
            elif "/agui" in request.url.path:
                log_entry["protocol"] = "ag_ui"
            elif "/acp" in request.url.path:
                log_entry["protocol"] = "acp"
            elif "/mqtt" in request.url.path:
                log_entry["protocol"] = "mqtt"
            elif "/ws" in request.url.path or "/websocket" in request.url.path:
                log_entry["protocol"] = "websocket"
            elif "/grpc" in request.url.path:
                log_entry["protocol"] = "grpc"
            elif "/graphql" in request.url.path:
                log_entry["protocol"] = "graphql"
            else:
                log_entry["protocol"] = "http"
            
            # Prefer the batched worker when running; fall back to direct export
            if log_export_manager.is_running():
                log_export_manager.enqueue(log_entry)
            else:
                asyncio.create_task(log_export_manager.export_logs([log_entry]))

        # Persist request log to the database (best-effort, non-blocking).
        # Skip the dashboard's own polling endpoints so they don't pollute
        # the metrics they're supposed to be reporting on.
        try:
            persist_path = request.url.path
            skip_persist_prefixes = (
                "/admin/dashboard/stats",
                "/admin/health",
                "/admin/logs",
                "/metrics",
            )
            if not any(persist_path.startswith(p) for p in skip_persist_prefixes):
                from app.models import RequestLog as RequestLogPydantic

                api_key_obj_for_log = getattr(request.state, "api_key", None)
                api_key_id_for_log = (
                    getattr(api_key_obj_for_log, "id", None)
                    if api_key_obj_for_log is not None
                    else None
                )

                # Determine protocol the same way as above for stable tagging
                if "/mcp" in persist_path:
                    persist_protocol = "mcp"
                elif "/a2a" in persist_path:
                    persist_protocol = "a2a"
                elif "/agui" in persist_path:
                    persist_protocol = "ag_ui"
                elif "/acp" in persist_path:
                    persist_protocol = "acp"
                elif "/mqtt" in persist_path:
                    persist_protocol = "mqtt"
                elif "/ws" in persist_path or "/websocket" in persist_path:
                    persist_protocol = "websocket"
                elif "/grpc" in persist_path:
                    persist_protocol = "grpc"
                elif "/graphql" in persist_path:
                    persist_protocol = "graphql"
                else:
                    persist_protocol = "http"

                pydantic_log = RequestLogPydantic(
                    request_id=request_id,
                    method=request.method,
                    path=persist_path,
                    client_ip=client_ip or "0.0.0.0",
                    api_key=None,
                    status_code=response.status_code,
                    response_time_ms=process_time * 1000,
                    user_agent=request.headers.get("user-agent"),
                    error=(getattr(request.state, "auth_error_code", None) if auth_rejected else None),
                )
                # Pydantic v2 ignores unknown fields; carry protocol via attribute
                # because LogRepository.create reads it via getattr()
                try:
                    setattr(pydantic_log, "protocol", persist_protocol)
                except Exception:
                    pass

                request_log_writer.submit(pydantic_log, api_key_id_for_log)
        except Exception as exc:
            logger.debug("request_log_persist_dispatch_failed", error=str(exc))

        return response
    except Exception as e:
        process_time = time.time() - start_time
        logger.error(
            "request_failed",
            method=request.method,
            path=request.url.path,
            error=str(e),
            error_type=type(e).__name__,
            duration_ms=round(process_time * 1000, 3),
        )
        if settings.metrics_enabled:
            metrics.error_count.labels(
                error_type=type(e).__name__,
                protocol="unknown"
            ).inc()
        raise
    finally:
        clear_request_context()


class _NoTransformation(Exception):
    """Internal: no response transformation rule applies to the request."""


class _SkipAI(Exception):
    """Internal: rejected requests are not fed to the AI analytics."""


def _rate_limit_metric_key(request: Request) -> str:
    """Bounded label for gateway_rate_limit_hits_total: the key's id."""
    principal = getattr(request.state, "api_key", None)
    key_id = getattr(principal, "id", None) if principal is not None else None
    if key_id:
        return str(key_id)
    if principal is not None:
        return "non-database-key"
    return "anonymous"


def _auth_reject(request: Request, status_code: int, data: Dict[str, Any], headers=None):
    """An error response produced by the auth layer, marked as such so the
    logging middleware records it (bounded metric labels, no tenant usage)."""
    try:
        request.state.auth_rejected = True
        request.state.auth_error_code = data.get("error")
    except Exception:  # noqa: BLE001
        pass
    if not data.get("request_id"):
        data["request_id"] = getattr(request.state, "request_id", None)
    return render_error_response(request, status_code, data, headers)


# Known first path segments, used as the metric label of rejected requests so
# that a scanner probing random paths cannot create unbounded series.
_REJECTED_PATH_LABEL_PREFIXES = frozenset({
    "mcp", "a2a", "agui", "acp", "mqtt", "ws", "wss", "websocket", "grpc",
    "graphql", "api", "rest", "http", "agents", "docs", "redoc", "openapi.json",
})


def _rejected_path_label(path: str) -> str:
    first = (path or "/").lstrip("/").split("/", 1)[0]
    return f"/{first}" if first in _REJECTED_PATH_LABEL_PREFIXES else "/other"


def _breaks_out_of_the_path(path: str) -> bool:
    """Whether a decoded request path holds a character that is not path data once it is in a URL.

    A caller's ``/api/x%3F/../admin`` is ``/api/x?/../admin`` once decoded. What reads the URL
    (``request.url.path``: the key's allowed paths, the policies, the agent edge) stops at that
    ``?``, while the path forwarded is the whole of it, so the two would not be the same path. A
    ``#`` does the same, and a control character makes a URL invalid.
    """
    return any(c in "?#" or ord(c) < 0x20 or ord(c) == 0x7F for c in path)


@app.middleware("http")
async def auth_middleware_handler(request: Request, call_next):
    """Authentication middleware handler."""
    # A path that decodes to `?`, `#` or a control character is refused before anything reads it.
    if _breaks_out_of_the_path(request.scope.get("path", "")):
        return render_error_response(
            request,
            status.HTTP_400_BAD_REQUEST,
            {
                "error": "INVALID_PATH",
                "message": "The request path holds an escaped '?', '#' or control character, which cannot be part of a path.",
                "request_id": getattr(request.state, "request_id", None),
                "timestamp": utcnow().isoformat(),
                "details": {},
            },
        )

    # Skip auth for OPTIONS requests (CORS preflight)
    if request.method == "OPTIONS":
        return await call_next(request)

    # Agent edge: `/agents/<slug>/...` is proxied to the per-slug
    # upstream. SSO is enforced by the upstream agent itself (its IDA token audience
    # equals the slug), so the gateway does NOT require its own API key here — it
    # forwards the Authorization header untouched. Stash the resolved route so the
    # proxy handler can forward without re-resolving.
    if settings.agent_edge_enabled or agent_edge_config.has_routes():
        agent_route = agent_edge_config.resolve(request.url.path)
        if agent_route is not None:
            request.state.agent_route = agent_route
            return await call_next(request)
        # A `/agents/<slug>/…` request whose slug is not registered is a genuine
        # 404 (mistyped/retired agent) — not a gateway auth failure. Returning
        # 401 "API key required" here would be misleading, so short-circuit with
        # a proper "agent not found" response (HTML page for browsers).
        if request.url.path.startswith("/agents/"):
            request_id = getattr(request.state, "request_id", None)
            slug = (request.url.path.split("/agents/", 1)[1].split("/", 1)[0]) or None
            return render_error_response(
                request,
                status.HTTP_404_NOT_FOUND,
                {
                    "error": "AGENT_NOT_FOUND",
                    "message": (
                        f"No agent is registered at '{slug}'."
                        if slug
                        else "No agent slug was provided."
                    ),
                    "request_id": request_id,
                    "timestamp": utcnow().isoformat(),
                    "details": {
                        "hint": "Check the agent slug, or register it in the gateway admin console.",
                        "slug": slug,
                    },
                },
            )
    
    # Skip auth for health, metrics, docs, ready/live, OAuth2 endpoints
    # and admin endpoints (admin has its own auth). OAuth2 endpoints are
    # themselves the means for clients to authenticate, so they must be
    # publicly reachable.
    if request.url.path in DOCS_PATHS:
        if app.docs_url or app.openapi_url:
            # DEBUG=true: FastAPI serves them, without a key.
            return await call_next(request)
        # DEBUG=false: not served, and never forwarded to a backend.
        return _auth_reject(
            request,
            status.HTTP_404_NOT_FOUND,
            {
                "error": "NOT_FOUND",
                "message": "API documentation is only served when DEBUG=true.",
                "request_id": getattr(request.state, "request_id", None),
                "timestamp": utcnow().isoformat(),
            },
        )

    public_paths = {
        settings.health_check_path,
        settings.metrics_path,
        "/ready", "/live", "/health/detailed",
        "/.well-known/agent-card.json",
        "/.well-known/ag-ui.json",
    }
    if (request.url.path in public_paths or
        request.url.path.startswith("/admin") or
        request.url.path.startswith("/oauth2")):
        return await call_next(request)
    
    try:
        # Get database session
        from app.database import get_db
        request_id = getattr(request.state, "request_id", None)

        # Plugin: auth providers (short-circuit if any provider authenticates)
        if settings.plugins_enabled:
            try:
                auth_ctx = PluginContext(
                    method=request.method,
                    path=request.url.path,
                    headers=dict(request.headers),
                    request_id=request_id,
                )
                plugin_auth = plugin_registry.authenticate(auth_ctx)
                if plugin_auth:
                    request.state.plugin_auth = plugin_auth
                    return await call_next(request)
            except Exception as exc:
                logger.warning("plugin_auth_provider_failed", error=str(exc))

        async for db in _iter_request_db():
            try:
                try:
                    api_key = await auth_middleware.authenticate(request, db)
                    if api_key:
                        request.state.api_key = api_key
                        # Check rate limits
                        rate_limiter.check_rate_limit(request, api_key)

                        # Resolve tenant + enforce quota (best-effort)
                        try:
                            from app.tenant import tenant_service
                            if getattr(request.state, "tenant_lookup_done", False):
                                # authenticate() already looked the tenant up for this request.
                                tenant_dict = getattr(request.state, "tenant", None)
                            else:
                                tenant_dict = await tenant_service.get_tenant_for_api_key(
                                    db, api_key.key
                                )
                            if tenant_dict and tenant_dict.get("is_active", True):
                                request.state.tenant_id = tenant_dict["tenant_id"]
                                request.state.tenant = tenant_dict
                                # One usage query shared by both checks (and none for
                                # a tenant without limits); falls back to per-call reads.
                                try:
                                    await tenant_service.prime_quota(db, tenant_dict)
                                except Exception as exc:
                                    logger.debug("tenant_quota_prime_failed", error=str(exc))
                                try:
                                    allowed_minute = await tenant_service.check_quota(
                                        db,
                                        tenant_dict["tenant_id"],
                                        window="minute",
                                    )
                                    allowed_hour = await tenant_service.check_quota(
                                        db,
                                        tenant_dict["tenant_id"],
                                        window="hour",
                                    )
                                finally:
                                    try:
                                        tenant_service.clear_quota_prime(db)
                                    except Exception:
                                        pass
                                if not (allowed_minute and allowed_hour):
                                    rid = getattr(request.state, "request_id", None)
                                    window = "minute" if not allowed_minute else "hour"
                                    quota = tenant_dict.get("quota", {}) or {}
                                    limit = quota.get(f"requests_per_{window}", 0)
                                    # RateLimitError's signature is
                                    # (limit, window, retry_after, request_id).
                                    # It was previously called with message=/details=,
                                    # which raised TypeError and was swallowed below,
                                    # silently disabling tenant quota enforcement.
                                    err = RateLimitError(
                                        limit=limit,
                                        window=window,
                                        retry_after=60 if window == "minute" else 3600,
                                        request_id=rid,
                                    )
                                    err.details.update({
                                        "tenant_id": tenant_dict["tenant_id"],
                                        "quota": quota,
                                    })
                                    raise err
                        except RateLimitError:
                            raise
                        except Exception as exc:
                            logger.debug("tenant_resolution_failed", error=str(exc))
                    else:
                        # Some endpoints might not require auth, but for production, we require it
                        logger.info(
                            "auth_middleware_raising_authentication_error",
                            request_id=request_id,
                            path=request.url.path,
                            message="API key required"
                        )
                        exc = AuthenticationError(
                            message="API key required",
                            details={
                                "hint": "Include your API key in the X-API-Key header or Authorization header.",
                                "documentation": docs_link("authentication")
                            },
                            request_id=request_id
                        )
                        logger.info(
                            "auth_middleware_exception_created",
                            exception_type=type(exc).__name__,
                            error_code=exc.error_code,
                            detail_type=type(exc.detail).__name__,
                            detail_is_dict=isinstance(exc.detail, dict),
                            is_gateway=isinstance(exc, GatewayException)
                        )
                        raise exc
                except (HTTPException, GatewayException) as e:
                    logger.info(
                        "auth_middleware_caught_exception",
                        exception_type=type(e).__name__,
                        is_gateway=isinstance(e, GatewayException),
                        detail_type=type(e.detail).__name__ if hasattr(e, 'detail') else None,
                        detail_is_dict=isinstance(e.detail, dict) if hasattr(e, 'detail') else False,
                        re_raising=True
                    )
                    # Format GatewayException directly in middleware
                    if isinstance(e, GatewayException):
                        request_id = e.request_id or getattr(request.state, "request_id", None)
                        response_data = {
                            "error": e.error_code,
                            "message": e.message,
                            "request_id": request_id,
                            "timestamp": utcnow().isoformat(),
                        }
                        if e.details:
                            response_data["details"] = e.details
                        
                        headers = {}
                        if e.retry_after:
                            headers["Retry-After"] = str(e.retry_after)
                        
                        logger.info(
                            "auth_middleware_inner_returning_structured_error",
                            error_code=e.error_code,
                            status_code=e.status_code
                        )
                        return _auth_reject(request, e.status_code, response_data, headers)
                    raise  # Let exception handlers process other exceptions
            except StopIteration:
                pass
            break
    except (HTTPException, GatewayException) as e:
        logger.info(
            "auth_middleware_outer_catch",
            exception_type=type(e).__name__,
            is_gateway=isinstance(e, GatewayException),
            detail_type=type(e.detail).__name__ if hasattr(e, 'detail') else None,
            detail_is_dict=isinstance(e.detail, dict) if hasattr(e, 'detail') else False,
            re_raising=True
        )
        # Format GatewayException directly in middleware
        if isinstance(e, GatewayException):
            request_id = e.request_id or getattr(request.state, "request_id", None)
            response_data = {
                "error": e.error_code,
                "message": e.message,
                "request_id": request_id,
                "timestamp": utcnow().isoformat(),
            }
            if e.details:
                response_data["details"] = e.details
            
            headers = {}
            if e.retry_after:
                headers["Retry-After"] = str(e.retry_after)
            
            logger.info(
                "auth_middleware_outer_returning_structured_error",
                error_code=e.error_code,
                status_code=e.status_code
            )
            return _auth_reject(request, e.status_code, response_data, headers)
        raise  # Re-raise to let exception handlers process other exceptions
    except Exception as e:
        logger.error("auth_middleware_error", error=str(e))
        # Fallback to legacy auth for backward compatibility
        try:
            api_key = await auth_middleware.authenticate(request, None)
            if api_key:
                request.state.api_key = api_key
                rate_limiter.check_rate_limit(request, api_key)
            else:
                request_id = getattr(request.state, "request_id", None)
                logger.debug(
                    "auth_middleware_fallback_raising_authentication_error",
                    request_id=request_id,
                    path=request.url.path
                )
                raise AuthenticationError(
                    message="API key required",
                    details={
                        "hint": "Include your API key in the X-API-Key header or Authorization header.",
                        "documentation": docs_link("authentication")
                    },
                    request_id=request_id
                )
        except (HTTPException, GatewayException) as e:
            logger.info(
                "auth_middleware_fallback_caught_exception",
                exception_type=type(e).__name__,
                is_gateway=isinstance(e, GatewayException),
                detail_type=type(e.detail).__name__ if hasattr(e, 'detail') else None,
                re_raising=True
            )
            # Format GatewayException directly in middleware
            if isinstance(e, GatewayException):
                request_id = e.request_id or getattr(request.state, "request_id", None)
                response_data = {
                    "error": e.error_code,
                    "message": e.message,
                    "request_id": request_id,
                    "timestamp": utcnow().isoformat(),
                }
                if e.details:
                    response_data["details"] = e.details
                
                headers = {}
                if e.retry_after:
                    headers["Retry-After"] = str(e.retry_after)
                
                logger.info(
                    "auth_middleware_fallback_returning_structured_error",
                    error_code=e.error_code,
                    status_code=e.status_code
                )
                return _auth_reject(request, e.status_code, response_data, headers)
            raise  # Let exception handlers process other exceptions
        except Exception:
            request_id = getattr(request.state, "request_id", None)
            raise InternalServerError(
                message="Authentication error occurred",
                request_id=request_id
            )
    
    return await call_next(request)


# Registered after the auth middleware so that it is the OUTER one: requests
# the auth layer rejects (401, 403, 429) still get an X-Request-ID, a metric
# and a request_logs row. Starlette runs the last-registered middleware first.
app.middleware("http")(logging_middleware)


@app.get(settings.health_check_path, response_model=HealthStatus)
async def health_check():
    """Liveness probe — fast, no external calls."""
    return HealthStatus(
        status="healthy",
        version=settings.app_version,
        services={},
    )


def _require_ops_admin(request: Request) -> None:
    """Admin check for the operational endpoints, when it is turned on.

    With ``OPS_ENDPOINTS_REQUIRE_ADMIN=true`` the metrics path and
    ``/health/detailed`` need ``Authorization: Bearer <admin credential>``,
    checked exactly like the admin API. Off by default.
    """
    if not settings.ops_endpoints_require_admin:
        return
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    verify_admin_token_value(token.strip() if scheme.lower() == "bearer" else None)


@app.get("/health/detailed")
async def detailed_health_check(request: Request):
    """Detailed health: runs every registered dependency probe.

    Returns aggregate ``status`` of ``healthy`` / ``degraded`` /
    ``unhealthy`` plus per-probe results. Sets HTTP 503 when overall
    status is unhealthy so external tooling can react.
    """
    _require_ops_admin(request)
    from app.observability import dependency_probes

    results = await dependency_probes.check_all()
    aggregate = dependency_probes.aggregate(results)
    body = {
        "status": aggregate,
        "version": settings.app_version,
        "checked_at": utcnow().isoformat() + "Z",
        "dependencies": [r.to_dict() for r in results],
    }
    return JSONResponse(
        content=body,
        status_code=503 if aggregate == "unhealthy" else 200,
    )


@app.get("/ready")
async def readiness_check():
    """Readiness probe — green only if critical dependencies are up.

    The database is the only hard requirement; cache / backends /
    exporters being down only flag a degraded state but the gateway
    can still serve.
    """
    from app.observability import dependency_probes

    db_result = await dependency_probes.check("database")
    if db_result is None or db_result.status == "unhealthy":
        return JSONResponse(
            content={
                "status": "not_ready",
                "reason": db_result.message if db_result else "no probe registered",
            },
            status_code=503,
        )
    return {"status": "ready", "database": db_result.to_dict()}


@app.get("/live")
async def liveness_check():
    """Liveness probe (for Kubernetes ``livenessProbe``)."""
    return {"status": "alive", "pid": os.getpid()}


@app.get(settings.metrics_path)
async def metrics_endpoint(request: Request):
    """Prometheus metrics endpoint."""
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="Metrics disabled")
    _require_ops_admin(request)
    from starlette.concurrency import run_in_threadpool

    # Rendering can take hundreds of ms with many series; keep it off the event loop.
    content = await run_in_threadpool(generate_latest)
    return Response(content=content, media_type=CONTENT_TYPE_LATEST)


@app.websocket("/ws/{path:path}")
async def websocket_proxy(websocket: WebSocket, path: str):
    """WebSocket proxy endpoint."""
    if not websocket_handler:
        await websocket.close(code=1003, reason="WebSocket not configured")
        return
    
    # Get API key from query params or headers
    api_key = websocket.query_params.get("api_key") or websocket.headers.get("X-API-Key")
    
    if not api_key:
        await websocket.close(code=1008, reason="API key required")
        return
    
    # Authenticate
    try:
        async for db in _iter_request_db():
            api_key_obj = await auth_middleware.authenticate(
                SimpleNamespace(
                    headers={**dict(websocket.headers), 'X-API-Key': api_key},
                    method="GET",
                    state=websocket.state,
                    client=websocket.client,
                    url=websocket.url,
                ),
                db
            )
            if not api_key_obj:
                await websocket.close(code=1008, reason="Invalid API key")
                return
            break
    except Exception:
        await websocket.close(code=1008, reason="Authentication failed")
        return
    
    # Handle WebSocket connection
    await websocket_handler.handle_connection(websocket, f"/{path}")


async def _relay_agent_ws(client_ws: WebSocket, upstream, slug: str):
    """Bidirectionally relay frames between the browser client and the upstream
    agent WebSocket until either side closes."""
    async def client_to_upstream():
        try:
            while True:
                msg = await client_ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except Exception:
            pass
        finally:
            try:
                await upstream.close()
            except Exception:
                pass

    async def upstream_to_client():
        try:
            async for message in upstream:
                if isinstance(message, (bytes, bytearray)):
                    await client_ws.send_bytes(bytes(message))
                else:
                    await client_ws.send_text(message)
        except Exception:
            pass
        finally:
            try:
                await client_ws.close()
            except Exception:
                pass

    await asyncio.gather(client_to_upstream(), upstream_to_client())


@app.websocket("/agents/{full_path:path}")
async def agent_ws_proxy(websocket: WebSocket, full_path: str):
    """WebSocket relay for `/agents/<slug>/...` -> per-slug upstream agent.

    The `/agents/<slug>` prefix is stripped; query string is preserved (browsers
    typically pass the auth token there). SSO is enforced by the upstream agent.
    """
    if not (settings.agent_edge_enabled or agent_edge_config.has_routes()):
        await websocket.close(code=1008)
        return
    route = agent_edge_config.resolve(websocket.url.path)
    if route is None:
        await websocket.close(code=4040)
        return

    upstream_uri = route.ws_upstream_uri(websocket.url.query)
    requested = websocket.scope.get("subprotocols") or []
    # Forward Authorization / Cookie for non-browser clients that can set them.
    fwd_headers = {}
    if websocket.headers.get("authorization"):
        fwd_headers["Authorization"] = websocket.headers["authorization"]
    if websocket.headers.get("cookie"):
        fwd_headers["Cookie"] = websocket.headers["cookie"]

    import websockets
    connect_kwargs = {"subprotocols": requested or None}
    try:
        try:
            upstream = await websockets.connect(
                upstream_uri, additional_headers=fwd_headers or None, **connect_kwargs
            )
        except TypeError:
            # websockets < 13 uses `extra_headers`
            upstream = await websockets.connect(
                upstream_uri, extra_headers=fwd_headers or None, **connect_kwargs
            )
    except Exception as exc:
        logger.warning(
            "agent_ws_upstream_connect_failed",
            slug=route.slug, upstream=upstream_uri, error=str(exc),
        )
        await websocket.close(code=1011)
        return

    await websocket.accept(subprotocol=getattr(upstream, "subprotocol", None))
    logger.info("agent_ws_relay_open", slug=route.slug, upstream=upstream_uri)
    try:
        await _relay_agent_ws(websocket, upstream, route.slug)
    finally:
        try:
            await upstream.close()
        except Exception:
            pass


@app.post("/mqtt/publish")
async def mqtt_publish(request: Request):
    """Publish message to MQTT topic."""
    if not mqtt_handler or not mqtt_handler.connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MQTT broker not connected"
        )
    
    data = await request.json()
    topic = data.get("topic")
    payload = data.get("payload", {})
    qos = data.get("qos", 1)
    
    if not topic:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Topic is required"
        )
    
    success = mqtt_handler.publish(topic, payload, qos=qos)
    
    if success:
        return {"status": "published", "topic": topic}
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to publish message"
        )


@app.post("/mqtt/subscribe")
async def mqtt_subscribe(request: Request):
    """Subscribe to MQTT topic."""
    if not mqtt_handler or not mqtt_handler.connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MQTT broker not connected"
        )
    
    data = await request.json()
    topic = data.get("topic")
    qos = data.get("qos", 1)
    
    if not topic:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Topic is required"
        )
    
    success = mqtt_handler.subscribe(topic, qos=qos)
    
    if success:
        return {"status": "subscribed", "topic": topic}
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to subscribe to topic"
        )


# ==================== OAuth2 Endpoints ====================

@app.get("/oauth2/authorize")
async def oauth2_authorize(
    provider: str,
    redirect_uri: str,
    state: Optional[str] = None,
    scope: Optional[str] = None,
    use_pkce: bool = True,
):
    """Build an OAuth2 authorization URL (RFC 6749, Section 4.1).

    When ``use_pkce`` is true (default), a PKCE verifier is generated and
    cached server-side keyed by ``state``; the matching ``code_challenge``
    is added to the URL.
    """
    try:
        scopes = scope.split() if scope else None
        result = await oauth2_manager.get_authorization_url(
            provider=provider,
            redirect_uri=redirect_uri,
            state=state,
            scopes=scopes,
            use_pkce=use_pkce,
        )
        return JSONResponse(content=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("oauth2_authorize_error", error=str(e), provider=provider)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to generate authorization URL: {str(e)}"
        )


def _client_credentials(request: Request, body: Dict[str, Any]):
    """The client id and secret a token request carries: HTTP Basic first, else the body."""
    scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() == "basic" and encoded.strip():
        try:
            import base64
            from urllib.parse import unquote_plus

            client_id, sep, secret = base64.b64decode(encoded.strip(), validate=True).decode("utf-8").partition(":")
            if sep:
                return unquote_plus(client_id), unquote_plus(secret)
        except Exception:  # noqa: BLE001 - not a Basic credential: nothing presented
            pass
        return None, None
    client_id, secret = body.get("client_id"), body.get("client_secret")
    return (client_id if isinstance(client_id, str) else None, secret if isinstance(secret, str) else None)


@app.post("/oauth2/token")
async def oauth2_token(request: Request):
    """OAuth2 token endpoint (RFC 6749, Sections 4.1.3, 4.4, 6).

    Supports the ``authorization_code``, ``client_credentials`` and
    ``refresh_token`` grant types. Accepts both JSON and form-encoded
    bodies.
    """
    body: Dict[str, Any] = {}
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        body = dict(form)
    else:
        try:
            body = await request.json()
        except Exception:
            try:
                form = await request.form()
                body = dict(form)
            except Exception:
                body = {}

    grant_type = body.get("grant_type", "authorization_code")
    provider = body.get("provider")
    if not provider:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_request", "error_description": "'provider' is required"},
        )

    try:
        if grant_type == "authorization_code":
            code = body.get("code")
            redirect_uri = body.get("redirect_uri")
            state = body.get("state")
            if not code or not redirect_uri:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "invalid_request",
                        "error_description": "'code' and 'redirect_uri' are required",
                    },
                )
            token_data = await oauth2_manager.exchange_code_for_token(
                provider=provider,
                code=code,
                redirect_uri=redirect_uri,
                state=state,
            )
            return JSONResponse(content=token_data)

        if grant_type == "client_credentials":
            # The caller proves who it is with the provider's client credentials (RFC 6749, 2.3.1):
            # HTTP Basic, or client_id and client_secret in the body.
            oauth2_manager.authenticate_client(provider, *_client_credentials(request, body))
            scope = body.get("scope")
            scopes = scope.split() if isinstance(scope, str) and scope else None
            token_data = await oauth2_manager.client_credentials(provider, scopes)
            return JSONResponse(content=token_data)

        if grant_type == "refresh_token":
            refresh_token = body.get("refresh_token")
            if not refresh_token:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={
                        "error": "invalid_request",
                        "error_description": "'refresh_token' is required",
                    },
                )
            token_data = await oauth2_manager.refresh_token(provider, refresh_token)
            return JSONResponse(content=token_data)

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unsupported_grant_type",
                "error_description": f"grant_type '{grant_type}' is not supported",
            },
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("oauth2_token_error", error=str(e), grant_type=grant_type, provider=provider)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "server_error", "error_description": str(e)},
        )


@app.post("/oauth2/introspect")
async def oauth2_introspect(request: Request):
    """OAuth2 token introspection endpoint (RFC 7662)."""
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        body = dict(form)
    else:
        try:
            body = await request.json()
        except Exception:
            form = await request.form()
            body = dict(form)

    provider = body.get("provider")
    token = body.get("token")
    if not provider or not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_request", "error_description": "'provider' and 'token' are required"},
        )
    token_type_hint = body.get("token_type_hint", "access_token")
    result = await oauth2_manager.introspect(provider, token, token_type_hint)
    return JSONResponse(content=result)


@app.post("/oauth2/revoke")
async def oauth2_revoke(request: Request):
    """OAuth2 token revocation endpoint (RFC 7009)."""
    content_type = request.headers.get("content-type", "")
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        body = dict(form)
    else:
        try:
            body = await request.json()
        except Exception:
            form = await request.form()
            body = dict(form)

    provider = body.get("provider")
    token = body.get("token")
    if not provider or not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_request", "error_description": "'provider' and 'token' are required"},
        )
    token_type_hint = body.get("token_type_hint", "access_token")
    revoked = await oauth2_manager.revoke(provider, token, token_type_hint)
    # Per RFC 7009 the response body is empty; we return JSON for clients.
    return JSONResponse(content={"revoked": revoked})


@app.get("/oauth2/userinfo")
async def oauth2_userinfo(provider: str, access_token: str):
    """OAuth2 user info endpoint."""
    try:
        user_info = await oauth2_manager.get_user_info(provider, access_token)
        return JSONResponse(content=user_info)
    except HTTPException:
        raise
    except Exception as e:
        logger.error("oauth2_userinfo_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to get user info: {str(e)}"
        )


@app.get("/oauth2/providers")
async def oauth2_list_providers():
    """List registered OAuth2 providers (public-safe metadata only)."""
    providers = []
    for name in oauth2_manager.list_providers():
        client = oauth2_manager.clients[name]
        providers.append({
            "name": name,
            "authorization_endpoint": client.authorization_endpoint,
            "token_endpoint": client.token_endpoint,
            "userinfo_endpoint": client.userinfo_endpoint,
            "introspection_endpoint": client.introspection_endpoint,
            "revocation_endpoint": client.revocation_endpoint,
            "scopes": client.scopes,
        })
    return JSONResponse(content={"providers": providers})


@app.get("/.well-known/agent-card.json")
async def well_known_a2a_agent_card():
    """A2A Agent Card discovery (RFC 8615 /.well-known)."""
    from app.tier1_registry import tier1_registry

    return JSONResponse(content=tier1_registry.build_agent_card())


@app.get("/.well-known/ag-ui.json")
async def well_known_ag_ui_metadata():
    """Static discovery pointer for AG-UI (Agent-User Interaction Protocol)."""
    return JSONResponse(
        content={
            "name": "AG-UI",
            "title": "Agent-User Interaction Protocol",
            "documentation": "https://docs.ag-ui.com/",
            "description": (
                "Event-based streaming between agents and frontends; gateway proxies "
                "SSE/WebSocket-friendly HTTP via /agui and /api/agui when configured."
            ),
            "transports": ["text/event-stream", "websocket"],
        }
    )


# Response headers the gateway must never copy verbatim from an upstream.
#
# `content-length` is recomputed by Starlette (or omitted for streaming).
#
# `content-encoding` MUST be dropped: httpx transparently *decodes* the body when
# we read it (`aread()` / `aiter_bytes()`), so the bytes we forward downstream are
# already identity. Leaving the original `Content-Encoding: gzip` header in place
# makes browsers try to gunzip plain data and fail with
# ERR_CONTENT_DECODING_FAILED. Terminating transfer compression at the proxy is
# valid reverse-proxy behaviour; the edge (Azure Application Gateway) can
# re-compress if desired.
_STRIP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
}


def _restore_repeated_header_lines(response: Response, upstream_headers, sent_headers) -> None:
    """Give the caller each line of a header the backend sent more than once.

    Response headers travel through the proxy as a dict, and reading httpx headers
    as a dict joins lines of the same name with ", ". For ``Set-Cookie`` that loses
    cookies: a browser reads the joined line as a single cookie, and it cannot be
    split apart again because a cookie may hold a comma of its own (``Expires``).

    ``sent_headers`` is the dict the response was built from. A header is put back
    as separate lines only when its value there is still the joined value httpx
    produced, so a header that a transformation or plugin rewrote or removed stays
    as that step left it.
    """
    lines: dict[str, list[str]] = {}
    for name, value in upstream_headers.multi_items():
        lines.setdefault(name.lower(), []).append(value)
    for name, values in lines.items():
        if len(values) < 2 or name in _STRIP_RESPONSE_HEADERS:
            continue
        sent = [v for k, v in sent_headers.items() if k.lower() == name]
        if sent != [upstream_headers.get(name)]:
            continue
        del response.headers[name]
        for value in values:
            response.headers.append(name, value)


async def _proxy_agent_route(request: Request, agent_route, request_id: str) -> Response:
    """Proxy an ``/agents/<slug>/...`` request to the per-slug upstream.

    The ``/agents/<slug>`` prefix is stripped (via ``rewrite_path``) so the
    upstream agent sees its own root path. Headers (including ``Authorization``,
    which the upstream validates) are forwarded untouched apart from hop-by-hop,
    ``Content-Length`` (httpx recomputes the latter from the body) and
    ``Content-Encoding`` (httpx already decoded the body, see
    :data:`_STRIP_RESPONSE_HEADERS`).
    """
    body = None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            body = await request.body()
        except Exception:
            body = None

    _skip = {"content-length", "connection", "keep-alive", "transfer-encoding", "upgrade"}
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() not in _skip}

    _edge_started = time.perf_counter()
    route_config = RouteConfig(
        path=request.url.path,
        backend_url=agent_route.upstream_base,
        protocol=ProtocolType.HTTP,
        rewrite_path=agent_route.upstream_path,
        timeout=settings.default_timeout,
    )
    # Stream the upstream response instead of buffering it with ``aread()``.
    # Agent endpoints include long-lived NDJSON/SSE progress streams (briefing
    # extraction, deliverables generation) that run for minutes. Buffering under
    # the short ``default_timeout`` (30s) truncated those into a spurious 500
    # (INTERNAL_ERROR) ~30s in. Streaming forwards chunks as they arrive and uses
    # the unbounded tier-2 stream read timeout, so slow pipelines complete and the
    # browser sees live progress.
    upstream = await router.start_upstream_stream(
        method=request.method,
        path=agent_route.upstream_path,
        headers=fwd_headers,
        query_params=dict(request.query_params),
        body=body,
        route_config=route_config,
        repeated=repeated_query(request.query_params),
    )
    resp_headers = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _STRIP_RESPONSE_HEADERS
    }
    ct = upstream.headers.get("content-type")
    # Feed the live Mission Control board (time-to-first-byte from the upstream).
    try:
        from app.agent_monitor import agent_monitor
        agent_monitor.record_request(
            agent_route.slug,
            upstream.status_code,
            round((time.perf_counter() - _edge_started) * 1000, 1),
        )
    except Exception:  # noqa: BLE001
        pass
    logger.info(
        "agent_edge_proxied",
        slug=agent_route.slug,
        upstream=agent_route.upstream_url,
        status=upstream.status_code,
        request_id=request_id,
    )

    async def _agent_stream_body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        _agent_stream_body(),
        status_code=upstream.status_code,
        headers=resp_headers,
        media_type=ct.split(";")[0].strip() if ct else None,
    )


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def gateway_proxy(request: Request, path: str):
    """Main gateway proxy endpoint."""
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    
    # Handle OPTIONS requests (CORS preflight) - return early
    if request.method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD",
                "Access-Control-Allow-Headers": "*",
            }
        )

    # Agent edge: forward `/agents/<slug>/...` to the per-slug upstream
    # (resolved in the auth middleware) with the prefix stripped.
    agent_route = getattr(request.state, "agent_route", None)
    if agent_route is not None:
        return await _proxy_agent_route(request, agent_route, request_id)
    
    try:
        # Handle gRPC requests (would need special handling)
        # Note: path comes without leading slash from FastAPI route
        if path.startswith("grpc") or path.startswith("api/grpc") or path.startswith("/grpc") or path.startswith("/api/grpc"):
            if not grpc_handler:
                request_id = getattr(request.state, "request_id", None)
                raise ServiceUnavailableError(
                    service="gRPC",
                    message="gRPC backend is not configured",
                    details={
                        "hint": "Please configure GRPC_BACKEND_URL in your environment variables.",
                        "documentation": docs_link("configuration")
                    },
                    request_id=request_id
                )
            
            data = await request.json()
            service = data.get("service", "Greeter")
            method = data.get("method", "SayHello")
            request_data = data.get("data", {})

            if data.get("stream") and service == "Greeter" and method == "SayManyHellos":
                done = object()

                def _pull_chunk(gen):
                    try:
                        return next(gen)
                    except StopIteration:
                        return done

                async def ndjson_stream():
                    gen = grpc_handler.iter_say_many_hellos(request_data)
                    loop = asyncio.get_event_loop()
                    while True:
                        row = await loop.run_in_executor(None, _pull_chunk, gen)
                        if row is done:
                            break
                        yield (json.dumps(row) + "\n").encode()

                return StreamingResponse(ndjson_stream(), media_type="application/x-ndjson")

            result = grpc_handler.call_unary(service, method, request_data)
            return JSONResponse(content=result)
        
        # Handle GraphQL requests
        if path.startswith("graphql") or path.startswith("api/graphql") or path.startswith("/graphql") or path.startswith("/api/graphql"):
            if not graphql_handler:
                request_id = getattr(request.state, "request_id", None)
                raise ServiceUnavailableError(
                    service="GraphQL",
                    message="GraphQL backend is not configured",
                    details={
                        "hint": "Please configure GRAPHQL_BACKEND_URL in your environment variables.",
                        "documentation": docs_link("configuration")
                    },
                    request_id=request_id
                )
            
            # Configured: the request goes to the backend as it came, like any other protocol's.

        # Get request body
        body = None
        if request.method in ["POST", "PUT", "PATCH"]:
            try:
                body = await request.body()
            except Exception:
                pass

        from app.tier1_a2a import validate_a2a_http_payload
        from app.tier1_mcp import validate_mcp_payload
        from app.tier1_registry import tier1_registry

        validate_mcp_payload(
            method=request.method,
            path=f"/{path}",
            raw_body=body if isinstance(body, bytes) else None,
            tool_input_schemas=tier1_registry.tool_input_schema_index,
            request_id=request_id,
        )
        validate_a2a_http_payload(
            method=request.method,
            path=f"/{path}",
            raw_body=body if isinstance(body, bytes) else None,
            request_id=request_id,
        )

        from app.tier2_acp import validate_acp_http_payload
        from app.tier2_ag_ui import validate_ag_ui_http_payload

        validate_acp_http_payload(
            method=request.method,
            path=f"/{path}",
            raw_body=body if isinstance(body, bytes) else None,
            request_id=request_id,
        )
        validate_ag_ui_http_payload(
            method=request.method,
            path=f"/{path}",
            raw_body=body if isinstance(body, bytes) else None,
            request_id=request_id,
        )

        # Get query parameters
        query_params = dict(request.query_params)
        
        # Get headers
        headers = dict(request.headers)

        # Plugin: protocol dispatch (custom protocols claim a path prefix)
        if settings.plugins_enabled:
            try:
                proto_ctx = PluginContext(
                    method=request.method,
                    path=f"/{path}",
                    headers=dict(headers),
                    query_params=dict(query_params),
                    body=body,
                    request_id=request_id,
                    api_key=getattr(getattr(request.state, "api_key", None), "key", None),
                    tenant_id=getattr(request.state, "tenant_id", None),
                )
                proto_result = plugin_registry.dispatch_protocol(proto_ctx)
                if proto_result is not None:
                    return JSONResponse(content=proto_result)
            except Exception as exc:
                logger.warning("plugin_protocol_dispatch_failed", error=str(exc))

        # Dynamic route lookup. A route registered via POST /admin/routes takes
        # precedence over the static prefix routing in Router._get_backend_url;
        # when nothing matches, `dynamic_route` stays None and the static
        # fallback applies exactly as before. In-memory read, no I/O.
        dynamic_route = None
        if settings.dynamic_routes_enabled:
            try:
                from app import route_registry
                dynamic_route = route_registry.match(f"/{path}")
                if dynamic_route is not None:
                    logger.debug(
                        "dynamic_route_matched",
                        path=f"/{path}",
                        route_path=dynamic_route.path,
                        backend=dynamic_route.backend_url,
                        request_id=request_id,
                    )
            except Exception as exc:  # noqa: BLE001
                # Routing must never fail because of the registry.
                logger.warning("dynamic_route_lookup_failed", error=str(exc))
                dynamic_route = None

        # Plugin: request transformations
        if settings.plugins_enabled:
            try:
                req_ctx = PluginContext(
                    method=request.method,
                    path=f"/{path}",
                    headers=dict(headers),
                    query_params=dict(query_params),
                    body=body,
                    request_id=request_id,
                    api_key=getattr(getattr(request.state, "api_key", None), "key", None),
                    tenant_id=getattr(request.state, "tenant_id", None),
                )
                req_ctx = plugin_registry.transform_request(req_ctx)
                headers = dict(req_ctx.headers)
                query_params = dict(req_ctx.query_params)
                body = req_ctx.body
                plugin_registry.run_hook("request_pre", req_ctx)
            except Exception as exc:
                logger.warning("plugin_request_transform_failed", error=str(exc))

        from app.tier2_streaming import should_proxy_streaming_http

        if should_proxy_streaming_http(request, f"/{path}"):
            upstream = await router.start_upstream_stream(
                method=request.method,
                path=f"/{path}",
                headers=headers,
                query_params=query_params,
                body=body,
                route_config=dynamic_route,
                repeated=repeated_query(request.query_params),
            )
            resp_headers = {
                k: v
                for k, v in upstream.headers.items()
                if k.lower() not in _STRIP_RESPONSE_HEADERS
            }
            ct = upstream.headers.get("content-type", "text/event-stream")

            async def _stream_body():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                finally:
                    await upstream.aclose()

            streaming_response = StreamingResponse(
                _stream_body(),
                status_code=upstream.status_code,
                headers=resp_headers,
                media_type=ct.split(";")[0].strip() if ct else None,
            )
            _restore_repeated_header_lines(streaming_response, upstream.headers, resp_headers)
            return streaming_response

        # Route request
        response = await router.route_request(
            method=request.method,
            path=f"/{path}",
            headers=headers,
            query_params=query_params,
            body=body,
            route_config=dynamic_route,
            repeated=repeated_query(request.query_params),
        )
        
        # Create response
        response_headers = dict(response.headers)
        logger.debug("response_headers_original", headers=dict(response_headers), request_id=request_id)
        
        # Remove hop-by-hop headers, Content-Length (recomputed) and Content-Encoding
        # (httpx already decoded the body — see _STRIP_RESPONSE_HEADERS).
        response_headers = {k: v for k, v in response_headers.items()
                          if k.lower() not in _STRIP_RESPONSE_HEADERS}
        logger.debug("response_headers_after_hop_by_hop", headers=dict(response_headers), request_id=request_id)
        
        # Get response body
        # httpx.Response.aread() is the async method to read content as bytes
        # We must use aread() in async context for AsyncClient responses
        try:
            content = await response.aread()
            logger.debug("response_content_read", content_length=len(content), request_id=request_id)
        except AttributeError:
            # Fallback: if aread() doesn't exist, try .content property
            try:
                content = response.content
                logger.debug("response_content_read_fallback_content", content_length=len(content), request_id=request_id)
            except Exception as e:
                logger.error("response_read_error", error=str(e), request_id=request_id)
                content = b""
        except Exception as e:
            logger.error("response_read_error", error=str(e), request_id=request_id)
            # Final fallback: try to get text and encode
            try:
                content = response.text.encode('utf-8')
                logger.debug("response_content_read_fallback_text", content_length=len(content), request_id=request_id)
            except:
                content = b""
        
        # Apply response transformations
        from app.transformers import transformation_manager
        request_context = {
            "method": request.method,
            "path": path,
            "headers": dict(request.headers),
        }
        
        # Stays None when transformation is skipped or fails (including binary
        # bodies that are not UTF-8), so the upstream status passes through.
        status_code = None
        try:
            rules = (
                transformation_manager.get_rules_for_path(path, request.method)
                if transformation_manager.rules_by_id else []
            )
            if not rules:
                # No rule applies to this request, so there is nothing to do. Parsing the body
                # and writing it back would change its bytes (spacing, escaping, number
                # formatting) and cost time that grows with its size.
                raise _NoTransformation()
            # The body is read only for a rule that can change it. A rule for headers or the
            # status applies whatever the body is, and leaves its bytes alone.
            body_json = None
            if content and any(rule.changes_response_body() for rule in rules):
                try:
                    body_json = json.loads(content.decode('utf-8'))
                except:
                    body_json = content.decode('utf-8')

            transformed_status, transformed_headers, transformed_body = transformation_manager.transform_response(
                status_code=response.status_code,
                headers=response_headers,
                body=body_json,
                request_context=request_context
            )

            # Convert transformed body back to bytes if needed. Text that came back as it went in
            # is the backend's own bytes already (a JSON string, say, keeps its quotes).
            if transformed_body and isinstance(transformed_body, dict):
                content = json.dumps(transformed_body).encode('utf-8')
            elif transformed_body and isinstance(transformed_body, str) and transformed_body is not body_json:
                content = transformed_body.encode('utf-8')

            response_headers = transformed_headers
            status_code = transformed_status
        except _NoTransformation:
            pass
        except Exception as e:
            logger.warning("response_transformation_error", error=str(e))
        
        # Ensure content is bytes
        if isinstance(content, str):
            content = content.encode('utf-8')
        elif content is None:
            content = b""
        
        # Double-check Content-Length removal (case-insensitive)
        # Starlette will calculate it automatically based on actual content length
        original_headers_count = len(response_headers)
        response_headers = {k: v for k, v in response_headers.items() 
                          if k.lower() != "content-length"}
        if len(response_headers) < original_headers_count:
            logger.info("content_length_removed_final", 
                       removed_count=original_headers_count - len(response_headers),
                       request_id=request_id)
        
        # Log final headers before creating Response (for debugging)
        content_length_in_headers = any(k.lower() == "content-length" for k in response_headers.keys())
        logger.info("response_final_preparation", 
                    content_length_bytes=len(content),
                    status_code=status_code if status_code else response.status_code,
                    headers_count=len(response_headers),
                    headers_keys=list(response_headers.keys())[:10],  # First 10 keys
                    content_length_in_headers=content_length_in_headers,
                    request_id=request_id)
        
        if content_length_in_headers:
            logger.error("content_length_still_present", 
                        headers=dict(response_headers),
                        request_id=request_id)
            # Force remove one more time
            response_headers = {k: v for k, v in response_headers.items() 
                              if k.lower() != "content-length"}
        
        # Use status_code from transformation or original response
        final_status_code = status_code if status_code else response.status_code

        # Plugin: response transformations
        if settings.plugins_enabled:
            try:
                resp_ctx = PluginContext(
                    method=request.method,
                    path=f"/{path}",
                    headers=dict(response_headers),
                    body=content,
                    status_code=final_status_code,
                    request_id=request_id,
                    api_key=getattr(getattr(request.state, "api_key", None), "key", None),
                    tenant_id=getattr(request.state, "tenant_id", None),
                )
                resp_ctx = plugin_registry.transform_response(resp_ctx)
                response_headers = dict(resp_ctx.headers)
                if resp_ctx.body is not None:
                    if isinstance(resp_ctx.body, (bytes, bytearray)):
                        content = bytes(resp_ctx.body)
                    elif isinstance(resp_ctx.body, str):
                        content = resp_ctx.body.encode("utf-8")
                    elif isinstance(resp_ctx.body, dict):
                        content = json.dumps(resp_ctx.body).encode("utf-8")
                if resp_ctx.status_code:
                    final_status_code = resp_ctx.status_code
                response_headers = {k: v for k, v in response_headers.items() if k.lower() != "content-length"}
                plugin_registry.run_hook("response_post", resp_ctx)
            except Exception as exc:
                logger.warning("plugin_response_transform_failed", error=str(exc))

        proxied_response = Response(
            content=content,
            status_code=final_status_code,
            headers=response_headers,
            media_type=response.headers.get("content-type", "application/json")
        )
        _restore_repeated_header_lines(proxied_response, response.headers, response_headers)
        return proxied_response
    
    except HTTPException:
        raise
    except VersionUnavailable as e:
        logger.info(
            "version_unavailable",
            request_id=request_id,
            path=path,
            version=e.version,
        )
        return JSONResponse(
            status_code=e.status_code,
            content={
                "error": "API_VERSION_UNAVAILABLE",
                "message": e.message,
                "version": e.version,
                "request_id": request_id,
                "timestamp": utcnow().isoformat(),
            },
        )
    except httpx.HTTPStatusError as e:
        # An open circuit breaker is surfaced as a synthetic 503 (see
        # Router.route_request); map it to a controlled ServiceUnavailable
        # rather than letting it fall through to the generic 500 handler.
        status_code = e.response.status_code if e.response is not None else 502
        if status_code == 503:
            logger.warning(
                "backend_circuit_open", request_id=request_id, path=path, error=str(e)
            )
            raise ServiceUnavailableError(
                service="Backend",
                message="Upstream service is temporarily unavailable (circuit open)",
                request_id=request_id,
            )
        logger.warning(
            "backend_http_status_error",
            request_id=request_id, path=path, status_code=status_code, error=str(e),
        )
        raise BackendError(
            service="Backend",
            message="Backend returned an error",
            details={"status_code": status_code},
            request_id=request_id,
        )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
            httpx.WriteTimeout, httpx.PoolTimeout, httpx.TransportError) as e:
        # Upstream is unreachable / timed out. Return 502 Bad Gateway, not a
        # generic 500 — the client needs to know the failure is upstream.
        logger.warning(
            "backend_unavailable",
            request_id=request_id, path=path,
            error=str(e), error_type=type(e).__name__,
        )
        if settings.metrics_enabled:
            metrics.error_count.labels(
                error_type=type(e).__name__, protocol="unknown"
            ).inc()
        raise BackendError(
            service="Backend",
            message="Backend service unavailable",
            details={
                "hint": "The upstream backend could not be reached. Check that the "
                        "backend is running and reachable from the gateway.",
            },
            request_id=request_id,
        )
    except RuntimeError as e:
        # Handle closed HTTP client (common in test environments)
        if "client has been closed" in str(e):
            logger.warning(
                "gateway_client_closed",
                request_id=request_id,
                path=path,
                error=str(e)
            )
            raise BackendError(
                service="Gateway",
                message="Gateway HTTP client is not available",
                details={
                    "hint": "The gateway HTTP client is not available. This may occur during shutdown or in test environments.",
                    "documentation": docs_link("troubleshooting")
                },
                request_id=request_id
            )
        raise
    except Exception as e:
        logger.error(
            "gateway_error",
            request_id=request_id,
            path=path,
            error=str(e),
            error_type=type(e).__name__
        )
        
        if settings.metrics_enabled:
            metrics.error_count.labels(
                error_type=type(e).__name__,
                protocol="unknown"
            ).inc()
        
        # Use InternalServerError to avoid exposing internal details
        raise InternalServerError(
            message="An error occurred while processing your request",
            request_id=request_id,
            log_error=str(e)
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        workers=settings.workers if settings.environment == "production" else 1,
        log_level=settings.log_level.lower(),
    )

