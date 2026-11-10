"""The working set the Rust data plane keeps in memory, and the internal endpoint that serves it.

The data plane (see ``dataplane/DESIGN.md``) authenticates API keys itself, so it needs the keys, as
SHA-256 digests only (never plaintext), their limits, and which of them it may serve without asking
this gateway. It polls ``GET /internal/dataplane/snapshot?since=<revision>`` a couple of times a second.
The revision is a hash of the snapshot's content, so any change, however it was made and by whichever
worker, changes it, and an unchanged snapshot costs a tiny response.

A key is marked ``native_ok: false`` whenever something this gateway enforces that the data plane does
not yet reproduce applies to it: a tenant (quota and usage), an IP rule, a policy, or a stored path
restriction. Dynamic routes, transformation rules and request-altering plugins turn native serving off for every path.
When in doubt the answer is "ask the Python gateway", which is always correct and only slower.

The endpoint exists only when ``DATAPLANE_TOKEN`` is set, requires it in ``X-Dataplane-Token`` and
belongs on an internal port: the data plane refuses ``/internal/`` from clients.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.db_models import APIKeyModel, IPRuleModel, PolicyModel, RouteModel, TenantAPIKeyModel
from app.secure_compare import constant_time_equals

router = APIRouter(prefix="/internal/dataplane", include_in_schema=False)

# One snapshot per process at most this often, however many data planes poll.
CACHE_SECONDS = 0.25
_cache: Optional[tuple] = None
_lock = asyncio.Lock()


def _epoch(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.timestamp())


def _versions() -> Dict[str, Any]:
    from app.versioning import VersionStatus, version_manager

    return {
        "default": version_manager.default_version,
        "versions": [
            {
                "version": v.version,
                "native_ok": v.status == VersionStatus.ACTIVE
                and not v.backend_url
                and v.deprecated_date is None
                and v.sunset_date is None,
            }
            for v in version_manager.versions.values()
        ],
    }


async def build_snapshot(db: AsyncSession) -> Dict[str, Any]:
    settings = get_settings()
    keys = (await db.execute(select(APIKeyModel).where(APIKeyModel.is_active.is_(True)))).scalars().all()
    tenant_key_ids = set((await db.execute(select(TenantAPIKeyModel.api_key_id))).scalars().all())
    rules = (await db.execute(select(IPRuleModel).where(IPRuleModel.enabled.is_(True)))).scalars().all()
    policies = (await db.execute(
        select(func.count()).select_from(PolicyModel).where(PolicyModel.enabled.is_(True))
    )).scalar_one()
    routes = (await db.execute(select(func.count()).select_from(RouteModel))).scalar_one()

    from app.plugins import PluginType, plugin_registry
    from app.transformers import transformation_manager

    # Anything that can change what a request does before it reaches the backend, for any path.
    global_gate = (
        policies > 0
        or any(r.scope == "global" for r in rules)
    )
    key_scoped_rules = {r.scope_id for r in rules if r.scope == "api_key"}
    # Plugins that can hook, transform or take over a request (an auth provider only adds a way in).
    # The stock request-id plugin only stamps X-Request-ID, which the data plane does itself, so it
    # does not count; the snapshot says whether it is on so the data plane behaves the same either way.
    active = [p for p in plugin_registry.list() if p.enabled] if getattr(settings, "plugins_enabled", False) else []
    inject_request_id = any(p.name == "request-id" and p.plugin_type == PluginType.TRANSFORM for p in active)
    request_plugins = any(
        p.plugin_type != PluginType.AUTH_PROVIDER and p.name != "request-id" for p in active
    )
    all_paths_via_python = bool(routes or transformation_manager.list_rules() or request_plugins)

    entries = []
    for k in sorted(keys, key=lambda k: k.key_hash):
        native_ok = not (
            global_gate
            or k.id in tenant_key_ids
            or k.id in key_scoped_rules
            or (k.allowed_paths or [])
        )
        entries.append({
            "hash": k.key_hash,
            "id": k.id,
            "is_active": True,
            "expires_at": _epoch(k.expires_at),
            "rate_limit_per_minute": int(k.rate_limit_per_minute or 0),
            "rate_limit_per_hour": int(k.rate_limit_per_hour or 0),
            "native_ok": native_ok,
        })
    snap: Dict[str, Any] = {
        "keys": entries,
        "fallthrough_prefixes": ["/"] if all_paths_via_python else [],
        "rate_limit_enabled": bool(settings.rate_limit_enabled),
        "inject_request_id": inject_request_id,
        "api_versions": _versions(),
    }
    snap["revision"] = hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()
    return snap


async def current_snapshot(db: AsyncSession) -> Dict[str, Any]:
    global _cache
    async with _lock:
        now = time.monotonic()
        if _cache is not None and now - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        snap = await build_snapshot(db)
        _cache = (now, snap)
        return snap


def reset_cache() -> None:
    global _cache
    _cache = None


def _authorize(token: Optional[str]) -> None:
    expected = get_settings().dataplane_token
    if not expected:
        raise HTTPException(status_code=404, detail="Not Found")
    if not token or not constant_time_equals(token, expected):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/snapshot")
async def snapshot(
    since: Optional[str] = None,
    x_dataplane_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    _authorize(x_dataplane_token)
    snap = await current_snapshot(db)
    if since and since == snap["revision"]:
        return {"unchanged": True, "revision": snap["revision"]}
    return snap
