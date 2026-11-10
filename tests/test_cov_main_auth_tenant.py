"""Behaviour tests for app.middleware.auth.AuthMiddleware and app.tenant.

No network, no Redis server and no .env: the database is the in-memory SQLite
session from conftest and Redis is replaced by an in-process fake.
"""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
import redis as redis_lib

from app import api_key_cache
from app.exceptions import AuthenticationError, AuthorizationError
from app.governance import IPRule, IPRuleEngine, Policy, PolicyConditions, PolicyEngine
from app.middleware import auth as auth_mod
from app.middleware.auth import AuthMiddleware, _normalise_scope_path, path_is_allowed
from app.repositories.api_key_repo import APIKeyRepository
from app.tenant import DEFAULT_QUOTA, Tenant, TenantManager, TenantService, tenant_manager
from app.time_utils import utcnow


def make_request(headers=None, path="/mcp/x", method="GET", client="203.0.113.9", with_state=True):
    req = SimpleNamespace(
        headers={k: v for k, v in (headers or {}).items()},
        url=SimpleNamespace(path=path),
        method=method,
        client=SimpleNamespace(host=client),
    )
    if with_state:
        req.state = SimpleNamespace(request_id="rid-test")
    return req


class FakeRedis:
    def __init__(self, fail=None):
        self.store, self.ttls, self.fail, self.gets = {}, {}, fail, 0

    def get(self, k):
        self.gets += 1
        if self.fail:
            raise self.fail
        return self.store.get(k)

    def setex(self, k, ttl, v):
        if self.fail:
            raise self.fail
        self.store[k] = v
        self.ttls[k] = ttl


@pytest.fixture
def mw():
    m = AuthMiddleware()
    m.redis_client = None
    return m


@pytest.fixture
def clean_governance(monkeypatch):
    import app.governance as gov
    monkeypatch.setattr(gov, "ip_rule_engine", IPRuleEngine())
    monkeypatch.setattr(gov, "policy_engine", PolicyEngine())
    return gov


# ---------------------------------------------------------------- scope paths
@pytest.mark.parametrize("raw,expected", [
    ("a//b/", "/a/b"), ("/", "/"), ("/a/./b", None), ("/a/../b", None), ("x", "/x"),
])
def test_normalise_scope_path(raw, expected):
    assert _normalise_scope_path(raw) == expected


@pytest.mark.parametrize("path,patterns,ok", [
    ("/mcp", ["/mcp"], True),
    ("/mcp/tools", ["/mcp"], True),
    ("/mcpx", ["/mcp"], False),
    ("/api/v2/x", ["/api/v*"], True),
    ("/api/x", ["/api/v*"], False),
    ("/anything", ["*"], True),
    ("/anything", ["/"], True),
    ("/anything", ["/*"], True),
    ("/mcp/a", ["/mcp/*"], True),
    ("/mcp", ["/mcp/*"], True),
    ("/files/a", ["/files/*"], True),
    ("/filesx", ["/files/*"], False),
    ("/api/v1", ["/api/*"], True),
    ("/a/../mcp", ["/mcp"], False),      # traversal refused for scoped keys
    ("/mcp", ["", "  ", None], False),   # blank patterns match nothing
    ("/mcp", ["/a/../b"], False),        # unusable pattern is skipped
    ("/data/x", ["/data/*", ], True),
    ("/pre/fix", ["/pre/f*"], True),
    ("/pre/xfix", ["/pre/f*"], False),
    ("/mcp//tools/", ["/mcp"], True),
    ("//", ["/x"], False),
])
def test_path_is_allowed(path, patterns, ok):
    assert path_is_allowed(path, patterns) is ok


def test_trailing_slash_wildcard_prefix_is_kept():
    # "/api/" + "*" must not allow "/apix".
    assert path_is_allowed("/api/v1", ["/api/*"]) is True
    assert path_is_allowed("/apix", ["/api/*"]) is False


