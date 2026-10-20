# Metrics reference

AgentAnywhere Universal Gateway serves Prometheus metrics at `METRICS_PATH`
(default `/metrics`) when `METRICS_ENABLED=true` (the default). It needs no
credential unless `OPS_ENDPOINTS_REQUIRE_ADMIN=true`, in which case send
`Authorization: Bearer <admin credential>`. All metrics are in the default `prometheus_client` registry of the
process that answers the scrape.

The metric families below were read from a live `/metrics` response of the
gateway in process. Label values in the examples are real.

## Request metrics

| Metric | Type | Labels | Updated when |
|---|---|---|---|
| `gateway_requests_total` | counter | `method`, `path`, `status_code`, `protocol` | After every request, including those rejected with 401, 403 or 429. |
| `gateway_request_duration_seconds` | histogram (buckets 0.01 to 10 s) | `method`, `path`, `protocol` | As above. |
| `gateway_errors_total` | counter | `error_type`, `protocol` | On 5xx responses produced by the gateway, unhandled exceptions and backend connection failures. |
| `gateway_red_requests_total` | counter | `method`, `route`, `status_class`, `protocol`, `tenant` | After every request, including rejected ones. |
| `gateway_rate_limit_hits_total` | counter | `client_id` | On every 429 from the authentication layer (key limit or tenant quota); `client_id` is the key's id. |
| `gateway_red_request_duration_seconds` | histogram (buckets 0.005 to 10 s) | `method`, `route`, `protocol`, `tenant` | As above. |
| `gateway_red_errors_total` | counter | `method`, `route`, `protocol`, `tenant`, `error_class` | When the status is 500 or above. |

Label notes:

- `path` is the raw request path for accepted requests. Every distinct path
  is a new series, so paths with ids in them can create many series; prefer
  the `gateway_red_*` metrics for dashboards. For rejected requests it is
  reduced to the first path segment (for example `/api`), or `/other`, so
  unauthenticated clients cannot create series at will.
- `route` keeps the first two path segments and replaces the rest with
  `...`, for example `/api/users/...`.
- `protocol` on `gateway_requests_total` is derived from the path (`mcp`,
  `a2a`, `ag_ui`, `acp`, `mqtt`, `websocket`, `grpc`, otherwise `http`). On
  the `gateway_red_*` metrics it is always `http`.
- `tenant` is the tenant id, or `_` when the key has no tenant.
- `status_class` is `2xx`, `3xx`, `4xx` or `5xx`.

Example lines:

```text
gateway_requests_total{method="GET",path="/api/users",protocol="http",status_code="200"} 8.0
gateway_requests_total{method="POST",path="/mcp/query",protocol="mcp",status_code="200"} 1.0
gateway_red_requests_total{method="GET",protocol="http",route="/api/users",status_class="2xx",tenant="acme"} 3.0
```

Rejected requests, from the end-to-end tutorial:

```text
gateway_requests_total{method="POST",path="/api",protocol="http",status_code="401"} 1.0
gateway_requests_total{method="GET",path="/api",protocol="http",status_code="429"} 4.0
```

## Health and SLO metrics

| Metric | Type | Labels | Updated when |
|---|---|---|---|
| `gateway_dependency_up` | gauge | `name`, `kind` | Each time a dependency probe runs: `/ready`, `/health/detailed`, `/admin/observability/dependencies`. 1 healthy, 0.5 degraded, 0 unhealthy. |
| `gateway_slo_availability` | gauge | `slo` | When SLOs are evaluated. Two SLOs are registered at start-up: `availability` (target 0.999) and `latency_p99` (target 0.99). |
| `gateway_slo_burn_rate` | gauge | `slo` | As above. |

SLOs are computed from requests seen by one process since it started.

## Registered but never updated

These are exported (with no samples, or zero) but no code changes them in this
release. Do not alert on them.

| Metric | Type | Labels |
|---|---|---|
| `gateway_circuit_breaker_state` | gauge | `backend_url` |
| `gateway_active_connections` | gauge | none |

To watch rate limiting, use `gateway_rate_limit_hits_total` or the
`rate_limit_exceeded` webhook ([Send events to a webhook](../how-to/configure-webhooks.md)).

## Custom metrics

Plugins and application code can create metrics through
`app.observability.custom_metrics` (`counter()`, `gauge()`, `histogram()`).
They appear on `/metrics` with the names you give them, and
`GET /admin/observability/metrics` lists their names.

## Standard collectors

The `prometheus_client` default collectors are also exported: `python_info`
and the `python_gc_*` metrics, and, on Linux, the `process_*` metrics (CPU,
memory, open file descriptors).

## Several processes

Each process has its own counters. With `uvicorn --workers N`, a scrape reaches
one worker; the multiprocess mode of `prometheus_client` is not configured.
Run one process per container and scrape each container, or accept that
counts are per worker.
