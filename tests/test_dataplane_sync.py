import json
"""The snapshot the Rust data plane reads: what it contains, when a key may be served natively, and who may read it."""
import hashlib

import pytest
from fastapi import HTTPException

from app import dataplane_sync
from app.config import get_settings
from app.db_models import IPRuleModel, PolicyModel, TenantAPIKeyModel
from app.repositories.api_key_repo import APIKeyRepository
from app.repositories.tenant_repo import TenantRepository


@pytest.fixture(autouse=True)
def _fresh_cache():
    dataplane_sync.reset_cache()
    yield
    dataplane_sync.reset_cache()


@pytest.fixture(autouse=True)
def _rules_as_found():
    """The IP rules and policies these tests load live in engines the whole process shares; they
    are put back, so that a test file run after this one does not meet them."""
    from app.governance import ip_rule_engine, policy_engine

    rules, policies, loaded = list(ip_rule_engine._rules), list(policy_engine._cache), policy_engine._loaded
    yield
    ip_rule_engine._rules = rules
    policy_engine._cache, policy_engine._loaded = policies, loaded


async def _key(db, name="k", **kw):
    plaintext, model = await APIKeyRepository.create(db=db, name=name, **kw)
    return plaintext, model


def _by_id(snap, key_id):
    return next(k for k in snap["keys"] if k["id"] == key_id)


async def test_snapshot_holds_digests_never_plaintext(db_session):
    plaintext, model = await _key(db_session, rate_limit_per_minute=7, rate_limit_per_hour=70)
    snap = await dataplane_sync.build_snapshot(db_session)
    entry = _by_id(snap, model.id)
    assert entry["hash"] == hashlib.sha256(plaintext.encode()).hexdigest()
    assert (entry["rate_limit_per_minute"], entry["rate_limit_per_hour"]) == (7, 70)
    assert entry["native_ok"] is True
    assert plaintext not in str(snap)


async def test_a_revoked_key_leaves_the_snapshot_and_the_revision_changes(db_session):
    _, model = await _key(db_session)
    before = await dataplane_sync.build_snapshot(db_session)
    assert _by_id(before, model.id)
    await APIKeyRepository.toggle_active(db_session, model.id)  # disable
    after = await dataplane_sync.build_snapshot(db_session)
    assert all(k["id"] != model.id for k in after["keys"])
    assert after["revision"] != before["revision"]


async def test_the_revision_is_stable_while_nothing_changes(db_session):
    await _key(db_session)
    assert (await dataplane_sync.build_snapshot(db_session))["revision"] == (
        await dataplane_sync.build_snapshot(db_session)
    )["revision"]


async def _tenant_key(db, tenant_id="t1", quota=None, name="tenant-key"):
    plaintext, model = await _key(db, name=name)
    await TenantRepository.create(db, tenant_id, tenant_id.upper(), quota=quota)
    db.add(TenantAPIKeyModel(tenant_id=tenant_id, api_key_id=model.id,
                             api_key_hash=hashlib.sha256(plaintext.encode()).hexdigest()))
    await db.commit()
    return plaintext, model


async def test_a_tenant_key_is_native_and_carries_its_tenants_quota(db_session):
    quota = {"requests_per_minute": 5, "requests_per_hour": 0}
    _, model = await _tenant_key(db_session, quota=quota)
    _, other = await _key(db_session, name="plain-key")
    snap = await dataplane_sync.build_snapshot(db_session)
    entry = _by_id(snap, model.id)
    assert entry["native_ok"] is True and entry["tenant"] == "t1"
    assert "tenant" not in _by_id(snap, other.id)
    # Zero means unlimited, as in check_quota; the refusal's details are rendered here, once.
    assert snap["tenants"]["t1"] == {
        "minute": 5, "hour": None,
        "details_json": '{"tenant_id":"t1","quota":{"requests_per_minute":5,"requests_per_hour":0,'
                        '"requests_per_day":100000,"storage_mb":1000}}',
    }


async def test_an_inactive_tenants_key_is_served_as_a_key_without_a_tenant(db_session):
    _, model = await _tenant_key(db_session)
    await TenantRepository.set_active(db_session, "t1", False)
    entry = _by_id(await dataplane_sync.build_snapshot(db_session), model.id)
    assert entry["native_ok"] is True and "tenant" not in entry


@pytest.mark.parametrize("quota", [
    {"requests_per_minute": "5"}, {"requests_per_minute": 2.5}, {"requests_per_hour": True},
])
async def test_a_quota_that_is_not_a_whole_number_stays_with_python(db_session, quota):
    _, model = await _tenant_key(db_session, quota=quota)
    snap = await dataplane_sync.build_snapshot(db_session)
    assert _by_id(snap, model.id)["native_ok"] is False and "t1" not in snap["tenants"]


async def test_a_key_linked_to_a_tenant_under_another_digest_stays_with_python(db_session):
    _, model = await _key(db_session)
    await TenantRepository.create(db_session, "t1", "T1")
    db_session.add(TenantAPIKeyModel(tenant_id="t1", api_key_id=model.id, api_key_hash="0" * 64))
    await db_session.commit()
    assert _by_id(await dataplane_sync.build_snapshot(db_session), model.id)["native_ok"] is False


async def test_ip_rules_are_compiled_in_evaluation_order_and_keys_stay_native(db_session):
    from datetime import datetime

    _, a = await _key(db_session, name="a")
    db_session.add_all([
        IPRuleModel(id="r2", cidr="10.0.0.0/8", action="allow", scope="api_key", scope_id=a.id, enabled=True,
                    created_at=datetime(2026, 1, 2)),
        IPRuleModel(id="r1", cidr="203.0.113.7/24", action="deny", scope="global", enabled=True,
                    created_at=datetime(2026, 1, 1)),
        IPRuleModel(id="r3", cidr="not-a-network", action="deny", scope="global", enabled=True,
                    created_at=datetime(2026, 1, 3)),
        # Ignored by the engine, so not sent: disabled, no scope id, an unknown action, an unknown scope.
        IPRuleModel(id="r4", cidr="10.0.0.0/8", action="deny", scope="global", enabled=False),
        IPRuleModel(id="r5", cidr="10.0.0.0/8", action="deny", scope="tenant", scope_id=None, enabled=True),
        IPRuleModel(id="r6", cidr="10.0.0.0/8", action="log", scope="global", enabled=True),
        IPRuleModel(id="r7", cidr="10.0.0.0/8", action="deny", scope="team", scope_id="x", enabled=True),
    ])
    await db_session.commit()
    snap = await dataplane_sync.build_snapshot(db_session)
    assert _by_id(snap, a.id)["native_ok"] is True
    assert snap["ip_rules"] == [
        {"scope": "global", "scope_id": None, "allow": False, "net": "203.0.113.0/24",
         "details_json": '{"rule_id":"r1","scope":"global"}'},
        {"scope": "api_key", "scope_id": a.id, "allow": True, "net": "10.0.0.0/8",
         "details_json": '{"rule_id":"r2","scope":"api_key"}'},
        {"scope": "global", "scope_id": None, "allow": False, "net": None,
         "details_json": '{"rule_id":"r3","scope":"global"}'},
    ]


async def test_a_disabled_ip_rule_changes_nothing(db_session):
    _, a = await _key(db_session)
    before = await dataplane_sync.build_snapshot(db_session)
    db_session.add(IPRuleModel(cidr="203.0.113.0/24", action="deny", scope="global", enabled=False))
    await db_session.commit()
    after = await dataplane_sync.build_snapshot(db_session)
    assert after["ip_rules"] == [] and after["revision"] == before["revision"]


async def test_policies_are_compiled_in_priority_order(db_session):
    _, a = await _key(db_session)
    db_session.add_all([
        PolicyModel(id="p2", name="late", priority=50, enabled=True, conditions={}, action={"type": "ALLOW"}),
        PolicyModel(id="p1", name="office hours", priority=10, enabled=True, conditions={
            "path": "/api/admin/*", "methods": ["post", "Delete"], "tenants": ["t1"],
            "cidrs": ["198.51.100.9/24", "nonsense"], "time_of_day": {"start": "22:30", "end": "06:00"},
        }, action={"type": "deny"}),
        PolicyModel(id="p3", name="said why", priority=60, enabled=True, conditions={"methods": ["GET"]},
                    action={"type": "deny", "reason": "no \"reads\" today"}),
        PolicyModel(id="p4", name="off", priority=1, enabled=False, conditions={}, action={"type": "deny"}),
        # API-key requests carry no role, so this one can never match them and is left out.
        PolicyModel(id="p5", name="admins", priority=5, enabled=True, conditions={"roles": ["admin"]},
                    action={"type": "deny"}),
    ])
    await db_session.commit()
    snap = await dataplane_sync.build_snapshot(db_session)
    assert _by_id(snap, a.id)["native_ok"] is True
    assert snap["policies"] == [
        {"path": "/api/admin/*", "methods": ["POST", "DELETE"], "tenants": ["t1"], "cidrs": ["198.51.100.0/24"],
         "time": [22 * 60 + 30, 6 * 60], "deny": True,
         "message_json": '"denied by policy \'office hours\'"', "details_json": '{"policy_id":"p1"}'},
        {"path": None, "methods": [], "tenants": [], "cidrs": None, "time": None, "deny": False,
         "message_json": '"denied by policy \'late\'"', "details_json": '{"policy_id":"p2"}'},
        {"path": None, "methods": ["GET"], "tenants": [], "cidrs": None, "time": None, "deny": True,
         "message_json": '"no \\"reads\\" today"', "details_json": '{"policy_id":"p3"}'},
    ]


@pytest.mark.parametrize("conditions", [
    {"headers": {"x-team": "^ops$"}},   # matching on headers is not reproduced
    {"path": "/api/[ab]*"},             # nor are character classes in a path pattern
    {"methods": "GET"},
])
async def test_a_policy_the_data_plane_cannot_evaluate_sends_every_key_to_python(db_session, conditions):
    _, a = await _key(db_session, name="a")
    _, b = await _key(db_session, name="b")
    db_session.add(PolicyModel(name="p1", conditions=conditions, action={"type": "deny"}, enabled=True))
    await db_session.commit()
    snap = await dataplane_sync.build_snapshot(db_session)
    assert not _by_id(snap, a.id)["native_ok"] and not _by_id(snap, b.id)["native_ok"]
    assert snap["policies"] == [] and snap["ip_rules"] == []


async def test_a_policy_with_unreadable_times_is_not_restricted_by_time(db_session):
    await _key(db_session)
    db_session.add(PolicyModel(id="p1", name="p1", enabled=True, action={"type": "deny"},
                               conditions={"time_of_day": {"start": "25:00", "end": "06:00"}}))
    await db_session.commit()
    assert (await dataplane_sync.build_snapshot(db_session))["policies"][0]["time"] is None


