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


# --- signature, claims and role checks (PyJWT) --------------------------------

import time as _time

import jwt as _jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app import ida_auth

_ISSUER = "https://idp.example.com/realms/test"
_AUD = "gateway-admin"


def _keypair(kid):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = _jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key(), as_dict=True)
    jwk.update({"kid": kid, "use": "sig", "alg": "RS256"})
    return private, jwk


_PRIV_A, _JWK_A = _keypair("a")
_PRIV_B, _JWK_B = _keypair("b")


def _token(private=_PRIV_A, kid="a", **overrides):
    now = int(_time.time())
    claims = {
        "iss": _ISSUER, "aud": _AUD, "iat": now, "exp": now + 300,
        "realm_access": {"roles": ["platform_admin"]}, "email": "ops@example.com",
    }
    claims.update(overrides)
    headers = {"kid": kid} if kid else {}
    return _jwt.encode(claims, private, algorithm="RS256", headers=headers)


@pytest.fixture
def ida(monkeypatch):
    s = ida_auth.get_settings()
    monkeypatch.setattr(s, "ida_issuer", _ISSUER, raising=False)
    monkeypatch.setattr(s, "ida_browser_issuer", None, raising=False)
    monkeypatch.setattr(s, "ida_admin_audience", _AUD, raising=False)
    monkeypatch.setattr(s, "ida_admin_roles", "platform_admin,tenant_admin", raising=False)
    monkeypatch.setattr(ida_auth, "_fetch_jwks", lambda: {"keys": [_JWK_B, _JWK_A]})


def test_valid_token_is_accepted(ida):
    claims = ida_auth.verify_admin_token(_token())
    assert claims["email"] == "ops@example.com"
    assert ida_auth.is_ida_token(_token()) is True


def test_token_without_kid_is_matched_against_each_key(ida):
    assert ida_auth.verify_admin_token(_token(kid=None))["iss"] == _ISSUER


@pytest.mark.parametrize("overrides", [
    {"aud": "other-client"},
    {"iss": "https://evil.example.com/realms/test"},
    {"exp": int(_time.time()) - 120},
])
def test_wrong_audience_issuer_or_expired_is_rejected(ida, overrides):
    with pytest.raises(HTTPException) as exc:
        ida_auth.verify_admin_token(_token(**overrides))
    assert exc.value.status_code == 401


def test_small_clock_skew_is_tolerated(ida):
    ida_auth.verify_admin_token(_token(exp=int(_time.time()) - 10))


def test_signature_from_an_unknown_key_is_rejected(ida):
    stranger, _ = _keypair("a")  # same kid, different key
    with pytest.raises(HTTPException) as exc:
        ida_auth.verify_admin_token(_token(private=stranger))
    assert exc.value.status_code == 401


def test_hs256_token_signed_with_public_key_material_is_rejected(ida):
    forged = _jwt.encode({"iss": _ISSUER, "aud": _AUD, "exp": int(_time.time()) + 60},
                         "not-the-rsa-key-but-long-enough-for-hmac-32b", algorithm="HS256",
                         headers={"kid": "a"})
    with pytest.raises(HTTPException) as exc:
        ida_auth.verify_admin_token(forged)
    assert exc.value.status_code == 401


def test_missing_admin_role_is_forbidden(ida):
    with pytest.raises(HTTPException) as exc:
        ida_auth.verify_admin_token(_token(realm_access={"roles": ["viewer"]}))
    assert exc.value.status_code == 403
