"""The working set the Rust data plane keeps in memory, and the internal endpoint that serves it.

The data plane (see ``dataplane/DESIGN.md``) authenticates API keys itself, so it needs the keys, as
SHA-256 digests only (never plaintext), their limits, and which of them it may serve without asking
this gateway. It polls ``GET /internal/dataplane/snapshot?since=<revision>`` a couple of times a second.
The revision is a hash of the snapshot's content, so any change, however it was made and by whichever
worker, changes it, and an unchanged snapshot costs a tiny response.

The snapshot also carries what this gateway enforces on a key's requests, compiled into a form the data
plane evaluates itself: the key's path restriction, IP rules, policies, its tenant's quota, and the
trusted proxies behind which the client address is read. Anything the data plane cannot evaluate exactly
as this gateway does (a policy that matches on headers, a quota that is not a whole number, ...) marks
the keys it applies to ``native_ok: false``. Dynamic routes, transformation rules and request-altering
plugins turn native serving off for every path. When in doubt the answer is "ask the Python gateway",
which is always correct and only slower.

Tenant usage is shared through the database: the data plane reports the requests it served per tenant,
they are added to the same counters this gateway's own requests feed, and every snapshot answer carries
the current totals, so a tenant's quota holds across both.

The endpoint exists only when ``DATAPLANE_TOKEN`` is set, requires it in ``X-Dataplane-Token`` and
belongs on an internal port: the data plane refuses ``/internal/`` from clients.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import time
from datetime import datetime, time as dtime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.db_models import (
    APIKeyModel,
    IPRuleModel,
    PolicyModel,
    RouteModel,
    TenantAPIKeyModel,
    TenantModel,
    TenantUsageModel,
)
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


class _NotReproducible(Exception):
    """Something the data plane cannot evaluate exactly as this gateway does."""


def _dumps(value: Any) -> str:
    """JSON exactly as this gateway's error responses render it, for the data plane to embed as is."""
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _net(value: Any) -> Optional[str]:
    """A CIDR as ``ipaddress`` reads it, in canonical text; None when it does not parse (and so never matches)."""
    if not isinstance(value, str):
        raise _NotReproducible("a CIDR that is not text")
    try:
        text = str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        return None
    if "%" in text:
        raise _NotReproducible("a scoped IPv6 network")
    return text


def _trusted_proxies(settings) -> List[str]:
    out = []
    for entry in getattr(settings, "trusted_proxies", None) or ():
        if not isinstance(entry, str):
            raise _NotReproducible("a trusted proxy that is not text")
        net = _net(entry.strip()) if entry.strip() else None
        if net:
            out.append(net)
    return out


def _ip_rules(rules) -> List[Dict[str, Any]]:
    """Enabled IP rules in the order ``IPRuleEngine`` indexes them, without those it ignores."""
    out = []
    for r in rules:
        if r.scope not in ("global", "tenant", "api_key") or r.action not in ("allow", "deny"):
            continue
        if r.scope != "global" and r.scope_id is None:
            continue
        out.append({
            "scope": r.scope,
            "scope_id": r.scope_id if r.scope != "global" else None,
            "allow": r.action == "allow",
            "net": _net(r.cidr),
            # What a refusal by this rule says, after the client address.
            "details_json": _dumps({"rule_id": r.id, "scope": r.scope}),
        })
    return out


def _minutes(text: Any) -> int:
    h, m = text.split(":")
    t = dtime(int(h), int(m))
    return t.hour * 60 + t.minute


