"""Tier-2 protocol helpers (AG-UI, ACP streaming detection, validation)."""
import json

import pytest
from fastapi import Request

from app.exceptions import ValidationError
from app.tier2_acp import validate_acp_http_payload
from app.tier2_ag_ui import validate_ag_ui_http_payload
from app.tier2_streaming import should_proxy_streaming_http


def _req(method: str, path: str, *, accept: str = "", query: str = ""):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [],
        "query_string": query.encode(),
    }
    if accept:
        scope["headers"].extend(
            [(b"accept", accept.encode())]
        )
    return Request(scope)


def test_well_known_ag_ui_public(client):
    r = client.get("/.well-known/ag-ui.json")
    assert r.status_code == 200
    assert r.json().get("name") == "AG-UI"


def test_should_proxy_streaming_a2a():
    r = _req("POST", "/a2a/message:stream", accept="text/event-stream")
    assert should_proxy_streaming_http(r, "/a2a/message:stream") is True


def test_should_proxy_streaming_ag_ui():
    r = _req("GET", "/agui/thread", accept="text/event-stream")
    assert should_proxy_streaming_http(r, "/agui/thread") is True


def test_should_proxy_streaming_acp_runs():
    r = _req(
        "POST",
        "/acp/runs",
        accept="text/event-stream",
    )
    assert should_proxy_streaming_http(r, "/acp/runs") is True


def test_acp_create_validation():
    body = json.dumps(
        {"agent_name": "chat", "input": [{"role": "user", "parts": [{"content_type": "text/plain", "content": "hi"}]}]}
    ).encode()
    validate_acp_http_payload(
        method="POST",
        path="/acp/runs",
        raw_body=body,
        request_id="1",
    )


def test_acp_create_validation_missing_agent():
    body = json.dumps({"input": [{"role": "user", "parts": [{"content_type": "text/plain", "content": "hi"}]}]}).encode()
    with pytest.raises(ValidationError):
        validate_acp_http_payload(method="POST", path="/api/acp/runs", raw_body=body, request_id="1")


def test_ag_ui_messages_must_be_list():
    body = json.dumps({"messages": "bad"}).encode()
    with pytest.raises(ValidationError):
        validate_ag_ui_http_payload(method="POST", path="/agui/run", raw_body=body, request_id="1")
