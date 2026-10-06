"""
Sample AG-UI (Agent-User Interaction Protocol) Backend Server

AG-UI is transport-agnostic; this sample speaks the common HTTP + Server-Sent
Events flavour: a client POSTs a run request (``messages`` / ``threadId`` /
``runId``) and the server streams back AG-UI events
(``RUN_STARTED`` -> ``TEXT_MESSAGE_START`` -> ``TEXT_MESSAGE_CONTENT`` ... ->
``TEXT_MESSAGE_END`` -> ``RUN_FINISHED``) as ``text/event-stream``.

Through the gateway the ``/agui`` prefix is stripped, so ``POST /agui`` is
forwarded here as ``POST /``. Direct callers can also use ``/run`` or ``/awp``.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = structlog.get_logger()
app = FastAPI(title="Sample AG-UI Backend", version="1.0.0")


class RunAgentInput(BaseModel):
    """Minimal AG-UI run input envelope (all fields optional)."""

    messages: List[Dict[str, Any]] = []
    threadId: Optional[str] = None
    runId: Optional[str] = None
    state: Optional[Dict[str, Any]] = None
    tools: Optional[List[Dict[str, Any]]] = None


def _sse(event: Dict[str, Any]) -> bytes:
    """Encode a single AG-UI event as an SSE frame."""
    return f"data: {json.dumps(event)}\n\n".encode()


def _last_user_message(messages: List[Dict[str, Any]]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


async def _run_event_stream(inp: RunAgentInput):
    thread_id = inp.threadId or f"thread_{uuid.uuid4().hex[:8]}"
    run_id = inp.runId or f"run_{uuid.uuid4().hex[:8]}"
    message_id = f"msg_{uuid.uuid4().hex[:8]}"

    user_text = _last_user_message(inp.messages)
    reply = f"You said: {user_text}" if user_text else "Hello from the sample AG-UI agent!"

    yield _sse({"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id})
    yield _sse({"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"})
    for token in reply.split(" "):
        await asyncio.sleep(0.05)  # simulate token-by-token generation
        yield _sse(
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": token + " "}
        )
    yield _sse({"type": "TEXT_MESSAGE_END", "messageId": message_id})
    yield _sse({"type": "RUN_FINISHED", "threadId": thread_id, "runId": run_id})


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "service": "ag-ui-backend"}


async def _handle_run(request: Request) -> StreamingResponse:
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    inp = RunAgentInput(**payload) if isinstance(payload, dict) else RunAgentInput()
    logger.info(
        "ag_ui_run",
        thread=inp.threadId,
        run=inp.runId,
        messages=len(inp.messages),
    )
    return StreamingResponse(_run_event_stream(inp), media_type="text/event-stream")


# The gateway strips `/agui`, forwarding to `/`. `/run` and `/awp` are aliases
# for direct (non-gateway) callers.
@app.post("/")
async def run_root(request: Request):
    return await _handle_run(request)


@app.post("/run")
async def run(request: Request):
    return await _handle_run(request)


@app.post("/awp")
async def awp(request: Request):
    return await _handle_run(request)


if __name__ == "__main__":
    logger.info("starting_ag_ui_backend", port=8004)
    uvicorn.run(app, host="0.0.0.0", port=8004)
