"""Behaviour tests for settings profiles: admin endpoints and the repository."""
import pytest

from app.config import get_settings
from app.models import ProtocolSettings, SettingsProfile
from app.repositories.settings_profile_repo import SettingsProfileRepository as Repo

AUTH = {"Authorization": f"Bearer {get_settings().secret_key}"}


class _NoSessionLocal:
    """Stand-in for app.database.AsyncSessionLocal. The access-log middleware persists
    rejected/handled requests through it as a fire-and-forget task; the real factory
    writes ./gateway.db, and a factory sharing the test connection races the test's own
    queries. Failing fast makes the middleware skip persistence (it swallows the error)."""

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        raise RuntimeError("request-log persistence disabled in tests")

    async def __aexit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _isolated_request_log_db(db_session, monkeypatch):
    """Keep the access-log middleware off the on-disk gateway.db."""
    import app.database as database
    monkeypatch.setattr(database, "AsyncSessionLocal", _NoSessionLocal())


def _msg(resp):
    body = resp.json()
    return body.get("message") or body.get("detail") or ""


def _profile(name="prod", **kw):
    body = {
        "name": name,
        "description": "d",
        "protocol_settings": {"http": {"rate_limit_per_minute": 5, "timeout": 9}},
        "global_settings": {"region": "in"},
    }
    body.update(kw)
    return body


# ----------------------------------------------------------------- admin API
def test_profile_endpoints_require_admin_token(client):
    for method, path in [
        ("get", "/admin/settings/profiles"),
        ("get", "/admin/settings/profiles/x"),
        ("delete", "/admin/settings/profiles/x"),
        ("get", "/admin/settings/protocols"),
    ]:
        assert getattr(client, method)(path).status_code == 401
        assert getattr(client, method)(path, headers={"Authorization": "Bearer no"}).status_code == 401


def test_create_get_list_roundtrip(client):
    r = client.post("/admin/settings/profiles", json=_profile(), headers=AUTH)
    assert r.status_code == 200
    created = r.json()
    assert created["name"] == "prod"
    assert created["is_default"] is False
    assert created["id"]
    assert created["created_at"] is not None
    # protocol settings are stored fully expanded with model defaults
    http = created["protocol_settings"]["http"]
    assert http["rate_limit_per_minute"] == 5 and http["timeout"] == 9
    assert http["retry_max_attempts"] == 3
    assert created["global_settings"] == {"region": "in"}

    got = client.get(f"/admin/settings/profiles/{created['id']}", headers=AUTH)
    assert got.status_code == 200 and got.json() == created

    listing = client.get("/admin/settings/profiles", headers=AUTH).json()
    assert [p["id"] for p in listing] == [created["id"]]


def test_get_unknown_404(client):
    r = client.get("/admin/settings/profiles/missing", headers=AUTH)
    assert r.status_code == 404
    assert "not found" in _msg(r).lower()


def test_create_validation_422(client):
    assert client.post("/admin/settings/profiles", json={}, headers=AUTH).status_code == 422
    bad = _profile(protocol_settings={"http": {"timeout": "soon"}})
    assert client.post("/admin/settings/profiles", json=bad, headers=AUTH).status_code == 422
    assert client.get("/admin/settings/profiles", headers=AUTH).json() == []


def test_creating_default_demotes_previous_default(client):
    a = client.post("/admin/settings/profiles", json=_profile("a", is_default=True), headers=AUTH).json()
    b = client.post("/admin/settings/profiles", json=_profile("b", is_default=True), headers=AUTH).json()
    assert b["is_default"] is True
    after = {p["name"]: p for p in client.get("/admin/settings/profiles", headers=AUTH).json()}
    assert after["a"]["is_default"] is False
    assert after["b"]["is_default"] is True
    assert a["id"] != b["id"]
    # default sorts first
    assert client.get("/admin/settings/profiles", headers=AUTH).json()[0]["name"] == "b"