# ---------------------------------------------------------------- headers
def test_api_key_from_headers(mw):
    assert mw._get_api_key_from_header(make_request({"X-API-Key": "k1"})) == "k1"
    assert mw._get_api_key_from_header(make_request({"Authorization": "Bearer tok"})) == "tok"
    assert mw._get_api_key_from_header(make_request({})) is None
    # X-API-Key wins over Authorization
    assert mw._get_api_key_from_header(make_request({"X-API-Key": "a", "Authorization": "Bearer b"})) == "a"


def test_bearer_token_parsing(mw):
    assert mw._get_bearer_token(make_request({"Authorization": "Bearer  abc "})) == "abc"
    assert mw._get_bearer_token(make_request({"Authorization": "bearer abc"})) == "abc"
    assert mw._get_bearer_token(make_request({"Authorization": "Bearer   "})) is None
    assert mw._get_bearer_token(make_request({"Authorization": "Basic abc"})) is None
    assert mw._get_bearer_token(make_request({})) is None


# ---------------------------------------------------------------- oauth2
async def test_oauth2_principal_built_from_stored_token(mw, monkeypatch):
    stored = SimpleNamespace(token_id="t1", provider="google", scope="a b",
                             expires_at=utcnow() + timedelta(hours=1))

    async def verify(tok):
        return stored if tok == "good" else None

    monkeypatch.setattr(auth_mod.oauth2_manager, "verify_access_token", verify)
    p = await mw._validate_oauth2_token(make_request({"Authorization": "Bearer good"}))
    assert p.key == "oauth2:t1" and p.name == "oauth2:google"
    assert p.metadata["auth_type"] == "oauth2" and p.metadata["scope"] == "a b"
    assert await mw._validate_oauth2_token(make_request({"Authorization": "Bearer bad"})) is None
    assert await mw._validate_oauth2_token(make_request({})) is None


async def test_authenticate_without_key_uses_oauth_only(mw, monkeypatch):
    assert await mw.authenticate(make_request({}), None) is None
    stored = SimpleNamespace(token_id="t2", provider="p", scope="", expires_at=utcnow() + timedelta(hours=1))

    async def verify(tok):
        return stored

    monkeypatch.setattr(auth_mod.oauth2_manager, "verify_access_token", verify)
    # A bearer value is first tried as an API key (fails), then as an OAuth2 token.
    p = await mw.authenticate(make_request({"Authorization": "Bearer whatever"}), None)
    assert p is not None and p.key == "oauth2:t2"


# ---------------------------------------------------------------- db keys
async def test_valid_db_key_authenticates_and_marks_used(mw, db_session):
    key, row = await APIKeyRepository.create(db_session, "k", allowed_paths=[])
    assert row.last_used_at is None
    principal = await mw.authenticate(make_request({"X-API-Key": key}), db_session)
    assert principal.id == row.id and principal.key == key
    await db_session.refresh(row)
    assert row.last_used_at is not None


async def test_unknown_inactive_and_expired_keys_rejected(mw, db_session):
    with pytest.raises(AuthenticationError) as ei:
        await mw.authenticate(make_request({"X-API-Key": "sk-nope"}), db_session)
    assert ei.value.status_code == 401
    assert ei.value.request_id == "rid-test"

    key, row = await APIKeyRepository.create(db_session, "inactive")
    await APIKeyRepository.toggle_active(db_session, row.id)
    with pytest.raises(AuthenticationError):
        await mw.authenticate(make_request({"X-API-Key": key}), db_session)

    key2, _ = await APIKeyRepository.create(db_session, "expired", expires_at=utcnow() - timedelta(minutes=1))
    with pytest.raises(AuthenticationError):
        await mw.authenticate(make_request({"X-API-Key": key2}), db_session)


