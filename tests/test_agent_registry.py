"""Tests for the DB-backed agent-edge registry (repository + provider + hot-swap)."""
from types import SimpleNamespace

import pytest

from app import agent_registry
from app.agent_routes import AgentEdgeConfig
from app.repositories.agent_registry_repo import AgentRegistryRepository


# --------------------------------------------------------------------------- validation
@pytest.mark.parametrize(
    "slug,ok",
    [
        ("orders-agent", True),
        ("a1", True),
        ("Orders-Agent", False),
        ("-bad", False),
        ("bad-", False),
        ("bad--slug", False),
        ("x", False),
        ("has_underscore", False),
    ],
)
def test_validate_slug(slug, ok):
    assert (agent_registry.validate_slug(slug) is None) == ok


@pytest.mark.parametrize(
    "url,ok",
    [
        ("http://10.0.0.10:8090", True),
        ("https://x.example.com", True),
        ("ftp://nope", False),
        ("", False),
        ("10.0.0.10:8090", False),
    ],
)
def test_validate_upstream(url, ok):
    assert (agent_registry.validate_upstream(url) is None) == ok


# --------------------------------------------------------------------------- hot-swap
def test_config_replace_and_resolve():
    cfg = AgentEdgeConfig(routes={}, public_paths={})
    assert not cfg.has_routes()

    cfg.replace(
        routes={"orders-agent": "http://up:8090/"},
        public_paths={"orders-agent": ["/invite"]},
        audience_by_slug={"orders-agent": "custom-aud"},
    )
    assert cfg.has_routes()

    route = cfg.resolve("/agents/orders-agent/api/health")
    assert route is not None
    assert route.upstream_base == "http://up:8090"  # trailing slash trimmed
    assert route.upstream_path == "/api/health"
    assert cfg.audience_for("orders-agent") == "custom-aud"

    # Unknown slug does not resolve.
    assert cfg.resolve("/agents/ghost/x") is None

    snap = cfg.snapshot()
    assert snap["routes"] == {"orders-agent": "http://up:8090"}
    assert snap["public_paths"] == {"orders-agent": ["/invite"]}


# --------------------------------------------------------------------------- repository
@pytest.mark.asyncio
async def test_repository_crud(db_session):
    row = await AgentRegistryRepository.create(
        db_session,
        {
            "slug": "support-agent",
            "upstream_base": "http://10.0.0.10:8091/",
            "public_paths": ["invite", "/api/v1/invites"],
        },
    )
    assert row.upstream_base == "http://10.0.0.10:8091"  # normalised
    assert row.public_paths == ["/invite", "/api/v1/invites"]  # leading slash added

    fetched = await AgentRegistryRepository.get(db_session, "support-agent")
    assert fetched is not None
    assert await AgentRegistryRepository.count(db_session) == 1

    updated = await AgentRegistryRepository.update(
        db_session, "support-agent", {"enabled": False, "audience": "aud-x"}
    )
    assert updated.enabled is False
    d = AgentRegistryRepository.to_dict(updated)
    assert d["effective_audience"] == "aud-x"

    assert await AgentRegistryRepository.delete(db_session, "support-agent") is True
    assert await AgentRegistryRepository.count(db_session) == 0


# --------------------------------------------------------------------------- provider
@pytest.mark.asyncio
async def test_seed_from_env_then_load(db_session):
    settings = SimpleNamespace(
        agent_routes_json='{"orders-agent": "http://up:8090", "hidden-one": "http://up:9000"}',
        agent_public_paths_json='{"orders-agent": ["/invite"]}',
    )
    seeded = await agent_registry.seed_from_env_if_empty(db_session, settings)
    assert seeded == 2

    # Idempotent: a second call is a no-op because the table is now populated.
    assert await agent_registry.seed_from_env_if_empty(db_session, settings) == 0

    cfg = AgentEdgeConfig(routes={}, public_paths={})
    count = await agent_registry.load_into(cfg, db_session)
    assert count == 2
    assert cfg.resolve("/agents/orders-agent/") is not None
    assert cfg.snapshot()["public_paths"]["orders-agent"] == ["/invite"]


@pytest.mark.asyncio
async def test_disabled_agent_not_routable(db_session):
    await AgentRegistryRepository.create(
        db_session, {"slug": "off-agent", "upstream_base": "http://up:1", "enabled": False}
    )
    cfg = AgentEdgeConfig(routes={}, public_paths={})
    count = await agent_registry.load_into(cfg, db_session)
    assert count == 0
    assert cfg.resolve("/agents/off-agent/x") is None
