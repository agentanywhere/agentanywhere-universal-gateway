"""Tests for the CLI tool."""
from __future__ import annotations

import io
import json
import sys
from typing import Any, Dict, List, Optional
from unittest import mock

import pytest

import sys

from app.cli.main import build_parser, main as cli_main
from app.cli.client import AdminClient, CLIError
from app.cli.formatting import format_table, format_json, print_kv

cli_module = sys.modules["app.cli.main"]


# ---------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------


def test_parser_has_top_level_subcommands():
    parser = build_parser()
    args = parser.parse_args(["health"])
    assert args.command == "health"


def test_parser_keys_create_args():
    parser = build_parser()
    args = parser.parse_args(["keys", "create", "--name", "k", "--rate-limit-minute", "10"])
    assert args.name == "k"
    assert args.rate_limit_minute == 10


def test_parser_routes_create_args():
    parser = build_parser()
    args = parser.parse_args([
        "routes", "create",
        "--path", "/svc/*",
        "--backend", "http://localhost:9000",
        "--methods", "GET", "POST",
    ])
    assert args.path == "/svc/*"
    assert args.backend == "http://localhost:9000"
    assert args.methods == ["GET", "POST"]


def test_parser_plugins_subcommands():
    parser = build_parser()
    for cmd in ["list", "show foo", "enable foo", "disable foo", "reload", "remove foo"]:
        argv = ["plugins", *cmd.split()]
        args = parser.parse_args(argv)
        assert args.plugins_cmd == cmd.split()[0]


def test_parser_logs_filters():
    parser = build_parser()
    args = parser.parse_args(["logs", "--limit", "5", "--status", "500"])
    assert args.limit == 5
    assert args.status == 500


# ---------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------


def test_format_table_with_columns():
    rows = [{"a": 1, "b": "x"}, {"a": 2, "b": "y"}]
    out = format_table(rows, columns=["a", "b"])
    assert "a" in out and "b" in out
    assert "1" in out and "y" in out


def test_format_table_empty():
    assert format_table([]) == "(no rows)"


def test_format_json_dict():
    payload = {"foo": "bar"}
    parsed = json.loads(format_json(payload))
    assert parsed == payload


def test_print_kv_outputs_pairs(capsys):
    print_kv({"k": "v", "x": 1})
    out = capsys.readouterr().out
    assert "k" in out and "v" in out
    assert "x" in out and "1" in out


# ---------------------------------------------------------------------
# AdminClient (with urlopen mocked)
# ---------------------------------------------------------------------


def _mock_urlopen(payload: Any, status: int = 200):
    body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return body

    return mock.patch("urllib.request.urlopen", return_value=_Resp())


def test_client_get_returns_json():
    client = AdminClient("http://localhost:8000", token="t")
    with _mock_urlopen({"hello": "world"}):
        assert client.get("/foo") == {"hello": "world"}


def test_client_post_sends_json_and_token():
    client = AdminClient("http://localhost:8000", token="t")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["data"] = req.data
        captured["headers"] = dict(req.header_items())
        return _Resp()

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.post("/admin/x", json_body={"a": 1})

    assert captured["url"] == "http://localhost:8000/admin/x"
    assert json.loads(captured["data"].decode("utf-8")) == {"a": 1}
    # urllib normalises header capitalisation
    headers_lower = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers_lower["authorization"] == "Bearer t"
    assert headers_lower["content-type"] == "application/json"


def test_client_login_caches_token():
    client = AdminClient("http://localhost:8000")
    with _mock_urlopen({"access_token": "tok-123", "token_type": "bearer"}):
        token = client.login("admin", "password")
    assert token == "tok-123"
    assert client.token == "tok-123"


def test_client_http_error_wraps_to_clierror():
    import urllib.error

    client = AdminClient("http://localhost:8000")

    err = urllib.error.HTTPError(
        url="http://localhost:8000/x",
        code=400,
        msg="bad",
        hdrs=None,
        fp=io.BytesIO(b'{"error":"x"}'),
    )

    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(CLIError) as exc:
            client.get("/x")
        assert exc.value.status_code == 400
        assert exc.value.payload == {"error": "x"}


def test_client_url_error_wraps_to_clierror():
    import urllib.error

    client = AdminClient("http://localhost:8000")
    err = urllib.error.URLError(reason="nope")
    with mock.patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(CLIError):
            client.get("/x")


def test_client_query_params_built():
    client = AdminClient("http://localhost:8000")
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"[]"

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        return _Resp()

    with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        client.get("/admin/logs", params={"limit": 5, "status": 500, "skip": None})

    assert "limit=5" in captured["url"]
    assert "status=500" in captured["url"]
    assert "skip" not in captured["url"]