async def test_allowed_paths_are_compiled_as_path_is_allowed_reads_them(db_session):
    from app.middleware.auth import path_is_allowed

    patterns = ["/api/users/*", " /api/orders ", "/api/inv*", "/api/files/*x*", "", "api/v1/"]
    _, a = await _key(db_session, name="scoped", allowed_paths=patterns)
    _, everything = await _key(db_session, name="star", allowed_paths=["/api/x", "/*"])
    _, plain = await _key(db_session, name="plain")
    snap = await dataplane_sync.build_snapshot(db_session)
    scope = _by_id(snap, a.id)["allowed_paths"]
    assert _by_id(snap, a.id)["native_ok"] is True
    assert scope["rules"] == [
        ["under", "/api/users"], ["under", "/api/orders"], ["prefix", "/api/inv"], ["prefix", "/api/files/*x"],
        ["under", "/api/v1"],
    ]
    assert scope["patterns_json"] == '["/api/users/*"," /api/orders ","/api/inv*","/api/files/*x*","","api/v1/"]'
    assert _by_id(snap, everything.id)["allowed_paths"]["rules"] == [["all"]]
    assert "allowed_paths" not in _by_id(snap, plain.id)

    # The compiled form decides exactly as the gateway's own check does.
    def compiled(path):
        from app.middleware.auth import _normalise_scope_path

        n = _normalise_scope_path(path or "/")
        if n is None:
            return False
        return any(
            r[0] == "all" or (r[0] == "prefix" and n.startswith(r[1]))
            or (r[0] == "under" and (n == r[1] or n.startswith(r[1] + "/")))
            for r in scope["rules"]
        )

    for path in ["/api/users", "/api/users/1", "/api/usersx", "/api/orders/9", "/api/inventory", "/api/in",
                 "/api/files/*x/1", "/api/files/a", "/api/v1", "/api/v1/z", "/api/../admin", "//api//users/", "/"]:
        assert compiled(path) == path_is_allowed(path, patterns), path


