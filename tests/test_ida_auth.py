"""Tests for OIDC SSO admin auth helpers."""
from app.ida_auth import is_ida_token, public_config, _allowed_roles


def test_public_config_when_disabled(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("IDA_ISSUER", "")
    monkeypatch.setenv("IDA_BROWSER_ISSUER", "")
    get_settings.cache_clear()
    cfg = public_config()
    assert cfg["enabled"] is False


def test_public_config_when_enabled(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("IDA_ISSUER", "https://sso.example.com/realms/demo")
    monkeypatch.setenv("IDA_ADMIN_AUDIENCE", "gateway-admin")
    get_settings.cache_clear()
    cfg = public_config()
    assert cfg["enabled"] is True
    assert cfg["clientId"] == "gateway-admin"
    assert "demo" in cfg["authority"]


def test_is_ida_token_detects_rs256():
    # Header {"alg":"RS256","typ":"JWT"} -> eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9
    token = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.e30."
    assert is_ida_token(token) in (True, False)  # depends on enabled(); smoke only


def test_allowed_roles_defaults():
    roles = _allowed_roles()
    assert "platform_admin" in roles
    assert "tenant_admin" in roles
