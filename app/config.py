"""Configuration management for AgentAnywhere Universal Gateway.

This module is the single source of truth for runtime configuration.
It is designed to be **production grade**:

* **Typed and validated.** Every field has a Python type plus, where
  meaningful, a validator (URL formats, value ranges, exclusivity).
* **Multi-source loading.** Sources are merged in this order (later
  overrides earlier):

    1. Built-in defaults
    2. Optional file pointed to by ``CONFIG_FILE`` (``.yaml`` / ``.yml`` /
       ``.json`` / ``.toml`` if installed)
    3. ``.env`` file (via pydantic-settings)
    4. Process environment variables

* **Secret-aware.** A ``SENSITIVE_FIELDS`` set is consulted by the
  ``as_redacted_dict()`` helper so we never log or return raw secrets
  via the admin API.
* **Hot-reloadable (whitelisted).** ``reload_settings()`` rebuilds the
  ``Settings`` object and ``apply_runtime_changes()`` applies a subset
  of fields to running subsystems (logging, alert thresholds, rate
  limits) without restart.
* **Sectioned view.** ``Settings.as_sections()`` returns the flat config
  organised into logical groups (``application``, ``server``,
  ``security``, ``logging`` …) which is what the admin UI renders.

For backwards compatibility the historic flat field layout is preserved
— callers that do ``settings.smtp_host`` etc. continue to work.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from app.time_utils import utcnow
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


SENSITIVE_FIELDS: Set[str] = {
    "secret_key",
    "admin_token",
    "smtp_password",
    "splunk_token",
    "datadog_api_key",
    "aws_access_key_id",
    "aws_secret_access_key",
    "slack_webhook_url",
    "rate_limit_redis_url",  # may contain credentials
    "redis_url",  # may contain credentials
    "database_url",  # may contain credentials
    "plugin_configs_json",  # plugin settings, e.g. static-token-auth tokens
    "otel_exporter_otlp_headers",  # usually carries an Authorization header
}

# Any field whose name ends in one of these is treated as secret too, so a new
# setting such as ``foo_api_key`` is redacted without having to remember to
# add it above. (``api_key_header`` and ``access_token_expire_minutes`` do not
# end in a secret word and stay visible.)
_SENSITIVE_SUFFIXES: Tuple[str, ...] = (
    "_token",
    "_secret",
    "_password",
    "_passwd",
    "_api_key",
    "_secret_key",
    "_access_key",
    "_access_key_id",
    "_private_key",
    "_credentials",
    "_dsn",
)

# scheme://user:password@host — the password part of a credentialed URL.
_URL_CREDENTIALS = re.compile(r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.\-]*://[^:/@\s]*:)(?P<pw>[^@\s]+)(?P<post>@)")


def is_sensitive_field(field: str) -> bool:
    """True when a setting's value must not be shown in clear."""
    name = (field or "").lower()
    if name in SENSITIVE_FIELDS:
        return True
    return name.endswith(_SENSITIVE_SUFFIXES) or name in {s.lstrip("_") for s in _SENSITIVE_SUFFIXES}


