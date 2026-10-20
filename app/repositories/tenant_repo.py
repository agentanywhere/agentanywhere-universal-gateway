"""Tenant repository for database operations."""
from datetime import datetime, timedelta
from app.time_utils import utcnow
from typing import Optional, List, Dict, Any

import hashlib
from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db_models import TenantModel, TenantAPIKeyModel, TenantUsageModel


class TenantRepository:
    """Repository for tenant persistence and quota tracking."""

    @staticmethod
    def _hash_api_key(api_key: str) -> str:
        """Hash an API key for stable lookups."""
        return hashlib.sha256(api_key.encode()).hexdigest()

    @staticmethod
    async def create(
        db: AsyncSession,
        tenant_id: str,
        name: str,
        description: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        quota: Optional[Dict[str, Any]] = None,
    ) -> TenantModel:
        """Create a new tenant."""
        default_quota = {
            "requests_per_minute": 1000,
            "requests_per_hour": 10000,
            "requests_per_day": 100000,  # not enforced yet (see app/tenant.py)
            "storage_mb": 1000,  # not enforced yet
        }
        tenant = TenantModel(
            tenant_id=tenant_id,
            name=name,
            description=description,
            config=config or {},
            quota={**default_quota, **(quota or {})},
            is_active=True,
        )
        db.add(tenant)
        await db.commit()
        await db.refresh(tenant)
        return tenant

    @staticmethod
    async def get_by_id(db: AsyncSession, tenant_id: str) -> Optional[TenantModel]:
        result = await db.execute(
            select(TenantModel).where(TenantModel.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_all(db: AsyncSession, active_only: bool = False) -> List[TenantModel]:
        query = select(TenantModel)
        if active_only:
            query = query.where(TenantModel.is_active.is_(True))
        result = await db.execute(query.order_by(TenantModel.created_at))
        return list(result.scalars().all())

    @staticmethod
    async def update_quota(
        db: AsyncSession,
        tenant_id: str,
        quota: Dict[str, Any],
    ) -> Optional[TenantModel]:
        tenant = await TenantRepository.get_by_id(db, tenant_id)
        if not tenant:
            return None
        merged = {**(tenant.quota or {}), **quota}
        tenant.quota = merged
        tenant.updated_at = utcnow()
        await db.commit()
        await db.refresh(tenant)
        return tenant

    @staticmethod
    async def update_config(
        db: AsyncSession,
        tenant_id: str,
        config: Dict[str, Any],
    ) -> Optional[TenantModel]:
        tenant = await TenantRepository.get_by_id(db, tenant_id)
        if not tenant:
            return None
        merged = {**(tenant.config or {}), **config}
        tenant.config = merged
        tenant.updated_at = utcnow()
        await db.commit()
        await db.refresh(tenant)
        return tenant

    @staticmethod
    async def set_active(
        db: AsyncSession,
        tenant_id: str,
        is_active: bool,
    ) -> Optional[TenantModel]:
        tenant = await TenantRepository.get_by_id(db, tenant_id)
        if not tenant:
            return None
        tenant.is_active = is_active
        tenant.updated_at = utcnow()
        await db.commit()
        await db.refresh(tenant)
        return tenant

    @staticmethod
    async def delete(db: AsyncSession, tenant_id: str) -> bool:
        tenant = await TenantRepository.get_by_id(db, tenant_id)
        if not tenant:
            return False
        # Explicit cascade — SQLite does not enforce FK ondelete by default,
        # so we clear associations and usage rows manually.
        await db.execute(
            delete(TenantAPIKeyModel).where(TenantAPIKeyModel.tenant_id == tenant_id)
        )
        await db.execute(
            delete(TenantUsageModel).where(TenantUsageModel.tenant_id == tenant_id)
        )
        await db.delete(tenant)
        await db.commit()
        return True

    @staticmethod
    async def associate_api_key(
        db: AsyncSession,
        tenant_id: str,
        api_key_id: str,
        api_key: str,
    ) -> TenantAPIKeyModel:
        """Associate an API key (and its hash) with a tenant."""
        await db.execute(
            delete(TenantAPIKeyModel).where(TenantAPIKeyModel.api_key_id == api_key_id)
        )
        link = TenantAPIKeyModel(
            tenant_id=tenant_id,
            api_key_id=api_key_id,
            api_key_hash=TenantRepository._hash_api_key(api_key),
        )
        db.add(link)
        await db.commit()
        await db.refresh(link)
        return link

    @staticmethod
    async def disassociate_api_key(
        db: AsyncSession,
        api_key_id: str,
    ) -> bool:
        result = await db.execute(
            delete(TenantAPIKeyModel).where(TenantAPIKeyModel.api_key_id == api_key_id)
        )
        await db.commit()
        return (result.rowcount or 0) > 0

    @staticmethod
    async def get_tenant_for_api_key(
        db: AsyncSession,
        api_key: str,
    ) -> Optional[TenantModel]:
        """Look up the tenant associated with an API key."""
        api_key_hash = TenantRepository._hash_api_key(api_key)
        result = await db.execute(
            select(TenantModel)
            .join(TenantAPIKeyModel, TenantAPIKeyModel.tenant_id == TenantModel.tenant_id)
            .where(TenantAPIKeyModel.api_key_hash == api_key_hash)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_api_keys_for_tenant(
        db: AsyncSession,
        tenant_id: str,
    ) -> List[str]:
        result = await db.execute(
            select(TenantAPIKeyModel.api_key_id)
            .where(TenantAPIKeyModel.tenant_id == tenant_id)
        )
        return [row[0] for row in result.all()]

    # ---------------- Usage tracking ----------------

    @staticmethod
    def _window_bounds(window: str, now: Optional[datetime] = None) -> datetime:
        now = now or utcnow()
        if window == "minute":
            return now.replace(second=0, microsecond=0)
        if window == "hour":
            return now.replace(minute=0, second=0, microsecond=0)
        if window == "day":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        return now.replace(second=0, microsecond=0)

    @staticmethod
    async def increment_usage(
        db: AsyncSession,
        tenant_id: str,
        windows: Optional[List[str]] = None,
        request_count: int = 1,
        error_count: int = 0,
        bytes_in: int = 0,
        bytes_out: int = 0,
    ) -> None:
        """Increment usage counters for a tenant across rolling windows."""
        windows = windows or ["minute", "hour", "day"]
        for window in windows:
            window_start = TenantRepository._window_bounds(window)
            existing = await db.execute(
                select(TenantUsageModel)
                .where(TenantUsageModel.tenant_id == tenant_id)
                .where(TenantUsageModel.window == window)
                .where(TenantUsageModel.window_start == window_start)
            )
            row = existing.scalar_one_or_none()
            if row is None:
                row = TenantUsageModel(
                    tenant_id=tenant_id,
                    window=window,
                    window_start=window_start,
                    request_count=request_count,
                    error_count=error_count,
                    bytes_in=bytes_in,
                    bytes_out=bytes_out,
                )
                db.add(row)
            else:
                row.request_count += request_count
                row.error_count += error_count
                row.bytes_in += bytes_in
                row.bytes_out += bytes_out
        await db.commit()

    @staticmethod
    async def current_usage(
        db: AsyncSession,
        tenant_id: str,
    ) -> Dict[str, Dict[str, int]]:
        """Return current usage counters across windows for a tenant."""
        usage: Dict[str, Dict[str, int]] = {}
        for window in ("minute", "hour", "day"):
            window_start = TenantRepository._window_bounds(window)
            result = await db.execute(
                select(TenantUsageModel)
                .where(TenantUsageModel.tenant_id == tenant_id)
                .where(TenantUsageModel.window == window)
                .where(TenantUsageModel.window_start == window_start)
            )
            row = result.scalar_one_or_none()
            usage[window] = {
                "request_count": row.request_count if row else 0,
                "error_count": row.error_count if row else 0,
                "bytes_in": row.bytes_in if row else 0,
                "bytes_out": row.bytes_out if row else 0,
            }
        return usage

    @staticmethod
    async def aggregate_usage(
        db: AsyncSession,
        tenant_id: str,
        window: str = "hour",
        lookback: int = 24,
    ) -> List[Dict[str, Any]]:
        """Return historical usage rows for a tenant for the given window."""
        if window == "minute":
            cutoff = utcnow() - timedelta(minutes=lookback)
        elif window == "day":
            cutoff = utcnow() - timedelta(days=lookback)
        else:
            cutoff = utcnow() - timedelta(hours=lookback)

        result = await db.execute(
            select(TenantUsageModel)
            .where(TenantUsageModel.tenant_id == tenant_id)
            .where(TenantUsageModel.window == window)
            .where(TenantUsageModel.window_start >= cutoff)
            .order_by(TenantUsageModel.window_start)
        )
        rows = list(result.scalars().all())
        return [
            {
                "window": r.window,
                "window_start": r.window_start.isoformat(),
                "request_count": r.request_count,
                "error_count": r.error_count,
                "bytes_in": r.bytes_in,
                "bytes_out": r.bytes_out,
            }
            for r in rows
        ]

    @staticmethod
    async def check_quota(
        db: AsyncSession,
        tenant: TenantModel,
        window: str = "minute",
        cost: int = 1,
    ) -> bool:
        """Return True if the tenant has remaining quota for the given window."""
        quota_key = f"requests_per_{window}"
        limit = (tenant.quota or {}).get(quota_key)
        if limit is None or limit <= 0:
            return True
        window_start = TenantRepository._window_bounds(window)
        result = await db.execute(
            select(func.coalesce(func.sum(TenantUsageModel.request_count), 0))
            .where(TenantUsageModel.tenant_id == tenant.tenant_id)
            .where(TenantUsageModel.window == window)
            .where(TenantUsageModel.window_start == window_start)
        )
        used = int(result.scalar() or 0)
        return (used + cost) <= int(limit)

    @staticmethod
    def to_dict(tenant: TenantModel) -> Dict[str, Any]:
        return {
            "tenant_id": tenant.tenant_id,
            "name": tenant.name,
            "description": tenant.description,
            "config": tenant.config or {},
            "quota": tenant.quota or {},
            "is_active": tenant.is_active,
            "created_at": tenant.created_at.isoformat() if tenant.created_at else None,
            "updated_at": tenant.updated_at.isoformat() if tenant.updated_at else None,
        }
