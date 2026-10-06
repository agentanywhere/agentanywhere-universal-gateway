"""Tests for the live agent monitor (traffic stats + health snapshot + SSE auth)."""
import pytest

from app.agent_monitor import AgentMonitor
from app.agent_routes import AgentEdgeConfig

# Was "test-admin-" + "x"*40, which authenticated only because any token over
# 20 characters was accepted.
from app.config import get_settings

TOKEN = get_settings().secret_key
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_record_request_and_traffic_stats():
    mon = AgentMonitor()
    for _ in range(8):
        mon.record_request("a1", 200, 12.0)
    mon.record_request("a1", 500, 40.0)  # one error

    cfg = AgentEdgeConfig(routes={"a1": "http://up:1"})
    snap = mon.snapshot(cfg)
    assert snap["summary"]["total"] == 1
    agent = snap["agents"][0]
    assert agent["slug"] == "a1"
    assert agent["total_requests"] == 9
    assert agent["req_per_min"] == 9
    assert agent["error_pct"] == pytest.approx(11.1, abs=0.2)
    assert agent["avg_latency_ms"] is not None


def test_snapshot_includes_live_routes_without_traffic():
    mon = AgentMonitor()
    cfg = AgentEdgeConfig(routes={"quiet": "http://up:2"})
    snap = mon.snapshot(cfg)
    slugs = [a["slug"] for a in snap["agents"]]
    assert "quiet" in slugs
    quiet = snap["agents"][0]
    assert quiet["status"] == "unknown"
    assert quiet["req_per_min"] == 0


@pytest.mark.asyncio
async def test_probe_marks_unreachable_down():
    mon = AgentMonitor()
    # Reserved TEST-NET-1 address / bogus port → connection fails fast.
    cfg = AgentEdgeConfig(routes={"dead": "http://192.0.2.1:9"})
    await mon.probe_once(cfg)
    snap = mon.snapshot(cfg)
    dead = next(a for a in snap["agents"] if a["slug"] == "dead")
    assert dead["status"] == "down"
    assert dead["consecutive_failures"] >= 1
    assert snap["summary"]["down"] == 1


def test_health_endpoint_requires_auth(client):
    assert client.get("/admin/agents/health").status_code == 401


def test_health_endpoint_returns_board(client):
    resp = client.get("/admin/agents/health", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert "summary" in body and "agents" in body


def test_stream_rejects_bad_token(client):
    # Stream auth is via query param; a short/invalid token must be rejected.
    assert client.get("/admin/agents/stream?token=nope").status_code == 401
