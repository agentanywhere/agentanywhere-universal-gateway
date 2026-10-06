"""Tier-2: detect streaming HTTP (SSE) proxy for AG-UI, ACP runs, A2A message:stream."""
from __future__ import annotations

from fastapi import Request


def should_proxy_streaming_http(request: Request, path: str) -> bool:
    """Return True when the gateway should use an HTTP streaming (chunked) upstream proxy."""
    p = path if path.startswith("/") else f"/{path}"
    acc = (request.headers.get("accept") or "").lower()
    wants_sse = "text/event-stream" in acc
    qp = request.query_params

    if "message%3Astream" in p.lower() or "message:stream" in p:
        return True

    if "/agui" in p or "/api/agui" in p:
        if wants_sse or qp.get("sse") in ("1", "true", "yes"):
            return True

    if "/acp" in p or "/api/acp" in p:
        if request.method.upper() == "POST" and "/runs" in p and wants_sse:
            return True
        if qp.get("stream") in ("1", "true", "yes") and "/runs" in p:
            return True

    return False
