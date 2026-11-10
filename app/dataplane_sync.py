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
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, update
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


# --- what the data plane served ----------------------------------------------------------------

# Upper bounds of gateway_request_duration_seconds; the data plane buckets with the same ones.
_BOUNDS = (0.01, 0.05, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0)


class ReportedAggregate(BaseModel):
    method: str
    path: str  # one real path from the group; the label is derived here
    status: int
    rejected: bool = False
    client_id: Optional[str] = None
    buckets: list = Field(default_factory=list)  # [[count, seconds]] per bucket, then +Inf


class ReportedRow(BaseModel):
    request_id: str
    method: str
    path: str
    client_ip: str = "0.0.0.0"
    api_key_id: Optional[str] = None
    status: int
    duration_ms: float
    user_agent: Optional[str] = None
    error: Optional[str] = None


class ReportedKeyUse(BaseModel):
    id: str
    at: int  # epoch seconds of the key's latest use


class DataplaneReport(BaseModel):
    keys_used: list[ReportedKeyUse] = Field(default_factory=list, max_length=5000)
    aggregates: list[ReportedAggregate] = Field(default_factory=list, max_length=5000)
    rows: list[ReportedRow] = Field(default_factory=list, max_length=5000)
    rows_skipped: int = 0


def apply_report(report: DataplaneReport) -> Dict[str, int]:
    """Feed what the data plane served into the same metrics, SLOs and request log a request through
    this gateway would have fed. Counts are exact; each latency bucket is applied as that many
    observations at the bucket's mean, which keeps both the bucket counts and the sum exact."""
    from app import main as gw
    from app.models import RequestLog
    from app.observability import slo_tracker

    counted = 0
    for agg in report.aggregates:
        metric_path = gw._rejected_path_label(agg.path) if agg.rejected else gw.obs_metric_path_label(agg.path)
        for i, pair in enumerate(agg.buckets[: len(_BOUNDS) + 1]):
            n, total = int(pair[0]), float(pair[1])
            if n <= 0:
                continue
            mean = total / n
            counted += n
            for _ in range(n):
                gw.obs_record_request(
                    method=agg.method, path=metric_path, status_code=agg.status,
                    duration_seconds=mean, protocol="http", tenant=None,
                )
            if gw.settings.metrics_enabled:
                gw.metrics.request_count.labels(
                    method=agg.method, path=metric_path, status_code=agg.status, protocol="http"
                ).inc(n)
                hist = gw.metrics.request_duration.labels(method=agg.method, path=metric_path, protocol="http")
                for _ in range(n):
                    hist.observe(mean)
                gw.metrics.dataplane_native_requests.labels(
                    status_class=f"{agg.status // 100}xx"
                ).inc(n)
                if agg.rejected and agg.status == 429:
                    gw.metrics.rate_limit_hits.labels(client_id=agg.client_id or "anonymous").inc(n)

    skipped = max(0, int(report.rows_skipped))
    for row in report.rows:
        log = RequestLog(
            request_id=row.request_id, method=row.method, path=row.path, client_ip=row.client_ip or "0.0.0.0",
            api_key=None, status_code=row.status, response_time_ms=row.duration_ms,
            user_agent=row.user_agent, error=row.error, protocol="http",
        )
        gw.request_log_writer.submit(log, row.api_key_id)
        if gw.log_export_manager.exporters:
            entry = {
                "request_id": row.request_id, "method": row.method, "path": row.path,
                "status_code": row.status, "duration_ms": row.duration_ms, "client_ip": row.client_ip,
                "timestamp": gw.utcnow().isoformat(), "user_agent": row.user_agent,
                "api_key_id": row.api_key_id, "tenant_id": None, "protocol": "http",
            }
            if gw.log_export_manager.is_running():
                gw.log_export_manager.enqueue(entry)
    if skipped and gw.settings.metrics_enabled:
        gw.metrics.dataplane_rows_skipped.inc(skipped)
    return {"requests": counted, "rows": len(report.rows), "rows_skipped": skipped}


async def apply_key_usage(db: AsyncSession, uses: list) -> int:
    """Move each key's ``last_used_at`` forward to the data plane's latest use (never backwards)."""
    n = 0
    for use in uses:
        when = datetime.fromtimestamp(use.at, timezone.utc).replace(tzinfo=None)
        result = await db.execute(
            update(APIKeyModel)
            .where(APIKeyModel.id == use.id)
            .where(or_(APIKeyModel.last_used_at.is_(None), APIKeyModel.last_used_at < when))
            .values(last_used_at=when)
        )
        n += result.rowcount or 0
    if n:
        await db.commit()
    return n


@router.post("/report")
async def report(
    body: DataplaneReport,
    x_dataplane_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, int]:
    _authorize(x_dataplane_token)
    out = apply_report(body)
    out["keys_updated"] = await apply_key_usage(db, body.keys_used)
    return out


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
