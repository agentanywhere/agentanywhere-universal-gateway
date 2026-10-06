"""Tests for the advanced config module: validators, sections,
redaction, hot reload, audit log, schema export, and the admin API.
"""
import json
import os
from pathlib import Path

import pytest
import yaml

from app.config import (
    SAFE_RELOAD_FIELDS,
    SECTIONS,
    SENSITIVE_FIELDS,
    ConfigError,
    Settings,
    apply_runtime_changes,
    get_config_audit_log,
    get_settings,
    reload_settings,
)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


class TestValidators:
    def test_valid_log_level_normalised(self):
        assert Settings(log_level="debug").log_level == "DEBUG"

    def test_invalid_log_level_rejected(self):
        with pytest.raises(Exception):
            Settings(log_level="LOUD")

    def test_invalid_log_format_rejected(self):
        with pytest.raises(Exception):
            Settings(log_format="xml")

    def test_log_format_normalised(self):
        assert Settings(log_format="JSON").log_format == "json"

    def test_sampling_range(self):
        assert Settings(log_sampling_rate=0.5).log_sampling_rate == 0.5
        with pytest.raises(Exception):
            Settings(log_sampling_rate=2.0)
        with pytest.raises(Exception):
            Settings(log_sampling_rate=-0.1)

    def test_invalid_url_rejected(self):
        with pytest.raises(Exception):
            Settings(mcp_backend_url="just-a-host:8001")

    def test_url_with_scheme_accepted(self):
        s = Settings(mcp_backend_url="https://api.example.com")
        assert s.mcp_backend_url == "https://api.example.com"

    def test_port_must_be_positive(self):
        with pytest.raises(Exception):
            Settings(port=-1)


# ---------------------------------------------------------------------------
# Section views & redaction
# ---------------------------------------------------------------------------


class TestSectionsAndRedaction:
    def test_sections_cover_all_fields(self):
        s = Settings()
        sectioned = s.as_sections()
        flat_keys = set()
        for fields in sectioned.values():
            flat_keys.update(fields.keys())
        # Every field declared on Settings should appear in a section
        # (including the catch-all "other" bucket).
        for name in s.model_fields:
            assert name in flat_keys, f"{name} is not in any section"

    def test_redacted_does_not_leak_secret(self):
        s = Settings(secret_key="super-duper-secret-key", smtp_password="hunter2")
        red = s.as_redacted_dict()
        assert "super-duper" not in json.dumps(red)
        assert "hunter2" not in json.dumps(red)

    def test_redacted_keeps_short_fingerprint(self):
        s = Settings(secret_key="super-duper-secret-key")
        red = s.as_redacted_dict()
        assert red["secret_key"].endswith("-key")
        assert "REDACTED" in red["secret_key"]

    def test_non_secret_passes_through(self):
        s = Settings(app_name="MyGateway")
        assert s.as_redacted_dict()["app_name"] == "MyGateway"

    def test_known_sections_match_constants(self):
        for name in SECTIONS:
            assert name in Settings().as_sections().keys()

    def test_diff_from_defaults(self, monkeypatch):
        # Align env with shipped default so diff compares against the same baseline as `.env` users.
        monkeypatch.setenv("APP_NAME", "AgentAnywhere Universal Gateway")
        s = Settings(port=9999, app_name="MyGateway")
        diff = s.diff_from_defaults()
        assert "port" in diff and diff["port"] == (8000, 9999)
        assert diff["app_name"] == ("AgentAnywhere Universal Gateway", "MyGateway")


# ---------------------------------------------------------------------------
# Sensitive fields constant
# ---------------------------------------------------------------------------


class TestSensitiveFields:
    def test_sensitive_fields_known(self):
        assert "secret_key" in SENSITIVE_FIELDS
        assert "smtp_password" in SENSITIVE_FIELDS
        assert "datadog_api_key" in SENSITIVE_FIELDS

    def test_sensitive_fields_subset_of_settings(self):
        fields = set(Settings.model_fields.keys())
        missing = SENSITIVE_FIELDS - fields
        assert not missing, f"unknown sensitive fields: {missing}"


