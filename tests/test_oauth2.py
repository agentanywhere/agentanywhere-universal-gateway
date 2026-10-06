"""Tests for the OAuth2 manager and grant flows.

External HTTP calls are mocked with ``httpx.MockTransport`` so the tests
are deterministic.
"""
import asyncio
import base64
import hashlib
import json
from datetime import timedelta
from app.time_utils import utcnow
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.oauth2 import (
    OAuth2Error,
    OAuth2Manager,
    StoredToken,
    generate_pkce_pair,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_token_response(handler):
    """Patch httpx.AsyncClient with a MockTransport for one test."""

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        # Replace any timeout/transport arg with our mock transport.
        kwargs.pop("transport", None)
        return real_async_client(transport=transport, **kwargs)

    return patch("app.oauth2.httpx.AsyncClient", _factory)


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


class TestPKCE:
    def test_generate_pkce_pair_produces_matching_challenge(self):
        verifier, challenge = generate_pkce_pair()
        digest = hashlib.sha256(verifier.encode()).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        assert challenge == expected
        assert len(verifier) >= 43  # RFC 7636


# ---------------------------------------------------------------------------
# Manager basics
# ---------------------------------------------------------------------------


class TestProviderManagement:
    def test_register_and_remove_provider(self):
        mgr = OAuth2Manager()
        mgr.register_provider(
            name="acme",
            client_id="cid",
            client_secret="secret",
            authorization_endpoint="https://acme.example/oauth/authorize",
            token_endpoint="https://acme.example/oauth/token",
        )
        assert "acme" in mgr.list_providers()
        assert mgr.remove_provider("acme") is True
        assert mgr.remove_provider("acme") is False


# ---------------------------------------------------------------------------
# Authorization URL + PKCE storage
# ---------------------------------------------------------------------------


class TestAuthorizationUrl:
    @pytest.mark.asyncio
    async def test_authorization_url_with_pkce(self):
        mgr = OAuth2Manager()
        mgr.register_provider(
            name="acme",
            client_id="cid",
            client_secret="secret",
            authorization_endpoint="https://acme.example/auth",
            token_endpoint="https://acme.example/token",
        )
        result = await mgr.get_authorization_url(
            provider="acme",
            redirect_uri="https://app.example/cb",
            use_pkce=True,
        )
        assert "code_challenge=" in result["authorization_url"]
        assert "code_challenge_method=S256" in result["authorization_url"]
        state = result["state"]
        assert state in mgr._states
        assert mgr._states[state]["code_verifier"] is not None

    @pytest.mark.asyncio
    async def test_authorization_url_without_pkce(self):
        mgr = OAuth2Manager()
        mgr.register_provider(
            name="acme",
            client_id="cid",
            client_secret="secret",
            authorization_endpoint="https://acme.example/auth",
            token_endpoint="https://acme.example/token",
        )
        result = await mgr.get_authorization_url(
            provider="acme",
            redirect_uri="https://app.example/cb",
            use_pkce=False,
        )
        assert "code_challenge" not in result["authorization_url"]

    @pytest.mark.asyncio
    async def test_authorization_url_unknown_provider_raises(self):
        mgr = OAuth2Manager()
        with pytest.raises(OAuth2Error):
            await mgr.get_authorization_url("missing", "https://app.example/cb")


# ---------------------------------------------------------------------------
# Token grants (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorization_code_grant_stores_token():
    mgr = OAuth2Manager()
    mgr.register_provider(
        name="acme",
        client_id="cid",
        client_secret="secret",
        authorization_endpoint="https://acme.example/auth",
        token_endpoint="https://acme.example/token",
    )

    received: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["url"] = str(request.url)
        received["body"] = request.content.decode()
        return httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "read",
            },
        )

    with _mock_token_response(handler):
        result = await mgr.exchange_code_for_token(
            provider="acme",
            code="auth-code",
            redirect_uri="https://app.example/cb",
        )

    assert result["access_token"] == "at-1"
    assert result["refresh_token"] == "rt-1"
    assert result["expires_in"] > 0

    # And the token is now resolvable from the access-token cache.
    stored = await mgr.verify_access_token("at-1")
    assert isinstance(stored, StoredToken)
    assert stored.provider == "acme"


