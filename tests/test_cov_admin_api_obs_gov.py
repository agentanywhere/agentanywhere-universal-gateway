"""Behaviour tests for admin/api.py observability and governance endpoints."""
import copy
from datetime import timedelta

import pytest

from app.config import get_settings
from app.repositories.api_key_repo import APIKeyRepository
from app.time_utils import utcnow

AUTH = {"Authorization": f"Bearer {get_settings().secret_key}"}


def _msg(resp):
    body = resp.json()
    return body.get("message") or body.get("detail") or ""


class _NoSessionLocal:
    """Stand-in for app.database.AsyncSessionLocal. The access-log middleware persists
    rejected/handled requests through it as a fire-and-forget task; the real factory
    writes ./gateway.db, and a factory sharing the test connection races the test's own
    queries. Failing fast makes the middleware skip persistence (it swallows the error)."""

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        raise RuntimeError("request-log persistence disabled in tests")

    async def __aexit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _isolated(db_session, monkeypatch):
    """In-memory request-log DB, plus snapshot/restore of the global trackers."""
    import app.database as database
    from app.governance import audit_log, ip_rule_engine, policy_engine
    from app.observability import slo_tracker, tracer
    monkeypatch.setattr(database, "AsyncSessionLocal", _NoSessionLocal())
    slo_saved = copy.copy(slo_tracker.slos)
    tracer_saved = copy.copy(tracer.__dict__)
    audit_saved = (list(audit_log._recent), audit_log._last_hash)
    audit_log._recent.clear()
    audit_log._last_hash = None
    policy_engine._cache.clear()
    ip_rule_engine._rules.clear()
    yield
    slo_tracker.slos = slo_saved
    tracer.__dict__.clear()
    tracer.__dict__.update(tracer_saved)
    audit_log._recent.clear()
    audit_log._recent.extend(audit_saved[0])
    audit_log._last_hash = audit_saved[1]
    policy_engine._cache.clear()
    ip_rule_engine._rules.clear()


# ------------------------------------------------------------------ auth gate
@pytest.mark.parametrize("method,path", [
    ("get", "/admin/observability/slos"), ("get", "/admin/observability/dashboard"),
    ("get", "/admin/governance/audit"), ("get", "/admin/governance/roles"),
    ("get", "/admin/governance/policies"), ("get", "/admin/governance/ip-rules"),
    ("get", "/admin/plugins"), ("get", "/admin/log-exporters"), ("get", "/admin/tenants"),
])
def test_endpoints_need_admin_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer wrong"}).status_code == 401


