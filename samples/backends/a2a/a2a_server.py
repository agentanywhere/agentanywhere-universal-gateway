"""
Sample A2A (Agent-to-Agent) Backend Server

This is a simple A2A server that demonstrates agent-to-agent communication.
"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional, List
import uvicorn
import structlog
from datetime import datetime, timezone
import asyncio

logger = structlog.get_logger()
app = FastAPI(title="Sample A2A Backend", version="1.0.0")


class A2AMessage(BaseModel):
    """A2A message request model."""
    message: str
    target_agent: str
    source_agent: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class A2AResponse(BaseModel):
    """A2A response model."""
    response: str
    agent_id: str
    timestamp: str
    metadata: Optional[Dict[str, Any]] = None


# Agent registry and message queue
agent_registry: Dict[str, Dict[str, Any]] = {}
message_queue: List[Dict[str, Any]] = []


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "service": "a2a-backend"}


@app.post("/a2a/message", response_model=A2AResponse)
async def handle_a2a_message(message: A2AMessage, request: Request):
    """Handle A2A message requests."""
    logger.info(
        "a2a_message_received",
        source=message.source_agent,
        target=message.target_agent,
        message=message.message[:50]
    )
    
    # Register source agent if not exists
    if message.source_agent and message.source_agent not in agent_registry:
        agent_registry[message.source_agent] = {
            "id": message.source_agent,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "message_count": 0
        }
    
    # Register target agent if not exists
    if message.target_agent not in agent_registry:
        agent_registry[message.target_agent] = {
            "id": message.target_agent,
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "message_count": 0
        }
    
    # Update message counts
    if message.source_agent:
        agent_registry[message.source_agent]["message_count"] += 1
    agent_registry[message.target_agent]["message_count"] += 1
    
    # Queue message for target agent
    queued_message = {
        "id": f"msg_{len(message_queue)}",
        "source": message.source_agent or "unknown",
        "target": message.target_agent,
        "message": message.message,
        "metadata": message.metadata or {},
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    message_queue.append(queued_message)
    
    # Generate response (simulate agent processing)
    response_text = f"A2A Response from {message.target_agent}: Received '{message.message}'"
    
    if message.metadata:
        priority = message.metadata.get("priority", "normal")
        response_text += f" [Priority: {priority}]"
    
    return A2AResponse(
        response=response_text,
        agent_id=message.target_agent,
        timestamp=datetime.now(timezone.utc).isoformat(),
        metadata={
            "message_id": queued_message["id"],
            "queued": True,
            "queue_length": len(message_queue)
        }
    )


@app.get("/a2a/agents")
async def list_agents():
    """List all registered agents."""
    return {
        "agents": list(agent_registry.values()),
        "total": len(agent_registry)
    }


@app.get("/a2a/queue/{agent_id}")
async def get_agent_queue(agent_id: str):
    """Get queued messages for an agent."""
    agent_messages = [msg for msg in message_queue if msg["target"] == agent_id]
    return {
        "agent_id": agent_id,
        "messages": agent_messages,
        "count": len(agent_messages)
    }


@app.delete("/a2a/queue/{agent_id}/{message_id}")
async def dequeue_message(agent_id: str, message_id: str):
    """Remove a message from the queue."""
    global message_queue
    original_count = len(message_queue)
    message_queue = [msg for msg in message_queue if not (msg["target"] == agent_id and msg["id"] == message_id)]
    
    if len(message_queue) < original_count:
        return {"success": True, "message_id": message_id, "removed": True}
    return {"success": False, "message_id": message_id, "removed": False}


if __name__ == "__main__":
    logger.info("starting_a2a_backend", port=8002)
    uvicorn.run(app, host="0.0.0.0", port=8002)