async def test_rejected_key_falls_back_to_valid_oauth_bearer(mw, db_session, monkeypatch):
    stored = SimpleNamespace(token_id="t3", provider="p", scope="", expires_at=utcnow() + timedelta(hours=1))

    async def verify(tok):
        return stored

    monkeypatch.setattr(auth_mod.oauth2_manager, "verify_access_token", verify)
    p = await mw.authenticate(
        make_request({"X-API-Key": "sk-bad", "Authorization": "Bearer o2tok"}), db_session
    )
    assert p.key == "oauth2:t3"


async def test_db_error_falls_through_to_rejection(mw, monkeypatch):
    class Boom:
        pass

    async def boom(db, key):
        raise RuntimeError("db down")

    monkeypatch.setattr(APIKeyRepository, "get_by_key", staticmethod(boom))
    assert await mw._validate_api_key("sk-x", Boom()) is None
    assert await mw._validate_api_key("", None) is None


async def test_break_glass_secret_key(mw, monkeypatch):
    monkeypatch.setattr(auth_mod.settings, "secret_key", "real-operator-secret-value-123456")
    p = await mw._validate_api_key("real-operator-secret-value-123456", None)
    assert p.name == "default" and p.id is None
    assert await mw._validate_api_key("real-operator-secret-value-12345X", None) is None
    # shipped placeholder grants nothing, even when presented verbatim
    monkeypatch.setattr(auth_mod.settings, "secret_key", "change-me-in-production")
    assert await mw._validate_api_key("change-me-in-production", None) is None
    monkeypatch.setattr(auth_mod.settings, "secret_key", "")
    assert await mw._validate_api_key("anything", None) is None


async def test_sk_prefix_is_not_enough(mw, db_session):
    assert await mw._validate_api_key("sk-anything-at-all", db_session) is None


# ---------------------------------------------------------------- redis cache
async def test_cache_put_then_hit_skips_database(mw, db_session):
    mw.redis_client = FakeRedis()
    key, row = await APIKeyRepository.create(db_session, "cached", allowed_paths=["/mcp"])
    first = await mw._validate_api_key(key, db_session)
    cache_key = api_key_cache.cache_key_for_hash(api_key_cache.hash_key(key))
    assert cache_key in mw.redis_client.store
    blob = mw.redis_client.store[cache_key]
    assert key not in blob  # the credential itself is never written to Redis
    assert 0 < mw.redis_client.ttls[cache_key] <= api_key_cache.CACHE_TTL_SECONDS
    # Delete the row: a cached key is still served (until invalidated)...
    await db_session.delete(row)
    await db_session.commit()
    hit = await mw._validate_api_key(key, db_session)
    assert hit.key == key and hit.allowed_paths == ["/mcp"] and hit.id == first.id
    # ...and after invalidation the deleted key stops working.
    mw.redis_client.store.clear()
    assert await mw._validate_api_key(key, db_session) is None


async def test_cache_ttl_capped_by_key_expiry(mw, db_session):
    mw.redis_client = FakeRedis()
    key, _ = await APIKeyRepository.create(db_session, "short", expires_at=utcnow() + timedelta(seconds=40))
    await mw._validate_api_key(key, db_session)
    ttl = next(iter(mw.redis_client.ttls.values()))
    assert 1 <= ttl <= 40


async def test_cached_inactive_or_expired_entry_not_trusted(mw, db_session):
    mw.redis_client = FakeRedis()
    key, _ = await APIKeyRepository.create(db_session, "x")
    h = api_key_cache.hash_key(key)
    cache_key = api_key_cache.cache_key_for_hash(h)
    mw.redis_client.store[cache_key] = json.dumps(
        {"name": "x", "is_active": False, "rate_limit_per_minute": 1, "rate_limit_per_hour": 1})
    # Inactive cache entry is ignored; the database row (active) decides.
    assert (await mw._validate_api_key(key, db_session)).is_active
    mw.redis_client.store[cache_key] = json.dumps(
        {"name": "x", "expires_at": (utcnow() - timedelta(hours=1)).isoformat()})
    assert (await mw._validate_api_key(key, db_session)) is not None
    mw.redis_client.store[cache_key] = "{not json"
    assert (await mw._validate_api_key(key, db_session)) is not None


