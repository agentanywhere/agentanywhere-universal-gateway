"""Synthetic multi-protocol agent: hits discovery + Tier 1 + Tier 2 through the app.

Upstream HTTP is mocked so no real backends are required; routing still resolves
AG-UI/ACP backends from settings. This complements finer-grained unit tests.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

import app.main as main_mod
import app.routing as routing_mod


@pytest.mark.asyncio
async def test_synthetic_agent_tier1_and_tier2_smoke(
    client, db_session, monkeypatch, sample_api_key
):
    api_key, _ = sample_api_key
    hdr = {main_mod.settings.api_key_header: api_key}

    monkeypatch.setattr(routing_mod.settings, "ag_ui_backend_url", "http://fake-agui:9")
    monkeypatch.setattr(routing_mod.settings, "acp_backend_url", "http://fake-acp:8")

    class _FakeStream:
        status_code = 200
        headers = httpx.Headers({"content-type": "text/event-stream"})

        async def aiter_bytes(self):
            yield b'data: {"ok":true}\n\n'

        async def aclose(self):
            return None

    mock_route = AsyncMock(
        return_value=httpx.Response(200, json={"proxied": True}),
    )
    mock_stream = AsyncMock(return_value=_FakeStream())
    monkeypatch.setattr(main_mod.router, "route_request", mock_route)
    monkeypatch.setattr(main_mod.router, "start_upstream_stream", mock_stream)

    r_card = client.get("/.well-known/agent-card.json")
    assert r_card.status_code == 200
    assert isinstance(r_card.json(), dict)

    r_agui_meta = client.get("/.well-known/ag-ui.json")
    assert r_agui_meta.status_code == 200
    assert r_agui_meta.json().get("name") == "AG-UI"

    r_mcp = client.post(
        "/mcp/sess",
        headers={**hdr, "content-type": "application/json"},
        content=json.dumps({"jsonrpc": "2.0", "method": "ping", "id": 1}),
    )
    assert r_mcp.status_code == 200

    a2a_body = {"message": {"role": "user", "parts": [{"text": "hello"}]}}
    r_a2a = client.post(
        "/a2a/message:send",
        headers={**hdr, "content-type": "application/json"},
        content=json.dumps(a2a_body),
    )
    assert r_a2a.status_code == 200

    assert mock_route.await_count >= 2

    r_stream_agui = client.get(
        "/agui/thread-1",
        headers={**hdr, "accept": "text/event-stream"},
    )
    assert r_stream_agui.status_code == 200
    ct = r_stream_agui.headers.get("content-type", "")
    assert "text/event-stream" in ct
    assert b"ok" in r_stream_agui.content

    acp_body = {
        "agent_name": "chat",
        "input": [
            {"role": "user", "parts": [{"content_type": "text/plain", "content": "hi"}]}
        ],
    }
    r_stream_acp = client.post(
        "/acp/runs",
        headers={
            **hdr,
            "content-type": "application/json",
            "accept": "text/event-stream",
        },
        content=json.dumps(acp_body),
    )
    assert r_stream_acp.status_code == 200
    assert b"ok" in r_stream_acp.content

    assert mock_stream.await_count >= 2
