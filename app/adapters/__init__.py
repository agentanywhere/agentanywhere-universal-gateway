"""Swappable parts of the request path.

``build_adapters(settings)`` picks one adapter per port from these settings, and
nothing else in the gateway names a concrete implementation:

=====================  ==========================  ==================================
Setting                Built-in choices            Port
=====================  ==========================  ==================================
``JSON_CODEC``         ``stdlib`` (default),       :class:`~app.adapters.ports.JsonCodec`
                       ``orjson``
``UPSTREAM_CLIENT``    ``httpx`` (default)         :class:`~app.adapters.ports.UpstreamClient`
``ANALYTICS_ADAPTER``  ``full`` (default), ``off`` :class:`~app.adapters.ports.Analytics`
``REQUEST_LOG_SINK``   ``database`` (default),     :class:`~app.adapters.ports.RequestLogSink`
                       ``null``
=====================  ==========================  ==================================

Any of them also accepts ``module:attribute``: a callable that takes the settings and
returns an object with the port's methods. To add a built-in, register a factory in
``app/adapters/builtin.py`` and add a contract test in ``tests/test_adapters.py``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.adapters import builtin
from app.adapters.ports import Analytics, JsonCodec, RequestLogSink, UpstreamClient
from app.adapters.registry import AdapterError, AdapterRegistry


@dataclass
class Adapters:
    json_codec: JsonCodec
    upstream: UpstreamClient
    analytics: Analytics
    request_log: RequestLogSink


def build_upstream_client(settings: Any) -> UpstreamClient:
    return builtin.upstream_clients.create(getattr(settings, "upstream_client", "httpx"), settings)


def build_adapters(settings: Any) -> Adapters:
    return Adapters(
        json_codec=builtin.json_codecs.create(getattr(settings, "json_codec", "stdlib"), settings),
        upstream=build_upstream_client(settings),
        analytics=builtin.analytics_engines.create(getattr(settings, "analytics_adapter", "full"), settings),
        request_log=builtin.request_log_sinks.create(getattr(settings, "request_log_sink", "database"), settings),
    )


def json_response_class(codec: JsonCodec):
    """The response class FastAPI uses by default. ``stdlib`` keeps Starlette's own class."""
    from starlette.responses import JSONResponse

    if getattr(codec, "name", "stdlib") == "stdlib":
        return JSONResponse

    class CodecJSONResponse(JSONResponse):
        def render(self, content: Any) -> bytes:
            return codec.dumps(content)

    return CodecJSONResponse


__all__ = [
    "json_response_class",
    "Adapters",
    "AdapterError",
    "AdapterRegistry",
    "build_adapters",
    "build_upstream_client",
    "Analytics",
    "JsonCodec",
    "RequestLogSink",
    "UpstreamClient",
]
