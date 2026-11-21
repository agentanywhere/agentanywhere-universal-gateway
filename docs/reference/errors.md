# Errors and status codes

Every status code and `error` value that AgentAnywhere Universal Gateway itself
produces. Responses that come from a backend are passed through unchanged: if
your backend answers 404 or 500, so does the gateway, with the backend's body.

## The envelope

```json
{
  "error": "RATE_LIMIT_EXCEEDED",
  "message": "Rate limit exceeded: 5 requests per minute",
  "request_id": "ea68b03b-e9e6-44fb-b442-1e647d5a1f99",
  "timestamp": "2026-10-20T19:29:25.960190",
  "details": {"limit": 5, "window": "minute", "retry_after": 60}
}
```

| Field | Meaning |
|---|---|
| `error` | Machine-readable code (table below). |
| `message` | Human-readable text. |
| `request_id` | The request id, also sent as the `X-Request-ID` header. |
| `timestamp` | UTC time, ISO 8601, without a zone suffix. |
| `details` | Optional. Hints, limits, validation errors. |

A client whose `Accept` header prefers `text/html` (a browser) gets an HTML
page with the same information. `curl`, SDKs and `fetch` with
`Accept: application/json` get JSON.

## Codes

| Status | `error` | When | What to do |
|---|---|---|---|
| 400 | `VALIDATION_ERROR` | A protocol boundary check failed: MCP body too large, not JSON, blocked method or prompt-injection pattern; A2A, ACP or AG-UI body shape. `details` names the field. | Fix the request body, or change the `MCP_BOUNDARY_*` / `*_BOUNDARY_ENABLED` settings. |
| 400 | `HTTP_ERROR` | An admin or protocol endpoint rejected its input, for example a past `expires_at`, MQTT without `topic`. | Read `message`. |
| 401 | `AUTHENTICATION_ERROR` | Data plane: `API key required` (no key) or `Invalid or inactive API key...` (unknown, disabled or expired key). | See [Troubleshoot 401, 403, 404, 502 and 429](../how-to/troubleshoot-401-502-429.md). |
| 401 | `HTTP_ERROR` | Admin plane: `Authentication required` (no bearer token), `Invalid admin token` (including `SECRET_KEY` while `ADMIN_TOKEN` is set), or on sign-in `Invalid username or password`. Also `/metrics` and `/health/detailed` without the admin credential when `OPS_ENDPOINTS_REQUIRE_ADMIN=true`. | Send `Authorization: Bearer <ADMIN_TOKEN>`. |
| 403 | `AUTHORIZATION_ERROR` | The key's `allowed_paths` does not cover the path (`This API key is not allowed to access this path`; `details` lists the scope), or an IP rule or governance policy denied the request (`details` has the rule or policy id). | Use a key with a wider scope; check `/admin/governance/ip-rules` and `/admin/governance/policies`. |
| 404 | `AGENT_NOT_FOUND` | `/agents/<slug>/...` with an unknown slug, when the agent edge is on. | Register the agent, or fix the slug. |
| 404 | `NOT_FOUND` | `/docs`, `/redoc` or `/openapi.json` with `DEBUG=false`. | Set `DEBUG=true` on a development machine. |
| 404 | `HTTP_ERROR` | An admin resource does not exist (`API key not found`, `Tenant 'x' not found`), or `/metrics` with `METRICS_ENABLED=false`. | Check the id. |
| 409 | `HTTP_ERROR` | Creating a tenant or an OpenAPI registration that already exists. | Use another id or name. |
| 410 | `API_VERSION_UNAVAILABLE` | The path resolves to an API version that has been removed. | Use a current version. |
| 422 | `VALIDATION_ERROR` | FastAPI request validation failed: a required parameter is missing or has the wrong type. `details.errors` lists each problem with its `location`. | Check the parameters ([HTTP endpoints](http-endpoints.md)). |
| 422 | `HTTP_ERROR` | `POST /admin/webhooks` without `url` or an event. | Send both. |
| 429 | `RATE_LIMIT_EXCEEDED` | The key's per-minute or per-hour limit, or its tenant's quota, is used up. `Retry-After` is 60 or 3600; tenant errors add `tenant_id` and `quota` to `details`. | Wait, or raise the limit. |
| 500 | `INTERNAL_ERROR` | An unexpected error, including a request to `/agui`, `/acp`, `/ws` or `/mqtt/...` when that backend is not configured. The cause is in the gateway log as `gateway_error`. | Read the log line with the same `request_id`. |
| 502 | `BACKEND_ERROR` | The backend could not be reached or did not answer in time (connection refused, DNS failure, connect or read timeout). | See [Troubleshoot 401, 403, 404, 502 and 429](../how-to/troubleshoot-401-502-429.md). |
| 503 | `SERVICE_UNAVAILABLE` | The backend's circuit breaker is open (`Upstream service is temporarily unavailable (circuit open)`), or `/grpc` or `/graphql` without `GRPC_BACKEND_URL` or `GRAPHQL_BACKEND_URL`. | Wait `CIRCUIT_BREAKER_TIMEOUT` seconds after fixing the backend. |
| 503 | `HTTP_ERROR` | `MQTT broker not connected`. | Start the broker and restart the gateway. |

A timeout is reported as 502, not 504.

## Readiness and health

| Endpoint | Status | Meaning |
|---|---|---|
| `/ready` | 503, `{"status": "not_ready", "reason": ...}` | The database probe failed. |
| `/health/detailed` | 503 | At least one dependency probe is `unhealthy`. |

These do not use the error envelope.

## WebSocket close codes

| Code | Route | Meaning |
|---|---|---|
| 1003 | `/ws/*` | `WEBSOCKET_BACKEND_URL` is not set. |
| 1008 | `/ws/*` | `API key required`, `Invalid API key` or `Authentication failed`. As shipped, a valid key also gets `Authentication failed`. |
| 1008 | `/agents/*` | The agent edge is off. |
| 4040 | `/agents/*` | Unknown slug. |
| 1011 | `/agents/*` | The upstream WebSocket could not be opened. |

## Command-line tool exit codes

See the [CLI reference](cli.md#exit-codes).
