"""Behaviour tests for admin/api.py integration-style endpoints: webhooks,
transformations, versions, OAuth2, tenants, log exporters, notifications, plugins."""
import copy
from datetime import timedelta

import pytest

from app.config import get_settings
from app.time_utils import utcnow

AUTH = {"Authorization": f"Bearer {get_settings().secret_key}"}


def _msg(resp):
    body = resp.json()
    return body.get("message") or body.get("detail") or ""


def _singletons():
    from app.log_exporters import log_export_manager
    from app.notifications import notification_manager
    from app.oauth2 import oauth2_manager
    from app.plugins import plugin_registry
    from app.tenant import tenant_manager
    from app.transformers import transformation_manager
    from app.versioning import version_manager
    from app.webhooks import webhook_manager

    return [webhook_manager, transformation_manager, version_manager, oauth2_manager,
            tenant_manager, log_export_manager, notification_manager, plugin_registry]


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
    """Redirect request-log persistence to the in-memory DB and snapshot/restore every
    process-global manager these endpoints mutate."""
    import app.database as database
    monkeypatch.setattr(database, "AsyncSessionLocal", _NoSessionLocal())
    objs = _singletons()
    saved = [copy.copy(o.__dict__) for o in objs]
    for o, s in zip(objs, saved):
        for k, v in s.items():
            if isinstance(v, (dict, list)):
                o.__dict__[k] = copy.copy(v)
    yield
    for o, s in zip(objs, saved):
        o.__dict__.clear()
        o.__dict__.update(s)


# ------------------------------------------------------------------ webhooks
def test_webhook_events_catalogue(client):
    events = client.get("/admin/webhooks/events", headers=AUTH).json()
    assert "api_key_created" in events and "api_key_revoked" in events
    assert len(events) == len(set(events)) == 9


