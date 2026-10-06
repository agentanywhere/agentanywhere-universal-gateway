"""Tier-2: light validation for ACP (BeeAI / IBM) REST shapes at the gateway boundary."""
from __future__ import annotations

import json
from typing import Mapping, Optional

from app.config import get_settings
from app.exceptions import ValidationError


def _is_acp_path(path: str) -> bool:
    p = path if path.startswith("/") else f"/{path}"
    return "/acp" in p or "/api/acp" in p


def validate_acp_http_payload(
    *,
    method: str,
    path: str,
    raw_body: Optional[bytes],
    request_id: Optional[str] = None,
) -> None:
    settings = get_settings()
    if not getattr(settings, "acp_boundary_enabled", True):
        return
    if not _is_acp_path(path):
        return
    if method.upper() not in ("POST", "PUT", "PATCH"):
        return
    if not raw_body:
        return

    p = path if path.startswith("/") else f"/{path}"
    tail = [x for x in p.rstrip("/").split("/") if x]
    if len(tail) >= 2 and tail[-1] == "cancel" and tail[-2] == "runs":
        return

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            message="ACP POST expects valid UTF-8 JSON",
            field="body",
            details={"error": str(exc)},
            request_id=request_id,
        )

    if not isinstance(payload, Mapping):
        raise ValidationError(
            message="ACP request body must be a JSON object",
            field="body",
            request_id=request_id,
        )

    # POST .../runs/{run_id} — resume
    is_resume = len(tail) >= 3 and tail[-2] == "runs" and tail[-1] != "runs"

    if is_resume:
        if "await_resume" not in payload and "awaitResume" not in payload:
            raise ValidationError(
                message="ACP RunResumeRequest requires 'await_resume'",
                field="await_resume",
                request_id=request_id,
            )
        return

    # POST .../runs — create
    if not (len(tail) >= 1 and tail[-1] == "runs"):
        return

    if "agent_name" not in payload and "agentName" not in payload:
        raise ValidationError(
            message="ACP RunCreateRequest requires 'agent_name'",
            field="agent_name",
            request_id=request_id,
        )
    inp = payload.get("input")
    if inp is None:
        raise ValidationError(
            message="ACP RunCreateRequest requires 'input' message list",
            field="input",
            request_id=request_id,
        )
    if not isinstance(inp, list) or not inp:
        raise ValidationError(
            message="ACP RunCreateRequest 'input' must be a non-empty array",
            field="input",
            request_id=request_id,
        )
