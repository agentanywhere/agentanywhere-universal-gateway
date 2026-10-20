# Sample Backends

This directory contains small sample servers, one per protocol, for trying
AgentAnywhere Universal Gateway. They have no authentication and keep data in
memory; use them for testing only. The
[getting-started tutorial](../../docs/tutorials/getting-started.md) uses the
HTTP sample, and the [end-to-end tutorial](../../docs/tutorials/two-backends-end-to-end.md)
adds the MCP sample.

Things to know:

- The MCP sample is a REST service with an MCP-like `/mcp/query` endpoint, not
  a full MCP server.
- The gateway cannot pass traffic to the WebSocket and gRPC samples; see the
  [protocol support matrix](../../docs/explanation/protocol-support.md).
- `env.example` sets `GRAPHQL_BACKEND_URL` to port 4000; the GraphQL sample
  below listens on 8006.
- The repository's root `docker-compose.yml` has its own copies of some of
  these services under `--profile backends`; this directory's
  `docker-compose.yml` is separate.

## Quick Start

### Start All Backends

```bash
# Using Docker Compose (recommended)
cd samples/backends
docker compose up -d

# Or start individually
python samples/backends/mcp/mcp_server.py &
python samples/backends/a2a/a2a_server.py &
python samples/backends/http/http_server.py &
python samples/backends/websocket/websocket_server.py &
python samples/backends/agui/agui_server.py &
python samples/backends/acp/acp_server.py &
python samples/backends/graphql/graphql_server.py &
```

## Available Backends

### MCP Backend (Port 8001)

**Start:**
```bash
python samples/backends/mcp/mcp_server.py
```

**Test:**
```bash
curl -X POST http://localhost:8001/mcp/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Hello MCP", "context": {"user_id": "user123"}}'
```

**Features:**
- Query handling
- Context storage per user
- Health check endpoint

### A2A Backend (Port 8002)

**Start:**
```bash
python samples/backends/a2a/a2a_server.py
```

**Test:**
```bash
curl -X POST http://localhost:8002/a2a/message \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello", "target_agent": "agent_b", "source_agent": "agent_a"}'
```

**Features:**
- Agent-to-agent messaging
- Agent registry
- Message queue
- Health check endpoint

### HTTP/REST Backend (Port 8003)

**Start:**
```bash
python samples/backends/http/http_server.py
```

**Test:**
```bash
# Create user
curl -X POST http://localhost:8003/api/users \
  -H "Content-Type: application/json" \
  -d '{"name": "John Doe", "email": "john@example.com"}'

# List users
curl http://localhost:8003/api/users

# Get user
curl http://localhost:8003/api/users/user_1
```

**Features:**
- CRUD operations for users
- RESTful API
- Health check endpoint

### WebSocket Backend (Port 8080)

**Start:**
```bash
python samples/backends/websocket/websocket_server.py
```

**Test:**
```javascript
// In browser console or Node.js
const ws = new WebSocket('ws://localhost:8080/ws');
ws.onmessage = (event) => console.log('Received:', event.data);
ws.send(JSON.stringify({message: 'Hello WebSocket'}));
```

**Features:**
- Real-time bidirectional communication
- Connection management
- Broadcasting
- Health check endpoint

### MQTT Broker

**Setup:**
```bash
bash samples/backends/mqtt/mqtt_broker_setup.sh
mosquitto -c mqtt_config/mosquitto.conf
```

**Test:**
```bash
# Subscribe
mosquitto_sub -h localhost -t test/topic

# Publish
mosquitto_pub -h localhost -t test/topic -m "Hello MQTT"
```

### gRPC Backend (Port 50051)

**Note:** Requires protobuf compilation. See the files in `samples/backends/grpc/` (`greeter.proto`, `grpc_server.py`, `requirements.txt`) for setup.

### AG-UI Backend (Port 8004)

**Start:**
```bash
python samples/backends/agui/agui_server.py
```

**Test** (streams AG-UI events as Server-Sent Events):
```bash
curl -N -X POST http://localhost:8004/ \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "hello"}], "threadId": "t1"}'
```