def test_update_changes_fields_and_keeps_id(client):
    pid = client.post("/admin/settings/profiles", json=_profile(), headers=AUTH).json()["id"]
    r = client.put(
        f"/admin/settings/profiles/{pid}",
        json=_profile("prod-renamed", description="new", global_settings={"x": 1}),
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == pid
    assert body["name"] == "prod-renamed"
    assert body["description"] == "new"
    assert body["global_settings"] == {"x": 1}
    assert body["updated_at"] is not None
    assert client.get(f"/admin/settings/profiles/{pid}", headers=AUTH).json()["name"] == "prod-renamed"


def test_update_same_name_allowed(client):
    pid = client.post("/admin/settings/profiles", json=_profile(), headers=AUTH).json()["id"]
    r = client.put(f"/admin/settings/profiles/{pid}", json=_profile(description="again"), headers=AUTH)
    assert r.status_code == 200 and r.json()["description"] == "again"


def test_update_unknown_404(client):
    r = client.put("/admin/settings/profiles/ghost", json=_profile(), headers=AUTH)
    assert r.status_code == 404


def test_update_to_taken_name_400(client):
    client.post("/admin/settings/profiles", json=_profile("taken"), headers=AUTH)
    pid = client.post("/admin/settings/profiles", json=_profile("mine"), headers=AUTH).json()["id"]
    r = client.put(f"/admin/settings/profiles/{pid}", json=_profile("taken"), headers=AUTH)
    assert r.status_code == 400
    assert "already exists" in _msg(r)
    assert client.get(f"/admin/settings/profiles/{pid}", headers=AUTH).json()["name"] == "mine"


def test_update_with_default_flag_demotes_others(client):
    a = client.post("/admin/settings/profiles", json=_profile("a", is_default=True), headers=AUTH).json()
    b = client.post("/admin/settings/profiles", json=_profile("b"), headers=AUTH).json()
    r = client.put(f"/admin/settings/profiles/{b['id']}", json=_profile("b", is_default=True), headers=AUTH)
    assert r.status_code == 200 and r.json()["is_default"] is True
    assert client.get(f"/admin/settings/profiles/{a['id']}", headers=AUTH).json()["is_default"] is False


def test_delete_then_gone_and_second_delete_404(client):
    pid = client.post("/admin/settings/profiles", json=_profile(), headers=AUTH).json()["id"]
    r = client.delete(f"/admin/settings/profiles/{pid}", headers=AUTH)
    assert r.status_code == 200 and r.json() == {"message": "Profile deleted"}
    assert client.get(f"/admin/settings/profiles/{pid}", headers=AUTH).status_code == 404
    assert client.delete(f"/admin/settings/profiles/{pid}", headers=AUTH).status_code == 404


def test_set_default_switches_and_404s(client):
    a = client.post("/admin/settings/profiles", json=_profile("a", is_default=True), headers=AUTH).json()
    b = client.post("/admin/settings/profiles", json=_profile("b"), headers=AUTH).json()
    r = client.post(f"/admin/settings/profiles/{b['id']}/set-default", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == {"message": "Profile set as default", "profile_id": b["id"]}
    flags = {p["name"]: p["is_default"] for p in client.get("/admin/settings/profiles", headers=AUTH).json()}
    assert flags == {"a": False, "b": True}
    assert a["id"]
    assert client.post("/admin/settings/profiles/nope/set-default", headers=AUTH).status_code == 404


def test_set_default_unknown_id_keeps_existing_default(client):
    b = client.post("/admin/settings/profiles", json=_profile("b", is_default=True), headers=AUTH).json()
    assert client.post("/admin/settings/profiles/nope/set-default", headers=AUTH).status_code == 404
    got = client.get(f"/admin/settings/profiles/{b['id']}", headers=AUTH).json()
    assert got["is_default"] is True


def test_supported_protocols_lists_defaults(client):
    r = client.get("/admin/settings/protocols", headers=AUTH)
    assert r.status_code == 200
    by_type = {p["type"]: p for p in r.json()}
    assert set(by_type) == {"mcp", "a2a", "http", "mqtt", "websocket", "grpc"}
    s = get_settings()
    assert by_type["mcp"]["default_settings"]["rate_limit_per_minute"] == s.protocol_mcp_rate_limit_per_minute
    assert by_type["mqtt"]["default_settings"]["qos_level"] == 1
    assert by_type["websocket"]["default_settings"]["circuit_breaker_enabled"] is False
    assert by_type["http"]["default_settings"]["compression_enabled"] is True


# ----------------------------------------------------------------- repository
def _model(name, **kw):
    return SettingsProfile(name=name, **kw)


@pytest.mark.asyncio
async def test_repo_create_assigns_id_and_serialises_protocol_settings(db_session):
    row = await Repo.create(
        db_session,
        _model("p", protocol_settings={"http": ProtocolSettings(timeout=11)}),
        created_by="alice",
    )
    assert row.id and row.created_by == "alice"
    assert row.protocol_settings["http"]["timeout"] == 11
    assert isinstance(row.protocol_settings["http"], dict)


@pytest.mark.asyncio
async def test_repo_create_honours_explicit_id_and_raw_dict_settings(db_session):
    row = await Repo.create(
        db_session,
        SettingsProfile.model_construct(
            id="fixed-id", name="raw", description=None,
            protocol_settings={"http": {"timeout": 3}}, global_settings={}, is_default=False,
        ),
    )
    assert row.id == "fixed-id"
    assert row.protocol_settings == {"http": {"timeout": 3}}


@pytest.mark.asyncio
async def test_repo_lookups(db_session):
    a = await Repo.create(db_session, _model("alpha"))
    assert (await Repo.get_by_id(db_session, a.id)).name == "alpha"
    assert (await Repo.get_by_name(db_session, "alpha")).id == a.id
    assert await Repo.get_by_id(db_session, "nope") is None
    assert await Repo.get_by_name(db_session, "nope") is None
    assert await Repo.get_default(db_session) is None


@pytest.mark.asyncio
async def test_repo_default_is_unique_and_ordered_first(db_session):
    a = await Repo.create(db_session, _model("a", is_default=True))
    b = await Repo.create(db_session, _model("b"))
    assert (await Repo.get_default(db_session)).id == a.id

    await Repo.set_default(db_session, b.id)
    assert (await Repo.get_default(db_session)).id == b.id
    rows = await Repo.list_all(db_session)
    assert [r.name for r in rows] == ["b", "a"]
    assert [r.is_default for r in rows] == [True, False]


@pytest.mark.asyncio
async def test_repo_set_default_unknown_id_returns_none(db_session):
    assert await Repo.set_default(db_session, "ghost") is None


@pytest.mark.asyncio
async def test_repo_update_replaces_values_and_touches_updated_at(db_session):
    row = await Repo.create(db_session, _model("old", description="x"))
    before = row.updated_at
    out = await Repo.update(
        db_session,
        row.id,
        _model("new", description="y", protocol_settings={"grpc": ProtocolSettings(timeout=2)},
               global_settings={"k": "v"}, is_default=True),
    )
    assert out.name == "new" and out.description == "y"
    assert out.protocol_settings["grpc"]["timeout"] == 2
    assert out.global_settings == {"k": "v"}
    assert out.is_default is True
    assert out.updated_at >= before


@pytest.mark.asyncio
async def test_repo_update_unknown_returns_none(db_session):
    assert await Repo.update(db_session, "ghost", _model("x")) is None


@pytest.mark.asyncio
async def test_repo_delete(db_session):
    row = await Repo.create(db_session, _model("gone"))
    assert await Repo.delete(db_session, row.id) is True
    assert await Repo.delete(db_session, row.id) is False
    assert await Repo.list_all(db_session) == []


@pytest.mark.asyncio
async def test_repo_unset_all_defaults_respects_exclusion(db_session):
    a = await Repo.create(db_session, _model("a", is_default=True))
    await Repo.unset_all_defaults(db_session, exclude_id=a.id)
    assert (await Repo.get_default(db_session)).id == a.id
    await Repo.unset_all_defaults(db_session)
    assert await Repo.get_default(db_session) is None
