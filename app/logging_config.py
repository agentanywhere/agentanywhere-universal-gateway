"""World-class structured logging for AgentAnywhere Universal Gateway.

Design goals:

* **Structured by default.** JSON in production, friendly console output
  when stdout is a TTY (or when ``log_format=text`` is set).
* **Stable schema.** Every record carries a predictable set of top-level
  keys (``@timestamp``, ``level``, ``service.name``, ``service.version``,
  ``env``, ``host``, ``pid``, ``message``, ``event``) plus any
  request-scoped context (``request_id``, ``trace.id``, ``span.id``,
  ``tenant_id``, ``api_key_id``, ``client_ip``) bound via
  ``contextvars`` — no plumbing required at call sites.
* **W3C Trace Context.** Parses incoming ``traceparent`` headers,
  generates new ones for un-traced requests, and exposes the trace +
  span IDs to every log line and to the propagation middleware.
* **PII / secret redaction.** A processor scrubs configurable keys and
  values matching configurable regex patterns (``Bearer …``, API keys,
  password fields, email addresses, …).
* **Sampling.** Probabilistic sampling per logger and per request status
  code, configurable at runtime.
* **Per-logger level overrides.** Override the level of any logger
  (``uvicorn.access``, ``httpx``, …) at runtime.
* **Audit channel.** A dedicated ``audit`` logger that bypasses
  sampling and emits to its own configurable destination.
* **OpenTelemetry / ECS compatible.** Field aliases follow the
  Elastic Common Schema and the OpenTelemetry log data model.

Most code should just do::

    from app.logging_config import get_logger
    log = get_logger(__name__)
    log.info("user_login", user_id=42)

Context (request id, tenant, etc.) is added automatically when set via
``bind_request_context`` from the request middleware.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import random
import re
import socket
import sys
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

import structlog

# ---------------------------------------------------------------------------
# Context vars
# ---------------------------------------------------------------------------
# These are set by HTTP middleware and read by structlog processors. They
# are also picked up by the stdlib ``logging`` filter so that *any*
# library logging through stdlib (uvicorn, sqlalchemy, httpx, ...) gets
# the same context fields.

_request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)
_trace_id_var: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)
_span_id_var: ContextVar[Optional[str]] = ContextVar("span_id", default=None)
_tenant_id_var: ContextVar[Optional[str]] = ContextVar("tenant_id", default=None)
_api_key_id_var: ContextVar[Optional[str]] = ContextVar("api_key_id", default=None)
_client_ip_var: ContextVar[Optional[str]] = ContextVar("client_ip", default=None)
_user_id_var: ContextVar[Optional[str]] = ContextVar("user_id", default=None)


def bind_request_context(
    *,
    request_id: Optional[str] = None,
    trace_id: Optional[str] = None,
    span_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
    api_key_id: Optional[str] = None,
    client_ip: Optional[str] = None,
    user_id: Optional[str] = None,
) -> None:
    """Set the per-request log context. Safe to call multiple times."""
    if request_id is not None:
        _request_id_var.set(request_id)
    if trace_id is not None:
        _trace_id_var.set(trace_id)
    if span_id is not None:
        _span_id_var.set(span_id)
    if tenant_id is not None:
        _tenant_id_var.set(tenant_id)
    if api_key_id is not None:
        _api_key_id_var.set(api_key_id)
    if client_ip is not None:
        _client_ip_var.set(client_ip)
    if user_id is not None:
        _user_id_var.set(user_id)


def clear_request_context() -> None:
    for var in (
        _request_id_var,
        _trace_id_var,
        _span_id_var,
        _tenant_id_var,
        _api_key_id_var,
        _client_ip_var,
        _user_id_var,
    ):
        var.set(None)


def get_request_context() -> Dict[str, Any]:
    """Return the current request context as a flat dict (for emitters)."""
    out: Dict[str, Any] = {}
    if (rid := _request_id_var.get()) is not None:
        out["request_id"] = rid
    if (tid := _trace_id_var.get()) is not None:
        out["trace.id"] = tid
    if (sid := _span_id_var.get()) is not None:
        out["span.id"] = sid
    if (tnt := _tenant_id_var.get()) is not None:
        out["tenant_id"] = tnt
    if (akid := _api_key_id_var.get()) is not None:
        out["api_key_id"] = akid
    if (ip := _client_ip_var.get()) is not None:
        out["client.ip"] = ip
    if (uid := _user_id_var.get()) is not None:
        out["user.id"] = uid
    return out


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


DEFAULT_REDACT_KEYS: Set[str] = {
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "x-api-key",
    "authorization",
    "auth",
    "access_token",
    "refresh_token",
    "client_secret",
    "session",
    "cookie",
    "set-cookie",
}

DEFAULT_REDACT_PATTERNS: List[str] = [
    # Bearer tokens
    r"(?i)bearer\s+[A-Za-z0-9._\-+/=]{8,}",
    # Generic "sk-..." API keys
    r"\bsk-[A-Za-z0-9]{16,}\b",
    # JWTs (xxx.yyy.zzz)
    r"\beyJ[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\.[A-Za-z0-9_\-]{5,}\b",
    # Credit-card-like 13–19 digit runs (loose)
    r"\b(?:\d[ -]*?){13,19}\b",
]

REDACTED = "***REDACTED***"


@dataclass
class LoggingConfig:
    """Runtime configuration for the logging stack."""

    level: str = "INFO"
    format: str = "json"  # "json" | "text" | "auto"
    service_name: str = "agentanywhere-gateway"
    service_version: str = "0.9.1"
    environment: str = "production"
    redact_keys: Set[str] = field(default_factory=lambda: set(DEFAULT_REDACT_KEYS))
    redact_patterns: List[str] = field(default_factory=lambda: list(DEFAULT_REDACT_PATTERNS))
    redaction_enabled: bool = True
    sample_rate: float = 1.0  # 0.0 - 1.0; 1.0 = log everything
    sample_always_levels: Set[str] = field(
        default_factory=lambda: {"WARNING", "ERROR", "CRITICAL"}
    )
    sample_always_status_ge: int = 400  # never sample-out access logs >= this
    logger_levels: Dict[str, str] = field(default_factory=dict)
    file_path: Optional[str] = None  # if set, also write to file (rotating)
    file_max_bytes: int = 100 * 1024 * 1024
    file_backup_count: int = 10
    audit_file_path: Optional[str] = None
    include_caller: bool = False
    include_process_info: bool = True
    include_hostname: bool = True
    timestamp_format: str = "iso"  # "iso" | "epoch"

    def resolve_format(self) -> str:
        if self.format == "auto":
            return "text" if sys.stdout.isatty() else "json"
        return self.format


# Global mutable state — runtime tweaks apply here.
_config = LoggingConfig()


def get_config() -> LoggingConfig:
    return _config


def update_config(**kwargs: Any) -> LoggingConfig:
    """Mutate runtime config. Pass any ``LoggingConfig`` field name."""
    cfg = _config
    for key, value in kwargs.items():
        if not hasattr(cfg, key):
            raise ValueError(f"Unknown logging config field: {key}")
        setattr(cfg, key, value)
    apply_logger_levels()
    return cfg


# ---------------------------------------------------------------------------
# Processors
# ---------------------------------------------------------------------------


def _add_service_metadata(_, __, event_dict: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _config
    event_dict.setdefault("service.name", cfg.service_name)
    event_dict.setdefault("service.version", cfg.service_version)
    event_dict.setdefault("env", cfg.environment)
    if cfg.include_hostname:
        event_dict.setdefault("host.name", _hostname())
    if cfg.include_process_info:
        event_dict.setdefault("process.pid", os.getpid())
    return event_dict


def _add_request_context(_, __, event_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Inject request-scoped context vars into every log line."""
    for k, v in get_request_context().items():
        event_dict.setdefault(k, v)
    return event_dict


