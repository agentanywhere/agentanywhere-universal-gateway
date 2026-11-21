"""Request routing logic."""
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse
from app.config import get_settings
from app.models import ProtocolType, RouteConfig
from app.transformers import transformation_manager
from app.versioning import version_manager
from app.webhooks import webhook_manager, WebhookEventType
import httpx
import time
import asyncio

settings = get_settings()



def repeated_query(query) -> Dict[str, List[str]]:
    """The values of every query key the caller gave more than once.

    The proxy path carries the query as a plain dict, which holds one value a
    key, so ``?tag=a&tag=b`` used to reach the backend as ``?tag=b``. The
    values are kept here, beside the dict, and put back before sending.
    """
    pairs = getattr(query, "multi_items", None)
    if pairs is None:  # a plain mapping holds one value a key already
        return {}
    seen: Dict[str, List[str]] = {}
    for key, value in pairs():
        seen.setdefault(key, []).append(value)
    return {key: values for key, values in seen.items() if len(values) > 1}


def _with_repeats(
    query: Dict[str, Any], repeated: Optional[Dict[str, List[str]]]
) -> Dict[str, Any]:
    """Put back the repeated values of each key nothing changed on the way.

    A key a rule or plugin set, changed or removed keeps what that left.
    """
    if not repeated:
        return query
    out = dict(query)
    for key, values in repeated.items():
        if key in out and out[key] == values[-1]:
            out[key] = list(values)
    return out

def graphql_base(url: str) -> str:
    """Where GraphQL requests are sent, less the ``/graphql`` their own path supplies.

    ``GRAPHQL_BACKEND_URL`` may name the server or its endpoint: ``http://host:4000`` and
    ``http://host:4000/graphql`` both send ``/graphql`` to ``http://host:4000/graphql``.
    """
    base = url.rstrip("/")
    return base[: -len("/graphql")] if base.endswith("/graphql") else base


