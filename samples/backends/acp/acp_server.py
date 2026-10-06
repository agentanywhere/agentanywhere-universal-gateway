"""
Sample ACP (Agent Communication Protocol) Backend Server

Implements the minimal BeeAI / IBM ACP REST surface the gateway understands:

  GET  /agents                 -> list available agents
  POST /runs                   -> create a run (requires ``agent_name`` + ``input`` list)
  GET  /runs/{run_id}          -> fetch a run
  POST /runs/{run_id}          -> resume a run (requires ``await_resume``)
  POST /runs/{run_id}/cancel   -> cancel a run

Through the gateway the ``/acp`` prefix is stripped, so ``POST /acp/runs``
arrives here as ``POST /runs`` (spec-native paths).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = structlog.get_logger()
app = FastAPI(title="Sample ACP Backend", version="1.0.0")

AGENTS = [
    {"name": "echo", "description": "Echoes the input message back."},
    {"name": "greeter", "description": "Greets the user by echoing their text."},
]

# In-memory run store (sample only).
RUNS: Dict[str, Dict[str, Any]] = {}


class RunCreateRequest(BaseModel):
    agent_name: str
    input: List[Dict[str, Any]]
    session_id: Optional[str] = None


class RunResumeRequest(BaseModel):
    await_resume: Dict[str, Any]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text_of(messages: List[Dict[str, Any]]) -> str:
    """Extract text from ACP messages ({role, parts:[{content}]} or {content})."""
    for m in messages:
        parts = m.get("parts")
        if parts:
            for p in parts:
                if p.get("content"):
                    return str(p["content"])
        if m.get("content"):
            return str(m["content"])
    return ""


def _make_output(agent_name: str, text: str) -> List[Dict[str, Any]]:
    if agent_name == "greeter":
        reply = f"Hello! You said: {text}" if text else "Hello!"
    else:  # echo (default)
        reply = text or ""
    return [
        {
            "role": f"agent/{agent_name}",
            "parts": [{"content_type": "text/plain", "content": reply}],
        }
    ]


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "service": "acp-backend"}


@app.get("/agents")
async def list_agents():
    """List available agents (ACP discovery)."""
    return {"agents": AGENTS}


@app.post("/runs")
async def create_run(req: RunCreateRequest):
    """Create (and, in this sample, synchronously complete) a run."""
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    text = _text_of(req.input)
    run = {
        "run_id": run_id,
        "agent_name": req.agent_name,
        "session_id": req.session_id,
        "status": "completed",
        "output": _make_output(req.agent_name, text),
        "created_at": _now(),
        "finished_at": _now(),
    }
    RUNS[run_id] = run
    logger.info("acp_run_created", run_id=run_id, agent=req.agent_name)
    return run


@app.get("/runs/{run_id}")
async def get_run(run_id: str):
    """Fetch a run by id."""
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    return run


@app.post("/runs/{run_id}")
async def resume_run(run_id: str, req: RunResumeRequest):
    """Resume a run that was awaiting input."""
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    run["status"] = "completed"
    run["output"] = _make_output(run["agent_name"], "resumed: " + json.dumps(req.await_resume))
    run["finished_at"] = _now()
    logger.info("acp_run_resumed", run_id=run_id)
    return run


@app.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """Cancel a run."""
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="run not found")
    run["status"] = "cancelled"
    run["finished_at"] = _now()
    logger.info("acp_run_cancelled", run_id=run_id)
    return run


if __name__ == "__main__":
    logger.info("starting_acp_backend", port=8005)
    uvicorn.run(app, host="0.0.0.0", port=8005)
