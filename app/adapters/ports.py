"""Ports: the small interfaces the request path depends on.

Each port names one thing the gateway needs from the outside world and nothing
else, so an implementation can be swapped by changing a setting (see
``app/adapters/__init__.py``). The built-in adapters keep today's behaviour.

A port is a ``typing.Protocol``: an adapter does not have to inherit from it,
it only has to have these methods. ``tests/test_adapters.py`` holds one contract
test per port that every adapter must pass.
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class JsonCodec(Protocol):
    """Serialises response bodies. Output must be compact UTF-8 JSON."""

    name: str

    def dumps(self, content: Any) -> bytes: ...

    def loads(self, data: "bytes | str") -> Any: ...


@runtime_checkable
class UpstreamClient(Protocol):
    """Sends requests to a backend. ``httpx.AsyncClient`` already has this shape.

    Responses must behave like ``httpx.Response`` (``status_code``, ``headers``,
    ``content``, ``json()``, ``aclose()``), because the proxy handler reads them
    that way. This is where a faster data plane (a Rust sidecar, for example) plugs in.
    """

    async def request(self, method: str, url: str, **kwargs: Any) -> Any: ...

    def build_request(self, method: str, url: str, **kwargs: Any) -> Any: ...

    async def send(self, request: Any, **kwargs: Any) -> Any: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class Analytics(Protocol):
    """Per-request anomaly detection and usage analytics.

    ``process_request`` receives the request summary and returns a dict; the
    keys ``is_anomaly`` and ``anomaly_score`` add response headers. An empty
    dict means "nothing to report".
    """

    def process_request(self, request_data: Dict[str, Any]) -> Dict[str, Any]: ...


@runtime_checkable
class RequestLogSink(Protocol):
    """Receives one entry per finished request and persists it somewhere.

    ``submit`` must never block or raise on the request path. ``written``,
    ``dropped`` and ``failed`` are counters reported at shutdown.
    """

    mode: str
    written: int
    dropped: int
    failed: int

    def submit(self, entry: Any, api_key_id: Optional[str]) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
