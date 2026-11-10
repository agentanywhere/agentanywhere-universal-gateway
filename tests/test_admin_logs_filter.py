"""/admin/logs filters by level in the query, so pages are full and the total is right."""
import pytest

from app.db_models import RequestLogModel

AUTH = {"Authorization": "Bearer test-only-secret-key-not-a-placeholder-32b"}


@pytest.fixture
async def seeded(db_session):
    rows = [(200, "/api/ok")] * 30 + [(404, "/api/missing")] * 12 + [(500, "/api/boom")] * 7
    for i, (code, path) in enumerate(rows):
        db_session.add(RequestLogModel(request_id=f"r{i}", method="GET", path=path, client_ip="10.0.0.1",
                                       status_code=code, response_time_ms=3, protocol="http"))
    await db_session.commit()


def test_error_level_pages_are_full_and_total_counts_all_matches(client, seeded):
    first = client.get("/admin/logs?level=error&limit=5", headers=AUTH).json()
    assert first["total"] == 7 and len(first["logs"]) == 5
    assert all(r["status_code"] >= 500 for r in first["logs"])
    second = client.get("/admin/logs?level=error&limit=5&offset=5", headers=AUTH).json()
    assert second["total"] == 7 and len(second["logs"]) == 2


def test_warning_level_is_4xx_only(client, seeded):
    body = client.get("/admin/logs?level=warning&limit=50", headers=AUTH).json()
    assert body["total"] == 12 and {r["status_code"] for r in body["logs"]} == {404}


def test_no_level_still_returns_everything_and_other_filters_combine(client, seeded):
    assert client.get("/admin/logs?limit=100", headers=AUTH).json()["total"] == 49
    both = client.get("/admin/logs?level=error&path=boom", headers=AUTH).json()
    assert both["total"] == 7
    assert client.get("/admin/logs?status_code=404", headers=AUTH).json()["total"] == 12


def test_limit_is_capped_and_negative_values_are_clamped(client, seeded):
    assert len(client.get("/admin/logs?limit=100000", headers=AUTH).json()["logs"]) == 49
    assert client.get("/admin/logs?limit=0&offset=-5", headers=AUTH).status_code == 200