def _policy(p) -> Optional[Dict[str, Any]]:
    """One policy as ``PolicyConditions.matches`` and ``PolicyEngine.evaluate`` read it; None when it can never match."""
    cond = p.conditions or {}
    action = p.action or {"type": "allow"}
    if not isinstance(cond, dict) or not isinstance(action, dict):
        raise _NotReproducible("a policy that is not an object")
    if cond.get("headers"):
        raise _NotReproducible("a policy that matches on headers")
    path = cond.get("path") or None
    if path is not None and (not isinstance(path, str) or "[" in path):
        raise _NotReproducible("a path pattern with a character class")
    lists = {}
    for name in ("methods", "tenants", "roles", "cidrs"):
        value = cond.get(name, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise _NotReproducible(f"policy {name} that is not a list of text")
        lists[name] = value
    # API-key requests carry no role, so a policy that requires one never matches them.
    if lists["roles"] and "" not in lists["roles"]:
        return None
    window = None
    tod = cond.get("time_of_day")
    if tod:
        if not isinstance(tod, dict):
            raise _NotReproducible("a time window that is not an object")
        start, end = tod.get("start"), tod.get("end")
        if start and end:
            try:
                window = [_minutes(start), _minutes(end)]
            except (ValueError, AttributeError):
                window = None  # unreadable times do not restrict, as in _time_in_window
    kind = action.get("type") or "allow"
    if not isinstance(kind, str):
        raise _NotReproducible("a policy action that is not text")
    reason = action.get("reason") or f"denied by policy '{p.name}'"
    if not isinstance(reason, str):
        raise _NotReproducible("a policy reason that is not text")
    return {
        "path": path,
        "methods": [m.upper() for m in lists["methods"]],
        "tenants": lists["tenants"],
        # null: any address. A list: the address must be in one of them (unreadable CIDRs never match).
        "cidrs": [n for n in map(_net, lists["cidrs"]) if n] if lists["cidrs"] else None,
        "time": window,
        "deny": kind.lower() == "deny",
        "message_json": _dumps(reason),
        "details_json": _dumps({"policy_id": p.id}),
    }


def _scope(patterns: Any) -> Optional[Dict[str, Any]]:
    """A key's ``allowed_paths`` as ``path_is_allowed`` reads them: ``["all"]``, ``["prefix", text]`` (the
    normalised path starts with it) or ``["under", base]`` (the path is ``base`` or below it)."""
    from app.middleware.auth import _normalise_scope_path

    if not patterns:
        return None
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise _NotReproducible("allowed_paths that are not a list of text")
    rules: List[list] = []
    for raw in patterns:
        pattern = raw.strip()
        if not pattern:
            continue
        if pattern in ("*", "/", "/*"):
            rules = [["all"]]
            break
        if pattern.endswith("/*"):
            pattern = pattern[:-2]
        elif pattern.endswith("*"):
            prefix = _normalise_scope_path(pattern[:-1]) or ""
            if pattern[:-1].endswith("/") and prefix != "/":
                prefix += "/"
            rules.append(["prefix", prefix])
            continue
        base = _normalise_scope_path(pattern)
        if base is not None:
            rules.append(["under", base])
    return {"rules": rules, "patterns_json": _dumps(patterns)}


def _tenant(t) -> Dict[str, Any]:
    quota = t.quota or {}
    if not isinstance(quota, dict):
        raise _NotReproducible("a quota that is not an object")
    limits = {}
    for window in ("minute", "hour"):
        value = quota.get(f"requests_per_{window}")
        if value is None:
            limits[window] = None
        elif isinstance(value, bool) or not isinstance(value, int):
            raise _NotReproducible("a quota that is not a whole number")
        else:
            limits[window] = value if value > 0 else None
    try:
        details = _dumps({"tenant_id": t.tenant_id, "quota": quota})
    except (TypeError, ValueError):
        raise _NotReproducible("a quota that does not render as JSON")
    # What a refusal over this tenant's quota says, after the limit that was hit.
    return {"minute": limits["minute"], "hour": limits["hour"], "details_json": details}


async def build_snapshot(db: AsyncSession) -> Dict[str, Any]:
    settings = get_settings()
    keys = (await db.execute(select(APIKeyModel).where(APIKeyModel.is_active.is_(True)))).scalars().all()
    links = (await db.execute(
        select(TenantAPIKeyModel.tenant_id, TenantAPIKeyModel.api_key_id, TenantAPIKeyModel.api_key_hash)
    )).all()
    tenant_rows = {t.tenant_id: t for t in (await db.execute(select(TenantModel))).scalars().all()} if links else {}
    # The orders the engines evaluate in (IPRuleEngine.reload, PolicyEngine.reload).
    rules = (await db.execute(
        select(IPRuleModel).where(IPRuleModel.enabled.is_(True)).order_by(IPRuleModel.created_at.asc())
    )).scalars().all()
    policy_rows = (await db.execute(
        select(PolicyModel).where(PolicyModel.enabled.is_(True))
        .order_by(PolicyModel.priority.asc(), PolicyModel.created_at.asc())
    )).scalars().all()
    routes = (await db.execute(select(func.count()).select_from(RouteModel))).scalar_one()

    from app.plugins import PluginType, plugin_registry
    from app.transformers import transformation_manager

    # Rules and policies apply to every key. If any of them cannot be reproduced, every key's requests
    # go to this gateway, as they all did before the data plane evaluated these itself.
    try:
        trusted = _trusted_proxies(settings)
        ip_rules = _ip_rules(rules)
        policies = [c for c in map(_policy, policy_rows) if c is not None]
        global_gate = False
    except _NotReproducible:
        trusted, ip_rules, policies, global_gate = [], [], [], True
    except Exception:  # noqa: BLE001 - a rule this code did not expect: let Python decide
        trusted, ip_rules, policies, global_gate = [], [], [], True

    # Plugins that can hook, transform or take over a request (an auth provider only adds a way in).
    # The stock request-id plugin only stamps X-Request-ID, which the data plane does itself, so it
    # does not count; the snapshot says whether it is on so the data plane behaves the same either way.
    active = [p for p in plugin_registry.list() if p.enabled] if getattr(settings, "plugins_enabled", False) else []
    inject_request_id = any(p.name == "request-id" and p.plugin_type == PluginType.TRANSFORM for p in active)
    request_plugins = any(
        p.plugin_type != PluginType.AUTH_PROVIDER and p.name != "request-id" for p in active
    )
    all_paths_via_python = bool(routes or transformation_manager.list_rules() or request_plugins)

    # This gateway finds a key's tenant by the key's digest (TenantRepository.get_tenant_for_api_key).
    by_hash: Dict[str, list] = {}
    for tenant_id, _key_id, key_hash in links:
        by_hash.setdefault(key_hash, []).append(tenant_id)
    linked_ids = {key_id for _tenant_id, key_id, _hash in links}

    tenants: Dict[str, Any] = {}
    unusable_tenants = set()
    entries = []
    for k in sorted(keys, key=lambda k: k.key_hash):
        native_ok = not global_gate
        tenant_id = None
        scope = None
        owners = by_hash.get(k.key_hash, [])
        if len(owners) > 1 or (not owners and k.id in linked_ids):
            native_ok = False  # an association this gateway itself resolves oddly: let it
        elif owners:
            row = tenant_rows.get(owners[0])
            # An inactive tenant's keys work as keys without a tenant.
            if row is not None and row.is_active:
                tenant_id = row.tenant_id
                if tenant_id not in tenants and tenant_id not in unusable_tenants:
                    try:
                        tenants[tenant_id] = _tenant(row)
                    except _NotReproducible:
                        unusable_tenants.add(tenant_id)
                if tenant_id in unusable_tenants:
                    native_ok, tenant_id = False, None
        try:
            scope = _scope(k.allowed_paths or [])
        except _NotReproducible:
            native_ok = False
        entry = {
            "hash": k.key_hash,
            "id": k.id,
            "is_active": True,
            "expires_at": _epoch(k.expires_at),
            "rate_limit_per_minute": int(k.rate_limit_per_minute or 0),
            "rate_limit_per_hour": int(k.rate_limit_per_hour or 0),
            "native_ok": native_ok,
        }
        if native_ok and tenant_id is not None:
            entry["tenant"] = tenant_id
        if native_ok and scope is not None:
            entry["allowed_paths"] = scope
        entries.append(entry)
    snap: Dict[str, Any] = {
        "keys": entries,
        "tenants": tenants,
        "trusted_proxies": trusted,
        "ip_rules": ip_rules,
        "policies": policies,
        "fallthrough_prefixes": ["/"] if all_paths_via_python else [],
        "rate_limit_enabled": bool(settings.rate_limit_enabled),
        "inject_request_id": inject_request_id,
        "circuit_breaker": {
            "enabled": bool(settings.circuit_breaker_enabled),
            "failure_threshold": int(settings.circuit_breaker_failure_threshold),
            "timeout_seconds": int(settings.circuit_breaker_timeout),
        },
        "upstream_timeout_seconds": float(settings.default_timeout),
        "api_versions": _versions(),
    }
    snap["revision"] = hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()
    return snap


async def tenant_usage(db: AsyncSession, tenant_ids) -> Dict[str, Any]:
    """Requests each tenant has used in the current minute and hour, from the counters every request
    feeds, whichever plane served it. Sent with every snapshot answer; never part of the revision."""
    from app.repositories.tenant_repo import TenantRepository

    minute = TenantRepository._window_bounds("minute")
    hour = TenantRepository._window_bounds("hour")
    used: Dict[str, list] = {}
    if tenant_ids:
        rows = await db.execute(
            select(TenantUsageModel.tenant_id, TenantUsageModel.window, func.sum(TenantUsageModel.request_count))
            .where(or_(
                and_(TenantUsageModel.window == "minute", TenantUsageModel.window_start == minute),
                and_(TenantUsageModel.window == "hour", TenantUsageModel.window_start == hour),
            ))
            .group_by(TenantUsageModel.tenant_id, TenantUsageModel.window)
        )
        for tenant_id, window, total in rows.all():
            if tenant_id in tenant_ids:
                used.setdefault(tenant_id, [0, 0])[0 if window == "minute" else 1] = int(total or 0)
    return {"minute_start": _epoch(minute), "hour_start": _epoch(hour), "used": used}


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
    tenant: Optional[str] = None
    # For a 5xx the data plane produced itself: the error code of its body, and for a failed call
    # to the backend the kind of failure, named as this gateway's HTTP client names it.
    error_code: Optional[str] = None
    error_type: Optional[str] = None
    buckets: list = Field(default_factory=list)  # [[count, seconds]] per bucket, then +Inf


class ReportedRow(BaseModel):
    request_id: str
    method: str
    path: str
    client_ip: str = "0.0.0.0"
    api_key_id: Optional[str] = None
    tenant_id: Optional[str] = None
    status: int
    duration_ms: float
    user_agent: Optional[str] = None
    error: Optional[str] = None


class ReportedKeyUse(BaseModel):
    id: str
    at: int  # epoch seconds of the key's latest use


class ReportedEvent(BaseModel):
    """Something this gateway announces by webhook and alert when it happens here: the circuit breaker
    opening or closing (``circuit_breaker``) or a key going over its limit (``rate_limit_exceeded``)."""

    type: str
    # circuit_breaker
    state: Optional[str] = None
    previous_state: Optional[str] = None
    backend_url: Optional[str] = None
    failure_count: int = 0
    failure_threshold: int = 0
    opened_at: float = 0.0
    timeout_seconds: int = 0
    # rate_limit_exceeded
    api_key_id: Optional[str] = None
    client_ip: Optional[str] = None
    limit: int = 0
    window: Optional[str] = None
    path: Optional[str] = None
    count: int = 1  # how many refused requests this one event stands for


class ReportedTenantUsage(BaseModel):
    """Requests served for one tenant, admitted in one minute (epoch seconds of its start)."""

    tenant_id: str
    minute: int
    requests: int = 0
    errors: int = 0
    bytes_out: int = 0


class DataplaneReport(BaseModel):
    tenant_usage: list[ReportedTenantUsage] = Field(default_factory=list, max_length=5000)
    events: list[ReportedEvent] = Field(default_factory=list, max_length=2000)
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
                    duration_seconds=mean, protocol="http", tenant=agg.tenant,
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
                # The error counter, as _log_handled_error and the proxy's own handler feed it.
                if agg.status >= 500 and agg.error_code:
                    gw.metrics.error_count.labels(error_type=agg.error_code, protocol="gateway").inc(n)
                if agg.status >= 500 and agg.error_type:
                    gw.metrics.error_count.labels(error_type=agg.error_type, protocol="unknown").inc(n)

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
                "api_key_id": row.api_key_id, "tenant_id": row.tenant_id, "protocol": "http",
            }
            if gw.log_export_manager.is_running():
                gw.log_export_manager.enqueue(entry)
    if skipped and gw.settings.metrics_enabled:
        gw.metrics.dataplane_rows_skipped.inc(skipped)
    return {"requests": counted, "rows": len(report.rows), "rows_skipped": skipped}