def test_webhook_lifecycle_query_params(client):
    r = client.post("/admin/webhooks", params={"event_type": "api_key_created", "url": "https://h.test/x", "secret": "s"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "registered" and body["events"] == ["api_key_created"]
    assert body["id"] == body["ids"][0]
    assert "unknown_events" not in body

    listing = client.get("/admin/webhooks", headers=AUTH).json()
    assert len(listing) == 1
    assert listing[0]["url"] == "https://h.test/x"
    assert listing[0]["enabled"] is True and listing[0]["failure_count"] == 0
    assert "secret" not in listing[0]
    assert client.get("/admin/webhooks", params={"event_type": "api_key_created"}, headers=AUTH).json()[0]["event_type"] == "api_key_created"
    assert client.get("/admin/webhooks", params={"event_type": "other"}, headers=AUTH).json() == []

    d = client.delete(f"/admin/webhooks/{body['id']}", headers=AUTH)
    assert d.status_code == 200 and d.json()["webhook_id"] == body["id"]
    assert client.get("/admin/webhooks", headers=AUTH).json() == []
    assert client.delete(f"/admin/webhooks/{body['id']}", headers=AUTH).status_code == 404


def test_webhook_json_body_multiple_events_dedupes_and_flags_unknown(client):
    r = client.post(
        "/admin/webhooks",
        json={"url": "https://h.test/y", "events": ["x.custom", "x.custom", "api_key_revoked"],
              "event_type": "api_key_revoked", "headers": {"X-A": "1"}, "filters": {"path": "/a"}},
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["events"] == ["api_key_revoked", "x.custom"]
    assert len(body["ids"]) == 2
    assert body["unknown_events"] == ["x.custom"]

    from app.webhooks import webhook_manager

    stored = webhook_manager.webhooks["x.custom"][0]
    assert stored["headers"] == {"X-A": "1"} and stored["filters"] == {"path": "/a"}


def test_webhook_requires_url_and_event_422(client):
    assert client.post("/admin/webhooks", json={"url": "https://h.test"}, headers=AUTH).status_code == 422
    assert client.post("/admin/webhooks", params={"event_type": "e"}, headers=AUTH).status_code == 422
    r = client.post("/admin/webhooks", json={}, headers=AUTH)
    assert r.status_code == 422 and "url" in _msg(r)


def test_webhook_delete_with_explicit_event_type(client):
    wid = client.post("/admin/webhooks", params={"event_type": "e.one", "url": "https://h.test"}, headers=AUTH).json()["id"]
    assert client.delete(f"/admin/webhooks/{wid}", params={"event_type": "e.two"}, headers=AUTH).status_code == 404
    assert client.delete(f"/admin/webhooks/{wid}", params={"event_type": "e.one"}, headers=AUTH).status_code == 200


# ------------------------------------------------------------------ transformations
RULE = {"name": "add-trace", "path_pattern": "/api/*", "methods": ["GET"],
        "request_config": {"headers_set": {"X-Trace": "1"}}}


def test_transformation_lifecycle(client):
    r = client.post("/admin/transformations", json=RULE, headers=AUTH)
    assert r.status_code == 200
    rule = r.json()
    assert rule["name"] == "add-trace" and rule["id"]

    assert [x["id"] for x in client.get("/admin/transformations", headers=AUTH).json()] == [rule["id"]]
    assert client.get(f"/admin/transformations/{rule['id']}", headers=AUTH).json()["id"] == rule["id"]

    assert client.delete(f"/admin/transformations/{rule['id']}", headers=AUTH).status_code == 200
    assert client.get(f"/admin/transformations/{rule['id']}", headers=AUTH).status_code == 404
    assert client.delete(f"/admin/transformations/{rule['id']}", headers=AUTH).status_code == 404
    assert client.get("/admin/transformations", headers=AUTH).json() == []


def test_transformation_validation_400(client):
    no_name = client.post("/admin/transformations", json={"request_config": {"headers_set": {}}}, headers=AUTH)
    assert no_name.status_code == 400 and "name" in _msg(no_name)
    no_cfg = client.post("/admin/transformations", json={"name": "x"}, headers=AUTH)
    assert no_cfg.status_code == 400 and "request_config" in _msg(no_cfg)
    assert client.get("/admin/transformations", headers=AUTH).json() == []


# ------------------------------------------------------------------ versions
def test_version_lifecycle_and_defaults(client):
    from app.versioning import version_manager

    version_manager.versions.clear()
    version_manager.default_version = None

    a = client.post("/admin/versions", json={"version": "v1", "base_path": "/v1"}, headers=AUTH)
    assert a.status_code == 200 and a.json()["version"] == "v1"
    b = client.post("/admin/versions", json={"version": "v2", "status": "bogus", "default": True,
                                             "backend_url": "http://b"}, headers=AUTH)
    assert b.status_code == 200
    assert version_manager.default_version == "v2"
    assert b.json()["status"] == "active"  # unknown status falls back to ACTIVE

    assert {v["version"] for v in client.get("/admin/versions", headers=AUTH).json()} == {"v1", "v2"}
    assert client.get("/admin/versions/v1", headers=AUTH).json()["version"] == "v1"
    assert client.get("/admin/versions/zzz", headers=AUTH).status_code == 404

    cfg = client.get("/admin/versions/config", headers=AUTH).json()
    assert cfg == {"default_version": "v2", "strip_prefix_on_proxy": False}
    upd = client.put("/admin/versions/config", json={"default_version": "v1", "strip_prefix_on_proxy": True}, headers=AUTH)
    assert upd.json() == {"default_version": "v1", "strip_prefix_on_proxy": True}
    assert version_manager.versions["v1"].default is True and version_manager.versions["v2"].default is False
    miss = client.put("/admin/versions/config", json={"default_version": "nope"}, headers=AUTH)
    assert miss.status_code == 404
    assert version_manager.default_version == "v1"
    assert client.put("/admin/versions/config", json={"default_version": None}, headers=AUTH).json()["default_version"] is None


def test_version_create_validation_400(client):
    assert client.post("/admin/versions", json={}, headers=AUTH).status_code == 400
    bad = client.post("/admin/versions", json={"version": "v9", "sunset_date": "not-a-date"}, headers=AUTH)
    assert bad.status_code == 400 and "ISO 8601" in _msg(bad)
    ok = client.post("/admin/versions", json={"version": "v9", "deprecated_date": "2030-01-01T00:00:00",
                                              "sunset_date": "2031-01-01T00:00:00"}, headers=AUTH)
    assert ok.status_code == 200 and ok.json()["sunset_date"].startswith("2031-01-01")


def test_version_deprecate_and_remove(client):
    client.post("/admin/versions", json={"version": "dv"}, headers=AUTH)
    dep = client.post("/admin/versions/dv/deprecate", headers=AUTH)
    assert dep.status_code == 200 and dep.json()["status"] == "deprecated"
    sun = client.post("/admin/versions/dv/deprecate", json="2035-05-05T00:00:00", headers=AUTH)
    assert sun.status_code == 200 and sun.json()["status"] == "sunset"
    assert sun.json()["sunset_date"].startswith("2035-05-05")
    bad = client.post("/admin/versions/dv/deprecate", json="garbage", headers=AUTH)
    assert bad.status_code == 400
    assert client.post("/admin/versions/none/deprecate", headers=AUTH).status_code == 404

    soft = client.delete("/admin/versions/dv", headers=AUTH)
    assert soft.status_code == 200 and soft.json()["status"] == "removed"
    assert client.get("/admin/versions/dv", headers=AUTH).status_code == 200
    hard = client.delete("/admin/versions/dv", params={"hard": True}, headers=AUTH)
    assert hard.status_code == 200 and "permanently" in hard.json()["message"]
    assert client.get("/admin/versions/dv", headers=AUTH).status_code == 404
    assert client.delete("/admin/versions/dv", headers=AUTH).status_code == 404


def test_hard_delete_of_default_version_reassigns_default(client):
    from app.versioning import version_manager

    version_manager.versions.clear()
    version_manager.default_version = None
    client.post("/admin/versions", json={"version": "only", "default": True}, headers=AUTH)
    client.post("/admin/versions", json={"version": "other"}, headers=AUTH)
    client.delete("/admin/versions/only", params={"hard": True}, headers=AUTH)
    assert version_manager.default_version == "other"
    client.delete("/admin/versions/other", params={"hard": True}, headers=AUTH)
    assert version_manager.default_version is None


# ------------------------------------------------------------------ oauth2
PROVIDER = {
    "name": "acme", "client_id": "cid", "client_secret": "csecret",
    "authorization_endpoint": "https://idp.test/auth", "token_endpoint": "https://idp.test/token",
    "scopes": ["openid"], "introspection_endpoint": "https://idp.test/introspect",
}


def test_oauth2_provider_lifecycle_never_leaks_secret(client):
    r = client.post("/admin/oauth2/providers", json=PROVIDER, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["status"] == "registered"
    assert "csecret" not in r.text

    listing = client.get("/admin/oauth2/providers", headers=AUTH)
    assert "csecret" not in listing.text
    row = listing.json()[0]
    assert row["name"] == "acme" and row["client_id"] == "cid"
    assert row["introspection_endpoint"] == "https://idp.test/introspect"
    assert row["scopes"] == ["openid"]

    assert client.delete("/admin/oauth2/providers/acme", headers=AUTH).status_code == 200
    gone = client.delete("/admin/oauth2/providers/acme", headers=AUTH)
    assert gone.status_code == 404 and "acme" in _msg(gone)
    assert client.get("/admin/oauth2/providers", headers=AUTH).json() == []


def test_oauth2_provider_missing_fields_422(client):
    bad = {k: v for k, v in PROVIDER.items() if k != "token_endpoint"}
    assert client.post("/admin/oauth2/providers", json=bad, headers=AUTH).status_code == 422


def test_oauth2_tokens_metadata_only_and_revoke(client):
    from app.oauth2 import StoredToken, oauth2_manager

    oauth2_manager.tokens["t1"] = StoredToken(
        token_id="t1", provider="acme", access_token="SECRET-ACCESS", refresh_token="SECRET-REFRESH",
        token_type="Bearer", expires_at=utcnow() - timedelta(minutes=1), scope="a b")
    oauth2_manager.tokens["t2"] = StoredToken(
        token_id="t2", provider="acme", access_token="A2", refresh_token=None,
        token_type="Bearer", expires_at=utcnow() + timedelta(hours=1))
    r = client.get("/admin/oauth2/tokens", headers=AUTH)
    assert "SECRET-ACCESS" not in r.text and "SECRET-REFRESH" not in r.text
    by_id = {t["token_id"]: t for t in r.json()}
    assert by_id["t1"]["expired"] is True and by_id["t1"]["has_refresh_token"] is True
    assert by_id["t2"]["expired"] is False and by_id["t2"]["has_refresh_token"] is False

    assert client.delete("/admin/oauth2/tokens/t1", headers=AUTH).status_code == 200
    assert "t1" not in oauth2_manager.tokens
    assert client.delete("/admin/oauth2/tokens/t1", headers=AUTH).status_code == 404


# ------------------------------------------------------------------ tenants
def _tenant(client, tid="acme", **kw):
    return client.post("/admin/tenants", json={"tenant_id": tid, "name": "Acme", **kw}, headers=AUTH)


def test_tenant_create_get_list_duplicate(client):
    r = _tenant(client, description="d", config={"a": 1}, quota={"requests_per_minute": 5})
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == "acme" and body["name"] == "Acme"

    assert client.get("/admin/tenants/acme", headers=AUTH).json()["tenant_id"] == "acme"
    dup = _tenant(client)
    assert dup.status_code == 409 and "already exists" in _msg(dup)
    _tenant(client, "beta")
    assert {t["tenant_id"] for t in client.get("/admin/tenants", headers=AUTH).json()} == {"acme", "beta"}
    client.post("/admin/tenants/beta/deactivate", headers=AUTH)
    active = client.get("/admin/tenants", params={"active_only": True}, headers=AUTH).json()
    assert [t["tenant_id"] for t in active] == ["acme"]
    assert client.get("/admin/tenants/ghost", headers=AUTH).status_code == 404
    assert client.post("/admin/tenants", json={"name": "x"}, headers=AUTH).status_code == 422


def test_tenant_quota_config_activate_delete(client):
    _tenant(client)
    q = client.put("/admin/tenants/acme/quota", json={"requests_per_minute": 9}, headers=AUTH)
    assert q.status_code == 200
    c = client.put("/admin/tenants/acme/config", json={"theme": "dark"}, headers=AUTH)
    assert c.status_code == 200
    assert client.get("/admin/tenants/acme", headers=AUTH).json()["config"]["theme"] == "dark"

    off = client.post("/admin/tenants/acme/deactivate", headers=AUTH)
    assert off.status_code == 200 and off.json()["is_active"] is False
    on = client.post("/admin/tenants/acme/activate", headers=AUTH)
    assert on.json()["is_active"] is True

    for method, path, kw in [
        ("put", "/admin/tenants/ghost/quota", {"json": {"a": 1}}),
        ("put", "/admin/tenants/ghost/config", {"json": {"a": 1}}),
        ("post", "/admin/tenants/ghost/activate", {}),
        ("post", "/admin/tenants/ghost/deactivate", {}),
        ("delete", "/admin/tenants/ghost", {}),
        ("get", "/admin/tenants/ghost/usage", {}),
        ("post", "/admin/tenants/ghost/associate-api-key", {"json": {"api_key_id": "i", "api_key": "k"}}),
    ]:
        assert getattr(client, method)(path, headers=AUTH, **kw).status_code == 404, path

    assert client.delete("/admin/tenants/acme", headers=AUTH).status_code == 200
    assert client.get("/admin/tenants/acme", headers=AUTH).status_code == 404


def test_tenant_api_key_association_masks_key_and_disassociate(client):
    _tenant(client)
    r = client.post("/admin/tenants/acme/associate-api-key",
                    json={"api_key_id": "kid-1", "api_key": "sk-supersecretvalue"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["api_key"] == "sk-super..."
    assert "supersecretvalue" not in r.text

    ok = client.delete("/admin/tenants/acme/api-keys/kid-1", headers=AUTH)
    assert ok.status_code == 200 and ok.json()["api_key_id"] == "kid-1"
    assert client.delete("/admin/tenants/acme/api-keys/kid-1", headers=AUTH).status_code == 404


def test_tenant_usage_window_validation(client):
    _tenant(client)
    bad = client.get("/admin/tenants/acme/usage", params={"window": "week"}, headers=AUTH)
    assert bad.status_code == 400 and "minute|hour|day" in _msg(bad)
    for w in ("minute", "hour", "day"):
        r = client.get("/admin/tenants/acme/usage", params={"window": w, "lookback": 3}, headers=AUTH)
        assert r.status_code == 200 and r.json()["tenant_id"] == "acme"


# ------------------------------------------------------------------ log exporters
class _FakeExporter:
    def __init__(self, name="fake", ok=True, enabled=True, **kw):
        self.name = name
        self.ok = ok
        self.enabled = enabled
        self.kw = kw
        self.closed = False

    async def test_connection(self):
        return self.ok

    async def close(self):
        self.closed = True

    def describe(self):
        return {"name": self.name}


def _patch_exporters(monkeypatch, **flags):
    import app.log_exporters as le

    def make(name):
        def _ctor(*a, **k):
            return _FakeExporter(name, enabled=flags.get(name, True), **k)
        return _ctor

    for cls, name in [("ElasticsearchExporter", "elasticsearch"), ("SplunkExporter", "splunk"),
                      ("S3Exporter", "s3"), ("CloudWatchExporter", "cloudwatch"),
                      ("DatadogExporter", "datadog")]:
        monkeypatch.setattr(le, cls, make(name))


def test_configure_each_exporter_and_list_remove_test(client, monkeypatch):
    _patch_exporters(monkeypatch)
    from app.log_exporters import log_export_manager

    log_export_manager.exporters = []
    cases = [
        ("elasticsearch", {"url": "http://es:9200"}, {"index": "gateway-logs"}),
        ("splunk", {"url": "http://sp", "token": "t"}, {"index": "gateway"}),
        ("s3", {"bucket": "b"}, {"bucket": "b", "region": "us-east-1", "prefix": "gateway-logs/"}),
        ("cloudwatch", {"log_group": "g", "log_stream": "s"}, {"log_group": "g", "log_stream": "s", "region": "us-east-1"}),
        ("datadog", {"api_key": "k"}, {"site": "datadoghq.com", "service": "agentanywhere-gateway"}),
    ]
    for name, payload, expect in cases:
        r = client.post(f"/admin/log-exporters/{name}", json=payload, headers=AUTH)
        assert r.status_code == 200, (name, r.text)
        body = r.json()
        assert body["exporter"] == name and body["status"] == "configured"
        for k, v in expect.items():
            assert body[k] == v
        assert "token" not in body and "api_key" not in body
    assert {e.name for e in log_export_manager.exporters} == {c[0] for c in cases}

    ok = client.post("/admin/log-exporters/test/splunk", headers=AUTH).json()
    assert ok == {"exporter": "splunk", "connected": True}
    log_export_manager.get_exporter("splunk").ok = False
    assert client.post("/admin/log-exporters/test/splunk", headers=AUTH).json()["connected"] is False
    assert client.post("/admin/log-exporters/test/missing", headers=AUTH).status_code == 404

    removed = client.delete("/admin/log-exporters/s3", headers=AUTH)
    assert removed.status_code == 200 and "s3" in removed.json()["message"]
    assert log_export_manager.get_exporter("s3") is None
    assert client.delete("/admin/log-exporters/s3", headers=AUTH).status_code == 200  # idempotent


def test_exporter_list_endpoint(client):
    from app.log_exporters import log_export_manager

    log_export_manager.exporters = [_FakeExporter("one")]
    assert client.get("/admin/log-exporters", headers=AUTH).json() == [{"name": "one"}]


@pytest.mark.parametrize("name,payload", [
    ("s3", {"bucket": "b"}), ("cloudwatch", {"log_group": "g", "log_stream": "s"}),
])
def test_aws_exporters_400_when_boto3_missing(client, monkeypatch, name, payload):
    _patch_exporters(monkeypatch, **{name: False})
    from app.log_exporters import log_export_manager

    log_export_manager.exporters = []
    r = client.post(f"/admin/log-exporters/{name}", json=payload, headers=AUTH)
    assert r.status_code == 400 and "boto3" in _msg(r)
    assert log_export_manager.exporters == []


def test_exporter_required_fields_422(client):
    assert client.post("/admin/log-exporters/elasticsearch", json={}, headers=AUTH).status_code == 422
    assert client.post("/admin/log-exporters/splunk", json={"url": "u"}, headers=AUTH).status_code == 422


def test_log_export_worker_start_status_stop(client):
    from app.log_exporters import log_export_manager

    before = client.get("/admin/log-exporters/worker/status", headers=AUTH).json()
    assert before["running"] is False
    started = client.post("/admin/log-exporters/worker/start", headers=AUTH).json()
    assert started["status"] == "running"
    assert started["batch_size"] == log_export_manager.batch_size
    assert client.get("/admin/log-exporters/worker/status", headers=AUTH).json()["running"] is True
    assert client.post("/admin/log-exporters/worker/stop", headers=AUTH).json() == {"status": "stopped"}
    assert client.get("/admin/log-exporters/worker/status", headers=AUTH).json()["running"] is False


# ------------------------------------------------------------------ notifications
def test_email_configure_recipients_and_test(client, monkeypatch):
    from app.notifications import notification_manager

    notification_manager.email_service = None
    nt = client.post("/admin/notifications/email/test", headers=AUTH)
    assert nt.status_code == 400 and "not configured" in _msg(nt)

    r = client.post("/admin/notifications/email/configure",
                    json={"smtp_host": "smtp.test", "smtp_port": 2525, "from_email": "a@b.test"}, headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"smtp_host": "smtp.test", "smtp_port": 2525, "status": "configured"}
    assert notification_manager.email_service is not None
    assert client.post("/admin/notifications/email/configure", json={}, headers=AUTH).status_code == 422

    rec = client.post("/admin/notifications/email/recipients", json=["a@x.test", "b@x.test"], headers=AUTH)
    assert rec.json() == {"recipients": ["a@x.test", "b@x.test"], "count": 2, "status": "updated"}
    assert notification_manager.alert_recipients == ["a@x.test", "b@x.test"]

    async def _conn():
        return True

    monkeypatch.setattr(notification_manager.email_service, "test_connection", _conn)
    assert client.post("/admin/notifications/email/test", headers=AUTH).json() == {"service": "email", "connected": True}


def test_alert_thresholds_get_update_and_validation(client):
    from app.notifications import notification_manager

    base = client.get("/admin/notifications/alert-thresholds", headers=AUTH).json()
    assert base["status"] == "success" and "thresholds" in base

    r = client.post("/admin/notifications/alert-thresholds",
                    json={"error_rate_threshold": 0.25, "min_interval_seconds": 30, "email_rate_limit_per_hour": 7,
                          "rate_limit_enabled": False}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "updated" and body["email_rate_limit_per_hour"] == 7
    assert notification_manager.email_rate_limit_per_hour == 7
    assert notification_manager.alert_thresholds.error_rate_threshold == 0.25
    assert notification_manager.alert_thresholds.min_interval_seconds == 30

    bad_payloads = [
        {"anomaly_score_threshold": 1.5}, {"anomaly_score_threshold": -0.1},
        {"error_rate_threshold": 2}, {"error_rate_threshold": -1},
        {"traffic_spike_threshold": -1}, {"min_interval_seconds": -1},
        {"email_rate_limit_per_hour": 0},
    ]
    for p in bad_payloads:
        resp = client.post("/admin/notifications/alert-thresholds", json=p, headers=AUTH)
        assert resp.status_code == 400, p
    # rejected updates changed nothing
    assert notification_manager.alert_thresholds.error_rate_threshold == 0.25
    ok = client.post("/admin/notifications/alert-thresholds",
                     json={"anomaly_score_threshold": 0.5, "traffic_spike_threshold": 3, "circuit_breaker_enabled": True},
                     headers=AUTH)
    assert ok.status_code == 200


def test_slack_configure_status_test_remove(client, monkeypatch):
    from app.notifications import notification_manager

    notification_manager.slack_service = None
    assert client.get("/admin/notifications/slack", headers=AUTH).json() == {"configured": False}
    nt = client.post("/admin/notifications/slack/test", headers=AUTH)
    assert nt.status_code == 400 and "not configured" in _msg(nt)

    r = client.post("/admin/notifications/slack/configure",
                    json={"webhook_url": "https://hooks.test/x", "default_channel": "#ops", "rate_limit_per_hour": 12},
                    headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"status": "configured", "default_channel": "#ops",
                        "username": "AgentAnywhere Gateway", "rate_limit_per_hour": 12}
    st = client.get("/admin/notifications/slack", headers=AUTH).json()
    assert st == {"configured": True, "default_channel": "#ops", "username": "AgentAnywhere Gateway",
                  "rate_limit_per_hour": 12}
    assert "hooks.test" not in str(st)  # webhook URL is a secret

    bad = client.post("/admin/notifications/slack/configure",
                      json={"webhook_url": "https://hooks.test/x", "rate_limit_per_hour": 0}, headers=AUTH)
    assert bad.status_code == 400
    assert client.post("/admin/notifications/slack/configure", json={}, headers=AUTH).status_code == 422

    async def _conn():
        return False

    monkeypatch.setattr(notification_manager.slack_service, "test_connection", _conn)
    assert client.post("/admin/notifications/slack/test", headers=AUTH).json() == {"service": "slack", "connected": False}

    closed = []

    async def _close():
        closed.append(1)

    monkeypatch.setattr(notification_manager.slack_service, "close", _close)
    assert client.delete("/admin/notifications/slack", headers=AUTH).json() == {"status": "removed"}
    assert closed == [1] and notification_manager.slack_service is None
    assert client.delete("/admin/notifications/slack", headers=AUTH).json() == {"status": "removed"}


def test_slack_remove_tolerates_close_failure(client):
    from app.notifications import notification_manager

    class _Bad:
        async def close(self):
            raise RuntimeError("x")

    notification_manager.slack_service = _Bad()
    assert client.delete("/admin/notifications/slack", headers=AUTH).status_code == 200
    assert notification_manager.slack_service is None


# ------------------------------------------------------------------ plugins
def _plugin(name="demo"):
    from app.plugins import Plugin, PluginMetadata

    class _P(Plugin):
        metadata = PluginMetadata(name=name, version="1.2")
        torn_down = False

        def teardown(self):
            type(self).torn_down = True

    return _P()


def test_plugin_endpoints(client):
    from app.plugins import plugin_registry

    plugin_registry.clear()
    p = _plugin()
    plugin_registry.register(p)

    listing = client.get("/admin/plugins", headers=AUTH).json()
    assert [x["name"] for x in listing] == ["demo"]

    one = client.get("/admin/plugins/demo", headers=AUTH).json()
    assert one["name"] == "demo" and one["enabled"] is True and one["version"] == "1.2"
    assert one["health"]["status"] == "ok" and "stats" in one and "config" in one
    assert client.get("/admin/plugins/ghost", headers=AUTH).status_code == 404

    off = client.post("/admin/plugins/demo/disable", headers=AUTH)
    assert off.json() == {"plugin": "demo", "enabled": False}
    assert plugin_registry.get("demo").enabled is False
    assert client.post("/admin/plugins/demo/enable", headers=AUTH).json() == {"plugin": "demo", "enabled": True}
    assert client.post("/admin/plugins/ghost/enable", headers=AUTH).status_code == 404
    assert client.post("/admin/plugins/ghost/disable", headers=AUTH).status_code == 404

    gone = client.delete("/admin/plugins/demo", headers=AUTH)
    assert gone.json() == {"plugin": "demo", "status": "unregistered"}
    assert type(p).torn_down is True
    assert client.delete("/admin/plugins/demo", headers=AUTH).status_code == 404


def test_plugin_reload_registers_builtins_and_honours_overrides(client, tmp_path):
    from app.plugins import plugin_registry

    plugin_registry.register(_plugin("stale"))
    r = client.post("/admin/plugins/reload", json={"plugin_paths": [str(tmp_path)], "plugins_disabled": []}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["reloaded"] is True
    assert "stale" not in body["registered"]  # registry was cleared first
    assert "request-id" in body["registered"]

    r = client.post("/admin/plugins/reload", json={"plugins_disabled": ["request-id"], "plugin_paths": [str(tmp_path)]}, headers=AUTH)
    assert "request-id" not in r.json()["registered"]
