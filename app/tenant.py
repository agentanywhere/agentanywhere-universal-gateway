"""Multi-tenant support.

Provides two layers:

* ``Tenant`` / ``TenantManager`` — lightweight in-memory representation
  used by parts of the codebase that don't have a database session
  available (and by existing tests that exercise the manager directly).

* ``TenantService`` — async, database-backed service that persists
  tenants, tracks per-tenant usage and enforces quotas. The admin
  endpoints prefer the service when a DB session is available, and fall
  back to the in-memory manager otherwise.
"""
from typing import Optional, Dict, Any, List
from app.time_utils import utcnow
import structlog

from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

DEFAULT_QUOTA: Dict[str, Any] = {
    "requests_per_minute": 1000,
    "requests_per_hour": 10000,
    # Stored and counted but not enforced yet: only the minute and hour
    # quotas are checked. storage_mb is not enforced either.
    "requests_per_day": 100000,
    "storage_mb": 1000,
}


class Tenant:
    """In-memory representation of a tenant."""

    def __init__(
        self,
        tenant_id: str,
        name: str,
        config: Optional[Dict[str, Any]] = None,
        quota: Optional[Dict[str, Any]] = None,
        description: Optional[str] = None,
    ):
        self.tenant_id = tenant_id
        self.name = name
        self.description = description
        self.config = config or {}
        self.quota = {**DEFAULT_QUOTA, **(quota or {})}
        self.created_at = utcnow()
        self.is_active = True

    def check_quota(self, resource: str, amount: int = 1) -> bool:
        """Check if tenant has quota for a resource (in-memory check)."""
        limit = (self.quota or {}).get(resource, 0)
        if not limit:
            return True
        return amount <= limit

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "config": self.config,
            "quota": self.quota,
            "created_at": self.created_at.isoformat(),
            "is_active": self.is_active,
        }


class TenantManager:
    """In-memory tenant registry (kept for back-compat)."""

    def __init__(self) -> None:
        self.tenants: Dict[str, Tenant] = {}
        self.tenant_by_api_key: Dict[str, str] = {}  # api_key -> tenant_id

    def create_tenant(
        self,
        tenant_id: str,
        name: str,
        config: Optional[Dict[str, Any]] = None,
        quota: Optional[Dict[str, Any]] = None,
        description: Optional[str] = None,
    ) -> Tenant:
        tenant = Tenant(tenant_id, name, config, quota, description)
        self.tenants[tenant_id] = tenant
        logger.info("tenant_created", tenant_id=tenant_id, name=name)
        return tenant

    def get_tenant(self, tenant_id: str) -> Optional[Tenant]:
        return self.tenants.get(tenant_id)

    def get_tenant_by_api_key(self, api_key: str) -> Optional[Tenant]:
        tenant_id = self.tenant_by_api_key.get(api_key)
        if tenant_id:
            return self.tenants.get(tenant_id)
        return None

    def associate_api_key(self, tenant_id: str, api_key: str) -> None:
        if tenant_id not in self.tenants:
            return
        self.tenant_by_api_key[api_key] = tenant_id
        logger.info("api_key_associated", tenant_id=tenant_id, api_key=api_key[:8])

    def disassociate_api_key(self, api_key: str) -> None:
        self.tenant_by_api_key.pop(api_key, None)

    def list_tenants(self) -> List[Tenant]:
        return list(self.tenants.values())

    def update_tenant_quota(self, tenant_id: str, quota: Dict[str, Any]) -> None:
        if tenant_id in self.tenants:
            self.tenants[tenant_id].quota.update(quota)
            logger.info("tenant_quota_updated", tenant_id=tenant_id, quota=quota)

    def update_tenant_config(self, tenant_id: str, config: Dict[str, Any]) -> None:
        if tenant_id in self.tenants:
            self.tenants[tenant_id].config.update(config)
            logger.info("tenant_config_updated", tenant_id=tenant_id)

    def deactivate_tenant(self, tenant_id: str) -> None:
        if tenant_id in self.tenants:
            self.tenants[tenant_id].is_active = False
            logger.info("tenant_deactivated", tenant_id=tenant_id)

    def activate_tenant(self, tenant_id: str) -> None:
        if tenant_id in self.tenants:
            self.tenants[tenant_id].is_active = True
            logger.info("tenant_activated", tenant_id=tenant_id)

    def delete_tenant(self, tenant_id: str) -> bool:
        if tenant_id not in self.tenants:
            return False
        del self.tenants[tenant_id]
        # Drop any API key associations to that tenant
        self.tenant_by_api_key = {
            k: v for k, v in self.tenant_by_api_key.items() if v != tenant_id
        }
        logger.info("tenant_deleted", tenant_id=tenant_id)
        return True

    async def load_from_db(self, db: AsyncSession) -> int:
        """Hydrate the in-memory registry from persisted tenants.

        Returns the number of tenants loaded. Safe to call multiple times.
        """
        try:
            from app.repositories.tenant_repo import TenantRepository  # noqa: WPS433

            tenants = await TenantRepository.list_all(db)
        except Exception as exc:
            logger.warning("tenant_manager_load_failed", error=str(exc))
            return 0

        self.tenants.clear()
        for tenant in tenants:
            self.tenants[tenant.tenant_id] = Tenant(
                tenant_id=tenant.tenant_id,
                name=tenant.name,
                config=tenant.config or {},
                quota=tenant.quota or {},
                description=tenant.description,
            )
            self.tenants[tenant.tenant_id].is_active = tenant.is_active
        logger.info("tenant_registry_hydrated", count=len(self.tenants))
        return len(self.tenants)