# ------------------------------------------------------------------ SLOs
def test_slo_lifecycle_with_defaults_and_overrides(client):
    r = client.post("/admin/observability/slos", json={"name": "checkout"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "checkout" and body["target"] == 0.999

    r = client.post(
        "/admin/observability/slos",
        json={"name": "fast", "target": 0.9, "latency_threshold_ms": 120,
              "rolling_window_seconds": 60, "budget_window_seconds": 600},
        headers=AUTH,
    )
    assert r.json()["target"] == 0.9

    assert client.get("/admin/observability/slos/fast", headers=AUTH).json()["name"] == "fast"
    names = {s["name"] for s in client.get("/admin/observability/slos", headers=AUTH).json()}
    assert {"checkout", "fast"} <= names

    assert client.delete("/admin/observability/slos/fast", headers=AUTH).json() == {"deleted": "fast"}
    assert client.get("/admin/observability/slos/fast", headers=AUTH).status_code == 404
    assert client.delete("/admin/observability/slos/fast", headers=AUTH).status_code == 404


def test_slo_requires_name_400(client):
    r = client.post("/admin/observability/slos", json={"target": 0.5}, headers=AUTH)
    assert r.status_code == 400 and "name" in _msg(r)


# ------------------------------------------------------------------ tracer
def test_tracer_configure_round_trip_and_traces(client):
    r = client.put(
        "/admin/observability/tracer",
        json={"enabled": False, "otlp_endpoint": "http://collector.invalid:4318",
              "otlp_headers": {"x-k": "v"}, "service_name": "svc", "buffer_size": 5},
        headers=AUTH,
    )
    assert r.status_code == 200
    d = r.json()
    assert d["service_name"] == "svc" and d["buffer_size"] == 5
    assert d["otlp_endpoint"] == "http://collector.invalid:4318" and d["enabled"] is False
    assert client.get("/admin/observability/tracer", headers=AUTH).json()["buffer_size"] == 5

    from app.observability import tracer

    with tracer.span("unit-test-span") as span:
        trace_id = span.trace_id
    recent = client.get("/admin/observability/traces", params={"limit": 5}, headers=AUTH).json()
    assert any(t["trace_id"] == trace_id for t in recent)
    one = client.get(f"/admin/observability/traces/{trace_id}", headers=AUTH)
    assert one.status_code == 200 and one.json()["trace_id"] == trace_id
    miss = client.get("/admin/observability/traces/nope", headers=AUTH)
    assert miss.status_code == 404 and "nope" in _msg(miss)
    # limit is clamped rather than rejected
    assert client.get("/admin/observability/traces", params={"limit": 0}, headers=AUTH).status_code == 200
    assert client.get("/admin/observability/traces", params={"limit": 99999}, headers=AUTH).status_code == 200


def test_custom_metrics_listing(client):
    from app.observability import custom_metrics

    custom_metrics.counter("cov_admin_test_counter", "doc").inc()
    body = client.get("/admin/observability/metrics", headers=AUTH).json()
    assert "cov_admin_test_counter" in sum(body.values(), [])


def test_dependencies_and_dashboard_use_probe_results(client, monkeypatch):
    from app.observability import ProbeResult, dependency_probes

    async def _fake_all():
        return [
            ProbeResult(name="db", kind="database", status="healthy", latency_ms=1.0),
            ProbeResult(name="cache", kind="cache", status="degraded", latency_ms=2.0, message="slow"),
        ]

    monkeypatch.setattr(dependency_probes, "check_all", _fake_all)
    deps = client.get("/admin/observability/dependencies", headers=AUTH).json()
    assert deps["status"] == "degraded"
    assert {d["name"]: d["status"] for d in deps["dependencies"]} == {"db": "healthy", "cache": "degraded"}

    dash = client.get("/admin/observability/dashboard", headers=AUTH).json()
    assert dash["dependencies"]["status"] == "degraded"
    assert len(dash["dependencies"]["results"]) == 2
    assert {"slos", "tracer", "custom_metrics"} <= set(dash)

    async def _bad():
        return [ProbeResult(name="x", kind="custom", status="unhealthy", latency_ms=None)]

    monkeypatch.setattr(dependency_probes, "check_all", _bad)
    assert client.get("/admin/observability/dependencies", headers=AUTH).json()["status"] == "unhealthy"

    async def _none():
        return []

    monkeypatch.setattr(dependency_probes, "check_all", _none)
    assert client.get("/admin/observability/dependencies", headers=AUTH).json()["status"] == "unknown"


# ------------------------------------------------------------------ roles
def test_role_assign_never_exposes_token_and_revoke(client):
    r = client.post("/admin/governance/roles",
                    json={"token": "super-secret-ops-token", "name": "ops", "role": "operator"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "operator" and body["is_active"] is True
    assert "super-secret-ops-token" not in r.text
    assert len(body["token_fingerprint"]) == 12

    roles = client.get("/admin/governance/roles", headers=AUTH).json()
    assert [x["id"] for x in roles] == [body["id"]]
    assert "super-secret-ops-token" not in str(roles)

    assert client.delete(f"/admin/governance/roles/{body['id']}", headers=AUTH).json() == {"revoked": body["id"]}
    # revoke is a soft delete: the row stays, inactive, and revoking again is idempotent
    assert client.get("/admin/governance/roles", headers=AUTH).json()[0]["is_active"] is False
    assert client.delete(f"/admin/governance/roles/{body['id']}", headers=AUTH).status_code == 200

    audit = client.get("/admin/governance/audit", params={"action": "rbac.role.assign"}, headers=AUTH).json()
    assert audit and audit[0]["resource_type"] == "admin_role"
    assert audit[0]["actor"] == get_settings().secret_key[-8:]


@pytest.mark.parametrize("payload", [
    {"name": "n", "role": "viewer"}, {"token": "t", "role": "viewer"}, {"token": "t", "name": "n"},
    {"token": "t", "name": "n", "role": "godmode"},
])
def test_role_assign_validation_400(client, payload):
    r = client.post("/admin/governance/roles", json=payload, headers=AUTH)
    assert r.status_code == 400
    assert client.get("/admin/governance/roles", headers=AUTH).json() == []


def test_role_revoke_unknown_404(client):
    assert client.delete("/admin/governance/roles/ghost", headers=AUTH).status_code == 404


# ------------------------------------------------------------------ policies
POLICY = {"name": "deny-secret", "priority": 5, "conditions": {"path": "/secret/*"},
          "action": {"type": "deny", "reason": "no"}}


def test_policy_null_priority_uses_default(client):
    r = client.post("/admin/governance/policies", json=POLICY | {"name": "np", "priority": None}, headers=AUTH)
    assert r.status_code == 200 and r.json()["priority"] == 100


@pytest.mark.parametrize("bad", ["high", True, [1]])
def test_policy_non_integer_priority_is_400(client, bad):
    r = client.post("/admin/governance/policies", json=POLICY | {"name": "bad", "priority": bad}, headers=AUTH)
    assert r.status_code == 400


def test_policy_update_non_integer_priority_is_400(client):
    pid = client.post("/admin/governance/policies", json=POLICY, headers=AUTH).json()["id"]
    r = client.put(f"/admin/governance/policies/{pid}", json={"priority": "high"}, headers=AUTH)
    assert r.status_code == 400


def test_policy_lifecycle(client):
    r = client.post("/admin/governance/policies", json=POLICY, headers=AUTH)
    assert r.status_code == 200
    p = r.json()
    assert p["name"] == "deny-secret" and p["priority"] == 5 and p["enabled"] is True
    assert p["action"] == {"type": "deny", "reason": "no"}

    defaults = client.post("/admin/governance/policies", json={k: v for k, v in POLICY.items() if k != "priority"} | {"name": "d2"}, headers=AUTH)
    assert defaults.status_code == 200 and defaults.json()["priority"] == 100

    listing = client.get("/admin/governance/policies", headers=AUTH).json()
    assert p["id"] in [x["id"] for x in listing]

    u = client.put(f"/admin/governance/policies/{p['id']}",
                   json={"priority": 1, "enabled": False, "ignored_field": "x"}, headers=AUTH)
    assert u.status_code == 200
    assert u.json()["priority"] == 1 and u.json()["enabled"] is False
    assert u.json()["name"] == "deny-secret"

    assert client.delete(f"/admin/governance/policies/{p['id']}", headers=AUTH).json() == {"deleted": p["id"]}
    assert client.delete(f"/admin/governance/policies/{p['id']}", headers=AUTH).status_code == 404

    actions = {a["action"] for a in client.get("/admin/governance/audit", headers=AUTH).json()}
    assert {"policy.create", "policy.update", "policy.delete"} <= actions


@pytest.mark.parametrize("payload", [
    {}, {"name": "n"}, {"name": "n", "conditions": {"path": "/x"}}, {"conditions": {"path": "/x"}, "action": {"type": "allow"}},
])
def test_policy_create_validation_400(client, payload):
    assert client.post("/admin/governance/policies", json=payload, headers=AUTH).status_code == 400


def test_policy_update_validation(client):
    r = client.put("/admin/governance/policies/x", json={"nothing": 1}, headers=AUTH)
    assert r.status_code == 400 and "Allowed" in _msg(r)
    assert client.put("/admin/governance/policies/ghost", json={"priority": 3}, headers=AUTH).status_code == 404


# ------------------------------------------------------------------ ip rules
def test_ip_rule_lifecycle_and_reload(client):
    r = client.post("/admin/governance/ip-rules",
                    json={"cidr": "203.0.113.0/24", "action": "deny", "description": "bad net"}, headers=AUTH)
    assert r.status_code == 200
    rule = r.json()
    assert rule["cidr"] == "203.0.113.0/24" and rule["scope"] == "global" and rule["enabled"] is True

    t = client.post("/admin/governance/ip-rules",
                    json={"cidr": "198.51.100.7", "action": "allow", "scope": "tenant", "scope_id": "acme"}, headers=AUTH)
    assert t.status_code == 200 and t.json()["scope_id"] == "acme"

    listing = client.get("/admin/governance/ip-rules", headers=AUTH).json()
    assert len(listing) == 2 and listing[0]["description"] == "bad net"

    reload = client.post("/admin/governance/reload", headers=AUTH).json()
    assert reload["ip_rules"] == 2 and "policies" in reload

    assert client.delete(f"/admin/governance/ip-rules/{rule['id']}", headers=AUTH).json() == {"deleted": rule["id"]}
    assert client.delete(f"/admin/governance/ip-rules/{rule['id']}", headers=AUTH).status_code == 404
    assert len(client.get("/admin/governance/ip-rules", headers=AUTH).json()) == 1


@pytest.mark.parametrize("payload", [
    {"cidr": "not-a-cidr", "action": "deny"},
    {"cidr": "10.0.0.0/8", "action": "maybe"},
    {"cidr": "10.0.0.0/8", "action": "deny", "scope": "planet"},
    {"action": "deny"},
    {"cidr": "10.0.0.0/8"},
])
def test_ip_rule_validation_400(client, payload):
    assert client.post("/admin/governance/ip-rules", json=payload, headers=AUTH).status_code == 400
    assert client.get("/admin/governance/ip-rules", headers=AUTH).json() == []


# ------------------------------------------------------------------ audit
def test_audit_filters_pagination_and_chain(client):
    for i in range(3):
        client.post("/admin/governance/policies", json={**POLICY, "name": f"p{i}"}, headers=AUTH)
    client.post("/admin/governance/ip-rules", json={"cidr": "10.1.0.0/16", "action": "deny"}, headers=AUTH)

    all_rows = client.get("/admin/governance/audit", headers=AUTH).json()
    assert len(all_rows) >= 4
    pol = client.get("/admin/governance/audit", params={"resource_type": "policy"}, headers=AUTH).json()
    assert len(pol) == 3 and {r["action"] for r in pol} == {"policy.create"}
    one = client.get("/admin/governance/audit", params={"resource_type": "policy", "limit": 1}, headers=AUTH).json()
    assert len(one) == 1
    skipped = client.get("/admin/governance/audit", params={"resource_type": "policy", "limit": 5, "offset": 2}, headers=AUTH).json()
    assert len(skipped) == 1
    assert client.get("/admin/governance/audit", params={"actor": "nobody"}, headers=AUTH).json() == []
    assert client.get("/admin/governance/audit", params={"outcome": "failure"}, headers=AUTH).json() == []

    v = client.get("/admin/governance/audit/verify", headers=AUTH).json()
    assert v["valid"] is True and v["checked"] >= 4


# ------------------------------------------------------------------ api key lifecycle
@pytest.mark.asyncio
async def test_expiring_and_sweep_expired_api_keys(client, db_session):
    _, soon = await APIKeyRepository.create(db_session, name="soon", expires_at=utcnow() + timedelta(days=2))
    _, later = await APIKeyRepository.create(db_session, name="later", expires_at=utcnow() + timedelta(days=60))
    _, never = await APIKeyRepository.create(db_session, name="never")
    _, gone = await APIKeyRepository.create(db_session, name="gone", expires_at=utcnow() - timedelta(hours=1))

    r = client.get("/admin/governance/api-keys/expiring", headers=AUTH).json()
    by_name = {k["name"]: k for k in r}
    assert set(by_name) == {"soon", "gone"}
    assert by_name["gone"]["expired"] is True and by_name["soon"]["expired"] is False
    assert 0 < by_name["soon"]["expires_in_seconds"] <= 2 * 86400

    wide = {k["name"] for k in client.get("/admin/governance/api-keys/expiring", params={"within_days": 90}, headers=AUTH).json()}
    assert wide == {"soon", "later", "gone"}
    # within_days is clamped to [1, 365], never an error
    assert client.get("/admin/governance/api-keys/expiring", params={"within_days": 0}, headers=AUTH).status_code == 200
    assert client.get("/admin/governance/api-keys/expiring", params={"within_days": 10**6}, headers=AUTH).status_code == 200

    sweep = client.post("/admin/governance/api-keys/sweep-expired", headers=AUTH)
    assert sweep.status_code == 200 and sweep.json() == {"deactivated_count": 1}
    active = {k["name"]: k["is_active"] for k in client.get("/admin/api-keys", headers=AUTH).json()}
    assert active == {"soon": True, "later": True, "never": True, "gone": False}
    assert client.post("/admin/governance/api-keys/sweep-expired", headers=AUTH).json() == {"deactivated_count": 0}
    assert soon.id and later.id and never.id and gone.id