def _normalize_event_key(_, __, event_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Move structlog's `event` to `message` (ECS) and keep a copy."""
    if "event" in event_dict:
        # Keep both keys: `event` is a stable code-side identifier (snake_case),
        # `message` is human-readable. structlog sets `event` first.
        message = event_dict.get("message")
        if not message:
            event_dict["message"] = event_dict["event"]
    return event_dict


def _add_timestamp(_, __, event_dict: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _config
    if cfg.timestamp_format == "epoch":
        event_dict["@timestamp"] = time.time()
    else:
        # ISO 8601 with millisecond precision and 'Z' suffix.
        ts = time.gmtime()
        ms = int((time.time() % 1) * 1000)
        event_dict["@timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S", ts) + f".{ms:03d}Z"
    event_dict.pop("timestamp", None)
    return event_dict


_compiled_patterns_cache: Tuple[List[str], List[re.Pattern]] = ([], [])


def _get_compiled_patterns() -> List[re.Pattern]:
    global _compiled_patterns_cache
    cached_patterns, cached_compiled = _compiled_patterns_cache
    if cached_patterns is _config.redact_patterns:
        return cached_compiled
    compiled = []
    for pat in _config.redact_patterns:
        try:
            compiled.append(re.compile(pat))
        except re.error:
            continue
    _compiled_patterns_cache = (_config.redact_patterns, compiled)
    return compiled


# A UUID (hyphenated, or 32 hex chars) standing as a whole token. Request ids are
# uuid4s; the loose card pattern would otherwise eat their long digit-only runs
# and break log correlation.
_UUID_TOKEN_RE = re.compile(
    r"(?<![0-9A-Za-z-])(?:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    r"|[0-9a-fA-F]{32})(?![0-9A-Za-z-])"
)


def _redact_string(value: str) -> str:
    patterns = _get_compiled_patterns()
    kept: List[str] = []

    def _stash(m: "re.Match[str]") -> str:
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"

    protected = _UUID_TOKEN_RE.sub(_stash, value) if len(value) >= 32 else value
    for rx in patterns:
        protected = rx.sub(REDACTED, protected)
    if kept:
        protected = re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))] if int(m.group(1)) < len(kept) else m.group(0), protected)
    return protected


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Mapping):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _redact_mapping(mapping: Mapping[str, Any]) -> Dict[str, Any]:
    cfg = _config
    keys_lower = {k.lower() for k in cfg.redact_keys}
    out: Dict[str, Any] = {}
    for k, v in mapping.items():
        if str(k).lower() in keys_lower:
            out[k] = REDACTED
        else:
            out[k] = _redact_value(v)
    return out