async def test_tenant_usage_reported_by_the_data_plane_counts_against_the_quota(db_session, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(get_settings(), "dataplane_token", "s3cret")
    _, model = await _tenant_key(db_session, quota={"requests_per_minute": 5})
    now = datetime.now(timezone.utc)
    minute = int(now.replace(second=0, microsecond=0).timestamp())
    out = await dataplane_sync.report(dataplane_sync.DataplaneReport(tenant_usage=[
        dataplane_sync.ReportedTenantUsage(tenant_id="t1", minute=minute, requests=3, errors=1, bytes_out=90),
        dataplane_sync.ReportedTenantUsage(tenant_id="t1", minute=minute - 3600 * 5, requests=40),
        dataplane_sync.ReportedTenantUsage(tenant_id="t1", minute=minute, requests=0),
    ]), x_dataplane_token="s3cret", db=db_session)
    assert out["tenant_requests"] == 43
    full = await dataplane_sync.snapshot(since=None, x_dataplane_token="s3cret", db=db_session)
    usage = full["tenant_usage"]
    # Only the three served in the current minute count now; the forty belong to an hour long past.
    assert usage["minute_start"] == minute and usage["used"]["t1"] == [3, 3]
    same = await dataplane_sync.snapshot(since=full["revision"], x_dataplane_token="s3cret", db=db_session)
    assert same.pop("ready")["status"] in (200, 503)
    assert same == {"unchanged": True, "revision": full["revision"], "tenant_usage": usage}
    tenant = await TenantRepository.get_by_id(db_session, "t1")
    assert await TenantRepository.check_quota(db_session, tenant, "minute", 2)
    assert not await TenantRepository.check_quota(db_session, tenant, "minute", 3)


async def test_the_snapshot_names_the_paths_this_gateway_answers_itself(db_session, monkeypatch):
    """With them, the data plane can send every other path with no prefix to the HTTP backend."""
    s = get_settings()
    monkeypatch.setattr(s, "http_backend_url", "http://backend.internal:9100")
    own = (await dataplane_sync.build_snapshot(db_session))["own_paths"]
    for path in ("/admin", "/oauth2", "/internal/dataplane", "/agents/", "/docs", "/openapi.json", s.health_check_path,
                 s.metrics_path, "/health/detailed", "/ready", "/live", "/mqtt/publish", "/mqtt/subscribe", "/ws/",
                 "/.well-known/agent-card.json", "/.well-known/ag-ui.json"):
        assert path in own, path
    # Every route but the catch-all is covered, so a route added later is too.
    from app.main import app, gateway_proxy
    for route in app.routes:
        if getattr(route, "endpoint", None) is not gateway_proxy:
            assert any(route.path.startswith(p) for p in own), route.path
    assert "/" not in own and "" not in own
    # Without an HTTP backend those paths go to the MCP backend: this gateway's to route.
    monkeypatch.setattr(s, "http_backend_url", None)
    assert (await dataplane_sync.build_snapshot(db_session))["own_paths"] is None


async def test_a_request_altering_plugin_sends_every_path_to_python(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.plugins import PluginType, plugin_registry

    await _key(db_session)
    assert (await dataplane_sync.build_snapshot(db_session))["fallthrough_prefixes"] == []
    auth_only = SimpleNamespace(name="a", enabled=True, plugin_type=PluginType.AUTH_PROVIDER)
    hook = SimpleNamespace(name="h", enabled=True, plugin_type=PluginType.HOOK)
    off = SimpleNamespace(name="o", enabled=False, plugin_type=PluginType.HOOK)
    monkeypatch.setattr(plugin_registry, "list", lambda: [auth_only, off])
    assert (await dataplane_sync.build_snapshot(db_session))["fallthrough_prefixes"] == []
    monkeypatch.setattr(plugin_registry, "list", lambda: [auth_only, hook])
    assert (await dataplane_sync.build_snapshot(db_session))["fallthrough_prefixes"] == ["/"]


async def test_the_stock_request_id_plugin_does_not_block_native_serving(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.plugins import PluginType, plugin_registry

    await _key(db_session)
    stock = SimpleNamespace(name="request-id", enabled=True, plugin_type=PluginType.TRANSFORM)
    monkeypatch.setattr(plugin_registry, "list", lambda: [stock])
    snap = await dataplane_sync.build_snapshot(db_session)
    assert snap["fallthrough_prefixes"] == [] and snap["inject_request_id"] is True
    stock.enabled = False
    dataplane_sync.reset_cache()
    snap = await dataplane_sync.build_snapshot(db_session)
    assert snap["fallthrough_prefixes"] == [] and snap["inject_request_id"] is False


async def test_the_snapshot_says_whether_versions_are_taken_off_the_path(db_session, monkeypatch):
    from app.versioning import version_manager

    assert (await dataplane_sync.build_snapshot(db_session))["api_versions"]["strip_prefix"] is False
    monkeypatch.setattr(version_manager, "strip_prefix_on_proxy", True)
    assert (await dataplane_sync.build_snapshot(db_session))["api_versions"]["strip_prefix"] is True


async def test_the_snapshot_says_what_a_version_announces(db_session):
    """A version on its way out is still the data plane's to serve, with what the gateway
    announces for it. One that is gone, about to go, routed elsewhere, or dated in a way only
    this side compares, is not."""
    from datetime import datetime, timedelta, timezone

    from app.time_utils import utcnow
    from app.versioning import VersionStatus, version_manager

    async def told():
        versions = (await dataplane_sync.build_snapshot(db_session))["api_versions"]["versions"]
        return {v["version"]: v for v in versions}["9"]

    version_manager.register_version("9")
    v = version_manager.versions["9"]
    try:
        assert await told() == {"version": "9", "native_ok": True}
        v.status, v.deprecated_date = VersionStatus.DEPRECATED, utcnow()
        assert await told() == {"version": "9", "native_ok": True, "notices": {
            "warning": '299 - "API version 9 is deprecated"', "sunset": None, "deprecation": True}}
        # With a sunset far enough off, the date is announced too.
        v.status, v.sunset_date = VersionStatus.SUNSET, datetime(2099, 1, 2, 3, 4, 5)
        assert await told() == {"version": "9", "native_ok": True, "notices": {
            "warning": '299 - "API version 9 is deprecated", date="2099-01-02T03:04:05"',
            "sunset": "Fri, 02 Jan 2099 03:04:05 -0000", "deprecation": True}}
        # A deprecation date alone is announced to the caller alone.
        v.status, v.sunset_date = VersionStatus.ACTIVE, None
        assert (await told())["notices"] == {"warning": None, "sunset": None, "deprecation": True}
        # Within a minute of the sunset, and after it, the version is this side's.
        v.status, v.sunset_date = VersionStatus.SUNSET, utcnow() + timedelta(seconds=dataplane_sync.SUNSET_MARGIN_SECONDS - 5)
        assert await told() == {"version": "9", "native_ok": False}
        v.sunset_date = utcnow() - timedelta(days=1)
        assert await told() == {"version": "9", "native_ok": False}
        # A date with an offset is compared here alone.
        v.sunset_date = datetime(2099, 1, 2, tzinfo=timezone.utc)
        assert await told() == {"version": "9", "native_ok": False}
        v.sunset_date, v.status = None, VersionStatus.REMOVED
        assert await told() == {"version": "9", "native_ok": False}
        v.status, v.backend_url = VersionStatus.DEPRECATED, "http://127.0.0.1:9"
        assert await told() == {"version": "9", "native_ok": False}
    finally:
        version_manager.versions.pop("9", None)


def test_the_endpoint_does_not_exist_without_a_token(monkeypatch):
    monkeypatch.setattr(get_settings(), "dataplane_token", "")
    with pytest.raises(HTTPException) as e:
        dataplane_sync._authorize("anything")
    assert e.value.status_code == 404


def test_a_wrong_or_missing_token_is_refused(monkeypatch):
    monkeypatch.setattr(get_settings(), "dataplane_token", "s3cret")
    for bad in (None, "", "nope"):
        with pytest.raises(HTTPException) as e:
            dataplane_sync._authorize(bad)
        assert e.value.status_code == 403
    dataplane_sync._authorize("s3cret")


async def test_since_the_current_revision_answers_unchanged(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "dataplane_token", "s3cret")
    await _key(db_session)
    full = await dataplane_sync.snapshot(since=None, x_dataplane_token="s3cret", db=db_session)
    assert "keys" in full
    same = await dataplane_sync.snapshot(since=full["revision"], x_dataplane_token="s3cret", db=db_session)
    assert same.pop("ready")["status"] in (200, 503)
    assert same == {"unchanged": True, "revision": full["revision"], "tenant_usage": full["tenant_usage"]}
    assert full["tenant_usage"]["used"] == {}
    stale = await dataplane_sync.snapshot(since="old", x_dataplane_token="s3cret", db=db_session)
    assert "keys" in stale


def test_the_route_is_registered_and_hidden_from_the_schema():
    from app.main import app

    paths = {r.path for r in app.routes}
    assert "/internal/dataplane/snapshot" in paths
    assert "/internal/dataplane/snapshot" not in app.openapi().get("paths", {})


# --- reports from the data plane ------------------------------------------------------------------

def _sample(name, **labels):
    from prometheus_client import REGISTRY

    v = REGISTRY.get_sample_value(name, labels)
    return 0.0 if v is None else v


def _agg(status=200, path="/api/users/42", n=1000, secs=2.0, bucket=0, **kw):
    buckets = [[0, 0.0] for _ in range(9)]
    buckets[bucket] = [n, secs]
    return dataplane_sync.ReportedAggregate(method="GET", path=path, status=status, buckets=buckets, **kw)


def test_counts_and_latency_are_applied_exactly():
    from app import main as gw

    label = gw.obs_metric_path_label("/api/users/42")
    before_c = _sample("gateway_requests_total", method="GET", path=label, status_code="200", protocol="http")
    before_n = _sample("gateway_request_duration_seconds_count", method="GET", path=label, protocol="http")
    before_s = _sample("gateway_request_duration_seconds_sum", method="GET", path=label, protocol="http")
    before_native = _sample("gateway_dataplane_native_requests_total", status_class="2xx")
    out = dataplane_sync.apply_report(dataplane_sync.DataplaneReport(aggregates=[_agg(n=1000, secs=2.0)]))
    assert out["requests"] == 1000
    assert _sample("gateway_requests_total", method="GET", path=label, status_code="200", protocol="http") - before_c == 1000
    assert _sample("gateway_request_duration_seconds_count", method="GET", path=label, protocol="http") - before_n == 1000
    assert abs(_sample("gateway_request_duration_seconds_sum", method="GET", path=label, protocol="http") - before_s - 2.0) < 1e-6
    assert _sample("gateway_dataplane_native_requests_total", status_class="2xx") - before_native == 1000


def test_latency_lands_in_the_right_histogram_bucket():
    from app import main as gw

    label = gw.obs_metric_path_label("/api/orders/9")
    key = dict(method="GET", path=label, protocol="http")
    b01 = _sample("gateway_request_duration_seconds_bucket", le="0.01", **key)
    b05 = _sample("gateway_request_duration_seconds_bucket", le="0.05", **key)
    dataplane_sync.apply_report(dataplane_sync.DataplaneReport(
        aggregates=[_agg(path="/api/orders/9", n=10, secs=0.3, bucket=1)]  # ten requests of 30 ms
    ))
    assert _sample("gateway_request_duration_seconds_bucket", le="0.01", **key) - b01 == 0
    assert _sample("gateway_request_duration_seconds_bucket", le="0.05", **key) - b05 == 10


def test_a_rate_limited_batch_feeds_the_rate_limit_metric():
    before = _sample("gateway_rate_limit_hits_total", client_id="key-xyz")
    dataplane_sync.apply_report(dataplane_sync.DataplaneReport(
        aggregates=[_agg(status=429, n=7, secs=0.007, rejected=True, client_id="key-xyz")]
    ))
    assert _sample("gateway_rate_limit_hits_total", client_id="key-xyz") - before == 7


def test_errors_reach_the_red_error_counter():
    from app import main as gw

    route = gw.obs_normalize_route(gw.obs_metric_path_label("/api/boom"))
    before = _sample("gateway_red_errors_total", method="GET", route=route, protocol="http", tenant="_", error_class="http_502")
    dataplane_sync.apply_report(dataplane_sync.DataplaneReport(aggregates=[_agg(status=502, path="/api/boom", n=4, secs=0.04)]))
    assert _sample("gateway_red_errors_total", method="GET", route=route, protocol="http", tenant="_", error_class="http_502") - before == 4


def test_rows_go_to_the_request_log_sink_and_skips_are_counted(monkeypatch):
    from app import main as gw

    seen = []
    monkeypatch.setattr(gw.request_log_writer, "submit", lambda entry, key_id: seen.append((entry, key_id)))
    before = _sample("gateway_dataplane_rows_skipped_total")
    out = dataplane_sync.apply_report(dataplane_sync.DataplaneReport(
        rows=[dataplane_sync.ReportedRow(request_id="r1", method="GET", path="/api/x", client_ip="10.0.0.9",
                                         api_key_id="k-1", status=200, duration_ms=3.5, user_agent="curl/8")],
        rows_skipped=41,
    ))
    assert out["rows"] == 1 and out["rows_skipped"] == 41
    (entry, key_id), = seen
    assert key_id == "k-1" and entry.protocol == "http" and entry.status_code == 200
    assert entry.client_ip == "10.0.0.9" and entry.response_time_ms == 3.5 and entry.api_key is None
    assert _sample("gateway_dataplane_rows_skipped_total") - before == 41


async def test_a_report_needs_the_token(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "dataplane_token", "s3cret")
    with pytest.raises(HTTPException) as e:
        await dataplane_sync.report(dataplane_sync.DataplaneReport(), x_dataplane_token="nope", db=db_session)
    assert e.value.status_code == 403


async def test_last_used_moves_forward_and_never_back(db_session):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.db_models import APIKeyModel

    _, model = await _key(db_session)
    later = int(datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc).timestamp())
    assert await dataplane_sync.apply_key_usage(db_session, [dataplane_sync.ReportedKeyUse(id=model.id, at=later)]) == 1
    row = (await db_session.execute(select(APIKeyModel).where(APIKeyModel.id == model.id))).scalars().one()
    assert row.last_used_at == datetime(2026, 11, 10, 12, 0)
    # An older report changes nothing; an unknown key is ignored.
    assert await dataplane_sync.apply_key_usage(db_session, [
        dataplane_sync.ReportedKeyUse(id=model.id, at=later - 3600),
        dataplane_sync.ReportedKeyUse(id="no-such-key", at=later),
    ]) == 0


async def test_the_snapshot_carries_the_breaker_settings_and_the_upstream_timeout(db_session, monkeypatch):
    s = get_settings()
    first = await dataplane_sync.build_snapshot(db_session)
    assert first["circuit_breaker"] == {
        "enabled": bool(s.circuit_breaker_enabled),
        "failure_threshold": s.circuit_breaker_failure_threshold,
        "timeout_seconds": s.circuit_breaker_timeout,
    }
    assert first["upstream_timeout_seconds"] == float(s.default_timeout)
    monkeypatch.setattr(s, "circuit_breaker_failure_threshold", s.circuit_breaker_failure_threshold + 1)
    dataplane_sync.reset_cache()
    assert (await dataplane_sync.build_snapshot(db_session))["revision"] != first["revision"]


@pytest.fixture
def announced(monkeypatch):
    """Record the webhooks and alerts a report's events cause, in place of delivering them."""
    from app.notifications import notification_manager
    from app.webhooks import webhook_manager

    seen = {"webhooks": [], "breaker_alerts": [], "limit_alerts": []}

    async def webhook(event_type, payload, filters=None):
        seen["webhooks"].append((event_type, payload))
        return []

    async def breaker_alert(**kw):
        seen["breaker_alerts"].append(kw)

    async def limit_alert(**kw):
        seen["limit_alerts"].append(kw)

    monkeypatch.setattr(webhook_manager, "trigger_webhook", webhook)
    monkeypatch.setattr(notification_manager, "send_circuit_breaker_alert", breaker_alert)
    monkeypatch.setattr(notification_manager, "send_rate_limit_alert", limit_alert)
    return seen


async def _settle():
    import asyncio

    while dataplane_sync._background:
        await asyncio.gather(*list(dataplane_sync._background))


async def test_a_breaker_opening_in_the_data_plane_is_announced_like_our_own(db_session, announced):
    event = dataplane_sync.ReportedEvent(
        type="circuit_breaker", state="open", previous_state="closed", backend_url="http://b:9",
        failure_count=5, failure_threshold=5, opened_at=1762776000.5, timeout_seconds=60,
    )
    assert await dataplane_sync.apply_events(db_session, [event]) == 1
    await _settle()
    assert announced["webhooks"] == [("circuit_breaker_open", {
        "backend_url": "http://b:9", "state": "open", "previous_state": "closed", "failure_count": 5,
        "failure_threshold": 5, "opened_at": 1762776000.5, "timeout_seconds": 60,
        "message": "Circuit breaker opened after 5 failures",
    })]
    assert announced["breaker_alerts"] == [{
        "backend_url": "http://b:9", "state": "open", "previous_state": "closed", "failure_count": 5,
        "message": "Circuit breaker opened after 5 failures",
    }]


async def test_a_breaker_closing_in_the_data_plane_is_announced_like_our_own(db_session, announced):
    event = dataplane_sync.ReportedEvent(
        type="circuit_breaker", state="closed", previous_state="half-open", backend_url="http://b:9",
    )
    assert await dataplane_sync.apply_events(db_session, [event]) == 1
    await _settle()
    assert announced["webhooks"] == [("circuit_breaker_closed", {
        "backend_url": "http://b:9", "state": "closed", "previous_state": "half-open", "failure_count": 0,
        "message": "Circuit breaker closed after successful request",
    })]
    assert announced["breaker_alerts"][0]["state"] == "closed"


async def test_a_native_rate_limit_refusal_is_announced_with_the_keys_name(db_session, announced):
    _, model = await _key(db_session, name="billing-service")
    event = dataplane_sync.ReportedEvent(
        type="rate_limit_exceeded", api_key_id=model.id, client_ip="198.51.100.7", limit=3, window="minute",
        path="/api/users", count=41,
    )
    assert await dataplane_sync.apply_events(db_session, [event]) == 1
    await _settle()
    assert announced["webhooks"] == [("rate_limit_exceeded", {
        "api_key_id": model.id, "api_key_name": "billing-service", "client_ip": "198.51.100.7", "limit": 3,
        "window": "minute", "path": "/api/users", "count": 41,
    })]
    assert announced["limit_alerts"] == [{
        "api_key_id": model.id, "client_ip": "198.51.100.7", "limit": 3, "window": "minute", "path": "/api/users",
    }]


async def test_an_event_of_an_unknown_kind_is_ignored_not_refused(db_session, announced):
    events = [dataplane_sync.ReportedEvent(type="something_newer"),
              dataplane_sync.ReportedEvent(type="circuit_breaker", state="half-open")]
    assert await dataplane_sync.apply_events(db_session, events) == 0
    await _settle()
    assert announced == {"webhooks": [], "breaker_alerts": [], "limit_alerts": []}


# --- MCP, A2A, AG-UI and ACP: backends, body checks and labels ------------------------------------


async def test_the_snapshot_names_each_protocols_backend(db_session, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "mcp_backend_url", "http://mcp.internal:9000")
    monkeypatch.setattr(s, "a2a_backend_url", "http://a2a.internal:9001/")
    monkeypatch.setattr(s, "ag_ui_backend_url", None)
    monkeypatch.setattr(s, "acp_backend_url", "")
    monkeypatch.setattr(s, "graphql_backend_url", None)
    snap = await dataplane_sync.build_snapshot(db_session)
    assert snap["backends"] == {
        "mcp": "http://mcp.internal:9000", "a2a": "http://a2a.internal:9001/", "ag_ui": None, "acp": None, "graphql": None,
    }
    # The GraphQL backend is named without the `/graphql` the request's own path supplies.
    for configured, named in (("http://gql:4000", "http://gql:4000"), ("http://gql:4000/graphql/", "http://gql:4000"),
                              ("http://gql:4000/v1/graphql", "http://gql:4000/v1")):
        monkeypatch.setattr(s, "graphql_backend_url", configured)
        assert (await dataplane_sync.build_snapshot(db_session))["backends"]["graphql"] == named


async def test_the_snapshot_carries_the_body_checks_as_they_are_shipped(db_session, monkeypatch):
    from app.tier1_registry import tier1_registry

    # Other tests register tools; the shipped state has none.
    monkeypatch.setattr(tier1_registry, "tool_input_schema_index", classmethod(lambda cls: {}))
    snap = await dataplane_sync.build_snapshot(db_session)
    assert snap["boundary"] == {
        "mcp": {
            "enabled": True, "max_body_bytes": 2_097_152, "blocked_methods": [], "allowed_methods": [],
            "block_injection": True, "default_patterns": True, "schema_tools": [], "tool_schemas": {},
        },
        "a2a": {"enabled": True, "enforce_send_message_shape": True},
        "acp": {"enabled": True},
        "ag_ui": {"enabled": True},
    }


async def test_a_change_to_the_body_checks_changes_the_revision(db_session, monkeypatch):
    before = await dataplane_sync.build_snapshot(db_session)
    monkeypatch.setattr(get_settings(), "mcp_boundary_blocked_methods", ["Tools/Call", "tools/call", "Shutdown"])
    after = await dataplane_sync.build_snapshot(db_session)
    assert after["boundary"]["mcp"]["blocked_methods"] == ["shutdown", "tools/call"]
    assert after["revision"] != before["revision"]


def test_method_lists_are_lower_cased_as_the_checks_compare_them(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "mcp_boundary_allowed_methods", ["Tools/List", "PING"])
    monkeypatch.setattr(s, "mcp_boundary_prompt_injection_action", "Audit")
    monkeypatch.setattr(s, "mcp_boundary_max_body_bytes", 0)
    mcp = dataplane_sync._boundary(s)["mcp"]
    assert mcp["allowed_methods"] == ["ping", "tools/list"]
    assert mcp["block_injection"] is False and mcp["max_body_bytes"] == 0


def test_custom_injection_patterns_keep_mcp_bodies_with_python(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "mcp_boundary_injection_patterns", [r"drop\s+table"])
    assert dataplane_sync._boundary(s)["mcp"]["default_patterns"] is False


def test_only_tools_with_a_schema_are_named(monkeypatch):
    from app.tier1_registry import tier1_registry

    monkeypatch.setattr(
        tier1_registry, "tool_input_schema_index",
        classmethod(lambda cls: {"add": {"type": "object"}, "noop": {}, "zeta": {"required": ["a"]}}),
    )
    mcp = dataplane_sync._boundary(get_settings())["mcp"]
    assert mcp["schema_tools"] == ["add", "zeta"]
    # With the schemas themselves, so the data plane can pass a call that is certain to fit.
    assert mcp["tool_schemas"] == {"add": {"type": "object"}, "zeta": {"required": ["a"]}}


def test_a_schema_that_is_not_plain_json_is_named_but_not_sent(monkeypatch):
    from app.tier1_registry import tier1_registry

    monkeypatch.setattr(
        tier1_registry, "tool_input_schema_index",
        classmethod(lambda cls: {"odd": {"minimum": float("inf")}, "set": {"enum": {1, 2}}, "fine": {"type": "object"}}),
    )
    mcp = dataplane_sync._boundary(get_settings())["mcp"]
    assert mcp["schema_tools"] == ["fine", "odd", "set"] and mcp["tool_schemas"] == {"fine": {"type": "object"}}


def test_a_registry_that_cannot_be_read_keeps_mcp_bodies_with_python(monkeypatch):
    from app.tier1_registry import tier1_registry

    def broken(cls):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(tier1_registry, "tool_input_schema_index", classmethod(broken))
    mcp = dataplane_sync._boundary(get_settings())["mcp"]
    assert mcp["default_patterns"] is False and mcp["schema_tools"] == []


@pytest.mark.parametrize("path, metric, log", [
    ("/api/users", "http", "http"),
    ("/mcp/tools", "mcp", "mcp"),
    ("/api/mcp", "mcp", "mcp"),
    ("/a2a/v1/message:send", "a2a", "a2a"),
    ("/agui/run", "ag_ui", "ag_ui"),
    ("/api/acp/runs", "acp", "acp"),
    ("/api/items/ws", "websocket", "websocket"),
    ("/api/graphql-proxy", "http", "graphql"),
    ("/api/mcp/acp", "mcp", "mcp"),
])
def test_protocol_labels_follow_the_logging_middleware(path, metric, log):
    assert dataplane_sync.metric_protocol(path) == metric
    assert dataplane_sync.log_protocol(path) == log


def test_native_mcp_requests_are_counted_under_their_protocol():
    label = "/mcp/tools"
    before = _sample("gateway_requests_total", method="POST", path=label, status_code="200", protocol="mcp")
    report = dataplane_sync.DataplaneReport(aggregates=[dataplane_sync.ReportedAggregate(
        method="POST", path=label, status=200, buckets=[[3, 0.006]],
    )])
    dataplane_sync.apply_report(report)
    assert _sample("gateway_requests_total", method="POST", path=label, status_code="200", protocol="mcp") - before == 3


# --- credentials: where a key is read from, and the 401s ------------------------------------------


async def test_the_snapshot_says_where_a_key_is_read_from_and_how_a_request_without_one_is_refused(db_session, monkeypatch):
    import json

    from app.middleware import auth

    monkeypatch.setattr(get_settings(), "api_key_header", "X-Gateway-Key")
    snap = await dataplane_sync.build_snapshot(db_session)
    assert snap["key_header"] == "X-Gateway-Key"
    missing, invalid = snap["refusals"]["missing_key"], snap["refusals"]["invalid_key"]
    assert json.loads(missing["message_json"]) == auth.MISSING_KEY_MESSAGE == "API key required"
    assert json.loads(invalid["message_json"]) == auth.INVALID_KEY_MESSAGE
    assert list(json.loads(missing["details_json"])) == list(json.loads(invalid["details_json"])) == ["hint", "documentation"]
    assert json.loads(missing["details_json"]) == auth.missing_key_details()
    assert json.loads(invalid["details_json"]) == auth.invalid_key_details()


async def test_with_an_auth_plugin_only_this_gateway_refuses(db_session, monkeypatch):
    from types import SimpleNamespace

    from app.plugins import PluginType, plugin_registry

    monkeypatch.setattr(get_settings(), "plugins_enabled", True)
    provider = SimpleNamespace(name="a", enabled=True, plugin_type=PluginType.AUTH_PROVIDER)
    monkeypatch.setattr(plugin_registry, "list", lambda: [provider])
    assert (await dataplane_sync.build_snapshot(db_session))["refusals"] is None
    monkeypatch.setattr(plugin_registry, "list", lambda: [SimpleNamespace(name="a", enabled=False, plugin_type=PluginType.AUTH_PROVIDER)])
    assert (await dataplane_sync.build_snapshot(db_session))["refusals"] is not None


def _client():
    from fastapi.testclient import TestClient

    from app.main import app

    return TestClient(app, raise_server_exceptions=False)


def test_a_refused_credential_is_marked_for_the_data_plane_only_behind_one(monkeypatch):
    import app.main as main

    with _client() as c:
        plain = c.get("/api/users", headers={"X-API-Key": "sk-not-a-key"})
        assert plain.status_code == 401 and "x-dataplane-auth" not in plain.headers
        monkeypatch.setattr(main.settings, "dataplane_token", "t")
        refused = c.get("/api/users", headers={"X-API-Key": "sk-not-a-key"})
        assert refused.status_code == 401 and refused.headers["x-dataplane-auth"] == "invalid-key"
        assert refused.json()["message"] == main.INVALID_KEY_MESSAGE
        # No credential at all is not about a credential: nothing for the data plane to remember.
        missing = c.get("/api/users")
        assert missing.status_code == 401 and "x-dataplane-auth" not in missing.headers
        assert missing.json()["message"] == main.MISSING_KEY_MESSAGE


def test_a_backend_cannot_mark_its_own_answer(monkeypatch):
    import httpx

    import app.main as main

    async def route_request(**kw):
        return httpx.Response(401, json={"detail": "no"}, headers={"X-Dataplane-Auth": "invalid-key", "X-Other": "kept"})

    monkeypatch.setattr(main.router, "route_request", route_request)
    # The configured secret stands in for a key here; where that needs switching on, switch it on.
    if "allow_secret_key_api_key" in type(main.settings).model_fields:
        monkeypatch.setattr(main.settings, "allow_secret_key_api_key", True)
    key = {"X-API-Key": main.settings.secret_key}
    with _client() as c:
        monkeypatch.setattr(main.settings, "dataplane_token", "t")
        r = c.get("/api/users", headers=key)
        assert r.status_code == 401 and r.json() == {"detail": "no"}
        assert "x-dataplane-auth" not in r.headers and r.headers["x-other"] == "kept"


async def test_the_gateways_own_endpoints_stay_with_python_wherever_they_live(db_session, monkeypatch):
    s = get_settings()
    assert (await dataplane_sync.build_snapshot(db_session))["fallthrough_prefixes"] == []
    monkeypatch.setattr(s, "health_check_path", "/api/health")
    monkeypatch.setattr(s, "metrics_path", "/mcp/metrics")
    assert (await dataplane_sync.build_snapshot(db_session))["fallthrough_prefixes"] == ["/api/health", "/mcp/metrics"]


async def test_the_snapshot_says_where_health_lives_and_which_version_it_reports(db_session, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_version", '2.0 "beta"')
    snap = await dataplane_sync.build_snapshot(db_session)
    assert snap["ops"] == {"health_path": settings.health_check_path, "version_json": '"2.0 \\"beta\\""'}
    # A path this gateway answers some other way, or would read differently, is not offered.
    for path in ("/docs", "/admin/health", "/agents/x", "/internal/health", "health", "/h%65alth", "/héalth", "/health?x=1"):
        monkeypatch.setattr(settings, "health_check_path", path)
        assert (await dataplane_sync.build_snapshot(db_session))["ops"]["health_path"] is None, path
    monkeypatch.setattr(settings, "health_check_path", "/api/status")
    moved = await dataplane_sync.build_snapshot(db_session)
    assert moved["ops"]["health_path"] == "/api/status" and moved["revision"] != snap["revision"]


async def test_every_answer_carries_this_gateways_own_answer_to_ready(db_session, monkeypatch):
    import json

    from app import main as gw
    from app.observability import ProbeResult, dependency_probes

    monkeypatch.setattr(get_settings(), "dataplane_token", "s3cret")
    results = {"database": ProbeResult(name="database", kind="database", status="healthy", latency_ms=1.5)}

    async def check(name):
        return results.get(name)

    monkeypatch.setattr(dependency_probes, "check", check)
    full = await dataplane_sync.snapshot(since=None, x_dataplane_token="s3cret", db=db_session)
    assert full["ready"]["status"] == 200
    body = json.loads(full["ready"]["body_json"])
    assert list(body) == ["status", "database"] and body["status"] == "ready"
    assert body["database"] == results["database"].to_dict()
    # It is what /ready itself answers, byte for byte.
    answer = await gw.readiness_check()
    assert (answer.status_code, answer.body.decode()) == (200, full["ready"]["body_json"])

    # Not ready: the same answer, and it never changes the revision.
    results["database"] = ProbeResult(name="database", kind="database", status="unhealthy", latency_ms=None, message="db is down")
    same = await dataplane_sync.snapshot(since=full["revision"], x_dataplane_token="s3cret", db=db_session)
    assert same["unchanged"] is True
    assert same["ready"] == {"status": 503, "body_json": '{"status":"not_ready","reason":"db is down"}'}
    answer = await gw.readiness_check()
    assert (answer.status_code, answer.body.decode()) == (503, same["ready"]["body_json"])
    results.clear()
    gone = await dataplane_sync.readiness_note()
    assert gone == {"status": 503, "body_json": '{"status":"not_ready","reason":"no probe registered"}'}

    # If the answer cannot be worked out, the snapshot still goes out, and the data plane asks each time.
    async def broken():
        raise RuntimeError("no answer")

    monkeypatch.setattr(dataplane_sync, "readiness_note", broken)
    assert (await dataplane_sync.snapshot(since=None, x_dataplane_token="s3cret", db=db_session))["ready"] is None


async def test_the_snapshot_names_the_websocket_backend_only_when_the_data_plane_may_relay(db_session, monkeypatch):
    from types import SimpleNamespace

    from app import main as gw

    async def named():
        return (await dataplane_sync.build_snapshot(db_session))["websocket"]

    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(gw, "websocket_handler", None)
    assert await named() is None
    for url in ("http://b:7000", "wss://b:7000", "ws://b:7000/x?token=1", "ws://b\u00e9:7000"):
        monkeypatch.setattr(gw, "websocket_handler", SimpleNamespace(backend_url=url))
        assert await named() is None, url
    monkeypatch.setattr(gw, "websocket_handler", SimpleNamespace(backend_url="ws://b:7000/base"))
    assert await named() == "ws://b:7000/base"
    # Behind TLS only when both sides are told which certificates to trust, in a file.
    monkeypatch.setattr(gw, "websocket_handler", SimpleNamespace(backend_url="wss://b:7443/base"))
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/gateway/ca.pem")
    assert await named() == "wss://b:7443/base"
    monkeypatch.setenv("SSL_CERT_DIR", "/etc/ssl/certs")
    assert await named() is None
    monkeypatch.setattr(gw, "websocket_handler", SimpleNamespace(backend_url="ws://b:7000/base"))
    # A key read from another header: this gateway relays.
    monkeypatch.setattr(get_settings(), "api_key_header", "X-Gateway-Key")
    assert await named() is None
    monkeypatch.setattr(get_settings(), "api_key_header", "X-API-Key")
    assert dataplane_sync._websocket(get_settings()) == "ws://b:7000/base"


async def test_the_snapshot_names_the_grpc_backend_when_it_is_a_plain_address(db_session, monkeypatch):
    async def named():
        return (await dataplane_sync.build_snapshot(db_session))["grpc"]

    for target, want in ((None, None), ("", None), ("localhost:50051", "http://localhost:50051"),
                         ("http://grpc.internal:9000/", "http://grpc.internal:9000"), ("[::1]:50051", "http://[::1]:50051"),
                         ("dns:///grpc.internal:9000", None), ("unix:/tmp/grpc.sock", None), ("https://grpc.internal:443", "https://grpc.internal:443"),
                         ("https://grpc.internal:443/", "https://grpc.internal:443"), ("https://grpc.internal", None),
                         ("grpc.internal", None), ("b\u00e9:1", None), ("host:50051/path", None)):
        monkeypatch.setattr(get_settings(), "grpc_backend_url", target)
        assert await named() == want, target


async def test_the_snapshot_says_how_the_json_bridge_under_grpc_is_served(db_session, monkeypatch):
    """The data plane serves the bridge as this gateway would: it needs the server the handler calls,
    and the two things about this installation that decide whether a call is made at all."""
    import sys
    import types

    from app import main as gw
    from app.protocols import GRPCHandler

    async def said():
        return (await dataplane_sync.build_snapshot(db_session))["grpc_bridge"]

    def installed(reflection, stubs):
        dataplane_sync._grpc_reflection.cache_clear()
        dataplane_sync._greeter_stubs.cache_clear()
        for name in ("grpc_reflection", "grpc_reflection.v1alpha", "grpc_reflection.v1alpha.reflection", "grpc_reflection.v1alpha.reflection_pb2"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name) if reflection else None)
        if reflection:
            sys.modules["grpc_reflection.v1alpha"].reflection_pb2 = sys.modules["grpc_reflection.v1alpha.reflection_pb2"]
        for name in ("greeter_pb2", "greeter_pb2_grpc"):
            if not stubs:
                monkeypatch.setitem(sys.modules, name, None)
            elif sys.modules.get(name, 0) is None:
                monkeypatch.delitem(sys.modules, name)

    monkeypatch.setattr(gw, "grpc_handler", None)
    assert await said() is None
    monkeypatch.setattr(gw, "grpc_handler", GRPCHandler("grpc.internal:9000"))
    installed(False, True)
    assert await said() == {"target": "http://grpc.internal:9000", "reflection": False, "stubs": True}
    installed(True, False)
    assert await said() == {"target": "http://grpc.internal:9000", "reflection": True, "stubs": False}
    # Asked once: what is installed does not change while the gateway runs.
    installed(True, True)
    dataplane_sync._greeter_stubs.cache_clear()
    assert (await said())["stubs"] is True
    # The handler's channel takes a plain host:port; any other address is left to this gateway.
    for target in ("http://grpc.internal:9000", "https://grpc.internal:443", "dns:///grpc.internal:9000", " grpc.internal:9000", "grpc.internal", ""):
        monkeypatch.setattr(gw, "grpc_handler", GRPCHandler(target))
        assert await said() is None, target
    monkeypatch.setattr(gw, "grpc_handler", types.SimpleNamespace(call_unary=None))
    assert await said() is None
    dataplane_sync._grpc_reflection.cache_clear()
    dataplane_sync._greeter_stubs.cache_clear()


async def test_the_snapshot_names_the_mqtt_broker_while_this_gateway_is_connected_to_it(db_session, monkeypatch):
    from app import main as gw
    from app.protocols import MQTTHandler

    async def named():
        return (await dataplane_sync.build_snapshot(db_session))["mqtt"]

    monkeypatch.setattr(gw, "mqtt_handler", None)
    assert await named() is None
    handler = MQTTHandler("mqtt://broker.internal:1884")
    monkeypatch.setattr(gw, "mqtt_handler", handler)
    assert await named() is None  # not connected: this gateway answers 503, and so must the data plane's answer be
    handler.connected = True
    assert await named() == {"host": "broker.internal", "port": 1884, "tls": False}
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    for url, want in (("mqtt://broker.internal", {"host": "broker.internal", "port": get_settings().protocol_mqtt_default_port, "tls": False}),
                      ("mqtts://broker.internal:8883", None), ("broker.internal:1883", None), ("mqtt://broker.internal:1883/path", None),
                      ("mqtt://user@broker.internal:1883", None), ("mqtt://[::1]:1883", None), ("mqtt://b\u00e9:1883", None),
                      ("mqtt://broker.internal:0", None), ("mqtt://broker.internal:70000", None)):
        handler.broker_url = url
        assert await named() == want, url
    # Behind TLS, only with a certificate file both sides read and no directory beside it.
    handler.broker_url = "mqtts://broker.internal:8883"
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/own.pem")
    assert await named() == {"host": "broker.internal", "port": 8883, "tls": True}
    monkeypatch.setenv("SSL_CERT_DIR", "/etc/ssl/certs")
    assert await named() is None


async def test_the_snapshot_names_the_agents_the_data_plane_can_reach(db_session, monkeypatch):
    from app import main as gw
    from app.agent_routes import AgentEdgeConfig

    async def named():
        return (await dataplane_sync.build_snapshot(db_session))["agents"]

    monkeypatch.setattr(gw, "agent_edge_config", AgentEdgeConfig(routes={}))
    assert await named() == {}
    monkeypatch.setattr(gw, "agent_edge_config", AgentEdgeConfig(routes={
        "hiring": "http://10.0.0.10:8001/", "docs.v2": "http://agents.internal/base", "six": "http://[::1]:9000",
        "secure": "https://agents.example.com", "odd slug": "http://10.0.0.11:8001", "query": "http://10.0.0.12:8001/?a=1",
        "caf\u00e9": "http://10.0.0.13:8001", "blank": "",
    }))
    assert await named() == {"hiring": "http://10.0.0.10:8001", "docs.v2": "http://agents.internal/base", "six": "http://[::1]:9000",
                             "secure": "https://agents.example.com"}
    # An agent registered or retired later changes the snapshot.
    before = (await dataplane_sync.build_snapshot(db_session))["revision"]
    gw.agent_edge_config.replace({"hiring": "http://10.0.0.10:8002"})
    after = await dataplane_sync.build_snapshot(db_session)
    assert after["agents"] == {"hiring": "http://10.0.0.10:8002"} and after["revision"] != before


def test_requests_the_data_plane_forwarded_to_an_agent_reach_the_agent_board(monkeypatch):
    from app import main as gw
    from app.agent_monitor import AgentMonitor
    from app import agent_monitor as board
    from app.agent_routes import AgentEdgeConfig

    monitor = AgentMonitor()
    monkeypatch.setattr(board, "agent_monitor", monitor)
    monkeypatch.setattr(gw, "agent_edge_config", AgentEdgeConfig(routes={"hiring": "http://10.0.0.10:8001"}))
    dataplane_sync.apply_report(dataplane_sync.DataplaneReport(aggregates=[
        dataplane_sync.ReportedAggregate(method="GET", path="/agents/hiring/briefs/7", status=200, buckets=[[3, 0.006]]),
        dataplane_sync.ReportedAggregate(method="POST", path="/agents/hiring/run", status=502, buckets=[[1, 0.5]]),
        # Not an agent, and an agent nobody registered: neither is put on the board.
        dataplane_sync.ReportedAggregate(method="GET", path="/api/users", status=200, buckets=[[5, 0.01]]),
        dataplane_sync.ReportedAggregate(method="GET", path="/agents/nobody/x", status=200, buckets=[[2, 0.01]]),
    ]))
    stats = monitor._traffic_stats("hiring")
    assert (stats["total_requests"], stats["error_pct"]) == (4, 25.0)
    assert stats["avg_latency_ms"] == round((3 * 2.0 + 500.0) / 4, 1)
    assert "nobody" not in monitor._events


async def test_the_snapshot_carries_the_answer_to_an_agent_that_fails(db_session):
    import html

    from app import main as gw

    failure = (await dataplane_sync.build_snapshot(db_session))["agent_failure"]
    assert json.loads(failure["message_json"]) == gw.internal_error_data("x")["message"]
    before, after = failure["page"]
    # Put together with an id, it is the page this gateway renders for that id.
    rid = 'abc-123 <&"\'>'
    assert before + html.escape(rid) + after == gw._error_html(500, gw.internal_error_data(rid))
    assert "INTERNAL_ERROR" not in failure["message_json"] and "<title>500" in before


async def test_the_snapshot_lists_the_dynamic_routes_in_force_in_matching_order(db_session, monkeypatch):
    from types import SimpleNamespace

    from app import route_registry

    def row(path, backend, **kw):
        return SimpleNamespace(path=path, backend_url=backend, is_active=kw.pop("is_active", True), protocol=kw.pop("protocol", "http"),
                               timeout=kw.pop("timeout", 30), rate_limit=None, requires_auth=True, headers=None, rewrite_path=kw.pop("rewrite", None))

    async def listed():
        return (await dataplane_sync.build_snapshot(db_session))["dynamic_routes"]

    monkeypatch.setattr(route_registry, "_ROUTES", route_registry._snapshot_from_rows([
        row("/api/*", "http://b1:9000/"),
        row("/api/orders/*", "http://b2:9000", protocol="mcp", timeout=5, rewrite="/v/orders"),
        row("/api/exact", "http://b3:9000"),
        row("/api/secure/*", "https://b4:9000"),
        row("/api/odd/*", "http://b5:9000", rewrite="/a?b=1"),
        row("/api/off/*", "http://b6:9000", is_active=False),
    ]))
    routes = await listed()
    # Exact first, then the longest prefix; an inactive route is not there at all.
    assert [(r["path"], r["wildcard"]) for r in routes] == [
        ("/api/exact", False), ("/api/orders/", True), ("/api/secure/", True), ("/api/odd/", True), ("/api/", True)]
    by_path = {r["path"]: r for r in routes}
    assert by_path["/api/"] == {"path": "/api/", "wildcard": True, "backend": "http://b1:9000", "protocol": "http",
                                "timeout_seconds": 30, "rewrite": None, "never_sent": False}
    assert by_path["/api/orders/"] == {"path": "/api/orders/", "wildcard": True, "backend": "http://b2:9000", "protocol": "mcp",
                                       "timeout_seconds": 5, "rewrite": "/v/orders", "never_sent": False}
    # A route the data plane cannot follow is listed, so that it leaves what it matches alone.
    assert by_path["/api/secure/"]["backend"] == "https://b4:9000" and by_path["/api/odd/"]["backend"] is None
    # Routes no longer keep every path with this gateway: the data plane follows them.
    assert (await dataplane_sync.build_snapshot(db_session))["fallthrough_prefixes"] != ["/"]
    monkeypatch.setattr(get_settings(), "dynamic_routes_enabled", False)
    assert await listed() == []
    assert (await dataplane_sync.build_snapshot(db_session))["fallthrough_prefixes"] != ["/"]


async def test_a_route_to_a_backend_that_is_not_http_is_described_as_this_gateways_client_treats_it(db_session, monkeypatch):
    """The gateway's HTTP client speaks HTTP to a `ws://` or `wss://` address and refuses any other
    scheme (or none) before it connects anywhere. The snapshot says which is which, and what it says
    is checked here against the client itself."""
    from types import SimpleNamespace

    import httpx

    from app import main as gw, route_registry
    from app.models import ProtocolType, RouteConfig

    # address -> (the backend the data plane is given, whether no request is ever sent)
    backends = {
        "http://127.0.0.1:9": ("http://127.0.0.1:9", False),
        "ws://127.0.0.1:9": ("ws://127.0.0.1:9", False),
        "wss://127.0.0.1:9/base": ("wss://127.0.0.1:9/base", False),
        "mqtt://127.0.0.1:1883": ("mqtt://127.0.0.1:1883", True),
        "MQTT://broker.internal:1883/base": ("MQTT://broker.internal:1883/base", True),
        "grpc://10.0.0.1:50051": ("grpc://10.0.0.1:50051", True),
        "redis://cache": ("redis://cache", True),
        "localhost:50051": ("localhost:50051", True),
        "127.0.0.1:50051/base": ("127.0.0.1:50051/base", True),
        "broker": ("broker", True),
        "h:99999": ("h:99999", True),
        # Addresses this code is not sure of stay with this gateway.
        "amqp://user:pw@h/x": (None, False),
        "mqtt://h:1883/a b": (None, False),
        "[::1]:50051": (None, False),
        "unix:/tmp/s": (None, False),
        "HTTP://h:1": (None, False),
        "WS://127.0.0.1:9": (None, False),
        "ftp://h/x?y=1": (None, False),
    }

    def row(path, backend):
        return SimpleNamespace(path=path, backend_url=backend, is_active=True, protocol="http", timeout=1, rate_limit=None,
                               requires_auth=True, headers=None, rewrite_path=None)

    monkeypatch.setattr(route_registry, "_ROUTES", route_registry._snapshot_from_rows([row(f"/api/r{i}/*", b) for i, b in enumerate(backends)]))
    told = {r["path"]: r for r in (await dataplane_sync.build_snapshot(db_session))["dynamic_routes"]}
    monkeypatch.setattr(gw.router, "client", httpx.AsyncClient())
    try:
        for i, (backend, (given, never_sent)) in enumerate(backends.items()):
            route = told[f"/api/r{i}/"]
            assert (route["backend"], route["never_sent"]) == (given, never_sent), backend
            try:
                await gw.router.route_request("GET", "/api/x", {"accept": "*/*"}, {"a": "1"}, None,
                                              route_config=RouteConfig(path="/api/x", backend_url=backend, protocol=ProtocolType.HTTP, timeout=1))
                outcome = "answered"
            except httpx.UnsupportedProtocol:
                outcome = "never sent"
            except httpx.TransportError:
                outcome = "sent"
            except Exception as e:  # noqa: BLE001
                outcome = type(e).__name__
            if never_sent:
                assert outcome == "never sent", (backend, outcome)
            elif given is not None:
                # Spoken to (in vain: nothing listens there).
                assert outcome == "sent", (backend, outcome)
    finally:
        await gw.router.client.aclose()
    # With another HTTP client, nothing is said of what this one does.
    monkeypatch.setattr(get_settings(), "upstream_client", "other", raising=False)
    told = {r["path"]: r for r in (await dataplane_sync.build_snapshot(db_session))["dynamic_routes"]}
    assert [(told[f"/api/r{i}/"]["backend"], told[f"/api/r{i}/"]["never_sent"]) for i in range(4)] == [
        ("http://127.0.0.1:9", False), (None, False), (None, False), (None, False)]


async def test_the_snapshot_says_how_a_request_from_another_origin_is_answered(db_session, monkeypatch):
    from app import main as gw

    async def said(preflight=False):
        found = (await dataplane_sync.build_snapshot(db_session))["cross_origin"]
        return found["preflight"] if preflight else found and {k: v for k, v in found.items() if k != "preflight"}

    monkeypatch.setattr(gw, "_cors_options", {"allow_origins": ["http://b.example", "http://a.example"], "allow_credentials": True,
                                              "allow_methods": ["*"], "allow_headers": ["*"]})
    assert await said() == {"any_origin": False, "origins": ["http://a.example", "http://b.example"], "credentials": True}
    # A preflight is answered from what the middleware itself holds once set up this way.
    asked = await said(preflight=True)
    assert asked["explicit_origin"] is True and asked["all_headers"] is True and "DELETE" in asked["methods"]
    assert asked["headers"][0] == ["Vary", "Origin"] and ["Access-Control-Allow-Credentials", "true"] in asked["headers"]
    monkeypatch.setattr(gw, "_cors_options", {"allow_origins": ["*", "http://a.example"], "allow_credentials": False,
                                              "allow_methods": ["GET"], "allow_headers": []})
    assert await said() == {"any_origin": True, "origins": ["http://a.example"], "credentials": False}
    asked = await said(preflight=True)
    assert asked["explicit_origin"] is False and asked["all_headers"] is False and asked["methods"] == ["GET"]
    assert asked["headers"][0] == ["Access-Control-Allow-Origin", "*"]
    assert asked["allow_headers"] == ["accept", "accept-language", "content-language", "content-type"]
    # An option the data plane does not reproduce: such requests stay with this gateway.
    monkeypatch.setattr(gw, "_cors_options", {"allow_origins": [], "allow_credentials": False, "allow_origin_regex": "https://.*"})
    assert await said() is None
    monkeypatch.setattr(gw, "_cors_options", {"allow_origins": [], "allow_credentials": False, "expose_headers": ["X-A"]})
    assert await said() is None


async def test_the_snapshot_carries_the_pages_a_browser_is_shown(db_session):
    import html

    from app import main as gw

    pages = (await dataplane_sync.build_snapshot(db_session))["error_pages"]
    assert sorted(pages) == ["401:00", "401:10", "403:00", "403:10", "429:00", "429:01", "429:10", "429:11", "502:00", "502:10", "503:00", "503:10"]

    def filled(key, code, message, rid, hint=None, retry=None):
        page = pages[key].replace("@@code@@", html.escape(code)).replace("@@message@@", html.escape(message))
        page = page.replace("@@request-id@@", html.escape(rid))
        if hint is not None:
            page = page.replace("@@hint@@", html.escape(hint))
        return page if retry is None else page.replace("987654321", str(retry))

    # Filled in, each is the page this gateway renders for the same error.
    data = {"error": "RATE_LIMIT_EXCEEDED", "message": "Rate limit exceeded: 3 requests per <minute>", "request_id": "r-1&2",
            "details": {"limit": 3, "retry_after": 60, "hint": "Wait & retry"}}
    assert filled("429:11", data["error"], data["message"], "r-1&2", "Wait & retry", 60) == gw._error_html(429, data)
    data = {"error": "AUTHORIZATION_ERROR", "message": "This API key is not allowed to access this path", "request_id": "r-2",
            "details": {"path": "/api/x"}}
    assert filled("403:00", data["error"], data["message"], "r-2") == gw._error_html(403, data)
    data = {"error": "BACKEND_ERROR", "message": "Backend: Backend service unavailable", "request_id": "r-3", "details": {"hint": "Check it"}}
    assert filled("502:10", data["error"], data["message"], "r-3", "Check it") == gw._error_html(502, data)


async def test_the_snapshot_says_which_requests_a_transformation_rule_applies_to(db_session, monkeypatch):
    from app.transformers import TransformationManager, TransformationRule, TransformConfig, transformation_manager

    fresh = TransformationManager()
    monkeypatch.setattr(transformation_manager, "rules_by_id", fresh.rules_by_id)
    monkeypatch.setattr(transformation_manager, "rules_by_path", fresh.rules_by_path)

    async def listed():
        rules = (await dataplane_sync.build_snapshot(db_session))["transform_rules"]
        # What each rule does is the next test's.
        return rules and [{k: v for k, v in r.items() if k not in ("native", "request", "response")} for r in rules]

    def rule(pattern, methods=(), enabled=True):
        return transformation_manager.register_rule(TransformationRule(
            name=pattern, path_pattern=pattern, methods=list(methods), enabled=enabled,
            request_config=TransformConfig(headers_set={"X-A": "1"})))

    assert await listed() == []
    rule("/api/orders/*", ["get", "POST"])
    rule("/api/item?")
    rule("/api/off/*", enabled=False)
    assert await listed() == [{"glob": "/api/orders/*", "methods": ["GET", "POST"]}, {"glob": "/api/item?", "methods": []}]
    # Rules the data plane can match no longer keep every path with this gateway.
    assert (await dataplane_sync.build_snapshot(db_session))["fallthrough_prefixes"] != ["/"]
    # Each other kind of pattern in the form the data plane matches as this gateway does.
    for pattern, described in (
            ("*", {"all": True}), ("**", {"all": True}),
            ("^/api/v[0-9]+/", {"regex": "^/api/v[0-9]+/"}), ("/api/.*$", {"regex": "/api/.*$"}),
            ("^/api/(?:a|b)/\\d{2,}$", {"regex": "^/api/(?:a|b)/\\d{2,}$"}),
            ("^/api/(x", {"contains": "^/api/(x"}), ("/api/[z-a]$", {"contains": "/api/[z-a]$"}),
            ("/api/[ab]/x", {"parts": [{"lit": "/api/"}, {"set": "ab"}, {"lit": "/x"}]}),
            ("/v[!0-9]*/?", {"parts": [{"lit": "/v"}, {"set": "".join(c for c in map(chr, range(128)) if not c.isdigit())}, "*", {"lit": "/"}, "?"]}),
            ("/a[]]b[", {"parts": [{"lit": "/a"}, {"set": "]"}, {"lit": "b["}]}),
            ("/a[b-d-f]", {"parts": [{"lit": "/a"}, {"set": "-bcdf"}]})):
        odd = rule(pattern, ["put"])
        assert (await listed())[2:] == [{**described, "methods": ["PUT"]}], pattern
        transformation_manager.remove_rule(odd.id)
    # A pattern with no such form: every path stays here.
    for pattern in ("/api/caf\u00e9/*", "^/api/(?=x)", "^/api/(a)\\1$", "(?i)/api/x$", "^/api/\\s$", "^/a^b", "/a$b$", "^/api/x*+$"):
        odd = rule(pattern)
        assert await listed() is None, pattern
        assert (await dataplane_sync.build_snapshot(db_session))["fallthrough_prefixes"] == ["/"]
        transformation_manager.remove_rule(odd.id)
    assert len(await listed()) == 2


def test_the_snapshot_says_what_a_rule_does_when_the_data_plane_can_do_the_same():
    from app.transformers import TransformationRule

    def actions(**sides):
        return dataplane_sync._rule_actions(TransformationRule.from_dict({"name": "r", **sides}))

    nothing = {"rename": [], "remove": [], "set": []}
    assert actions() == {"request": None, "response": None}
    assert actions(request_config={
        "headers_rename": {"X-Old": "X-New"}, "headers_remove": ["X-Secret", "x-secret", "X-B"], "headers_set": {"X-A": "1", "x-b": "two words"},
        "query_remove": ["debug"], "query_set": {"source": "gw", "empty": ""}}) == {"response": None, "request": {
            "rename": [["X-Old", "X-New"]], "remove": ["x-b", "x-secret"], "set": [["X-A", "1"], ["x-b", "two words"]],
            "query_remove": ["debug"], "query_set": [["source", "gw"], ["empty", ""]]}}
    # The fields of a request's body: a value to set goes as its JSON text.
    assert actions(request_config={"body_rename": {"user": "meta.user"}, "body_remove": ["secret"],
                                   "body_set": {"meta.source": "gateway", "n": 1.5, "tags": ["a", {"b": None}], "caf\u00e9": "\u00e9"}})["request"] == {
        **nothing, "query_remove": [], "query_set": [], "body_rename": [["user", "meta.user"]], "body_remove": ["secret"],
        "body_set": [["meta.source", '"gateway"'], ["n", "1.5"], ["tags", '["a", {"b": null}]'], ["caf\u00e9", '"\\u00e9"']]}
    assert actions(response_config={"headers_set": {"X-A": "1"}, "status_map": {"500": 502, "abc": 200, "0404": 200}}) == {
        "request": None, "response": {**nothing, "set": [["X-A", "1"]], "status": [[500, 502]]}}
    # A rule that only changes what is not looked at on that side changes nothing there.
    assert actions(request_config={"status_map": {"200": 204}}, response_config={"query_set": {"a": "b c"}, "path_rewrite": {"pattern": "a"}}) == {
        "request": {**nothing, "query_remove": [], "query_set": []}, "response": {**nothing, "status": []}}

    # The fields of a body are told for a request and for an answer alike.
    for fields, told in (({"body_set": {"a": 1}}, {"body_set": [["a", "1"]]}), ({"body_remove": ["a"]}, {"body_remove": ["a"]}),
                         ({"body_rename": {"a": "b.c"}}, {"body_rename": [["a", "b.c"]]})):
        assert actions(request_config=fields)["request"] == {**nothing, "query_remove": [], "query_set": [], **told}, fields
        assert actions(response_config=fields)["response"] == {**nothing, "status": [], **told}, fields
    # Anything else is this gateway's to apply: templates, the path, code...
    assert actions(request_config={"template": "{{ body }}"}) is None and actions(response_config={"template": "{{ body }}"}) is None
    # ...a value to set in a body that is not plain JSON, or a path that cannot be written...
    for kept in ({"body_set": {"a": float("nan")}}, {"body_set": {"a": float("inf")}}, {"body_set": {"a": {1: "b"}}}, {"body_set": {"a": (1, 2)}},
                 {"body_set": {"a": 10 ** 400}}, {"body_set": {"a": "\ud800"}}, {"body_set": {"a": {"\udc00": 1}}}, {"body_set": {"\ud800": 1}},
                 {"body_remove": [1]}, {"body_remove": ["\ud800"]}, {"body_rename": {"a": 1}}, {"body_set": ["a"]}, {"body_set": {"a": b"bytes"}}):
        assert actions(request_config=kept) is None and actions(response_config=kept) is None, kept
    # A path rewrite that replaces plain text is told as that...
    assert actions(request_config={"path_rewrite": {"pattern": "^/a", "replacement": "/b"}})["request"]["path"] == {"from": "/a", "to": "/b", "start": True}
    assert actions(request_config={"path_rewrite": {"pattern": r"/x\.y/a\-b"}})["request"]["path"] == {"from": "/x.y/a-b", "to": "", "start": False}
    for nothing_to_do in ({}, {"replacement": "/b"}, {"pattern": ""}):
        assert "path" not in actions(request_config={"headers_set": {"X-A": "1"}, "path_rewrite": nothing_to_do})["request"]
    assert actions(request_config={"path_rewrite": {"pattern": "^", "replacement": "/base"}})["request"]["path"] == {"from": "", "to": "/base", "start": True}
    # ...and one by an expression both engines read alike, with the groups it puts back.
    for rewrite, told in (
        ({"pattern": "^/a$"}, {"regex": "^/a$", "parts": []}),
        ({"pattern": "/a.b", "replacement": "/c"}, {"regex": "/a.b", "parts": ["/c"]}),
        ({"pattern": r"^/users/(\d+)/(\w+)", "replacement": r"/\2/by-user/\g<1>"}, {"regex": r"^/users/(\d+)/(\w+)", "parts": ["/", 2, "/by-user/", 1]}),
        ({"pattern": "/a", "replacement": r"\g<0>\g<0>"}, {"regex": "/a", "parts": [0, 0]}),
        ({"pattern": "/a|/b", "replacement": "/c"}, {"regex": "/a|/b", "parts": ["/c"]}),
        ({"pattern": r"/(posts|notes)/(\d+|new)", "replacement": r"/\2"}, {"regex": r"/(posts|notes)/(\d+|new)", "parts": ["/", 2]}),
        ({"pattern": "/a+|/b", "replacement": "/c"}, {"regex": "/a+|/b", "parts": ["/c"]}),
        ({"pattern": "$", "replacement": ".json"}, {"regex": "$", "parts": [".json"]}),
        ({"pattern": "(a+)+b", "replacement": r"\g<0>\1"}, {"regex": "(a+)+b", "parts": [0, 1]}),
        ({"pattern": "(a*)?b", "replacement": r"\1"}, {"regex": "(a*)?b", "parts": [1]}),
        ({"pattern": "((a*)b)+c", "replacement": r"\2"}, {"regex": "((a*)b)+c", "parts": [2]}),
    ):
        assert actions(request_config={"path_rewrite": rewrite})["request"]["path"] == told, rewrite
    # An expression that does not compile rewrites nothing there, so nothing is told of it.
    for broken in ("(a", "a{3,1}", "[b-a]", "*a"):
        assert "path" not in actions(request_config={"headers_set": {"X-A": "1"}, "path_rewrite": {"pattern": broken, "replacement": "\\9"}})["request"]
    for kept in (
        # What can match nothing is replaced in more places by `re.sub`.
        {"pattern": "a*"}, {"pattern": "(?:/a)*"}, {"pattern": "/?$"}, {"pattern": "^/a|"}, {"pattern": "^$"}, {"pattern": "^", "replacement": r"\g<0>"},
        # What only Python's engine reads, or reads its own way.
        {"pattern": "/(?=a)"}, {"pattern": r"\ba"}, {"pattern": "(?i)/a"}, {"pattern": "(?P<n>/a)"}, {"pattern": r"(/a)\1"}, {"pattern": "/a++"}, {"pattern": "/a$b"},
        # A group that can match nothing, repeated: `re` stops at such a turn.
        {"pattern": "(a*)+b", "replacement": r"\1"}, {"pattern": "(?:(a?))*b"}, {"pattern": "(/a|){2}b", "replacement": "x"}, {"pattern": "(?:/{0,2}?[a-c]?)+?c"},
        # Alternatives that each hold a repetition or a group: the engine here takes what they begin with out in front.
        {"pattern": "a*(aa)|a*a"}, {"pattern": "/(?:[ab]*x|[ab]*y)"}, {"pattern": "/a+b|/a+c"}, {"pattern": "/(a)|/(b)"}, {"pattern": "/(?:(?:a|b)c|(?:a|b)d)"},
        # A replacement with a group the expression does not have, or any other escape.
        {"pattern": "/(a)", "replacement": r"\2"}, {"pattern": "/a", "replacement": r"\1"}, {"pattern": "/(a)", "replacement": r"\10"},
        {"pattern": "/(a)", "replacement": r"\g<01>"}, {"pattern": "/(a)", "replacement": r"\n"}, {"pattern": "/(a)", "replacement": "\\\\"},
        {"pattern": "/a", "replacement": "/a b"}, {"pattern": "/a", "replacement": "/b?q=1"},
        {"pattern": "/caf\u00e9"}, {"pattern": 5}, {"pattern": "/a", "replacement": 5}, "text",
    ):
        assert actions(request_config={"path_rewrite": kept}) is None, kept
    for code in ("condition", "transform_request", "transform_response"):
        assert dataplane_sync._rule_actions(TransformationRule(name="r", **{code: lambda *a: None})) is None
    # ...a header an HTTP library on the way has its own say in, or one the request is read by again
    # after the rules (the version headers), on that side...
    for name in ("Host", "content-length", "User-Agent", "Accept-Encoding", "Accept", "X-API-Version", "accept-version"):
        for kept in ({"headers_set": {name: "v"}}, {"headers_remove": [name]}, {"headers_rename": {name: "X-A"}}, {"headers_rename": {"X-A": name}}):
            assert actions(request_config=kept) is None, kept
    for name in ("Content-Type", "content-length", "Set-Cookie", "Vary", "Access-Control-Allow-Origin", "X-RateLimit-Limit", "x-tenant-id", "X-Served-By"):
        assert actions(response_config={"headers_set": {name: "v"}}) is None, name
        assert actions(response_config={"headers_remove": [name]}) is None, name
    # ...but not the headers this gateway itself writes after the rules, which the data plane writes as it does.
    for name in ("X-Forwarded-For", "x-forwarded-for", "X-Protocol", "traceparent", "Traceparent", "x-request-id", "X-Request-ID"):
        for told in ({"headers_set": {name: "v"}}, {"headers_remove": [name]}, {"headers_rename": {name: "X-A"}}, {"headers_rename": {"X-A": name}}):
            assert actions(request_config=told) is not None, told
    for name in ("X-Request-ID", "x-response-time", "traceparent", "X-API-Version"):
        for told in ({"headers_set": {name: "v"}}, {"headers_remove": [name]}, {"headers_rename": {name: "X-A"}}, {"headers_rename": {"X-A": name}}):
            assert actions(response_config=told) is not None, told
    assert actions(request_config={"headers_set": {"Content-Type": "text/plain", "Set-Cookie": "a=b"}}) is not None
    # ...a name or a value that is not plain, or that the two sides' libraries might write differently...
    for kept in ({"headers_set": {"bad name": "v"}}, {"headers_set": {"": "v"}}, {"headers_set": {"X-A": "Z\u00fcrich"}}, {"headers_set": {"X-A": "two\nlines"}},
                 {"headers_set": {"X-A": " lead"}}, {"headers_set": {"X-A": "trail "}}, {"headers_set": {"X-A": 7}}, {"headers_remove": [7]},
                 {"headers_rename": {"X-A": "X-B", "x-a": "X-C"}}, {"headers_set": ["X-A"]}):
        assert actions(request_config=kept) is None and actions(response_config=kept) is None, kept
    for kept in ({"query_set": {"q": "a b"}}, {"query_set": {"q": "caf\u00e9"}}, {"query_set": {"": "v"}}, {"query_set": {"q": 1}}, {"query_remove": [1]}):
        assert actions(request_config=kept) is None, kept
    # ...and a status that carries no body, or is none.
    for kept in ({"200": 204}, {"204": 200}, {"304": 200}, {"200": 99}, {"200": 600}, {"100": 200}, {"200": 0}):
        assert actions(response_config={"status_map": kept}) is None, kept


async def test_a_rule_the_data_plane_cannot_apply_is_marked_and_keeps_only_its_own_requests(db_session, monkeypatch):
    from app.transformers import TransformationManager, TransformationRule, transformation_manager

    fresh = TransformationManager()
    monkeypatch.setattr(transformation_manager, "rules_by_id", fresh.rules_by_id)
    monkeypatch.setattr(transformation_manager, "rules_by_path", fresh.rules_by_path)
    transformation_manager.register_from_dict({"name": "a", "path_pattern": "/api/a/*", "request_config": {"headers_set": {"X-A": "1"}}})
    transformation_manager.register_from_dict({"name": "b", "path_pattern": "/api/b/*", "response_config": {"template": "{{ body }}"}})
    snap = await dataplane_sync.build_snapshot(db_session)
    assert snap["transform_rules"] == [
        {"glob": "/api/a/*", "methods": [], "native": True, "response": None,
         "request": {"rename": [], "remove": [], "set": [["X-A", "1"]], "query_remove": [], "query_set": []}},
        {"glob": "/api/b/*", "methods": [], "native": False}]
    assert snap["fallthrough_prefixes"] != ["/"]


def test_a_glob_with_classes_is_described_so_that_it_matches_as_fnmatch_does():
    """The parts a pattern is described in, matched the obvious way, agree with Python's own fnmatch
    on every pattern and path tried: the description is what the data plane matches by."""
    import fnmatch
    import random

    def matches(parts, text):
        if not parts:
            return text == ""
        head, rest = parts[0], parts[1:]
        if head == "*":
            return any(matches(rest, text[k:]) for k in range(len(text) + 1))
        if head == "?":
            return text != "" and matches(rest, text[1:])
        if "lit" in head:
            return text.startswith(head["lit"]) and matches(rest, text[len(head["lit"]):])
        return text != "" and text[0] in head["set"] and matches(rest, text[1:])

    rng = random.Random(20261113)
    alphabet = "ab-]![^\\/*?z&~|0"
    for _ in range(4000):
        pattern = "".join(rng.choice(alphabet) for _ in range(rng.randrange(1, 9)))
        if "[" not in pattern or pattern in ("*", "**") or pattern.startswith("^") or pattern.endswith("$"):
            continue
        parts = dataplane_sync._rule_pattern(pattern)["parts"]
        for _ in range(30):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 6)))
            assert matches(parts, text) == fnmatch.fnmatchcase(text, pattern), (pattern, text, parts)


