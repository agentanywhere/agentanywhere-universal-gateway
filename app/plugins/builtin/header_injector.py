"""Built-in plugin: inject configurable static headers into requests."""
from __future__ import annotations

from typing import Optional

from app.plugins.base import (
    PluginContext,
    PluginMetadata,
    PluginType,
    TransformPlugin,
)


class HeaderInjectorPlugin(TransformPlugin):
    """Inject static headers from plugin config.

    Configure with::

        plugin_configs = {
            "header-injector": {
                "request_headers": {"X-Gateway": "mcp"},
                "response_headers": {"X-Powered-By": "MCP-Gateway"},
            }
        }
    """

    metadata = PluginMetadata(
        name="header-injector",
        version="1.0.0",
        description="Inject static headers configured per-deployment.",
        author="AgentAnywhere",
        plugin_type=PluginType.TRANSFORM,
        tags=["headers"],
        config_schema={
            "type": "object",
            "properties": {
                "request_headers": {"type": "object"},
                "response_headers": {"type": "object"},
            },
        },
    )

    def __init__(self, config=None):
        super().__init__(config)
        self.enabled = bool(self.config.get("enabled", False))

    def transform_request(self, ctx: PluginContext) -> Optional[PluginContext]:
        extra = self.config.get("request_headers") or {}
        if not extra:
            return None
        ctx.headers = {**ctx.headers, **extra}
        return ctx

    def transform_response(self, ctx: PluginContext) -> Optional[PluginContext]:
        extra = self.config.get("response_headers") or {}
        if not extra:
            return None
        ctx.headers = {**ctx.headers, **extra}
        return ctx
