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
