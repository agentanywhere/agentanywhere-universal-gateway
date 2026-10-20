"""Keep the documented quick start and reference checks from rotting.

These tests replay the requests in docs/tutorials/getting-started.md and
README.md against the app in process, with the sample HTTP backend from
samples/backends/http mounted through an ASGI transport instead of on port
8003. They also run the two documentation checkers in scripts/.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from app.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
settings = get_settings()


def _load_sample_http_backend():
    path = ROOT / "samples" / "backends" / "http" / "http_server.py"
    spec = importlib.util.spec_from_file_location("docs_sample_http_backend", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


@pytest.fixture
def sample_backend():
    """Route the gateway's upstream client to the sample HTTP backend."""
    import app.main as gateway_main

    original = gateway_main.router.client
    gateway_main.router.client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_load_sample_http_backend())
    )
    gateway_main.router.circuit_breakers.clear()
    try:
        yield
    finally:
        gateway_main.router.client = original
        gateway_main.router.circuit_breakers.clear()


def test_getting_started_flow(client, sample_backend):
    admin = {"Authorization": f"Bearer {settings.secret_key}"}

    # Step 3: health
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["status"] == "healthy"

    # Step 5: create a key with query parameters
    created = client.post("/admin/api-keys?name=first-key", headers=admin)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["name"] == "first-key"
    assert body["key"].startswith("sk-")
    assert body["id"]
    api_key = body["key"]

    # Step 6: a request through the gateway reaches the sample backend
    user = client.post(
        "/api/users",
        headers={"X-API-Key": api_key},
        json={"name": "Ada", "email": "ada@example.com"},
    )
    assert user.status_code == 200, user.text
    assert user.json()["name"] == "Ada"

    listed = client.get("/api/users", headers={"X-API-Key": api_key})
    assert listed.status_code == 200
    assert any(u["name"] == "Ada" for u in listed.json())

    # Step 7: no key, 401 with the documented envelope
    denied = client.post("/api/users", json={"name": "Ada", "email": "ada@example.com"})
    assert denied.status_code == 401
    assert denied.json()["error"] == "AUTHENTICATION_ERROR"
    assert denied.json()["message"] == "API key required"


def test_api_key_create_rejects_json_body(client):
    """docs/reference/cli.md documents that a JSON body answers 422."""
    admin = {"Authorization": f"Bearer {settings.secret_key}"}
    res = client.post("/admin/api-keys", json={"name": "cli"}, headers=admin)
    assert res.status_code == 422
    assert res.json()["details"]["errors"][0]["location"] == ["query", "name"]


def test_documented_rate_limit_response(client, sample_backend):
    """docs/tutorials/two-backends-end-to-end.md: the sixth request is 429."""
    admin = {"Authorization": f"Bearer {settings.secret_key}"}
    key = client.post(
        "/admin/api-keys?name=limited&rate_limit_per_minute=5", headers=admin
    ).json()["key"]
    codes = [
        client.get("/api/users", headers={"X-API-Key": key}).status_code
        for _ in range(6)
    ]
    assert codes == [200, 200, 200, 200, 200, 429]
    last = client.get("/api/users", headers={"X-API-Key": key})
    assert last.headers["Retry-After"] == "60"
    assert last.json()["message"] == "Rate limit exceeded: 5 requests per minute"


@pytest.mark.parametrize(
    "script",
    ["scripts/check_docs.py", "scripts/check_env_vars.py"],
)
def test_docs_checkers_pass(script):
    result = subprocess.run(
        [sys.executable, str(ROOT / script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
