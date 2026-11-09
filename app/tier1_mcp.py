"""MCP-aware request validation: JSON-RPC envelope, tool schemas, prompt-injection heuristics."""
from __future__ import annotations

import functools
import json
import re
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Set, Tuple, Union

from app.config import get_settings
from app.exceptions import ValidationError

try:
    import jsonschema
except ImportError:  # pragma: no cover
    jsonschema = None

_DEFAULT_INJECTION_PATTERNS: List[str] = [
    r"ignore\s+(all\s+)?(previous|prior)\s+(instructions?|prompts?|rules?)",
    r"disregard\s+(the\s+)?(above|system)",
    r"system\s*:\s*you\s+are",
    r"\[\s*INST\s*\]",
    r"new\s+instructions?\s*:",
    r"override\s+(the\s+)?(system|developer)",
    r"you\s+are\s+now\s+(a\s+)?(DAN|unrestricted)",
]


@functools.lru_cache(maxsize=32)
def _compile_patterns_cached(raw: Tuple[str, ...]) -> Tuple[re.Pattern, ...]:
    out: List[re.Pattern] = []
    for p in raw:
        try:
            out.append(re.compile(p, re.IGNORECASE | re.MULTILINE))
        except re.error:
            continue
    return tuple(out)


def _compile_patterns(raw: List[str]) -> List[re.Pattern]:
    """Compile (once per distinct pattern list) the injection regexes."""
    return list(_compile_patterns_cached(tuple(raw)))


def _iter_json_strings(obj: Any, depth: int = 0) -> Iterator[str]:
    if depth > 24:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, Mapping):
        for v in obj.values():
            yield from _iter_json_strings(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_json_strings(v, depth + 1)


def _flatten_method_name(method: Optional[str]) -> Optional[str]:
    if not method:
        return None
    m = str(method).strip()
    if "/" in m:
        return m.split("/")[-1].strip() or m
    return m


def is_mcp_path(path: str) -> bool:
    p = path if path.startswith("/") else f"/{path}"
    return p.startswith("/mcp") or p.startswith("/api/mcp")


def validate_mcp_payload(
    *,
    method: str,
    path: str,
    raw_body: Optional[bytes],
    tool_input_schemas: Mapping[str, Mapping[str, Any]],
    request_id: Optional[str] = None,
) -> None:
    """Validate MCP JSON-RPC bodies for POSTs. No-op for non-MCP paths or empty bodies.

    Raises ValidationError when the payload fails policy or schema checks.
    """
    settings = get_settings()
    if not settings.mcp_boundary_enabled:
        return
    if not is_mcp_path(path):
        return
    if method.upper() != "POST":
        return
    if not raw_body:
        return
    max_b = settings.mcp_boundary_max_body_bytes
    if max_b and len(raw_body) > max_b:
        raise ValidationError(
            message="MCP request body exceeds configured maximum size",
            field="body",
            details={"max_bytes": max_b, "actual": len(raw_body)},
            request_id=request_id,
        )
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            message="MCP POST expects valid UTF-8 JSON",
            field="body",
            details={"error": str(exc)},
            request_id=request_id,
        )

    # Notifications may omit id; batch arrays are unusual for MCP HTTP — validate each element.
    if isinstance(payload, list):
        for i, item in enumerate(payload):
            if isinstance(item, Mapping):
                _validate_single_jsonrpc(
                    item,
                    tool_input_schemas=tool_input_schemas,
                    request_id=request_id,
                    idx=i,
                )
        return

    if not isinstance(payload, Mapping):
        raise ValidationError(
            message="MCP JSON-RPC root must be an object or batch array",
            field="body",
            request_id=request_id,
        )
    _validate_single_jsonrpc(
        payload,
        tool_input_schemas=tool_input_schemas,
        request_id=request_id,
    )