async def test_redis_errors_trip_backoff_then_recover(mw, db_session, monkeypatch):
    mw.redis_client = FakeRedis(fail=redis_lib.RedisError("down"))
    key, _ = await APIKeyRepository.create(db_session, "r")
    assert await mw._validate_api_key(key, db_session) is not None  # served from DB
    assert mw._redis_available() is False
    gets = mw.redis_client.gets
    await mw._validate_api_key(key, db_session)
    assert mw.redis_client.gets == gets  # skipped while backing off
    # after the retry window, Redis is tried again
    mw._redis_down = (mw.redis_client, 0.0)
    assert mw._redis_available() is True
    # non-Redis errors are swallowed without tripping the breaker
    mw.redis_client = FakeRedis(fail=ValueError("weird"))
    mw._redis_down = (None, 0.0)
    assert mw._cache_get("h") is None and mw._redis_available() is True
    mw._cache_put("h", SimpleNamespace(expires_at=None))  # attribute errors are swallowed
    assert mw._redis_available() is True


def test_cache_put_redis_error_trips_breaker(mw):
    mw.redis_client = FakeRedis(fail=redis_lib.RedisError("down"))
    row = SimpleNamespace(id="i", name="n", rate_limit_per_minute=1, rate_limit_per_hour=1,
                          allowed_paths=None, key_metadata={}, created_at=None,
                          expires_at=None, is_active=True)
    mw._cache_put("h", row)
    assert mw._redis_available() is False


