# API Reference

> Reference material. It is kept in step with the code by the automated tests and by
> review, not by a line-by-line manual check. If this page and the gateway disagree,
> the gateway is right; please open an issue.

Complete API reference for AgentAnywhere Universal Gateway.

## Base URL

```
http://localhost:8000
```

## Authentication

All endpoints (except `/health`, `/ready`, `/metrics`, `/docs`) require authentication.

### API Key Authentication

```bash
# Using X-API-Key header
curl -H "X-API-Key: your-api-key" http://localhost:8000/mcp/query

# Using Authorization header
curl -H "Authorization: Bearer your-api-key" http://localhost:8000/mcp/query
```

## Gateway Endpoints

### Health Check

```http
GET /health
```

**Response:**
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "timestamp": "2025-12-03T10:00:00Z",
  "services": {
    "mcp_backend": "healthy",
    "a2a_backend": "healthy"
  }
}
```

### Readiness Check

```http
GET /ready
```

**Response:**
```json
{
  "status": "ready"
}
```

### Metrics

```http
GET /metrics
```

Returns Prometheus metrics in text format.

## Protocol Endpoints

### MCP (Model Context Protocol)

#### Query

```http
POST /mcp/query
POST /api/mcp/query
```

**Request:**
```json
{
  "query": "What is the weather?",
  "context": {
    "user_id": "user123"
  }
}
```

**Response:**
```json
{
  "response": "...",
  "context": {...}
}
```

### A2A (Agent-to-Agent)

#### Send Message

```http
POST /a2a/message
POST /api/a2a/message
```

**Request:**
```json
{
  "message": "Hello from agent A",
  "target_agent": "agent_b",
  "metadata": {
    "priority": "high"
  }
}
```

**Response:**
```json
{
  "response": "...",
  "agent_id": "agent_b"
}
```

### MQTT

#### Publish

```http
POST /mqtt/publish
```

**Request:**
```json
{
  "topic": "sensors/temperature",
  "payload": {
    "value": 25.5,
    "unit": "celsius"
  },
  "qos": 1,
  "retain": false
}
```

**Response:**
```json
{
  "success": true,
  "message_id": 12345
}
```

#### Subscribe

```http
POST /mqtt/subscribe
```

**Request:**
```json
{
  "topic": "sensors/+",
  "qos": 1
}
```

**Response:**
```json
{
  "success": true,
  "subscription_id": "sub_123"
}
```

### WebSocket

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/endpoint?api_key=your-key');
ws.onmessage = (event) => console.log('Received:', event.data);
ws.send(JSON.stringify({message: 'Hello'}));
```

### gRPC

```http
POST /grpc/call
```

**Request:**
```json
{
  "service": "ServiceName",
  "method": "MethodName",
  "data": {...}
}
```

### GraphQL

```http
POST /graphql
```

**Request:**
```json
{
  "query": "query { users { id name } }",
  "variables": {},
  "operationName": "GetUsers"
}
```

### AG-UI (Agent-GUI streaming)

AG-UI surfaces are proxied to `AG_UI_BACKEND_URL`. Requests are streamed as
Server-Sent Events when the client asks for them.

```http
GET /agui/...        # add `Accept: text/event-stream` (or `?sse=1`) to stream
POST /agui/...
```

```bash
curl -N \
  -H "X-API-Key: your-key" \
  -H "Accept: text/event-stream" \
  http://localhost:8000/agui/events
```

The gateway forwards the upstream event stream chunk-by-chunk (it does not
buffer), so long-running agent UIs see live progress.

### ACP (Agent Communication Protocol)

ACP runs are proxied to `ACP_BACKEND_URL`. A `POST .../runs` with
`Accept: text/event-stream`, or any ACP request with `?stream=1`, is streamed.

```http
POST /acp/agents/{agent}/runs   # streams when Accept: text/event-stream
GET  /acp/...
```

