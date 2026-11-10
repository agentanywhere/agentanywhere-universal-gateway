"""A minimal upstream for benchmarks: a raw ASGI app that answers every request with the
same 11-byte JSON body. It does almost no work, so any latency you measure through the
gateway beyond a direct call to this backend is the gateway's. Run it with several workers
so it stays well above the gateway's ceiling::

    uvicorn --app-dir perf/fleet bench_backend:app --host 0.0.0.0 --port 8003 --workers 3

Set BENCH_DELAY_MS to add a fixed think time.
"""
import asyncio
import os

_DELAY = float(os.getenv("BENCH_DELAY_MS", "0")) / 1000.0
_BODY = b'{"ok":true}'
_HEADERS = [(b"content-type", b"application/json"), (b"content-length", str(len(_BODY)).encode())]


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    if scope["type"] != "http":
        return
    while True:                                   # read the whole request body
        message = await receive()
        if not message.get("more_body"):
            break
    if _DELAY:
        await asyncio.sleep(_DELAY)
    await send({"type": "http.response.start", "status": 200, "headers": _HEADERS})
    await send({"type": "http.response.body", "body": _BODY})
