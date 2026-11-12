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


async def test_a_deprecated_version_is_not_native(db_session):
    from app.versioning import VersionStatus, version_manager

    version_manager.register_version("9")
    try:
        assert {v["version"]: v["native_ok"] for v in (await dataplane_sync.build_snapshot(db_session))["api_versions"]["versions"]}["9"]
        version_manager.versions["9"].status = VersionStatus.DEPRECATED
        assert not {v["version"]: v["native_ok"] for v in (await dataplane_sync.build_snapshot(db_session))["api_versions"]["versions"]}["9"]
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
    snap = await dataplane_sync.build_snapshot(db_session)
    assert snap["backends"] == {
        "mcp": "http://mcp.internal:9000", "a2a": "http://a2a.internal:9001/", "ag_ui": None, "acp": None,
    }


async def test_the_snapshot_carries_the_body_checks_as_they_are_shipped(db_session, monkeypatch):
    from app.tier1_registry import tier1_registry

    # Other tests register tools; the shipped state has none.
    monkeypatch.setattr(tier1_registry, "tool_input_schema_index", classmethod(lambda cls: {}))
    snap = await dataplane_sync.build_snapshot(db_session)
    assert snap["boundary"] == {
        "mcp": {
            "enabled": True, "max_body_bytes": 2_097_152, "blocked_methods": [], "allowed_methods": [],
            "block_injection": True, "default_patterns": True, "schema_tools": [],
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
    assert dataplane_sync._boundary(get_settings())["mcp"]["schema_tools"] == ["add", "zeta"]


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

    monkeypatch.setattr(gw, "websocket_handler", None)
    assert await named() is None
    for url in ("http://b:7000", "wss://b:7000", "ws://b:7000/x?token=1", "ws://b\u00e9:7000"):
        monkeypatch.setattr(gw, "websocket_handler", SimpleNamespace(backend_url=url))
        assert await named() is None, url
    monkeypatch.setattr(gw, "websocket_handler", SimpleNamespace(backend_url="ws://b:7000/base"))
    assert await named() == "ws://b:7000/base"
    # A key read from another header, or any rule or policy to evaluate: this gateway relays.
    monkeypatch.setattr(get_settings(), "api_key_header", "X-Gateway-Key")
    assert await named() is None
    monkeypatch.setattr(get_settings(), "api_key_header", "X-API-Key")
    assert dataplane_sync._websocket(get_settings(), governed=True) is None
    assert dataplane_sync._websocket(get_settings(), governed=False) == "ws://b:7000/base"
