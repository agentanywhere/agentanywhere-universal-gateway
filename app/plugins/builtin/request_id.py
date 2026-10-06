"""Built-in plugin: ensure every outbound request carries an X-Request-ID."""
from __future__ import annotations

import uuid
from typing import Optional

from app.plugins.base import (
    PluginContext,
    PluginMetadata,
    PluginType,
    TransformPlugin,
)


class RequestIDPlugin(TransformPlugin):
    """Stamp ``X-Request-ID`` onto requests/responses if missing."""

    metadata = PluginMetadata(
        name="request-id",
        version="1.0.0",
        description="Adds an X-Request-ID header to all requests and responses.",
        author="AgentAnywhere",
        plugin_type=PluginType.TRANSFORM,
        tags=["observability", "default"],
    )

    HEADER = "X-Request-ID"

    def transform_request(self, ctx: PluginContext) -> Optional[PluginContext]:
        headers = dict(ctx.headers)
        if self.HEADER not in headers and self.HEADER.lower() not in {k.lower() for k in headers}:
            headers[self.HEADER] = ctx.request_id or uuid.uuid4().hex
            ctx.headers = headers
        return ctx

    def transform_response(self, ctx: PluginContext) -> Optional[PluginContext]:
        headers = dict(ctx.headers)
        if self.HEADER not in headers and self.HEADER.lower() not in {k.lower() for k in headers}:
            headers[self.HEADER] = ctx.request_id or uuid.uuid4().hex
            ctx.headers = headers
        return ctx
