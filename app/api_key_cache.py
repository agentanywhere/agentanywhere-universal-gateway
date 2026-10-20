"""The Redis cache of validated API keys, and its invalidation.

The auth middleware caches a validated key for a few minutes so that a busy
key does not cost a database read per request. The cache is keyed by the
key's SHA-256 hash (never the key itself, so Redis holds no usable
credential), which is also what the database stores, so the admin API can
drop an entry when it deletes, disables or changes a key and the change takes
effect on the next request instead of up to five minutes later.

Every Redis client that holds cached keys registers here; with Redis shared
between processes, deleting the entry once is enough for all of them.
"""
from __future__ import annotations

import hashlib
import weakref
from typing import Any, Optional

CACHE_TTL_SECONDS = 300
CACHE_PREFIX = "api_key:"

# Objects with a ``redis_client`` attribute (AuthMiddleware instances).
_holders: "weakref.WeakSet[Any]" = weakref.WeakSet()


def hash_key(key: str) -> str:
    """SHA-256 hex digest of an API key, as stored in ``api_keys.key_hash``."""
    return hashlib.sha256(key.encode()).hexdigest()


def cache_key_for_hash(key_hash: str) -> str:
    return f"{CACHE_PREFIX}{key_hash}"


def register(holder: Any) -> None:
    """Register an object whose ``redis_client`` caches API keys."""
    try:
        _holders.add(holder)
    except TypeError:
        pass


def invalidate(key_hash: Optional[str], plaintext: Optional[str] = None) -> None:
    """Drop the cached record of a key. Best effort: never raises.

    ``plaintext`` also clears an entry written by an older release, which
    keyed the cache by the key itself.
    """
    if not key_hash and not plaintext:
        return
    names = []
    if key_hash:
        names.append(cache_key_for_hash(key_hash))
    if plaintext:
        names.append(f"{CACHE_PREFIX}{plaintext}")
    for holder in list(_holders):
        client = getattr(holder, "redis_client", None)
        if client is None:
            continue
        for name in names:
            try:
                client.delete(name)
            except Exception:  # noqa: BLE001 - Redis is optional
                pass


__all__ = [
    "CACHE_PREFIX",
    "CACHE_TTL_SECONDS",
    "cache_key_for_hash",
    "hash_key",
    "invalidate",
    "register",
]