# Webhook and alert deliveries in flight; held here so they are not collected before they finish.
_background: set = set()


def _in_background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def apply_events(db: AsyncSession, events: list) -> int:
    """Announce what happened in the data plane exactly as this gateway announces its own: the same
    webhook event types and payloads, the same alerts (which keep their own thresholds and intervals).
    Deliveries run in the background so a slow receiver never delays the report.

    A rate-limit event stands for every request one key had refused in one window since the last
    report (``count``), where this gateway fires one webhook per refused request."""
    from app.notifications import notification_manager
    from app.webhooks import WebhookEventType, webhook_manager

    limited = [e for e in events if e.type == "rate_limit_exceeded"]
    names: Dict[str, Optional[str]] = {}
    ids = {e.api_key_id for e in limited if e.api_key_id}
    if ids:
        rows = await db.execute(select(APIKeyModel.id, APIKeyModel.name).where(APIKeyModel.id.in_(ids)))
        names = {row[0]: row[1] for row in rows.all()}

    announced = 0
    for e in events:
        if e.type == "circuit_breaker" and e.state == "open":
            message = f"Circuit breaker opened after {e.failure_count} failures"
            _in_background(webhook_manager.trigger_webhook(WebhookEventType.CIRCUIT_BREAKER_OPEN, {
                "backend_url": e.backend_url, "state": "open", "previous_state": e.previous_state,
                "failure_count": e.failure_count, "failure_threshold": e.failure_threshold,
                "opened_at": e.opened_at, "timeout_seconds": e.timeout_seconds, "message": message,
            }))
        elif e.type == "circuit_breaker" and e.state == "closed":
            message = "Circuit breaker closed after successful request"
            _in_background(webhook_manager.trigger_webhook(WebhookEventType.CIRCUIT_BREAKER_CLOSED, {
                "backend_url": e.backend_url, "state": "closed", "previous_state": e.previous_state,
                "failure_count": e.failure_count, "message": message,
            }))
        elif e.type == "rate_limit_exceeded":
            _in_background(webhook_manager.trigger_webhook(WebhookEventType.RATE_LIMIT_EXCEEDED, {
                "api_key_id": e.api_key_id, "api_key_name": names.get(e.api_key_id or ""),
                "client_ip": e.client_ip, "limit": e.limit, "window": e.window, "path": e.path,
                "count": max(1, e.count),
            }))
            _in_background(notification_manager.send_rate_limit_alert(
                api_key_id=e.api_key_id, client_ip=e.client_ip or "", limit=e.limit,
                window=e.window or "", path=e.path or "",
            ))
            announced += 1
            continue
        else:
            continue  # a kind this version does not know: ignore rather than refuse the whole report
        _in_background(notification_manager.send_circuit_breaker_alert(
            backend_url=e.backend_url or "", state=e.state, previous_state=e.previous_state or "",
            failure_count=e.failure_count, message=message,
        ))
        announced += 1
    return announced


