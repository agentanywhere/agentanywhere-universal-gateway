"""The catch-all proxy passes each response header line on as the backend sent it.

A backend that sets two cookies sends two ``Set-Cookie`` lines. httpx joins lines of
the same name with ", " when headers are read as a dict, and a browser reads the
joined line as a single cookie, so the second cookie is lost. These tests send a
request down each proxy path with a backend that answers two ``Set-Cookie`` lines
and check that the caller receives two.

All upstreams are fakes; nothing leaves the process.
"""
import httpx
import pytest

import app.main as main
from app.plugins.base import PluginContext
from app.transformers import transformation_manager as tm

# Shared fixtures/helpers live in the sibling modules.
from tests.test_cov_main_gateway import (  # noqa: F401
    H, _isolated_database, _restore_handlers, backend, gw, gwdb, key,
)
from tests.test_cov_main_proxy import FakeUpstream, a_rule, stream_backend  # noqa: F401

# The first cookie carries a comma of its own (in Expires), which is why a joined
# line cannot be split apart again by the caller.
COOKIES = [
    "session=abc; Path=/; Expires=Wed, 21 Oct 2037 07:28:00 GMT; HttpOnly",
    "csrf=xyz; Path=/; SameSite=Lax",
]


def cookie_lines(extra=()):
    return [("set-cookie", c) for c in COOKIES] + list(extra)


def buffered(monkeypatch, extra=()):
    resp = httpx.Response(
        200, json={"ok": True},
        headers=cookie_lines([("x-upstream", "yes"), ("connection", "close"), *extra]),
    )
    return backend(monkeypatch, resp)


# ====================================================================== buffered responses
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/things"),            # plain REST
    ("POST", "/api/things"),
    ("POST", "/mcp/tools/call"),       # MCP over HTTP, not streamed
    ("POST", "/a2a/message:send"),     # A2A, not streamed
    ("GET", "/agui/events"),           # AG-UI without an SSE Accept: buffered
])
def test_buffered_response_keeps_each_set_cookie_line(gwdb, monkeypatch, H, method, path):
    calls = buffered(monkeypatch)
    r = gwdb.request(method, path, headers=H, json={"n": 1} if method == "POST" else None)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calls[0]["path"] == path
    assert r.headers.get_list("set-cookie") == COOKIES
    # The existing stripping and the other headers are unchanged.
    assert r.headers["x-upstream"] == "yes"
    assert "connection" not in r.headers
    assert r.headers.get_list("content-length") == [str(len(r.content))]
    assert len(r.headers.get_list("content-type")) == 1


def test_buffered_response_keeps_other_repeated_header_lines(gwdb, monkeypatch, H):
    buffered(monkeypatch, extra=[("link", "</a>; rel=preload"), ("link", "</b>; rel=preload")])
    r = gwdb.get("/api/things", headers=H)
    assert r.headers.get_list("link") == ["</a>; rel=preload", "</b>; rel=preload"]
    assert r.headers.get_list("set-cookie") == COOKIES


def test_single_set_cookie_line_is_unchanged(gwdb, monkeypatch, H):
    backend(monkeypatch, httpx.Response(200, json={}, headers=[("set-cookie", COOKIES[0])]))
    r = gwdb.get("/api/things", headers=H)
    assert r.headers.get_list("set-cookie") == [COOKIES[0]]


def test_set_cookie_lines_survive_a_transformation_that_leaves_them_alone(gwdb, monkeypatch, H, a_rule):
    buffered(monkeypatch)
    monkeypatch.setattr(
        tm, "transform_response",
        lambda **kw: (kw["status_code"], {**kw["headers"], "x-t": "1"}, kw["body"]),
    )
    r = gwdb.get("/api/things", headers=H)
    assert r.headers["x-t"] == "1"
    assert r.headers.get_list("set-cookie") == COOKIES


def test_transformation_that_rewrites_or_drops_set_cookie_wins(gwdb, monkeypatch, H, a_rule):
    buffered(monkeypatch)
    monkeypatch.setattr(
        tm, "transform_response",
        lambda **kw: (200, {**kw["headers"], "set-cookie": "only=1"}, kw["body"]),
    )
    assert gwdb.get("/api/things", headers=H).headers.get_list("set-cookie") == ["only=1"]

    monkeypatch.setattr(
        tm, "transform_response",
        lambda **kw: (200, {k: v for k, v in kw["headers"].items() if k != "set-cookie"}, kw["body"]),
    )
    assert "set-cookie" not in gwdb.get("/api/things", headers=H).headers


def test_set_cookie_lines_survive_response_plugins(gwdb, monkeypatch, H):
    buffered(monkeypatch)
    monkeypatch.setattr(main.settings, "plugins_enabled", True)

    def add_header(ctx: PluginContext) -> PluginContext:
        ctx.headers["x-plugin"] = "1"
        return ctx

    monkeypatch.setattr(main.plugin_registry, "transform_request", lambda ctx: ctx)
    monkeypatch.setattr(main.plugin_registry, "transform_response", add_header)
    monkeypatch.setattr(main.plugin_registry, "run_hook", lambda *a, **kw: None)
    r = gwdb.get("/api/things", headers=H)
    assert r.headers["x-plugin"] == "1"
    assert r.headers.get_list("set-cookie") == COOKIES


# ====================================================================== streamed responses
@pytest.mark.parametrize("method,path,accept", [
    ("GET", "/agui/events", "text/event-stream"),          # AG-UI SSE
    ("POST", "/a2a/message:stream", "application/json"),   # A2A streaming
    ("POST", "/acp/runs", "text/event-stream"),            # ACP run stream
])
def test_streamed_response_keeps_each_set_cookie_line(gwdb, monkeypatch, H, method, path, accept):
    up = FakeUpstream(chunks=(b"data: a\n\n", b"data: b\n\n"), headers=cookie_lines([
        ("content-type", "text/event-stream; charset=utf-8"), ("content-length", "99"), ("x-s", "1"),
    ]))
    calls = stream_backend(monkeypatch, up)
    backend(monkeypatch, response=None, exc=AssertionError("buffered path must not be used"))
    r = gwdb.request(method, path, headers={**H, "Accept": accept})
    assert r.status_code == 200 and r.text == "data: a\n\ndata: b\n\n"
    assert calls[0]["path"] == path and up.closed is True
    assert r.headers.get_list("set-cookie") == COOKIES
    # The existing stripping and the other headers are unchanged.
    assert r.headers.get_list("content-type") == ["text/event-stream; charset=utf-8"]
    assert r.headers["x-s"] == "1"
    assert "content-length" not in r.headers