class TenantService:
    """Database-backed tenant service.

    Wraps :class:`TenantRepository` to provide persistence and quota
    enforcement. Falls back gracefully when persistence is unavailable.
    """

    def __init__(self) -> None:
        # Lazy import keeps app.tenant importable without a DB engine
        from app.repositories.tenant_repo import TenantRepository  # noqa: WPS433

        self._repo = TenantRepository

    async def create_tenant(
        self,
        db: AsyncSession,
        tenant_id: str,
        name: str,
        description: Optional[str] = None,
        config: Optional[Dict[str, Any]] = None,
        quota: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        existing = await self._repo.get_by_id(db, tenant_id)
        if existing:
            raise ValueError(f"tenant '{tenant_id}' already exists")
        tenant = await self._repo.create(
            db,
            tenant_id=tenant_id,
            name=name,
            description=description,
            config=config,
            quota=quota,
        )
        # Mirror to the in-memory manager for synchronous lookups
        tenant_manager.create_tenant(tenant_id, name, tenant.config, tenant.quota, description)
        return self._repo.to_dict(tenant)

    async def get_tenant(self, db: AsyncSession, tenant_id: str) -> Optional[Dict[str, Any]]:
        tenant = await self._repo.get_by_id(db, tenant_id)
        if not tenant:
            return None
        return self._repo.to_dict(tenant)

    async def list_tenants(
        self,
        db: AsyncSession,
        active_only: bool = False,
    ) -> List[Dict[str, Any]]:
        tenants = await self._repo.list_all(db, active_only=active_only)
        return [self._repo.to_dict(t) for t in tenants]

    async def update_quota(
        self,
        db: AsyncSession,
        tenant_id: str,
        quota: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        tenant = await self._repo.update_quota(db, tenant_id, quota)
        if not tenant:
            return None
        tenant_manager.update_tenant_quota(tenant_id, tenant.quota or {})
        return self._repo.to_dict(tenant)

    async def update_config(
        self,
        db: AsyncSession,
        tenant_id: str,
        config: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        tenant = await self._repo.update_config(db, tenant_id, config)
        if not tenant:
            return None
        tenant_manager.update_tenant_config(tenant_id, tenant.config or {})
        return self._repo.to_dict(tenant)

    async def set_active(
        self,
        db: AsyncSession,
        tenant_id: str,
        is_active: bool,
    ) -> Optional[Dict[str, Any]]:
        tenant = await self._repo.set_active(db, tenant_id, is_active)
        if not tenant:
            return None
        if is_active:
            tenant_manager.activate_tenant(tenant_id)
        else:
            tenant_manager.deactivate_tenant(tenant_id)
        return self._repo.to_dict(tenant)

    async def delete(self, db: AsyncSession, tenant_id: str) -> bool:
        ok = await self._repo.delete(db, tenant_id)
        if ok:
            tenant_manager.delete_tenant(tenant_id)
        return ok

    async def associate_api_key(
        self,
        db: AsyncSession,
        tenant_id: str,
        api_key_id: str,
        api_key: str,
    ) -> bool:
        tenant = await self._repo.get_by_id(db, tenant_id)
        if not tenant:
            return False
        await self._repo.associate_api_key(db, tenant_id, api_key_id, api_key)
        tenant_manager.associate_api_key(tenant_id, api_key)
        return True

    async def disassociate_api_key(
        self,
        db: AsyncSession,
        api_key_id: str,
    ) -> bool:
        return await self._repo.disassociate_api_key(db, api_key_id)

    async def get_tenant_for_api_key(
        self,
        db: AsyncSession,
        api_key: str,
    ) -> Optional[Dict[str, Any]]:
        tenant = await self._repo.get_tenant_for_api_key(db, api_key)
        if not tenant:
            return None
        return self._repo.to_dict(tenant)

    async def record_request(
        self,
        db: AsyncSession,
        tenant_id: str,
        is_error: bool = False,
        bytes_in: int = 0,
        bytes_out: int = 0,
    ) -> None:
        await self._repo.increment_usage(
            db,
            tenant_id=tenant_id,
            request_count=1,
            error_count=1 if is_error else 0,
            bytes_in=bytes_in,
            bytes_out=bytes_out,
        )

    async def usage(
        self,
        db: AsyncSession,
        tenant_id: str,
        window: str = "hour",
        lookback: int = 24,
    ) -> Dict[str, Any]:
        current = await self._repo.current_usage(db, tenant_id)
        history = await self._repo.aggregate_usage(
            db, tenant_id, window=window, lookback=lookback
        )
        return {"current": current, "history": history}

    _PRIME_KEY = "tenant_quota_prime"

    async def prime_quota(
        self,
        db: AsyncSession,
        tenant: Dict[str, Any],
        windows: tuple = ("minute", "hour"),
    ) -> None:
        """Load what check_quota needs for ``windows`` in one query (none if unlimited).

        Until :meth:`clear_quota_prime`, check_quota for this tenant on this
        session answers from that snapshot instead of re-reading the tenant row
        and re-summing usage on every call. ``tenant`` is the dict the caller
        already resolved (it carries the quota and is_active).
        """
        quota = tenant.get("quota") or {}
        limited = [
            w for w in windows
            if (quota.get(f"requests_per_{w}") or 0) > 0
        ]
        used = (
            await self._repo.used_in_windows(db, tenant["tenant_id"], limited)
            if limited and tenant.get("is_active", True) else {}
        )
        db.info[self._PRIME_KEY] = {
            "tenant_id": tenant["tenant_id"],
            "quota": quota,
            "is_active": tenant.get("is_active", True),
            "used": used,
        }

    def clear_quota_prime(self, db: AsyncSession) -> None:
        db.info.pop(self._PRIME_KEY, None)

    async def check_quota(
        self,
        db: AsyncSession,
        tenant_id: str,
        window: str = "minute",
        cost: int = 1,
    ) -> bool:
        prime = db.info.get(self._PRIME_KEY)
        if prime and prime["tenant_id"] == tenant_id and (
            not prime["is_active"] or window in prime["used"]
            or (prime["quota"].get(f"requests_per_{window}") or 0) <= 0
        ):
            if not prime["is_active"]:
                return False
            limit = prime["quota"].get(f"requests_per_{window}")
            if limit is None or limit <= 0:
                return True
            return (prime["used"][window] + cost) <= int(limit)
        tenant = await self._repo.get_by_id(db, tenant_id)
        if not tenant or not tenant.is_active:
            return False
        return await self._repo.check_quota(db, tenant, window=window, cost=cost)


# Global singletons
tenant_manager = TenantManager()
tenant_service = TenantService()
