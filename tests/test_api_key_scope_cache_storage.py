"""API keys: path scope, cache invalidation, hashed storage, and the list API.

* ``allowed_paths`` was stored but never checked, so a scoped key could call
  every route.
* With the Redis key cache, a deleted or disabled key kept working for up to
  five minutes: nothing removed its cache entry.
* Keys were stored in clear text, returned in full by ``GET /admin/api-keys``
  and written into Redis key names.
* ``GET /admin/api-keys`` did not return key ids, so the console sent the key
  itself to the delete and toggle endpoints and got 404.
"""
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db_models import APIKeyModel
from app.middleware.auth import path_is_allowed
from app.repositories.api_key_repo import APIKeyRepository

ADMIN = {"Authorization": f"Bearer {get_settings().secret_key}"}


class FakeRedis:
    """get / setex / delete, backed by a dict (what the key cache uses)."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def cached_auth():
    import app.main as main

    original = main.auth_middleware.redis_client
    main.auth_middleware.redis_client = FakeRedis()
    main.rate_limiter._memory_counters.clear()
    try:
        yield main.auth_middleware.redis_client
    finally:
        main.auth_middleware.redis_client = original
        main.rate_limiter._memory_counters.clear()


@pytest.fixture
def ok_backend():
    with patch("app.routing.Router.route_request") as route:
        route.return_value = httpx.Response(200, json={"status": "ok"})
        yield route


def _create(client, **params):
    params.setdefault("name", "scoped")
    response = client.post("/admin/api-keys", params=params, headers=ADMIN)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# S3: allowed_paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,patterns,allowed", [
    ("/mcp", ["/mcp"], True),
    ("/mcp/tools/list", ["/mcp"], True),
    ("/mcp/", ["/mcp"], True),
    ("/mcpx", ["/mcp"], False),          # prefix must end on a segment boundary
    ("/a2a/send", ["/mcp"], False),
    ("/a2a/send", ["/mcp", "/a2a"], True),
    ("/mcp/../a2a/send", ["/mcp"], False),   # dot segments are refused outright
    ("/mcp/./x", ["/mcp"], False),
    ("//mcp//tools", ["/mcp"], True),        # repeated slashes collapse
    ("/mcp/tools", ["/mcp/"], True),
    ("/mcp/tools", ["/mcp/*"], True),
    ("/api/v1/x", ["/api/v*"], True),
    ("/api/w1/x", ["/api/v*"], False),
    ("/anything", ["*"], True),
    ("/anything", ["/"], True),
    ("/MCP/tools", ["/mcp"], False),         # paths are case-sensitive, as routing is
])
def test_path_matching(path, patterns, allowed):
    assert path_is_allowed(path, patterns) is allowed


def test_scoped_key_is_refused_outside_its_paths(client, ok_backend):
    key = _create(client, allowed_paths="/mcp")["key"]
    headers = {"X-API-Key": key}

    assert client.post("/mcp/query", headers=headers, json={}).status_code == 200
    refused = client.post("/a2a/send", headers=headers, json={})
    assert refused.status_code == 403
    assert refused.json()["error"] == "AUTHORIZATION_ERROR"
    assert client.get("/mcpx/thing", headers=headers).status_code == 403
    assert client.get("/api/whatever", headers=headers).status_code == 403


def test_scoped_key_cannot_escape_with_dot_segments(client, ok_backend):
    key = _create(client, allowed_paths="/mcp")["key"]
    # httpx would normalise "..", so send it percent-encoded; the gateway
    # sees the decoded path.
    response = client.get("/mcp/%2e%2e/a2a/send", headers={"X-API-Key": key})
    assert response.status_code == 403


def test_unscoped_key_reaches_every_path(client, ok_backend):
    key = _create(client)["key"]
    headers = {"X-API-Key": key}
    assert client.post("/mcp/query", headers=headers, json={}).status_code == 200
    assert client.post("/a2a/send", headers=headers, json={}).status_code == 200


def test_scope_holds_on_a_cached_key(client, ok_backend, cached_auth):
    key = _create(client, allowed_paths="/mcp,/a2a")["key"]
    headers = {"X-API-Key": key}
    assert client.post("/mcp/query", headers=headers, json={}).status_code == 200
    assert cached_auth.store, "the cache should hold the key now"
    assert client.post("/a2a/x", headers=headers, json={}).status_code == 200
    assert client.post("/agui/x", headers=headers, json={}).status_code == 403


def test_allowed_paths_as_a_json_list_in_the_body(client, ok_backend):
    response = client.post(
        "/admin/api-keys", json={"name": "body-scoped", "allowed_paths": ["/mcp"]}, headers=ADMIN
    )
    assert response.status_code == 200, response.text
    key = response.json()["key"]
    assert response.json()["allowed_paths"] == ["/mcp"]
    assert client.post("/a2a/x", headers={"X-API-Key": key}, json={}).status_code == 403


# ---------------------------------------------------------------------------
# S4: revocation takes effect with the cache on
# ---------------------------------------------------------------------------


def test_deleted_key_stops_working_at_once_with_the_cache(client, ok_backend, cached_auth):
    created = _create(client)
    headers = {"X-API-Key": created["key"]}
    assert client.post("/mcp/q", headers=headers, json={}).status_code == 200
    assert client.post("/mcp/q", headers=headers, json={}).status_code == 200  # served from cache
    assert cached_auth.store

    assert client.delete(f"/admin/api-keys/{created['id']}", headers=ADMIN).status_code == 200
    assert not cached_auth.store, "the cache entry should be gone"
    assert client.post("/mcp/q", headers=headers, json={}).status_code == 401


def test_disabled_key_stops_working_at_once_and_comes_back(client, ok_backend, cached_auth):
    created = _create(client)
    headers = {"X-API-Key": created["key"]}
    assert client.post("/mcp/q", headers=headers, json={}).status_code == 200
    assert cached_auth.store

    toggled = client.put(f"/admin/api-keys/{created['id']}/toggle", headers=ADMIN)
    assert toggled.json()["is_active"] is False
    assert client.post("/mcp/q", headers=headers, json={}).status_code == 401

    client.put(f"/admin/api-keys/{created['id']}/toggle", headers=ADMIN)
    assert client.post("/mcp/q", headers=headers, json={}).status_code == 200


def test_cache_never_holds_the_key_itself(client, ok_backend, cached_auth):
    created = _create(client)
    client.post("/mcp/q", headers={"X-API-Key": created["key"]}, json={})
    assert cached_auth.store
    for name, value in cached_auth.store.items():
        assert created["key"] not in name
        assert created["key"] not in value


def test_rate_limit_counters_are_not_named_after_the_key(client, ok_backend):
    import app.main as main

    main.rate_limiter._memory_counters.clear()
    created = _create(client)
    client.post("/mcp/q", headers={"X-API-Key": created["key"]}, json={})
    assert main.rate_limiter._memory_counters
    assert all(created["key"] not in name for name in main.rate_limiter._memory_counters)


# ---------------------------------------------------------------------------
# Hashed storage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_keys_are_stored_as_a_hash_only(db_session):
    key, db_key = await APIKeyRepository.create(db_session, name="hashed")
    row = (await db_session.execute(select(APIKeyModel).where(APIKeyModel.id == db_key.id))).scalar_one()
    assert row.key is None
    assert row.key_hash == APIKeyRepository._hash_key(key)
    assert key not in str(row.key_metadata)
    assert (await APIKeyRepository.get_by_key(db_session, key)).id == db_key.id


@pytest.mark.asyncio
async def test_legacy_plaintext_row_works_and_is_migrated_on_first_use(client, ok_backend, db_session):
    legacy_key = "sk-legacy-plaintext-key-from-an-older-release"
    db_session.add(APIKeyModel(
        id="legacy-1",
        key=legacy_key,
        key_hash=APIKeyRepository._hash_key(legacy_key),
        name="legacy",
        rate_limit_per_minute=100,
        rate_limit_per_hour=1000,
        allowed_paths=[],
        key_metadata={},
        is_active=True,
    ))
    await db_session.commit()

    assert client.post("/mcp/q", headers={"X-API-Key": legacy_key}, json={}).status_code == 200

    row = (await db_session.execute(select(APIKeyModel).where(APIKeyModel.id == "legacy-1"))).scalar_one()
    await db_session.refresh(row)
    assert row.key is None, "the plaintext copy should be dropped on first use"
    assert row.key_metadata.get("key_prefix") == legacy_key[:11]
    # Still works after migration.
    assert client.post("/mcp/q", headers={"X-API-Key": legacy_key}, json={}).status_code == 200


# ---------------------------------------------------------------------------
# B1: the list API returns ids and never the key
# ---------------------------------------------------------------------------


def test_list_returns_ids_and_prefixes_but_never_keys(client):
    created = _create(client, name="listed")
    listed = client.get("/admin/api-keys", headers=ADMIN)
    assert listed.status_code == 200
    assert created["key"] not in listed.text
    entry = next(k for k in listed.json() if k["id"] == created["id"])
    assert entry["key_prefix"] == created["key"][:11]
    assert "key" not in entry
    assert entry["name"] == "listed"


def test_ids_from_the_list_work_for_toggle_and_delete(client):
    created = _create(client, name="console-flow")
    entry = next(k for k in client.get("/admin/api-keys", headers=ADMIN).json() if k["name"] == "console-flow")
    assert client.put(f"/admin/api-keys/{entry['id']}/toggle", headers=ADMIN).status_code == 200
    assert client.delete(f"/admin/api-keys/{entry['id']}", headers=ADMIN).status_code == 200
    assert all(k["id"] != created["id"] for k in client.get("/admin/api-keys", headers=ADMIN).json())


def test_create_requires_a_name(client):
    assert client.post("/admin/api-keys", headers=ADMIN).status_code == 422
    assert client.post("/admin/api-keys", json={"rate_limit_per_minute": 5}, headers=ADMIN).status_code == 422