def test_init_tolerates_bad_redis_url(monkeypatch):
    monkeypatch.setattr(auth_mod.settings, "redis_enabled", True)
    monkeypatch.setattr(auth_mod.settings, "redis_url", "redis://localhost:6379/0")
    m = AuthMiddleware()  # redis.from_url is lazy: no connection is attempted
    assert m.redis_client is not None
    monkeypatch.setattr(auth_mod.redis, "from_url", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
    assert AuthMiddleware().redis_client is None


# ---------------------------------------------------------------- tenant attach
async def test_authenticate_attaches_active_tenant_only(mw, db_session):
    key, row = await APIKeyRepository.create(db_session, "t")
    svc = TenantService()
    await svc.create_tenant(db_session, "cov-tenant-a", "A")
    await svc.associate_api_key(db_session, "cov-tenant-a", row.id, key)
    try:
        req = make_request({"X-API-Key": key})
        await mw.authenticate(req, db_session)
        assert req.state.tenant_id == "cov-tenant-a"
        assert req.state.tenant["name"] == "A"

        await svc.set_active(db_session, "cov-tenant-a", False)
        req2 = make_request({"X-API-Key": key})
        await mw.authenticate(req2, db_session)
        assert not hasattr(req2.state, "tenant_id")
    finally:
        await svc.delete(db_session, "cov-tenant-a")


async def test_authenticate_without_db_skips_tenant(mw, monkeypatch):
    monkeypatch.setattr(auth_mod.settings, "secret_key", "real-operator-secret-value-123456")
    req = make_request({"X-API-Key": "real-operator-secret-value-123456"})
    p = await mw.authenticate(req, None)
    assert p.name == "default" and not hasattr(req.state, "tenant_id")


# ---------------------------------------------------------------- allowed paths
async def test_allowed_paths_enforced_with_403(mw, db_session, clean_governance):
    key, _ = await APIKeyRepository.create(db_session, "scoped", allowed_paths=["/mcp"])
    ok = await mw.authenticate(make_request({"X-API-Key": key}, path="/mcp/tools"), db_session)
    assert ok.allowed_paths == ["/mcp"]
    with pytest.raises(AuthorizationError) as ei:
        await mw.authenticate(make_request({"X-API-Key": key}, path="/graphql"), db_session)
    assert ei.value.status_code == 403
    assert ei.value.details["allowed_paths"] == ["/mcp"] and ei.value.details["path"] == "/graphql"
    assert ei.value.request_id == "rid-test"


def test_enforce_allowed_paths_no_patterns_is_noop(mw):
    mw._enforce_allowed_paths(make_request(), SimpleNamespace(allowed_paths=[]))
    mw._enforce_allowed_paths(make_request(), SimpleNamespace())


# ---------------------------------------------------------------- governance
async def test_ip_rule_blocks_authenticated_request(mw, db_session, clean_governance):
    key, _ = await APIKeyRepository.create(db_session, "ip")
    clean_governance.ip_rule_engine._rules = [
        IPRule(id="r9", cidr="203.0.113.0/24", action="deny", scope="global",
               scope_id=None, description=None, enabled=True)]
    with pytest.raises(AuthorizationError) as ei:
        await mw.authenticate(make_request({"X-API-Key": key}, client="203.0.113.9"), db_session)
    assert ei.value.message == "Request blocked by IP rule"
    assert ei.value.details["rule_id"] == "r9" and ei.value.details["scope"] == "global"
    # An address outside the denied range is fine.
    assert await mw.authenticate(make_request({"X-API-Key": key}, client="198.51.100.4"), db_session)


async def test_policy_deny_and_rate_limit_decision(mw, db_session, clean_governance):
    key, _ = await APIKeyRepository.create(db_session, "pol")
    pe = clean_governance.policy_engine
    pe._cache = [Policy(id="p1", name="no-admin", conditions=PolicyConditions(path="/secret/*"),
                        action={"type": "deny", "reason": "nope"})]
    with pytest.raises(AuthorizationError) as ei:
        await mw.authenticate(make_request({"X-API-Key": key}, path="/secret/x"), db_session)
    assert ei.value.message == "nope" and ei.value.details["policy_id"] == "p1"
    pe._cache = [Policy(id="p2", name="deny-default", conditions=PolicyConditions(path="/d/*"),
                        action={"type": "deny"})]
    with pytest.raises(AuthorizationError) as ei:
        await mw.authenticate(make_request({"X-API-Key": key}, path="/d/x"), db_session)
    assert "deny-default" in ei.value.message

    pe._cache = [Policy(id="p3", name="slow", conditions=PolicyConditions(),
                        action={"type": "rate_limit", "requests_per_minute": 5})]
    req = make_request({"X-API-Key": key})
    await mw.authenticate(req, db_session)
    assert req.state.policy_rate_limit["requests_per_minute"] == 5
    assert req.state.policy_id == "p3"


# ---------------------------------------------------------------- hmac
def test_verify_hmac_signature(mw):
    import hashlib
    import hmac as _h

    body = b'{"a":1}'
    sig = _h.new(b"s3cret", body, hashlib.sha256).hexdigest()
    req = make_request({"X-Signature": sig})
    req._body = body
    assert mw.verify_hmac_signature(req, "s3cret") is True
    assert mw.verify_hmac_signature(req, "other") is False
    assert mw.verify_hmac_signature(make_request({}), "s3cret") is False
    # no cached body -> signature over empty body only
    empty_sig = _h.new(b"s3cret", b"", hashlib.sha256).hexdigest()
    assert mw.verify_hmac_signature(make_request({"X-Signature": empty_sig}), "s3cret") is True
    assert mw.verify_hmac_signature(make_request({"X-Signature": sig}), "s3cret") is False


# ====================================================================== tenant
def test_tenant_quota_and_dict():
    t = Tenant("t1", "One", quota={"requests_per_minute": 5, "requests_per_hour": 0})
    assert t.quota["requests_per_day"] == DEFAULT_QUOTA["requests_per_day"]
    assert t.check_quota("requests_per_minute", 5) is True
    assert t.check_quota("requests_per_minute", 6) is False
    assert t.check_quota("requests_per_hour", 10**9) is True   # 0 = unlimited
    assert t.check_quota("unknown_resource", 10**9) is True
    d = t.to_dict()
    assert d["tenant_id"] == "t1" and d["is_active"] is True and d["quota"]["requests_per_minute"] == 5


def test_tenant_manager_lifecycle():
    m = TenantManager()
    t = m.create_tenant("a", "A", config={"x": 1}, description="d")
    assert m.get_tenant("a") is t and m.get_tenant("zz") is None
    m.associate_api_key("missing", "k0")  # unknown tenant: ignored
    assert m.get_tenant_by_api_key("k0") is None
    m.associate_api_key("a", "sk-abcdefghij")
    assert m.get_tenant_by_api_key("sk-abcdefghij") is t
    m.tenant_by_api_key["dangling"] = "gone"
    assert m.get_tenant_by_api_key("dangling") is None
    m.update_tenant_quota("a", {"requests_per_minute": 7})
    m.update_tenant_quota("zz", {"requests_per_minute": 7})
    assert t.quota["requests_per_minute"] == 7
    m.update_tenant_config("a", {"y": 2})
    m.update_tenant_config("zz", {"y": 2})
    assert t.config == {"x": 1, "y": 2}
    m.deactivate_tenant("a")
    assert t.is_active is False
    m.activate_tenant("a")
    assert t.is_active is True
    m.deactivate_tenant("zz")
    m.activate_tenant("zz")
    assert [x.tenant_id for x in m.list_tenants()] == ["a"]
    m.disassociate_api_key("sk-abcdefghij")
    assert m.get_tenant_by_api_key("sk-abcdefghij") is None
    m.associate_api_key("a", "k2")
    assert m.delete_tenant("a") is True and m.delete_tenant("a") is False
    assert m.tenant_by_api_key == {"dangling": "gone"}  # associations of the deleted tenant dropped


async def test_tenant_manager_load_from_db(db_session):
    svc = TenantService()
    await svc.create_tenant(db_session, "cov-load-1", "L1", quota={"requests_per_minute": 3})
    await svc.create_tenant(db_session, "cov-load-2", "L2")
    await svc.set_active(db_session, "cov-load-2", False)
    m = TenantManager()
    m.create_tenant("stale", "Stale")
    assert await m.load_from_db(db_session) == 2
    assert set(m.tenants) == {"cov-load-1", "cov-load-2"}  # stale entry cleared
    assert m.tenants["cov-load-1"].quota["requests_per_minute"] == 3
    assert m.tenants["cov-load-2"].is_active is False
    for tid in ("cov-load-1", "cov-load-2"):
        await svc.delete(db_session, tid)


async def test_tenant_manager_load_from_db_failure_returns_zero(monkeypatch):
    from app.repositories.tenant_repo import TenantRepository

    async def boom(db, **kw):
        raise RuntimeError("no db")

    monkeypatch.setattr(TenantRepository, "list_all", staticmethod(boom))
    m = TenantManager()
    m.create_tenant("keep", "Keep")
    assert await m.load_from_db(None) == 0
    assert "keep" in m.tenants  # untouched on failure


async def test_tenant_service_crud_and_mirroring(db_session):
    svc = TenantService()
    tid = "cov-svc-1"
    try:
        created = await svc.create_tenant(db_session, tid, "Svc", description="desc",
                                          config={"a": 1}, quota={"requests_per_minute": 2})
        assert created["quota"]["requests_per_minute"] == 2
        assert tenant_manager.get_tenant(tid) is not None  # mirrored
        with pytest.raises(ValueError, match="already exists"):
            await svc.create_tenant(db_session, tid, "dup")
        assert (await svc.get_tenant(db_session, tid))["description"] == "desc"
        assert await svc.get_tenant(db_session, "nope") is None
        assert tid in [t["tenant_id"] for t in await svc.list_tenants(db_session)]

        up = await svc.update_quota(db_session, tid, {"requests_per_hour": 9})
        assert up["quota"]["requests_per_hour"] == 9
        assert tenant_manager.get_tenant(tid).quota["requests_per_hour"] == 9
        assert await svc.update_quota(db_session, "nope", {}) is None
        cfg = await svc.update_config(db_session, tid, {"b": 2})
        assert cfg["config"] == {"a": 1, "b": 2}
        assert await svc.update_config(db_session, "nope", {}) is None

        off = await svc.set_active(db_session, tid, False)
        assert off["is_active"] is False and tenant_manager.get_tenant(tid).is_active is False
        assert tid not in [t["tenant_id"] for t in await svc.list_tenants(db_session, active_only=True)]
        on = await svc.set_active(db_session, tid, True)
        assert on["is_active"] is True and tenant_manager.get_tenant(tid).is_active is True
        assert await svc.set_active(db_session, "nope", True) is None
    finally:
        assert await svc.delete(db_session, tid) is True
    assert tenant_manager.get_tenant(tid) is None
    assert await svc.delete(db_session, tid) is False


async def test_tenant_service_api_key_association_usage_and_quota(db_session):
    svc = TenantService()
    tid = "cov-svc-2"
    key, row = await APIKeyRepository.create(db_session, "assoc")
    try:
        await svc.create_tenant(db_session, tid, "Q", quota={"requests_per_minute": 2, "requests_per_hour": 100})
        assert await svc.associate_api_key(db_session, "nope", row.id, key) is False
        assert await svc.associate_api_key(db_session, tid, row.id, key) is True
        assert tenant_manager.get_tenant_by_api_key(key).tenant_id == tid
        assert (await svc.get_tenant_for_api_key(db_session, key))["tenant_id"] == tid
        assert await svc.get_tenant_for_api_key(db_session, "sk-unlinked") is None

        assert await svc.check_quota(db_session, tid, "minute") is True
        await svc.record_request(db_session, tid)
        await svc.record_request(db_session, tid, is_error=True, bytes_in=3, bytes_out=5)
        usage = await svc.usage(db_session, tid)
        cur = usage["current"]["minute"]
        assert (cur["request_count"], cur["error_count"], cur["bytes_in"], cur["bytes_out"]) == (2, 1, 3, 5)
        assert usage["history"] and usage["history"][0]["window"] == "hour"
        # 2 used of 2 per minute: the next request would exceed it; the hour window still has room
        assert await svc.check_quota(db_session, tid, "minute") is False
        assert await svc.check_quota(db_session, tid, "hour") is True
        assert await svc.check_quota(db_session, tid, "day") is True

        await svc.set_active(db_session, tid, False)
        assert await svc.check_quota(db_session, tid, "hour") is False  # inactive tenant has no quota
        assert await svc.check_quota(db_session, "nope") is False

        assert await svc.disassociate_api_key(db_session, row.id) is True
        assert await svc.disassociate_api_key(db_session, row.id) is False
        assert await svc.get_tenant_for_api_key(db_session, key) is None
    finally:
        await svc.delete(db_session, tid)


async def test_valid_key_is_accepted_when_recording_last_use_fails(mw, db_session, monkeypatch):
    """A failed 'last used' write (SQLite lock under load) must not turn a valid key into a 401."""
    key, _ = await APIKeyRepository.create(db=db_session, name="lock-test")

    async def locked(db, db_key):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(APIKeyRepository, "mark_used", staticmethod(locked))
    principal = await mw._validate_api_key(key, db_session)
    assert principal is not None and principal.key == key and principal.is_active


async def test_inactive_key_is_still_rejected_when_recording_last_use_fails(mw, db_session, monkeypatch):
    key, model = await APIKeyRepository.create(db=db_session, name="inactive-lock-test")
    model.is_active = False
    await db_session.commit()

    async def locked(db, db_key):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(APIKeyRepository, "mark_used", staticmethod(locked))
    assert await mw._validate_api_key(key, db_session) is None
