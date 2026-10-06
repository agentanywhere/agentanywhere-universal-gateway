"""Plugin registry and runtime dispatch.

The registry is a process-wide singleton that owns all loaded plugins
and exposes a small set of hooks the gateway invokes:

* :meth:`PluginRegistry.transform_request` / ``transform_response``
* :meth:`PluginRegistry.authenticate`
* :meth:`PluginRegistry.dispatch_protocol`
* :meth:`PluginRegistry.run_hook`

Errors in a single plugin must NEVER crash the request: hooks are
guarded and individual failures are logged and recorded against the
plugin so operators can debug via the admin API.
"""
from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Dict, Iterable, List, Optional

import structlog

from app.plugins.base import (
    AuthProviderPlugin,
    HookPlugin,
    Plugin,
    PluginContext,
    PluginType,
    ProtocolPlugin,
    TransformPlugin,
)

logger = structlog.get_logger()


class PluginError(Exception):
    """Raised when a plugin operation fails in a non-recoverable way."""


class PluginRegistry:
    """Thread-safe registry of plugins."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._plugins: Dict[str, Plugin] = {}
        self._stats: Dict[str, Dict[str, Any]] = {}
        self._listeners: Dict[str, List[Plugin]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def register(self, plugin: Plugin, *, replace: bool = False) -> Plugin:
        """Register a plugin instance. Calls ``setup`` exactly once."""
        with self._lock:
            existing = self._plugins.get(plugin.name)
            if existing is not None and not replace:
                raise PluginError(
                    f"Plugin '{plugin.name}' already registered (use replace=True to override)"
                )
            if existing is not None:
                self._teardown(existing)
            try:
                plugin.setup()
            except Exception as exc:
                logger.error(
                    "plugin_setup_failed",
                    plugin=plugin.name,
                    error=str(exc),
                    traceback=traceback.format_exc(limit=5),
                )
                raise PluginError(f"setup failed for plugin '{plugin.name}': {exc}") from exc
            self._plugins[plugin.name] = plugin
            self._stats[plugin.name] = {
                "calls": 0,
                "errors": 0,
                "last_error": None,
                "last_called_at": None,
                "registered_at": time.time(),
            }
            logger.info(
                "plugin_registered",
                plugin=plugin.name,
                version=plugin.version,
                type=plugin.plugin_type.value,
            )
            return plugin

    def unregister(self, name: str) -> bool:
        """Unregister a plugin by name. Returns True if removed."""
        with self._lock:
            plugin = self._plugins.pop(name, None)
            if plugin is None:
                return False
            self._teardown(plugin)
            self._stats.pop(name, None)
            logger.info("plugin_unregistered", plugin=name)
            return True

    def clear(self) -> None:
        """Tear down and remove every plugin (used in tests)."""
        with self._lock:
            for name in list(self._plugins.keys()):
                self.unregister(name)

    def _teardown(self, plugin: Plugin) -> None:
        try:
            plugin.teardown()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("plugin_teardown_failed", plugin=plugin.name, error=str(exc))

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def list(self) -> List[Plugin]:
        with self._lock:
            return list(self._plugins.values())

    def get(self, name: str) -> Optional[Plugin]:
        with self._lock:
            return self._plugins.get(name)

    def by_type(self, plugin_type: PluginType) -> List[Plugin]:
        with self._lock:
            return [p for p in self._plugins.values() if p.plugin_type == plugin_type]

    def stats(self, name: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            if name is not None:
                return dict(self._stats.get(name, {}))
            return {n: dict(s) for n, s in self._stats.items()}

    def set_enabled(self, name: str, enabled: bool) -> bool:
        with self._lock:
            plugin = self._plugins.get(name)
            if plugin is None:
                return False
            plugin.enabled = bool(enabled)
            logger.info("plugin_enabled_changed", plugin=name, enabled=plugin.enabled)
            return True

    # ------------------------------------------------------------------
    # Dispatch helpers
    # ------------------------------------------------------------------

    def _record(self, name: str, error: Optional[Exception] = None) -> None:
        stats = self._stats.setdefault(
            name,
            {"calls": 0, "errors": 0, "last_error": None, "last_called_at": None, "registered_at": time.time()},
        )
        stats["calls"] += 1
        stats["last_called_at"] = time.time()
        if error is not None:
            stats["errors"] += 1
            stats["last_error"] = str(error)

    def transform_request(self, ctx: PluginContext) -> PluginContext:
        for plugin in self.by_type(PluginType.TRANSFORM):
            if not plugin.enabled or not isinstance(plugin, TransformPlugin):
                continue
            try:
                result = plugin.transform_request(ctx)
                self._record(plugin.name)
                if result is not None:
                    ctx = result
            except Exception as exc:
                self._record(plugin.name, exc)
                logger.error(
                    "plugin_transform_request_error",
                    plugin=plugin.name,
                    error=str(exc),
                )
        return ctx

    def transform_response(self, ctx: PluginContext) -> PluginContext:
        for plugin in self.by_type(PluginType.TRANSFORM):
            if not plugin.enabled or not isinstance(plugin, TransformPlugin):
                continue
            try:
                result = plugin.transform_response(ctx)
                self._record(plugin.name)
                if result is not None:
                    ctx = result
            except Exception as exc:
                self._record(plugin.name, exc)
                logger.error(
                    "plugin_transform_response_error",
                    plugin=plugin.name,
                    error=str(exc),
                )
        return ctx

    def authenticate(self, ctx: PluginContext) -> Optional[Dict[str, Any]]:
        """Run auth providers; returns first non-None result."""
        for plugin in self.by_type(PluginType.AUTH_PROVIDER):
            if not plugin.enabled or not isinstance(plugin, AuthProviderPlugin):
                continue
            try:
                result = plugin.authenticate(ctx)
                self._record(plugin.name)
                if result:
                    return result
            except Exception as exc:
                self._record(plugin.name, exc)
                logger.error("plugin_auth_error", plugin=plugin.name, error=str(exc))
        return None

    def dispatch_protocol(self, ctx: PluginContext) -> Optional[Dict[str, Any]]:
        """Find and invoke the first protocol plugin matching ``ctx.path``."""
        for plugin in self.by_type(PluginType.PROTOCOL):
            if not plugin.enabled or not isinstance(plugin, ProtocolPlugin):
                continue
            try:
                if plugin.matches(ctx.path):
                    result = plugin.handle(ctx)
                    self._record(plugin.name)
                    if result is not None:
                        return result
            except Exception as exc:
                self._record(plugin.name, exc)
                logger.error("plugin_protocol_error", plugin=plugin.name, error=str(exc))
        return None

    def run_hook(self, name: str, ctx: Optional[PluginContext] = None) -> List[Any]:
        """Run all matching named hooks, returning a list of results."""
        results: List[Any] = []
        ctx = ctx or PluginContext()
        for plugin in self.by_type(PluginType.HOOK):
            if not plugin.enabled or not isinstance(plugin, HookPlugin):
                continue
            hooks = {}
            try:
                hooks = plugin.hooks() or {}
            except Exception as exc:
                self._record(plugin.name, exc)
                logger.error("plugin_hooks_error", plugin=plugin.name, error=str(exc))
                continue
            handler = hooks.get(name)
            if handler is None:
                continue
            try:
                results.append(handler(ctx))
                self._record(plugin.name)
            except Exception as exc:
                self._record(plugin.name, exc)
                logger.error(
                    "plugin_hook_invocation_error",
                    plugin=plugin.name,
                    hook=name,
                    error=str(exc),
                )
        return results

    # ------------------------------------------------------------------
    # Admin / serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    **plugin.metadata.to_dict(),
                    "enabled": plugin.enabled,
                    "stats": dict(self._stats.get(plugin.name, {})),
                    "health": _safe_health(plugin),
                }
                for plugin in self._plugins.values()
            ]


def _safe_health(plugin: Plugin) -> Dict[str, Any]:
    try:
        return plugin.health()
    except Exception as exc:  # pragma: no cover - defensive
        return {"name": plugin.name, "status": "error", "error": str(exc)}


# Process-wide singleton used by the gateway. Tests should call
# ``plugin_registry.clear()`` between cases.
plugin_registry = PluginRegistry()
