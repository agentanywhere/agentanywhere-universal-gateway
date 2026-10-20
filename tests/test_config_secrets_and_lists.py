"""Configuration: secrets are redacted, list settings parse, .env reaches every reader.

* ``GET /admin/config`` returned ``ADMIN_TOKEN`` in clear text, because only
  fields named in a fixed set were redacted.
* A comma-separated value for a list setting (``PLUGINS_DISABLED=a,b``)
  stopped start-up: pydantic-settings only accepts JSON for lists.
* ``MCP_GATEWAY_PLUGIN_PATH`` and ``OTEL_*`` were read from ``os.environ`` at
  import time, so a value written in ``.env`` never applied when uvicorn was
  started directly.
"""
import json

import pytest

from app.config import Settings, _redact_value, get_settings, is_sensitive_field

SETTINGS = get_settings()


def _admin_headers():
    return {"Authorization": f"Bearer {SETTINGS.secret_key}"}


# ---------------------------------------------------------------------------
# S2: redaction
# ---------------------------------------------------------------------------


def _walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)
    else:
        yield obj


SECRETS = {
    "admin_token": "admin-token-value-that-must-not-leak-1234",
    "smtp_password": "smtp-password-that-must-not-leak",
    "splunk_token": "splunk-token-that-must-not-leak",
    "datadog_api_key": "datadog-key-that-must-not-leak",
    "aws_secret_access_key": "aws-secret-that-must-not-leak",
    "plugin_configs_json": '{"static-token-auth": {"tokens": ["plugin-token-that-must-not-leak"]}}',
    "otel_exporter_otlp_headers": "authorization=Bearer otlp-token-that-must-not-leak",
}


def test_get_config_redacts_every_secret(client, monkeypatch):
    import admin.api as admin_api

    live = get_settings()  # what the endpoint reads
    for field, value in SECRETS.items():
        monkeypatch.setattr(live, field, value)
    # The admin check reads the settings object admin.api imported, which is
    # a different object once some other test has reloaded the settings.
    monkeypatch.setattr(admin_api.settings, "admin_token", SECRETS["admin_token"])
    monkeypatch.setattr(live, "database_url", "postgresql+asyncpg://gw:db-password-that-must-not-leak@db:5432/gw")
    monkeypatch.setattr(live, "elasticsearch_url", "https://elastic:es-password-that-must-not-leak@es:9200")

    # ADMIN_TOKEN is now set, so it is the admin credential (not SECRET_KEY).
    admin = {"Authorization": f"Bearer {SECRETS['admin_token']}"}
    for path in ("/admin/config", "/admin/config?section=security", "/admin/config/diff"):
        response = client.get(path, headers=admin)
        assert response.status_code == 200, response.text
        text = response.text
        assert live.secret_key not in text, path
        for value in SECRETS.values():
            assert value not in text, (path, value)
            assert "must-not-leak" not in text, (path, text)

    security = client.get("/admin/config", headers=admin).json()["sections"]["security"]
    assert security["admin_token"].startswith("***REDACTED***")


def test_credentialed_url_in_any_field_loses_its_password():
    assert _redact_value("elasticsearch_url", "https://u:pw@es:9200") == "https://u:***REDACTED***@es:9200"
    assert _redact_value("mqtt_broker_url", "mqtt://broker:1883") == "mqtt://broker:1883"


@pytest.mark.parametrize("field", [
    "admin_token", "secret_key", "foo_token", "foo_api_key", "foo_password", "foo_secret", "x_dsn",
])
def test_secret_looking_field_names_are_sensitive(field):
    assert is_sensitive_field(field)


@pytest.mark.parametrize("field", [
    "api_key_header", "access_token_expire_minutes", "admin_username", "log_level",
])
def test_ordinary_field_names_are_not(field):
    assert not is_sensitive_field(field)


# ---------------------------------------------------------------------------
# B4: list settings accept JSON or a comma-separated list
# ---------------------------------------------------------------------------


LIST_FIELDS = [
    name for name, f in Settings.model_fields.items()
    if "List" in str(f.annotation) or "list" in str(f.annotation)
]


def test_the_list_fields_are_the_ones_we_expect():
    for name in ("cors_origins", "plugins_disabled", "plugin_paths", "trusted_proxies",
                 "mcp_boundary_allowed_methods", "mcp_boundary_injection_patterns"):
        assert name in LIST_FIELDS


