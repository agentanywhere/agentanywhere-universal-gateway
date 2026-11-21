"""Request/Response transformation engine.

Supports two ways to define transformations:

1. Declarative config-based rules (recommended) — JSON-serializable
   dictionaries describing what to do to headers/query/body/status. Easy
   to manage from the admin API.

2. Imperative `TransformationRule` objects with Python callables — kept
   for backwards compatibility and advanced use cases.

Rules are matched against an incoming request path using glob-style
patterns (e.g. ``/api/users/*``) and can be filtered by HTTP method.
"""
from __future__ import annotations

import copy
import fnmatch
import functools
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import structlog
from jinja2 import Environment, select_autoescape

logger = structlog.get_logger()


jinja_env = Environment(
    autoescape=select_autoescape(["html", "xml"]),
    trim_blocks=True,
    lstrip_blocks=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=2048)
def _glob_to_regex(pattern: str) -> re.Pattern:
    """Convert a glob path pattern (``*`` / ``**``) to a compiled regex.

    Cached per pattern: compiling on every request for every rule is wasted work.
    """
    if pattern.startswith("^") or pattern.endswith("$"):
        return re.compile(pattern)
    return re.compile(fnmatch.translate(pattern))


def _coerce_body_to_dict(body: Any) -> Optional[Dict[str, Any]]:
    """Best-effort decode a body to a dict; returns None if not possible."""
    if body is None:
        return None
    if isinstance(body, dict):
        return body
    if isinstance(body, (bytes, bytearray)):
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return None
    if isinstance(body, str):
        try:
            return json.loads(body)
        except Exception:
            return None
    return None


def _set_nested(d: Dict[str, Any], path: str, value: Any) -> None:
    """Set a value at a dotted path, creating intermediate dicts as needed."""
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def _remove_nested(d: Dict[str, Any], path: str) -> None:
    """Remove a value at a dotted path; no-op if missing."""
    parts = path.split(".")
    cur = d
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return
        cur = cur[part]
    if isinstance(cur, dict):
        cur.pop(parts[-1], None)


def _get_nested(d: Dict[str, Any], path: str) -> Any:
    parts = path.split(".")
    cur: Any = d
    for part in parts:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


# ---------------------------------------------------------------------------
# Declarative rule config
# ---------------------------------------------------------------------------


