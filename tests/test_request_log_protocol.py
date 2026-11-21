"""The request log's protocol column is filled, not left NULL."""
from sqlalchemy import select

from app.db_models import RequestLogModel
from app.models import RequestLog
from app.repositories.log_repo import LogRepository


def _log(**kw):
    return RequestLog(request_id="r1", method="GET", path="/mcp/x", client_ip="10.0.0.1",
                      status_code=200, response_time_ms=3.0, **kw)


def test_the_protocol_can_be_set_after_construction():
    entry = _log()
    assert entry.protocol is None
    entry.protocol = "mcp"  # what the request middleware does
    assert entry.protocol == "mcp"


async def test_create_stores_the_protocol(db_session):
    await LogRepository.create(db_session, _log(protocol="a2a"))
    row = (await db_session.execute(select(RequestLogModel))).scalars().one()
    assert row.protocol == "a2a"


async def test_create_many_stores_the_protocol(db_session):
    await LogRepository.create_many(db_session, [(_log(protocol="graphql"), None), (_log(), None)])
    rows = {r.protocol for r in (await db_session.execute(select(RequestLogModel))).scalars().all()}
    assert rows == {"graphql", None}
