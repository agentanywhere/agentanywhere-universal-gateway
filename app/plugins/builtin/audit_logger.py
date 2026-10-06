"""Built-in plugin: lightweight audit hook that emits structured logs."""
from __future__ import annotations

from typing import Dict, Optional

import structlog

from app.plugins.base import (
    HookPlugin,
    PluginContext,
    PluginMetadata,
    PluginType,
)


logger = structlog.get_logger("mcp_gateway.audit")


class AuditLoggerPlugin(HookPlugin):
    """Emit structured audit log entries on key lifecycle events.

    Activated by setting ``enabled: true`` in plugin config. Useful as
    a worked example for plugin authors.
    """

    metadata = PluginMetadata(
        name="audit-logger",
        version="1.0.0",
        description="Emit structured audit logs at key lifecycle points.",
        author="AgentAnywhere",
        plugin_type=PluginType.HOOK,
        tags=["observability", "audit"],
    )

    def __init__(self, config=None):
        super().__init__(config)
        self.enabled = bool(self.config.get("enabled", False))

    def hooks(self) -> Dict[str, callable]:
        return {
            "request_pre": self._on_request,
            "response_post": self._on_response,
            "startup": self._on_startup,
            "shutdown": self._on_shutdown,
        }

    def _on_startup(self, ctx: PluginContext) -> Optional[PluginContext]:
        logger.info("audit_startup")
        return None

    def _on_shutdown(self, ctx: PluginContext) -> Optional[PluginContext]:
        logger.info("audit_shutdown")
        return None

    def _on_request(self, ctx: PluginContext) -> Optional[PluginContext]:
        logger.info(
            "audit_request",
            method=ctx.method,
            path=ctx.path,
            request_id=ctx.request_id,
            tenant_id=ctx.tenant_id,
            api_key_present=bool(ctx.api_key),
        )
        return None

    def _on_response(self, ctx: PluginContext) -> Optional[PluginContext]:
        logger.info(
            "audit_response",
            method=ctx.method,
            path=ctx.path,
            status_code=ctx.status_code,
            request_id=ctx.request_id,
            tenant_id=ctx.tenant_id,
        )
        return None