SECTIONS: Dict[str, Sequence[str]] = {
    "application": ("app_name", "app_version", "environment", "debug"),
    "server": ("host", "port", "workers", "gateway_url_base"),
    "security": (
        "secret_key",
        "admin_token",
        "admin_username",
        "admin_accept_secret_key",
        "allow_secret_key_api_key",
        "algorithm",
        "access_token_expire_minutes",
        "api_key_header",
        "trusted_proxies",
    ),
    "cors": (
        "cors_origins",
        "cors_allow_credentials",
        "cors_allow_methods",
        "cors_allow_headers",
    ),
    "rate_limiting": (
        "rate_limit_enabled",
        "rate_limit_per_minute",
        "rate_limit_per_hour",
        "rate_limit_redis_url",
    ),
    "retention": ("request_log_retention_days", "retention_interval_hours"),
    "redis": ("redis_url", "redis_enabled"),
    "database": ("database_url",),
    "backends": (
        "mcp_backend_url",
        "a2a_backend_url",
        "ag_ui_backend_url",
        "acp_backend_url",
        "http_backend_url",
        "mqtt_broker_url",
        "websocket_backend_url",
        "grpc_backend_url",
        "graphql_backend_url",
        "default_timeout",
    ),
    "tier1_protocols": (
        "mcp_boundary_enabled",
        "mcp_boundary_max_body_bytes",
        "mcp_boundary_allowed_methods",
        "mcp_boundary_blocked_methods",
        "mcp_boundary_prompt_injection_action",
        "mcp_boundary_injection_patterns",
        "a2a_boundary_enabled",
        "a2a_enforce_send_message_shape",
    ),
    "logging": (
        "log_level",
        "log_format",
        "log_export_enabled",
        "log_export_batch_size",
        "log_export_flush_interval",
        "log_export_max_queue_size",
        "log_sampling_rate",
        "log_redaction_enabled",
        "log_file_path",
        "log_audit_file_path",
        "log_include_caller",
    ),
    "log_export": (
        "elasticsearch_url",
        "elasticsearch_index",
        "elasticsearch_enabled",
        "splunk_url",
        "splunk_token",
        "splunk_index",
        "splunk_enabled",
        "aws_access_key_id",
        "aws_secret_access_key",
        "s3_log_export_enabled",
        "s3_log_bucket",
        "s3_log_prefix",
        "s3_log_region",
        "cloudwatch_logs_enabled",
        "cloudwatch_log_group",
        "cloudwatch_log_stream",
        "cloudwatch_region",
        "datadog_logs_enabled",
        "datadog_api_key",
        "datadog_site",
        "datadog_service",
        "datadog_source",
        "datadog_tags",
    ),
    "metrics": (
        "metrics_enabled",
        "metrics_path",
        "health_check_path",
        "ops_endpoints_require_admin",
    ),
    "circuit_breaker": (
        "circuit_breaker_enabled",
        "circuit_breaker_failure_threshold",
        "circuit_breaker_timeout",
    ),
    "protocols": (
        "protocol_mcp_rate_limit_per_minute",
        "protocol_mcp_rate_limit_per_hour",
        "protocol_a2a_rate_limit_per_minute",
        "protocol_a2a_rate_limit_per_hour",
        "protocol_http_rate_limit_per_minute",
        "protocol_http_rate_limit_per_hour",
        "protocol_mqtt_rate_limit_per_minute",
        "protocol_mqtt_rate_limit_per_hour",
        "protocol_mqtt_default_port",
        "protocol_mqtt_keep_alive",
        "protocol_websocket_rate_limit_per_minute",
        "protocol_websocket_rate_limit_per_hour",
        "protocol_websocket_max_connections",
        "protocol_websocket_keep_alive",
        "protocol_grpc_rate_limit_per_minute",
        "protocol_grpc_rate_limit_per_hour",
        "protocol_grpc_keepalive_time_ms",
        "protocol_grpc_keepalive_timeout_ms",
        "protocol_grpc_min_time_between_pings_ms",
        "protocol_grpc_min_ping_interval_without_data_ms",
        "protocol_ag_ui_rate_limit_per_minute",
        "protocol_ag_ui_rate_limit_per_hour",
        "protocol_acp_rate_limit_per_minute",
        "protocol_acp_rate_limit_per_hour",
        "tier2_stream_connect_timeout",
        "tier2_stream_read_timeout",
        "acp_boundary_enabled",
        "ag_ui_boundary_enabled",
    ),
    "notifications_email": (
        "smtp_host",
        "smtp_port",
        "smtp_user",
        "smtp_password",
        "smtp_use_tls",
        "smtp_from_email",
        "email_notifications_enabled",
        "email_alert_recipients",
        "email_rate_limit_per_hour",
    ),
    "notifications_slack": (
        "slack_notifications_enabled",
        "slack_webhook_url",
        "slack_channel",
        "slack_username",
        "slack_icon_emoji",
        "slack_rate_limit_per_hour",
    ),
    "alert_thresholds": (
        "alert_threshold_rate_limit",
        "alert_threshold_circuit_breaker",
        "alert_threshold_anomaly_score",
        "alert_threshold_error_rate",
        "alert_threshold_traffic_spike",
        "alert_threshold_min_interval_seconds",
    ),
    "plugins": (
        "plugins_enabled",
        "plugin_paths",
        "plugins_disabled",
        "plugin_configs_json",
        "mcp_gateway_plugin_path",
        "mcp_gateway_plugin_disabled",
    ),
    "tracing": (
        "otel_tracing_enabled",
        "otel_exporter_otlp_endpoint",
        "otel_exporter_otlp_headers",
        "otel_service_name",
    ),
}


