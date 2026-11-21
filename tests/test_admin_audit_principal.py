"""Admin audit records name the authenticated principal, never a token fragment.

Nine admin actions used to record ``admin_token[-8:]`` as the actor (and the
key-creation alert ``admin_token[:8]``); with single sign-on that is part of a
live bearer token's signature.
"""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from admin.api import admin_principal
from app import ida_auth
from app.config import get_settings

_ISSUER = "https://idp.example.com/realms/test"
_AUD = "gateway-admin"
_PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = jwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE.public_key(), as_dict=True)
_JWK.update({"kid": "k1", "use": "sig", "alg": "RS256"})


def _sso_token(**claims):
    now = int(time.time())
    body = {"iss": _ISSUER, "aud": _AUD, "iat": now, "exp": now + 300,
            "realm_access": {"roles": ["platform_admin"]}}
    body.update(claims)
    return jwt.encode(body, _PRIVATE, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def sso(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "ida_issuer", _ISSUER, raising=False)
    monkeypatch.setattr(s, "ida_browser_issuer", None, raising=False)
    monkeypatch.setattr(s, "ida_admin_audience", _AUD, raising=False)
    monkeypatch.setattr(s, "ida_admin_roles", "platform_admin", raising=False)
    monkeypatch.setattr(ida_auth, "_fetch_jwks", lambda: {"keys": [_JWK]})


def test_principal_for_each_credential(sso, monkeypatch):
    # The settings object admin.api holds, which another test's cache_clear()
    # can leave different from get_settings().
    import admin.api as admin_api

    s = admin_api.settings
    monkeypatch.setattr(s, "admin_token", "static-admin-token-for-tests-0123456789", raising=False)
    assert admin_principal(None) is None
    assert admin_principal(_sso_token(preferred_username="ops.lead", email="ops@example.com")) == "sso:ops.lead"
    assert admin_principal(_sso_token(email="ops@example.com")) == "sso:ops@example.com"
    assert admin_principal(_sso_token(sub="user-123")) == "sso:user-123"
    assert admin_principal("static-admin-token-for-tests-0123456789") == "admin-token"
    assert admin_principal(s.secret_key) == "secret-key"


def _is_principal_label(actor):
    """Actors are labels or SSO identities, never credential material."""
    return actor in (None, "secret-key", "admin-token", "unknown", "env-seed") or str(actor).startswith("sso:")


def _audit_actors(client, headers):
    r = client.get("/admin/governance/audit?limit=50", headers=headers)
    assert r.status_code == 200, r.text
    return [e.get("actor") for e in r.json()]


def test_audit_actor_with_shared_secret(client):
    secret = get_settings().secret_key
    headers = {"Authorization": f"Bearer {secret}"}
    r = client.post("/admin/governance/roles", headers=headers,
                    json={"token": "operator-token-for-tests-0123456789", "name": "op", "role": "operator"})
    assert r.status_code == 200, r.text
    actors = _audit_actors(client, headers)
    assert "secret-key" in actors
    assert all(_is_principal_label(a) for a in actors), actors
    assert not any(a and (secret[-8:] in a or secret[:8] in a) for a in actors)


def test_audit_actor_with_single_sign_on(client, sso):
    token = _sso_token(preferred_username="ops.lead")
    headers = {"Authorization": f"Bearer {token}"}
    r = client.post("/admin/governance/policies", headers=headers, json={
        "name": "audit-principal-test", "priority": 50,
        "conditions": {"path_prefix": "/nothing-here"}, "action": {"type": "allow"},
    })
    assert r.status_code in (200, 201), r.text
    actors = _audit_actors(client, headers)
    assert "sso:ops.lead" in actors
    assert all(_is_principal_label(a) for a in actors), actors
    assert not any(a and (token[-8:] in a or token[:8] in a) for a in actors)
