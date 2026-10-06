"""
Sample HTTP/REST Backend Server

This is a simple REST API server that demonstrates HTTP/REST integration.
"""

from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Dict, Any, Optional, List
import uvicorn
import structlog
from datetime import datetime, timezone

logger = structlog.get_logger()
app = FastAPI(title="Sample HTTP Backend", version="1.0.0")


class User(BaseModel):
    """User model."""
    id: Optional[str] = None
    name: str
    email: str
    metadata: Optional[Dict[str, Any]] = None


# Simple in-memory database
users_db: Dict[str, User] = {}
users_counter = 0


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "service": "http-backend"}


@app.post("/graphql")
async def graphql_endpoint(request: Request):
    """GraphQL endpoint for testing."""
    try:
        data = await request.json()
        query = data.get("query", "")
        
        # Simple GraphQL response
        if "hello" in query.lower():
            return {
                "data": {
                    "hello": "Hello from GraphQL backend!"
                }
            }
        elif "__typename" in query:
            return {
                "data": {
                    "__typename": "Query"
                }
            }
        else:
            return {
                "data": {},
                "errors": [{"message": "Unknown query"}]
            }
    except Exception as e:
        return {
            "errors": [{"message": str(e)}]
        }


@app.get("/api/users", response_model=List[Dict[str, Any]])
async def list_users(skip: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
    """List all users."""
    users_list = list(users_db.values())
    return [
        {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "metadata": user.metadata
        }
        for user in users_list[skip:skip + limit]
    ]


@app.get("/api/users/{user_id}", response_model=Dict[str, Any])
async def get_user(user_id: str):
    """Get a specific user."""
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    
    user = users_db[user_id]
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "metadata": user.metadata
    }


@app.post("/api/users", response_model=Dict[str, Any])
async def create_user(user: User, request: Request):
    """Create a new user."""
    global users_counter
    users_counter += 1
    
    user_id = user.id or f"user_{users_counter}"
    user.id = user_id
    
    users_db[user_id] = user
    
    logger.info("user_created", user_id=user_id, name=user.name)
    
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "metadata": user.metadata,
        "created_at": datetime.now(timezone.utc).isoformat()
    }


@app.put("/api/users/{user_id}", response_model=Dict[str, Any])
async def update_user(user_id: str, user: User):
    """Update a user."""
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.id = user_id
    users_db[user_id] = user
    
    logger.info("user_updated", user_id=user_id)
    
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "metadata": user.metadata,
        "updated_at": datetime.now(timezone.utc).isoformat()
    }


@app.delete("/api/users/{user_id}")
async def delete_user(user_id: str):
    """Delete a user."""
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    
    del users_db[user_id]
    logger.info("user_deleted", user_id=user_id)
    
    return {"success": True, "user_id": user_id, "deleted_at": datetime.now(timezone.utc).isoformat()}


@app.post("/api/users/{user_id}/action")
async def user_action(user_id: str, action: Dict[str, Any], request: Request):
    """Perform an action on a user (generic endpoint)."""
    if user_id not in users_db:
        raise HTTPException(status_code=404, detail="User not found")
    
    action_type = action.get("action", "unknown")
    logger.info("user_action", user_id=user_id, action=action_type)
    
    return {
        "success": True,
        "user_id": user_id,
        "action": action_type,
        "result": f"Action '{action_type}' performed on user {user_id}",
        "timestamp": datetime.now(timezone.utc).isoformat()
    }


if __name__ == "__main__":
    logger.info("starting_http_backend", port=8003)
    uvicorn.run(app, host="0.0.0.0", port=8003)

