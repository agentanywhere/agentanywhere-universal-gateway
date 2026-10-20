"""/docs, /redoc, /openapi.json with DEBUG=false; and who the client is.

* With DEBUG=false the docs paths skipped authentication and fell through to
  the catch-all proxy, which sent them, without any key check, to the default
  backend. /openapi.json was also served by the gateway itself, describing
  every admin route.
* The gateway took the first X-Forwarded-For entry as the client address, and
  the shipped nginx config kept whatever the client sent there, so a client
  could pick its own address for IP rules and logs.
"""
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from app.client_ip import client_ip, resolve_client_ip
from app.config import get_settings

REPO = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# S5
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_paths_are_404_and_never_proxied_without_debug(client, path):
    import app.main as main

    assert not main.settings.debug, "the suite runs with DEBUG=false"
    with patch("app.routing.Router.route_request") as route:
        route.return_value = httpx.Response(200, text="backend docs")
        response = client.get(path)
        with_key = client.get(path, headers={"X-API-Key": get_settings().secret_key})
    assert response.status_code == 404
    assert with_key.status_code == 404
    route.assert_not_called()
    assert "backend docs" not in response.text


def test_the_schema_is_not_served_without_debug():
    import app.main as main

    assert main.app.openapi_url is None
    assert main.app.docs_url is None
    assert main.app.redoc_url is None


# ---------------------------------------------------------------------------
# S6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("peer,xff,trusted,expected", [
    # No trusted proxies: the header is ignored.
    ("203.0.113.9", "1.2.3.4", [], "203.0.113.9"),
    # Peer is not a trusted proxy: ignored as well.
    ("203.0.113.9", "1.2.3.4", ["10.0.0.0/8"], "203.0.113.9"),
    # One trusted proxy that overwrote the header.
    ("10.0.0.5", "198.51.100.7", ["10.0.0.5"], "198.51.100.7"),
    # Proxy that appended: the client's own left part is never used.
    ("10.0.0.5", "1.2.3.4, 198.51.100.7", ["10.0.0.5"], "198.51.100.7"),
    # Two proxy hops, both trusted: the right-most untrusted entry wins.
    ("10.0.0.5", "1.2.3.4, 198.51.100.7, 10.0.0.6", ["10.0.0.0/8"], "198.51.100.7"),
    # Every entry trusted: the left-most is the best we have.
    ("10.0.0.5", "10.0.0.7, 10.0.0.6", ["10.0.0.0/8"], "10.0.0.7"),
    # Garbage in the header: fall back to the peer.
    ("10.0.0.5", "not-an-ip", ["10.0.0.5"], "10.0.0.5"),
    # IPv6 and ports.
    ("::1", "[2001:db8::1]:443", ["::1"], "2001:db8::1"),
    ("10.0.0.5", "198.51.100.7:5555", ["10.0.0.5"], "198.51.100.7"),
])
def test_resolve_client_ip(peer, xff, trusted, expected):
    assert resolve_client_ip(peer, xff, trusted) == expected


class _Req:
    def __init__(self, peer, xff=None):
        self.client = type("C", (), {"host": peer})()
        self.headers = {"x-forwarded-for": xff} if xff else {}


def test_default_setting_trusts_no_forwarded_header(monkeypatch):
    monkeypatch.setattr(get_settings(), "trusted_proxies", [])
    assert client_ip(_Req("203.0.113.9", "1.2.3.4")) == "203.0.113.9"


def test_trusted_proxies_setting_is_honoured(monkeypatch):
    monkeypatch.setattr(get_settings(), "trusted_proxies", ["10.0.0.5"])
    assert client_ip(_Req("10.0.0.5", "1.2.3.4, 198.51.100.7")) == "198.51.100.7"


def test_spoofed_header_cannot_dodge_an_ip_deny_rule(client, monkeypatch):
    """A client denied by IP must not get through by sending X-Forwarded-For."""
    from app.governance import ip_rule_engine

    monkeypatch.setattr(get_settings(), "trusted_proxies", [])
    seen = []
    original = ip_rule_engine.evaluate

    def spy(ip, **kw):
        seen.append(ip)
        return original(ip, **kw)

    monkeypatch.setattr(ip_rule_engine, "evaluate", spy)
    key = client.post(
        "/admin/api-keys", params={"name": "ip"},
        headers={"Authorization": f"Bearer {get_settings().secret_key}"},
    ).json()["key"]
    with patch("app.routing.Router.route_request") as route:
        route.return_value = httpx.Response(200, json={})
        client.get("/mcp/x", headers={"X-API-Key": key, "X-Forwarded-For": "8.8.8.8"})
    assert seen, "IP rules were not evaluated"
    assert "8.8.8.8" not in seen


def test_nginx_overwrites_x_forwarded_for():
    text = (REPO / "nginx" / "nginx.conf").read_text()
    directives = [
        line.strip() for line in text.splitlines()
        if line.strip().startswith("proxy_set_header X-Forwarded-For")
    ]
    assert directives, "nginx.conf no longer sets X-Forwarded-For"
    for line in directives:
        assert "$proxy_add_x_forwarded_for" not in line
        assert line == "proxy_set_header X-Forwarded-For $remote_addr;"