# Fields that are *safe* to mutate on a running gateway. Anything outside
# this set requires a process restart.
SAFE_RELOAD_FIELDS: Set[str] = {
    "log_level",
    "log_format",
    "log_sampling_rate",
    "log_redaction_enabled",
    "log_include_caller",
    "alert_threshold_rate_limit",
    "alert_threshold_circuit_breaker",
    "alert_threshold_anomaly_score",
    "alert_threshold_error_rate",
    "alert_threshold_traffic_spike",
    "alert_threshold_min_interval_seconds",
    "rate_limit_enabled",
    "rate_limit_per_minute",
    "rate_limit_per_hour",
    "circuit_breaker_enabled",
    "circuit_breaker_failure_threshold",
    "circuit_breaker_timeout",
    "metrics_enabled",
    "mcp_boundary_enabled",
    "mcp_boundary_max_body_bytes",
    "mcp_boundary_prompt_injection_action",
    "a2a_boundary_enabled",
    "a2a_enforce_send_message_shape",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redact_value(field: str, value: Any) -> Any:
    """Redact a single value if it is a secret.

    A field is secret when it is in ``SENSITIVE_FIELDS`` or its name ends in
    a secret word (see :func:`is_sensitive_field`). Any other string that is
    a URL with ``user:password@`` in it has the password replaced, so a DSN
    in an unexpected field does not leak either.
    """
    if value is None or value == "":
        return value
    if is_sensitive_field(field):
        if isinstance(value, str):
            # Show only the last 4 characters as a fingerprint.
            tail = value[-4:] if len(value) > 8 else ""
            return f"***REDACTED***{tail}" if tail else "***REDACTED***"
        return "***REDACTED***"
    if isinstance(value, str) and "@" in value:
        return _URL_CREDENTIALS.sub(r"\g<pre>***REDACTED***\g<post>", value)
    if isinstance(value, (list, tuple)):
        return [_redact_value(field, item) for item in value]
    return value


def _load_config_file(path: str) -> Dict[str, Any]:
    """Load a YAML, JSON or TOML config file. Unknown formats raise."""
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    suffix = p.suffix.lower()
    if suffix == ".json":
        return json.loads(p.read_text())
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # noqa: WPS433
        except ImportError as e:
            raise RuntimeError("YAML config requires PyYAML") from e
        return yaml.safe_load(p.read_text()) or {}
    if suffix == ".toml":
        try:
            import tomllib  # py311+
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore
            except ImportError as e:
                raise RuntimeError("TOML config requires tomli on Python < 3.11") from e
        return tomllib.loads(p.read_text())
    raise ValueError(f"Unsupported config file format: {suffix}")


def _flatten_config_file(data: Mapping[str, Any]) -> Dict[str, Any]:
    """Flatten a sectioned config-file dict into the flat field layout.

    Both shapes are accepted::

        {"smtp_host": "..."}
        {"notifications_email": {"smtp_host": "..."}}
    """
    flat: Dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict) and key in SECTIONS:
            for sub_key, sub_value in value.items():
                flat[sub_key.lower()] = sub_value
        else:
            flat[key.lower()] = value
    return flat


# ---------------------------------------------------------------------------
# List settings: JSON array or comma-separated
# ---------------------------------------------------------------------------


def _is_list_annotation(annotation: Any) -> bool:
    """True for ``List[...]`` and ``Optional[List[...]]`` annotations."""
    import typing

    origin = typing.get_origin(annotation)
    if origin in (list, List, tuple, set):
        return True
    if origin is typing.Union:
        return any(_is_list_annotation(a) for a in typing.get_args(annotation) if a is not type(None))
    return False


