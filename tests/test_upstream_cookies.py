"""The gateway's shared upstream clients must not carry one caller's cookies to another."""
import httpx
import pytest

from app.adapters import build_upstream_client
from app.config import get_settings
from app.protocols.graphql_handler import GraphQLHandler


def _backend(seen):
    """A backend that sets a session cookie on every answer and notes the Cookie header it was sent."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, json={"data": {}}, headers=[("set-cookie", "session=alice; Path=/"), ("set-cookie", "csrf=1")])

    return httpx.MockTransport(handler)


async def _through(client: httpx.AsyncClient, seen):
    client._transport = _backend(seen)
    first = await client.get("http://backend.test/login")
    await client.get("http://backend.test/profile")
    await client.get("http://backend.test/profile", headers={"Cookie": "session=bob"})
    return first


@pytest.mark.asyncio
async def test_a_cookie_a_backend_sets_is_not_sent_for_the_next_caller():
    seen = []
    client = build_upstream_client(get_settings())
    try:
        first = await _through(client, seen)
    finally:
        await client.aclose()
    # The next caller's request carries no cookie; a caller's own Cookie header goes as it came.
    assert seen == [None, None, "session=bob"]
    # The caller who was answered still gets the backend's Set-Cookie headers.
    assert first.headers.get_list("set-cookie") == ["session=alice; Path=/", "csrf=1"]
    assert len(client.cookies.jar) == 0


@pytest.mark.asyncio
async def test_the_graphql_client_keeps_no_cookies_either():
    seen = []
    handler = GraphQLHandler("http://backend.test")
    try:
        await _through(handler.client, seen)
    finally:
        await handler.client.aclose()
    assert seen == [None, None, "session=bob"]


@pytest.mark.asyncio
async def test_an_ordinary_client_would_have_leaked_it():
    """What the fix prevents, so this test fails loudly if httpx ever changes underneath it."""
    seen = []
    async with httpx.AsyncClient() as client:
        await _through(client, seen)
    assert seen[1] is not None and "session=alice" in seen[1]
