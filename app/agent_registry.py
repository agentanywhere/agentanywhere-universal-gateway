"""Dynamic agent-edge registry provider.

Bridges the DB-backed :class:`app.db_models.AgentRegistryModel` rows to the live,
in-memory :class:`app.agent_routes.AgentEdgeConfig` used by the proxy/WS relay.

Responsibilities:

* **Seed** — on first boot, if the table is empty, import the legacy
  ``AGENT_ROUTES_JSON`` / ``AGENT_PUBLIC_PATHS_JSON`` env vars so migrating off
  the static setting is seamless and lossless.
* **Load** — build the slug→upstream / slug→public-paths / slug→audience tables
  from ``enabled`` rows and atomically swap them into the running config.
* **Refresh** — a lightweight periodic reload so multiple workers/replicas
  converge on admin edits within a few seconds (belt-and-suspenders on top of
  the immediate in-process swap performed by the write path).

All functions are defensive: a registry failure must never take the gateway
down, so callers wrap them and fall back to the env-seeded config.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()

# kebab-case, 2–63 chars, no leading/trailing/double dashes.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VALID_STATUS = {"published", "hidden"}
_VALID_RUNTIME = {"dedicated", "shared"}


def validate_slug(slug: str) -> Optional[str]:
    """Return an error string if ``slug`` is invalid, else ``None``."""
    if not slug or not (2 <= len(slug) <= 63):
        return "slug must be 2–63 characters"
    if not SLUG_RE.match(slug):
        return "slug must be kebab-case (lowercase letters, digits, single dashes)"
    return None


def validate_upstream(url: str) -> Optional[str]:
    if not url:
        return "upstream_base is required"
    if not (url.startswith("http://") or url.startswith("https://")):
        return "upstream_base must start with http:// or https://"
    if " " in url.strip():
        return "upstream_base must not contain spaces"
    return None


def _tables_from_rows(rows) -> Tuple[Dict[str, str], Dict[str, List[str]], Dict[str, str]]:
    """Build (routes, public_paths, audience_by_slug) from registry rows.

    Only ``enabled`` rows are routable; ``status`` (published/hidden) is a
    catalog concern, not a routing gate.
    """
    routes: Dict[str, str] = {}
    public: Dict[str, List[str]] = {}
    audience: Dict[str, str] = {}
    for row in rows:
        if not getattr(row, "enabled", True):
            continue
        base = (row.upstream_base or "").strip()
        if not base:
            continue
        routes[row.slug] = base
        if row.public_paths:
            public[row.slug] = list(row.public_paths)
        if row.audience:
            audience[row.slug] = row.audience
    return routes, public, audience


async def load_into(config, db) -> int:
    """Load the DB registry into ``config`` (hot-swap). Returns route count."""
    from app.repositories.agent_registry_repo import AgentRegistryRepository

    rows = await AgentRegistryRepository.list_all(db)
    routes, public, audience = _tables_from_rows(rows)
    config.replace(routes=routes, public_paths=public, audience_by_slug=audience)
    logger.info("agent_registry_loaded", agents=len(routes))
    return len(routes)


def _seed_items_from_settings(settings) -> List[dict]:
    """Parse the legacy env vars into registry rows for first-boot seeding."""
    raw_routes = getattr(settings, "agent_routes_json", None)
    raw_public = getattr(settings, "agent_public_paths_json", None)
    try:
        routes = json.loads(raw_routes) if raw_routes else {}
    except (ValueError, TypeError):
        routes = {}
    try:
        public = json.loads(raw_public) if raw_public else {}
    except (ValueError, TypeError):
        public = {}
    if not isinstance(routes, dict):
        return []

    items: List[dict] = []
    for slug, upstream in routes.items():
        if not upstream:
            continue
        paths = public.get(slug) if isinstance(public, dict) else None
        items.append(
            {
                "slug": str(slug),
                "upstream_base": str(upstream),
                "public_paths": list(paths) if isinstance(paths, (list, tuple)) else [],
                "status": "published",
                "enabled": True,
                "runtime_mode": "dedicated",
                "data_residency": "in",
            }
        )
    return items


async def seed_from_env_if_empty(db, settings) -> int:
    """If the registry table is empty, import the env config. Returns rows seeded."""
    from app.repositories.agent_registry_repo import AgentRegistryRepository

    if await AgentRegistryRepository.count(db) > 0:
        return 0
    items = _seed_items_from_settings(settings)
    if not items:
        return 0
    written = await AgentRegistryRepository.upsert_many(db, items, actor="env-seed")
    logger.info("agent_registry_seeded_from_env", agents=written)
    return written


async def bootstrap(config, settings) -> None:
    """One-shot startup: seed (if empty) then load into ``config``. Best-effort."""
    from app.database import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as session:
            await seed_from_env_if_empty(session, settings)
            await load_into(config, session)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent_registry_bootstrap_failed", error=str(exc))


async def refresh_loop(config, interval_seconds: int = 15) -> None:
    """Periodically reload the registry so replicas converge on edits."""
    from app.database import AsyncSessionLocal

    while True:
        try:
            await asyncio.sleep(interval_seconds)
            async with AsyncSessionLocal() as session:
                await load_into(config, session)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug("agent_registry_refresh_failed", error=str(exc))