@pytest.mark.asyncio
async def test_client_credentials_grant():
    mgr = OAuth2Manager()
    mgr.register_provider(
        name="acme",
        client_id="cid",
        client_secret="secret",
        authorization_endpoint="https://acme.example/auth",
        token_endpoint="https://acme.example/token",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "grant_type=client_credentials" in body
        return httpx.Response(
            200,
            json={"access_token": "cc-1", "token_type": "Bearer", "expires_in": 600},
        )

    with _mock_token_response(handler):
        result = await mgr.client_credentials("acme", scopes=["api:read"])

    assert result["access_token"] == "cc-1"
    assert result["expires_in"] > 0


@pytest.mark.asyncio
async def test_refresh_token_grant_replaces_old_token():
    mgr = OAuth2Manager()
    mgr.register_provider(
        name="acme",
        client_id="cid",
        client_secret="secret",
        authorization_endpoint="https://acme.example/auth",
        token_endpoint="https://acme.example/token",
    )

    # Pre-seed a token whose refresh_token we'll exchange.
    seeded = mgr._store_token(
        "acme",
        {"access_token": "old", "refresh_token": "rt-old", "expires_in": 3600},
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert "grant_type=refresh_token" in body
        assert "refresh_token=rt-old" in body
        return httpx.Response(
            200,
            json={
                "access_token": "new",
                "refresh_token": "rt-new",
                "expires_in": 3600,
            },
        )

    with _mock_token_response(handler):
        result = await mgr.refresh_token("acme", "rt-old")

    assert result["access_token"] == "new"
    # Old token should be evicted.
    assert seeded.token_id not in mgr.tokens
    assert "old" not in mgr._access_index


@pytest.mark.asyncio
async def test_token_endpoint_error_raises_oauth2_error():
    mgr = OAuth2Manager()
    mgr.register_provider(
        name="acme",
        client_id="cid",
        client_secret="secret",
        authorization_endpoint="https://acme.example/auth",
        token_endpoint="https://acme.example/token",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "bad code"},
        )

    with _mock_token_response(handler):
        with pytest.raises(OAuth2Error) as exc:
            await mgr.exchange_code_for_token(
                "acme", "bad-code", "https://app.example/cb"
            )

    assert exc.value.detail["error"] == "invalid_grant"


# ---------------------------------------------------------------------------
# Introspection / revocation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_introspect_falls_back_to_local_cache():
    mgr = OAuth2Manager()
    mgr.register_provider(
        name="acme",
        client_id="cid",
        client_secret="secret",
        authorization_endpoint="https://acme.example/auth",
        token_endpoint="https://acme.example/token",
    )
    mgr._store_token("acme", {"access_token": "at-x", "expires_in": 3600, "scope": "s"})
    result = await mgr.introspect("acme", "at-x")
    assert result["active"] is True
    assert result["client_id"] == "cid"


@pytest.mark.asyncio
async def test_introspect_unknown_token_inactive():
    mgr = OAuth2Manager()
    mgr.register_provider(
        name="acme",
        client_id="cid",
        client_secret="secret",
        authorization_endpoint="https://acme.example/auth",
        token_endpoint="https://acme.example/token",
    )
    result = await mgr.introspect("acme", "bogus")
    assert result == {"active": False}


@pytest.mark.asyncio
async def test_revoke_local_token_removes_from_cache():
    mgr = OAuth2Manager()
    mgr.register_provider(
        name="acme",
        client_id="cid",
        client_secret="secret",
        authorization_endpoint="https://acme.example/auth",
        token_endpoint="https://acme.example/token",
    )
    stored = mgr._store_token("acme", {"access_token": "at-x", "expires_in": 3600})
    await mgr.revoke("acme", "at-x")
    assert stored.token_id not in mgr.tokens
    assert "at-x" not in mgr._access_index


# ---------------------------------------------------------------------------
# Expiry semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_token_returns_none():
    mgr = OAuth2Manager()
    mgr.register_provider(
        name="acme",
        client_id="cid",
        client_secret="secret",
        authorization_endpoint="https://acme.example/auth",
        token_endpoint="https://acme.example/token",
    )
    stored = mgr._store_token("acme", {"access_token": "expiring", "expires_in": 0})
    # Force expiry
    stored.expires_at = utcnow() - timedelta(seconds=1)
    assert await mgr.verify_access_token("expiring") is None
    assert "expiring" not in mgr._access_index
