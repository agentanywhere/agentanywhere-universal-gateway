"""Admin API for the AgentAnywhere agent-edge registry.

The control-plane surface that replaces hand-editing
the ``AGENT_ROUTES_JSON`` setting. Every write persists to the
DB registry, hot-reloads the running proxy (no redeploy), and is recorded in the
tamper-evident governance audit log.

All endpoints require an admin token (OIDC SSO in production, via the
shared :func:`admin.api.verify_admin_token`).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, List, Optional

import httpx
import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from admin.api import verify_admin_token, verify_admin_token_value
from app import agent_registry
from app.agent_monitor import agent_monitor
from app.database import get_db
from app.repositories.agent_registry_repo import AgentRegistryRepository

logger = structlog.get_logger()

router = APIRouter(prefix="/admin/agents", tags=["agents"])


# --------------------------------------------------------------------------- models
class AgentCreate(BaseModel):
    slug: str = Field(..., description="kebab-case path slug, e.g. orders-agent")
    upstream_base: str = Field(..., description="Upstream origin, e.g. http://10.0.0.10:8090")
    audience: Optional[str] = Field(None, description="Token audience / entitlement; defaults to slug")
    public_paths: List[str] = Field(default_factory=list, description="Path prefixes that bypass SSO")
    status: str = Field("published", description="published | hidden (catalog visibility)")
    display_name: Optional[str] = None
    description: Optional[str] = None
    runtime_mode: str = Field("dedicated", description="dedicated | shared")
    data_residency: str = Field("in")
    enabled: bool = True


class AgentUpdate(BaseModel):
    upstream_base: Optional[str] = None
    audience: Optional[str] = None
    public_paths: Optional[List[str]] = None
    status: Optional[str] = None
    display_name: Optional[str] = None
    description: Optional[str] = None
    runtime_mode: Optional[str] = None
    data_residency: Optional[str] = None
    enabled: Optional[bool] = None


# --------------------------------------------------------------------------- helpers
def _live_config():
    """Return the process-global AgentEdgeConfig (lazy import avoids a cycle)."""
    import app.main as main_module

    return main_module.agent_edge_config


async def _hot_reload(db: AsyncSession) -> int:
    """Rebuild the live routing tables from the DB immediately after a write."""
    return await agent_registry.load_into(_live_config(), db)


def _actor(token: Optional[str]) -> Optional[str]:
    """A short, non-secret fingerprint of the admin token for audit rows."""
    if not token:
        return None
    return token[-8:]


async def _audit(
    db: AsyncSession,
    request: Request,
    action: str,
    slug: str,
    token: Optional[str],
    before: Optional[Dict[str, Any]] = None,
    after: Optional[Dict[str, Any]] = None,
    outcome: str = "success",
) -> None:
    """Best-effort hash-chained audit entry; never fails the request."""
    try:
        from app.governance import AuditEntry, audit_log

        await audit_log.record(
            db,
            AuditEntry(
                action=action,
                actor=_actor(token),
                resource_type="agent",
                resource_id=slug,
                request_id=getattr(request.state, "request_id", None),
                client_ip=getattr(request.client, "host", None) if request.client else None,
                user_agent=request.headers.get("user-agent"),
                outcome=outcome,
                before=before,
                after=after,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("agent_audit_failed", action=action, slug=slug, error=str(exc))


def _validate(slug: str, upstream: str, status_val: str, runtime: str) -> None:
    err = agent_registry.validate_slug(slug)
    if err:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)
    err = agent_registry.validate_upstream(upstream)
    if err:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=err)
    if status_val not in agent_registry._VALID_STATUS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"status must be one of {sorted(agent_registry._VALID_STATUS)}",
        )
    if runtime not in agent_registry._VALID_RUNTIME:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"runtime_mode must be one of {sorted(agent_registry._VALID_RUNTIME)}",
        )


def _decorate(row_dict: Dict[str, Any], live_routes: Dict[str, str]) -> Dict[str, Any]:
    """Annotate a registry row with whether it is currently live in the proxy."""
    row_dict = dict(row_dict)
    row_dict["live"] = row_dict["slug"] in live_routes
    row_dict["public_url"] = f"/agents/{row_dict['slug']}/"
    return row_dict


# --------------------------------------------------------------------------- fixed routes
@router.get("")
async def list_agents(
    _: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
) -> List[Dict[str, Any]]:
    """List every registered agent, annotated with live-routing status."""
    rows = await AgentRegistryRepository.list_all(db)
    live = _live_config().snapshot().get("routes", {})
    return [_decorate(AgentRegistryRepository.to_dict(r), live) for r in rows]


@router.get("/export")
async def export_agents(
    _: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Export the registry as ``AGENT_ROUTES_JSON`` / ``AGENT_PUBLIC_PATHS_JSON``.

    Handy as a git-committable backup and for pinning the static setting to a
    known-good snapshot during migration.
    """
    rows = await AgentRegistryRepository.list_all(db)
    routes: Dict[str, str] = {}
    public: Dict[str, List[str]] = {}
    for r in rows:
        if not r.enabled:
            continue
        routes[r.slug] = r.upstream_base
        if r.public_paths:
            public[r.slug] = list(r.public_paths)
    return {
        "AGENT_ROUTES_JSON": routes,
        "AGENT_PUBLIC_PATHS_JSON": public,
        "count": len(routes),
    }


