"""The CLI's requests, sent to the real admin API.

The existing CLI tests check what the CLI sends against a fake that accepts
anything, which is how ``keys create`` and ``webhooks create`` (422: the
endpoints took query parameters, the CLI sent JSON) and ``logs --status``
(sent ``status``, the endpoint filters on ``status_code``) went unnoticed.
Here the CLI's AdminClient is backed by FastAPI's TestClient, so every
command goes through the gateway's own validation.
"""
import json
import sys
from typing import Any, Dict, Optional

import pytest

from app.cli.client import CLIError
from app.cli.main import main as cli_main
from app.config import get_settings

cli_module = sys.modules["app.cli.main"]


class GatewayBackedClient:
    """AdminClient look-alike that sends each request to the test app."""

    def __init__(self, test_client, base_url="http://testserver", token=None, *, timeout=15.0):
        self.http = test_client
        self.base_url = base_url
        self.token = token or get_settings().secret_key
        self.sent = []

    def request(self, method, path, *, json_body=None, params=None, admin=True):
        headers = {"Authorization": f"Bearer {self.token}"} if admin and self.token else {}
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        self.sent.append({"method": method, "path": path, "json": json_body, "params": clean})
        response = self.http.request(method, path, json=json_body, params=clean, headers=headers)
        if response.status_code >= 400:
            raise CLIError(
                f"{method} {path} -> {response.status_code}: {response.text}",
                status_code=response.status_code,
            )
        return response.json() if response.content else None

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def put(self, path, **kw):
        return self.request("PUT", path, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


@pytest.fixture
def gateway_cli(client, monkeypatch):
    made = []

    def factory(*args, **kwargs):
        c = GatewayBackedClient(client)
        made.append(c)
        return c

    monkeypatch.setattr(cli_module, "AdminClient", factory)
    return made


def _run(argv, capsys):
    rc = cli_main(["--json", *argv])
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_keys_create_succeeds_against_the_api(gateway_cli, capsys):
    rc, out, err = _run(["keys", "create", "--name", "cli-key", "--rate-limit-minute", "7",
                         "--allowed-paths", "/mcp", "/a2a"], capsys)
    assert rc == 0, err
    created = json.loads(out)
    assert created["name"] == "cli-key"
    assert created["rate_limit_per_minute"] == 7
    assert created["allowed_paths"] == ["/mcp", "/a2a"]
    assert created["key"].startswith("sk-")


def test_keys_list_toggle_delete_round_trip(gateway_cli, capsys):
    _run(["keys", "create", "--name", "round-trip"], capsys)
    rc, out, _ = _run(["keys", "list"], capsys)
    assert rc == 0
    entry = next(k for k in json.loads(out) if k["name"] == "round-trip")
    assert "key" not in entry
    assert _run(["keys", "toggle", entry["id"]], capsys)[0] == 0
    assert _run(["keys", "delete", entry["id"]], capsys)[0] == 0


def test_webhooks_create_and_delete_succeed_against_the_api(gateway_cli, capsys):
    from app.webhooks import webhook_manager

    rc, out, err = _run(["webhooks", "create", "--target-url", "https://hooks.example/x",
                         "--events", "rate_limit_exceeded", "api_key_revoked",
                         "--secret", "s3cr3t"], capsys)
    assert rc == 0, err
    created = json.loads(out)
    assert created["events"] == ["rate_limit_exceeded", "api_key_revoked"]
    try:
        hooks = webhook_manager.list_webhooks("rate_limit_exceeded")
        assert any(h["id"] == created["ids"][0] and h["secret"] == "s3cr3t" for h in hooks)
        # delete without --event-type: the API finds it.
        assert _run(["webhooks", "delete", created["ids"][0]], capsys)[0] == 0
        assert all(h["id"] != created["ids"][0] for h in webhook_manager.list_webhooks("rate_limit_exceeded"))
    finally:
        for event, hook_id in zip(created["events"], created["ids"]):
            webhook_manager.webhooks[event] = [
                h for h in webhook_manager.webhooks.get(event, []) if h["id"] != hook_id
            ]


def test_webhooks_create_without_events_is_a_clear_cli_error(gateway_cli, capsys):
    rc, _, err = _run(["webhooks", "create", "--target-url", "https://hooks.example/x"], capsys)
    assert rc == 1
    assert "--events" in err


def test_logs_status_filter_reaches_the_endpoint(gateway_cli, capsys):
    rc, _, err = _run(["logs", "--status", "404"], capsys)
    assert rc == 0, err
    sent = gateway_cli[-1].sent[-1]
    assert sent["path"] == "/admin/logs"
    assert sent["params"]["status_code"] == 404
    assert "status" not in sent["params"]


def test_query_parameter_api_key_creation_still_works(client):
    """The console and existing scripts send query parameters; keep them working."""
    response = client.post(
        "/admin/api-keys",
        params={"name": "query-style", "rate_limit_per_minute": 9, "allowed_paths": "/mcp,/a2a"},
        headers={"Authorization": f"Bearer {get_settings().secret_key}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["allowed_paths"] == ["/mcp", "/a2a"]
    assert response.json()["rate_limit_per_minute"] == 9


def test_query_parameter_webhook_creation_still_works(client):
    from app.webhooks import webhook_manager

    response = client.post(
        "/admin/webhooks",
        params={"event_type": "anomaly_detected", "url": "https://hooks.example/y"},
        json={"headers": {"X-Test": "1"}},
        headers={"Authorization": f"Bearer {get_settings().secret_key}"},
    )
    assert response.status_code == 200, response.text
    hook_id = response.json()["id"]
    hooks = webhook_manager.list_webhooks("anomaly_detected")
    assert any(h["id"] == hook_id and h["headers"] == {"X-Test": "1"} for h in hooks)
    webhook_manager.webhooks["anomaly_detected"] = [h for h in hooks if h["id"] != hook_id]