**Features:**
- AG-UI run envelope (`messages` / `threadId` / `runId`)
- SSE event stream (`RUN_STARTED` → `TEXT_MESSAGE_*` → `RUN_FINISHED`)
- Health check endpoint

> The gateway strips the `/agui` prefix, so `POST /agui` is forwarded here as `POST /`.

### ACP Backend (Port 8005)

**Start:**
```bash
python samples/backends/acp/acp_server.py
```

**Test:**
```bash
# List agents
curl http://localhost:8005/agents

# Create a run
curl -X POST http://localhost:8005/runs \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "greeter", "input": [{"role": "user", "parts": [{"content": "Ada"}]}]}'
```

**Features:**
- BeeAI/IBM ACP REST surface: `GET /agents`, `POST /runs`, `GET /runs/{id}`, resume `POST /runs/{id}`, `POST /runs/{id}/cancel`
- In-memory run store
- Health check endpoint

> The gateway strips the `/acp` prefix, so `POST /acp/runs` is forwarded here as `POST /runs`.

### GraphQL Backend (Port 8006)

**Start:**
```bash
python samples/backends/graphql/graphql_server.py
```

**Test:**
```bash
curl -X POST http://localhost:8006/graphql \
  -H "Content-Type: application/json" \
  -d '{"query": "{ hello users { id name } user(id: \"1\") { email } }"}'
```

**Features:**
- Real schema via `graphql-core` (`hello`, `users`, `user(id)`)
- Query execution and full schema introspection
- Health check endpoint

## Docker Compose Setup

A `docker-compose.yml` file is provided to start all backends at once:

```bash
cd samples/backends
docker compose up -d
```

This will start:
- MCP backend on port 8001
- A2A backend on port 8002
- HTTP backend on port 8003
- WebSocket backend on port 8080
- MQTT broker on port 1883
- gRPC backend on port 50051
- AG-UI backend on port 8004
- ACP backend on port 8005
- GraphQL backend on port 8006

## Testing with Gateway

Once backends are running, configure the gateway:

```bash
# Set backend URLs in .env (gateway running directly on this machine; the
# repository's docker-compose.yml ignores these and uses service names, see
# docs/DEPLOYMENT.md)
MCP_BACKEND_URL=http://localhost:8001
A2A_BACKEND_URL=http://localhost:8002
HTTP_BACKEND_URL=http://localhost:8003
WEBSOCKET_BACKEND_URL=ws://localhost:8080
MQTT_BROKER_URL=mqtt://localhost:1883
GRPC_BACKEND_URL=localhost:50051
AG_UI_BACKEND_URL=http://localhost:8004
ACP_BACKEND_URL=http://localhost:8005
GRAPHQL_BACKEND_URL=http://localhost:8006/graphql
```

Then test through the gateway:

```bash
# Get API key
export API_KEY=$(curl -s -X POST "http://localhost:8000/admin/api-keys?name=test&rate_limit_per_minute=100" \
  -H "Authorization: Bearer $ADMIN_TOKEN" | jq -r '.key')

# Test MCP through gateway
curl -X POST http://localhost:8000/mcp/query \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "Hello", "context": {"user_id": "user123"}}'

# Test A2A through gateway
curl -X POST http://localhost:8000/a2a/message \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello", "target_agent": "agent_b"}'

# Test HTTP through gateway
curl -X POST http://localhost:8000/api/users \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "John", "email": "john@example.com"}'
```

## Requirements

Most Python backends require:
```bash
pip install fastapi uvicorn structlog pydantic
```

The GraphQL backend also needs `graphql-core`:
```bash
pip install graphql-core
```

MQTT requires Mosquitto (see setup script).

gRPC requires protobuf compilation (see the gRPC Backend section above).

## Notes

- These are **sample implementations** for testing purposes
- They demonstrate basic functionality, not production-ready code
- Modify as needed for your use cases
- All backends include health check endpoints for gateway monitoring