@router.post("/import")
async def import_agents(
    request: Request,
    payload: Dict[str, Any] = Body(...),
    token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Bulk upsert from an ``AGENT_ROUTES_JSON``-shaped body.

    Body: ``{"routes": {slug: upstream}, "public_paths": {slug: [..]}}`` (also
    accepts the ``AGENT_ROUTES_JSON`` / ``AGENT_PUBLIC_PATHS_JSON`` keys).
    """
    routes = payload.get("routes") or payload.get("AGENT_ROUTES_JSON") or {}
    public = payload.get("public_paths") or payload.get("AGENT_PUBLIC_PATHS_JSON") or {}
    if not isinstance(routes, dict) or not routes:
        raise HTTPException(status_code=422, detail="routes map is required")

    items: List[Dict[str, Any]] = []
    for slug, upstream in routes.items():
        if agent_registry.validate_slug(str(slug)) or agent_registry.validate_upstream(str(upstream)):
            continue
        paths = public.get(slug) if isinstance(public, dict) else None
        items.append(
            {
                "slug": str(slug),
                "upstream_base": str(upstream),
                "public_paths": list(paths) if isinstance(paths, (list, tuple)) else [],
            }
        )
    written = await AgentRegistryRepository.upsert_many(db, items, actor=_actor(token))
    count = await _hot_reload(db)
    await _audit(db, request, "agent.import", f"{written} agents", token, after={"written": written})
    return {"imported": written, "live_routes": count}


@router.post("/reload")
async def reload_agents(
    _: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Force a reload of the live routing tables from the DB."""
    count = await _hot_reload(db)
    return {"reloaded": True, "live_routes": count}


@router.get("/health")
async def agents_health(
    _: str = Depends(verify_admin_token),
) -> Dict[str, Any]:
    """Live health + traffic snapshot for the Mission Control board."""
    return agent_monitor.snapshot(_live_config())


@router.get("/stream")
async def agents_health_stream(
    request: Request,
    token: Optional[str] = Query(None),
) -> StreamingResponse:
    """Server-Sent Events stream of the live board.

    Browsers' ``EventSource`` cannot set an ``Authorization`` header, so the
    admin token is accepted as a ``?token=`` query parameter (falling back to
    the header for programmatic clients).
    """
    bearer = None
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header[7:]
    verify_admin_token_value(token or bearer)

    async def _events():
        # Prime with an immediate snapshot, then push on an interval until the
        # client disconnects.
        try:
            while True:
                if await request.is_disconnected():
                    break
                payload = json.dumps(agent_monitor.snapshot(_live_config()), default=str)
                yield f"data: {payload}\n\n"
                await asyncio.sleep(2.0)
        except asyncio.CancelledError:  # client went away
            raise

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable proxy buffering (nginx)
        },
    )


