# Monitor the gateway

**Goal:** know whether the gateway is up, whether its backends are, and how
traffic behaves, using its health endpoints, Prometheus metrics and logs.

**Before you start:** the gateway is running. For Prometheus and Grafana, see
[Add monitoring](deploy-with-docker-compose.md#add-monitoring) or use your own.

## Contents

- [Health endpoints](#health-endpoints)
- [Prometheus](#prometheus)
- [Grafana](#grafana)
- [Logs](#logs)
- [Request log table and dashboards](#request-log-table-and-dashboards)
- [Tracing](#tracing)
- [Check that it worked](#check-that-it-worked)
- [If it did not work](#if-it-did-not-work)

## Health endpoints

| Endpoint | Use it for | Answers |
|---|---|---|
| `GET /health` (`HEALTH_CHECK_PATH`) | Liveness, load-balancer checks | Always 200 while the process serves requests. |
| `GET /live` | Liveness | 200 with the process id. |
| `GET /ready` | Readiness | 200 when the database answers, 503 otherwise. |
| `GET /health/detailed` | Diagnosis | Database, Redis, backends and log exporters; 503 if any is unhealthy. |

`/health`, `/live` and `/ready` never need a credential. `/health/detailed`
needs none either unless `OPS_ENDPOINTS_REQUIRE_ADMIN=true`, in which case it
answers 401 without `Authorization: Bearer <admin credential>`. Keep it off
the public internet; it lists your dependencies.

`/health/detailed` reports `unhealthy` when no configured backend answers, so
do not use it as a liveness or readiness probe: an outage of one backend would
take the gateway out of rotation.

The Docker image's `HEALTHCHECK` and the Compose healthcheck both call
`/health`.

## Prometheus

1. Scrape `/metrics`. The repository's `prometheus.yml`:

   ```yaml
   scrape_configs:
     - job_name: 'gateway'
       static_configs:
         - targets: ['gateway:8000']
       metrics_path: '/metrics'
       # With OPS_ENDPOINTS_REQUIRE_ADMIN=true the gateway answers 401 without the
       # admin credential. Put the token in a file mounted into this container and
       # uncomment:
       # authorization:
       #   type: Bearer
       #   credentials_file: /etc/prometheus/gateway_admin_token
   ```

2. Keep `/metrics` private. The shipped nginx serves it to loopback and
   private addresses only; port 8000 is not behind nginx. With
   `OPS_ENDPOINTS_REQUIRE_ADMIN=true` the gateway itself requires the admin
   credential: mount the token file and uncomment the `authorization:` block.

Useful queries, built from the [metrics reference](../reference/metrics.md):

```promql
# Requests per second by route and status class
sum by (route, status_class) (rate(gateway_red_requests_total[5m]))

# Share of 5xx responses
sum(rate(gateway_red_requests_total{status_class="5xx"}[5m]))
  / sum(rate(gateway_red_requests_total[5m]))

# 95th percentile latency by route
histogram_quantile(0.95, sum by (le, route) (rate(gateway_red_request_duration_seconds_bucket[5m])))

# Backend connection failures
sum by (error_type) (rate(gateway_errors_total[5m]))

# Dependencies down
gateway_dependency_up == 0
```

Three things to know:

- Rejected requests (401, 403, 429) are counted, with the `path` label of
  `gateway_requests_total` reduced to the first path segment (or `/other`).
  `gateway_rate_limit_hits_total` counts 429s from the authentication layer:
  `sum(rate(gateway_rate_limit_hits_total[5m]))`.
- `gateway_circuit_breaker_state` and `gateway_active_connections` are never
  updated. Do not alert on them.
- `gateway_dependency_up` is only updated when a probe runs (`/ready`,
  `/health/detailed`). Scraping `/metrics` alone does not refresh it; probe
  `/ready` from your orchestrator.

## Grafana

The `monitoring` profile starts Grafana without dashboards or data sources.
Add Prometheus as a data source (`http://prometheus:9090` on the Compose
network) and build panels from the queries above.

## Logs

The gateway writes one JSON object per line to stdout (`LOG_FORMAT=json`, the
default). Every request that passes authentication produces an `http_access`
line:

```json
{"http.request.method": "GET", "http.request.path": "/health", "http.request.query": "", "http.response.status_code": 200, "http.response.bytes": 93, "http.duration_ms": 0.773, "user_agent.original": "testclient", "http.referrer": null, "event": "http_access", "level": "info", "logger": "gateway", "@timestamp": "2026-10-20T17:59:41.115Z", "service.name": "AgentAnywhere Universal Gateway", "service.version": "1.0.0", "env": "production", "host.name": "example-host", "process.pid": 75400, "request_id": "4b784307-008f-45bd-8e82-de1fffa79bd3", "trace.id": "50c97aec0c5aeea41be4b03f142cf10f", "span.id": "2624ac91d057e56f", "client.ip": "testclient", "message": "http_access"}
```

(Captured in process with FastAPI's test client, so the user agent and client
address read `testclient`; the host name is replaced.)

Events worth alerting on:

| Event | Meaning |
|---|---|
| `backend_unavailable` | A backend could not be reached (502). |
| `backend_circuit_open` | A circuit breaker is open (503). |
| `gateway_error`, `unhandled_exception` | An unexpected error (500). |
| `mqtt_connection_failed` | The MQTT broker was not reachable at start-up. |
| `tenant_usage_record_failed`, `request_log_persist_failed` | Side writes failing (logged at debug level). |
| `plugin_bootstrap_failed`, `route_registry_init_failed`, `governance_load_failed` | Start-up steps that failed without stopping the gateway. |

Change the level at run time with `PUT /admin/config` and the body
`{"log_level": "DEBUG"}`, or `PUT /admin/logging` with `{"level": "DEBUG"}`. Write logs to a rotating file
with `LOG_FILE_PATH`, or rotate files with `scripts/rotate_logs.sh` (30 days,
100M by default). Ship them with the built-in exporters (Elasticsearch,
Splunk, S3, CloudWatch, Datadog; see the
[configuration reference](../reference/configuration.md#log-export)) or your
platform's log collector.

## Request log table and dashboards

- `GET /admin/logs` reads the `request_logs` table (see the
  [HTTP API reference](../reference/http-api.md#request-logs)).
- `GET /admin/dashboard/stats` summarises the last hour.
- `GET /admin/observability/dashboard` combines dependency status, SLOs and
  recent traces from the process that answers.

## Tracing

The gateway honours an incoming W3C `traceparent`, sends one to the backend,
and keeps recent spans in memory (`GET /admin/observability/traces`). To export
spans over OTLP/HTTP, set these in `.env` or the environment (the environment
wins):

```bash
OTEL_TRACING_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318/v1/traces
```

## Check that it worked

```bash
curl -s http://localhost:8000/ready
curl -s http://localhost:8000/metrics | grep -c '^gateway_'
```

In Prometheus, `up{job="gateway"}` is 1.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| `/metrics` answers 404 | `METRICS_ENABLED=false`, or a different `METRICS_PATH` | Check the settings. |
| Prometheus target is down | Wrong address, or the proxy blocks Prometheus | Scrape the gateway directly on the internal network. |
| Counts look low with several workers | Each worker has its own counters | See the [metrics reference](../reference/metrics.md#several-processes). |
| `/metrics` or `/health/detailed` answers 401 | `OPS_ENDPOINTS_REQUIRE_ADMIN=true` | Send the admin credential; configure Prometheus's `authorization:` block. |
| `/metrics` answers 403 through nginx | The scraper is outside nginx's allow-list | Add it to the `allow` lines, or scrape `gateway:8000` directly. |
