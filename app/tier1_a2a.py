"""A2A HTTP+JSON binding: validate high-signal operations at the gateway boundary."""
from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from app.config import get_settings
from app.exceptions import ValidationError


def is_a2a_path(path: str) -> bool:
    p = path if path.startswith("/") else f"/{path}"
    return p.startswith("/a2a") or p.startswith("/api/a2a")


def _path_implies_send_message(path: str) -> bool:
    p = path if path.startswith("/") else f"/{path}"
    return "message:send" in p or "message%3Asend" in p.lower()


def _path_implies_stream_message(path: str) -> bool:
    p = path if path.startswith("/") else f"/{path}"
    return "message:stream" in p or "message%3Astream" in p.lower()


def validate_a2a_http_payload(
    *,
    method: str,
    path: str,
    raw_body: Optional[bytes],
    request_id: Optional[str] = None,
) -> None:
    settings = get_settings()
    if not settings.a2a_boundary_enabled:
        return
    if not is_a2a_path(path):
        return
    if method.upper() != "POST":
        return
    if not raw_body:
        return
    enforce = settings.a2a_enforce_send_message_shape
    if not enforce:
        return
    if not (_path_implies_send_message(path) or _path_implies_stream_message(path)):
        return
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            message="A2A POST expects valid UTF-8 JSON",
            field="body",
            details={"error": str(exc)},
            request_id=request_id,
        )
    if not isinstance(payload, Mapping):
        raise ValidationError(
            message="A2A request body must be a JSON object",
            field="body",
            request_id=request_id,
        )
    msg = payload.get("message")
    if msg is None:
        raise ValidationError(
            message="A2A SendMessage-style request requires a 'message' object",
            field="message",
            request_id=request_id,
        )
    if not isinstance(msg, Mapping):
        raise ValidationError(
            message="A2A 'message' must be an object",
            field="message",
            request_id=request_id,
        )
    role = msg.get("role")
    if role is None:
        raise ValidationError(
            message="A2A message requires 'role'",
            field="message.role",
            request_id=request_id,
        )
    if not isinstance(role, str):
        raise ValidationError(
            message="A2A message.role must be a string",
            field="message.role",
            request_id=request_id,
        )
    parts = msg.get("parts")
    if parts is None:
        raise ValidationError(
            message="A2A message requires non-empty 'parts' array",
            field="message.parts",
            request_id=request_id,
        )
    if not isinstance(parts, list) or not parts:
        raise ValidationError(
            message="A2A message.parts must be a non-empty array",
            field="message.parts",
            request_id=request_id,
        )
    for i, part in enumerate(parts):
        if not isinstance(part, Mapping):
            raise ValidationError(
                message=f"A2A message.parts[{i}] must be an object",
                field="message.parts",
                request_id=request_id,
            )
        if "text" not in part and "data" not in part and "file" not in part:
            raise ValidationError(
                message=f"A2A message.parts[{i}] must include text, data, or file content",
                field="message.parts",
                request_id=request_id,
            )
