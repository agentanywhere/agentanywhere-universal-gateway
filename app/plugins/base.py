"""Plugin base classes.

Every plugin shipped or installed into the gateway is a subclass of
:class:`Plugin`. Specialized base classes provide the contracts for the
three first-class extension surfaces:

* :class:`TransformPlugin`  — request / response rewriting.
* :class:`AuthProviderPlugin` — pluggable authentication backends.
* :class:`ProtocolPlugin` — custom protocol handlers (e.g. AMQP, NATS).
* :class:`HookPlugin` — arbitrary lifecycle / request / response hooks.

Plugins are intentionally lightweight: they do not have to own server
state; the gateway owns the registry, lifecycle, and dispatch.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


class PluginType(str, enum.Enum):
    TRANSFORM = "transform"
    AUTH_PROVIDER = "auth_provider"
    PROTOCOL = "protocol"
    HOOK = "hook"
    GENERIC = "generic"


@dataclass
class PluginMetadata:
    """Static metadata describing a plugin.

    A plugin must set at minimum ``name`` and ``version``. Everything
    else is optional and used by the admin UI / CLI for display.
    """

    name: str
    version: str = "0.0.0"
    description: str = ""
    author: str = ""
    homepage: str = ""
    plugin_type: PluginType = PluginType.GENERIC
    tags: List[str] = field(default_factory=list)
    config_schema: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "homepage": self.homepage,
            "plugin_type": self.plugin_type.value,
            "tags": list(self.tags),
            "config_schema": self.config_schema,
        }


@dataclass
class PluginContext:
    """Per-request context passed to plugin hooks.

    Plugins should treat the fields as read-only inputs and return
    transformed copies rather than mutating the dict in place. The
    gateway will merge results before continuing the pipeline.
    """

    method: str = ""
    path: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    query_params: Dict[str, Any] = field(default_factory=dict)
    body: Optional[Any] = None
    status_code: Optional[int] = None
    request_id: Optional[str] = None
    api_key: Optional[str] = None
    tenant_id: Optional[str] = None
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "headers": dict(self.headers),
            "query_params": dict(self.query_params),
            "body": self.body,
            "status_code": self.status_code,
            "request_id": self.request_id,
            "api_key": self.api_key,
            "tenant_id": self.tenant_id,
            "extras": dict(self.extras),
        }


class Plugin:
    """Base class for all plugins.

    Subclasses MUST set the ``metadata`` class attribute. The gateway
    instantiates plugins lazily (one instance per registration) and
    calls :meth:`setup` once the registry is ready. Plugins receive a
    free-form dict ``config`` from the gateway settings.
    """

    metadata: PluginMetadata = PluginMetadata(name="unnamed")

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config: Dict[str, Any] = config or {}
        self.enabled: bool = True

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def version(self) -> str:
        return self.metadata.version

    @property
    def plugin_type(self) -> PluginType:
        return self.metadata.plugin_type

    def setup(self) -> None:
        """Called once when the plugin is registered. Override to
        initialise resources, open connections, etc."""

    def teardown(self) -> None:
        """Called when the plugin is unregistered or the gateway
        shuts down. Override to release resources."""

    def health(self) -> Dict[str, Any]:
        """Return a health snapshot for the admin API."""
        return {
            "name": self.name,
            "version": self.version,
            "type": self.plugin_type.value,
            "enabled": self.enabled,
            "status": "ok",
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<{type(self).__name__} {self.name} v{self.version}>"


class TransformPlugin(Plugin):
    """Plugin that rewrites requests and / or responses.

    Override :meth:`transform_request` to rewrite outbound requests to
    the backend, and :meth:`transform_response` to rewrite responses
    before they are returned to the client. Returning ``None`` leaves
    the value untouched.
    """

    metadata = PluginMetadata(name="unnamed", plugin_type=PluginType.TRANSFORM)

    def transform_request(self, ctx: PluginContext) -> Optional[PluginContext]:
        return None

    def transform_response(self, ctx: PluginContext) -> Optional[PluginContext]:
        return None


class AuthProviderPlugin(Plugin):
    """Plugin that authenticates requests.

    The gateway calls :meth:`authenticate` with the request context
    before applying the built-in API key / JWT validators. Returning a
    truthy dict short-circuits authentication; the dict is attached to
    ``request.state.plugin_auth`` for downstream consumers.
    """

    metadata = PluginMetadata(name="unnamed", plugin_type=PluginType.AUTH_PROVIDER)

    def authenticate(self, ctx: PluginContext) -> Optional[Dict[str, Any]]:
        return None


class ProtocolPlugin(Plugin):
    """Plugin that handles a custom wire protocol.

    Implement :meth:`handle` to process a request and return a dict
    payload (which the gateway will JSON-encode) or ``None`` to defer
    to the next handler. The plugin must declare which path prefix or
    scheme it claims via :meth:`matches`.
    """

    metadata = PluginMetadata(name="unnamed", plugin_type=PluginType.PROTOCOL)

    #: Path prefix this protocol claims, e.g. ``/amqp/`` or ``/nats/``.
    path_prefix: str = ""

    def matches(self, path: str) -> bool:
        if not self.path_prefix:
            return False
        return path.startswith(self.path_prefix)

    def handle(self, ctx: PluginContext) -> Optional[Dict[str, Any]]:
        return None


HookCallable = Callable[[PluginContext], Optional[PluginContext]]
AsyncHookCallable = Callable[[PluginContext], Awaitable[Optional[PluginContext]]]


class HookPlugin(Plugin):
    """Plugin that registers arbitrary lifecycle hooks.

    The default implementation does nothing; override :meth:`hooks` to
    return a mapping of hook name -> callable. Built-in hook names are:

    * ``startup`` / ``shutdown`` — called once during app lifespan.
    * ``request_pre`` / ``request_post`` — called around each request.
    * ``response_pre`` / ``response_post`` — called around each response.
    """

    metadata = PluginMetadata(name="unnamed", plugin_type=PluginType.HOOK)

    def hooks(self) -> Dict[str, HookCallable]:
        return {}
