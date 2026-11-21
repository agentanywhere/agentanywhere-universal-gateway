"""Tests for multi-tenant persistence and quota tracking."""
import pytest

from app.repositories.tenant_repo import TenantRepository
from app.tenant import (
    DEFAULT_QUOTA,
    Tenant,
    TenantManager,
    TenantService,
)


@pytest.fixture(autouse=True)
def _clock_holds_still(monkeypatch):
    """Usage is counted per minute window; two requests either side of a minute boundary
    would land in two windows. Hold the clock the repository reads still."""
    import app.repositories.tenant_repo as tenant_repo
    frozen = tenant_repo.utcnow().replace(second=30, microsecond=0)
    monkeypatch.setattr(tenant_repo, "utcnow", lambda: frozen)


@pytest.fixture
async def tenant_service(db_session):
    service = TenantService()
    return service


class TestTenantManager:
    def test_create_and_lookup(self):
        manager = TenantManager()
        manager.create_tenant("acme", "Acme Inc")
        assert manager.get_tenant("acme") is not None
        assert manager.get_tenant("missing") is None

    def test_associate_api_key(self):
        manager = TenantManager()
        manager.create_tenant("acme", "Acme Inc")
        manager.associate_api_key("acme", "sk-abc")
        assert manager.get_tenant_by_api_key("sk-abc").tenant_id == "acme"
        assert manager.get_tenant_by_api_key("sk-zzz") is None

    def test_associate_unknown_tenant_is_noop(self):
        manager = TenantManager()
        manager.associate_api_key("ghost", "sk-1")
        assert manager.get_tenant_by_api_key("sk-1") is None

    def test_default_quota_merged(self):
        tenant = Tenant("t1", "Tenant", quota={"requests_per_minute": 5})
        for key in DEFAULT_QUOTA:
            assert key in tenant.quota
        assert tenant.quota["requests_per_minute"] == 5

    def test_deactivate_and_delete(self):
        manager = TenantManager()
        manager.create_tenant("acme", "Acme Inc")
        manager.associate_api_key("acme", "sk-1")
        manager.deactivate_tenant("acme")
        assert manager.get_tenant("acme").is_active is False
        manager.activate_tenant("acme")
        assert manager.get_tenant("acme").is_active is True
        assert manager.delete_tenant("acme") is True
        assert manager.get_tenant("acme") is None
        assert manager.get_tenant_by_api_key("sk-1") is None


class TestTenantRepository:
    @pytest.mark.asyncio
    async def test_create_get_list(self, db_session):
        await TenantRepository.create(
            db_session,
            tenant_id="t1",
            name="Tenant One",
            quota={"requests_per_minute": 100},
        )
        tenant = await TenantRepository.get_by_id(db_session, "t1")
        assert tenant is not None
        assert tenant.name == "Tenant One"
        assert tenant.quota["requests_per_minute"] == 100
        # default fields preserved
        assert "requests_per_hour" in tenant.quota
        all_tenants = await TenantRepository.list_all(db_session)
        assert len(all_tenants) == 1

    @pytest.mark.asyncio
    async def test_associate_and_lookup_api_key(self, db_session):
        await TenantRepository.create(db_session, "t1", "Tenant")
        await TenantRepository.associate_api_key(
            db_session, "t1", "key-id-1", "sk-abc"
        )
        tenant = await TenantRepository.get_tenant_for_api_key(db_session, "sk-abc")
        assert tenant is not None
        assert tenant.tenant_id == "t1"

        # Disassociate cleans up
        ok = await TenantRepository.disassociate_api_key(db_session, "key-id-1")
        assert ok is True
        assert await TenantRepository.get_tenant_for_api_key(db_session, "sk-abc") is None

    @pytest.mark.asyncio
    async def test_increment_usage_and_quota(self, db_session):
        await TenantRepository.create(
            db_session, "t1", "Tenant", quota={"requests_per_minute": 2}
        )
        tenant = await TenantRepository.get_by_id(db_session, "t1")
        assert await TenantRepository.check_quota(db_session, tenant, window="minute") is True

        await TenantRepository.increment_usage(db_session, "t1", windows=["minute"])
        await TenantRepository.increment_usage(db_session, "t1", windows=["minute"])
        assert await TenantRepository.check_quota(db_session, tenant, window="minute") is False

        usage = await TenantRepository.current_usage(db_session, "t1")
        assert usage["minute"]["request_count"] == 2

    @pytest.mark.asyncio
    async def test_update_quota_and_config(self, db_session):
        await TenantRepository.create(db_session, "t1", "Tenant")
        await TenantRepository.update_quota(
            db_session, "t1", {"requests_per_minute": 10}
        )
        tenant = await TenantRepository.get_by_id(db_session, "t1")
        assert tenant.quota["requests_per_minute"] == 10
        await TenantRepository.update_config(db_session, "t1", {"region": "eu"})
        tenant = await TenantRepository.get_by_id(db_session, "t1")
        assert tenant.config["region"] == "eu"


class TestTenantService:
    @pytest.mark.asyncio
    async def test_create_persists_and_mirrors(self, db_session):
        service = TenantService()
        result = await service.create_tenant(
            db_session, tenant_id="t-svc", name="Svc"
        )
        assert result["tenant_id"] == "t-svc"
        assert result["is_active"] is True

    @pytest.mark.asyncio
    async def test_create_duplicate_raises(self, db_session):
        service = TenantService()
        await service.create_tenant(db_session, tenant_id="dup", name="Dup")
        with pytest.raises(ValueError):
            await service.create_tenant(db_session, tenant_id="dup", name="Dup")

    @pytest.mark.asyncio
    async def test_set_active_and_delete(self, db_session):
        service = TenantService()
        await service.create_tenant(db_session, tenant_id="t-active", name="X")
        deactivated = await service.set_active(db_session, "t-active", False)
        assert deactivated["is_active"] is False
        assert await service.delete(db_session, "t-active") is True
        assert await service.get_tenant(db_session, "t-active") is None

    @pytest.mark.asyncio
    async def test_record_request_increments_usage(self, db_session):
        service = TenantService()
        await service.create_tenant(db_session, tenant_id="usage", name="U")
        await service.record_request(db_session, "usage", is_error=False)
        await service.record_request(db_session, "usage", is_error=True, bytes_out=1024)
        report = await service.usage(db_session, "usage")
        assert report["current"]["minute"]["request_count"] == 2
        assert report["current"]["minute"]["error_count"] == 1
        assert report["current"]["minute"]["bytes_out"] == 1024

    @pytest.mark.asyncio
    async def test_check_quota(self, db_session):
        service = TenantService()
        await service.create_tenant(
            db_session,
            tenant_id="q",
            name="Q",
            quota={"requests_per_minute": 1},
        )
        assert await service.check_quota(db_session, "q", window="minute") is True
        await service.record_request(db_session, "q")
        assert await service.check_quota(db_session, "q", window="minute") is False

    @pytest.mark.asyncio
    async def test_associate_api_key(self, db_session):
        service = TenantService()
        await service.create_tenant(db_session, tenant_id="t-api", name="A")
        ok = await service.associate_api_key(db_session, "t-api", "key1", "sk-abc")
        assert ok is True
        tenant = await service.get_tenant_for_api_key(db_session, "sk-abc")
        assert tenant["tenant_id"] == "t-api"

    @pytest.mark.asyncio
    async def test_associate_missing_tenant(self, db_session):
        service = TenantService()
        ok = await service.associate_api_key(db_session, "ghost", "k", "sk")
        assert ok is False