```bash
curl -N -X POST \
  -H "X-API-Key: your-key" \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"input": {"prompt": "hello"}}' \
  http://localhost:8000/acp/agents/demo/runs
```

## Admin API

Base URL: `/admin`

All admin endpoints require admin token authentication.

### API Keys

#### List API Keys

```http
GET /admin/api-keys
```

**Headers:**
```
Authorization: Bearer <ADMIN_TOKEN, or SECRET_KEY if ADMIN_TOKEN is not set>
```

#### Create API Key

Parameters are passed as query parameters.

```http
POST /admin/api-keys?name=My%20API%20Key&rate_limit_per_minute=100&rate_limit_per_hour=1000
```

| Query param | Description |
|-------------|-------------|
| `name` | **Required.** Human-readable key name. |
| `rate_limit_per_minute` | Per-minute rate limit (default 60). |
| `rate_limit_per_hour` | Per-hour rate limit (default 1000). |
| `expires_at` | Optional absolute ISO-8601 expiry (e.g. `2026-01-01T00:00:00`). Must be in the future. |
| `expires_in_days` | Optional relative expiry in days. Mutually exclusive with `expires_at`. |
| `allowed_paths` | Optional comma-separated path allow-list (e.g. `/mcp,/a2a`). |

**Response** (the raw `key` is only returned once, on creation):
```json
{
  "id": "…",
  "name": "My API Key",
  "key": "sk-…",
  "rate_limit_per_minute": 100,
  "rate_limit_per_hour": 1000,
  "is_active": true,
  "created_at": "2026-01-01T00:00:00",
  "expires_at": "2026-04-01T00:00:00",
  "allowed_paths": ["/mcp", "/a2a"]
}
```

Validation errors (past `expires_at`, invalid ISO, or both expiry styles at
once) return `400 Bad Request`.

#### Delete API Key

```http
DELETE /admin/api-keys/{key_id}
```

#### Toggle API Key

```http
PUT /admin/api-keys/{key_id}/toggle
```

### Routes

Dynamic routes let you point a path at a backend at runtime, without editing
configuration or restarting the gateway. A registered route **takes precedence**
over the static, prefix-based routing (`/mcp/*`, `/a2a/*`, …) built from
environment settings.

Routes are held in an in-memory snapshot, so matching costs no database work per
request. The snapshot is refreshed immediately in the worker that handles the
write, and every `DYNAMIC_ROUTES_REFRESH_SECONDS` (default 15) in other replicas.

Set `DYNAMIC_ROUTES_ENABLED=false` to disable dynamic routes entirely and pin the
gateway to static prefix routing.

#### List Routes

```http
GET /admin/routes
```

Returns both statically configured routes and registered ones. Each entry carries
a `source` field: `"static"` (derived from settings) or `"dynamic"` (persisted,
registered through this API).

#### Create Route

```http
POST /admin/routes
```

**Request:**
```json
{
  "path": "/custom/*",
  "backend_url": "http://backend:8000",
  "protocol": "http",
  "timeout": 30
}
```

**Response:**
```json
{
  "id": "8f2c…",
  "path": "/custom/*",
  "backend_url": "http://backend:8000",
  "protocol": "http",
  "timeout": 30,
  "requires_auth": true,
  "active": true
}
```

`active` confirms the route was loaded into the live routing table, not merely
stored — if it is `false`, the row was persisted but the registry could not be
refreshed (check the logs for `route_registry_refresh_after_create_failed`).

**Matching rules.** An exact path match wins over a wildcard. Among wildcards the
**longest** matching prefix wins, so `/api/v1/*` beats `/api/*` regardless of the
order in which they were created. Paths with no matching route fall through to
static prefix routing.

> **Fixed in this release.** Routes created through this endpoint were previously
> persisted but never consulted at request time, and did not appear in
> `GET /admin/routes` — registering a route silently had no effect. Both the
> listing and the request path now read the registered routes.