def _redact_processor(_, __, event_dict: Dict[str, Any]) -> Dict[str, Any]:
    if not _config.redaction_enabled:
        return event_dict
    return _redact_mapping(event_dict)


def _sampling_processor(_, method_name: str, event_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Drop events when sampling is enabled and they don't survive the dice roll.

    Always keeps:
      * any record at WARNING/ERROR/CRITICAL,
      * any record carrying ``status_code`` >= ``sample_always_status_ge``,
      * any record on the ``audit`` logger.
    """
    cfg = _config
    if cfg.sample_rate >= 1.0:
        return event_dict

    level = (event_dict.get("level") or method_name).upper()
    if level in cfg.sample_always_levels:
        return event_dict
    if event_dict.get("logger") == "audit":
        return event_dict
    status_code = event_dict.get("status_code")
    if isinstance(status_code, int) and status_code >= cfg.sample_always_status_ge:
        return event_dict
    if random.random() <= cfg.sample_rate:
        return event_dict
    raise structlog.DropEvent


def _ensure_level(_, method_name: str, event_dict: Dict[str, Any]) -> Dict[str, Any]:
    event_dict.setdefault("level", method_name.upper())
    return event_dict


_HOSTNAME: Optional[str] = None


def _hostname() -> str:
    global _HOSTNAME
    if _HOSTNAME is None:
        try:
            _HOSTNAME = socket.gethostname()
        except Exception:
            _HOSTNAME = "unknown"
    return _HOSTNAME


# ---------------------------------------------------------------------------
# Stdlib bridge
# ---------------------------------------------------------------------------


class _ContextFilter(logging.Filter):
    """Adds request-context fields to every stdlib log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        for k, v in get_request_context().items():
            setattr(record, k.replace(".", "_"), v)
        return True


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def _shared_processors(include_sampling: bool) -> List[structlog.types.Processor]:
    """Processors shared between native structlog logs and foreign (stdlib) logs.

    The ``include_sampling`` flag controls whether the
    :func:`_sampling_processor` is included. Sampling is only enabled
    for native structlog calls because the stdlib ``ProcessorFormatter``
    does not handle ``structlog.DropEvent`` cleanly when raised from
    inside ``foreign_pre_chain``.
    """
    cfg = _config
    pre: List[structlog.types.Processor] = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _ensure_level,
        _add_timestamp,
        _add_service_metadata,
        _add_request_context,
    ]
    if cfg.include_caller:
        pre.append(
            structlog.processors.CallsiteParameterAdder(
                parameters={
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.LINENO,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                }
            )
        )
    pre.extend(
        [
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            _normalize_event_key,
            _redact_processor,
        ]
    )
    if include_sampling:
        pre.append(_sampling_processor)
    return pre


def _build_processors() -> List[structlog.types.Processor]:
    """Processors for native structlog calls — terminate with the
    ``ProcessorFormatter.wrap_for_formatter`` shim so the stdlib
    ``ProcessorFormatter`` does the final rendering."""
    return [
        structlog.stdlib.filter_by_level,
        *_shared_processors(include_sampling=True),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ]


def _final_renderer() -> structlog.types.Processor:
    if _config.resolve_format() == "json":
        return structlog.processors.JSONRenderer(sort_keys=False)
    return structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())


