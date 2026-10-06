"""Dynamic route registry provider.

Bridges the DB-backed :class:`app.db_models.RouteModel` rows (written by
``POST /admin/routes``) to the live proxy, so a registered route is actually
honoured at request time.

**The bug this fixes.** ``POST /admin/routes`` persisted a row, and
:meth:`app.repositories.route_repo.RouteRepository.find_matching_route` existed
and was unit-tested — but nothing on the request path ever called it. The proxy
resolved every backend from hardcoded path prefixes in
:meth:`app.routing.Router._get_backend_url`, so a route you registered silently
did nothing. The docs presented it as the supported way to add a backend, which
made it a trap for operators (and, since the gateway is open source, for
external contributors).

Design mirrors :mod:`app.agent_registry`, which already solved the same problem
for ``/agents/<slug>``:

* **Load** — build an immutable snapshot from ``is_active`` rows and swap it in
  atomically. No per-request database work on the hot path.
* **Refresh** — a lightweight periodic reload so replicas converge on admin
  edits within seconds, on top of the immediate in-process swap done by the
  write path.
* **Match** — deterministic precedence: an exact path match wins; otherwise the
  **longest** matching wildcard prefix wins, so ``/api/v1/*`` beats ``/api/*``
  regardless of insertion order.

All functions are defensive: a registry failure must never take the gateway
down or block a request. Callers fall back to the static prefix routing.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

import structlog

from app.models import ProtocolType, RouteConfig

logger = structlog.get_logger()

# Immutable snapshot, swapped atomically. Each entry is
# (match_path, is_wildcard, prefix_len, RouteConfig).
_ROUTES: Tuple[Tuple[str, bool, int, RouteConfig], ...] = ()


def _to_config(row) -> Optional[RouteConfig]:
    """Build a RouteConfig from a DB row, or None if the row is unusable."""
    path = (getattr(row, "path", "") or "").strip()
    backend = (getattr(row, "backend_url", "") or "").strip()
    if not path or not backend:
        return None
    try:
        protocol = ProtocolType(getattr(row, "protocol", None) or ProtocolType.HTTP.value)
    except ValueError:
        protocol = ProtocolType.HTTP
    return RouteConfig(
        path=path,
        backend_url=backend,
        protocol=protocol,
        timeout=getattr(row, "timeout", None) or 30,
        rate_limit=getattr(row, "rate_limit", None),
        requires_auth=bool(getattr(row, "requires_auth", True)),
        headers=getattr(row, "headers", None) or {},
        rewrite_path=getattr(row, "rewrite_path", None),
    )


def _snapshot_from_rows(rows) -> Tuple[Tuple[str, bool, int, RouteConfig], ...]:
    """Build the ordered match table. Longest wildcard prefix first."""
    entries: List[Tuple[str, bool, int, RouteConfig]] = []
    for row in rows:
        if not getattr(row, "is_active", True):
            continue
        cfg = _to_config(row)
        if cfg is None:
            continue
        if cfg.path.endswith("*"):
            prefix = cfg.path[:-1]
            entries.append((prefix, True, len(prefix), cfg))
        else:
            entries.append((cfg.path, False, len(cfg.path), cfg))
    # Exact matches first, then wildcards by descending prefix length so the
    # most specific route wins deterministically regardless of row order.
    entries.sort(key=lambda e: (e[1], -e[2]))
    return tuple(entries)


def match(path: str) -> Optional[RouteConfig]:
    """Return the RouteConfig registered for ``path``, or None.

    Hot path: reads an immutable tuple, no I/O, no locking.
    """
    if not _ROUTES or not path:
        return None
    for match_path, is_wildcard, _plen, cfg in _ROUTES:
        if is_wildcard:
            if path.startswith(match_path):
                return cfg
        elif path == match_path:
            return cfg
    return None


def replace(snapshot: Tuple[Tuple[str, bool, int, RouteConfig], ...]) -> None:
    """Atomically swap the live snapshot (module-global rebind is atomic)."""
    global _ROUTES
    _ROUTES = snapshot


def count() -> int:
    """Number of live registered routes (for /health and admin surfaces)."""
    return len(_ROUTES)


async def load(db) -> int:
    """Load active routes from the DB and swap them in. Returns route count."""
    from app.repositories.route_repo import RouteRepository

    rows = await RouteRepository.list_all(db, active_only=True)
    snapshot = _snapshot_from_rows(rows)
    replace(snapshot)
    logger.info("route_registry_loaded", routes=len(snapshot))
    return len(snapshot)


async def bootstrap() -> None:
    """Initial load at startup. Never raises."""
    from app.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            await load(session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("route_registry_bootstrap_failed", error=str(exc))


async def refresh_now() -> None:
    """Reload immediately (called by the admin write path). Never raises."""
    await bootstrap()


async def refresh_loop(interval_seconds: int = 15) -> None:
    """Periodically reload so replicas converge on admin edits."""
    from app.database import AsyncSessionLocal

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            async with AsyncSessionLocal() as session:
                await load(session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("route_registry_refresh_failed", error=str(exc))
