"""Tests for authentication middleware."""
import pytest
from fastapi import Request, HTTPException
from unittest.mock import Mock, AsyncMock, patch
from app.middleware.auth import AuthMiddleware
from app.config import get_settings
from app.repositories.api_key_repo import APIKeyRepository

settings = get_settings()


@pytest.mark.asyncio
async def test_get_api_key_from_header():
    """Test extracting API key from headers."""
    middleware = AuthMiddleware()
    
    # Test X-API-Key header
    request = Mock(spec=Request)
    request.headers = {"X-API-Key": "test-key-123"}
    key = middleware._get_api_key_from_header(request)
    assert key == "test-key-123"
    
    # Test Authorization header
    request.headers = {"Authorization": "Bearer bearer-key-456"}
    key = middleware._get_api_key_from_header(request)
    assert key == "bearer-key-456"
    
    # Test no header
    request.headers = {}
    key = middleware._get_api_key_from_header(request)
    assert key is None


@pytest.mark.asyncio
async def test_validate_api_key_with_db(db_session, sample_api_key):
    """Test API key validation with database."""
    middleware = AuthMiddleware()
    api_key, db_key = sample_api_key
    
    validated = await middleware._validate_api_key(api_key, db_session)
    
    assert validated is not None
    assert validated.key == api_key
    assert validated.name == db_key.name


@pytest.mark.asyncio
async def test_validate_api_key_inactive(db_session):
    """Test validation of inactive API key."""
    middleware = AuthMiddleware()
    
    # Create inactive key
    api_key, db_key = await APIKeyRepository.create(
        db=db_session,
        name="Inactive Key",
    )
    await APIKeyRepository.toggle_active(db_session, db_key.id)
    
    validated = await middleware._validate_api_key(api_key, db_session)
    assert validated is None  # Inactive keys should not validate


@pytest.mark.asyncio
async def test_validate_api_key_expired(db_session):
    """Test validation of expired API key."""
    from datetime import timedelta
    from app.time_utils import utcnow
    middleware = AuthMiddleware()
    
    # Create expired key
    expired_date = utcnow() - timedelta(days=1)
    api_key, db_key = await APIKeyRepository.create(
        db=db_session,
        name="Expired Key",
        expires_at=expired_date,
    )
    
    validated = await middleware._validate_api_key(api_key, db_session)
    assert validated is None  # Expired keys should not validate


@pytest.mark.asyncio
async def test_bootstrap_key_requires_configured_secret(db_session, monkeypatch):
    """Break-glass: the configured SECRET_KEY authenticates only when it has
    been changed from the shipped default."""
    middleware = AuthMiddleware()
    from app.middleware import auth as auth_module

    # Off by default: even a real secret is refused on the proxy.
    monkeypatch.setattr(settings, "secret_key", "a-real-strong-secret-value-1234567890")
    monkeypatch.setattr(auth_module.settings, "secret_key", settings.secret_key)
    assert await middleware._validate_api_key(settings.secret_key, None) is None

    # When enabled, a real, non-default secret authenticates as the bootstrap key.
    monkeypatch.setattr(auth_module.settings, "allow_secret_key_api_key", True)
    validated = await middleware._validate_api_key(settings.secret_key, None)
    assert validated is not None
    assert validated.name == "default"

    # The shipped default grants nothing, even when enabled.
    monkeypatch.setattr(settings, "secret_key", "change-me-in-production-use-strong-random-key")
    monkeypatch.setattr(auth_module.settings, "secret_key", settings.secret_key)
    assert await middleware._validate_api_key(settings.secret_key, None) is None


@pytest.mark.asyncio
async def test_sk_prefix_is_not_a_valid_key(db_session):
    """Regression: any value beginning with 'sk-' must NOT authenticate.

    Previously `startswith("sk-")` accepted arbitrary keys — an auth bypass.
    """
    middleware = AuthMiddleware()
    assert await middleware._validate_api_key("sk-test123", None) is None
    assert await middleware._validate_api_key("sk-anything-at-all", None) is None


@pytest.mark.asyncio
async def test_authenticate_success(db_session, sample_api_key):
    """Test successful authentication."""
    middleware = AuthMiddleware()
    api_key, db_key = sample_api_key
    
    request = Mock(spec=Request)
    request.headers = {"X-API-Key": api_key}
    
    result = await middleware.authenticate(request, db_session)
    
    assert result is not None
    assert result.key == api_key


@pytest.mark.asyncio
async def test_authenticate_no_key():
    """Test authentication with no API key."""
    middleware = AuthMiddleware()
    
    request = Mock(spec=Request)
    request.headers = {}
    
    result = await middleware.authenticate(request, None)
    assert result is None


@pytest.mark.asyncio
async def test_authenticate_invalid_key(db_session):
    """Test authentication with invalid key."""
    middleware = AuthMiddleware()
    
    request = Mock(spec=Request)
    request.headers = {"X-API-Key": "invalid-key-12345"}
    
    with pytest.raises(HTTPException) as exc_info:
        await middleware.authenticate(request, db_session)
    
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_verify_hmac_signature():
    """Test HMAC signature verification."""
    import hmac
    import hashlib
    middleware = AuthMiddleware()
    
    secret = "test-secret"
    body = b"test body"
    
    # Create valid signature
    signature = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    
    request = Mock(spec=Request)
    request.headers = {"X-Signature": signature}
    request._body = body
    
    result = middleware.verify_hmac_signature(request, secret)
    assert result is True
    
    # Test invalid signature
    request.headers = {"X-Signature": "invalid-signature"}
    result = middleware.verify_hmac_signature(request, secret)
    assert result is False


