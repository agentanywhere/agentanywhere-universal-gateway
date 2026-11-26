"""Tests for repository classes."""
import pytest
from datetime import timedelta
from app.time_utils import utcnow
from app.repositories.api_key_repo import APIKeyRepository
from app.repositories.route_repo import RouteRepository
from app.repositories.log_repo import LogRepository
from app.models import RouteConfig, ProtocolType, RequestLog


@pytest.mark.asyncio
async def test_api_key_create(db_session):
    """Test API key creation."""
    api_key, db_key = await APIKeyRepository.create(
        db=db_session,
        name="Test Key",
        rate_limit_per_minute=60,
        rate_limit_per_hour=1000,
    )
    
    assert api_key is not None
    assert api_key.startswith("sk-")
    assert db_key.name == "Test Key"
    assert db_key.rate_limit_per_minute == 60
    assert db_key.rate_limit_per_hour == 1000
    assert db_key.is_active is True


@pytest.mark.asyncio
async def test_api_key_get_by_key(db_session, sample_api_key):
    """Test retrieving API key by key value."""
    api_key, db_key = sample_api_key
    
    retrieved = await APIKeyRepository.get_by_key(db_session, api_key)
    assert retrieved is not None
    assert retrieved.id == db_key.id
    assert retrieved.name == db_key.name


@pytest.mark.asyncio
async def test_api_key_get_by_id(db_session, sample_api_key):
    """Test retrieving API key by ID."""
    api_key, db_key = sample_api_key
    
    retrieved = await APIKeyRepository.get_by_id(db_session, db_key.id)
    assert retrieved is not None
    assert retrieved.id == db_key.id


@pytest.mark.asyncio
async def test_api_key_list_all(db_session):
    """Test listing all API keys."""
    # Create multiple keys
    await APIKeyRepository.create(db_session, name="Key 1")
    await APIKeyRepository.create(db_session, name="Key 2")
    await APIKeyRepository.create(db_session, name="Key 3")
    
    keys = await APIKeyRepository.list_all(db_session)
    assert len(keys) >= 3


@pytest.mark.asyncio
async def test_api_key_toggle_active(db_session, sample_api_key):
    """Test toggling API key active status."""
    api_key, db_key = sample_api_key
    original_status = db_key.is_active
    
    toggled = await APIKeyRepository.toggle_active(db_session, db_key.id)
    assert toggled is not None
    assert toggled.is_active != original_status


@pytest.mark.asyncio
async def test_api_key_delete(db_session, sample_api_key):
    """Test deleting API key."""
    api_key, db_key = sample_api_key
    
    result = await APIKeyRepository.delete(db_session, db_key.id)
    assert result is True
    
    # Verify deletion
    retrieved = await APIKeyRepository.get_by_id(db_session, db_key.id)
    assert retrieved is None


@pytest.mark.asyncio
async def test_api_key_update_last_used(db_session, sample_api_key):
    """Test updating last used timestamp."""
    api_key, db_key = sample_api_key
    
    await APIKeyRepository.update_last_used(db_session, db_key.id)
    
    # Refresh from database
    updated = await APIKeyRepository.get_by_id(db_session, db_key.id)
    assert updated.last_used_at is not None


@pytest.mark.asyncio
async def test_api_key_expires_at(db_session):
    """Test API key with expiration."""
    expires_at = utcnow() + timedelta(days=30)
    api_key, db_key = await APIKeyRepository.create(
        db=db_session,
        name="Expiring Key",
        expires_at=expires_at,
    )
    
    assert db_key.expires_at is not None
    assert db_key.expires_at == expires_at


@pytest.mark.asyncio
async def test_route_create(db_session):
    """Test route creation."""
    route_config = RouteConfig(
        path="/api/test/*",
        backend_url="http://backend:8000",
        protocol=ProtocolType.HTTP,
        timeout=60,
        requires_auth=True,
    )
    
    route = await RouteRepository.create(db_session, route_config)
    
    assert route.path == route_config.path
    assert route.backend_url == route_config.backend_url
    assert route.protocol == route_config.protocol.value
    assert route.timeout == route_config.timeout
    assert route.requires_auth == route_config.requires_auth


@pytest.mark.asyncio
async def test_route_find_matching(db_session):
    """Test finding matching route."""
    # Create routes
    await RouteRepository.create(
        db_session,
        RouteConfig(
            path="/api/v1/*",
            backend_url="http://v1:8000",
            protocol=ProtocolType.HTTP,
        )
    )
    await RouteRepository.create(
        db_session,
        RouteConfig(
            path="/api/v2/*",
            backend_url="http://v2:8000",
            protocol=ProtocolType.HTTP,
        )
    )
    
    # Test wildcard matching
    match = await RouteRepository.find_matching_route(db_session, "/api/v1/users")
    assert match is not None
    assert match.path == "/api/v1/*"
    
    # Test exact match
    await RouteRepository.create(
        db_session,
        RouteConfig(
            path="/exact",
            backend_url="http://exact:8000",
            protocol=ProtocolType.HTTP,
        )
    )
    match = await RouteRepository.find_matching_route(db_session, "/exact")
    assert match is not None
    assert match.path == "/exact"