async def apply_tenant_usage(db: AsyncSession, usage: list) -> int:
    """Add what the data plane served for each tenant to the counters this gateway's own requests feed,
    in the windows the requests were admitted in. Committed before the report is answered, so the next
    snapshot answer already includes it."""
    from app.repositories.tenant_repo import TenantRepository

    applied = 0
    for u in usage:
        if u.requests <= 0:
            continue
        when = datetime.fromtimestamp(u.minute, timezone.utc).replace(tzinfo=None)
        try:
            await TenantRepository.increment_usage(
                db, u.tenant_id, request_count=u.requests, error_count=max(0, u.errors),
                bytes_out=max(0, u.bytes_out), now=when,
            )
            applied += u.requests
        except Exception:  # noqa: BLE001 - a tenant deleted meanwhile must not refuse the whole report
            await db.rollback()
    return applied


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
    """What the Rust data plane served and saw: counts, log rows, key use and events."""
    _authorize(x_dataplane_token)
    out = apply_report(body)
    out["tenant_requests"] = await apply_tenant_usage(db, body.tenant_usage)
    out["keys_updated"] = await apply_key_usage(db, body.keys_used)
    out["events"] = await apply_events(db, body.events)
    return out


@router.get("/snapshot")
async def snapshot(
    since: Optional[str] = None,
    x_dataplane_token: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """The working set the Rust data plane serves from: key digests, limits and settings."""
    _authorize(x_dataplane_token)
    snap = await current_snapshot(db)
    limited = {t for t, entry in snap["tenants"].items() if entry["minute"] or entry["hour"]}
    usage = await tenant_usage(db, limited)
    if since and since == snap["revision"]:
        return {"unchanged": True, "revision": snap["revision"], "tenant_usage": usage}
    return {**snap, "tenant_usage": usage}
