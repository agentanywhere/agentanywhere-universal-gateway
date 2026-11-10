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


async def test_a_tenant_key_is_not_native(db_session):
    plaintext, model = await _key(db_session, name="tenant-key")
    await TenantRepository.create(db_session, "t1", "T1", quota={"requests_per_minute": 5})
    db_session.add(TenantAPIKeyModel(tenant_id="t1", api_key_id=model.id, api_key_hash=hashlib.sha256(plaintext.encode()).hexdigest()))
    await db_session.commit()
    other_plain, other = await _key(db_session, name="plain-key")
    snap = await dataplane_sync.build_snapshot(db_session)
    assert _by_id(snap, model.id)["native_ok"] is False
    assert _by_id(snap, other.id)["native_ok"] is True


async def test_a_global_ip_rule_makes_every_key_non_native(db_session):
    _, a = await _key(db_session, name="a")
    _, b = await _key(db_session, name="b")
    db_session.add(IPRuleModel(cidr="203.0.113.0/24", action="deny", scope="global", enabled=True))
    await db_session.commit()
    snap = await dataplane_sync.build_snapshot(db_session)
    assert not _by_id(snap, a.id)["native_ok"] and not _by_id(snap, b.id)["native_ok"]


async def test_a_disabled_ip_rule_changes_nothing(db_session):
    _, a = await _key(db_session)
    db_session.add(IPRuleModel(cidr="203.0.113.0/24", action="deny", scope="global", enabled=False))
    await db_session.commit()
    assert _by_id(await dataplane_sync.build_snapshot(db_session), a.id)["native_ok"] is True


async def test_a_key_scoped_ip_rule_affects_only_that_key(db_session):
    _, a = await _key(db_session, name="a")
    _, b = await _key(db_session, name="b")
    db_session.add(IPRuleModel(cidr="10.0.0.0/8", action="allow", scope="api_key", scope_id=a.id, enabled=True))
    await db_session.commit()
    snap = await dataplane_sync.build_snapshot(db_session)
    assert _by_id(snap, a.id)["native_ok"] is False
    assert _by_id(snap, b.id)["native_ok"] is True


async def test_an_enabled_policy_makes_every_key_non_native(db_session):
    _, a = await _key(db_session)
    db_session.add(PolicyModel(name="p1", conditions={}, action={"type": "deny"}, enabled=True))
    await db_session.commit()
    assert _by_id(await dataplane_sync.build_snapshot(db_session), a.id)["native_ok"] is False


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
    assert same == {"unchanged": True, "revision": full["revision"]}
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
