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

Every path requires authentication except the following, which the
authentication middleware (`app/main.py`) lets through:

| Path | Notes |
|---|---|
| `/health` | The default; it is the `HEALTH_CHECK_PATH` setting. |
| `/metrics` | The default; it is the `METRICS_PATH` setting. Prometheus metrics. Needs the admin credential when `OPS_ENDPOINTS_REQUIRE_ADMIN=true`. |
| `/ready` | Readiness probe. |
| `/live` | Liveness probe. |
| `/health/detailed` | Per-dependency health. Needs the admin credential when `OPS_ENDPOINTS_REQUIRE_ADMIN=true`. |
| `/docs`, `/redoc`, `/openapi.json` | Interactive documentation and the OpenAPI schema, served only with `DEBUG=true`. With `DEBUG=false` (the default) the gateway answers 404 for them and never forwards them to a backend. |
| `/.well-known/agent-card.json`, `/.well-known/ag-ui.json` | Discovery documents. |
| `/oauth2/*` | The gateway's OAuth2 endpoints, which clients use to obtain tokens. |
| `/admin/*` | Not covered by API keys, but not open: the admin endpoints have their own check (see below). All but three answer 401 without the admin credential. |

> **Warning.** By default `/metrics` and `/health/detailed` need no credential
> and expose operational detail (Prometheus metrics, per-dependency health and
> the gateway version) to anyone who can reach the gateway. The shipped
> `nginx/nginx.conf` answers them only for loopback and private addresses, but
> the gateway's own port 8000 is not behind it. Set
> `OPS_ENDPOINTS_REQUIRE_ADMIN=true` to require the admin credential
> (`Authorization: Bearer ...`) on both, or block them at your firewall. The
> same applies to `/docs`, `/redoc` and `/openapi.json` when you run with
> `DEBUG=true`.

The admin credential is `ADMIN_TOKEN`, or `SECRET_KEY` when `ADMIN_TOKEN` is
empty and `SECRET_KEY` is not the shipped placeholder. Once `ADMIN_TOKEN` is
set, `SECRET_KEY` is not accepted (unless `ADMIN_ACCEPT_SECRET_KEY=true`). See
[CONFIGURATION.md](CONFIGURATION.md).

Every response, including 401, 403 and 429 answers from the authentication
layer, carries an `X-Request-ID` header (the client's own, when it sent one)
and the same value in the body's `request_id`. Rejected requests are counted
in `gateway_requests_total` (with the path label reduced to its first
segment, or `/other`) and written to `request_logs` with the error code.

### API Key Authentication

The paths in these examples are placeholders for whatever your backend serves
under the protocol prefix (see the note under "Protocol Endpoints").

```bash
# Using X-API-Key header
curl -H "X-API-Key: your-api-key" http://localhost:8000/api/users

# Using Authorization header
curl -H "Authorization: Bearer your-api-key" http://localhost:8000/api/users
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
  "services": {}
}
```

This is a fast liveness check. It makes no calls to backends, so `services` is
empty. `/live` is a second liveness probe; `/health/detailed` runs the
dependency probes.

### Readiness Check

```http
GET /ready
```

Returns 503 with `"status": "not_ready"` if the database check fails.

**Response:**
```json
{
  "status": "ready",
  "database": {...}
}
```

### Metrics

```http
GET /metrics
```

Returns Prometheus metrics in text format. With
`OPS_ENDPOINTS_REQUIRE_ADMIN=true`, send `Authorization: Bearer <admin credential>`;
without it the answer is 401.

## Protocol Endpoints

The gateway does not define the paths below. It authenticates, limits and
proxies anything under a protocol prefix (`/mcp/*`, `/a2a/*`, and so on) to the
configured backend. `/mcp/query` and `/a2a/message` are the paths used by the
sample backends in `samples/backends/`; the request and response bodies shown
are the samples' shapes, and a real backend defines its own. See
[PROTOCOLS.md](PROTOCOLS.md) for which protocols work today.

### MCP (Model Context Protocol)

#### Example: query (sample backend path)

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

#### Example: send message (sample backend path)

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

> **Not working.** The `/ws/*` route refuses every connection and has no relay
> to a backend. See [PROTOCOLS.md](PROTOCOLS.md). The example shows the
> intended interface.

```javascript
const ws = new WebSocket('ws://localhost:8000/ws/endpoint?api_key=your-key');
ws.onmessage = (event) => console.log('Received:', event.data);
ws.send(JSON.stringify({message: 'Hello'}));
```

### gRPC

> **Not working.** The JSON-to-gRPC bridge does not forward calls to a
> backend; with the stock requirements it answers HTTP 200 with
> `"status": "unsupported"`. See [PROTOCOLS.md](PROTOCOLS.md). The example
> shows the intended interface.

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

Each entry has the key's `id` (what delete and toggle take), `name`,
`key_prefix` (the first characters, to recognise it by), limits,
`allowed_paths`, `is_active`, `created_at`, `expires_at` and `last_used_at`.
The full key is never listed: the gateway stores only its SHA-256 hash.

#### Create API Key

Parameters are passed as query parameters, or as a JSON body with the same
names (`allowed_paths` may then be a list). A query parameter wins when both
are given.

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
| `allowed_paths` | Optional comma-separated path allow-list (e.g. `/mcp,/a2a`). Each entry is a path prefix matched on whole segments (`/mcp` allows `/mcp` and `/mcp/tools`, not `/mcpx`); an entry ending in `*` is a plain prefix. Requests elsewhere answer 403, and so does any path with `.` or `..` segments. Empty means every path. |

**Response** (the raw `key` is only returned once, on creation; the gateway
keeps only its hash, so it cannot be shown again):
```json
{
  "id": "…",
  "name": "My API Key",
  "key": "sk-…",
  "key_prefix": "sk-AbCdEfGh",
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

Deleting or disabling a key takes effect on the next request, also when the
Redis key cache is in use.

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
GET /admin/ai/anomalies?window_minutes=60
```

#### Insights

```http
GET /admin/ai/insights
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

`url`, `event_type` and `secret` may also be query parameters. One webhook is
registered per event; the response lists them in `ids` (and the first in
`id`). `DELETE /admin/webhooks/{id}` finds the webhook without `event_type`.

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

With `DEBUG=true`, visit http://localhost:8000/docs for interactive
Swagger/OpenAPI documentation. It is not served otherwise.

## SDK Generation

Generate SDKs for your preferred language:

```http
GET  /admin/sdk/languages
GET  /admin/sdk/preview/{language}
POST /admin/sdk/generate/{language}
```

`{language}` is `python`, `javascript` or `go`. `generate` is a POST and returns
a zip file (`{language}_sdk.zip`); `preview` is a GET and returns the code.
Both need the admin credential.