@pytest.mark.parametrize("field", LIST_FIELDS)
def test_every_list_setting_accepts_a_comma_separated_env_value(field, monkeypatch):
    monkeypatch.setenv(field.upper(), "alpha, beta,gamma")
    s = Settings(_env_file=None)
    assert getattr(s, field) == ["alpha", "beta", "gamma"]


@pytest.mark.parametrize("field", LIST_FIELDS)
def test_every_list_setting_still_accepts_json(field, monkeypatch):
    monkeypatch.setenv(field.upper(), json.dumps(["alpha", "beta"]))
    s = Settings(_env_file=None)
    assert getattr(s, field) == ["alpha", "beta"]


def test_single_value_and_empty_value(monkeypatch):
    monkeypatch.setenv("PLUGINS_DISABLED", "audit-logger")
    assert Settings(_env_file=None).plugins_disabled == ["audit-logger"]
    monkeypatch.setenv("PLUGINS_DISABLED", "")
    assert Settings(_env_file=None).plugins_disabled == []


def test_comma_list_in_a_dotenv_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PLUGINS_DISABLED", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    env = tmp_path / ".env"
    env.write_text("PLUGINS_DISABLED=a,b\nCORS_ORIGINS=https://a.example, https://b.example\n")
    s = Settings(_env_file=str(env))
    assert s.plugins_disabled == ["a", "b"]
    assert s.cors_origins == ["https://a.example", "https://b.example"]


def test_malformed_json_array_is_an_error_naming_the_setting(monkeypatch):
    monkeypatch.setenv("PLUGINS_DISABLED", '["a", ')
    with pytest.raises(Exception) as e:
        Settings(_env_file=None)
    assert "plugins_disabled" in str(e.value).lower()


def test_comma_list_from_init_kwargs_and_config_file_values():
    assert Settings(_env_file=None, plugins_disabled="a,b").plugins_disabled == ["a", "b"]


def test_non_list_settings_are_untouched(monkeypatch):
    monkeypatch.setenv("EMAIL_ALERT_RECIPIENTS", "a@example.com,b@example.com")
    assert Settings(_env_file=None).email_alert_recipients == "a@example.com,b@example.com"


# ---------------------------------------------------------------------------
# B5: MCP_GATEWAY_PLUGIN_PATH and OTEL_* from .env
# ---------------------------------------------------------------------------


def test_otel_and_plugin_path_are_read_from_a_dotenv_file(tmp_path, monkeypatch):
    for name in ("OTEL_TRACING_ENABLED", "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_SERVICE_NAME",
                 "MCP_GATEWAY_PLUGIN_PATH", "MCP_GATEWAY_PLUGIN_DISABLED"):
        monkeypatch.delenv(name, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "OTEL_TRACING_ENABLED=true\n"
        "OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318/v1/traces\n"
        "OTEL_SERVICE_NAME=from-dotenv\n"
        f"MCP_GATEWAY_PLUGIN_PATH={tmp_path}\n"
        "MCP_GATEWAY_PLUGIN_DISABLED=audit-logger\n"
    )
    s = Settings(_env_file=str(env))
    assert s.otel_tracing_enabled is True
    assert s.otel_service_name == "from-dotenv"
    assert s.mcp_gateway_plugin_path == str(tmp_path)

    import app.config as config_module
    from app import observability
    from app.plugins.loader import PluginLoader

    monkeypatch.setattr(config_module, "get_settings", lambda: s)
    captured = {}
    monkeypatch.setattr(observability.tracer, "configure", lambda **kw: captured.update(kw))
    observability.configure_from_env()
    assert captured["enabled"] is True
    assert captured["otlp_endpoint"] == "http://collector:4318/v1/traces"
    assert captured["service_name"] == "from-dotenv"

    loader = PluginLoader()
    assert str(tmp_path) in loader.plugin_paths
    assert "audit-logger" in loader.disabled


def test_process_environment_still_wins_over_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OTEL_SERVICE_NAME=from-dotenv\n")
    s = Settings(_env_file=str(env))
    import app.config as config_module
    from app import observability

    monkeypatch.setattr(config_module, "get_settings", lambda: s)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "from-environment")
    captured = {}
    monkeypatch.setattr(observability.tracer, "configure", lambda **kw: captured.update(kw))
    observability.configure_from_env()
    assert captured["service_name"] == "from-environment"
