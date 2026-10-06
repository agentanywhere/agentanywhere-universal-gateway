"""Admin-API tests for observability + governance endpoints."""
import pytest

from app.config import get_settings
from app.governance import audit_log, ip_rule_engine, policy_engine

settings = get_settings()


@pytest.fixture
def admin_token():
    return settings.secret_key


@pytest.fixture(autouse=True)
def _reset_governance():
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
# Observability
# ---------------------------------------------------------------------------


def test_observability_dashboard(client, admin_token):
    res = client.get(
        "/admin/observability/dashboard",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "dependencies" in body
    assert "slos" in body
    assert "tracer" in body
    assert "custom_metrics" in body


def test_list_slos(client, admin_token):
    res = client.get(
        "/admin/observability/slos",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    names = {slo["name"] for slo in res.json()}
    assert "availability" in names


def test_create_and_delete_slo(client, admin_token):
    payload = {"name": "ttest-slo", "target": 0.95, "latency_threshold_ms": 200}
    res = client.post(
        "/admin/observability/slos",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "ttest-slo"
    assert body["target"] == 0.95

    res = client.delete(
        "/admin/observability/slos/ttest-slo",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200


def test_slo_404(client, admin_token):
    res = client.get(
        "/admin/observability/slos/does-not-exist",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404


def test_tracer_config(client, admin_token):
    res = client.get(
        "/admin/observability/tracer",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert "enabled" in res.json()


def test_update_tracer(client, admin_token):
    res = client.put(
        "/admin/observability/tracer",
        json={"enabled": False, "service_name": "test-svc"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is False
    assert body["service_name"] == "test-svc"


def test_traces_endpoint(client, admin_token):
    res = client.get(
        "/admin/observability/traces",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_dependencies_endpoint(client, admin_token):
    res = client.get(
        "/admin/observability/dependencies",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "dependencies" in body
    assert body["status"] in {"healthy", "degraded", "unhealthy", "unknown"}


# ---------------------------------------------------------------------------
# Health endpoints (no auth)
# ---------------------------------------------------------------------------


def test_health_basic(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "healthy"


def test_health_detailed(client):
    res = client.get("/health/detailed")
    assert res.status_code in {200, 503}
    body = res.json()
    assert "status" in body
    assert "dependencies" in body


def test_live_endpoint(client):
    res = client.get("/live")
    assert res.status_code == 200
    assert res.json()["status"] == "alive"


# ---------------------------------------------------------------------------
# Governance
# ---------------------------------------------------------------------------


def test_create_and_list_policy_via_admin(client, admin_token):
    payload = {
        "name": "deny-from-public",
        "priority": 10,
        "conditions": {"path": "/secret/*"},
        "action": {"type": "deny", "reason": "test"},
    }
    res = client.post(
        "/admin/governance/policies",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    policy_id = res.json()["id"]

    listed = client.get(
        "/admin/governance/policies",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    assert any(p["id"] == policy_id for p in listed)

    deleted = client.delete(
        f"/admin/governance/policies/{policy_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert deleted.status_code == 200


def test_create_policy_validates_payload(client, admin_token):
    res = client.post(
        "/admin/governance/policies",
        json={"name": "missing-conditions"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 400


def test_update_unknown_policy_404(client, admin_token):
    res = client.put(
        "/admin/governance/policies/does-not-exist",
        json={"priority": 1},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404


def test_create_and_delete_ip_rule_via_admin(client, admin_token):
    payload = {"cidr": "203.0.113.0/24", "action": "deny", "scope": "global"}
    res = client.post(
        "/admin/governance/ip-rules",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    rule_id = res.json()["id"]

    listed = client.get(
        "/admin/governance/ip-rules",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    assert any(r["id"] == rule_id for r in listed)

    deleted = client.delete(
        f"/admin/governance/ip-rules/{rule_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert deleted.status_code == 200


def test_create_invalid_ip_rule_returns_400(client, admin_token):
    res = client.post(
        "/admin/governance/ip-rules",
        json={"cidr": "not-a-cidr", "action": "deny"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 400


def test_assign_role_via_admin(client, admin_token):
    res = client.post(
        "/admin/governance/roles",
        json={"token": "ops-token-1", "name": "ops", "role": "operator"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["role"] == "operator"
    role_id = body["id"]

    revoked = client.delete(
        f"/admin/governance/roles/{role_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert revoked.status_code == 200


def test_assign_role_invalid_role(client, admin_token):
    res = client.post(
        "/admin/governance/roles",
        json={"token": "x", "name": "y", "role": "godmode"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 400


def test_audit_log_endpoints(client, admin_token):
    # Trigger an audit-able action via a policy create.
    client.post(
        "/admin/governance/policies",
        json={
            "name": "for-audit-test",
            "priority": 50,
            "conditions": {"path": "/x"},
            "action": {"type": "allow"},
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    listing = client.get(
        "/admin/governance/audit",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert listing.status_code == 200
    rows = listing.json()
    assert any(r["action"] == "policy.create" for r in rows)

    verify = client.get(
        "/admin/governance/audit/verify",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert verify.status_code == 200
    assert verify.json()["valid"] is True


def test_governance_reload_endpoint(client, admin_token):
    res = client.post(
        "/admin/governance/reload",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "policies" in body
    assert "ip_rules" in body


def test_expiring_api_keys_endpoint(client, admin_token):
    res = client.get(
        "/admin/governance/api-keys/expiring",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_sweep_expired_keys(client, admin_token):
    res = client.post(
        "/admin/governance/api-keys/sweep-expired",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert "deactivated_count" in res.json()