# ---------------------------------------------------------------------
# main() integration: command dispatch with mocked AdminClient
# ---------------------------------------------------------------------


class _FakeClient:
    def __init__(self, base_url: str = "http://localhost:8000", token: Optional[str] = None, *, timeout: float = 15.0):
        self.base_url = base_url
        self.token = token
        self.calls: List[Dict[str, Any]] = []
        self.responses: Dict[str, Any] = {}

    def _record(self, method: str, path: str, **kwargs):
        self.calls.append({"method": method, "path": path, **kwargs})
        return self.responses.get((method, path), self.responses.get(path, {"ok": True}))

    def get(self, path, **kwargs):
        return self._record("GET", path, **kwargs)

    def post(self, path, **kwargs):
        return self._record("POST", path, **kwargs)

    def put(self, path, **kwargs):
        return self._record("PUT", path, **kwargs)

    def delete(self, path, **kwargs):
        return self._record("DELETE", path, **kwargs)

    def login(self, username, password):
        self._record("POST", "/admin/auth/login", json_body={"username": username, "password": password})
        self.token = "fake-token"
        return self.token


@pytest.fixture
def fake_client(monkeypatch):
    instances: List[_FakeClient] = []

    def factory(*args, **kwargs):
        c = _FakeClient(*args, **kwargs)
        instances.append(c)
        return c

    monkeypatch.setattr(cli_module, "AdminClient", factory)
    return instances


def test_cli_health_dispatches_to_health_endpoint(fake_client, capsys):
    rc = cli_main(["--url", "http://gw:1234", "health"])
    assert rc == 0
    assert fake_client[0].calls[0]["path"] == "/health"


def test_cli_keys_list_dispatches(fake_client, capsys):
    fake_client_responses = {("GET", "/admin/api-keys"): [{"id": "a", "name": "k", "is_active": True}]}
    rc = cli_main(["keys", "list"])
    assert rc == 0
    # The factory call records into fake_client[0]
    assert fake_client[0].calls[0]["path"] == "/admin/api-keys"


def test_cli_plugins_enable_posts(fake_client):
    rc = cli_main(["plugins", "enable", "request-id"])
    assert rc == 0
    assert fake_client[0].calls[0]["method"] == "POST"
    assert fake_client[0].calls[0]["path"] == "/admin/plugins/request-id/enable"


def test_cli_plugins_reload_with_paths(fake_client, tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"header-injector": {"enabled": True}}))
    rc = cli_main([
        "plugins", "reload",
        "--path", str(tmp_path),
        "--disable", "audit-logger",
        "--config-file", str(cfg),
    ])
    assert rc == 0
    call = fake_client[0].calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/admin/plugins/reload"
    body = call["json_body"]
    assert body["plugin_paths"] == [str(tmp_path)]
    assert body["plugins_disabled"] == ["audit-logger"]
    assert body["plugin_configs"] == {"header-injector": {"enabled": True}}


def test_cli_keys_create_sends_body(fake_client):
    rc = cli_main(["keys", "create", "--name", "n", "--rate-limit-minute", "30"])
    assert rc == 0
    body = fake_client[0].calls[0]["json_body"]
    assert body["name"] == "n"
    assert body["rate_limit_per_minute"] == 30


def test_cli_routes_create_sends_body(fake_client):
    rc = cli_main([
        "routes", "create",
        "--path", "/svc/*",
        "--backend", "http://api.local",
        "--methods", "GET",
    ])
    assert rc == 0
    body = fake_client[0].calls[0]["json_body"]
    assert body["path"] == "/svc/*"
    assert body["backend_url"] == "http://api.local"
    assert body["methods"] == ["GET"]


def test_cli_logout_removes_config(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    monkeypatch.setattr(cli_module, "DEFAULT_CONFIG_PATH", cfg)
    rc = cli_main(["logout"])
    assert rc == 0
    assert not cfg.exists()


def test_cli_login_writes_config(monkeypatch, tmp_path, fake_client):
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(cli_module, "DEFAULT_CONFIG_PATH", cfg)
    rc = cli_main(["login", "--username", "admin", "--password", "password"])
    assert rc == 0
    saved = json.loads(cfg.read_text())
    assert saved["token"] == "fake-token"


def test_cli_returns_error_on_clierror(monkeypatch, capsys):
    class BoomClient(_FakeClient):
        def get(self, path, **kwargs):
            raise CLIError("kaboom")

    monkeypatch.setattr(cli_module, "AdminClient", lambda *a, **k: BoomClient())
    rc = cli_main(["health"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "kaboom" in err
