"""Tests for admin API endpoints."""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.config import get_settings
from app.repositories.api_key_repo import APIKeyRepository
from app.repositories.route_repo import RouteRepository
from app.models import RouteConfig, ProtocolType

settings = get_settings()


@pytest.fixture
def admin_token():
    """Get admin token for testing."""
    return settings.secret_key


@pytest.mark.asyncio
async def test_list_api_keys(client, admin_token, db_session):
    """Test listing API keys."""
    # Create test keys
    await APIKeyRepository.create(db_session, name="Test Key 1")
    await APIKeyRepository.create(db_session, name="Test Key 2")
    
    response = client.get(
        "/admin/api-keys",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 2


@pytest.mark.asyncio
async def test_create_api_key(client, admin_token):
    """Test creating API key."""
    response = client.post(
        "/admin/api-keys",
        params={
            "name": "New Test Key",
            "rate_limit_per_minute": 100,
            "rate_limit_per_hour": 2000,
        },
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Test Key"
    assert data["key"].startswith("sk-")
    assert data["rate_limit_per_minute"] == 100


@pytest.mark.asyncio
async def test_delete_api_key(client, admin_token, db_session):
    """Test deleting API key."""
    # Create key
    api_key, db_key = await APIKeyRepository.create(db_session, name="To Delete")
    
    response = client.delete(
        f"/admin/api-keys/{db_key.id}",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    
    assert response.status_code == 200
    assert response.json()["message"] == "API key deleted"


@pytest.mark.asyncio
async def test_toggle_api_key(client, admin_token, db_session):
    """Test toggling API key status."""
    # Create key
    api_key, db_key = await APIKeyRepository.create(db_session, name="Toggle Test")
    original_status = db_key.is_active
    
    response = client.put(
        f"/admin/api-keys/{db_key.id}/toggle",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["is_active"] != original_status


@pytest.mark.asyncio
async def test_list_routes(client, admin_token, db_session):
    """Test listing routes."""
    # Create test route
    route_config = RouteConfig(
        path="/test/*",
        backend_url="http://test:8000",
        protocol=ProtocolType.HTTP,
    )
    await RouteRepository.create(db_session, route_config)
    
    response = client.get(
        "/admin/routes",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert len(data) > 0


@pytest.mark.asyncio
async def test_create_route(client, admin_token):
    """Test creating route."""
    route_data = {
        "path": "/new-route/*",
        "backend_url": "http://new-backend:8000",
        "protocol": "http",
        "timeout": 60,
        "requires_auth": True,
    }
    
    response = client.post(
        "/admin/routes",
        json=route_data,
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert data["path"] == route_data["path"]
    assert data["backend_url"] == route_data["backend_url"]


@pytest.mark.asyncio
async def test_get_logs(client, admin_token, db_session):
    """Test getting logs."""
    from app.repositories.log_repo import LogRepository
    from app.models import RequestLog
    
    # Create test logs
    for i in range(3):
        log = RequestLog(
            request_id=f"log-{i}",
            method="GET",
            path=f"/test/{i}",
            client_ip="127.0.0.1",
            status_code=200,
            response_time_ms=50.0,
        )
        await LogRepository.create(db_session, log)
    
    response = client.get(
        "/admin/logs",
        params={"limit": 10},
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert len(data["logs"]) >= 3
    assert data["total"] >= 3


@pytest.mark.asyncio
async def test_get_logs_with_filters(client, admin_token, db_session):
    """Test getting logs with filters."""
    from app.repositories.log_repo import LogRepository
    from app.models import RequestLog
    
    # Create logs with different status codes
    log1 = RequestLog(
        request_id="log-200",
        method="GET",
        path="/test",
        client_ip="127.0.0.1",
        status_code=200,
        response_time_ms=50.0,
    )
    log2 = RequestLog(
        request_id="log-500",
        method="GET",
        path="/test",
        client_ip="127.0.0.1",
        status_code=500,
        response_time_ms=100.0,
    )
    await LogRepository.create(db_session, log1)
    await LogRepository.create(db_session, log2)
    
    # Filter by error level
    response = client.get(
        "/admin/logs",
        params={"level": "error", "limit": 10},
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    
    assert response.status_code == 200
    data = response.json()
    # Should only get error logs (500+)
    assert all(log["status_code"] >= 500 for log in data["logs"])


@pytest.mark.asyncio
async def test_get_config(client, admin_token):
    """Test getting configuration."""
    response = client.get(
        "/admin/config",
        headers={"Authorization": f"Bearer {admin_token}"}
    )

    assert response.status_code == 200
    data = response.json()
    # New sectioned layout.
    assert "sections" in data
    backends = data["sections"]["backends"]
    assert "mcp_backend_url" in backends
    assert "a2a_backend_url" in backends
    # Sensitive section should be redacted by default.
    security = data["sections"]["security"]
    assert security["secret_key"].startswith("***REDACTED***") or security["secret_key"] == "***REDACTED***"


@pytest.mark.asyncio
async def test_admin_auth_required(client):
    """Test that admin endpoints require authentication."""
    response = client.get("/admin/api-keys")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_admin_invalid_token(client):
    """Test admin endpoint with invalid token."""
    response = client.get(
        "/admin/api-keys",
        headers={"Authorization": "Bearer invalid-token"}
    )
    assert response.status_code == 401


# SDK Generation Tests
@pytest.mark.asyncio
async def test_get_supported_languages(client, admin_token):
    """Test getting supported SDK languages."""
    response = client.get(
        "/admin/sdk/languages",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert "languages" in data
    assert "count" in data
    assert isinstance(data["languages"], list)
    assert len(data["languages"]) > 0


@pytest.mark.asyncio
async def test_preview_sdk_python(client, admin_token):
    """Test previewing Python SDK."""
    response = client.get(
        "/admin/sdk/preview/python",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 200
    data = response.json()
    assert data["language"] == "python"
    assert "files" in data
    assert "file_count" in data
    assert "preview" in data


@pytest.mark.asyncio
async def test_preview_sdk_unsupported_language(client, admin_token):
    """Test previewing unsupported language."""
    response = client.get(
        "/admin/sdk/preview/rust",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_generate_sdk_python(client, admin_token):
    """Test generating Python SDK."""
    import zipfile
    import io
    
    response = client.post(
        "/admin/sdk/generate/python",
        headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    
    # Verify ZIP contents
    zip_data = io.BytesIO(response.content)
    with zipfile.ZipFile(zip_data, 'r') as zip_file:
        files = zip_file.namelist()
        assert len(files) > 0

