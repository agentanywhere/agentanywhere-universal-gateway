"""Admin-API tests for /admin/config and /admin/logging."""
import pytest

from app.config import get_settings, reload_settings

settings = get_settings()


@pytest.fixture
def admin_token():
    return settings.secret_key


@pytest.fixture(autouse=True)
def _restore():
    yield
    reload_settings()  # ensure no test pollutes the global config


# ---------------------------------------------------------------------------
# /admin/config
# ---------------------------------------------------------------------------


def test_get_config_default_is_redacted(client, admin_token):
    res = client.get(
        "/admin/config",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "sections" in data
    assert "safe_reload_fields" in data
    secret = data["sections"]["security"]["secret_key"]
    assert "REDACTED" in secret


def test_get_config_section(client, admin_token):
    res = client.get(
        "/admin/config?section=logging",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "logging" in data
    assert "log_level" in data["logging"]


def test_get_config_unknown_section_404(client, admin_token):
    res = client.get(
        "/admin/config?section=nope",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404


def test_config_diff(client, admin_token):
    res = client.get(
        "/admin/config/diff",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert "differs" in data
    assert "count" in data


def test_config_schema(client, admin_token):
    res = client.get(
        "/admin/config/schema",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    schema = res.json()
    assert "properties" in schema
    assert "log_level" in schema["properties"]


def test_config_validate_valid(client, admin_token):
    res = client.post(
        "/admin/config/validate",
        json={"log_level": "DEBUG"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert res.json()["valid"] is True


def test_config_validate_invalid(client, admin_token):
    res = client.post(
        "/admin/config/validate",
        json={"log_level": "BANANA"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["valid"] is False
    assert "errors" in body


def test_update_config_safe_field(client, admin_token):
    res = client.put(
        "/admin/config",
        json={"log_level": "DEBUG"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert "log_level" in res.json()["applied"]
    # Audit log should record it.
    audit = client.get(
        "/admin/config/audit",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    assert any(entry["field"] == "log_level" for entry in audit)


def test_update_config_unsafe_field_rejected(client, admin_token):
    res = client.put(
        "/admin/config",
        json={"secret_key": "haxx"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 400


def test_config_reload_endpoint(client, admin_token):
    res = client.post(
        "/admin/config/reload",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert res.json()["reloaded"] is True


# ---------------------------------------------------------------------------
# /admin/logging
# ---------------------------------------------------------------------------


def test_get_logging(client, admin_token):
    res = client.get(
        "/admin/logging",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["level"] in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    assert "redact_keys" in data
    assert "sample_rate" in data


def test_update_logging(client, admin_token):
    res = client.put(
        "/admin/logging",
        json={"level": "DEBUG", "sample_rate": 0.5},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["level"] == "DEBUG"
    assert body["sample_rate"] == 0.5

    # Restore.
    client.put(
        "/admin/logging",
        json={"level": "INFO", "sample_rate": 1.0},
        headers={"Authorization": f"Bearer {admin_token}"},
    )


def test_update_logging_rejects_unknown_field(client, admin_token):
    res = client.put(
        "/admin/logging",
        json={"weird_field": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 400


def test_emit_test_log(client, admin_token):
    res = client.post(
        "/admin/logging/test",
        json={"level": "info", "message": "hello-from-tests"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert res.json()["emitted"] is True
