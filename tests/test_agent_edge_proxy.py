"""Wiring test for the agent-edge HTTP proxy handler (``_proxy_agent_route``).

Exercises the handler directly with a fake request + a mocked upstream stream,
so it needs neither the app lifespan, a database, nor real network access.

The handler streams the upstream response (see the "stream proxied responses"
change), so we mock :meth:`app.routing.Router.start_upstream_stream` — the call
the handler actually makes — and assert on the resulting ``StreamingResponse``.
"""
import asyncio
from unittest.mock import AsyncMock

import httpx

import app.main as main
from app.agent_routes import AgentEdgeConfig


class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeRequest:
    def __init__(self, method, path, headers, query=None, body=b""):
        self.method = method
        self.url = _FakeURL(path)
        self.headers = headers
        self.query_params = query or {}
        self._body = body

    async def body(self):
        return self._body


class _FakeUpstream:
    """Minimal stand-in for the streaming ``httpx.Response`` returned by
    ``start_upstream_stream``: exposes ``status_code``, case-insensitive
    ``headers``, an async ``aiter_bytes`` and an ``aclose`` coroutine."""

    def __init__(self, status_code, body: bytes, headers: dict):
        self.status_code = status_code
        self.headers = httpx.Headers(headers)
        self._body = body
        self.closed = False

    async def aiter_bytes(self):
        yield self._body

    async def aclose(self):
        self.closed = True


def _run(coro):
    """Run a coroutine on a throwaway loop without polluting the global loop
    other tests rely on (save/restore the current event loop)."""
    try:
        prev = asyncio.get_event_loop()
    except RuntimeError:
        prev = None
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(prev)


async def _collect(streaming_response) -> bytes:
    """Drain a Starlette ``StreamingResponse`` body iterator into bytes."""
    chunks = []
    async for chunk in streaming_response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
    return b"".join(chunks)


def _route(path):
    cfg = AgentEdgeConfig(routes={"orders-agent": "http://10.0.0.10:8090"})
    return cfg.resolve(path)


def test_proxy_strips_prefix_and_forwards(monkeypatch):
    captured = {}

    async def fake_start_stream(**kwargs):
        captured.update(kwargs)
        return _FakeUpstream(
            200,
            b'{"ok":true}',
            {"content-type": "application/json", "content-length": "999"},
        )

    monkeypatch.setattr(
        main.router, "start_upstream_stream", AsyncMock(side_effect=fake_start_stream)
    )

    req = _FakeRequest(
        "GET",
        "/agents/orders-agent/api/v1/health",
        headers={"authorization": "Bearer tok", "content-length": "0", "host": "gateway.example.com"},
    )
    resp = _run(main._proxy_agent_route(req, _route(req.url.path), "req-1"))

    assert resp.status_code == 200
    assert _run(_collect(resp)) == b'{"ok":true}'
    # stale upstream content-length (999) must NOT propagate — a chunked
    # StreamingResponse computes framing itself.
    assert "content-length" not in {k.lower() for k in resp.headers.keys()}

    # upstream received the prefix-stripped path + preserved Authorization
    rc = captured["route_config"]
    assert rc.backend_url == "http://10.0.0.10:8090"
    assert rc.rewrite_path == "/api/v1/health"
    assert captured["path"] == "/api/v1/health"
    assert captured["headers"]["authorization"] == "Bearer tok"
    # content-length is stripped from the forwarded request headers (httpx recomputes)
    assert "content-length" not in {k.lower() for k in captured["headers"].keys()}


def test_proxy_strips_content_encoding(monkeypatch):
    """httpx transparently decodes the upstream body on read, so the gateway must
    NOT forward the upstream ``Content-Encoding`` header — otherwise browsers try
    to gunzip already-decoded bytes and fail with ERR_CONTENT_DECODING_FAILED.

    ``start_upstream_stream`` returns an httpx response whose ``aiter_bytes``
    yields *decoded* (identity) bytes, so we model the upstream as already-decoded
    content still carrying a stale ``content-encoding: gzip`` header and assert the
    gateway drops that header.
    """
    raw = b'{"ok":true}'

    async def fake_start_stream(**kwargs):
        return _FakeUpstream(
            200,
            raw,
            {"content-type": "application/json", "content-encoding": "gzip"},
        )

    monkeypatch.setattr(
        main.router, "start_upstream_stream", AsyncMock(side_effect=fake_start_stream)
    )

    req = _FakeRequest("GET", "/agents/orders-agent/", headers={})
    resp = _run(main._proxy_agent_route(req, _route(req.url.path), "req-enc"))

    assert resp.status_code == 200
    assert _run(_collect(resp)) == raw
    lower_headers = {k.lower() for k in resp.headers.keys()}
    assert "content-encoding" not in lower_headers


def test_proxy_forwards_body_and_status(monkeypatch):
    async def fake_start_stream(**kwargs):
        return _FakeUpstream(201, kwargs.get("body") or b"", {"content-type": "application/json"})

    monkeypatch.setattr(
        main.router, "start_upstream_stream", AsyncMock(side_effect=fake_start_stream)
    )

    req = _FakeRequest(
        "POST",
        "/agents/orders-agent/api/v1/submissions",
        headers={"content-type": "application/json"},
        body=b'{"code":"x"}',
    )
    resp = _run(main._proxy_agent_route(req, _route(req.url.path), "req-2"))
    assert resp.status_code == 201
    assert _run(_collect(resp)) == b'{"code":"x"}'