class Router:
    """Request router for MCP and A2A protocols."""
    
    def __init__(self, client=None):
        if client is None:
            from app.adapters import build_upstream_client

            client = build_upstream_client(settings)
        self.client = client
        self.circuit_breakers: Dict[str, Dict[str, Any]] = {}
    
    def _get_backend_url(self, path: str) -> Tuple[str, ProtocolType]:
        """Determine backend URL and protocol based on path."""
        if path.startswith("/mcp") or path.startswith("/api/mcp"):
            return settings.mcp_backend_url, ProtocolType.MCP
        elif path.startswith("/a2a") or path.startswith("/api/a2a"):
            return settings.a2a_backend_url, ProtocolType.A2A
        elif path.startswith("/agui") or path.startswith("/api/agui"):
            if settings.ag_ui_backend_url:
                return settings.ag_ui_backend_url, ProtocolType.AG_UI
            raise ValueError("AG-UI backend URL not configured")
        elif path.startswith("/acp") or path.startswith("/api/acp"):
            if settings.acp_backend_url:
                return settings.acp_backend_url, ProtocolType.ACP
            raise ValueError("ACP backend URL not configured")
        elif path.startswith("/mqtt") or path.startswith("/api/mqtt"):
            # MQTT routing
            if settings.mqtt_broker_url:
                return settings.mqtt_broker_url, ProtocolType.MQTT
            raise ValueError("MQTT broker URL not configured")
        elif path.startswith("/ws") or path.startswith("/websocket") or path.startswith("/wss"):
            # WebSocket routing
            if settings.websocket_backend_url:
                return settings.websocket_backend_url, ProtocolType.WEBSOCKET
            raise ValueError("WebSocket backend URL not configured")
        elif path.startswith("/grpc") or path.startswith("/api/grpc"):
            # gRPC routing
            if settings.grpc_backend_url:
                return settings.grpc_backend_url, ProtocolType.GRPC
            raise ValueError("gRPC backend URL not configured")
        elif path.startswith("/graphql") or path.startswith("/api/graphql"):
            # GraphQL routing
            if settings.graphql_backend_url:
                return graphql_base(settings.graphql_backend_url), ProtocolType.GRAPHQL
            raise ValueError("GraphQL backend URL not configured")
        elif path.startswith("/api") or path.startswith("/rest") or path.startswith("/http"):
            # HTTP/REST routing
            if settings.http_backend_url:
                return settings.http_backend_url, ProtocolType.HTTP
            # Fallback to MCP if HTTP backend not configured
            return settings.mcp_backend_url, ProtocolType.HTTP
        else:
            # Default routing - can be configured
            # If HTTP backend is set, use it; otherwise default to MCP
            if settings.http_backend_url:
                return settings.http_backend_url, ProtocolType.HTTP
            return settings.mcp_backend_url, ProtocolType.MCP
    
    def _check_circuit_breaker(self, backend_url: str) -> bool:
        """Check if circuit breaker is open."""
        if not settings.circuit_breaker_enabled:
            return True
        
        cb = self.circuit_breakers.get(backend_url, {})
        if cb.get("state") == "open":
            # Check if timeout has passed
            if cb.get("opened_at", 0) + settings.circuit_breaker_timeout < time.time():
                # Try to close circuit breaker
                cb["state"] = "half-open"
                cb["failure_count"] = 0
                return True
            return False
        
        return True
    
    def _record_success(self, backend_url: str):
        """Record successful request."""
        if settings.circuit_breaker_enabled:
            cb = self.circuit_breakers.get(backend_url, {})
            previous_state = cb.get("state", "closed")
            if cb.get("state") == "half-open":
                cb["state"] = "closed"
                # Trigger webhook when circuit breaker closes (from half-open to closed)
                if previous_state == "half-open":
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(
                                webhook_manager.trigger_webhook(
                                    WebhookEventType.CIRCUIT_BREAKER_CLOSED,
                                    {
                                        "backend_url": backend_url,
                                        "state": "closed",
                                        "previous_state": previous_state,
                                        "failure_count": cb.get("failure_count", 0),
                                        "message": "Circuit breaker closed after successful request"
                                    }
                                )
                            )
                            # Send email notification
                            from app.notifications import notification_manager
                            asyncio.create_task(
                                notification_manager.send_circuit_breaker_alert(
                                    backend_url=backend_url,
                                    state="closed",
                                    previous_state=previous_state,
                                    failure_count=cb.get("failure_count", 0),
                                    message="Circuit breaker closed after successful request"
                                )
                            )
                    except (RuntimeError, AttributeError):
                        # No event loop running, webhook will be skipped
                        pass
            cb["failure_count"] = 0
            self.circuit_breakers[backend_url] = cb
    
    def _record_failure(self, backend_url: str):
        """Record failed request."""
        if settings.circuit_breaker_enabled:
            cb = self.circuit_breakers.get(backend_url, {
                "state": "closed",
                "failure_count": 0,
                "opened_at": 0
            })
            previous_state = cb.get("state", "closed")
            cb["failure_count"] = cb.get("failure_count", 0) + 1
            
            if cb["failure_count"] >= settings.circuit_breaker_failure_threshold:
                cb["state"] = "open"
                cb["opened_at"] = time.time()
                # Trigger webhook when circuit breaker opens
                if previous_state != "open":
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            asyncio.create_task(
                                webhook_manager.trigger_webhook(
                                    WebhookEventType.CIRCUIT_BREAKER_OPEN,
                                    {
                                        "backend_url": backend_url,
                                        "state": "open",
                                        "previous_state": previous_state,
                                        "failure_count": cb["failure_count"],
                                        "failure_threshold": settings.circuit_breaker_failure_threshold,
                                        "opened_at": cb["opened_at"],
                                        "timeout_seconds": settings.circuit_breaker_timeout,
                                        "message": f"Circuit breaker opened after {cb['failure_count']} failures"
                                    }
                                )
                            )
                            # Send email notification
                            from app.notifications import notification_manager
                            asyncio.create_task(
                                notification_manager.send_circuit_breaker_alert(
                                    backend_url=backend_url,
                                    state="open",
                                    previous_state=previous_state,
                                    failure_count=cb["failure_count"],
                                    message=f"Circuit breaker opened after {cb['failure_count']} failures"
                                )
                            )
                    except (RuntimeError, AttributeError):
                        # No event loop running, webhook will be skipped
                        pass
            
            self.circuit_breakers[backend_url] = cb
    
    def _apply_tier2_path_strip(self, path: str, protocol: ProtocolType) -> str:
        """Strip /acp or /agui gateway prefixes so ACP/AG-UI servers see spec-native paths.

        A GraphQL path loses only a leading ``/api``: the server is sent ``/graphql`` either way.
        """
        if protocol == ProtocolType.GRAPHQL:
            return path[len("/api"):] if path.startswith("/api/graphql") else path
        if protocol not in (ProtocolType.ACP, ProtocolType.AG_UI):
            return path
        normalized = path if path.startswith("/") else f"/{path}"
        segments = ("acp", "api/acp") if protocol == ProtocolType.ACP else ("agui", "api/agui")
        for seg in segments:
            prefix = f"/{seg}"
            if normalized == prefix:
                return "/"
            if normalized.startswith(prefix + "/"):
                rest = normalized[len(prefix):]
                return rest or "/"
        return path

    def _prepare_upstream_parts(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        query_params: Dict[str, Any],
        body: Optional[Any] = None,
        route_config: Optional[RouteConfig] = None,
        repeated: Optional[Dict[str, List[str]]] = None,
    ) -> Tuple[str, Dict[str, str], Dict[str, Any], Optional[bytes], Optional[Any], str, ProtocolType, int]:
        if route_config:
            backend_url = route_config.backend_url
            protocol = route_config.protocol
            timeout = route_config.timeout
            rewrite_path = route_config.rewrite_path
        else:
            backend_url, protocol = self._get_backend_url(path)
            timeout = settings.default_timeout
            rewrite_path = None

        target_path = rewrite_path if rewrite_path else path
        if not route_config:
            target_path = self._apply_tier2_path_strip(target_path, protocol)

        transformed_headers, transformed_query_params, transformed_body, target_path = (
            transformation_manager.transform_request(
                method=method,
                path=target_path,
                headers=headers,
                query_params=query_params,
                body=body,
            )
        )
        transformed_query_params = _with_repeats(transformed_query_params, repeated)

        api_version, version_target_path = version_manager.resolve(
            target_path, transformed_headers
        )
        if api_version:
            transformed_headers["X-API-Version"] = api_version.version
            warning = api_version.get_warning_header()
            if warning:
                transformed_headers["Warning"] = warning
            sunset = api_version.get_sunset_header()
            if sunset:
                transformed_headers["Sunset"] = sunset
            if not route_config and api_version.backend_url:
                backend_url = api_version.backend_url
            if version_manager.strip_prefix_on_proxy and version_target_path:
                target_path = version_target_path

        # ``Transfer-Encoding`` says how the caller framed its message. The body has been
        # read whole by now and goes on with its length, so the header does not go on with it.
        filtered_headers = {k: v for k, v in transformed_headers.items()
                            if not k.lower().startswith("x-gateway-")
                            and k.lower() != "transfer-encoding"}

        filtered_headers["X-Forwarded-For"] = transformed_headers.get("X-Forwarded-For", "")
        filtered_headers["X-Protocol"] = protocol.value

        try:
            from app.logging_config import (
                get_request_context,
                format_traceparent,
                new_trace_context,
            )

            ctx = get_request_context()
            trace_id = ctx.get("trace.id")
            span_id = ctx.get("span.id")
            if not trace_id or not span_id:
                trace_id, span_id, flags = new_trace_context()
            else:
                flags = "01"
            filtered_headers["traceparent"] = format_traceparent(trace_id, span_id, flags)
        except Exception:
            pass

        url = f"{backend_url.rstrip('/')}{target_path}"

        content: Optional[bytes] = None
        json_payload: Optional[Any] = None
        if transformed_body is not None:
            if isinstance(transformed_body, bytes):
                content = transformed_body
            elif isinstance(transformed_body, str) and transformed_body is not body:
                # Text a rule made of the body (a filled-in template) is sent as that text.
                content = transformed_body.encode("utf-8")
            else:
                json_payload = transformed_body
        if transformed_body is not body:
            # A rule changed the body, so the length the caller declared is no longer its length.
            filtered_headers = {k: v for k, v in filtered_headers.items() if k.lower() != "content-length"}

        return url, filtered_headers, transformed_query_params, content, json_payload, backend_url, protocol, timeout

    async def route_request(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        query_params: Dict[str, Any],
        body: Optional[Any] = None,
        route_config: Optional[RouteConfig] = None,
        repeated: Optional[Dict[str, List[str]]] = None,
    ) -> httpx.Response:
        """Route request to appropriate backend."""
        url, filtered_headers, transformed_query_params, content, json_payload, backend_url, protocol, timeout = (
            self._prepare_upstream_parts(
                method, path, headers, query_params, body, route_config, repeated
            )
        )
        if not self._check_circuit_breaker(backend_url):
            raise httpx.HTTPStatusError(
                "Circuit breaker is open",
                request=httpx.Request(method, backend_url),
                response=httpx.Response(503)
            )

        try:
            from app.observability import tracer as _tracer

            async with _tracer.aspan(
                "backend.forward",
                attributes={
                    "backend.url": backend_url,
                    "backend.protocol": protocol.value,
                    "http.method": method,
                    "http.target": urlparse(url).path or "/",
                },
            ) as upstream_span:
                response = await self.client.request(
                    method=method,
                    url=url,
                    headers=filtered_headers,
                    params=transformed_query_params,
                    content=content,
                    json=json_payload,
                    timeout=timeout,
                )
                upstream_span.set_attribute("http.status_code", response.status_code)
            self._record_success(backend_url)
            return response
        except Exception as e:
            self._record_failure(backend_url)
            raise

    async def start_upstream_stream(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        query_params: Dict[str, Any],
        body: Optional[Any] = None,
        route_config: Optional[RouteConfig] = None,
        repeated: Optional[Dict[str, List[str]]] = None,
    ) -> httpx.Response:
        """Open a streaming HTTP response; caller must ``await resp.aclose()`` when done."""
        url, filtered_headers, transformed_query_params, content, json_payload, backend_url, protocol, _timeout = (
            self._prepare_upstream_parts(
                method, path, headers, query_params, body, route_config, repeated
            )
        )
        if not self._check_circuit_breaker(backend_url):
            raise httpx.HTTPStatusError(
                "Circuit breaker is open",
                request=httpx.Request(method, backend_url),
                response=httpx.Response(503)
            )
        read_t = settings.tier2_stream_read_timeout
        stream_timeout = httpx.Timeout(
            settings.tier2_stream_connect_timeout,
            read=read_t,
        )
        req = self.client.build_request(
            method,
            url,
            headers=filtered_headers,
            params=transformed_query_params,
            content=content,
            json=json_payload,
            timeout=stream_timeout,
        )
        try:
            from app.observability import tracer as _tracer

            async with _tracer.aspan(
                "backend.forward.stream",
                attributes={
                    "backend.url": backend_url,
                    "backend.protocol": protocol.value,
                    "http.method": method,
                },
            ) as upstream_span:
                resp = await self.client.send(req, stream=True)
                upstream_span.set_attribute("http.status_code", resp.status_code)
            self._record_success(backend_url)
            return resp
        except Exception:
            self._record_failure(backend_url)
            raise

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

