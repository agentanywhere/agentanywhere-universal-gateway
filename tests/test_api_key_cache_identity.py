"""Limits must hold when the Redis API-key cache is in use.

With Redis enabled, a validated key is cached for five minutes. The cached
record used to carry the key's SHA-256 hash in its ``key`` field, while a
record read from the database carried the key itself. Everything downstream
identifies the caller by that field, so from a key's second request onwards:

* the tenant lookup hashed a hash, found no tenant, and tenant quotas were
  never enforced again until the cache entry expired;
* the rate limiter counted the first request under one identity and the rest
  under another, letting ``limit + 1`` requests through.

Neither showed up without Redis, and the earlier tenant-quota test mocked the
quota check itself, so it could not see this. These tests use a real key, a
real tenant, the real quota arithmetic, and a stand-in for the Redis client.
"""
from unittest.mock import patch

import httpx
import pytest

from app.config import get_settings

ADMIN = {"Authorization": f"Bearer {get_settings().secret_key}"}


class FakeRedis:
    """The two calls the API-key cache makes, backed by a dict."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


@pytest.fixture
def cached_auth():
    """Give the gateway's auth middleware a working API-key cache."""
    import app.main as main

    original = main.auth_middleware.redis_client
    main.auth_middleware.redis_client = FakeRedis()
    main.rate_limiter._memory_counters.clear()
    try:
        yield main.auth_middleware.redis_client
    finally:
        main.auth_middleware.redis_client = original
        main.rate_limiter._memory_counters.clear()


def _new_key(client, per_minute):
    response = client.post(
        f"/admin/api-keys?name=cache-identity&rate_limit_per_minute={per_minute}&rate_limit_per_hour=1000",
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _statuses(client, key, count):
    return [
        client.post("/mcp/query", headers={"X-API-Key": key}, json={"query": "x"}).status_code
        for _ in range(count)
    ]


@patch("app.routing.Router.route_request")
def test_cached_key_keeps_its_identity(mock_route, client, cached_auth):
    mock_route.return_value = httpx.Response(200, json={"status": "ok"})
    key = _new_key(client, per_minute=100)["key"]

    import app.main as main
    seen = []
    original = main.rate_limiter._identity

    def spy(request, api_key):
        seen.append(original(request, api_key))
        return seen[-1]

    with patch.object(main.rate_limiter, "_identity", side_effect=spy):
        assert _statuses(client, key, 3) == [200, 200, 200]

    assert cached_auth.store, "the API-key cache was never written; the test is not exercising it"
    assert len(set(seen)) == 1, "the caller's identity changed between requests"


@patch("app.routing.Router.route_request")
def test_rate_limit_is_exact_with_the_key_cache(mock_route, client, cached_auth):
    mock_route.return_value = httpx.Response(200, json={"status": "ok"})
    key = _new_key(client, per_minute=3)["key"]

    assert _statuses(client, key, 5) == [200, 200, 200, 429, 429]


@pytest.mark.asyncio
@patch("app.routing.Router.route_request")
async def test_tenant_quota_is_enforced_with_the_key_cache(mock_route, client, cached_auth, db_session):
    mock_route.return_value = httpx.Response(200, json={"status": "ok"})
    created = _new_key(client, per_minute=100)

    tenant = client.post(
        "/admin/tenants",
        headers=ADMIN,
        json={"tenant_id": "cache-identity-tenant", "name": "Quota",
              "quota": {"requests_per_minute": 2, "requests_per_hour": 1000}},
    )
    assert tenant.status_code == 200, tenant.text
    linked = client.post(
        "/admin/tenants/cache-identity-tenant/associate-api-key",
        headers=ADMIN,
        json={"api_key_id": created["id"], "api_key": created["key"]},
    )
    assert linked.status_code == 200, linked.text

    # The gateway writes tenant usage after the response, from a background
    # task with its own session on the default database. Here usage is written
    # to the test database through the same service call, so the quota check
    # (real lookup by the presented key, real arithmetic) has something to read.
    from app.tenant import tenant_service

    statuses = []
    for _ in range(4):
        response = client.post(
            "/mcp/query", headers={"X-API-Key": created["key"]}, json={"query": "x"}
        )
        statuses.append(response.status_code)
        if response.status_code == 200:
            await tenant_service.record_request(db_session, "cache-identity-tenant")

    assert statuses == [200, 200, 429, 429]
