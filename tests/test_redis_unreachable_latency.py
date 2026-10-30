"""A Redis that does not answer must not slow every request.

The API-key cache calls Redis synchronously on each request. With no socket
timeout, a Redis that is absent or silent (on Windows a refused loopback
connection alone takes seconds) made each request wait, and sequential
requests were spread so far apart that a per-minute rate limit never tripped.
"""
import socket
import time
from types import SimpleNamespace

from app.config import get_settings
from app.middleware.auth import AuthMiddleware


def _silent_server():
    """Accepts connections (kernel backlog) and never replies."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv


def test_silent_redis_costs_one_short_timeout_then_is_skipped(monkeypatch):
    srv = _silent_server()
    try:
        settings = get_settings()
        monkeypatch.setattr(settings, "redis_enabled", True)
        monkeypatch.setattr(settings, "redis_socket_timeout", 0.3)
        monkeypatch.setattr(
            settings, "redis_url", f"redis://127.0.0.1:{srv.getsockname()[1]}/0"
        )
        auth = AuthMiddleware()

        start = time.monotonic()
        assert auth._cache_get("h") is None
        assert time.monotonic() - start < 2.0

        # Subsequent calls skip Redis entirely while it is marked down.
        start = time.monotonic()
        for _ in range(20):
            assert auth._cache_get("h") is None
            auth._cache_put("h", SimpleNamespace(expires_at=None))
        assert time.monotonic() - start < 0.1
    finally:
        srv.close()