### OpenAPI Registrations

Register an OpenAPI 3.x document so its operations are advertised as **MCP tools**
and **A2A skills** (and discoverable via the Agent Card / MCP manifest).

#### Create a registration

```http
POST /admin/registrations/openapi
```

**Request:**
```json
{
  "name": "petstore",
  "openapi_document": { "openapi": "3.0.0", "info": {"title": "Demo", "version": "1.0"}, "paths": { "/pets": { "get": { "operationId": "listPets" } } } },
  "upstream_base_url": "https://api.example.com",
  "description": "Pet store API",
  "path_prefix": "",
  "expose_mcp_tools": true,
  "expose_a2a_skills": true
}
```

Returns the created registration including its `id`. An invalid document
returns `400`; a duplicate active `name` returns `409`.

#### List registrations / synthesized MCP tools

```http
GET /admin/registrations/openapi     # { "count": N, "registrations": [...] }
GET /admin/registrations/mcp-tools   # { "count": N, "tools": [...] }
```

#### Delete a registration

```http
DELETE /admin/registrations/openapi/{reg_id}          # soft delete (mark inactive)
DELETE /admin/registrations/openapi/{reg_id}?hard=true # remove the row
```

### Logs

#### Get Logs

```http
GET /admin/logs?limit=100&offset=0&level=error
```

**Query Parameters:**
- `limit`: Number of logs to return (default: 100)
- `offset`: Pagination offset (default: 0)
- `level`: Filter by level (info, warning, error)
- `protocol`: Filter by protocol
- `start_time`: Start timestamp (ISO format)
- `end_time`: End timestamp (ISO format)

### AI Features

#### Anomaly Detection

```http
GET /admin/ai/anomalies?limit=10
```

#### Intelligent Rate Limiting

```http
GET /admin/ai/rate-limiting/{client_id}
```

#### Scaling Recommendations

```http
GET /admin/ai/scaling-recommendations
```

#### Cost Analysis

```http
GET /admin/ai/cost-analysis
```

#### Traffic Forecast

```http
GET /admin/ai/traffic-forecast?hours=24
```

### Tenants

#### List Tenants

```http
GET /admin/tenants
```

#### Create Tenant

```http
POST /admin/tenants
```

**Request:**
```json
{
  "name": "Tenant Name",
  "quota_per_minute": 1000,
  "quota_per_hour": 10000
}
```

### OAuth2

#### List Providers

```http
GET /admin/oauth2/providers
```

#### Create Provider

```http
POST /admin/oauth2/providers
```

**Request:**
```json
{
  "name": "google",
  "client_id": "...",
  "client_secret": "...",
  "authorization_url": "...",
  "token_url": "...",
  "userinfo_url": "..."
}
```

### Webhooks

#### List Webhooks

```http
GET /admin/webhooks
```

#### Create Webhook

```http
POST /admin/webhooks
```

**Request:**
```json
{
  "url": "https://example.com/webhook",
  "events": ["rate_limit_exceeded", "anomaly_detected"],
  "secret": "webhook-secret"
}
```

## Error Responses

### 400 Bad Request

```json
{
  "error": "Invalid request",
  "detail": "..."
}
```

### 401 Unauthorized

```json
{
  "error": "Unauthorized",
  "detail": "Invalid API key"
}
```

### 429 Too Many Requests

```json
{
  "error": "Rate limit exceeded",
  "detail": "60 requests per minute limit exceeded"
}
```

### 502 Bad Gateway

```json
{
  "error": "Backend service unavailable",
  "detail": "..."
}
```

## Interactive API Documentation

Visit http://localhost:8000/docs for interactive Swagger/OpenAPI documentation.

## SDK Generation

Generate SDKs for your preferred language:

```http
GET /admin/sdk/preview/python
GET /admin/sdk/generate/python
GET /admin/sdk/generate/javascript
GET /admin/sdk/generate/go
```


