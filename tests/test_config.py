"""Tests for configuration."""
import pytest
import os
from app.config import get_settings, Settings


def test_settings_defaults():
    """Test built-in defaults (`.env` may override `app_name`; field default is authoritative)."""
    assert Settings.model_fields["app_name"].default == "AgentAnywhere Universal Gateway"
    settings = get_settings()
    assert settings.port == 8000
    assert settings.host == "0.0.0.0"
    assert settings.rate_limit_enabled is True


def test_settings_from_env(monkeypatch):
    """Test settings loaded from environment."""
    monkeypatch.setenv("PORT", "9000")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    
    # Clear cache to reload settings
    get_settings.cache_clear()
    settings = get_settings()
    
    assert settings.port == 9000
    assert settings.log_level == "DEBUG"
    
    # Restore cache
    get_settings.cache_clear()


def test_settings_validation():
    """Test settings validation."""
    settings = Settings()
    
    # Test that required fields have defaults
    assert settings.secret_key is not None
    assert settings.mcp_backend_url is not None
    assert settings.a2a_backend_url is not None


def test_cors_settings():
    """Test CORS configuration."""
    settings = get_settings()
    
    assert isinstance(settings.cors_origins, list)
    assert settings.cors_allow_methods is not None
    assert settings.cors_allow_headers is not None