def _validate_single_jsonrpc(
    payload: Mapping[str, Any],
    *,
    tool_input_schemas: Mapping[str, Mapping[str, Any]],
    request_id: Optional[str],
    idx: Optional[int] = None,
) -> None:
    settings = get_settings()
    if "jsonrpc" in payload and payload["jsonrpc"] != "2.0":
        raise ValidationError(
            message="Invalid or unsupported jsonrpc version (expected 2.0)",
            field="jsonrpc",
            details={"value": payload.get("jsonrpc"), "index": idx},
            request_id=request_id,
        )
    rpc_method = payload.get("method")
    if rpc_method is not None and not isinstance(rpc_method, str):
        raise ValidationError(
            message="JSON-RPC method must be a string",
            field="method",
            request_id=request_id,
        )
    flat = _flatten_method_name(rpc_method) if rpc_method else None

    blocked: Set[str] = {b.lower() for b in (settings.mcp_boundary_blocked_methods or [])}
    if rpc_method and str(rpc_method).lower() in blocked:
        raise ValidationError(
            message=f"MCP method {rpc_method!r} is blocked by gateway policy",
            field="method",
            request_id=request_id,
        )
    if flat and flat.lower() in blocked:
        raise ValidationError(
            message=f"MCP method {flat!r} is blocked by gateway policy",
            field="method",
            request_id=request_id,
        )

    allowed = settings.mcp_boundary_allowed_methods
    if allowed and rpc_method:
        allowed_l = {a.lower() for a in allowed}
        rm = str(rpc_method).lower()
        fl = flat.lower() if flat else ""
        if rm not in allowed_l and fl not in allowed_l:
            raise ValidationError(
                message=f"MCP method {rpc_method!r} is not on the gateway allowlist",
                field="method",
                request_id=request_id,
            )

    params = payload.get("params")
    if params is not None and not isinstance(params, Mapping):
        # Some endpoints pass list params — allow without deep inspection
        if not isinstance(params, list):
            raise ValidationError(
                message="JSON-RPC params must be an object or array when present",
                field="params",
                request_id=request_id,
            )

    if isinstance(params, Mapping):
        _check_prompt_injection(params, request_id=request_id)
        rm_l = str(rpc_method or "").lower().replace(".", "/")
        if rm_l == "tools/call" or rm_l.endswith("/tools/call"):
            # ``tool_input_schemas`` may be a zero-arg callable so callers
            # only pay for the schema index on actual tools/call requests.
            schemas = tool_input_schemas() if callable(tool_input_schemas) else tool_input_schemas
            _validate_tools_call(params, schemas, request_id=request_id)


def _check_prompt_injection(params: Mapping[str, Any], *, request_id: Optional[str]) -> None:
    settings = get_settings()
    patterns = _compile_patterns(
        list(settings.mcp_boundary_injection_patterns or []) or _DEFAULT_INJECTION_PATTERNS
    )
    for text in _iter_json_strings(params):
        sample = text if len(text) <= 50_000 else text[:50_000]
        for rx in patterns:
            if rx.search(sample):
                action = (settings.mcp_boundary_prompt_injection_action or "block").lower()
                if action == "audit":
                    return
                raise ValidationError(
                    message="Possible prompt-injection pattern blocked at MCP boundary",
                    field="params",
                    details={"pattern": rx.pattern[:120]},
                    request_id=request_id,
                )


def _validate_tools_call(
    params: Mapping[str, Any],
    tool_input_schemas: Mapping[str, Mapping[str, Any]],
    *,
    request_id: Optional[str],
) -> None:
    # MCP tools/call: { "name": "...", "arguments": { ... } }
    name = params.get("name")
    if name is not None and not isinstance(name, str):
        raise ValidationError(
            message="tools/call requires string tool name",
            field="params.name",
            request_id=request_id,
        )
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, Mapping):
        raise ValidationError(
            message="tools/call arguments must be an object",
            field="params.arguments",
            request_id=request_id,
        )
    if not name:
        return
    schema = tool_input_schemas.get(name)
    if not schema or jsonschema is None:
        return
    try:
        jsonschema.validate(instance=dict(arguments), schema=dict(schema))
    except jsonschema.ValidationError as exc:  # type: ignore[union-attr]
        raise ValidationError(
            message=f"MCP tools/call arguments failed JSON Schema validation: {exc.message}",
            field="params.arguments",
            details={"tool": name, "validator": exc.validator},
            request_id=request_id,
        )
