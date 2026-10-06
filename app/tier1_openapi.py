"""OpenAPI 3.x parsing for Tier-1 MCP tool and A2A skill registration.

Maps operations to MCP-style tool descriptors and A2A AgentSkill objects
without importing a full OpenAPI stack — sufficient for gateway registration
and schema validation at the MCP boundary.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, MutableMapping, Tuple


def validate_openapi_document(doc: Mapping[str, Any]) -> None:
    """Raise ValueError if the payload is not a minimal OpenAPI 3.x document."""
    if not isinstance(doc, Mapping):
        raise ValueError("OpenAPI document must be a JSON object")
    ver = doc.get("openapi")
    if not ver:
        raise ValueError("Missing 'openapi' field (OpenAPI 3.x required)")
    if not str(ver).startswith("3."):
        raise ValueError(f"Unsupported OpenAPI version: {ver!r} (need 3.x)")
    paths = doc.get("paths")
    if paths is None or not isinstance(paths, Mapping):
        raise ValueError("OpenAPI document must contain a 'paths' object")


def _resolve_ref(
    node: Any,
    doc_root: Mapping[str, Any],
    depth: int = 0,
) -> Any:
    if depth > 32:
        raise ValueError("$ref nesting too deep")
    if isinstance(node, MutableMapping) and "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ValueError(f"Unsupported $ref: {ref!r}")
        parts = ref[2:].split("/")
        cur: Any = doc_root
        for p in parts:
            if not isinstance(cur, Mapping) or p not in cur:
                raise ValueError(f"Cannot resolve $ref {ref!r}")
            cur = cur[p]
        return _resolve_ref(cur, doc_root, depth + 1)
    if isinstance(node, MutableMapping):
        return {k: _resolve_ref(v, doc_root, depth + 1) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_ref(i, doc_root, depth + 1) for i in node]
    return node


def _slug_tool_name(method: str, path_pattern: str, op: Mapping[str, Any]) -> str:
    op_id = op.get("operationId")
    if isinstance(op_id, str) and op_id.strip():
        slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", op_id.strip())
        return slug[:128]
    p = path_pattern.strip("/").replace("/", "_")
    p = re.sub(r"[\{\}]", "", p)
    return f"{method.lower()}_{p}"[:128]


def _merge_parameters(op: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    params: List[Mapping[str, Any]] = []
    seen = set()
    for group in (op.get("parameters") or []):
        if not isinstance(group, Mapping):
            continue
        key = (group.get("name"), group.get("in"))
        if key in seen:
            continue
        seen.add(key)
        params.append(group)
    return params


def _operation_input_schema(
    doc: Mapping[str, Any],
    method: str,
    path_pattern: str,
    op: Mapping[str, Any],
) -> Dict[str, Any]:
    components = doc.get("components") or {}
    props: Dict[str, Any] = {}
    required: List[str] = []

    for param in _merge_parameters(op):
        name = param.get("name")
        if not name or param.get("in") not in ("query", "path", "header"):
            continue
        schema = param.get("schema") or {"type": "string"}
        try:
            props[name] = _resolve_ref(schema, doc)
        except ValueError:
            props[name] = schema
        if param.get("required"):
            required.append(str(name))

    body = op.get("requestBody")
    if isinstance(body, Mapping):
        content = body.get("content") or {}
        for ct, cobj in content.items():
            if "json" not in ct.lower():
                continue
            if isinstance(cobj, Mapping) and "schema" in cobj:
                try:
                    return _resolve_ref(cobj["schema"], doc)
                except ValueError:
                    pass
                return cobj["schema"] if isinstance(cobj["schema"], dict) else {"type": "object"}
            break

    if not props:
        return {"type": "object", "additionalProperties": True}
    sch: Dict[str, Any] = {
        "type": "object",
        "properties": props,
    }
    if required:
        sch["required"] = required
    return sch


def registration_to_mcp_tools(reg: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Build MCP tool descriptors (+ gateway routing hints) for one registration."""
    doc = reg["openapi_document"]
    if isinstance(doc, str):
        raise ValueError("openapi_document must be an object (not a string)")
    validate_openapi_document(doc)
    reg_id = reg["id"]
    base = str(reg["upstream_base_url"]).rstrip("/")
    prefix = str(reg.get("path_prefix") or "").strip().rstrip("/")
    tools: List[Dict[str, Any]] = []
    paths = doc.get("paths") or {}
    for path_pattern, path_item in paths.items():
        if not isinstance(path_item, Mapping):
            continue
        for method in ("get", "post", "put", "patch", "delete"):
            if method not in path_item:
                continue
            op = path_item[method]
            if not isinstance(op, Mapping):
                continue
            name = _slug_tool_name(method, path_pattern, op)
            desc = (op.get("summary") or op.get("description") or "").strip()
            if not desc:
                desc = f"{method.upper()} {path_pattern}"
            input_schema = _operation_input_schema(doc, method, path_pattern, op)
            rel_path = path_pattern if path_pattern.startswith("/") else f"/{path_pattern}"
            if prefix:
                rel_path = f"/{prefix.strip('/')}{rel_path}"
            tools.append(
                {
                    "name": name,
                    "description": desc,
                    "inputSchema": input_schema,
                    "_gateway": {
                        "registration_id": reg_id,
                        "registration_name": reg.get("name"),
                        "http": {
                            "method": method.upper(),
                            "path": rel_path,
                            "base_url": base,
                        },
                    },
                }
            )
    return tools


def registration_to_a2a_skills(reg: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Build A2A AgentSkill entries from OpenAPI operations."""
    doc = reg["openapi_document"]
    validate_openapi_document(doc)
    reg_name = reg.get("name") or "service"
    skills: List[Dict[str, Any]] = []
    path_objs: List[Tuple[str, str, Mapping[str, Any]]] = []
    paths = doc.get("paths") or {}
    for path_pattern, path_item in paths.items():
        if not isinstance(path_item, Mapping):
            continue
        for method in ("get", "post", "put", "patch", "delete"):
            if method not in path_item:
                continue
            op = path_item[method]
            if isinstance(op, Mapping):
                path_objs.append((method, path_pattern, op))
    for idx, (method, path_pattern, op) in enumerate(path_objs):
        skill_id = f"{reg_name}.{_slug_tool_name(method, path_pattern, op)}"
        desc = (op.get("summary") or op.get("description") or "").strip()
        if not desc:
            desc = f"{method.upper()} {path_pattern}"
        tags = sorted(
            {t for t in [method.upper(), reg_name, "openapi"] if t}
        )
        examples: List[str] = []
        summary = (op.get("summary") or "").strip()
        if summary:
            examples.append(summary)
        skills.append(
            {
                "id": skill_id[:256],
                "name": (_slug_tool_name(method, path_pattern, op))[:128],
                "description": desc[:4096],
                "tags": tags,
                "examples": examples[:5],
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            }
        )
        if idx > 500:
            break
    return skills
