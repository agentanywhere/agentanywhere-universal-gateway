"""OAuth2 integration support.

This module provides:

* Provider registration (multiple OAuth2 providers can be registered).
* Authorization-code flow with optional PKCE (RFC 7636).
* Client-credentials grant.
* Refresh-token grant.
* Token introspection (RFC 7662) and revocation (RFC 7009) over the
  registered providers.
* Local token caching so the gateway itself can authenticate requests
  using OAuth2 access tokens (alongside API keys).

The implementation uses ``httpx`` for HTTP calls and falls back to
in-memory storage. It is intentionally provider-agnostic: any
RFC 6749-compliant provider works.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from app.time_utils import utcnow
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
import structlog
from fastapi import HTTPException, Request, status

from app.config import get_settings

logger = structlog.get_logger()
settings = get_settings()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OAuth2Error(HTTPException):
    """OAuth2 protocol error mapped to RFC 6749 error responses."""

    def __init__(
        self,
        error: str,
        description: Optional[str] = None,
        status_code: int = status.HTTP_400_BAD_REQUEST,
    ):
        detail = {"error": error}
        if description:
            detail["error_description"] = description
        super().__init__(status_code=status_code, detail=detail)


# ---------------------------------------------------------------------------
# Provider config
# ---------------------------------------------------------------------------


@dataclass
class OAuth2Client:
    """OAuth2 provider configuration."""

    client_id: str
    client_secret: str
    authorization_endpoint: str
    token_endpoint: str
    userinfo_endpoint: Optional[str] = None
    introspection_endpoint: Optional[str] = None
    revocation_endpoint: Optional[str] = None
    scopes: List[str] = field(default_factory=lambda: ["openid", "profile", "email"])

    def basic_auth_header(self) -> str:
        creds = f"{self.client_id}:{self.client_secret}".encode()
        return "Basic " + base64.b64encode(creds).decode()


# ---------------------------------------------------------------------------
# Token bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class StoredToken:
    """A token issued to or fetched by the gateway."""

    token_id: str
    provider: str
    access_token: str
    refresh_token: Optional[str]
    token_type: str
    expires_at: datetime
    scope: str = ""
    user_info: Optional[Dict[str, Any]] = None

    def is_expired(self) -> bool:
        return utcnow() >= self.expires_at

    def to_response(self) -> Dict[str, Any]:
        return {
            "token_id": self.token_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": max(int((self.expires_at - utcnow()).total_seconds()), 0),
            "scope": self.scope,
        }


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def generate_pkce_pair() -> Tuple[str, str]:
    """Return a ``(code_verifier, code_challenge)`` pair (RFC 7636 S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class OAuth2Manager:
    """Manages OAuth2 providers and tokens."""

    def __init__(self):
        self.clients: Dict[str, OAuth2Client] = {}
        # token_id -> StoredToken
        self.tokens: Dict[str, StoredToken] = {}
        # access_token -> token_id (for fast bearer-token auth)
        self._access_index: Dict[str, str] = {}
        # in-flight authorization-code states: state -> { provider, code_verifier, redirect_uri }
        self._states: Dict[str, Dict[str, Any]] = {}

    # --- provider registration ----------------------------------------

    def register_provider(
        self,
        name: str,
        client_id: str,
        client_secret: str,
        authorization_endpoint: str,
        token_endpoint: str,
        userinfo_endpoint: Optional[str] = None,
        introspection_endpoint: Optional[str] = None,
        revocation_endpoint: Optional[str] = None,
        scopes: Optional[List[str]] = None,
    ) -> OAuth2Client:
        client = OAuth2Client(
            client_id=client_id,
            client_secret=client_secret,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            userinfo_endpoint=userinfo_endpoint,
            introspection_endpoint=introspection_endpoint,
            revocation_endpoint=revocation_endpoint,
            scopes=scopes or ["openid", "profile", "email"],
        )
        self.clients[name] = client
        logger.info("oauth2_provider_registered", provider=name)
        return client

    def remove_provider(self, name: str) -> bool:
        return self.clients.pop(name, None) is not None

    def list_providers(self) -> List[str]:
        return list(self.clients.keys())

    def _require_client(self, provider: str) -> OAuth2Client:
        client = self.clients.get(provider)
        if not client:
            raise OAuth2Error(
                "invalid_client",
                f"OAuth2 provider '{provider}' not registered",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return client

    # --- authorization code flow --------------------------------------

    async def get_authorization_url(
        self,
        provider: str,
        redirect_uri: str,
        state: Optional[str] = None,
        scopes: Optional[List[str]] = None,
        use_pkce: bool = True,
    ) -> Dict[str, str]:
        """Build an authorization URL.

        When PKCE is enabled the verifier is stored server-side keyed by
        ``state`` so it can be replayed during the token exchange.
        """
        client = self._require_client(provider)
        if not state:
            state = secrets.token_urlsafe(32)

        params = {
            "client_id": client.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "scope": " ".join(scopes or client.scopes),
        }

        verifier = None
        if use_pkce:
            verifier, challenge = generate_pkce_pair()
            params["code_challenge"] = challenge
            params["code_challenge_method"] = "S256"

        self._states[state] = {
            "provider": provider,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
            "created_at": utcnow(),
        }

        url = f"{client.authorization_endpoint}?{urlencode(params)}"
        return {
            "authorization_url": url,
            "state": state,
            "provider": provider,
        }

    async def exchange_code_for_token(
        self,
        provider: str,
        code: str,
        redirect_uri: str,
        state: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Exchange an authorization code for an access token."""
        client = self._require_client(provider)

        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
        }

        if state and state in self._states:
            verifier = self._states[state].get("code_verifier")
            if verifier:
                data["code_verifier"] = verifier

        token_data = await self._post_token(client, data)
        stored = self._store_token(provider, token_data)

        if state in self._states:
            del self._states[state]

        return stored.to_response()

    # --- client credentials -------------------------------------------

    async def client_credentials(
        self,
        provider: str,
        scopes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Perform the client-credentials grant."""
        client = self._require_client(provider)
        data = {
            "grant_type": "client_credentials",
            "client_id": client.client_id,
            "client_secret": client.client_secret,
        }
        if scopes:
            data["scope"] = " ".join(scopes)

        token_data = await self._post_token(client, data)
        stored = self._store_token(provider, token_data)
        return stored.to_response()

    # --- refresh token ------------------------------------------------

    async def refresh_token(
        self,
        provider: str,
        refresh_token: str,
    ) -> Dict[str, Any]:
        """Use a refresh token to obtain a new access token."""
        client = self._require_client(provider)
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
        }
        token_data = await self._post_token(client, data)

        # Drop the old token if we know about it (some providers rotate
        # the refresh token, others reuse it).
        old_id = None
        for tid, stored in list(self.tokens.items()):
            if stored.refresh_token == refresh_token:
                old_id = tid
                break
        if old_id:
            self._remove_token(old_id)

        stored = self._store_token(provider, token_data)
        return stored.to_response()

    # --- introspection ------------------------------------------------

    async def introspect(
        self,
        provider: str,
        token: str,
        token_type_hint: str = "access_token",
    ) -> Dict[str, Any]:
        """Introspect a token (RFC 7662).

        If the provider exposes an introspection endpoint, that is used.
        Otherwise the gateway answers from its local cache for tokens it
        issued.
        """
        client = self._require_client(provider)

        if client.introspection_endpoint:
            async with httpx.AsyncClient(timeout=settings.default_timeout) as http_client:
                response = await http_client.post(
                    client.introspection_endpoint,
                    data={"token": token, "token_type_hint": token_type_hint},
                    headers={
                        "Authorization": client.basic_auth_header(),
                        "Accept": "application/json",
                    },
                )
                if response.status_code == 200:
                    return response.json()
                logger.warning(
                    "oauth2_introspection_failed",
                    provider=provider,
                    status=response.status_code,
                )

        token_id = self._access_index.get(token)
        if token_id and token_id in self.tokens:
            stored = self.tokens[token_id]
            return {
                "active": not stored.is_expired(),
                "client_id": client.client_id,
                "token_type": stored.token_type,
                "exp": int(stored.expires_at.timestamp()),
                "scope": stored.scope,
            }

        return {"active": False}

    # --- revocation ---------------------------------------------------

    async def revoke(
        self,
        provider: str,
        token: str,
        token_type_hint: str = "access_token",
    ) -> bool:
        """Revoke a token (RFC 7009)."""
        client = self._require_client(provider)

        # Local revocation always succeeds.
        token_id = self._access_index.get(token)
        if token_id:
            self._remove_token(token_id)

        if client.revocation_endpoint:
            try:
                async with httpx.AsyncClient(timeout=settings.default_timeout) as http_client:
                    response = await http_client.post(
                        client.revocation_endpoint,
                        data={"token": token, "token_type_hint": token_type_hint},
                        headers={"Authorization": client.basic_auth_header()},
                    )
                    return response.status_code in (200, 204)
            except httpx.HTTPError as e:
                logger.warning("oauth2_revoke_failed", provider=provider, error=str(e))
                return False

        return True

    # --- userinfo -----------------------------------------------------

    async def get_user_info(self, provider: str, access_token: str) -> Dict[str, Any]:
        client = self._require_client(provider)
        if not client.userinfo_endpoint:
            return {}

        async with httpx.AsyncClient(timeout=settings.default_timeout) as http_client:
            response = await http_client.get(
                client.userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if response.status_code == 200:
                return response.json()
            return {}

    # --- token verification (used by auth middleware) -----------------

    async def verify_access_token(self, access_token: str) -> Optional[StoredToken]:
        """Verify an access token against the local token cache.

        Returns the matching ``StoredToken`` if the token is known and
        not expired, otherwise ``None``.
        """
        token_id = self._access_index.get(access_token)
        if not token_id:
            return None
        stored = self.tokens.get(token_id)
        if not stored:
            return None
        if stored.is_expired():
            self._remove_token(token_id)
            return None
        return stored

    async def verify_token(self, token_id: str) -> Optional[Dict[str, Any]]:
        """Backwards-compatible wrapper for token-id verification."""
        stored = self.tokens.get(token_id)
        if not stored:
            return None
        if stored.is_expired():
            self._remove_token(token_id)
            return None
        return {
            "provider": stored.provider,
            "access_token": stored.access_token,
            "refresh_token": stored.refresh_token,
            "expires_at": stored.expires_at,
            "token_type": stored.token_type,
            "scope": stored.scope,
        }

    # --- internals ----------------------------------------------------

    async def _post_token(self, client: OAuth2Client, data: Dict[str, Any]) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=settings.default_timeout) as http_client:
            response = await http_client.post(
                client.token_endpoint,
                data=data,
                headers={"Accept": "application/json"},
            )
        if response.status_code != 200:
            try:
                payload = response.json()
            except Exception:
                payload = {"error": "server_error", "error_description": response.text[:200]}
            raise OAuth2Error(
                error=payload.get("error", "invalid_grant"),
                description=payload.get("error_description"),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        return response.json()

    def _store_token(self, provider: str, token_data: Dict[str, Any]) -> StoredToken:
        access_token = token_data.get("access_token")
        if not access_token:
            raise OAuth2Error(
                "invalid_token_response",
                "Token endpoint did not return an access_token",
            )

        expires_in = int(token_data.get("expires_in", 3600))
        token_id = secrets.token_urlsafe(32)
        stored = StoredToken(
            token_id=token_id,
            provider=provider,
            access_token=access_token,
            refresh_token=token_data.get("refresh_token"),
            token_type=token_data.get("token_type", "Bearer"),
            expires_at=utcnow() + timedelta(seconds=expires_in),
            scope=token_data.get("scope", ""),
        )

        self.tokens[token_id] = stored
        self._access_index[access_token] = token_id
        return stored

    def _remove_token(self, token_id: str) -> None:
        stored = self.tokens.pop(token_id, None)
        if stored:
            self._access_index.pop(stored.access_token, None)


oauth2_manager = OAuth2Manager()
