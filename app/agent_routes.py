"""AgentAnywhere agent-edge routing.

Resolves incoming ``/agents/<slug>/...`` requests to a per-slug upstream base
URL, stripping the ``/agents/<slug>`` prefix so the upstream agent sees its own
root path (``/...``). Also carries the auth expectations for the slug:

* ``audience`` — the OIDC client id / OIDC SSO entitlement the token's
  ``agents`` claim must include (defaults to the slug).
* ``is_public`` — whether this specific path is in the slug's public allowlist
  (candidate/invite flows that must bypass SSO).

This module is pure/config-driven and holds no request state, so it is safe to
unit-test in isolation and to reuse from both the HTTP proxy and the WebSocket
relay.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

_AGENTS_PREFIX = "/agents/"


@dataclass(frozen=True)
class AgentRoute:
    """A resolved agent-edge route for a single request."""

    slug: str
    upstream_base: str      # upstream origin, no trailing slash, e.g. http://10.0.0.10:8001
    upstream_path: str      # prefix-stripped path for the upstream, always starts with "/"
    audience: str           # expected token audience / `agents` entitlement
    is_public: bool         # True → this path bypasses SSO (candidate/invite allowlist)

    @property
    def upstream_url(self) -> str:
        return f"{self.upstream_base}{self.upstream_path}"

    @property
    def upstream_ws_base(self) -> str:
        """Upstream origin as a WebSocket scheme (http->ws, https->wss)."""
        base = self.upstream_base
        if base.startswith("https://"):
            return "wss://" + base[len("https://"):]
        if base.startswith("http://"):
            return "ws://" + base[len("http://"):]
        return base

    def ws_upstream_uri(self, query: str = "") -> str:
        """Full upstream WebSocket URI (prefix-stripped path + optional query)."""
        uri = f"{self.upstream_ws_base}{self.upstream_path}"
        return f"{uri}?{query}" if query else uri


def parse_agent_path(path: str) -> Optional[Tuple[str, str]]:
    """Split ``/agents/<slug>/<rest>`` into ``(slug, "/<rest>")``.

    Returns ``None`` when ``path`` is not under ``/agents/`` or has no slug.
    ``/agents/<slug>`` (no trailing segment) yields ``(slug, "/")``.
    """
    if not path.startswith(_AGENTS_PREFIX):
        return None
    remainder = path[len(_AGENTS_PREFIX):]
    slug, _, rest = remainder.partition("/")
    if not slug:
        return None
    rest = f"/{rest}" if rest else "/"
    return slug, rest


def _load_json_map(raw: Optional[str]) -> Dict[str, str]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if v}


def _load_json_list_map(raw: Optional[str]) -> Dict[str, List[str]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for slug, prefixes in data.items():
        if isinstance(prefixes, (list, tuple)):
            out[str(slug)] = [str(p) for p in prefixes if p]
    return out


class AgentEdgeConfig:
    """Parsed, slug-indexed view of the agent-edge configuration."""

    def __init__(
        self,
        routes: Dict[str, str],
        public_paths: Optional[Dict[str, List[str]]] = None,
        audience_override: Optional[str] = None,
    ) -> None:
        self._routes = {k: v.rstrip("/") for k, v in routes.items()}
        self._public = public_paths or {}
        self._audience_override = audience_override or None
        # Optional per-slug audience overrides (from the dynamic registry). When a
        # slug is absent here we fall back to the global override, then the slug.
        self._audience_by_slug: Dict[str, str] = {}

    @classmethod
    def from_settings(cls, settings) -> "AgentEdgeConfig":
        return cls(
            routes=_load_json_map(getattr(settings, "agent_routes_json", None)),
            public_paths=_load_json_list_map(getattr(settings, "agent_public_paths_json", None)),
            audience_override=getattr(settings, "ida_audience", None),
        )

    def replace(
        self,
        routes: Dict[str, str],
        public_paths: Optional[Dict[str, List[str]]] = None,
        audience_by_slug: Optional[Dict[str, str]] = None,
    ) -> None:
        """Atomically swap the live routing tables (hot-reload).

        Assigning the freshly-built dicts in one shot means in-flight
        ``resolve()`` calls always observe a fully-consistent table — never a
        half-updated one — under CPython's GIL. Used by the DB-backed registry
        provider so admin edits take effect without a process restart.
        """
        self._routes = {k: v.rstrip("/") for k, v in routes.items() if v}
        self._public = public_paths or {}
        self._audience_by_slug = {k: v for k, v in (audience_by_slug or {}).items() if v}

    def has_routes(self) -> bool:
        return bool(self._routes)

    def snapshot(self) -> Dict[str, object]:
        """Return the current live tables (for export / admin display)."""
        return {
            "routes": dict(self._routes),
            "public_paths": {k: list(v) for k, v in self._public.items()},
        }

    @property
    def slugs(self) -> List[str]:
        return sorted(self._routes.keys())

    def audience_for(self, slug: str) -> str:
        return self._audience_by_slug.get(slug) or self._audience_override or slug

    def is_public_path(self, slug: str, upstream_path: str) -> bool:
        for prefix in self._public.get(slug, []):
            if not prefix:
                continue
            trimmed = prefix.rstrip("/")
            if upstream_path == prefix or upstream_path == trimmed:
                return True
            if upstream_path.startswith(trimmed + "/"):
                return True
        return False

    def resolve(self, path: str) -> Optional[AgentRoute]:
        """Resolve ``path`` to an :class:`AgentRoute`, or ``None`` if not an
        agent path or the slug is unknown."""
        parsed = parse_agent_path(path)
        if not parsed:
            return None
        slug, rest = parsed
        base = self._routes.get(slug)
        if not base:
            return None
        return AgentRoute(
            slug=slug,
            upstream_base=base,
            upstream_path=rest,
            audience=self.audience_for(slug),
            is_public=self.is_public_path(slug, rest),
        )
