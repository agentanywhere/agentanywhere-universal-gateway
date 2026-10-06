"""OIDC SSO (Keycloak-compatible) token validation for the gateway admin console.

Accepts RS256 access tokens from sso.example.com and gates admin API access
to operator roles (platform_admin / tenant_admin by default). Coexists with the
legacy admin username/password and SECRET_KEY bearer tokens for local dev.
"""
from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.config import get_settings

logger = logging.getLogger(__name__)

_JWKS_CACHE: Dict[str, Any] = {"fetched_at": 0.0, "keys": None}


def _settings():
    return get_settings()


def enabled() -> bool:
    s = _settings()
    return bool(s.ida_issuer or s.ida_browser_issuer or s.ida_issuer_base)


def _issuer() -> str:
    s = _settings()
    return (s.ida_browser_issuer or s.ida_issuer or "").rstrip("/")


def _jwks_uri() -> str:
    s = _settings()
    if s.ida_jwks_base:
        base = s.ida_jwks_base.rstrip("/")
        if "/realms/" in _issuer():
            realm = _issuer().rsplit("/", 1)[-1]
            return f"{base}/realms/{realm}/protocol/openid-connect/certs"
    return f"{_issuer()}/protocol/openid-connect/certs"


def is_ida_token(token: str) -> bool:
    if not enabled():
        return False
    try:
        from jose import jwt

        alg = (jwt.get_unverified_header(token) or {}).get("alg", "")
        return str(alg).upper().startswith("RS")
    except Exception:
        return False


def _fetch_jwks() -> dict:
    import httpx

    s = _settings()
    ttl = max(int(getattr(s, "ida_jwks_cache_seconds", 3600) or 3600), 60)
    now = time.time()
    if _JWKS_CACHE["keys"] is not None and (now - _JWKS_CACHE["fetched_at"]) < ttl:
        return _JWKS_CACHE["keys"]

    uri = _jwks_uri()
    resp = httpx.get(uri, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    _JWKS_CACHE["keys"] = data
    _JWKS_CACHE["fetched_at"] = now
    return data


def _allowed_roles() -> set[str]:
    s = _settings()
    raw = getattr(s, "ida_admin_roles", None) or "platform_admin,tenant_admin"
    if isinstance(raw, str):
        raw = raw.strip()
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    return {str(r).lower() for r in parsed if r}
            except (ValueError, TypeError):
                pass
        return {r.strip().lower() for r in raw.split(",") if r.strip()}
    return {"platform_admin", "tenant_admin"}


def _token_roles(claims: dict) -> set[str]:
    roles: set[str] = set()
    for key in ("roles",):
        val = claims.get(key)
        if isinstance(val, str):
            roles.add(val.lower())
        elif isinstance(val, (list, tuple)):
            roles.update(str(r).lower() for r in val)
    realm = claims.get("realm_access") or {}
    if isinstance(realm, dict):
        for r in realm.get("roles") or []:
            roles.add(str(r).lower())
    resource = claims.get("resource_access") or {}
    if isinstance(resource, dict):
        for client_roles in resource.values():
            if isinstance(client_roles, dict):
                for r in client_roles.get("roles") or []:
                    roles.add(str(r).lower())
    return roles


def verify_admin_token(token: str) -> dict:
    """Validate an IDA access token for admin-console access."""
    from jose import jwt
    from jose.exceptions import JWTError

    s = _settings()
    issuer = _issuer()
    audience = getattr(s, "ida_admin_audience", None) or "gateway-admin"
    # python-jose's jwt.decode() has no `leeway` kwarg (unlike PyJWT). Clock skew
    # is typically small enough that we rely on Keycloak's short-lived tokens.

    try:
        jwks = _fetch_jwks()
        claims = jwt.decode(
            token,
            jwks,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"verify_aud": bool(audience), "verify_iss": bool(issuer)},
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid token: {exc}",
        ) from exc
    except Exception as exc:
        logger.warning("JWKS/token validation failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        ) from exc

    allowed = _allowed_roles()
    found = _token_roles(claims)
    if allowed and not (found & allowed):
        logger.warning(
            "ida_admin_role_denied email=%s found_roles=%s required=%s",
            claims.get("email") or claims.get("preferred_username"),
            sorted(found),
            sorted(allowed),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient privileges for gateway administration.",
        )
    return claims


def public_config() -> dict:
    issuer = _issuer()
    s = _settings()
    client_id = getattr(s, "ida_admin_audience", None) or "gateway-admin"
    return {
        "enabled": bool(issuer),
        "authority": issuer,
        "clientId": client_id,
    }
