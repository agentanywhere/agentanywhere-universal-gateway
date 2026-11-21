"""Unit tests for app.ida_auth (settings stubbed, JWKS fetch and PyJWT mocked)."""
import sys
import types
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app import ida_auth as ida


def _settings(**kw):
    base = dict(ida_issuer=None, ida_browser_issuer=None, ida_issuer_base=None, ida_jwks_base=None,
                ida_jwks_cache_seconds=3600, ida_admin_roles=None, ida_admin_audience=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def cfg(monkeypatch):
    def _set(**kw):
        monkeypatch.setattr(ida, "_settings", lambda: _settings(**kw))
    monkeypatch.setitem(ida._JWKS_CACHE, "keys", None)
    monkeypatch.setitem(ida._JWKS_CACHE, "fetched_at", 0.0)
    return _set


def test_enabled_and_issuer_precedence(cfg):
    cfg()
    assert ida.enabled() is False and ida._issuer() == ""
    cfg(ida_issuer_base="https://sso/realms")
    assert ida.enabled() is True
    cfg(ida_issuer="https://a/realms/r/", ida_browser_issuer="https://b/realms/r/")
    assert ida._issuer() == "https://b/realms/r"
    cfg(ida_issuer="https://a/realms/r/")
    assert ida._issuer() == "https://a/realms/r"


def test_jwks_uri(cfg):
    cfg(ida_issuer="https://sso/realms/demo")
    assert ida._jwks_uri() == "https://sso/realms/demo/protocol/openid-connect/certs"
    cfg(ida_issuer="https://sso/realms/demo", ida_jwks_base="http://internal:8080/")
    assert ida._jwks_uri() == "http://internal:8080/realms/demo/protocol/openid-connect/certs"
    cfg(ida_issuer="https://sso/auth", ida_jwks_base="http://internal")  # no /realms/ -> falls through
    assert ida._jwks_uri() == "https://sso/auth/protocol/openid-connect/certs"


def test_is_ida_token(cfg):
    rs = "eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.e30."
    hs = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30."
    cfg()
    assert ida.is_ida_token(rs) is False  # disabled
    cfg(ida_issuer="https://x/realms/r")
    assert ida.is_ida_token(rs) is True
    assert ida.is_ida_token(hs) is False
    assert ida.is_ida_token("garbage") is False


def test_fetch_jwks_fetch_cache_and_error(cfg, monkeypatch):
    import httpx
    cfg(ida_issuer="https://x/realms/r", ida_jwks_cache_seconds=10)  # clamped to >=60
    resp = MagicMock()
    resp.json.return_value = {"keys": [1]}
    get = MagicMock(return_value=resp)
    monkeypatch.setattr(httpx, "get", get)
    assert ida._fetch_jwks() == {"keys": [1]}
    assert ida._fetch_jwks() == {"keys": [1]}
    assert get.call_count == 1  # cached
    assert get.call_args.args[0].endswith("/certs")
    ida._JWKS_CACHE["fetched_at"] = 0.0  # expired
    resp.raise_for_status.side_effect = httpx.HTTPStatusError("e", request=MagicMock(), response=MagicMock())
    with pytest.raises(httpx.HTTPStatusError):
        ida._fetch_jwks()


@pytest.mark.parametrize("raw,expected", [
    (None, {"platform_admin", "tenant_admin"}),
    ("Ops, Admin ,", {"ops", "admin"}),
    ('["A", "b", ""]', {"a", "b"}),
    ("[not json", {"[not json"}),
    ('{"a":1}', {'{"a":1}'}),
    ("[1]", {"1"}),
])
def test_allowed_roles(cfg, raw, expected):
    cfg(ida_admin_roles=raw)
    assert ida._allowed_roles() == expected


def test_allowed_roles_non_string(cfg):
    cfg(ida_admin_roles=["x"])
    assert ida._allowed_roles() == {"platform_admin", "tenant_admin"}
    cfg(ida_admin_roles='["x"')  # starts with [ but invalid -> comma split
    assert ida._allowed_roles() == {'["x"'}
    cfg(ida_admin_roles='[]')  # valid empty list -> empty set
    assert ida._allowed_roles() == set()


def test_token_roles():
    claims = {"roles": "Admin", "realm_access": {"roles": ["R1"]},
              "resource_access": {"c1": {"roles": ["C1"]}, "c2": "bad", "c3": {}}}
    assert ida._token_roles(claims) == {"admin", "r1", "c1"}
    assert ida._token_roles({"roles": ["A", "B"]}) == {"a", "b"}
    assert ida._token_roles({"realm_access": "bad", "resource_access": "bad"}) == set()
    assert ida._token_roles({}) == set()


@pytest.fixture
def jwt_stub(monkeypatch):
    """PyJWT with decode() mocked and key selection bypassed."""
    import jwt as real

    mod = types.ModuleType("jwt")
    mod.decode = MagicMock()
    for name in ("PyJWTError", "InvalidTokenError", "InvalidSignatureError"):
        setattr(mod, name, getattr(real, name))
    monkeypatch.setitem(sys.modules, "jwt", mod)
    monkeypatch.setattr(ida, "_signing_keys", lambda token, jwks: ["key"])
    return mod, real.InvalidTokenError


def test_verify_admin_token_success(cfg, monkeypatch, jwt_stub):
    jwt, _ = jwt_stub
    cfg(ida_issuer="https://x/realms/r", ida_admin_audience="aud")
    monkeypatch.setattr(ida, "_fetch_jwks", lambda: {"keys": []})
    jwt.decode.return_value = {"email": "a@b", "realm_access": {"roles": ["Platform_Admin"]}}
    claims = ida.verify_admin_token("tok")
    assert claims["email"] == "a@b"
    kw = jwt.decode.call_args.kwargs
    assert kw["audience"] == "aud" and kw["issuer"] == "https://x/realms/r" and kw["algorithms"] == ["RS256"]
    assert kw["leeway"] == 30


def test_verify_admin_token_empty_role_list_denies_everyone(cfg, monkeypatch, jwt_stub):
    jwt, _ = jwt_stub
    cfg(ida_issuer="https://x/realms/r", ida_admin_roles="[]")
    monkeypatch.setattr(ida, "_fetch_jwks", lambda: {"keys": []})
    jwt.decode.return_value = {"email": "a@b", "realm_access": {"roles": ["platform_admin"]}}
    with pytest.raises(HTTPException) as e:
        ida.verify_admin_token("tok")
    assert e.value.status_code == 403


def test_verify_admin_token_errors(cfg, monkeypatch, jwt_stub):
    jwt, InvalidTokenError = jwt_stub
    cfg(ida_issuer="https://x/realms/r")
    monkeypatch.setattr(ida, "_fetch_jwks", lambda: {})
    jwt.decode.side_effect = InvalidTokenError("expired")
    with pytest.raises(HTTPException) as e:
        ida.verify_admin_token("tok")
    assert e.value.status_code == 401 and "expired" in e.value.detail
    jwt.decode.side_effect = None
    monkeypatch.setattr(ida, "_fetch_jwks", lambda: (_ for _ in ()).throw(OSError("jwks down")))
    with pytest.raises(HTTPException) as e:
        ida.verify_admin_token("tok")
    assert e.value.status_code == 401 and e.value.detail == "Invalid token"


def test_verify_admin_token_role_denied(cfg, monkeypatch, jwt_stub):
    jwt, _ = jwt_stub
    cfg(ida_issuer="https://x/realms/r")
    monkeypatch.setattr(ida, "_fetch_jwks", lambda: {})
    jwt.decode.return_value = {"preferred_username": "bob", "roles": ["viewer"]}
    with pytest.raises(HTTPException) as e:
        ida.verify_admin_token("tok")
    assert e.value.status_code == 403
    # default audience applied, verify_iss off when no issuer
    cfg()
    jwt.decode.return_value = {"roles": ["tenant_admin"]}
    ida.verify_admin_token("tok")
    assert jwt.decode.call_args.kwargs["audience"] == "gateway-admin"
    assert jwt.decode.call_args.kwargs["options"]["verify_iss"] is False


def test_public_config(cfg):
    cfg(ida_issuer="https://x/realms/r/")
    assert ida.public_config() == {"enabled": True, "authority": "https://x/realms/r", "clientId": "gateway-admin"}
    cfg(ida_admin_audience="cid")
    assert ida.public_config() == {"enabled": False, "authority": "", "clientId": "cid"}