# --------------------------------------------------------------------------- create
@router.post("", status_code=status.HTTP_201_CREATED)
async def create_agent(
    request: Request,
    body: AgentCreate,
    token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    _validate(body.slug, body.upstream_base, body.status, body.runtime_mode)
    if await AgentRegistryRepository.get(db, body.slug) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"agent '{body.slug}' already exists")

    row = await AgentRegistryRepository.create(db, body.model_dump(), actor=_actor(token))
    count = await _hot_reload(db)
    out = AgentRegistryRepository.to_dict(row)
    await _audit(db, request, "agent.create", body.slug, token, after=out)
    logger.info("agent_created", slug=body.slug, live_routes=count)
    return _decorate(out, _live_config().snapshot().get("routes", {}))


# --------------------------------------------------------------------------- per-slug
@router.get("/{slug}")
async def get_agent(
    slug: str,
    _: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    row = await AgentRegistryRepository.get(db, slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"agent '{slug}' not found")
    live = _live_config().snapshot().get("routes", {})
    return _decorate(AgentRegistryRepository.to_dict(row), live)


@router.put("/{slug}")
async def update_agent(
    slug: str,
    request: Request,
    body: AgentUpdate,
    token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    existing = await AgentRegistryRepository.get(db, slug)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"agent '{slug}' not found")
    before = AgentRegistryRepository.to_dict(existing)

    merged_upstream = body.upstream_base if body.upstream_base is not None else existing.upstream_base
    merged_status = body.status if body.status is not None else existing.status
    merged_runtime = body.runtime_mode if body.runtime_mode is not None else existing.runtime_mode
    _validate(slug, merged_upstream, merged_status, merged_runtime)

    row = await AgentRegistryRepository.update(
        db, slug, body.model_dump(exclude_unset=True), actor=_actor(token)
    )
    count = await _hot_reload(db)
    out = AgentRegistryRepository.to_dict(row)
    await _audit(db, request, "agent.update", slug, token, before=before, after=out)
    logger.info("agent_updated", slug=slug, live_routes=count)
    return _decorate(out, _live_config().snapshot().get("routes", {}))


@router.delete("/{slug}")
async def delete_agent(
    slug: str,
    request: Request,
    token: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    existing = await AgentRegistryRepository.get(db, slug)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"agent '{slug}' not found")
    before = AgentRegistryRepository.to_dict(existing)
    await AgentRegistryRepository.delete(db, slug)
    count = await _hot_reload(db)
    await _audit(db, request, "agent.delete", slug, token, before=before)
    logger.info("agent_deleted", slug=slug, live_routes=count)
    return {"deleted": True, "slug": slug, "live_routes": count}


@router.post("/{slug}/health")
async def probe_agent(
    slug: str,
    path: str = Body("/", embed=True),
    _: str = Depends(verify_admin_token),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Probe an agent's upstream reachability from the gateway's network.

    Used by the console's "Test upstream" button before publishing, so an
    operator gets immediate feedback instead of discovering a bad address in
    production traffic.
    """
    row = await AgentRegistryRepository.get(db, slug)
    if row is None:
        raise HTTPException(status_code=404, detail=f"agent '{slug}' not found")

    target = row.upstream_base.rstrip("/") + (path if path.startswith("/") else "/" + path)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
            resp = await client.get(target)
        latency = round((time.perf_counter() - started) * 1000, 1)
        return {
            "reachable": True,
            "status_code": resp.status_code,
            "latency_ms": latency,
            "target": target,
        }
    except Exception as exc:  # noqa: BLE001
        latency = round((time.perf_counter() - started) * 1000, 1)
        return {
            "reachable": False,
            "error": str(exc),
            "latency_ms": latency,
            "target": target,
        }