@pytest.mark.asyncio
async def test_route_list_all(db_session):
    """Test listing all routes."""
    await RouteRepository.create(
        db_session,
        RouteConfig(
            path="/route1/*",
            backend_url="http://backend1:8000",
            protocol=ProtocolType.MCP,
        )
    )
    await RouteRepository.create(
        db_session,
        RouteConfig(
            path="/route2/*",
            backend_url="http://backend2:8000",
            protocol=ProtocolType.A2A,
        )
    )
    
    routes = await RouteRepository.list_all(db_session)
    assert len(routes) >= 2


@pytest.mark.asyncio
async def test_route_update(db_session, sample_route):
    """Test updating route."""
    new_config = RouteConfig(
        path="/updated/*",
        backend_url="http://updated:8000",
        protocol=ProtocolType.HTTP,
        timeout=60,
    )
    
    updated = await RouteRepository.update(db_session, sample_route.id, new_config)
    assert updated is not None
    assert updated.path == new_config.path
    assert updated.backend_url == new_config.backend_url


@pytest.mark.asyncio
async def test_route_delete(db_session, sample_route):
    """Test deleting route."""
    result = await RouteRepository.delete(db_session, sample_route.id)
    assert result is True
    
    retrieved = await RouteRepository.get_by_id(db_session, sample_route.id)
    assert retrieved is None


@pytest.mark.asyncio
async def test_log_create(db_session):
    """Test creating request log."""
    log = RequestLog(
        request_id="test-123",
        method="GET",
        path="/test",
        client_ip="127.0.0.1",
        status_code=200,
        response_time_ms=50.5,
    )
    
    db_log = await LogRepository.create(db_session, log)
    
    assert db_log.request_id == log.request_id
    assert db_log.method == log.method
    assert db_log.status_code == log.status_code


@pytest.mark.asyncio
async def test_log_list_with_filters(db_session):
    """Test listing logs with filters."""
    # Create test logs
    for i in range(5):
        log = RequestLog(
            request_id=f"test-{i}",
            method="GET",
            path=f"/test/{i}",
            client_ip="127.0.0.1",
            status_code=200 if i < 3 else 500,
            response_time_ms=50.0,
        )
        await LogRepository.create(db_session, log)
    
    # Test filtering by status code
    logs, total = await LogRepository.list_logs(
        db_session,
        limit=10,
        status_code=500,
    )
    assert total >= 2
    
    # Test filtering by path
    logs, total = await LogRepository.list_logs(
        db_session,
        limit=10,
        path="/test/1",
    )
    assert total >= 1


@pytest.mark.asyncio
async def test_log_get_stats(db_session):
    """Test getting log statistics."""
    # Create logs with different status codes
    for status in [200, 200, 404, 500]:
        log = RequestLog(
            request_id=f"stats-{status}",
            method="GET",
            path="/test",
            client_ip="127.0.0.1",
            status_code=status,
            response_time_ms=100.0,
        )
        await LogRepository.create(db_session, log)
    
    stats = await LogRepository.get_stats(db_session)
    
    assert stats["total_requests"] >= 4
    assert stats["total_errors"] >= 2  # 404 and 500
    assert stats["avg_response_time"] > 0


@pytest.mark.asyncio
async def test_log_pagination(db_session):
    """Test log pagination."""
    # Create multiple logs
    for i in range(15):
        log = RequestLog(
            request_id=f"page-{i}",
            method="GET",
            path="/test",
            client_ip="127.0.0.1",
            status_code=200,
            response_time_ms=50.0,
        )
        await LogRepository.create(db_session, log)
    
    # Test pagination
    logs, total = await LogRepository.list_logs(db_session, limit=5, offset=0)
    assert len(logs) == 5
    assert total >= 15
    
    # Test second page
    logs2, total2 = await LogRepository.list_logs(db_session, limit=5, offset=5)
    assert len(logs2) == 5
    assert total2 == total  # Total should be same




@pytest.mark.asyncio
async def test_mark_used_writes_last_used_at_most_once_per_interval(db_session, sample_api_key, monkeypatch):
    """A busy key is not written on every request: one write per interval is enough."""
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "api_key_last_used_interval_seconds", 60.0)
    _, db_key = sample_api_key
    await APIKeyRepository.mark_used(db_session, db_key)
    first = (await APIKeyRepository.get_by_id(db_session, db_key.id)).last_used_at
    assert first is not None

    writes = []
    real_execute = db_session.execute

    async def counting_execute(*a, **kw):
        writes.append(a[0])
        return await real_execute(*a, **kw)

    monkeypatch.setattr(db_session, "execute", counting_execute)
    await APIKeyRepository.mark_used(db_session, db_key)
    assert writes == []                       # within the interval: nothing written

    db_key.last_used_at = first - timedelta(seconds=61)
    await APIKeyRepository.mark_used(db_session, db_key)
    assert len(writes) == 1                   # interval passed: written again


@pytest.mark.asyncio
async def test_mark_used_interval_zero_writes_every_use(db_session, sample_api_key, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "api_key_last_used_interval_seconds", 0)
    _, db_key = sample_api_key
    await APIKeyRepository.mark_used(db_session, db_key)
    first = db_key.last_used_at
    await APIKeyRepository.mark_used(db_session, db_key)
    assert db_key.last_used_at >= first and db_key.last_used_at is not None


def test_used_recently_compares_naive_and_aware_times():
    from datetime import timezone
    now = utcnow()
    assert APIKeyRepository._used_recently(now.replace(tzinfo=timezone.utc) - timedelta(seconds=5), now) is True
    assert APIKeyRepository._used_recently(None, now) is False
    assert APIKeyRepository._used_recently(now + timedelta(seconds=5), now) is False   # clock skew: write
