"""Tier-2: optional AG-UI (Agent-User Interaction) JSON checks at the HTTP boundary.

AG-UI is transport-agnostic (SSE, WebSockets, etc.); the gateway validates
common JSON POST envelopes when enabled.
"""
from __future__ import annotations

import json
from typing import Mapping, Optional

from app.config import get_settings
from app.exceptions import ValidationError


def _is_ag_ui_path(path: str) -> bool:
    p = path if path.startswith("/") else f"/{path}"
    return "/agui" in p or "/api/agui" in p


def validate_ag_ui_http_payload(
    *,
    method: str,
    path: str,
    raw_body: Optional[bytes],
    request_id: Optional[str] = None,
) -> None:
    settings = get_settings()
    if not getattr(settings, "ag_ui_boundary_enabled", True):
        return
    if not _is_ag_ui_path(path):
        return
    if method.upper() != "POST":
        return
    if not raw_body:
        return

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            message="AG-UI POST expects valid UTF-8 JSON",
            field="body",
            details={"error": str(exc)},
            request_id=request_id,
        )

    if not isinstance(payload, Mapping):
        raise ValidationError(
            message="AG-UI request body must be a JSON object",
            field="body",
            request_id=request_id,
        )

    # Minimal run / thread envelope many AG-UI servers accept (all optional)
    messages = payload.get("messages")
    if messages is not None and not isinstance(messages, list):
        raise ValidationError(
            message="AG-UI 'messages' must be an array when present",
            field="messages",
            request_id=request_id,
        )