@dataclass
class TransformConfig:
    """Declarative transformation config.

    All fields are optional and combined in order:

      headers_set:   add or replace headers, e.g. ``{"X-Foo": "bar"}``
      headers_remove: list of header names to drop
      headers_rename: ``{"X-Old": "X-New"}``
      query_set / query_remove: query parameter manipulation
      body_set:    ``{"path.to.field": value}``
      body_remove: list of dotted paths
      body_rename: ``{"old.path": "new.path"}``
      status_map:  ``{"500": 502}`` to remap response status codes
      path_rewrite: ``{"pattern": regex, "replacement": str}``
      template:    Jinja2 template applied to the body
    """

    headers_set: Dict[str, str] = field(default_factory=dict)
    headers_remove: List[str] = field(default_factory=list)
    headers_rename: Dict[str, str] = field(default_factory=dict)
    query_set: Dict[str, str] = field(default_factory=dict)
    query_remove: List[str] = field(default_factory=list)
    body_set: Dict[str, Any] = field(default_factory=dict)
    body_remove: List[str] = field(default_factory=list)
    body_rename: Dict[str, str] = field(default_factory=dict)
    status_map: Dict[str, int] = field(default_factory=dict)
    path_rewrite: Optional[Dict[str, str]] = None
    template: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransformConfig":
        return cls(
            headers_set=data.get("headers_set") or {},
            headers_remove=list(data.get("headers_remove") or []),
            headers_rename=data.get("headers_rename") or {},
            query_set=data.get("query_set") or {},
            query_remove=list(data.get("query_remove") or []),
            body_set=data.get("body_set") or {},
            body_remove=list(data.get("body_remove") or []),
            body_rename=data.get("body_rename") or {},
            status_map={str(k): int(v) for k, v in (data.get("status_map") or {}).items()},
            path_rewrite=data.get("path_rewrite"),
            template=data.get("template"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "headers_set": dict(self.headers_set),
            "headers_remove": list(self.headers_remove),
            "headers_rename": dict(self.headers_rename),
            "query_set": dict(self.query_set),
            "query_remove": list(self.query_remove),
            "body_set": dict(self.body_set),
            "body_remove": list(self.body_remove),
            "body_rename": dict(self.body_rename),
            "status_map": {str(k): int(v) for k, v in self.status_map.items()},
            "path_rewrite": dict(self.path_rewrite) if self.path_rewrite else None,
            "template": self.template,
        }


@dataclass
class TransformationRule:
    """A transformation rule.

    Either provide a declarative ``request_config`` / ``response_config``
    (recommended) or imperative ``transform_request`` / ``transform_response``
    callables.
    """

    name: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    path_pattern: str = "*"
    methods: List[str] = field(default_factory=list)  # empty = all methods
    enabled: bool = True
    request_config: Optional[TransformConfig] = None
    response_config: Optional[TransformConfig] = None
    condition: Optional[Callable[[Dict[str, Any]], bool]] = None
    transform_request: Optional[Callable] = None
    transform_response: Optional[Callable] = None
    template: Optional[str] = None

    def matches(self, context: Dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        path = context.get("path", "") or ""
        method = (context.get("method") or "").upper()
        if self.methods and method and method not in {m.upper() for m in self.methods}:
            return False
        if self.path_pattern not in ("*", "**"):
            try:
                if not _glob_to_regex(self.path_pattern).match(path):
                    return False
            except re.error:
                if self.path_pattern not in path:
                    return False
        if self.condition is not None:
            try:
                return bool(self.condition(context))
            except Exception as e:
                logger.warning("transformation_condition_error", rule=self.name, error=str(e))
                return False
        return True

    def changes_response_body(self) -> bool:
        """Whether applying the rule to an answer can change its body (rather than only its
        headers or status), which is when the body has to be read."""
        cfg = self.response_config
        return self.transform_response is not None or (
            cfg is not None and bool(cfg.body_set or cfg.body_remove or cfg.body_rename or cfg.template)
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path_pattern": self.path_pattern,
            "methods": list(self.methods),
            "enabled": self.enabled,
            "request_config": self.request_config.to_dict() if self.request_config else None,
            "response_config": self.response_config.to_dict() if self.response_config else None,
            "has_request_transform": self.transform_request is not None or self.request_config is not None,
            "has_response_transform": self.transform_response is not None or self.response_config is not None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransformationRule":
        req_cfg = data.get("request_config")
        resp_cfg = data.get("response_config")
        return cls(
            name=data["name"],
            id=data.get("id") or uuid.uuid4().hex[:12],
            path_pattern=data.get("path_pattern", "*"),
            methods=list(data.get("methods") or []),
            enabled=bool(data.get("enabled", True)),
            request_config=TransformConfig.from_dict(req_cfg) if req_cfg else None,
            response_config=TransformConfig.from_dict(resp_cfg) if resp_cfg else None,
        )


# ---------------------------------------------------------------------------
# Declarative apply functions
# ---------------------------------------------------------------------------


def _apply_header_ops(headers: Dict[str, str], cfg: TransformConfig) -> Dict[str, str]:
    out = dict(headers)
    # Renames first (case-insensitive lookup).
    if cfg.headers_rename:
        lower_map = {k.lower(): k for k in out.keys()}
        for old, new in cfg.headers_rename.items():
            existing = lower_map.get(old.lower())
            if existing is not None:
                out[new] = out.pop(existing)
    if cfg.headers_remove:
        rm = {h.lower() for h in cfg.headers_remove}
        out = {k: v for k, v in out.items() if k.lower() not in rm}
    if cfg.headers_set:
        out.update(cfg.headers_set)
    return out


def _apply_query_ops(query: Dict[str, Any], cfg: TransformConfig) -> Dict[str, Any]:
    out = dict(query)
    if cfg.query_remove:
        for q in cfg.query_remove:
            out.pop(q, None)
    if cfg.query_set:
        out.update(cfg.query_set)
    return out


def _apply_body_ops(body: Any, cfg: TransformConfig) -> Any:
    """Apply body operations.

    The field operations (set, remove, rename) apply to a JSON object. Any other body, JSON or
    not, is left as it came, unless a template is to be filled in from it.
    """
    parsed = _coerce_body_to_dict(body)

    has_body_ops = bool(cfg.body_set or cfg.body_remove or cfg.body_rename)
    has_template = bool(cfg.template)

    if not has_body_ops and not has_template:
        return body

    if parsed is None:
        if has_template:
            try:
                return jinja_env.from_string(cfg.template).render(body=body)
            except Exception as e:
                logger.error("body_template_render_error", error=str(e))
                return body
        return body

    if isinstance(parsed, dict):
        if cfg.body_rename:
            for old, new in cfg.body_rename.items():
                val = _get_nested(parsed, old)
                if val is not None:
                    _remove_nested(parsed, old)
                    _set_nested(parsed, new, val)
        if cfg.body_remove:
            for path in cfg.body_remove:
                _remove_nested(parsed, path)
        if cfg.body_set:
            for path, val in cfg.body_set.items():
                # A copy, so that nothing a later operation puts inside it stays in the rule.
                _set_nested(parsed, path, copy.deepcopy(val))
    elif not has_template:
        return body

    if has_template:
        try:
            rendered = jinja_env.from_string(cfg.template).render(body=parsed)
            try:
                return json.loads(rendered)
            except Exception:
                return rendered
        except Exception as e:
            logger.error("body_template_render_error", error=str(e))
            return parsed

    return parsed


def _apply_path_rewrite(path: str, cfg: TransformConfig) -> str:
    if not cfg.path_rewrite:
        return path
    pattern = cfg.path_rewrite.get("pattern")
    replacement = cfg.path_rewrite.get("replacement", "")
    if not pattern:
        return path
    try:
        return re.sub(pattern, replacement, path)
    except re.error as e:
        logger.warning("path_rewrite_regex_error", pattern=pattern, error=str(e))
        return path


# ---------------------------------------------------------------------------
# Transformer classes
# ---------------------------------------------------------------------------


class RequestTransformer:
    """Transforms incoming requests (legacy imperative rules)."""

    def __init__(self):
        self.rules: List[TransformationRule] = []

    def add_rule(self, rule: TransformationRule):
        self.rules.append(rule)
        logger.info("transformation_rule_added", rule=rule.name)

    def transform(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        query_params: Dict[str, Any],
        body: Optional[Any] = None,
    ) -> Tuple[Dict[str, str], Dict[str, Any], Optional[Any], str]:
        context = {
            "method": method,
            "path": path,
            "headers": headers,
            "query_params": query_params,
            "body": body,
        }
        h = dict(headers)
        q = dict(query_params)
        b = body
        p = path
        for rule in self.rules:
            if not rule.matches(context):
                continue
            if rule.request_config is not None:
                cfg = rule.request_config
                h = _apply_header_ops(h, cfg)
                q = _apply_query_ops(q, cfg)
                b = _apply_body_ops(b, cfg)
                p = _apply_path_rewrite(p, cfg)
            if rule.transform_request:
                try:
                    result = rule.transform_request(method, p, h, q, b, context)
                    if result:
                        if len(result) == 3:
                            h, q, b = result
                        elif len(result) == 4:
                            h, q, b, p = result
                except Exception as e:
                    logger.error("transformation_error", rule=rule.name, error=str(e))
        return h, q, b, p

    def transform_headers(self, headers: Dict[str, str], template: str) -> Dict[str, str]:
        try:
            template_obj = jinja_env.from_string(template)
            result = template_obj.render(headers=headers)
            try:
                return json.loads(result)
            except Exception:
                return headers
        except Exception as e:
            logger.error("header_transformation_error", error=str(e))
            return headers

    def transform_body(self, body: Any, template: str) -> Any:
        try:
            if isinstance(body, bytes):
                body_str = body.decode("utf-8")
            elif isinstance(body, dict):
                body_str = json.dumps(body)
            else:
                body_str = str(body)
            template_obj = jinja_env.from_string(template)
            result = template_obj.render(
                body=body_str,
                body_json=json.loads(body_str) if isinstance(body, (dict, str)) else {},
            )
            try:
                return json.loads(result)
            except Exception:
                return result
        except Exception as e:
            logger.error("body_transformation_error", error=str(e))
            return body


class ResponseTransformer:
    """Transforms outgoing responses (legacy imperative rules)."""

    def __init__(self):
        self.rules: List[TransformationRule] = []

    def add_rule(self, rule: TransformationRule):
        self.rules.append(rule)
        logger.info("transformation_rule_added", rule=rule.name)

    def transform(
        self,
        status_code: int,
        headers: Dict[str, str],
        body: Optional[Any] = None,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, str], Optional[Any]]:
        context = {
            "status_code": status_code,
            "headers": headers,
            "body": body,
            "request": request_context or {},
            "method": (request_context or {}).get("method", ""),
            "path": (request_context or {}).get("path", ""),
        }

        s = status_code
        h = dict(headers)
        b = body

        # Always strip Content-Length (Starlette will recompute).
        h.pop("Content-Length", None)
        h.pop("content-length", None)

        for rule in self.rules:
            if not rule.matches(context):
                continue
            if rule.response_config is not None:
                cfg = rule.response_config
                h = _apply_header_ops(h, cfg)
                b = _apply_body_ops(b, cfg)
                if cfg.status_map and str(s) in cfg.status_map:
                    s = cfg.status_map[str(s)]
            if rule.transform_response:
                try:
                    result = rule.transform_response(s, h, b, context)
                    if result:
                        s, h, b = result
                except Exception as e:
                    logger.error("transformation_error", rule=rule.name, error=str(e))

            h.pop("Content-Length", None)
            h.pop("content-length", None)

        return s, h, b


class TransformationManager:
    """Manages request and response transformations."""

    def __init__(self):
        self.request_transformer = RequestTransformer()
        self.response_transformer = ResponseTransformer()
        self.rules_by_path: Dict[str, List[TransformationRule]] = {}
        self.rules_by_id: Dict[str, TransformationRule] = {}

    # ---- declarative API --------------------------------------------------

    def register_rule(self, rule: TransformationRule) -> TransformationRule:
        """Register a rule (declarative or imperative)."""
        self.rules_by_path.setdefault(rule.path_pattern, []).append(rule)
        self.rules_by_id[rule.id] = rule
        logger.info(
            "transformation_rule_registered",
            id=rule.id,
            name=rule.name,
            path=rule.path_pattern,
            methods=rule.methods,
        )
        return rule

    def register_from_dict(self, data: Dict[str, Any]) -> TransformationRule:
        rule = TransformationRule.from_dict(data)
        return self.register_rule(rule)

    def remove_rule(self, rule_id: str) -> bool:
        rule = self.rules_by_id.pop(rule_id, None)
        if not rule:
            return False
        bucket = self.rules_by_path.get(rule.path_pattern, [])
        self.rules_by_path[rule.path_pattern] = [r for r in bucket if r.id != rule_id]
        if not self.rules_by_path[rule.path_pattern]:
            del self.rules_by_path[rule.path_pattern]
        logger.info("transformation_rule_removed", id=rule_id, name=rule.name)
        return True

    def list_rules(self) -> List[TransformationRule]:
        return list(self.rules_by_id.values())

    def get_rule(self, rule_id: str) -> Optional[TransformationRule]:
        return self.rules_by_id.get(rule_id)

    # ---- legacy imperative API -------------------------------------------

    def add_rule(self, path_pattern: str, rule: TransformationRule):
        """Add a transformation rule for a specific path pattern (legacy)."""
        rule.path_pattern = path_pattern
        self.register_rule(rule)

    def get_rules_for_path(self, path: str, method: Optional[str] = None) -> List[TransformationRule]:
        """Get transformation rules that match a path (and optional method)."""
        out: List[TransformationRule] = []
        ctx = {"path": path, "method": (method or "").upper()}
        for rule in self.rules_by_id.values():
            if rule.matches(ctx):
                out.append(rule)
        return out

    # ---- transform entrypoints -------------------------------------------

    def transform_request(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        query_params: Dict[str, Any],
        body: Optional[Any] = None,
    ) -> Tuple[Dict[str, str], Dict[str, Any], Optional[Any], str]:
        """Transform a request, returning (headers, query, body, path)."""
        applicable = self.get_rules_for_path(path, method)
        original = self.request_transformer.rules
        self.request_transformer.rules = applicable
        try:
            return self.request_transformer.transform(method, path, headers, query_params, body)
        finally:
            self.request_transformer.rules = original

    def transform_response(
        self,
        status_code: int,
        headers: Dict[str, str],
        body: Optional[Any] = None,
        request_context: Optional[Dict[str, Any]] = None,
    ) -> Tuple[int, Dict[str, str], Optional[Any]]:
        """Transform a response."""
        applicable = self.get_rules_for_path(
            (request_context or {}).get("path", ""),
            (request_context or {}).get("method"),
        )
        original = self.response_transformer.rules
        self.response_transformer.rules = applicable
        try:
            return self.response_transformer.transform(status_code, headers, body, request_context)
        finally:
            self.response_transformer.rules = original


transformation_manager = TransformationManager()


# ---------------------------------------------------------------------------
# Built-in helpers
# ---------------------------------------------------------------------------


def create_version_transformation(from_version: str, to_version: str) -> TransformationRule:
    """Create a transformation rule that rewrites a path from one API
    version prefix to another and tags the request with version headers."""
    return TransformationRule(
        name=f"version_{from_version}_to_{to_version}",
        path_pattern=f"*/{from_version}/*",
        request_config=TransformConfig(
            headers_set={
                "X-API-Version": to_version,
                "X-Original-Version": from_version,
            },
            path_rewrite={
                "pattern": rf"/{re.escape(from_version)}/",
                "replacement": f"/{to_version}/",
            },
        ),
    )


def create_header_enrichment_transformation(headers_to_add: Dict[str, str]) -> TransformationRule:
    """Create a transformation rule that adds headers to every matching request."""
    return TransformationRule(
        name="header_enrichment",
        request_config=TransformConfig(headers_set=dict(headers_to_add)),
    )