def _make_stdlib_formatter() -> logging.Formatter:
    """A ``ProcessorFormatter`` that runs the same processor chain for
    foreign (non-structlog) loggers."""
    return structlog.stdlib.ProcessorFormatter(
        processor=_final_renderer(),
        foreign_pre_chain=_shared_processors(include_sampling=False),
    )


def _make_stdlib_handler() -> logging.Handler:
    handler: logging.Handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_make_stdlib_formatter())
    handler.addFilter(_ContextFilter())
    return handler


def _make_file_handler(path: str) -> logging.Handler:
    cfg = _config
    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=cfg.file_max_bytes,
        backupCount=cfg.file_backup_count,
        encoding="utf-8",
    )
    # Always JSON for files regardless of stdout format.
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(sort_keys=False),
            foreign_pre_chain=_shared_processors(include_sampling=False),
        )
    )
    handler.addFilter(_ContextFilter())
    return handler


def apply_logger_levels() -> None:
    """Apply per-logger level overrides from the runtime config."""
    cfg = _config
    for name, level in cfg.logger_levels.items():
        logging.getLogger(name).setLevel(_level_for(level))


def _level_for(level: str) -> int:
    return getattr(logging, level.upper(), logging.INFO)


VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
VALID_FORMATS = frozenset({"json", "text", "auto"})
VALID_TIMESTAMP_FORMATS = frozenset({"iso", "epoch"})


def parse_level_overrides(raw: Optional[str]) -> Tuple[Dict[str, str], List[str]]:
    """Parse ``"uvicorn.access=WARNING,httpx=ERROR"`` into ``({logger: LEVEL}, problems)``."""
    levels: Dict[str, str] = {}
    problems: List[str] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, level = item.partition("=")
        name, level = name.strip(), level.strip().upper()
        if not sep or not name or level not in VALID_LEVELS:
            problems.append(f"LOG_LEVEL_OVERRIDES entry {item!r} (expected logger=LEVEL)")
            continue
        levels[name] = level
    return levels, problems


def _csv(raw: Optional[str]) -> List[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _apply_settings(cfg: "LoggingConfig", settings: Any) -> List[str]:
    """Copy logging settings onto ``cfg``. Returns human-readable problems, never raises."""
    problems: List[str] = []

    level = str(settings.log_level or cfg.level).upper()
    if level not in VALID_LEVELS:
        problems.append(f"LOG_LEVEL {settings.log_level!r} is not one of {sorted(VALID_LEVELS)}; using INFO")
        level = "INFO"
    cfg.level = level

    fmt = str(settings.log_format or cfg.format).lower()
    if fmt not in VALID_FORMATS:
        problems.append(f"LOG_FORMAT {settings.log_format!r} is not one of {sorted(VALID_FORMATS)}; using json")
        fmt = "json"
    cfg.format = fmt

    cfg.service_name = settings.app_name or cfg.service_name
    cfg.service_version = settings.app_version or cfg.service_version
    cfg.environment = settings.environment or cfg.environment
    cfg.sample_rate = min(1.0, max(0.0, float(getattr(settings, "log_sampling_rate", cfg.sample_rate))))
    cfg.redaction_enabled = bool(getattr(settings, "log_redaction_enabled", cfg.redaction_enabled))
    cfg.include_caller = bool(getattr(settings, "log_include_caller", cfg.include_caller))
    cfg.include_hostname = bool(getattr(settings, "log_include_hostname", cfg.include_hostname))
    cfg.include_process_info = bool(getattr(settings, "log_include_process_info", cfg.include_process_info))
    cfg.sample_always_status_ge = int(
        getattr(settings, "log_sample_always_status_ge", cfg.sample_always_status_ge)
    )
    cfg.file_max_bytes = max(1024, int(getattr(settings, "log_file_max_bytes", cfg.file_max_bytes)))
    cfg.file_backup_count = max(0, int(getattr(settings, "log_file_backup_count", cfg.file_backup_count)))

    ts = str(getattr(settings, "log_timestamp_format", cfg.timestamp_format)).lower()
    if ts not in VALID_TIMESTAMP_FORMATS:
        problems.append(f"LOG_TIMESTAMP_FORMAT {ts!r} is not one of {sorted(VALID_TIMESTAMP_FORMATS)}; using iso")
        ts = "iso"
    cfg.timestamp_format = ts

    file_path = getattr(settings, "log_file_path", None)
    if file_path:
        cfg.file_path = file_path
    audit_file = getattr(settings, "log_audit_file_path", None)
    if audit_file:
        cfg.audit_file_path = audit_file

    overrides, bad = parse_level_overrides(getattr(settings, "log_level_overrides", ""))
    cfg.logger_levels.update(overrides)
    problems += bad

    cfg.redact_keys |= {k.lower() for k in _csv(getattr(settings, "log_redact_keys", ""))}
    for pattern in _csv(getattr(settings, "log_redact_patterns", "")):
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"LOG_REDACT_PATTERNS entry {pattern!r} is not a valid regex ({exc}); ignored")
            continue
        if pattern not in cfg.redact_patterns:
            cfg.redact_patterns = [*cfg.redact_patterns, pattern]
    return problems