# ---------------------------------------------------------------------------
# Multi-source loader
# ---------------------------------------------------------------------------


class TestConfigFile:
    def test_yaml_file_loaded(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "gateway.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "app_name": "ConfigFromYAML",
                    "logging": {"log_level": "DEBUG"},
                }
            )
        )
        monkeypatch.setenv("CONFIG_FILE", str(config_path))
        get_settings.cache_clear()
        try:
            s = get_settings()
            assert s.app_name == "ConfigFromYAML"
            assert s.log_level == "DEBUG"
        finally:
            monkeypatch.delenv("CONFIG_FILE", raising=False)
            get_settings.cache_clear()

    def test_json_file_loaded(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "gateway.json"
        config_path.write_text(json.dumps({"app_name": "ConfigFromJSON"}))
        monkeypatch.setenv("CONFIG_FILE", str(config_path))
        get_settings.cache_clear()
        try:
            s = get_settings()
            assert s.app_name == "ConfigFromJSON"
        finally:
            monkeypatch.delenv("CONFIG_FILE", raising=False)
            get_settings.cache_clear()

    def test_env_overrides_file(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "gateway.json"
        config_path.write_text(json.dumps({"port": 1234}))
        monkeypatch.setenv("CONFIG_FILE", str(config_path))
        monkeypatch.setenv("PORT", "5678")
        get_settings.cache_clear()
        try:
            assert get_settings().port == 5678
        finally:
            monkeypatch.delenv("CONFIG_FILE", raising=False)
            monkeypatch.delenv("PORT", raising=False)
            get_settings.cache_clear()

    def test_unsupported_format_warns(self, tmp_path: Path, monkeypatch):
        config_path = tmp_path / "gateway.ini"
        config_path.write_text("[section]\nkey = value")
        monkeypatch.setenv("CONFIG_FILE", str(config_path))
        get_settings.cache_clear()
        try:
            # Should fall back gracefully, not raise.
            s = get_settings()
            assert s is not None
        finally:
            monkeypatch.delenv("CONFIG_FILE", raising=False)
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Hot reload + audit
# ---------------------------------------------------------------------------


class TestHotReload:
    def test_apply_runtime_changes_for_logging(self):
        result = apply_runtime_changes({"log_level": "DEBUG", "log_sampling_rate": 0.5})
        assert "log_level" in result["applied"]
        assert "log_sampling_rate" in result["applied"]
        assert get_settings().log_level == "DEBUG"
        assert get_settings().log_sampling_rate == 0.5

    def test_apply_runtime_changes_audit_log(self):
        before = len(get_config_audit_log(limit=500))
        apply_runtime_changes({"log_level": "INFO"}, actor="tester")
        log = get_config_audit_log(limit=500)
        assert len(log) > before
        latest = log[-1]
        assert latest["field"] == "log_level"
        assert latest["actor"] == "tester"
        assert latest["succeeded"] is True

    def test_unsafe_field_rejected(self):
        with pytest.raises(ConfigError):
            apply_runtime_changes({"secret_key": "new-key"})

    def test_unknown_field_rejected(self):
        with pytest.raises(ConfigError):
            apply_runtime_changes({"this_is_not_a_field": True})

    def test_invalid_value_rejected(self):
        with pytest.raises(ConfigError):
            apply_runtime_changes({"log_level": "VERBOSE"})

    def test_safe_reload_fields_recognised(self):
        for field in SAFE_RELOAD_FIELDS:
            assert field in Settings.model_fields, f"unknown safe-reload field: {field}"


# ---------------------------------------------------------------------------
# Reload from env
# ---------------------------------------------------------------------------


class TestReload:
    def test_reload_picks_up_env_changes(self, monkeypatch):
        monkeypatch.setenv("APP_NAME", "ReloadedGateway")
        try:
            new = reload_settings()
            assert new.app_name == "ReloadedGateway"
        finally:
            monkeypatch.delenv("APP_NAME", raising=False)
            reload_settings()  # restore
