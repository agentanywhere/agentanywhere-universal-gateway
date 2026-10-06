"""Repository for the AgentAnywhere agent-edge registry.

CRUD over :class:`app.db_models.AgentRegistryModel` — the DB-backed source of
truth for ``/agents/<slug>`` routing that the admin console manages and the
proxy hot-reloads.
"""
from __future__ import annotations

from app.time_utils import utcnow
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_models import AgentRegistryModel


def _normalize_upstream(url: str) -> str:
    return (url or "").strip().rstrip("/")


def _normalize_paths(paths: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    for p in paths or []:
        p = str(p).strip()
        if not p:
            continue
        if not p.startswith("/"):
            p = "/" + p
        out.append(p)
    # de-dupe, preserve order
    seen: set[str] = set()
    return [p for p in out if not (p in seen or seen.add(p))]


class AgentRegistryRepository:
    """Data-access helpers for the agent registry."""

    @staticmethod
    async def list_all(db: AsyncSession) -> List[AgentRegistryModel]:
        result = await db.execute(
            select(AgentRegistryModel).order_by(AgentRegistryModel.slug)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get(db: AsyncSession, slug: str) -> Optional[AgentRegistryModel]:
        result = await db.execute(
            select(AgentRegistryModel).where(AgentRegistryModel.slug == slug)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def count(db: AsyncSession) -> int:
        result = await db.execute(select(AgentRegistryModel.slug))
        return len(list(result.scalars().all()))

    @staticmethod
    async def create(db: AsyncSession, data: Dict[str, Any], actor: Optional[str] = None) -> AgentRegistryModel:
        row = AgentRegistryModel(
            slug=data["slug"],
            upstream_base=_normalize_upstream(data["upstream_base"]),
            audience=(data.get("audience") or None),
            public_paths=_normalize_paths(data.get("public_paths")),
            status=data.get("status") or "published",
            display_name=data.get("display_name") or None,
            description=data.get("description") or None,
            runtime_mode=data.get("runtime_mode") or "dedicated",
            data_residency=data.get("data_residency") or "in",
            enabled=bool(data.get("enabled", True)),
            updated_by=actor,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    @staticmethod
    async def update(
        db: AsyncSession, slug: str, data: Dict[str, Any], actor: Optional[str] = None
    ) -> Optional[AgentRegistryModel]:
        row = await AgentRegistryRepository.get(db, slug)
        if row is None:
            return None
        if "upstream_base" in data and data["upstream_base"] is not None:
            row.upstream_base = _normalize_upstream(data["upstream_base"])
        if "audience" in data:
            row.audience = data["audience"] or None
        if "public_paths" in data and data["public_paths"] is not None:
            row.public_paths = _normalize_paths(data["public_paths"])
        for field in ("status", "display_name", "description", "runtime_mode", "data_residency"):
            if field in data and data[field] is not None:
                setattr(row, field, data[field])
        if "enabled" in data and data["enabled"] is not None:
            row.enabled = bool(data["enabled"])
        row.updated_by = actor
        row.updated_at = utcnow()
        await db.commit()
        await db.refresh(row)
        return row

    @staticmethod
    async def delete(db: AsyncSession, slug: str) -> bool:
        row = await AgentRegistryRepository.get(db, slug)
        if row is None:
            return False
        await db.delete(row)
        await db.commit()
        return True

    @staticmethod
    async def upsert_many(
        db: AsyncSession, items: List[Dict[str, Any]], actor: Optional[str] = None
    ) -> int:
        """Bulk create/update. Returns the number of rows written."""
        written = 0
        for item in items:
            slug = item.get("slug")
            if not slug:
                continue
            existing = await AgentRegistryRepository.get(db, slug)
            if existing is None:
                await AgentRegistryRepository.create(db, item, actor=actor)
            else:
                await AgentRegistryRepository.update(db, slug, item, actor=actor)
            written += 1
        return written

    @staticmethod
    def to_dict(row: AgentRegistryModel) -> Dict[str, Any]:
        return {
            "slug": row.slug,
            "upstream_base": row.upstream_base,
            "audience": row.audience,
            "effective_audience": row.audience or row.slug,
            "public_paths": list(row.public_paths or []),
            "status": row.status,
            "display_name": row.display_name,
            "description": row.description,
            "runtime_mode": row.runtime_mode,
            "data_residency": row.data_residency,
            "enabled": row.enabled,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "updated_by": row.updated_by,
        }
