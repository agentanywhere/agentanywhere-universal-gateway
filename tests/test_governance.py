"""Tests for the governance module: audit log, RBAC, policies, IP rules."""
from __future__ import annotations

from datetime import timedelta
from app.time_utils import utcnow

import pytest

from app.governance import (
    AuditEntry,
    IPRuleEngine,
    PolicyConditions,
    PolicyEngine,
    Role,
    _ip_matches_any_cidr,
    _time_in_window,
    assign_role,
    audit_log,
    auto_deactivate_expired_keys,
    create_ip_rule,
    create_policy,
    delete_ip_rule,
    delete_policy,
    expiring_api_keys,
    get_role_for_token,
    hash_token,
    ip_rule_engine,
    list_ip_rules,
    list_policies,
    list_roles,
    policy_engine,
    revoke_role,
    role_has_permission,
    update_policy,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestRBACHelpers:
    def test_super_admin_has_everything(self):
        assert role_has_permission(Role.SUPER_ADMIN, "audit:read")
        assert role_has_permission(Role.SUPER_ADMIN, "anything:write")

    def test_viewer_read_only(self):
        assert role_has_permission(Role.VIEWER, "config:read")
        assert not role_has_permission(Role.VIEWER, "config:write")

    def test_auditor_audit_only(self):
        assert role_has_permission(Role.AUDITOR, "audit:read")
        assert not role_has_permission(Role.AUDITOR, "config:read")

    def test_hash_token_stable(self):
        assert hash_token("abc") == hash_token("abc")
        assert hash_token("abc") != hash_token("abcd")
        assert len(hash_token("abc")) == 64


class TestIPHelper:
    def test_ip_in_cidr(self):
        assert _ip_matches_any_cidr("10.0.0.5", ["10.0.0.0/8"])

    def test_ip_outside_cidr(self):
        assert not _ip_matches_any_cidr("172.16.0.1", ["10.0.0.0/8"])

    def test_invalid_ip(self):
        assert not _ip_matches_any_cidr("not-an-ip", ["10.0.0.0/8"])

    def test_invalid_cidr_skipped(self):
        assert _ip_matches_any_cidr("10.0.0.1", ["bogus", "10.0.0.0/8"])


class TestTimeWindow:
    def test_no_window_matches(self):
        assert _time_in_window({}) is True

    def test_within_window(self):
        # All-day window — always matches.
        assert _time_in_window({"start": "00:00", "end": "23:59"}) is True

    def test_invalid_window_matches(self):
        assert _time_in_window({"start": "bogus", "end": "23:59"}) is True


# ---------------------------------------------------------------------------
# Policy conditions
# ---------------------------------------------------------------------------


class TestPolicyConditions:
    def test_path_glob_matches(self):
        c = PolicyConditions.from_dict({"path": "/admin/*"})
        assert c.matches({"path": "/admin/users"})
        assert not c.matches({"path": "/api/users"})

    def test_method_filter(self):
        c = PolicyConditions.from_dict({"methods": ["POST"]})
        assert c.matches({"method": "POST"})
        assert not c.matches({"method": "GET"})

    def test_cidr_filter(self):
        c = PolicyConditions.from_dict({"cidrs": ["10.0.0.0/8"]})
        assert c.matches({"client_ip": "10.0.0.5"})
        assert not c.matches({"client_ip": "8.8.8.8"})

    def test_tenant_filter(self):
        c = PolicyConditions.from_dict({"tenants": ["acme"]})
        assert c.matches({"tenant": "acme"})
        assert not c.matches({"tenant": "other"})

    def test_header_regex_match(self):
        c = PolicyConditions.from_dict({"headers": {"x-region": "us-.*"}})
        assert c.matches({"headers": {"X-Region": "us-east-1"}})
        assert not c.matches({"headers": {"X-Region": "eu-west-1"}})

    def test_to_dict_round_trip(self):
        data = {
            "path": "/x/*",
            "methods": ["GET", "POST"],
            "cidrs": ["10.0.0.0/8"],
            "tenants": ["acme"],
            "roles": ["operator"],
            "time_of_day": {"start": "00:00", "end": "23:59"},
            "headers": {"x-foo": "bar"},
        }
        c = PolicyConditions.from_dict(data)
        round_tripped = c.to_dict()
        assert round_tripped["path"] == "/x/*"
        assert round_tripped["methods"] == ["GET", "POST"]
        assert round_tripped["headers"] == {"x-foo": "bar"}


# ---------------------------------------------------------------------------
# Audit log + DB-backed flows
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_audit_log_state():
    """Each test starts with clean global audit + engine state."""
    audit_log._recent.clear()
    audit_log._last_hash = None
    policy_engine._cache.clear()
    ip_rule_engine._rules.clear()
    yield
    audit_log._recent.clear()
    audit_log._last_hash = None
    policy_engine._cache.clear()
    ip_rule_engine._rules.clear()


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_record_and_chain(db_session):
    a = await audit_log.record(
        db_session,
        AuditEntry(action="config.update", actor="root", outcome="success"),
    )
    b = await audit_log.record(
        db_session,
        AuditEntry(action="policy.create", actor="root", outcome="success"),
    )
    assert a["entry_hash"]
    assert b["prev_hash"] == a["entry_hash"]


@pytest.mark.asyncio
async def test_audit_chain_verification(db_session):
    for i in range(5):
        await audit_log.record(
            db_session,
            AuditEntry(action=f"action.{i}", actor="root"),
        )
    result = await audit_log.verify_chain(db_session)
    assert result["valid"] is True
    assert result["checked"] == 5


@pytest.mark.asyncio
async def test_audit_list_with_filter(db_session):
    await audit_log.record(db_session, AuditEntry(action="alpha", actor="alice"))
    await audit_log.record(db_session, AuditEntry(action="beta", actor="bob"))
    rows = await audit_log.list(db_session, action="alpha")
    assert all(r["action"] == "alpha" for r in rows)


# ---------------------------------------------------------------------------
# RBAC DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_assign_and_lookup_role(db_session):
    await assign_role(db_session, token="t-1", name="ops", role=Role.OPERATOR)
    looked_up = await get_role_for_token(db_session, "t-1")
    assert looked_up == Role.OPERATOR


@pytest.mark.asyncio
async def test_revoke_role(db_session):
    row = await assign_role(
        db_session, token="t-2", name="ops", role=Role.OPERATOR
    )
    revoked = await revoke_role(db_session, row.id)
    assert revoked is True
    result = await get_role_for_token(db_session, "t-2")
    assert result is None


@pytest.mark.asyncio
async def test_list_roles_redacts_token(db_session):
    await assign_role(db_session, token="t-3", name="audit", role=Role.AUDITOR)
    roles = await list_roles(db_session)
    assert any(r["name"] == "audit" for r in roles)
    assert all("token" not in r for r in roles)
    assert all("token_fingerprint" in r for r in roles)


# ---------------------------------------------------------------------------
# Policies DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_list_policy(db_session):
    policy = await create_policy(
        db_session,
        name="deny-public-admin",
        conditions={"path": "/admin/*"},
        action={"type": "deny"},
        priority=10,
    )
    assert policy.name == "deny-public-admin"
    policies = await list_policies(db_session)
    assert any(p["id"] == policy.id for p in policies)


@pytest.mark.asyncio
async def test_update_and_delete_policy(db_session):
    policy = await create_policy(
        db_session,
        name="some-policy",
        conditions={"path": "/x"},
        action={"type": "allow"},
    )
    updated = await update_policy(
        db_session, policy.id, priority=999, action={"type": "deny"}
    )
    assert updated.priority == 999
    assert updated.action["type"] == "deny"
    deleted = await delete_policy(db_session, policy.id)
    assert deleted is True


@pytest.mark.asyncio
async def test_engine_evaluates_policies(db_session):
    await create_policy(
        db_session,
        name="deny-admin",
        priority=10,
        conditions={"path": "/admin/*"},
        action={"type": "deny", "reason": "admin closed"},
    )
    await policy_engine.reload(db_session)
    decision = policy_engine.evaluate(
        {"path": "/admin/users", "method": "GET"}
    )
    assert decision.allowed is False
    assert "admin closed" in (decision.reason or "")


# ---------------------------------------------------------------------------
# IP rules DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_evaluate_global_deny(db_session):
    await create_ip_rule(
        db_session, cidr="10.0.0.0/8", action="deny", scope="global"
    )
    engine = IPRuleEngine()
    await engine.reload(db_session)
    allowed, _ = engine.evaluate("10.0.0.5")
    assert allowed is False
    allowed, _ = engine.evaluate("8.8.8.8")
    assert allowed is True


@pytest.mark.asyncio
async def test_global_allow_only(db_session):
    await create_ip_rule(
        db_session, cidr="10.0.0.0/8", action="allow", scope="global"
    )
    engine = IPRuleEngine()
    await engine.reload(db_session)
    allowed_in, _ = engine.evaluate("10.0.0.1")
    allowed_out, _ = engine.evaluate("8.8.8.8")
    assert allowed_in is True
    assert allowed_out is False


@pytest.mark.asyncio
async def test_invalid_cidr_rejected(db_session):
    with pytest.raises(ValueError):
        await create_ip_rule(db_session, cidr="not-a-cidr", action="deny")


@pytest.mark.asyncio
async def test_invalid_action_rejected(db_session):
    with pytest.raises(ValueError):
        await create_ip_rule(db_session, cidr="0.0.0.0/0", action="bogus")


@pytest.mark.asyncio
async def test_delete_ip_rule(db_session):
    rule = await create_ip_rule(
        db_session, cidr="172.16.0.0/12", action="deny"
    )
    deleted = await delete_ip_rule(db_session, rule.id)
    assert deleted is True


# ---------------------------------------------------------------------------
# API key lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expiring_keys_listed(db_session):
    from app.repositories.api_key_repo import APIKeyRepository

    soon = utcnow() + timedelta(days=3)
    await APIKeyRepository.create(
        db=db_session, name="expiring-soon", expires_at=soon
    )
    listing = await expiring_api_keys(db_session, within_days=7)
    assert any(item["name"] == "expiring-soon" for item in listing)


@pytest.mark.asyncio
async def test_auto_deactivate_expired(db_session):
    from app.repositories.api_key_repo import APIKeyRepository

    past = utcnow() - timedelta(days=1)
    await APIKeyRepository.create(
        db=db_session, name="already-expired", expires_at=past
    )
    count = await auto_deactivate_expired_keys(db_session)
    assert count >= 1
