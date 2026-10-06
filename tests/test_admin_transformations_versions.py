"""Admin-API tests for transformations and versioning."""
import pytest

from app.config import get_settings
from app.transformers import transformation_manager
from app.versioning import version_manager

settings = get_settings()


@pytest.fixture
def admin_token():
    return settings.secret_key


@pytest.fixture(autouse=True)
def _reset_managers():
    """Each test starts with empty transformation rules and the default versions."""
    transformation_manager.rules_by_id.clear()
    transformation_manager.rules_by_path.clear()
    yield
    transformation_manager.rules_by_id.clear()
    transformation_manager.rules_by_path.clear()


# ---------------------------------------------------------------------------
# Transformations admin API
# ---------------------------------------------------------------------------


def test_create_transformation_via_admin(client, admin_token):
    payload = {
        "name": "tracking-headers",
        "path_pattern": "/api/*",
        "request_config": {"headers_set": {"X-Trace-Id": "abc"}},
    }
    res = client.post(
        "/admin/transformations",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["name"] == "tracking-headers"
    assert data["path_pattern"] == "/api/*"
    assert data["request_config"]["headers_set"] == {"X-Trace-Id": "abc"}
    assert data["has_request_transform"] is True


def test_list_and_delete_transformation(client, admin_token):
    payload = {
        "name": "rule-1",
        "request_config": {"headers_set": {"X-A": "1"}},
    }
    create = client.post(
        "/admin/transformations",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert create.status_code == 200
    rule_id = create.json()["id"]

    listed = client.get(
        "/admin/transformations",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    assert any(r["id"] == rule_id for r in listed)

    deleted = client.delete(
        f"/admin/transformations/{rule_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert deleted.status_code == 200
    listed_after = client.get(
        "/admin/transformations",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    assert all(r["id"] != rule_id for r in listed_after)


def test_create_transformation_validates_payload(client, admin_token):
    res = client.post(
        "/admin/transformations",
        json={"name": "missing-config"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 400


def test_get_transformation_404(client, admin_token):
    res = client.get(
        "/admin/transformations/does-not-exist",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Versioning admin API
# ---------------------------------------------------------------------------


def test_register_and_list_version(client, admin_token):
    payload = {"version": "3", "default": False}
    res = client.post(
        "/admin/versions",
        json=payload,
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["version"] == "3"
    assert data["status"] == "active"

    listed = client.get(
        "/admin/versions",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    assert any(v["version"] == "3" for v in listed)

    # Cleanup
    client.delete(
        "/admin/versions/3?hard=true",
        headers={"Authorization": f"Bearer {admin_token}"},
    )


def test_get_version_404(client, admin_token):
    res = client.get(
        "/admin/versions/99",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404


def test_deprecate_then_remove_version(client, admin_token):
    # Register a fresh version we can mutate.
    client.post(
        "/admin/versions",
        json={"version": "9"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    deprecate = client.post(
        "/admin/versions/9/deprecate",
        json="2030-01-01T00:00:00",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert deprecate.status_code == 200
    assert deprecate.json()["status"] in ("sunset", "deprecated")

    soft_remove = client.delete(
        "/admin/versions/9",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert soft_remove.status_code == 200
    assert soft_remove.json()["status"] == "removed"

    hard_remove = client.delete(
        "/admin/versions/9?hard=true",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert hard_remove.status_code == 200


def test_version_config_roundtrip(client, admin_token):
    res = client.put(
        "/admin/versions/config",
        json={"strip_prefix_on_proxy": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200
    assert res.json()["strip_prefix_on_proxy"] is True

    # Reset
    client.put(
        "/admin/versions/config",
        json={"strip_prefix_on_proxy": False},
        headers={"Authorization": f"Bearer {admin_token}"},
    )


# ---------------------------------------------------------------------------
# OAuth2 admin API
# ---------------------------------------------------------------------------


def test_register_and_delete_oauth2_provider(client, admin_token):
    res = client.post(
        "/admin/oauth2/providers",
        json={
            "name": "test-acme",
            "client_id": "cid",
            "client_secret": "secret",
            "authorization_endpoint": "https://acme.example/auth",
            "token_endpoint": "https://acme.example/token",
            "introspection_endpoint": "https://acme.example/introspect",
            "revocation_endpoint": "https://acme.example/revoke",
            "scopes": ["read", "write"],
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 200, res.text

    listed = client.get(
        "/admin/oauth2/providers",
        headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    assert any(p["name"] == "test-acme" for p in listed)

    deleted = client.delete(
        "/admin/oauth2/providers/test-acme",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert deleted.status_code == 200


def test_delete_unknown_oauth2_provider(client, admin_token):
    res = client.delete(
        "/admin/oauth2/providers/nope",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert res.status_code == 404