_initialised = False


def setup_logging(
    config_overrides: Optional[Dict[str, Any]] = None,
    refresh_from_settings: Optional[bool] = None,
) -> structlog.stdlib.BoundLogger:
    """Initialise the logging stack.

    On the *first* call, defaults are pulled from
    ``app.config.get_settings()`` (so ``LOG_LEVEL`` etc. are honoured).
    On subsequent calls we keep whatever runtime values
    ``update_config`` has set, unless ``refresh_from_settings=True`` is
    explicitly passed.

    Safe to call multiple times — handlers are replaced each time.
    """
    global _initialised
    cfg = _config

    # Decide whether to (re)load defaults from app.config.
    if refresh_from_settings is None:
        refresh_from_settings = not _initialised

    problems: List[str] = []
    if refresh_from_settings:
        try:
            from app.config import get_settings  # local to avoid import cycles

            problems = _apply_settings(cfg, get_settings())
        except Exception as exc:
            problems = [f"could not read logging settings ({type(exc).__name__}: {exc}); using defaults"]

    if config_overrides:
        for key, value in config_overrides.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)

    # Reset stdlib root to avoid duplicate handlers on re-entry.
    root = logging.getLogger()
    root.setLevel(_level_for(cfg.level))
    root.handlers = []
    root.addHandler(_make_stdlib_handler())
    if cfg.file_path:
        try:
            root.addHandler(_make_file_handler(cfg.file_path))
        except Exception:
            pass

    # Audit log channel.
    audit = logging.getLogger("audit")
    audit.handlers = []
    audit.propagate = False
    audit.setLevel(logging.INFO)
    audit_handler = (
        _make_file_handler(cfg.audit_file_path) if cfg.audit_file_path else _make_stdlib_handler()
    )
    audit.addHandler(audit_handler)

    # Apply per-logger overrides (e.g. uvicorn.access -> WARNING)
    apply_logger_levels()

    structlog.configure(
        processors=_build_processors(),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,  # so config changes apply
    )

    _initialised = True
    log = structlog.get_logger("gateway")
    for problem in problems:
        log.warning("logging_config_invalid", detail=problem)
    return log


def get_logger(name: Optional[str] = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger, initialising the stack if needed."""
    if not _initialised:
        setup_logging()
    return structlog.get_logger(name) if name else structlog.get_logger()


def get_audit_logger() -> structlog.stdlib.BoundLogger:
    """Dedicated audit channel — bypasses sampling and redaction-of-message."""
    if not _initialised:
        setup_logging()
    return structlog.get_logger("audit")


# ---------------------------------------------------------------------------
# Trace context (W3C)
# ---------------------------------------------------------------------------


_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-"
    r"(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<span_id>[0-9a-f]{16})-"
    r"(?P<flags>[0-9a-f]{2})$"
)


def parse_traceparent(value: Optional[str]) -> Optional[Tuple[str, str, str]]:
    """Return ``(trace_id, parent_span_id, flags)`` or ``None``."""
    if not value:
        return None
    m = _TRACEPARENT_RE.match(value.strip())
    if not m:
        return None
    return m.group("trace_id"), m.group("span_id"), m.group("flags")


def new_trace_context() -> Tuple[str, str, str]:
    """Generate a fresh ``(trace_id, span_id, flags)`` triple."""
    trace_id = "%032x" % random.getrandbits(128)
    span_id = "%016x" % random.getrandbits(64)
    return trace_id, span_id, "01"  # sampled flag set


def format_traceparent(trace_id: str, span_id: str, flags: str = "01") -> str:
    return f"00-{trace_id}-{span_id}-{flags}"


__all__ = [
    "LoggingConfig",
    "bind_request_context",
    "clear_request_context",
    "get_request_context",
    "get_audit_logger",
    "get_config",
    "get_logger",
    "format_traceparent",
    "new_trace_context",
    "parse_traceparent",
    "setup_logging",
    "update_config",
]
