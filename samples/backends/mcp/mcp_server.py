"""
Sample MCP (Model Context Protocol) Backend Server

This is a simple MCP server that demonstrates how to integrate with the gateway.
"""

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional
import uvicorn
import structlog

logger = structlog.get_logger()
app = FastAPI(title="Sample MCP Backend", version="1.0.0")


class MCPQuery(BaseModel):
    """MCP query request model."""
    query: str
    context: Optional[Dict[str, Any]] = None


class MCPResponse(BaseModel):
    """MCP response model."""
    response: str
    context: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None


# Simple in-memory context store
context_store: Dict[str, Any] = {}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "service": "mcp-backend"}


@app.post("/mcp/query", response_model=MCPResponse)
async def handle_mcp_query(query: MCPQuery, request: Request):
    """Handle MCP query requests."""
    logger.info("mcp_query_received", query=query.query, context=query.context)
    
    # Extract user context
    user_id = None
    if query.context:
        user_id = query.context.get("user_id")
        # Store context for this user
        if user_id:
            context_store[user_id] = query.context
    
    # Simple echo response (replace with actual MCP logic)
    response_text = f"MCP Response: {query.query}"
    
    # Add context-aware response
    if user_id and user_id in context_store:
        stored_context = context_store[user_id]
        response_text += f" (User: {user_id}, Context: {stored_context})"
    
    return MCPResponse(
        response=response_text,
        context=query.context or {},
        metadata={
            "processed_at": "2025-12-03T10:00:00Z",
            "user_id": user_id,
            "query_length": len(query.query)
        }
    )


@app.get("/mcp/context/{user_id}")
async def get_context(user_id: str):
    """Get stored context for a user."""
    if user_id in context_store:
        return {"user_id": user_id, "context": context_store[user_id]}
    return {"user_id": user_id, "context": None}


@app.post("/mcp/context/{user_id}")
async def update_context(user_id: str, context: Dict[str, Any]):
    """Update context for a user."""
    context_store[user_id] = context
    return {"user_id": user_id, "context": context, "updated": True}


if __name__ == "__main__":
    logger.info("starting_mcp_backend", port=8001)
    uvicorn.run(app, host="0.0.0.0", port=8001)


