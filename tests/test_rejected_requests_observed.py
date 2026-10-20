"""Requests the auth layer rejects are visible; rate-limit alerts fire.

* 401, 403 and 429 answered by the auth middleware were invisible: it ran
  outside the logging middleware, so they had no X-Request-ID, no
  ``request_id`` in the body, no ``gateway_requests_total`` sample and no
  ``request_logs`` row.
* The ``rate_limit_exceeded`` webhook and the email/Slack alert were built
  from ``api_key.id``, which the principal did not have; the AttributeError
  was swallowed and nothing was ever sent.
"""
import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from prometheus_client import REGISTRY

from app.config import get_settings

ADMIN = {"Authorization": f"Bearer {get_settings().secret_key}"}


def _count(path, status):
    value = REGISTRY.get_sample_value(
        "gateway_requests_total",
        {"method": "GET", "path": path, "status_code": str(status), "protocol": "mcp"},
    )
    return value or 0.0


def _wait_for(predicate, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def captured_logs():
    """Capture request_logs rows the logging middleware writes."""
    rows = []

    async def fake_create(session, log, api_key_id=None):
        rows.append({"log": log, "api_key_id": api_key_id})

    with patch("app.repositories.log_repo.LogRepository.create", side_effect=fake_create):
        yield rows


def test_401_is_counted_logged_and_has_a_request_id(client, captured_logs):
    before = _count("/mcp", 401)
    response = client.get("/mcp/some/thing")
    assert response.status_code == 401
    rid = response.headers.get("X-Request-ID")
    assert rid
    assert response.json()["request_id"] == rid
    assert _count("/mcp", 401) == before + 1

    assert _wait_for(lambda: any(r["log"].request_id == rid for r in captured_logs))
    row = next(r for r in captured_logs if r["log"].request_id == rid)["log"]
    assert row.status_code == 401
    assert row.error == "AUTHENTICATION_ERROR"


def test_incoming_request_id_is_echoed_on_a_rejection(client, captured_logs):
    response = client.get("/mcp/x", headers={"X-Request-ID": "trace-me-123"})
    assert response.status_code == 401
    assert response.headers["X-Request-ID"] == "trace-me-123"


def test_403_from_path_scope_is_observed(client, captured_logs):
    key = client.post("/admin/api-keys", params={"name": "s7", "allowed_paths": "/a2a"}, headers=ADMIN).json()["key"]
    before = _count("/mcp", 403)
    response = client.get("/mcp/x", headers={"X-API-Key": key})
    assert response.status_code == 403
    assert response.headers.get("X-Request-ID")
    assert _count("/mcp", 403) == before + 1


def test_metric_labels_of_rejected_requests_are_bounded(client, captured_logs):
    for i in range(5):
        assert client.get(f"/random-{i}/probe/{i}").status_code == 401
    samples = [
        s for metric in REGISTRY.collect() if metric.name == "gateway_requests"
        for s in metric.samples if s.labels.get("status_code") == "401"
    ]
    paths = {s.labels["path"] for s in samples}
    assert not any(p.startswith("/random-") for p in paths), paths
    assert "/other" in paths


@patch("app.routing.Router.route_request")
def test_429_is_observed_and_fires_the_webhook_and_alert(route, client, captured_logs):
    import app.main as main

    route.return_value = httpx.Response(200, json={"ok": True})
    main.rate_limiter._memory_counters.clear()
    created = client.post(
        "/admin/api-keys", params={"name": "limited", "rate_limit_per_minute": 1}, headers=ADMIN
    ).json()

    with patch("app.webhooks.webhook_manager.trigger_webhook", new_callable=AsyncMock) as hook, \
         patch("app.notifications.notification_manager.send_rate_limit_alert", new_callable=AsyncMock) as alert:
        assert client.get("/mcp/x", headers={"X-API-Key": created["key"]}).status_code == 200
        limited = client.get("/mcp/x", headers={"X-API-Key": created["key"]})
        assert limited.status_code == 429
        assert limited.headers.get("X-Request-ID")
        assert _wait_for(lambda: hook.await_count >= 1 and alert.await_count >= 1)

    event, payload = hook.await_args.args
    assert event == "rate_limit_exceeded"
    assert payload["api_key_id"] == created["id"]
    assert payload["window"] == "minute"
    assert payload["limit"] == 1
    assert created["key"] not in str(payload)
    assert alert.await_args.kwargs["api_key_id"] == created["id"]

    hits = REGISTRY.get_sample_value("gateway_rate_limit_hits_total", {"client_id": created["id"]})
    assert hits and hits >= 1
    main.rate_limiter._memory_counters.clear()


def test_rate_limiter_outside_an_event_loop_does_not_fail():
    """Unit callers (no running loop) still get the 429, without errors."""
    from app.exceptions import RateLimitError
    from app.middleware.rate_limit import RateLimiter
    from app.models import APIKey

    limiter = RateLimiter()
    limiter.redis_client = None
    key = APIKey(id="k1", key="sk-unit", name="unit", rate_limit_per_minute=1, rate_limit_per_hour=10)

    class Req:
        client = type("C", (), {"host": "127.0.0.1"})()
        headers = {}
        url = type("U", (), {"path": "/mcp/x"})()
        state = type("S", (), {})()

    limiter.check_rate_limit(Req(), key)
    with pytest.raises(RateLimitError):
        limiter.check_rate_limit(Req(), key)