def _split_list_setting(value: Any) -> Any:
    """Turn ``"a, b,c"`` into ``["a", "b", "c"]``; anything else is returned as is.

    A value that starts with ``[`` is meant as JSON and is parsed as JSON, so a
    malformed array is an error rather than being split on its commas.
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("["):
        return json.loads(text)
    return [part.strip() for part in text.split(",") if part.strip()]


class _ListFriendlyDecodeMixin:
    """Lets an environment / .env source accept comma-separated lists.

    pydantic-settings decodes list-typed settings as JSON and stops start-up
    with "error parsing value for field" otherwise. For list fields, a value
    that is not JSON is handed on as the raw string, and the field validator
    on :class:`Settings` splits it on commas.
    """

    def decode_complex_value(self, field_name, field, value):  # type: ignore[override]
        try:
            decoded = json.loads(value)
        except ValueError:
            if _is_list_annotation(field.annotation) and not str(value).strip().startswith("["):
                return value
            raise
        if _is_list_annotation(field.annotation) and not isinstance(decoded, (list, type(None))):
            # e.g. PLUGINS_DISABLED=123 or "audit-logger" given as a JSON string.
            return value if not isinstance(decoded, str) else decoded
        return decoded


def _list_friendly_sources():
    from pydantic_settings import DotEnvSettingsSource, EnvSettingsSource

    class ListFriendlyEnvSettingsSource(_ListFriendlyDecodeMixin, EnvSettingsSource):
        pass

    class ListFriendlyDotEnvSettingsSource(_ListFriendlyDecodeMixin, DotEnvSettingsSource):
        pass

    return ListFriendlyEnvSettingsSource, ListFriendlyDotEnvSettingsSource


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """Application settings (flat layout — preserved for backwards compat)."""

    # Application
    app_name: str = "AgentAnywhere Universal Gateway"
    app_version: str = "1.0.0"
    debug: bool = False
    environment: str = "production"

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4
    gateway_url_base: Optional[str] = None

    # Security
    # Dedicated admin API token. Preferred over reusing secret_key. Empty by
    # default: with no ADMIN_TOKEN and a placeholder SECRET_KEY, the admin API
    # is unreachable rather than open, which is the safe direction to fail.
    admin_token: str = ""
    # Username the admin console's local sign-in form expects. The password is
    # ADMIN_TOKEN (or a non-placeholder SECRET_KEY); there is no default password.
    admin_username: str = "admin"
    secret_key: str = "change-me-in-production-use-strong-random-key"
    # SECRET_KEY is accepted as an admin credential only while ADMIN_TOKEN is
    # empty. true restores the old behaviour of accepting both.
    admin_accept_secret_key: bool = False
    # Break-glass, off by default: accept SECRET_KEY as an API key on the proxy
    # (data plane), for bootstrapping before any API key exists. When enabled,
    # every use is logged as a warning, counted
    # (gateway_secret_key_api_key_uses_total) and written to the governance
    # audit log. Create a real API key instead wherever possible.
    allow_secret_key_api_key: bool = False
    # Not used yet: nothing in this release signs tokens, so ALGORITHM and
    # ACCESS_TOKEN_EXPIRE_MINUTES have no effect.
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30
    api_key_header: str = "X-API-Key"
    # Addresses (IPs or CIDR ranges) of the reverse proxies in front of the
    # gateway. X-Forwarded-For is only read when the connection comes from one
    # of them, and then from the right: the client address is the last entry
    # that is not itself a trusted proxy. Empty (the default) means the header
    # is ignored and the socket peer address is the client address.
    trusted_proxies: List[str] = []

    # CORS. The default lists the admin console's local origins (Vite dev
    # server and docker-compose both serve it on port 3001). A "*" entry turns
    # credentials off; see cors_middleware_options().
    cors_origins: List[str] = ["http://localhost:3001", "http://127.0.0.1:3001"]
    cors_allow_credentials: bool = True
    cors_allow_methods: List[str] = ["*"]
    cors_allow_headers: List[str] = ["*"]

    # Rate Limiting
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000
    rate_limit_redis_url: Optional[str] = None

    # Retention (see app/retention.py): request log rows older than this many
    # days are deleted (0 keeps them forever); pruning runs at start-up and
    # every retention_interval_hours.
    request_log_retention_days: int = 0
    retention_interval_hours: int = 24

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_enabled: bool = True
    # Seconds a Redis call may block (connect and read). Redis is optional;
    # without a short limit an absent server stalls every request.
    redis_socket_timeout: float = 1.0

    # Database
    database_url: Optional[str] = None

    # Backend Services
    mcp_backend_url: str = "http://localhost:8001"
    a2a_backend_url: str = "http://localhost:8002"
    ag_ui_backend_url: Optional[str] = None
    acp_backend_url: Optional[str] = None
    http_backend_url: Optional[str] = None
    mqtt_broker_url: Optional[str] = None
    websocket_backend_url: Optional[str] = None
    grpc_backend_url: Optional[str] = None
    graphql_backend_url: Optional[str] = None
    default_timeout: int = 30

    # ------------------------------------------------------------------
    # Dynamic routes (``POST /admin/routes``). When enabled, a registered
    # route is honoured at request time and takes precedence over the static
    # prefix routing in Router._get_backend_url. Defaults to ON because the
    # documented behaviour has always been that registering a route works —
    # it silently did nothing before this was wired. Set false to pin the
    # gateway to static prefix routing only.
    # ------------------------------------------------------------------
    dynamic_routes_enabled: bool = True
    dynamic_routes_refresh_seconds: int = 15

    # ------------------------------------------------------------------
    # Agent edge — serves multiple agents at
    # gateway.example.com/agents/<slug>/... over one gateway.
    #
    # When enabled, requests under `/agents/<slug>/...` are proxied to the
    # per-slug upstream (prefix-stripped), gated by an OIDC SSO
    # (Keycloak) RS256 token whose `agents` claim must include <slug> —
    # except for paths in the per-slug public allowlist (for example an invite link).
    # ------------------------------------------------------------------
    agent_edge_enabled: bool = False
    # JSON map of slug -> upstream base URL, e.g.
    #   {"orders-agent":"http://10.0.0.10:8090","support-agent":"http://10.0.0.10:8001"}
    agent_routes_json: Optional[str] = None
    # JSON map of slug -> list of path prefixes (relative to the agent root)
    # that bypass SSO, e.g. {"orders-agent":["/invite","/api/v1/invites"]}
    agent_public_paths_json: Optional[str] = None
    # OIDC SSO / Keycloak validation (mirrors the OIDC provider env).
    ida_issuer: Optional[str] = None            # exact realm issuer (single-tenant)
    ida_issuer_base: Optional[str] = None       # multi-tenant base, e.g. https://sso.example.com/realms
    ida_audience: Optional[str] = None          # optional global aud; per-agent defaults to <slug>
    ida_jwks_base: Optional[str] = None         # split-horizon JWKS origin
    ida_jwks_cache_seconds: int = 3600
    ida_leeway_seconds: int = 30
    # Admin console SSO (OIDC public client + PKCE)
    ida_browser_issuer: Optional[str] = None       # browser-facing issuer; defaults to ida_issuer
    ida_admin_audience: Optional[str] = None         # OIDC client id, e.g. gateway-admin
    ida_admin_roles: Optional[str] = None            # comma-separated or JSON list of realm roles

    # Tier-1: MCP / A2A protocol boundary (validation & abuse defenses)
    mcp_boundary_enabled: bool = True
    mcp_boundary_max_body_bytes: int = 2_097_152
    mcp_boundary_allowed_methods: Optional[List[str]] = None
    mcp_boundary_blocked_methods: List[str] = []
    mcp_boundary_prompt_injection_action: str = "block"  # block | audit
    mcp_boundary_injection_patterns: Optional[List[str]] = None
    a2a_boundary_enabled: bool = True
    a2a_enforce_send_message_shape: bool = True

    # Tier-2: AG-UI + ACP + HTTP streaming tuning
    tier2_stream_connect_timeout: float = 60.0
    tier2_stream_read_timeout: Optional[float] = None
    acp_boundary_enabled: bool = True
    ag_ui_boundary_enabled: bool = True
    # Not enforced yet (see the protocol defaults below).
    protocol_ag_ui_rate_limit_per_minute: int = 120
    protocol_ag_ui_rate_limit_per_hour: int = 6000
    protocol_acp_rate_limit_per_minute: int = 120
    protocol_acp_rate_limit_per_hour: int = 6000

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"  # json | text | auto
    log_sampling_rate: float = 1.0
    log_redaction_enabled: bool = True
    log_file_path: Optional[str] = None
    log_audit_file_path: Optional[str] = None
    log_include_caller: bool = False
    # Extra per-logger levels, e.g. "uvicorn.access=WARNING,httpx=WARNING".
    log_level_overrides: str = ""
    # Extra field names to redact (comma-separated) and extra regexes to scrub
    # from values (comma-separated); both add to the built-in defaults.
    log_redact_keys: str = ""
    log_redact_patterns: str = ""
    log_file_max_bytes: int = 100 * 1024 * 1024
    log_file_backup_count: int = 10
    log_include_hostname: bool = True
    log_include_process_info: bool = True
    log_timestamp_format: str = "iso"  # iso | epoch
    # Access logs with a status at or above this are never sampled out.
    log_sample_always_status_ge: int = 400
    startup_banner: bool = True
    # Where request-log rows go: inline (one write per request, the default), batched
    # (bounded queue, one transaction per batch) or off. See app/request_log_writer.py.
    request_log_mode: str = "inline"
    request_log_batch_size: int = 200
    request_log_flush_interval: float = 0.25
    request_log_queue_size: int = 20000
    # Seconds an API key stays cached in each worker's memory when Redis is not used (0 = off).
    # While cached, a request needs no database read or write for authentication; a key that is
    # revoked through another worker can still be accepted here until the entry expires.
    api_key_local_cache_ttl_seconds: float = 0.0
    # Per-request anomaly detection and usage analytics (scikit-learn, run in the request path).
    ai_analytics_enabled: bool = True
    # SQLite only, applied to every new connection. Empty keeps SQLite's own default.
    sqlite_journal_mode: str = ""      # for example WAL
    sqlite_synchronous: str = ""       # for example NORMAL
    sqlite_busy_timeout_ms: int = 0    # 0 keeps the driver's default (5 seconds)
    # Swappable parts of the request path (see app/adapters/__init__.py). Each takes a built-in
    # name or a "module:attribute" path to a factory that receives the settings.
    json_codec: str = "stdlib"            # stdlib | orjson
    upstream_client: str = "httpx"        # httpx
    analytics_adapter: str = "full"       # full | off
    request_log_sink: str = "database"    # database | null
    # Where the start-up banner points people for help; set it to your own docs site.
    documentation_url: str = "https://docs.agentanywhere.ai/gateway/"

    # Metrics
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    # Opt-in: require the admin credential (Authorization: Bearer <ADMIN_TOKEN>,
    # same check as the admin API) on the metrics path and /health/detailed.
    # Off by default so an unauthenticated Prometheus scrape keeps working;
    # when you turn it on, give Prometheus the token (see prometheus.yml).
    ops_endpoints_require_admin: bool = False

    # Health Check
    health_check_path: str = "/health"

    # Circuit Breaker
    circuit_breaker_enabled: bool = True
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_timeout: int = 60

    # Protocol-specific Defaults. The *_rate_limit_per_minute/_hour values are
    # NOT enforced yet: they are only reported by GET /admin/settings/protocols.
    # Requests are limited per API key.
    protocol_mcp_rate_limit_per_minute: int = 60
    protocol_mcp_rate_limit_per_hour: int = 1000
    protocol_a2a_rate_limit_per_minute: int = 60
    protocol_a2a_rate_limit_per_hour: int = 1000
    protocol_http_rate_limit_per_minute: int = 100
    protocol_http_rate_limit_per_hour: int = 5000
    protocol_mqtt_rate_limit_per_minute: int = 200
    protocol_mqtt_rate_limit_per_hour: int = 10000
    protocol_mqtt_default_port: int = 1883
    protocol_mqtt_keep_alive: int = 60
    protocol_websocket_rate_limit_per_minute: int = 300
    protocol_websocket_rate_limit_per_hour: int = 15000
    protocol_websocket_max_connections: int = 500
    protocol_websocket_keep_alive: int = 30
    protocol_grpc_rate_limit_per_minute: int = 150
    protocol_grpc_rate_limit_per_hour: int = 8000
    protocol_grpc_keepalive_time_ms: int = 30000
    protocol_grpc_keepalive_timeout_ms: int = 5000
    protocol_grpc_min_time_between_pings_ms: int = 10000
    protocol_grpc_min_ping_interval_without_data_ms: int = 300000

    # Log Export
    log_export_enabled: bool = False
    log_export_batch_size: int = 100
    log_export_flush_interval: float = 5.0
    log_export_max_queue_size: int = 10000
    elasticsearch_url: Optional[str] = None
    elasticsearch_index: str = "gateway-logs"
    elasticsearch_enabled: bool = False
    splunk_url: Optional[str] = None
    splunk_token: Optional[str] = None
    splunk_index: str = "gateway"
    splunk_enabled: bool = False
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    s3_log_export_enabled: bool = False
    s3_log_bucket: Optional[str] = None
    s3_log_prefix: str = "gateway-logs/"
    s3_log_region: str = "us-east-1"
    cloudwatch_logs_enabled: bool = False
    cloudwatch_log_group: Optional[str] = None
    cloudwatch_log_stream: Optional[str] = None
    cloudwatch_region: str = "us-east-1"
    datadog_logs_enabled: bool = False
    datadog_api_key: Optional[str] = None
    datadog_site: str = "datadoghq.com"
    datadog_service: str = "agentanywhere-gateway"
    datadog_source: str = "python"
    datadog_tags: str = ""

    # Email Notifications
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_use_tls: bool = True
    smtp_from_email: Optional[str] = None
    email_notifications_enabled: bool = False
    email_alert_recipients: str = ""
    email_rate_limit_per_hour: int = 50

    # Slack Notifications
    slack_notifications_enabled: bool = False
    slack_webhook_url: Optional[str] = None
    slack_channel: Optional[str] = None
    slack_username: str = "AgentAnywhere Gateway"
    slack_icon_emoji: str = ":rotating_light:"
    slack_rate_limit_per_hour: int = 60

    # Plugin System
    plugins_enabled: bool = True
    plugin_paths: List[str] = []
    plugins_disabled: List[str] = []
    plugin_configs_json: Optional[str] = None
    # Compatibility names read by the plugin loader. Declared here so that a
    # value in .env takes effect too, not only one in the process environment.
    # MCP_GATEWAY_PLUGIN_PATH is os.pathsep-separated; *_DISABLED is a comma list.
    mcp_gateway_plugin_path: str = ""
    mcp_gateway_plugin_disabled: str = ""

    # OpenTelemetry export (see app/observability.py). Declared here so that
    # .env values apply when uvicorn is started directly.
    otel_tracing_enabled: bool = False
    otel_exporter_otlp_endpoint: Optional[str] = None
    otel_exporter_otlp_headers: str = ""
    otel_service_name: str = "agentanywhere-gateway"

    # Alert Thresholds Configuration
    alert_threshold_rate_limit: bool = True
    alert_threshold_circuit_breaker: bool = True
    alert_threshold_anomaly_score: float = 0.7
    alert_threshold_error_rate: float = 0.1
    alert_threshold_traffic_spike: float = 2.0
    alert_threshold_min_interval_seconds: int = 300

    # ------------------------------------------------------------------
    # Pydantic configuration
    # ------------------------------------------------------------------

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Same sources and order as pydantic-settings, but list settings may
        be written as ``a,b`` as well as ``["a","b"]`` (see
        :class:`_ListFriendlyDecodeMixin`)."""
        env_cls, dotenv_cls = _list_friendly_sources()
        env_settings = env_cls(
            settings_cls,
            case_sensitive=env_settings.case_sensitive,
            env_prefix=env_settings.env_prefix,
            env_nested_delimiter=env_settings.env_nested_delimiter,
        )
        dotenv_settings = dotenv_cls(
            settings_cls,
            env_file=dotenv_settings.env_file,
            env_file_encoding=dotenv_settings.env_file_encoding,
            case_sensitive=dotenv_settings.case_sensitive,
            env_prefix=dotenv_settings.env_prefix,
            env_nested_delimiter=dotenv_settings.env_nested_delimiter,
        )
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("*", mode="before")
    @classmethod
    def _accept_comma_separated_lists(cls, v: Any, info: Any) -> Any:
        """Every list setting accepts a JSON array or a comma-separated string."""
        if not isinstance(v, str):
            return v
        field = cls.model_fields.get(info.field_name)
        if field is None or not _is_list_annotation(field.annotation):
            return v
        return _split_list_setting(v)

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        upper = (v or "INFO").upper()
        if upper not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(
                f"log_level must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL, got {v!r}"
            )
        return upper

    @field_validator("log_format")
    @classmethod
    def _validate_log_format(cls, v: str) -> str:
        lower = (v or "json").lower()
        if lower not in {"json", "text", "auto"}:
            raise ValueError(f"log_format must be 'json', 'text' or 'auto', got {v!r}")
        return lower

    @field_validator("log_sampling_rate")
    @classmethod
    def _validate_sampling(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("log_sampling_rate must be between 0.0 and 1.0")
        return v

    @field_validator("port", "workers")
    @classmethod
    def _validate_positive_int(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be a positive integer")
        return v

    @field_validator("environment")
    @classmethod
    def _validate_environment(cls, v: str) -> str:
        v = (v or "production").lower()
        if v not in {"development", "staging", "production", "test", "local"}:
            # Don't reject unknown environments; just normalise.
            return v
        return v

    @field_validator(
        "mcp_backend_url",
        "a2a_backend_url",
        "http_backend_url",
        "websocket_backend_url",
        "graphql_backend_url",
        "elasticsearch_url",
        "splunk_url",
        mode="before",
    )
    @classmethod
    def _validate_url(cls, v: Optional[str]) -> Optional[str]:
        if v in (None, ""):
            return None if v is None else v
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", v):
            raise ValueError(f"URL must include a scheme, got {v!r}")
        return v

    @model_validator(mode="after")
    def _validate_config_consistency(self) -> "Settings":
        # Production cannot run with the default secret key.
        if self.environment == "production" and self.secret_key.startswith("change-me"):
            # Don't crash — just warn via stderr; the admin UI will show
            # a critical config alert.
            import sys

            print(
                "[config] WARNING: SECRET_KEY is set to its default value in "
                "production. Generate a real key with scripts/generate_secret_key.py.",
                file=sys.stderr,
            )
        return self

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def as_dict(self) -> Dict[str, Any]:
        """Return the full config as a dict (no redaction)."""
        return self.model_dump()

    def as_redacted_dict(self) -> Dict[str, Any]:
        """Return the full config with sensitive fields redacted.

        Safe for logging or returning via the admin API.
        """
        out: Dict[str, Any] = {}
        for key, value in self.model_dump().items():
            out[key] = _redact_value(key, value)
        return out

    def as_sections(self, redacted: bool = True) -> Dict[str, Dict[str, Any]]:
        """Return the config grouped into logical sections."""
        source = self.as_redacted_dict() if redacted else self.as_dict()
        sectioned: Dict[str, Dict[str, Any]] = {name: {} for name in SECTIONS}
        unsectioned: Dict[str, Any] = {}
        seen: Set[str] = set()
        for section_name, fields in SECTIONS.items():
            for field_name in fields:
                if field_name in source:
                    sectioned[section_name][field_name] = source[field_name]
                    seen.add(field_name)
        for k, v in source.items():
            if k not in seen:
                unsectioned[k] = v
        if unsectioned:
            sectioned["other"] = unsectioned
        return sectioned

    def diff_from_defaults(self) -> Dict[str, Tuple[Any, Any]]:
        """Return ``{field: (default, current)}`` for fields that differ
        from their built-in defaults. Sensitive fields are redacted."""
        defaults = type(self)().model_dump()
        current = self.model_dump()
        out: Dict[str, Tuple[Any, Any]] = {}
        for key, current_value in current.items():
            default_value = defaults.get(key)
            if current_value != default_value:
                out[key] = (
                    _redact_value(key, default_value),
                    _redact_value(key, current_value),
                )
        return out


# ---------------------------------------------------------------------------
# Loader & cache
# ---------------------------------------------------------------------------


def _build_settings() -> Settings:
    """Build a Settings object honoring the optional CONFIG_FILE.

    Precedence (lowest to highest):
      1. Built-in defaults
      2. Values from ``CONFIG_FILE`` (YAML/JSON/TOML)
      3. ``.env`` file
      4. Process environment variables
    """
    config_file = os.environ.get("CONFIG_FILE")
    file_overrides: Dict[str, Any] = {}
    if config_file:
        try:
            file_overrides = _flatten_config_file(_load_config_file(config_file))
        except Exception as e:  # noqa: BLE001
            import sys

            print(f"[config] WARNING: failed to load CONFIG_FILE: {e}", file=sys.stderr)

    # Filter out any file-supplied keys whose env var is also set, so
    # env always wins. (pydantic-settings would otherwise treat init
    # kwargs as the highest-priority source.)
    if file_overrides:
        filtered = {
            key: value
            for key, value in file_overrides.items()
            if key.upper() not in os.environ
        }
        if filtered:
            return Settings(**filtered)
    return Settings()


def cors_middleware_options(settings: "Settings") -> Tuple[Dict[str, Any], Optional[str]]:
    """Keyword arguments for Starlette's ``CORSMiddleware``, plus a warning.

    Browsers reject ``Access-Control-Allow-Origin: *`` together with
    ``Access-Control-Allow-Credentials: true``, and Starlette works around
    that by echoing whatever ``Origin`` the request sent, which lets any web
    site make credentialed requests. So when the origin list contains ``*``,
    credentials are switched off and a warning is returned for the caller to
    log. An explicit origin list keeps ``cors_allow_credentials`` as set.
    """
    origins = list(settings.cors_origins or [])
    allow_credentials = bool(settings.cors_allow_credentials)
    warning = None
    if "*" in origins and allow_credentials:
        allow_credentials = False
        warning = (
            'CORS_ORIGINS contains "*" and CORS_ALLOW_CREDENTIALS is true; '
            "credentials are disabled for cross-origin requests. List the "
            "exact origins of your front ends in CORS_ORIGINS to allow "
            "credentials."
        )
    options = {
        "allow_origins": origins,
        "allow_credentials": allow_credentials,
        "allow_methods": settings.cors_allow_methods,
        "allow_headers": settings.cors_allow_headers,
    }
    return options, warning


@lru_cache()
def get_settings() -> Settings:
    """Cached settings accessor. Use ``reload_settings()`` to refresh."""
    return _build_settings()


def reload_settings() -> Settings:
    """Rebuild ``Settings`` from current env / file and refresh the cache."""
    get_settings.cache_clear()
    return get_settings()


# ---------------------------------------------------------------------------
# Audit log of runtime config changes
# ---------------------------------------------------------------------------


@dataclass
class ConfigChange:
    """A single change applied to runtime config."""

    field: str
    old_value: Any
    new_value: Any
    actor: Optional[str]
    timestamp: datetime
    succeeded: bool
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "field": self.field,
            "old_value": _redact_value(self.field, self.old_value),
            "new_value": _redact_value(self.field, self.new_value),
            "actor": self.actor,
            "timestamp": self.timestamp.isoformat(),
            "succeeded": self.succeeded,
            "error": self.error,
        }


_audit_log: List[ConfigChange] = []
_AUDIT_LOG_MAX = 500


def get_config_audit_log(limit: int = 100) -> List[Dict[str, Any]]:
    return [entry.to_dict() for entry in _audit_log[-limit:]]


def _record_change(change: ConfigChange) -> None:
    _audit_log.append(change)
    if len(_audit_log) > _AUDIT_LOG_MAX:
        del _audit_log[: len(_audit_log) - _AUDIT_LOG_MAX]


# ---------------------------------------------------------------------------
# Safe runtime application of config
# ---------------------------------------------------------------------------


class ConfigError(ValueError):
    """Raised when a runtime config change is rejected."""


def apply_runtime_changes(
    updates: Mapping[str, Any],
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply a subset of config fields to running subsystems.

    Only fields in ``SAFE_RELOAD_FIELDS`` are accepted. Unknown or unsafe
    fields are rejected wholesale (atomic). On success the fields are
    written back into the cached ``Settings`` instance and applied to:

    * the logging stack (level, format, sampling, redaction, caller)
    * the alert thresholds module
    * the rate-limit / circuit-breaker config used by the gateway

    Returns ``{"applied": [...], "skipped": [...]}``.
    """
    if not updates:
        return {"applied": [], "skipped": []}

    invalid = [k for k in updates if k not in SAFE_RELOAD_FIELDS]
    if invalid:
        raise ConfigError(
            f"Fields cannot be hot-reloaded: {invalid}. "
            f"Allowed: {sorted(SAFE_RELOAD_FIELDS)}"
        )

    settings = get_settings()
    # Validate by constructing a fresh instance — this runs all field
    # and model validators (``model_copy`` does not).
    try:
        merged = {**settings.model_dump(), **dict(updates)}
        validated = Settings(**merged)
    except Exception as e:
        raise ConfigError(f"Invalid config values: {e}") from e

    applied: List[str] = []
    now = utcnow()

    for field_name, new_value in updates.items():
        old_value = getattr(settings, field_name)
        try:
            setattr(settings, field_name, getattr(validated, field_name))
            _apply_to_subsystem(field_name, getattr(validated, field_name))
            applied.append(field_name)
            _record_change(
                ConfigChange(
                    field=field_name,
                    old_value=old_value,
                    new_value=new_value,
                    actor=actor,
                    timestamp=now,
                    succeeded=True,
                )
            )
        except Exception as e:  # noqa: BLE001
            _record_change(
                ConfigChange(
                    field=field_name,
                    old_value=old_value,
                    new_value=new_value,
                    actor=actor,
                    timestamp=now,
                    succeeded=False,
                    error=str(e),
                )
            )
            raise ConfigError(f"Failed to apply {field_name}: {e}") from e

    return {"applied": applied, "skipped": []}


def _apply_to_subsystem(field_name: str, value: Any) -> None:
    """Push a single field value into the running subsystems that use it."""
    if field_name in {
        "log_level",
        "log_format",
        "log_sampling_rate",
        "log_redaction_enabled",
        "log_include_caller",
    }:
        # Lazy import to avoid an import cycle at module load time.
        from app.logging_config import update_config as _update_logging
        from app.logging_config import setup_logging as _setup_logging

        kwargs: Dict[str, Any] = {}
        if field_name == "log_level":
            kwargs["level"] = value
        elif field_name == "log_format":
            kwargs["format"] = value
        elif field_name == "log_sampling_rate":
            kwargs["sample_rate"] = value
        elif field_name == "log_redaction_enabled":
            kwargs["redaction_enabled"] = value
        elif field_name == "log_include_caller":
            kwargs["include_caller"] = value
        _update_logging(**kwargs)
        # Re-apply for level changes that affect handlers.
        if field_name in {"log_level", "log_format", "log_include_caller"}:
            _setup_logging()
        return

    # Alert thresholds are read live from settings, no extra wiring.
    if field_name.startswith("alert_threshold_"):
        return

    # Rate limiting and circuit breaker are read live too.
    if field_name in {
        "rate_limit_enabled",
        "rate_limit_per_minute",
        "rate_limit_per_hour",
        "circuit_breaker_enabled",
        "circuit_breaker_failure_threshold",
        "circuit_breaker_timeout",
        "metrics_enabled",
    }:
        return


__all__ = [
    "ConfigChange",
    "ConfigError",
    "SAFE_RELOAD_FIELDS",
    "SECTIONS",
    "SENSITIVE_FIELDS",
    "Settings",
    "apply_runtime_changes",
    "cors_middleware_options",
    "get_config_audit_log",
    "get_settings",
    "reload_settings",
]
