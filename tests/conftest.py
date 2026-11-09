"""Pytest configuration and fixtures."""
# The admin API refuses the placeholder SECRET_KEY shipped in env.example,
# because that value is public in this repository and accepting it would
# authenticate anyone. Tests therefore need a real key, set before app.config
# is imported so get_settings() caches the right value.
import os

os.environ.setdefault("SECRET_KEY", "test-only-secret-key-not-a-placeholder-32b")
os.environ.setdefault("STARTUP_BANNER", "false")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import StaticPool
from app.database import Base, get_db
# Import all models to ensure they're registered with Base
from app.db_models import (
    APIKeyModel,
    RouteModel,
    RequestLogModel,
    SettingsProfileModel,
    TenantModel,
    TenantAPIKeyModel,
    TenantUsageModel,
    OpenAPIRegistrationModel,
    AgentRegistryModel,  # noqa: F401
)
from app.config import get_settings
from fastapi.testclient import TestClient
from app.main import app

settings = get_settings()


def pytest_collection_modifyitems(config, items):
    """Performance tests start real processes and take a while: run them only on request."""
    if "performance" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="performance test: run with `pytest -m performance`")
    for item in items:
        if "performance" in item.keywords:
            item.add_marker(skip)


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """Create a test database session."""
    # Use in-memory SQLite for testing
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    async_session = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    
    async with async_session() as session:
        yield session
    
    # Clean up
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    await engine.dispose()


@pytest.fixture
def override_get_db(db_session):
    """Override get_db dependency."""
    async def _get_db():
        yield db_session
    return _get_db


@pytest.fixture
def client(override_get_db):
    """Create test client with database override."""
    from app.database import get_db
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def sample_api_key(db_session):
    """Create a sample API key for testing."""
    from app.repositories.api_key_repo import APIKeyRepository
    api_key, db_key = await APIKeyRepository.create(
        db=db_session,
        name="Test Key",
        rate_limit_per_minute=100,
        rate_limit_per_hour=1000,
    )
    return api_key, db_key


@pytest_asyncio.fixture
async def sample_route(db_session):
    """Create a sample route for testing."""
    from app.repositories.route_repo import RouteRepository
    from app.models import RouteConfig, ProtocolType
    
    route_config = RouteConfig(
        path="/test/*",
        backend_url="http://test-backend:8000",
        protocol=ProtocolType.HTTP,
        timeout=30,
    )
    route = await RouteRepository.create(db_session, route_config)
    return route

