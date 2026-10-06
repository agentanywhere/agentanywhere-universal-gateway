"""Tests for the world-class logging stack."""
import io
import json
import logging
import re

import pytest
import structlog

from app.logging_config import (
    DEFAULT_REDACT_KEYS,
    LoggingConfig,
    bind_request_context,
    clear_request_context,
    format_traceparent,
    get_config,
    get_request_context,
    new_trace_context,
    parse_traceparent,
    setup_logging,
    update_config,
)


@pytest.fixture(autouse=True)
def _reset_logging_context():
    clear_request_context()
    # Reset to known state.
    update_config(level="INFO", format="json", sample_rate=1.0, redaction_enabled=True)
    yield
    clear_request_context()


def _capture_log() -> io.StringIO:
    """Replace the root handler's stream with a buffer and return it."""
    buf = io.StringIO()
    setup_logging()
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.stream = buf
    audit = logging.getLogger("audit")
    for handler in audit.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.stream = buf
    return buf


def _last_log_dict(buf: io.StringIO) -> dict:
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert lines, "no log lines emitted"
    return json.loads(lines[-1])


# ---------------------------------------------------------------------------
# Trace context
# ---------------------------------------------------------------------------


class TestTraceContext:
    def test_parse_traceparent_valid(self):
        result = parse_traceparent(
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        )
        assert result == (
            "0af7651916cd43dd8448eb211c80319c",
            "b7ad6b7169203331",
            "01",
        )

    def test_parse_traceparent_invalid(self):
        assert parse_traceparent("garbage") is None
        assert parse_traceparent(None) is None
        assert parse_traceparent("") is None

    def test_new_trace_context_shape(self):
        trace_id, span_id, flags = new_trace_context()
        assert re.fullmatch(r"[0-9a-f]{32}", trace_id)
        assert re.fullmatch(r"[0-9a-f]{16}", span_id)
        assert flags == "01"

    def test_format_traceparent(self):
        s = format_traceparent("a" * 32, "b" * 16, "01")
        assert s == f"00-{'a' * 32}-{'b' * 16}-01"


# ---------------------------------------------------------------------------
# Context vars
# ---------------------------------------------------------------------------


class TestRequestContext:
    def test_bind_and_get_context(self):
        bind_request_context(request_id="r1", trace_id="t1", tenant_id="acme")
        ctx = get_request_context()
        assert ctx == {
            "request_id": "r1",
            "trace.id": "t1",
            "tenant_id": "acme",
        }

    def test_clear_context(self):
        bind_request_context(request_id="r1")
        clear_request_context()
        assert get_request_context() == {}

    def test_log_emits_context(self):
        buf = _capture_log()
        bind_request_context(request_id="r1", trace_id="abc", api_key_id="key-7")
        log = structlog.get_logger("test")
        log.info("hello")
        last = _last_log_dict(buf)
        assert last["request_id"] == "r1"
        assert last["trace.id"] == "abc"
        assert last["api_key_id"] == "key-7"


# ---------------------------------------------------------------------------
# Schema / metadata
# ---------------------------------------------------------------------------


class TestLogSchema:
    def test_schema_keys_present(self):
        buf = _capture_log()
        log = structlog.get_logger("test")
        log.info("hello")
        last = _last_log_dict(buf)
        for key in (
            "@timestamp",
            "level",
            "service.name",
            "service.version",
            "env",
            "host.name",
            "process.pid",
            "event",
            "message",
        ):
            assert key in last, f"missing key {key} in {last}"

    def test_event_and_message(self):
        buf = _capture_log()
        log = structlog.get_logger("test")
        log.info("user_login")
        last = _last_log_dict(buf)
        assert last["event"] == "user_login"
        # `message` defaults to event when not explicitly set.
        assert last["message"] == "user_login"

    def test_explicit_message_preserved(self):
        buf = _capture_log()
        log = structlog.get_logger("test")
        log.info("user_login", message="User has logged in")
        last = _last_log_dict(buf)
        assert last["event"] == "user_login"
        assert last["message"] == "User has logged in"


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_default_keys_redacted(self):
        buf = _capture_log()
        log = structlog.get_logger("test")
        log.info("auth", api_key="sk-secret", password="hunter2", normal="visible")
        last = _last_log_dict(buf)
        assert last["api_key"] == "***REDACTED***"
        assert last["password"] == "***REDACTED***"
        assert last["normal"] == "visible"

    def test_bearer_token_pattern_redacted(self):
        buf = _capture_log()
        log = structlog.get_logger("test")
        log.info("inbound", header="Bearer eyJabcdefghIJK.payload.sig")
        last = _last_log_dict(buf)
        assert "***REDACTED***" in last["header"]

    def test_disable_redaction(self):
        update_config(redaction_enabled=False)
        buf = _capture_log()
        log = structlog.get_logger("test")
        log.info("auth", api_key="sk-secret")
        last = _last_log_dict(buf)
        assert last["api_key"] == "sk-secret"

    def test_nested_dict_redacted(self):
        buf = _capture_log()
        log = structlog.get_logger("test")
        log.info("auth", payload={"password": "x", "username": "alice"})
        last = _last_log_dict(buf)
        assert last["payload"]["password"] == "***REDACTED***"
        assert last["payload"]["username"] == "alice"


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


class TestSampling:
    def test_warning_always_kept(self):
        update_config(sample_rate=0.0)
        buf = _capture_log()
        log = structlog.get_logger("test")
        log.info("noise")
        log.warning("important")
        # sample_rate=0 drops INFO but keeps WARNING.
        lines = [
            line
            for line in buf.getvalue().splitlines()
            if line.strip()
        ]
        assert any("important" in l for l in lines)
        assert not any("noise" in l for l in lines)

    def test_high_status_kept(self):
        update_config(sample_rate=0.0)
        buf = _capture_log()
        log = structlog.get_logger("test")
        log.info("http_access", status_code=500)
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        assert any("http_access" in l for l in lines)


# ---------------------------------------------------------------------------
# Per-logger overrides
# ---------------------------------------------------------------------------


class TestLoggerOverrides:
    def test_logger_levels_apply(self):
        update_config(logger_levels={"chatty": "ERROR"})
        setup_logging()
        chatty = logging.getLogger("chatty")
        assert chatty.level == logging.ERROR

    def test_invalid_field_raises(self):
        with pytest.raises(ValueError):
            update_config(does_not_exist=42)


# ---------------------------------------------------------------------------
# Foreign loggers
# ---------------------------------------------------------------------------


class TestForeignLogs:
    def test_uvicorn_log_picks_up_context(self):
        buf = _capture_log()
        bind_request_context(request_id="r-7", trace_id="t-7")
        logging.getLogger("uvicorn.access").info("GET /foo 200")
        last = _last_log_dict(buf)
        assert last["request_id"] == "r-7"
        assert last["trace.id"] == "t-7"
        assert "GET /foo 200" in last["event"] or "GET /foo 200" in last["message"]
