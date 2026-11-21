"""The rate limiter must not try an unreachable Redis on every request.

With REDIS_ENABLED=true and no Redis listening (the env.example defaults),
each request checked the per-minute and the per-hour limit, and each check
tried Redis before falling back to the in-memory counters. A connection
attempt to a closed port can take seconds, so every proxied request waited
that long several times over. After a failed Redis call the limiter now uses
the in-memory counters for a backoff window without trying Redis.
"""
from unittest.mock import Mock

import pytest
import redis
from fastapi import Request

from app.middleware import rate_limit as rate_limit_module
from app.middleware.rate_limit import RateLimiter
from app.models import APIKey


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _DownRedis:
    """A Redis client whose every call fails as a refused connection would."""

    def __init__(self):
        self.calls = 0

    def get(self, key):
        self.calls += 1
        raise redis.ConnectionError("Connection refused")

    def pipeline(self):
        self.calls += 1
        raise redis.ConnectionError("Connection refused")


class _FakeRedis:
    """A working in-process stand-in for the commands the limiter uses."""

    def __init__(self):
        self.store = {}
        self.calls = 0

    def get(self, key):
        self.calls += 1
        return self.store.get(key)

    def pipeline(self):
        self.calls += 1
        fake = self

        class _Pipe:
            def incr(self, key):
                fake.store[key] = str(int(fake.store.get(key, 0)) + 1)

            def expire(self, key, seconds):
                pass

            def execute(self):
                pass

        return _Pipe()


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(rate_limit_module.time, "monotonic", c)
    return c


@pytest.fixture
def limiter(monkeypatch):
    # RateLimiter reads the settings object bound in its module.
    monkeypatch.setattr(rate_limit_module.settings, "rate_limit_enabled", True)
    return RateLimiter()


@pytest.fixture
def request_():
    request = Mock(spec=Request)
    request.client = Mock()
    request.client.host = "127.0.0.1"
    request.headers = {}
    request.state = Mock()
    return request


@pytest.fixture
def api_key():
    return APIKey(key="sk-backoff", name="k", rate_limit_per_minute=5, rate_limit_per_hour=100)


def test_unreachable_redis_is_tried_once_then_skipped_for_the_window(
    limiter, clock, request_, api_key
):
    down = _DownRedis()
    limiter.redis_client = down

    # First request: the per-minute check fails over to memory and marks Redis
    # down; the per-hour check in the same request does not try Redis again.
    assert limiter.check_rate_limit(request_, api_key) is True
    assert down.calls == 1

    # Later requests inside the window never touch Redis.
    for _ in range(3):
        clock.now += 1
        assert limiter.check_rate_limit(request_, api_key) is True
    assert down.calls == 1


def test_limits_still_enforced_from_memory_while_redis_is_down(
    limiter, clock, request_, api_key
):
    from app.exceptions import RateLimitError

    limiter.redis_client = _DownRedis()
    for _ in range(api_key.rate_limit_per_minute):
        assert limiter.check_rate_limit(request_, api_key) is True
    with pytest.raises(RateLimitError):
        limiter.check_rate_limit(request_, api_key)


def test_redis_is_tried_again_after_the_window(limiter, clock, request_, api_key):
    down = _DownRedis()
    limiter.redis_client = down
    assert limiter.check_rate_limit(request_, api_key) is True
    assert down.calls == 1

    clock.now += limiter.REDIS_RETRY_SECONDS - 0.5
    assert limiter.check_rate_limit(request_, api_key) is True
    assert down.calls == 1

    # Window over, Redis still down: one more attempt, then backed off again.
    clock.now += 1
    assert limiter.check_rate_limit(request_, api_key) is True
    assert down.calls == 2
    assert limiter.check_rate_limit(request_, api_key) is True
    assert down.calls == 2


def test_recovers_to_redis_after_the_window(limiter, clock, request_, api_key):
    limiter.redis_client = _DownRedis()
    assert limiter.check_rate_limit(request_, api_key) is True

    # Redis comes back on the same client object.
    up = _FakeRedis()
    limiter.redis_client.get = up.get
    limiter.redis_client.pipeline = up.pipeline
    clock.now += limiter.REDIS_RETRY_SECONDS + 1
    assert limiter.check_rate_limit(request_, api_key) is True
    # Both the per-minute and per-hour checks went to Redis (get + pipeline each).
    assert up.calls == 4
    assert sorted(int(v) for v in up.store.values()) == [1, 1]


def test_a_new_client_is_tried_immediately(limiter, clock, request_, api_key):
    limiter.redis_client = _DownRedis()
    assert limiter.check_rate_limit(request_, api_key) is True

    up = _FakeRedis()
    limiter.redis_client = up
    assert limiter.check_rate_limit(request_, api_key) is True
    assert up.calls == 4


def test_working_redis_is_used_on_every_check(limiter, clock, request_, api_key):
    up = _FakeRedis()
    limiter.redis_client = up
    for _ in range(3):
        assert limiter.check_rate_limit(request_, api_key) is True
    assert up.calls == 12  # get + pipeline for both windows, each request
    assert limiter._memory_counters == {}


def test_non_redis_error_falls_back_without_backing_off(limiter, clock, request_, api_key):
    up = _FakeRedis()
    up.get = Mock(return_value="not-a-number")
    limiter.redis_client = up
    assert limiter.check_rate_limit(request_, api_key) is True
    assert limiter._redis_available() is True


def test_rate_limit_client_uses_short_bounded_timeouts(monkeypatch):
    settings = rate_limit_module.settings
    monkeypatch.setattr(settings, "rate_limit_redis_url", "redis://127.0.0.1:1/1")
    monkeypatch.setattr(settings, "redis_socket_timeout", 0.25)
    kwargs = RateLimiter().redis_client.connection_pool.connection_kwargs
    assert kwargs["socket_connect_timeout"] == 0.25
    assert kwargs["socket_timeout"] == 0.25
