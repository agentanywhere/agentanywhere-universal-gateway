"""Logging is configurable from settings, bad values are reported, and the start-up banner is safe."""
import io
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import logging_config as lc
from app.banner import banner_facts, print_banner, render_banner


def _settings(**over):
    base = dict(
        app_name="Test Gateway", app_version="9.9.9", environment="staging",
        host="0.0.0.0", port=8123, debug=False, admin_token="", secret_key="change-me-x",
        database_url=None, redis_enabled=False, agent_edge_enabled=False,
        ops_endpoints_require_admin=False, startup_banner=True,
        log_level="info", log_format="json", log_sampling_rate=0.5, log_redaction_enabled=True,
        log_include_caller=False, log_file_path=None, log_audit_file_path=None,
        log_level_overrides="", log_redact_keys="", log_redact_patterns="",
        log_file_max_bytes=2048, log_file_backup_count=3, log_include_hostname=False,
        log_include_process_info=False, log_timestamp_format="epoch", log_sample_always_status_ge=500,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def cfg():
    return lc.LoggingConfig()


def test_parse_level_overrides_accepts_pairs_and_reports_bad_entries():
    levels, problems = lc.parse_level_overrides("uvicorn.access=warning, httpx = ERROR ,bad,x=LOUD,=INFO,")
    assert levels == {"uvicorn.access": "WARNING", "httpx": "ERROR"}
    assert len(problems) == 3 and all("LOG_LEVEL_OVERRIDES" in p for p in problems)
    assert lc.parse_level_overrides(None) == ({}, [])


def test_settings_are_applied_to_the_logging_config(cfg):
    problems = lc._apply_settings(
        cfg,
        _settings(log_level_overrides="httpx=WARNING", log_redact_keys="SSN, card_number",
                  log_redact_patterns=r"\bAKIA[0-9A-Z]{16}\b"),
    )
    assert problems == []
    assert (cfg.level, cfg.format, cfg.sample_rate) == ("INFO", "json", 0.5)
    assert cfg.file_max_bytes == 2048 and cfg.file_backup_count == 3
    assert cfg.include_hostname is False and cfg.include_process_info is False
    assert cfg.timestamp_format == "epoch" and cfg.sample_always_status_ge == 500
    assert cfg.logger_levels["httpx"] == "WARNING"
    assert {"ssn", "card_number"} <= cfg.redact_keys and "password" in cfg.redact_keys
    assert r"\bAKIA[0-9A-Z]{16}\b" in cfg.redact_patterns


def test_invalid_values_fall_back_and_are_reported(cfg):
    problems = lc._apply_settings(
        cfg,
        _settings(log_level="loud", log_format="xml", log_timestamp_format="rfc", log_sampling_rate=7,
                  log_redact_patterns="([unclosed,ok", log_level_overrides="nope"),
    )
    joined = "\n".join(problems)
    for name in ("LOG_LEVEL", "LOG_FORMAT", "LOG_TIMESTAMP_FORMAT", "LOG_REDACT_PATTERNS", "LOG_LEVEL_OVERRIDES"):
        assert name in joined
    assert (cfg.level, cfg.format, cfg.timestamp_format) == ("INFO", "json", "iso")
    assert cfg.sample_rate == 1.0
    assert "ok" in cfg.redact_patterns and "([unclosed" not in cfg.redact_patterns


def test_extra_redact_pattern_scrubs_values(monkeypatch):
    monkeypatch.setattr(lc, "_config", lc.LoggingConfig())
    lc._apply_settings(lc._config, _settings(log_redact_patterns=r"\bAKIA[0-9A-Z]{16}\b"))
    assert "AKIAABCDEFGHIJKLMNOP" not in str(lc._redact_value("key AKIAABCDEFGHIJKLMNOP here"))


def test_setup_logging_reports_bad_settings_as_a_log_line(monkeypatch, capsys):
    import app.config as appconfig
    monkeypatch.setattr(lc, "_config", lc.LoggingConfig())
    monkeypatch.setattr(appconfig, "get_settings", lambda: _settings(log_level="loud"))
    lc.setup_logging(refresh_from_settings=True)
    assert "logging_config_invalid" in capsys.readouterr().out


def test_banner_shows_urls_and_never_a_secret():
    s = _settings(admin_token="super-secret-admin-token", secret_key="another-secret-value-123",
                  database_url="postgresql+asyncpg://user:hunter2@db.internal:5432/gw")
    text = render_banner(s)
    assert "Test Gateway" in text and "v9.9.9" in text and "staging" in text
    assert "http://127.0.0.1:8123/health" in text
    assert "ADMIN_TOKEN" in text and "postgresql database" in text
    for secret in ("super-secret-admin-token", "another-secret-value-123", "hunter2", "db.internal"):
        assert secret not in text


@pytest.mark.parametrize("token,key,expect", [
    ("t", "change-me", "ADMIN_TOKEN"),
    ("", "a-real-secret-key", "SECRET_KEY"),
    ("", "change-me-in-production", "locked"),
])
def test_banner_names_the_admin_credential(token, key, expect):
    assert expect in render_banner(_settings(admin_token=token, secret_key=key))


def test_banner_docs_row_follows_debug():
    assert "off" in render_banner(_settings(debug=False))
    assert banner_facts(_settings(debug=True))["docs_url"] == "http://127.0.0.1:8123/docs"


def test_banner_color_only_when_asked():
    assert "\033[" not in render_banner(_settings())
    assert "\033[" in render_banner(_settings(), color=True)


def test_print_banner_respects_the_switch_and_no_color():
    out = io.StringIO()
    assert print_banner(_settings(startup_banner=False), out) is False and out.getvalue() == ""
    assert print_banner(_settings(), out) is True
    assert "Test Gateway" in out.getvalue() and not re.search(r"\033\[", out.getvalue())


def test_print_banner_survives_a_closed_stream():
    out = io.StringIO()
    out.close()
    assert print_banner(_settings(), out) is False


def test_startup_logs_gateway_ready_with_the_banner_facts(monkeypatch):
    from unittest.mock import MagicMock

    from fastapi.testclient import TestClient

    import app.main as main

    log = MagicMock()
    monkeypatch.setattr(main, "logger", log)
    monkeypatch.setattr(main, "init_db", AsyncMock())
    monkeypatch.setattr(main, "close_db", AsyncMock())
    with TestClient(main.app):
        pass
    ready = [c for c in log.info.call_args_list if c.args and c.args[0] == "gateway_ready"]
    assert len(ready) == 1
    assert {"health_url", "protocols", "admin_credential", "log_level"} <= ready[0].kwargs.keys()


@pytest.mark.parametrize("argv,expected", [
    (["/venv/bin/uvicorn", "app.main:app"], True),
    (["/venv/lib/uvicorn/__main__.py", "app.main:app"], True),
    (["/venv/bin/gunicorn", "app.main:app"], True),
    (["/venv/bin/pytest", "tests"], False),
    (["script.py", "uvicorn"], True),
    ([], False),
])
def test_banner_only_when_started_by_a_server(argv, expected):
    from app.banner import running_as_server
    assert running_as_server(argv) is expected


def test_announce_startup_prints_once_and_not_for_tools(monkeypatch):
    import app.banner as banner

    monkeypatch.setattr(banner, "_announced", False)
    out = io.StringIO()
    assert banner.announce_startup(_settings(), out, argv=["pytest"]) is False and out.getvalue() == ""
    assert banner.announce_startup(_settings(), out, argv=["uvicorn", "app.main:app"]) is True
    assert "Test Gateway" in out.getvalue()
    assert banner.announce_startup(_settings(), out, argv=["uvicorn", "app.main:app"]) is False
    assert out.getvalue().count("Test Gateway") == 1


def test_announce_startup_respects_the_switch(monkeypatch):
    import app.banner as banner

    monkeypatch.setattr(banner, "_announced", False)
    out = io.StringIO()
    assert banner.announce_startup(_settings(startup_banner=False), out, argv=["uvicorn"]) is False
    assert out.getvalue() == ""


@pytest.mark.parametrize("argv,expected", [
    (["app.main:app", "--port", "9100"], ("0.0.0.0", 9100)),
    (["app.main:app", "--host=10.1.2.3", "--port=9200"], ("10.1.2.3", 9200)),
    (["app.main:app", "--port", "not-a-number"], ("0.0.0.0", 8123)),
    ([], ("0.0.0.0", 8123)),
])
def test_banner_address_follows_the_uvicorn_flags(argv, expected):
    from app.banner import _bind_address
    assert _bind_address(_settings(), argv) == expected


def test_banner_health_url_uses_the_real_port(monkeypatch):
    monkeypatch.setattr("sys.argv", ["uvicorn", "app.main:app", "--port", "9300"])
    assert banner_facts(_settings())["health_url"] == "http://127.0.0.1:9300/health"


def test_banner_lists_protocols_console_and_documentation():
    text = render_banner(_settings(
        http_backend_url="http://h:1", mcp_backend_url="http://m:2", a2a_backend_url=None,
        documentation_url="https://docs.example.com/gw",
    ))
    assert "HTTP/REST · MCP · A2A · AG-UI · ACP · GraphQL · MQTT" in text
    assert "WebSocket · gRPC" in text and "no pass-through" in text
    assert "http://127.0.0.1:3001" in text and "npm run dev" in text
    assert "https://docs.example.com/gw" in text
    assert "HTTP/REST, MCP" in text and "A2A," not in text.split("Backends")[1].splitlines()[0]
    assert "http://h:1" not in text and "http://m:2" not in text      # names only, never URLs


def test_banner_backends_row_when_none_configured_and_default_docs_link():
    s = _settings(http_backend_url=None, mcp_backend_url=None, a2a_backend_url=None)
    text = render_banner(s)
    assert "none configured" in text and "docs/README.md" in text
