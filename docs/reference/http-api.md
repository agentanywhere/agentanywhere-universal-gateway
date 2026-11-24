# HTTP API reference

This page describes the conventions of the gateway's HTTP API and the
endpoints you will use most, with real responses. The complete list of
operations, with parameters and the credential each needs, is generated from
the code: [HTTP endpoints](http-endpoints.md).

Responses on this page were captured by calling the gateway in process with
FastAPI's test client (no server), with the configuration noted. Ids, keys and
timestamps differ on every run. JSON is indented for reading.

## Contents

- [Base URL and credentials](#base-url-and-credentials)
- [Response headers](#response-headers)
- [Errors](#errors)
- [Health, readiness and metrics](#health-readiness-and-metrics)
- [The data plane](#the-data-plane)
- [API keys](#api-keys)
- [Tenants](#tenants)
- [Routes](#routes)
- [Request logs](#request-logs)
- [Runtime configuration](#runtime-configuration)
- [Webhooks](#webhooks)
- [Other admin areas](#other-admin-areas)
- [OpenAPI schema](#openapi-schema)

## Base URL and credentials

The gateway listens on `http://127.0.0.1:8000` by default (use `127.0.0.1`: `localhost` may resolve to IPv6 first). There is no version
prefix.

| Plane | Paths | Send |
|---|---|---|
| Data plane | everything except the ones below | `X-API-Key: <key>` or `Authorization: Bearer <key>` |
| Admin plane | `/admin/*` | `Authorization: Bearer <ADMIN_TOKEN>` |
| Public | `/health`, `/live`, `/ready`, `/.well-known/*`, `/oauth2/*`, `/admin/health`, `/admin/auth/config`, `/admin/auth/login`; `/metrics` and `/health/detailed` unless `OPS_ENDPOINTS_REQUIRE_ADMIN=true` (then the admin credential); `/openapi.json`, `/docs`, `/redoc` only with `DEBUG=true` (404 otherwise) | nothing |

The [security model](../explanation/security-model.md) explains each
credential.

## Response headers

Every response that passes the authentication middleware carries:

| Header | Meaning |
|---|---|
| `X-Request-ID` | The incoming `X-Request-ID`, or a new UUID. |
| `X-Response-Time` | Seconds the gateway spent, to three decimals. |
| `traceparent` | W3C trace context for this hop. |
| `X-Tenant-ID` | The tenant, when the key belongs to one. |
| `X-API-Version`, `Warning`, `Sunset`, `Deprecation` | When the path matches a registered API version. |
| `X-Anomaly-Detected`, `X-Anomaly-Score` | When the in-process anomaly detector flags the request. |

Responses rejected by the authentication layer (401, 403, 429) carry
`X-Request-ID` too, with the same value in the body's `request_id`. 429
responses carry `Retry-After`.

## Errors

Errors use one JSON envelope:

```json
{
  "error": "BACKEND_ERROR",
  "message": "Backend: Backend service unavailable",
  "request_id": "8f6abf94-63f5-4fb5-ab94-cd19f3086f74",
  "details": {
    "hint": "The upstream backend could not be reached. Check that the backend is running and reachable from the gateway.",
    "service": "Backend"
  },
  "timestamp": "2026-10-20T17:26:25.615769"
}
```

A browser that prefers `text/html` gets an HTML page with the same content
instead. Every `error` code and status is listed in the
[errors reference](errors.md).

## Health, readiness and metrics

### `GET /health`

Liveness. No dependency checks.

```json
{"status": "healthy", "version": "1.0.0", "timestamp": "2026-10-20T17:26:24.926871", "services": {}}
```

### `GET /live`

```json
{"status": "alive", "pid": 25034}
```

### `GET /ready`

200 when the database answers; 503 with `{"status": "not_ready", "reason": ...}`
when it does not.

```json
{
  "status": "ready",
  "database": {
    "name": "database",
    "kind": "database",
    "status": "healthy",
    "latency_ms": 2.25,
    "message": null,
    "last_checked": "2026-10-20T17:26:24.935388Z"
  }
}
```

### `GET /health/detailed`

Runs every dependency probe (database, Redis, backends, log exporters).
Answers 503 when the aggregate is `unhealthy`. Captured with Redis disabled
and no backends running:

```json
{
  "status": "unhealthy",
  "version": "1.0.0",
  "checked_at": "2026-10-20T17:26:24.948643Z",
  "dependencies": [
    {"name": "database", "kind": "database", "status": "healthy", "latency_ms": 9.53, "message": null, "last_checked": "2026-10-20T17:26:24.946603Z"},
    {"name": "redis", "kind": "cache", "status": "healthy", "latency_ms": null, "message": "disabled", "last_checked": "2026-10-20T17:26:24.937152Z"},
    {"name": "backends", "kind": "backend", "status": "unhealthy", "latency_ms": 11.35, "message": "0/3 reachable", "last_checked": "2026-10-20T17:26:24.948542Z"},
    {"name": "log_exporters", "kind": "exporter", "status": "healthy", "latency_ms": null, "message": "none configured", "last_checked": "2026-10-20T17:26:24.941856Z"}
  ]
}
```

### `GET /metrics`

Prometheus text format. See the [metrics reference](metrics.md).

## The data plane

The gateway does not define the paths under each protocol prefix. It forwards
the method, path, query string, headers and body to the backend chosen by the
prefix (or by a dynamic route) and returns the backend's status, headers and
body. The [protocol support matrix](../explanation/protocol-support.md) lists
the prefixes.

Example, with the sample HTTP backend:

```bash
curl -s -X POST http://127.0.0.1:8000/api/users \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "Ada", "email": "ada@example.com"}'
```

```json
{"id":"user_1","name":"Ada","email":"ada@example.com","metadata":null,"created_at":"2026-10-20T17:26:25.810109+00:00"}
```

Dedicated data-plane endpoints:

| Endpoint | Body | Success response |
|---|---|---|
| `/graphql`, `/api/graphql` (any method) | Whatever the GraphQL server takes; it is passed on unchanged | The backend's own response, status and headers included. 503 without `GRAPHQL_BACKEND_URL`. |
| `POST /mqtt/publish` | `{"topic": "...", "payload": {...}, "qos": 1}` | `{"status": "published", "topic": "..."}`. 503 when the broker is not connected; 400 without `topic`. |
| `POST /mqtt/subscribe` | `{"topic": "...", "qos": 1}` | `{"status": "subscribed", "topic": "..."}`. Messages are not returned to the caller. |

## API keys

### `POST /admin/api-keys`

Parameters can be query parameters or a JSON body with the same names (in a
body, `allowed_paths` may be a list). A query parameter wins when both are
given.

| Parameter | Default | Meaning |
|---|---|---|
| `name` | required | Label for the key. |
| `rate_limit_per_minute` | `60` | Requests per minute. |
| `rate_limit_per_hour` | `1000` | Requests per hour. |
| `expires_at` | none | ISO 8601 time in the future, for example `2027-01-01T00:00:00`. |
| `expires_in_days` | none | Positive whole number of days. Not together with `expires_at`. |
| `allowed_paths` | none | Path scope: comma-separated prefixes (or a JSON list in a body), matched on whole segments; an entry ending in `*` is a plain prefix. Requests elsewhere, and paths with `.` or `..` segments, answer 403. Empty means every path. |

```bash
curl -s -X POST "http://127.0.0.1:8000/admin/api-keys?name=first-key" -H "Authorization: Bearer $ADMIN"
```

```json
{
  "id": "37a9faca-3c3c-4f12-940c-d5671d720671",
  "name": "first-key",
  "key": "sk-fyR0Gj_aKRMbpMmMpCKbz2NpJpWskKOmX583PpC70p0",
  "key_prefix": "sk-fyR0Gj_a",
  "rate_limit_per_minute": 60,
  "rate_limit_per_hour": 1000,
  "is_active": true,
  "created_at": "2026-10-20T19:28:40.389319",
  "expires_at": null,
  "allowed_paths": []
}
```

`key` appears only in this response: the gateway stores a SHA-256 hash.
400 if `expires_at` is in the past or not ISO 8601, if `expires_in_days` is
not positive, or if both are given.

### `GET /admin/api-keys`

Returns every key's `id` and `key_prefix`, never the key:

```json
[
  {
    "id": "37a9faca-3c3c-4f12-940c-d5671d720671",
    "name": "first-key",
    "key_prefix": "sk-fyR0Gj_a",
    "rate_limit_per_minute": 60,
    "rate_limit_per_hour": 1000,
    "allowed_paths": [],
    "metadata": {},
    "created_at": "2026-10-20T19:28:40.389319",
    "expires_at": null,
    "last_used_at": null,
    "is_active": true
  }
]
```

### `PUT /admin/api-keys/{key_id}/toggle`

Flips `is_active`. `{key_id}` is the `id`, not the key; anything else answers
404 `API key not found`. Takes effect on the next request, also with the Redis
key cache.

```json
{"id": "37a9faca-3c3c-4f12-940c-d5671d720671", "name": "first-key", "is_active": false}
```

### `DELETE /admin/api-keys/{key_id}`

Deletes the key, with immediate effect. `{"message": "API key deleted"}`, or 404.

## Tenants

### `POST /admin/tenants`

JSON body: `tenant_id` and `name` (required), `description`, `config`
(object) and `quota` (object). Quota keys you leave out take the defaults
shown below. 409 if the tenant exists.

```json
{"tenant_id": "acme", "name": "Acme", "quota": {"requests_per_minute": 2}}
```

```json
{
  "tenant_id": "acme",
  "name": "Acme",
  "description": null,
  "config": {},
  "quota": {"requests_per_minute": 2, "requests_per_hour": 10000, "requests_per_day": 100000, "storage_mb": 1000},
  "is_active": true,
  "created_at": "2026-10-20T17:26:25.852110",
  "updated_at": "2026-10-20T17:26:25.852114"
}
```

Only `requests_per_minute` and `requests_per_hour` are enforced. A quota value
of 0 or less means no limit for that window.

### `POST /admin/tenants/{tenant_id}/associate-api-key`

JSON body `{"api_key_id": "<id>", "api_key": "<key>"}`. Both are required.

```json
{"tenant_id": "acme", "api_key_id": "db9bd1a1-7333-4e6f-8d2b-372a25ee484e", "api_key": "sk-GAXiO...", "message": "API key associated"}
```

### Others

`GET /admin/tenants`, `GET /admin/tenants/{id}`,
`PUT /admin/tenants/{id}/quota` (JSON object merged into the quota),
`PUT /admin/tenants/{id}/config`, `GET /admin/tenants/{id}/usage?window=minute|hour|day&lookback=N`,
`POST /admin/tenants/{id}/activate`, `POST /admin/tenants/{id}/deactivate`,
`DELETE /admin/tenants/{id}/api-keys/{api_key_id}`, `DELETE /admin/tenants/{id}`.

## Routes

### `POST /admin/routes`

JSON body (`RouteConfig`):

| Field | Default | Meaning |
|---|---|---|
| `path` | required | Exact path, or a prefix ending in `*`, for example `/svc/*`. |
| `backend_url` | required | Base URL of the backend. The full request path is appended. |
| `protocol` | `http` | One of `mcp`, `a2a`, `ag_ui`, `acp`, `http`, `mqtt`, `websocket`, `grpc`, `graphql`. Sent to the backend as `X-Protocol`. |
| `timeout` | `30` | Seconds. |
| `rewrite_path` | none | Replace the whole path with this value. |
| `requires_auth`, `rate_limit`, `headers` | `true`, none, `{}` | Stored, **not applied**. |

```json
{"path": "/svc/*", "backend_url": "http://127.0.0.1:59998", "protocol": "http", "timeout": 10}
```

```json
{"id": "3294731b-8fc3-4e2a-80ef-db9af98e7e4d", "path": "/svc/*", "backend_url": "http://127.0.0.1:59998", "protocol": "http", "timeout": 10, "requires_auth": true, "active": true}
```

`active: true` means the route is in the live table of the process that
handled the call.

### `GET /admin/routes`

Lists static routes (from settings, `"source": "static"`) and registered ones
(`"source": "dynamic"`). The static list also shows entries for `/test/mcp`,
`/test/a2a`, `/test/websocket` and `/test/grpc`; the gateway has no handlers
for those paths. There is no endpoint to delete a dynamic route; delete the
row from the `routes` table and wait for the refresh.

## Request logs

### `GET /admin/logs`

| Parameter | Default | Meaning |
|---|---|---|
| `limit` | `100` | Rows to return, newest first. |
| `offset` | `0` | Rows to skip. |
| `status_code` | none | Exact status code. |
| `path` | none | Rows whose path contains this text. |
| `level` | none | `error` (status 500 and above) or `warning` (400 to 499). Applied to the page after `limit` and `offset`, and `total` then counts that page only. |

```json
{
  "logs": [
    {"id": 16, "request_id": "f236eeea-513a-4c9f-9a70-9fcb4c14968a", "method": "GET", "path": "/api/users", "client_ip": "testclient", "api_key_id": "5915a9cc-9e95-450e-8558-8732092d3913", "status_code": 429, "response_time_ms": 3, "timestamp": "2026-10-20T19:28:40.765216", "user_agent": "testclient", "error": "RATE_LIMIT_EXCEEDED", "protocol": null}
  ],
  "total": 2
}
```

(`GET /admin/logs?limit=2&status_code=429`, one row shown.) Rejected requests
have their error code in `error`. `protocol` is always `null` in this
release.

## Runtime configuration

| Endpoint | Does |
|---|---|
| `GET /admin/config?section=&redacted=true` | Current settings grouped by section. Secrets (including `ADMIN_TOKEN`, every `*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `*_API_KEY` setting and URL passwords) are redacted; `redacted=false` returns raw values. |
| `GET /admin/config/diff` | Fields that differ from the defaults, redacted the same way. |
| `GET /admin/config/schema` | JSON Schema of the settings. |
| `POST /admin/config/validate` | Dry-run validation of a JSON object of fields. |
| `PUT /admin/config` | Apply a JSON object of **live** fields (see the [configuration reference](configuration.md)) to the running process. Other fields are refused. |
| `POST /admin/config/reload` | Re-read settings from the environment and `CONFIG_FILE`. |
| `GET /admin/config/audit?limit=100` | Recent runtime changes, redacted. |

## Webhooks

Webhooks are kept in memory. `POST /admin/webhooks` takes `url`, `event_type`
or `events`, `secret`, `headers` and `filters` as a JSON body (`url`,
`event_type` and `secret` may also be query parameters), registers one webhook
per event and answers with their `ids`; unknown event names are accepted and
listed in `unknown_events`. Reproduced:

```json
{"id": "nonsense_event_1792504721.285245", "ids": ["nonsense_event_1792504721.285245"], "event_type": "nonsense_event", "events": ["nonsense_event"], "url": "https://hooks.example.com/x", "status": "registered", "unknown_events": ["nonsense_event"]}
```

`DELETE /admin/webhooks/{id}` needs no `event_type`. See
[Send events to a webhook](../how-to/configure-webhooks.md).

## Other admin areas

These areas are listed with their parameters in the
[generated endpoint list](http-endpoints.md). Their behaviour is covered by
the automated tests in `tests/`; it was not checked by hand for this page.

| Prefix | Purpose |
|---|---|
| `/admin/governance/*` | Policies, IP rules, admin roles (not enforced yet), the hash-chained audit log, expiring keys. |
| `/admin/transformations` | Declarative request and response rewrite rules (in memory). |
| `/admin/versions` | API versions, deprecation and sunset headers (in memory). |
| `/admin/oauth2/*` | OAuth2 providers and tokens for `/oauth2/*` (in memory). |
| `/admin/plugins` | List, enable, disable, remove and reload plugins. |
| `/admin/registrations/*` | OpenAPI documents advertised as MCP tools and A2A skills. |
| `/admin/agents` | The agent-edge registry. |
| `/admin/log-exporters/*`, `/admin/logging`, `/admin/notifications/*` | Log export, logging levels, email and Slack alerts. |
| `/admin/observability/*` | Dependency probes, SLOs, in-process traces, the tracer. |
| `/admin/settings/*` | Settings profiles and per-protocol defaults. |
| `/admin/sdk/*` | Generated client SDKs for Python, JavaScript and Go. |
| `/admin/ai/*` | Experimental analytics; see [known limitations](../known-limitations.md#the-adminai-analytics-endpoints-are-experimental). |
| `/admin/dashboard/stats`, `/admin/health/detailed` | Console dashboard data. |

## OpenAPI schema

`/openapi.json`, `/docs` (Swagger UI) and `/redoc` are served only with
`DEBUG=true`, without a credential. With `DEBUG=false` (the default) the
gateway answers 404 for them (reproduced) and never forwards them to a
backend.
