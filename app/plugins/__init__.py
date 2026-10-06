"""Plugin system for AgentAnywhere Universal Gateway.

Third-party extensions can register transformations, authentication
providers, custom protocol handlers, and arbitrary lifecycle/request
hooks against the gateway.

Quick start
-----------

1. Subclass one of the base classes (``Plugin``, ``TransformPlugin``,
   ``AuthProviderPlugin``, or ``ProtocolPlugin``) in your package.
2. Either:
   - Register your class via the ``mcp_gateway.plugins`` entry point in
     your ``pyproject.toml`` / ``setup.py``; or
   - Drop a Python module into a directory listed in
     ``settings.plugin_paths`` (or ``MCP_GATEWAY_PLUGIN_PATH``).
3. The gateway loads plugins automatically at startup. Use the
   ``/admin/plugins`` API or the ``mcp-gateway plugins`` CLI to inspect
   and toggle them at runtime.

See :mod:`app.plugins.base` for the available base classes and
:mod:`app.plugins.registry` for the runtime registry.
"""
from app.plugins.base import (
    Plugin,
    PluginContext,
    PluginMetadata,
    PluginType,
    TransformPlugin,
    AuthProviderPlugin,
    ProtocolPlugin,
    HookPlugin,
)
from app.plugins.registry import PluginRegistry, plugin_registry
from app.plugins.loader import PluginLoader, load_plugins

__all__ = [
    "Plugin",
    "PluginContext",
    "PluginMetadata",
    "PluginType",
    "TransformPlugin",
    "AuthProviderPlugin",
    "ProtocolPlugin",
    "HookPlugin",
    "PluginRegistry",
    "plugin_registry",
    "PluginLoader",
    "load_plugins",
]