async def test_the_snapshot_lists_the_oauth2_tokens_accepted_as_credentials(db_session, monkeypatch):
    import hashlib
    from datetime import timedelta, timezone

    from app.governance import create_ip_rule
    from app.oauth2 import oauth2_manager

    monkeypatch.setattr(oauth2_manager, "tokens", {})
    monkeypatch.setattr(oauth2_manager, "_access_index", {})

    async def listed():
        return (await dataplane_sync.build_snapshot(db_session))["tokens"]

    assert await listed() == []
    stored = oauth2_manager._store_token("idp", {"access_token": "tok-live", "expires_in": 600})
    spent = oauth2_manager._store_token("idp", {"access_token": "tok-spent", "expires_in": 600})
    spent.expires_at -= timedelta(seconds=601)
    oauth2_manager._store_token("idp", {"access_token": " padded", "expires_in": 600})
    oauth2_manager._store_token("idp", {"access_token": "tok-é", "expires_in": 600})
    settings = dataplane_sync.get_settings()
    # By digest, never the token; with the gateway's default limits, which is what a token gets.
    assert await listed() == [{
        "hash": hashlib.sha256(b"tok-live").hexdigest(), "id": "non-database-key", "name": "oauth2:idp",
        "expires_at": int(stored.expires_at.replace(tzinfo=timezone.utc).timestamp()),
        "rate_limit_per_minute": settings.rate_limit_per_minute, "rate_limit_per_hour": settings.rate_limit_per_hour,
    }]
    assert "tok-live" not in json.dumps(await dataplane_sync.build_snapshot(db_session))
    # A token's caller goes through the IP rules and policies on both planes, so it stays listed.
    await create_ip_rule(db_session, cidr="203.0.113.0/24", action="deny")
    assert len(await listed()) == 1


async def test_a_native_rate_limit_refusal_of_a_token_is_announced_as_our_own_is(db_session, announced):
    event = dataplane_sync.ReportedEvent(
        type="rate_limit_exceeded", api_key_id="non-database-key", api_key_name="oauth2:idp",
        client_ip="198.51.100.7", limit=3, window="minute", path="/api/users", count=2,
    )
    assert await dataplane_sync.apply_events(db_session, [event]) == 1
    await _settle()
    assert announced["webhooks"] == [("rate_limit_exceeded", {
        "api_key_id": None, "api_key_name": "oauth2:idp", "client_ip": "198.51.100.7", "limit": 3,
        "window": "minute", "path": "/api/users", "count": 2,
    })]
    assert announced["limit_alerts"][0]["api_key_id"] is None
