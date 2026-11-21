"""The adapters that ship with the gateway. The first of each list is the default."""
from __future__ import annotations

import http.cookiejar

import json
from typing import Any, Dict, Optional

from app.adapters.ports import Analytics, JsonCodec, RequestLogSink, UpstreamClient
from app.adapters.registry import AdapterError, AdapterRegistry

json_codecs: AdapterRegistry[JsonCodec] = AdapterRegistry("JSON codec", JsonCodec)
upstream_clients: AdapterRegistry[UpstreamClient] = AdapterRegistry("upstream client", UpstreamClient)
analytics_engines: AdapterRegistry[Analytics] = AdapterRegistry("analytics", Analytics)
request_log_sinks: AdapterRegistry[RequestLogSink] = AdapterRegistry("request-log sink", RequestLogSink)


# --- JSON codecs ---------------------------------------------------------------------------

class StdlibJsonCodec:
    """Same bytes Starlette's ``JSONResponse`` produces."""

    name = "stdlib"

    def dumps(self, content: Any) -> bytes:
        return json.dumps(
            content, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")
        ).encode("utf-8")

    def loads(self, data: "bytes | str") -> Any:
        return json.loads(data)


class OrjsonCodec:
    """Much faster, but not byte-identical to ``stdlib`` in edge cases: ``NaN`` and
    infinity become ``null`` instead of raising, and integers beyond 64 bits raise."""

    name = "orjson"

    def __init__(self) -> None:
        try:
            import orjson
        except ImportError as exc:
            raise AdapterError("JSON_CODEC=orjson needs the 'orjson' package: pip install orjson") from exc
        self._orjson = orjson

    def dumps(self, content: Any) -> bytes:
        return self._orjson.dumps(content, option=self._orjson.OPT_NON_STR_KEYS)

    def loads(self, data: "bytes | str") -> Any:
        return self._orjson.loads(data)


@json_codecs.register("stdlib")
def _stdlib_codec(settings: Any) -> JsonCodec:
    return StdlibJsonCodec()


@json_codecs.register("orjson")
def _orjson_codec(settings: Any) -> JsonCodec:
    return OrjsonCodec()


# --- Upstream clients ----------------------------------------------------------------------

class NoCookieJar(http.cookiejar.CookieJar):
    """A cookie jar that keeps nothing.

    One HTTP client carries every caller's requests to the backends. With an ordinary jar, a
    cookie a backend sets in its answer to one caller (a session, say) would be kept and sent on
    the next callers' requests. A caller's own ``Cookie`` header is forwarded as it came, and the
    backend's ``Set-Cookie`` reaches the caller; the gateway itself remembers neither."""

    def set_cookie(self, cookie: http.cookiejar.Cookie) -> None:
        return None


@upstream_clients.register("httpx")
def _httpx_client(settings: Any) -> UpstreamClient:
    import httpx

    return httpx.AsyncClient(timeout=settings.default_timeout, cookies=NoCookieJar())


# --- Analytics -----------------------------------------------------------------------------

class NoAnalytics:
    """Reports nothing and costs nothing."""

    def process_request(self, request_data: Dict[str, Any]) -> Dict[str, Any]:
        return {}


@analytics_engines.register("full")
def _full_analytics(settings: Any) -> Analytics:
    from app.ai_service import AIService

    return AIService()


@analytics_engines.register("off")
def _no_analytics(settings: Any) -> Analytics:
    return NoAnalytics()


# --- Request-log sinks ---------------------------------------------------------------------

class NullRequestLogSink:
    """Drops every entry. Same effect as ``REQUEST_LOG_MODE=off``, but selected by name."""

    mode = "off"

    def __init__(self) -> None:
        self.written = 0
        self.dropped = 0
        self.failed = 0

    def submit(self, entry: Any, api_key_id: Optional[str]) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@request_log_sinks.register("database")
def _database_sink(settings: Any) -> RequestLogSink:
    from app.request_log_writer import writer_from_settings

    return writer_from_settings(settings)


@request_log_sinks.register("null")
def _null_sink(settings: Any) -> RequestLogSink:
    return NullRequestLogSink()
