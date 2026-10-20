"""OPS_ENDPOINTS_REQUIRE_ADMIN: optional admin check on /metrics and /health/detailed."""
import pytest

import admin.api as admin_api
import app.main as gateway_main
from app.config import Settings

TOKEN = "ops-endpoints-test-admin-token"
OPS_PATHS = [gateway_main.settings.metrics_path, "/health/detailed"]


def test_setting_is_off_by_default():
    assert Settings(_env_file=None).ops_endpoints_require_admin is False


@pytest.mark.parametrize("path", OPS_PATHS)
def test_open_by_default(client, monkeypatch, path):
    monkeypatch.setattr(gateway_main.settings, "ops_endpoints_require_admin", False)
    res = client.get(path)
    assert res.status_code in {200, 503}  # 503: /health/detailed with a probe down


@pytest.fixture
def require_admin(monkeypatch):
    monkeypatch.setattr(gateway_main.settings, "ops_endpoints_require_admin", True)
    monkeypatch.setattr(admin_api.settings, "admin_token", TOKEN)


@pytest.mark.parametrize("path", OPS_PATHS)
@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer wrong-token"},
    {"Authorization": TOKEN},             # no scheme
    {"Authorization": f"Basic {TOKEN}"},  # wrong scheme
    {"X-API-Key": TOKEN},                 # API-key header is not the admin credential
])
def test_rejected_without_admin_credential(client, require_admin, path, headers):
    res = client.get(path, headers=headers)
    assert res.status_code == 401


@pytest.mark.parametrize("path", OPS_PATHS)
def test_allowed_with_admin_credential(client, require_admin, path):
    res = client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
    assert res.status_code in {200, 503}
    if path == gateway_main.settings.metrics_path:
        assert res.status_code == 200
        assert "text/plain" in res.headers["content-type"]
    else:
        assert "dependencies" in res.json()


@pytest.mark.parametrize("path", ["/health", "/live"])
def test_liveness_probes_stay_public(client, require_admin, path):
    assert client.get(path).status_code == 200
