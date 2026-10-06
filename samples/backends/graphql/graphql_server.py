"""
Sample GraphQL Backend Server

Serves ``POST /graphql`` with a tiny but real schema (``hello``, ``users``,
``user(id)``) built on ``graphql-core``, so the gateway's GraphQL handler can
both execute queries and run schema introspection.

Through the gateway, ``POST /graphql`` (with ``{query, variables,
operationName}``) is forwarded here unchanged.
"""
from __future__ import annotations

from typing import Any, Dict

import structlog
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from graphql import (
    GraphQLArgument,
    GraphQLField,
    GraphQLID,
    GraphQLList,
    GraphQLNonNull,
    GraphQLObjectType,
    GraphQLSchema,
    GraphQLString,
    graphql,
)

logger = structlog.get_logger()
app = FastAPI(title="Sample GraphQL Backend", version="1.0.0")

USERS: Dict[str, Dict[str, str]] = {
    "1": {"id": "1", "name": "Ada Lovelace", "email": "ada@example.com"},
    "2": {"id": "2", "name": "Alan Turing", "email": "alan@example.com"},
}

UserType = GraphQLObjectType(
    "User",
    {
        "id": GraphQLField(GraphQLNonNull(GraphQLID)),
        "name": GraphQLField(GraphQLString),
        "email": GraphQLField(GraphQLString),
    },
)

QueryType = GraphQLObjectType(
    "Query",
    {
        "hello": GraphQLField(
            GraphQLString,
            resolve=lambda *_: "Hello from the sample GraphQL backend!",
        ),
        "users": GraphQLField(
            GraphQLList(UserType),
            resolve=lambda *_: list(USERS.values()),
        ),
        "user": GraphQLField(
            UserType,
            args={"id": GraphQLArgument(GraphQLNonNull(GraphQLID))},
            resolve=lambda _root, _info, id: USERS.get(id),
        ),
    },
)

schema = GraphQLSchema(query=QueryType)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "service": "graphql-backend"}


@app.post("/graphql")
async def graphql_endpoint(request: Request):
    """Execute a GraphQL query/mutation (also serves introspection)."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    query = body.get("query")
    if not query:
        return JSONResponse(
            status_code=400,
            content={"errors": [{"message": "A 'query' field is required"}]},
        )

    result = await graphql(
        schema,
        query,
        variable_values=body.get("variables"),
        operation_name=body.get("operationName"),
    )

    out: Dict[str, Any] = {}
    if result.data is not None:
        out["data"] = result.data
    if result.errors:
        out["errors"] = [{"message": str(err)} for err in result.errors]
    return JSONResponse(content=out)


if __name__ == "__main__":
    logger.info("starting_graphql_backend", port=8006)
    uvicorn.run(app, host="0.0.0.0", port=8006)
