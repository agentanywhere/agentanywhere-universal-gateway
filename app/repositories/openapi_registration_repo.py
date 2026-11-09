"""Persistence for OpenAPI → MCP / A2A registrations."""
from __future__ import annotations

import uuid
from typing import Any, List, Mapping, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_models import OpenAPIRegistrationModel


class OpenAPIRegistrationRepository:
    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        name: str,
        description: Optional[str],
        openapi_document: Mapping[str, Any],
        upstream_base_url: str,
        path_prefix: str = "",
        expose_mcp_tools: bool = True,
        expose_a2a_skills: bool = True,
    ) -> OpenAPIRegistrationModel:
        # `name` is UNIQUE, so a soft-deleted registration with the same name
        # would make the insert fail; it is gone as far as callers are concerned.
        await db.execute(
            delete(OpenAPIRegistrationModel).where(
                OpenAPIRegistrationModel.name == name,
                OpenAPIRegistrationModel.is_active.is_(False),
            )
        )
        row = OpenAPIRegistrationModel(
            id=str(uuid.uuid4()),
            name=name,
            description=description,
            openapi_document=dict(openapi_document),
            upstream_base_url=upstream_base_url.rstrip("/"),
            path_prefix=(path_prefix or "").strip(),
            expose_mcp_tools=expose_mcp_tools,
            expose_a2a_skills=expose_a2a_skills,
            is_active=True,
        )
        db.add(row)
        await db.flush()
        await db.refresh(row)
        return row

    @staticmethod
    async def list_all(db: AsyncSession, *, active_only: bool = True) -> List[OpenAPIRegistrationModel]:
        stmt = select(OpenAPIRegistrationModel)
        if active_only:
            stmt = stmt.where(OpenAPIRegistrationModel.is_active.is_(True))
        stmt = stmt.order_by(OpenAPIRegistrationModel.name)
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_by_id(db: AsyncSession, reg_id: str) -> Optional[OpenAPIRegistrationModel]:
        res = await db.execute(
            select(OpenAPIRegistrationModel).where(OpenAPIRegistrationModel.id == reg_id)
        )
        return res.scalars().first()

    @staticmethod
    async def get_active_by_name(db: AsyncSession, name: str) -> Optional[OpenAPIRegistrationModel]:
        res = await db.execute(
            select(OpenAPIRegistrationModel).where(
                OpenAPIRegistrationModel.name == name,
                OpenAPIRegistrationModel.is_active.is_(True),
            )
        )
        return res.scalars().first()

    @staticmethod
    async def delete_soft(db: AsyncSession, reg_id: str) -> bool:
        row = await OpenAPIRegistrationRepository.get_by_id(db, reg_id)
        if not row:
            return False
        row.is_active = False
        await db.flush()
        return True

    @staticmethod
    async def delete_hard(db: AsyncSession, reg_id: str) -> bool:
        res = await db.execute(delete(OpenAPIRegistrationModel).where(OpenAPIRegistrationModel.id == reg_id))
        return res.rowcount > 0
