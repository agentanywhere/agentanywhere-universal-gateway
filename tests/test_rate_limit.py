"""Tests for rate limiting."""
import pytest
from unittest.mock import Mock, patch
from fastapi import Request, HTTPException
from app.middleware.rate_limit import RateLimiter
from app.models import APIKey
from app.config import get_settings

settings = get_settings()


@pytest.fixture
def rate_limiter():
    """Create rate limiter instance."""
    return RateLimiter()


@pytest.fixture
def mock_request():
    """Create mock request."""
    request = Mock(spec=Request)
    request.client = Mock()
    request.client.host = "127.0.0.1"
    return request


@pytest.fixture
def sample_api_key():
    """Create sample API key."""
    return APIKey(
        key="sk-test123",
        name="Test Key",
        rate_limit_per_minute=10,
        rate_limit_per_hour=100,
    )


def test_get_key_func_with_api_key(rate_limiter, sample_api_key):
    """Test key function with API key."""
    key_func = rate_limiter.get_key_func(sample_api_key)
    request = Mock(spec=Request)
    
    key = key_func(request)
    assert key == f"api_key:{sample_api_key.key}"


def test_get_key_func_without_api_key(rate_limiter):
    """Test key function without API key."""
    key_func = rate_limiter.get_key_func(None)
    request = Mock(spec=Request)
    request.client = Mock()
    request.client.host = "192.168.1.1"
    
    key = key_func(request)
    assert key == "192.168.1.1"


@patch('app.middleware.rate_limit.RateLimiter._check_limit')
def test_check_rate_limit_success(mock_check, rate_limiter, mock_request, sample_api_key):
    """Test successful rate limit check."""
    mock_check.return_value = True
    
    result = rate_limiter.check_rate_limit(mock_request, sample_api_key)
    assert result is True


@patch('app.middleware.rate_limit.RateLimiter._check_limit')
def test_check_rate_limit_exceeded_minute(mock_check, rate_limiter, mock_request, sample_api_key):
    """Test rate limit exceeded for per-minute."""
    def side_effect(*args, **kwargs):
        # First call (per minute) fails
        if not hasattr(side_effect, 'called'):
            side_effect.called = True
            return False
        return True
    
    mock_check.side_effect = side_effect
    
    with pytest.raises(HTTPException) as exc_info:
        rate_limiter.check_rate_limit(mock_request, sample_api_key)
    
    assert exc_info.value.status_code == 429
    assert "per minute" in str(exc_info.value.detail).lower()


@patch('app.middleware.rate_limit.RateLimiter._check_limit')
def test_check_rate_limit_exceeded_hour(mock_check, rate_limiter, mock_request, sample_api_key):
    """Test rate limit exceeded for per-hour."""
    def side_effect(*args, **kwargs):
        # Second call (per hour) fails
        if not hasattr(side_effect, 'count'):
            side_effect.count = 0
        side_effect.count += 1
        if side_effect.count == 2:
            return False
        return True
    
    mock_check.side_effect = side_effect
    
    with pytest.raises(HTTPException) as exc_info:
        rate_limiter.check_rate_limit(mock_request, sample_api_key)
    
    assert exc_info.value.status_code == 429
    assert "per hour" in str(exc_info.value.detail).lower()


def test_check_rate_limit_disabled(rate_limiter, mock_request):
    """Test rate limiting when disabled."""
    with patch.object(settings, 'rate_limit_enabled', False):
        result = rate_limiter.check_rate_limit(mock_request, None)
        assert result is True


@patch('app.middleware.rate_limit.redis')
def test_check_limit_with_redis(mock_redis, rate_limiter, mock_request, sample_api_key):
    """Test rate limit check with Redis."""
    # Mock Redis client
    mock_client = Mock()
    mock_client.get.return_value = "5"  # Current count
    mock_client.pipeline.return_value = Mock()
    rate_limiter.redis_client = mock_client
    
    result = rate_limiter._check_limit(mock_request, sample_api_key, 10, 60)
    assert result is True  # 5 < 10, so should pass


@patch('app.middleware.rate_limit.redis')
def test_check_limit_exceeded_redis(mock_redis, rate_limiter, mock_request, sample_api_key):
    """Test rate limit exceeded with Redis."""
    # Mock Redis client
    mock_client = Mock()
    mock_client.get.return_value = "15"  # Current count exceeds limit
    rate_limiter.redis_client = mock_client
    
    result = rate_limiter._check_limit(mock_request, sample_api_key, 10, 60)
    assert result is False  # 15 >= 10, so should fail


@patch('app.middleware.rate_limit.redis')
def test_check_limit_redis_failure_falls_back_to_memory(mock_redis, rate_limiter, mock_request, sample_api_key):
    """When Redis errors, fall back to the in-memory limiter (not fail-open)."""
    # Mock Redis client that raises exception
    mock_client = Mock()
    mock_client.get.side_effect = Exception("Redis error")
    rate_limiter.redis_client = mock_client

    # First call within the window is allowed via the in-memory fallback...
    assert rate_limiter._check_limit(mock_request, sample_api_key, 2, 60) is True
    assert rate_limiter._check_limit(mock_request, sample_api_key, 2, 60) is True
    # ...and the third is rejected, proving the fallback actually counts.
    assert rate_limiter._check_limit(mock_request, sample_api_key, 2, 60) is False


def test_memory_limiter_enforces_without_redis(mock_request, sample_api_key):
    """Regression for the D3 defect: with no Redis configured, the per-minute
    limit must actually be enforced (previously it silently allowed everything,
    and the 429 raise was dead code nested inside an `except`)."""
    rl = RateLimiter()
    rl.redis_client = None  # force the in-memory path
    # sample_api_key allows 10/minute.
    for _ in range(10):
        assert rl.check_rate_limit(mock_request, sample_api_key) is True
    with pytest.raises(HTTPException) as exc_info:
        rl.check_rate_limit(mock_request, sample_api_key)
    assert exc_info.value.status_code == 429
    assert "per minute" in str(exc_info.value.detail).lower()


