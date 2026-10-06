"""Built-in plugin: simple static-token authentication provider.

This plugin is OFF by default. Operators can enable it to permit a
fixed shared bearer token (e.g. for internal service-to-service calls)
without having to mint an API key.

Configure with::

    plugin_configs = {
        "static-token-auth": {
            "enabled": True,
            "token": "super-secret-token",
            "principal": "internal-service",
        }
    }
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from app.plugins.base import (
    AuthProviderPlugin,
    PluginContext,
    PluginMetadata,
    PluginType,
)


class StaticTokenAuthPlugin(AuthProviderPlugin):
    metadata = PluginMetadata(
        name="static-token-auth",
        version="1.0.0",
        description="Authenticate requests carrying a fixed shared token.",
        author="AgentAnywhere",
        plugin_type=PluginType.AUTH_PROVIDER,
        tags=["auth"],
        config_schema={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "token": {"type": "string"},
                "principal": {"type": "string"},
                "header": {"type": "string"},
            },
            "required": ["token"],
        },
    )

    def __init__(self, config=None):
        super().__init__(config)
        self.enabled = bool(self.config.get("enabled", False))
        self._token: Optional[str] = self.config.get("token")
        self._principal: str = self.config.get("principal", "static-token")
        self._header: str = self.config.get("header", "Authorization")

    def authenticate(self, ctx: PluginContext) -> Optional[Dict[str, Any]]:
        if not self._token:
            return None
        candidate = ctx.headers.get(self._header) or ctx.headers.get(self._header.lower())
        if not candidate:
            return None
        normalized = candidate.removeprefix("Bearer ").strip()
        if normalized != self._token:
            return None
        return {
            "authenticated": True,
            "principal": self._principal,
            "method": "static-token",
        }
