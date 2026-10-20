# Protocols Guide

AgentAnywhere Universal Gateway puts one authentication, rate-limiting and
logging layer in front of several agent and application protocols. Seven of the
routes below work today. **gRPC and WebSocket do not**; their sections say what
happens instead. This guide gives each protocol a single reference: what path
it uses, which environment variable points it at a backend, a runnable
request/response example, and the matching **sample backend** you can start
locally.

Every protocol below has:

- **📖 Docs** — the section here (plus deeper notes in [`API_REFERENCE.md`](API_REFERENCE.md) and [`CONFIGURATION.md`](CONFIGURATION.md)).
- **🧪 Sample** — a ready-to-run backend under [`samples/backends/`](../samples/backends/).

## At a glance

| Protocol | Gateway route | Backend env var | Sample backend |
|----------|---------------|-----------------|----------------|
| MCP | `/mcp/*`, `/api/mcp/*` | `MCP_BACKEND_URL` | `samples/backends/mcp/` |
| A2A | `/a2a/*`, `/api/a2a/*` | `A2A_BACKEND_URL` | `samples/backends/a2a/` |
| AG-UI | `/agui/*`, `/api/agui/*` | `AG_UI_BACKEND_URL` | `samples/backends/agui/` |
| ACP | `/acp/*`, `/api/acp/*` | `ACP_BACKEND_URL` | `samples/backends/acp/` |
| HTTP/REST | `/api/*`, `/rest/*`, `/http/*` | `HTTP_BACKEND_URL` | `samples/backends/http/` |
| MQTT | `/mqtt/*`, `/api/mqtt/*` | `MQTT_BROKER_URL` | `samples/backends/mqtt/` |
| WebSocket | `/ws/*`, `/websocket/*` | `WEBSOCKET_BACKEND_URL` | `samples/backends/websocket/` |
| gRPC | `/grpc/*`, `/api/grpc/*` | `GRPC_BACKEND_URL` | `samples/backends/grpc/` |
| GraphQL | `/graphql`, `/api/graphql` | `GRAPHQL_BACKEND_URL` | `samples/backends/graphql/` |

> **Auth:** every example assumes an API key in `X-API-Key` (or a bearer token).
> See [QUICKSTART.md](QUICKSTART.md) for how to mint one.

## Run every sample backend

```bash
cd samples/backends
docker compose up -d          # all backends, or:
bash start_all.sh             # start the Python backends locally
```

Point the gateway at them (in your `.env`):

```bash
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

---

## MCP (Model Context Protocol)

Requests under `/mcp/*` are proxied to the MCP backend.

- **Route:** `/mcp/*` · **Backend:** `MCP_BACKEND_URL` · **Sample:** `samples/backends/mcp/` (port 8001)

```bash
curl -X POST http://localhost:8000/mcp/query \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"query": "Hello MCP", "context": {"user_id": "user123"}}'
```

## A2A (Agent-to-Agent)

Requests under `/a2a/*` are proxied to the A2A backend.

- **Route:** `/a2a/*` · **Backend:** `A2A_BACKEND_URL` · **Sample:** `samples/backends/a2a/` (port 8002)

```bash
curl -X POST http://localhost:8000/a2a/message \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"message": "Hello", "target_agent": "agent_b", "source_agent": "agent_a"}'
```

## AG-UI (Agent-User Interaction)

Transport-agnostic agent↔UI protocol; the sample uses the HTTP + Server-Sent
Events flavour. **The gateway strips the `/agui` prefix**, so `POST /agui`
reaches the backend as `POST /`.

- **Route:** `/agui/*` · **Backend:** `AG_UI_BACKEND_URL` · **Sample:** `samples/backends/agui/` (port 8004)

```bash
curl -N -X POST http://localhost:8000/agui \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "hello"}], "threadId": "t1"}'
```

Response is an SSE stream: `RUN_STARTED` → `TEXT_MESSAGE_START` →
`TEXT_MESSAGE_CONTENT`… → `TEXT_MESSAGE_END` → `RUN_FINISHED`.

## ACP (Agent Communication Protocol)

BeeAI / IBM ACP REST runs. **The gateway strips the `/acp` prefix**, so
`POST /acp/runs` reaches the backend as `POST /runs`.

- **Route:** `/acp/*` · **Backend:** `ACP_BACKEND_URL` · **Sample:** `samples/backends/acp/` (port 8005)

```bash
curl -X POST http://localhost:8000/acp/runs \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"agent_name": "greeter", "input": [{"role": "user", "parts": [{"content": "Ada"}]}]}'
```

Surface: `GET /agents`, `POST /runs`, `GET /runs/{id}`, resume `POST /runs/{id}`,
`POST /runs/{id}/cancel`.

## HTTP / REST

Generic reverse-proxy for any REST backend.

- **Route:** `/api/*`, `/rest/*`, `/http/*` · **Backend:** `HTTP_BACKEND_URL` · **Sample:** `samples/backends/http/` (port 8003)

```bash
curl -X POST http://localhost:8000/api/users \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "John Doe", "email": "john@example.com"}'
```

## MQTT

Publishes to an MQTT broker. The message goes in the `payload` field.

- **Route:** `/mqtt/*` · **Backend:** `MQTT_BROKER_URL` · **Sample:** `samples/backends/mqtt/` (broker on 1883)

```bash
# Publish through the gateway
curl -X POST http://localhost:8000/mqtt/publish \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"topic": "test/topic", "payload": {"text": "Hello MQTT"}, "qos": 1}'
```

## WebSocket

> **Not working.** The `/ws/*` route currently refuses every connection,
> including ones that present a valid API key, and it has no relay to the
> backend. The section below describes the intended interface.

- **Route:** `/ws/*`, `/websocket/*` · **Backend:** `WEBSOCKET_BACKEND_URL` · **Sample:** `samples/backends/websocket/` (port 8080)

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/endpoint?api_key=YOUR_KEY');
ws.onmessage = (e) => console.log('Received:', e.data);
ws.onopen = () => ws.send(JSON.stringify({ message: 'Hello WebSocket' }));
```

## gRPC

> **Not working.** The JSON-to-gRPC bridge does not forward calls to a backend.
> With the stock requirements the request below answers HTTP 200 with
> `"status": "unsupported"`. The section describes the intended interface.

The gateway exposes a JSON-to-gRPC bridge (`{service, method, data, stream}`).

- **Route:** `/grpc/*` · **Backend:** `GRPC_BACKEND_URL` · **Sample:** `samples/backends/grpc/` (port 50051)

```bash
curl -X POST http://localhost:8000/grpc \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"service": "Greeter", "method": "SayHello", "data": {"name": "Ada"}}'
```

## GraphQL

Query proxying to a GraphQL backend.

- **Route:** `/graphql` · **Backend:** `GRAPHQL_BACKEND_URL` · **Sample:** `samples/backends/graphql/` (port 8006)

```bash
curl -X POST http://localhost:8000/graphql \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"query": "{ hello users { id name } user(id: \"1\") { email } }"}'
```

---

## See also

- [QUICKSTART.md](QUICKSTART.md) — install, run, mint an API key
- [CONFIGURATION.md](CONFIGURATION.md) — every environment variable
- [API_REFERENCE.md](API_REFERENCE.md) — full endpoint reference
- [samples/backends/README.md](../samples/backends/README.md) — sample backends in depth
