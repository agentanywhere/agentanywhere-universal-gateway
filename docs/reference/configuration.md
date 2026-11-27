# Configuration reference

Every environment variable that AgentAnywhere Universal Gateway reads, with its
default and what it does. The list is checked against the code by
`scripts/check_env_vars.py`, which fails if a variable is read but not listed
here, or listed but not read.

## Contents

- [How settings are loaded](#how-settings-are-loaded)
- [Value formats](#value-formats)
- [Notes used in the tables](#notes-used-in-the-tables)
- [Application and server](#application-and-server)
- [Credentials](#credentials)
- [CORS](#cors)
- [Rate limiting and Redis](#rate-limiting-and-redis)
- [Database](#database)
- [Backends](#backends)
- [Dynamic routes](#dynamic-routes)
- [Protocol boundaries and streaming](#protocol-boundaries-and-streaming)
- [Per-protocol settings](#per-protocol-settings)
- [Circuit breaker, metrics and health](#circuit-breaker-metrics-and-health)
- [Logging](#logging)
- [Tracing](#tracing)
- [Log export](#log-export)
- [Email and Slack notifications](#email-and-slack-notifications)
- [Alert thresholds](#alert-thresholds)
- [Plugins](#plugins)
- [OpenID Connect and the agent edge](#openid-connect-and-the-agent-edge)
- [Command-line tool](#command-line-tool)
- [Variables read by other files, not by the gateway](#variables-read-by-other-files-not-by-the-gateway)

## How settings are loaded

All gateway settings are fields of the `Settings` class in `app/config.py`
(pydantic-settings, no prefix, names are not case-sensitive). They are merged
in this order, later sources winning:

1. the defaults in `app/config.py`;
2. the file named by `CONFIG_FILE`, if set (`.json`, `.yaml`/`.yml` or `.toml`;
   keys are field names, either flat or grouped under the section names used
   by `GET /admin/config`);
3. the `.env` file in the **current working directory**;
4. the process environment.

Settings are read once, when the gateway starts. Restart after you change
them, except for the fields marked **live**, which `PUT /admin/config` can
change on a running process (see the [HTTP API reference](http-api.md)).

A few variables are read directly from the process environment rather than
through `Settings`; they are marked **env only**: `CONFIG_FILE`, the three
`LOG_EXPORT_BATCH_SIZE`/`_FLUSH_INTERVAL`/`_MAX_QUEUE_SIZE` values and the
CLI's variables. Neither `.env` nor `CONFIG_FILE` applies to them when you
start `uvicorn` yourself; export them in the shell. Under Docker Compose,
`env_file: .env` puts every line of `.env` into the container's environment,
so there they do apply. (The `OTEL_*` and `MCP_GATEWAY_PLUGIN_*` variables are
settings too, so `.env` applies to them; the process environment wins.)

## Value formats

| Type | Write it as |
|---|---|
| Boolean | `true` or `false` (also `1`/`0`, `yes`/`no`) |
| List | A JSON array (`CORS_ORIGINS=["https://a.example","https://b.example"]`) or a comma-separated value (`CORS_ORIGINS=https://a.example,https://b.example`). A value that starts with `[` must be valid JSON. |
| JSON object | A JSON object on one line, for example `AGENT_ROUTES_JSON={"orders":"http://10.0.0.10:8090"}` |
| URL | Must include a scheme (`http://`, `ws://` and so on) for `MCP_BACKEND_URL`, `A2A_BACKEND_URL`, `HTTP_BACKEND_URL`, `WEBSOCKET_BACKEND_URL`, `GRAPHQL_BACKEND_URL`, `ELASTICSEARCH_URL` and `SPLUNK_URL`; other URL settings are not validated. |

## Notes used in the tables

- **live**: can be changed at run time with `PUT /admin/config`.
- **secret**: redacted in `GET /admin/config`, `/admin/config/diff` and the
  config audit log. Every setting whose name ends in a secret word (`_TOKEN`,
  `_SECRET`, `_PASSWORD`, `_API_KEY` and similar) is redacted, and so is the
  password of any `user:password@` URL; `?redacted=false` returns raw values
  to an admin.
- **not enforced yet**: accepted and shown by the admin API, but no code acts
  on it in this release.
- **not read**: accepted, but no code reads it in this release.

<!-- env-vars:start -->

## Application and server

| Variable | Default | Description |
|---|---|---|
| `APP_NAME` | `AgentAnywhere Universal Gateway` | Title of the OpenAPI schema and `service.name` in logs. |
| `APP_VERSION` | `0.9.1` | Version reported by `/health`, `/health/detailed` and the OpenAPI schema. |
| `ENVIRONMENT` | `production` | Free text, lower-cased. With `production` and a placeholder `SECRET_KEY`, a warning is printed at start-up. Also used in log fields and to choose `https` for generated SDKs. |
| `DEBUG` | `false` | When `true`, serves `/docs`, `/redoc` and `/openapi.json`, and echoes SQL statements. With `false`, those three paths answer 404 from the gateway. |
| `HOST` | `0.0.0.0` | Bind address, used only when the gateway is started with `python -m app.main`. `uvicorn` on the command line takes `--host` instead. |
| `PORT` | `8000` | Port, used like `HOST`. `docker-compose.yml` also uses it for the host side of the port mapping. |
| `WORKERS` | `4` | Worker processes, used only by `python -m app.main` with `ENVIRONMENT=production`. The container image runs one process. |
| `GATEWAY_URL_BASE` | unset | Public base URL of the gateway, used in generated SDKs and the A2A Agent Card. Unset: built from `HOST` and `PORT`. |
| `CONFIG_FILE` | unset | **env only.** Path to a JSON, YAML or TOML settings file. A file that cannot be read prints a warning and is ignored. |
| `SSL_CERT_FILE` | unset | **env only.** A file of certificates to trust when a backend is behind TLS, read by the TLS libraries and by the data plane. The data plane relays to a `wss://` WebSocket backend only when this is set for both it and the gateway. |
| `SSL_CERT_DIR` | unset | **env only.** A directory of certificates to trust, read by the TLS libraries. With it set, the data plane speaks no TLS to backends and leaves them to the gateway. |

## Credentials

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `change-me-in-production-use-strong-random-key` | **secret.** Break-glass credential: the admin credential (and console password) only while `ADMIN_TOKEN` is empty, and an API key on the data plane only with `ALLOW_SECRET_KEY_API_KEY=true`. A value starting with `change-me` is treated as unset. Signs nothing. See the [security model](../explanation/security-model.md#the-secret_key-break-glass-credential). |
| `ADMIN_TOKEN` | empty | **secret.** The admin credential (`Authorization: Bearer`) and, when set, the console password. Once set, `SECRET_KEY` no longer opens the admin API. |
| `ADMIN_ACCEPT_SECRET_KEY` | `false` | `true` accepts `SECRET_KEY` on the admin API even when `ADMIN_TOKEN` is set, as earlier builds did. The placeholder is never accepted. |
| `ALLOW_SECRET_KEY_API_KEY` | `false` | `true` accepts `SECRET_KEY` as an API key on the data plane, for a fresh install with no keys yet. Every use is counted (`gateway_secret_key_api_key_uses_total`), logged (`secret_key_used_as_api_key`) and written to the governance audit log (`auth.secret_key_as_api_key`). The placeholder is never accepted. |
| `ADMIN_USERNAME` | `admin` | User name the console's sign-in form expects. |
| `API_KEY_HEADER` | `X-API-Key` | Header the gateway reads API keys from, before falling back to `Authorization: Bearer`. |
| `ALGORITHM` | `HS256` | **not read.** Nothing issues or signs tokens yet. |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `30` | **not read.** As above. |
| `TRUSTED_PROXIES` | `[]` | List of IP addresses or CIDR ranges of reverse proxies. `X-Forwarded-For` is read only when the connection comes from one of them, from the right; the last entry that is not a trusted proxy is the client. Empty: the header is ignored and the connection's address is the client. Example: `TRUSTED_PROXIES=10.0.0.5,172.18.0.0/16`. |

## CORS

| Variable | Default | Description |
|---|---|---|
| `CORS_ORIGINS` | `["http://localhost:3001", "http://127.0.0.1:3001"]` | Allowed origins; the default is the admin console on the same machine (also the value in `env.example`). If the list contains `"*"`, credentials are turned off and `cors_wildcard_credentials_disabled` is logged at start-up. |
| `CORS_ALLOW_CREDENTIALS` | `true` | Allow credentials on cross-origin requests. Ignored (treated as false) when `CORS_ORIGINS` contains `"*"`. |
| `CORS_ALLOW_METHODS` | `["*"]` | Allowed methods. |
| `CORS_ALLOW_HEADERS` | `["*"]` | Allowed request headers. |

## Rate limiting and Redis

| Variable | Default | Description |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | **live.** Turns per-key rate limiting on or off. Tenant quotas are not affected. |
| `RATE_LIMIT_PER_MINUTE` | `60` | **live.** Limit for principals without their own limit: `SECRET_KEY` used as a key, and OAuth2 tokens. Keys created through the admin API carry their own limits (default 60). |
| `RATE_LIMIT_PER_HOUR` | `1000` | **live.** As above, per hour. |
| `RATE_LIMIT_REDIS_URL` | unset | **secret.** Redis URL for rate-limit counters. Unset: counters live in each process's memory. `env.example` sets `redis://localhost:6379/1`. If Redis is unreachable, the gateway falls back to memory. |
| `REQUEST_LOG_RETENTION_DAYS` | `0` | Request log rows older than this many days are deleted. `0` keeps them forever. Per-minute tenant usage rows older than a day are always deleted; hourly and daily usage and the audit log are kept. |
| `RETENTION_INTERVAL_HOURS` | `24` | Hours between pruning runs. Pruning also runs at start-up. |
| `REDIS_URL` | `redis://localhost:6379/0` | **secret.** Redis URL for the API-key cache (entries keyed by the key's hash, at most five minutes, dropped when a key is deleted or disabled). |
| `REDIS_ENABLED` | `true` | Use `REDIS_URL` for the API-key cache. If Redis is unreachable, the cache is skipped. |
| `REDIS_SOCKET_TIMEOUT` | `1.0` | Seconds a Redis call may block. After a failed call the cache is skipped for 30 seconds, so a missing Redis does not slow requests. |

## Database

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | unset, which means `sqlite+aiosqlite:///./gateway.db` | **secret.** SQLAlchemy async URL. SQLite (`sqlite+aiosqlite:///path`) and PostgreSQL (`postgresql+asyncpg://user:password@host:5432/db`) drivers are in `requirements.txt`. A relative SQLite path is relative to the working directory. Under `docker-compose.yml` the default is `sqlite+aiosqlite:////app/data/gateway.db`, on the `gateway_data` volume; `env.example` leaves the line commented out so that default applies. Alembic uses the same value. |

## Backends

| Variable | Default | Description |
|---|---|---|
| `MCP_BACKEND_URL` | `http://localhost:8001` | Backend for `/mcp/*` and `/api/mcp/*`. Also the fallback for paths with no known prefix when `HTTP_BACKEND_URL` is unset. |
| `A2A_BACKEND_URL` | `http://localhost:8002` | Backend for `/a2a/*` and `/api/a2a/*`. |
| `AG_UI_BACKEND_URL` | unset | Backend for `/agui/*` and `/api/agui/*` (prefix stripped). Unset: those paths answer 500. |
| `ACP_BACKEND_URL` | unset | Backend for `/acp/*` and `/api/acp/*` (prefix stripped). Unset: those paths answer 500. |
| `HTTP_BACKEND_URL` | unset | Backend for `/api/*`, `/rest/*`, `/http/*` and every path with no known prefix. `env.example` sets `http://localhost:8003`. |
| `GRAPHQL_BACKEND_URL` | unset | GraphQL server, or its endpoint, that requests under `/graphql` are passed on to. `env.example` sets `http://localhost:4000/graphql`; the sample backend is on port 8006. |
| `MQTT_BROKER_URL` | unset | Broker for `/mqtt/publish` and `/mqtt/subscribe`, for example `mqtt://localhost:1883` (`mqtts://` for TLS). The gateway connects when it starts. |
| `WEBSOCKET_BACKEND_URL` | unset | The WebSocket server `/ws/*` connections are relayed to. Give a `ws://` or `wss://` address; see the [protocol support matrix](../explanation/protocol-support.md). |
| `GRPC_BACKEND_URL` | unset | The gRPC server, as `host:port`, for example `localhost:50051`. The Rust data plane relays gRPC calls to it; the Python gateway's own JSON-to-gRPC bridge does not work. See the [protocol support matrix](../explanation/protocol-support.md). |
| `DEFAULT_TIMEOUT` | `30` | Seconds the gateway waits for a buffered backend response. Also the timeout of agent-edge requests. |

These values are for a gateway running directly on a machine. Under the
repository's `docker-compose.yml`, the gateway container does not take the
`*_BACKEND_URL` lines from `.env`: Compose sets them to the sample backends'
service names, or to the `DOCKER_*_BACKEND_URL` values you set (see
[Variables read by other files](#variables-read-by-other-files-not-by-the-gateway)).
`AG_UI_BACKEND_URL` and `ACP_BACKEND_URL` have no sample service and are read
from `.env` as usual. Inside a container, `localhost` is the container itself.

## Dynamic routes

| Variable | Default | Description |
|---|---|---|
| `DYNAMIC_ROUTES_ENABLED` | `true` | Honour routes registered with `POST /admin/routes`. `false`: prefix routing only. |
| `DYNAMIC_ROUTES_REFRESH_SECONDS` | `15` | How often each process reloads the route table from the database. |

## Protocol boundaries and streaming

| Variable | Default | Description |
|---|---|---|
| `MCP_BOUNDARY_ENABLED` | `true` | **live.** Validate `POST` bodies under `/mcp`. |
| `MCP_BOUNDARY_MAX_BODY_BYTES` | `2097152` | **live.** Largest MCP body accepted; larger answers 400. |
| `MCP_BOUNDARY_ALLOWED_METHODS` | unset | List. If set, only these JSON-RPC methods pass. |
| `MCP_BOUNDARY_BLOCKED_METHODS` | `[]` | List. JSON-RPC methods that are refused. |
| `MCP_BOUNDARY_PROMPT_INJECTION_ACTION` | `block` | **live.** `block` answers 400 when a string matches an injection pattern; `audit` only logs it. |
| `MCP_BOUNDARY_INJECTION_PATTERNS` | unset | List of regular expressions. Unset: a built-in list in `app/tier1_mcp.py`. |
| `A2A_BOUNDARY_ENABLED` | `true` | **live.** Validate A2A `POST` bodies. |
| `A2A_ENFORCE_SEND_MESSAGE_SHAPE` | `true` | **live.** Check the shape of A2A send-message bodies. |
| `ACP_BOUNDARY_ENABLED` | `true` | Validate ACP bodies. |
| `AG_UI_BOUNDARY_ENABLED` | `true` | Validate AG-UI bodies. |
| `TIER2_STREAM_CONNECT_TIMEOUT` | `60.0` | Seconds to connect for streamed requests (AG-UI, ACP runs, A2A `message:stream`, agent edge). |
| `TIER2_STREAM_READ_TIMEOUT` | unset (no limit) | Seconds between chunks for streamed requests. |

## Per-protocol settings

The rate-limit values in this section are **not enforced yet**. They are shown by
`GET /admin/settings/protocols`. Requests are limited per key.

| Variable | Default | Description |
|---|---|---|
| `PROTOCOL_MCP_RATE_LIMIT_PER_MINUTE` | `60` | **not enforced yet.** |
| `PROTOCOL_MCP_RATE_LIMIT_PER_HOUR` | `1000` | **not enforced yet.** |
| `PROTOCOL_A2A_RATE_LIMIT_PER_MINUTE` | `60` | **not enforced yet.** |
| `PROTOCOL_A2A_RATE_LIMIT_PER_HOUR` | `1000` | **not enforced yet.** |
| `PROTOCOL_HTTP_RATE_LIMIT_PER_MINUTE` | `100` | **not enforced yet.** |
| `PROTOCOL_HTTP_RATE_LIMIT_PER_HOUR` | `5000` | **not enforced yet.** |
| `PROTOCOL_MQTT_RATE_LIMIT_PER_MINUTE` | `200` | **not enforced yet.** |
| `PROTOCOL_MQTT_RATE_LIMIT_PER_HOUR` | `10000` | **not enforced yet.** |
| `PROTOCOL_WEBSOCKET_RATE_LIMIT_PER_MINUTE` | `300` | **not enforced yet.** |
| `PROTOCOL_WEBSOCKET_RATE_LIMIT_PER_HOUR` | `15000` | **not enforced yet.** |
| `PROTOCOL_GRPC_RATE_LIMIT_PER_MINUTE` | `150` | **not enforced yet.** |
| `PROTOCOL_GRPC_RATE_LIMIT_PER_HOUR` | `8000` | **not enforced yet.** |
| `PROTOCOL_AG_UI_RATE_LIMIT_PER_MINUTE` | `120` | **not read.** |
| `PROTOCOL_AG_UI_RATE_LIMIT_PER_HOUR` | `6000` | **not read.** |
| `PROTOCOL_ACP_RATE_LIMIT_PER_MINUTE` | `120` | **not read.** |
| `PROTOCOL_ACP_RATE_LIMIT_PER_HOUR` | `6000` | **not read.** |
| `PROTOCOL_MQTT_DEFAULT_PORT` | `1883` | Broker port when `MQTT_BROKER_URL` has none. |
| `PROTOCOL_MQTT_KEEP_ALIVE` | `60` | Shown by the admin API only; the client uses 60 seconds. |
| `PROTOCOL_WEBSOCKET_MAX_CONNECTIONS` | `500` | Shown by the admin API only. |
| `PROTOCOL_WEBSOCKET_KEEP_ALIVE` | `30` | Shown by the admin API only. |
| `PROTOCOL_GRPC_KEEPALIVE_TIME_MS` | `30000` | gRPC channel keepalive option. |
| `PROTOCOL_GRPC_KEEPALIVE_TIMEOUT_MS` | `5000` | gRPC channel keepalive option. |
| `PROTOCOL_GRPC_MIN_TIME_BETWEEN_PINGS_MS` | `10000` | gRPC channel option. |
| `PROTOCOL_GRPC_MIN_PING_INTERVAL_WITHOUT_DATA_MS` | `300000` | gRPC channel option. |

## Circuit breaker, metrics and health

| Variable | Default | Description |
|---|---|---|
| `CIRCUIT_BREAKER_ENABLED` | `true` | **live.** One breaker per backend URL, per process. |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD` | `5` | **live.** Consecutive failures that open the breaker. While open, requests answer 503. |
| `CIRCUIT_BREAKER_TIMEOUT` | `60` | **live.** Seconds before an open breaker lets a request through again. |
| `METRICS_ENABLED` | `true` | **live.** Record and serve Prometheus metrics. `false`: the metrics path answers 404. |
| `METRICS_PATH` | `/metrics` | Path of the metrics endpoint. Public unless `OPS_ENDPOINTS_REQUIRE_ADMIN=true`. |
| `OPS_ENDPOINTS_REQUIRE_ADMIN` | `false` | `true`: `METRICS_PATH` and `/health/detailed` answer 401 without `Authorization: Bearer <admin credential>` (the same check as the admin API). `/health`, `/live` and `/ready` stay public. |
| `HEALTH_CHECK_PATH` | `/health` | Path of the liveness endpoint. Public. |

## Logging

| Variable | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | **live.** `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `LOG_FORMAT` | `json` | **live.** `json`, `text`, or `auto` (text on a terminal, JSON otherwise). |
| `LOG_SAMPLING_RATE` | `1.0` | **live.** Fraction of INFO lines kept, 0.0 to 1.0. |
| `LOG_REDACTION_ENABLED` | `true` | **live.** Redact sensitive fields and values in log lines (see `DEFAULT_REDACT_KEYS` and `DEFAULT_REDACT_PATTERNS` in `app/logging_config.py`). |
| `LOG_INCLUDE_CALLER` | `false` | **live.** Add file, line and function to each line. |
| `LOG_FILE_PATH` | unset | Also write JSON logs to this rotating file. |
| `LOG_AUDIT_FILE_PATH` | unset | Write audit log lines to this rotating file. |
| `LOG_LEVEL_OVERRIDES` | unset | Per-logger levels as `logger=LEVEL` pairs, comma-separated, for example `uvicorn.access=WARNING,httpx=WARNING`. Bad entries are skipped and reported in a `logging_config_invalid` line. |
| `LOG_REDACT_KEYS` | unset | Extra field names to redact, comma-separated. Adds to the built-in `DEFAULT_REDACT_KEYS`. |
| `LOG_REDACT_PATTERNS` | unset | Extra regular expressions to scrub from values, comma-separated. Adds to the built-in patterns; an invalid regex is skipped and reported. |
| `LOG_FILE_MAX_BYTES` | `104857600` | Rotate `LOG_FILE_PATH` and `LOG_AUDIT_FILE_PATH` at this size. |
| `LOG_FILE_BACKUP_COUNT` | `10` | Rotated files to keep. |
| `LOG_INCLUDE_HOSTNAME` | `true` | Add `host.name` to every line. |
| `LOG_INCLUDE_PROCESS_INFO` | `true` | Add `process.pid` to every line. |
| `LOG_TIMESTAMP_FORMAT` | `iso` | `iso` (UTC, milliseconds) or `epoch` (seconds). |
| `LOG_SAMPLE_ALWAYS_STATUS_GE` | `400` | Access logs with a status at or above this are never sampled out. |
| `STARTUP_BANNER` | `true` | Print a configuration summary to stderr when the gateway is ready. It shows URLs and which admin credential applies, never a secret. Each worker process prints its own. |
| `REQUEST_LOG_MODE` | `batched` | Where request-log rows go. `batched`: rows go into a bounded queue and one background task writes them in batches (one transaction each); when the queue is full the newest row is dropped and counted, and rows still queued are lost if the process is killed. `inline`: one write and commit per request (the default before 0.9.2); every row is written before the next request's, at the cost of a database transaction per request. `off`: no rows are written, so the console's log views stay empty. |
| `REQUEST_LOG_BATCH_SIZE` | `200` | `batched` mode: most rows per transaction. |
| `REQUEST_LOG_FLUSH_INTERVAL` | `0.25` | `batched` mode: longest wait, in seconds, before a partly filled batch is written. |
| `REQUEST_LOG_QUEUE_SIZE` | `20000` | `batched` mode: rows held in memory before new ones are dropped. |
| `SQLITE_JOURNAL_MODE` | `WAL` | SQLite only: `WAL`, `DELETE`, `TRUNCATE`, `PERSIST`, `MEMORY` or `OFF`, applied to every new connection. Empty keeps SQLite's own default (`DELETE`, the default before 0.9.2). `WAL` lets readers and the writer work at once and avoids most "database is locked" errors with several workers; it keeps `-wal` and `-shm` files next to the database, and needs a local disk (not a network file system). |
| `SQLITE_SYNCHRONOUS` | `NORMAL` | SQLite only: `OFF`, `NORMAL`, `FULL` or `EXTRA`. Empty keeps SQLite's own default (`FULL`, the default before 0.9.2). `NORMAL` with `WAL` is the usual pairing; a power loss can then lose the last commits but not corrupt the file. |
| `SQLITE_BUSY_TIMEOUT_MS` | `5000` | SQLite only: how long a connection waits for a lock, in milliseconds. `0` keeps the driver's default of five seconds. |
| `API_KEY_LAST_USED_INTERVAL_SECONDS` | `60` | A key's last-used time is written at most once per this many seconds, instead of on every request (before 0.9.2 every request wrote and committed it). `0` writes on every use. The time shown for a key can be up to this much older than its latest use. |
| `API_KEY_LOCAL_CACHE_TTL_SECONDS` | `0` (off) | Seconds a validated API key, and the tenant it belongs to, stay cached in each worker's memory when Redis is not used. While cached, authenticating a request needs no database read or write. A key revoked or changed through another worker can still be accepted here until the entry expires; changes made through the same worker take effect at once. |
| `DATAPLANE_TOKEN` | unset | Shared secret for the experimental Rust data plane (`dataplane/`). When set, `GET /internal/dataplane/snapshot` serves the data plane its keys (SHA-256 digests only) and limits, and requires this value in `X-Dataplane-Token`. Unset switches the endpoint off. Serve it only on an internal port. |
| `JSON_CODEC` | `stdlib` | Serialiser for response bodies: `stdlib`, or `orjson` (much faster; needs `pip install orjson`; `NaN` and infinity become `null` instead of raising). Applies to responses FastAPI builds from a handler's return value. A `module:attribute` path loads your own; see [Adapters](../explanation/adapters.md). |
| `UPSTREAM_CLIENT` | `httpx` | The client that forwards requests to backends. `httpx`, or a `module:attribute` path to a factory returning an object with the same `request`, `build_request`, `send` and `aclose` methods. |
| `ANALYTICS_ADAPTER` | `full` | `full` runs the built-in anomaly detection and usage analytics, `off` runs none, or a `module:attribute` path to your own. `AI_ANALYTICS_ENABLED=false` still turns the per-request call off whichever adapter is chosen. |
| `REQUEST_LOG_SINK` | `database` | Where request-log entries go: `database` (see `REQUEST_LOG_MODE`), `null` (discard), or a `module:attribute` path to your own. |
| `AI_ANALYTICS_ENABLED` | `true` | Run the anomaly detection and usage analytics. With `AI_ANALYTICS_MODE=background` (the default) they run off the request path; set `false` to save their CPU. |
| `AI_ANALYTICS_MODE` | `background` | `background`: each request's facts are queued and a background task scores them in batches (one model call per batch), so scoring never holds up a response; anomaly logs, webhooks, alerts and the AI pages work as before, and a full queue drops its oldest entry. `inline`: scored before the response is sent (the behaviour before 0.9.2), which adds `X-Anomaly-Detected` and `X-Anomaly-Score` to an anomalous response at a cost of milliseconds of CPU per request. |
| `AI_ANALYTICS_BATCH_SIZE` | `500` | `background` mode: most requests scored per batch. |
| `AI_ANALYTICS_FLUSH_INTERVAL` | `0.25` | `background` mode: seconds between batches. |
| `AI_ANALYTICS_QUEUE_SIZE` | `10000` | `background` mode: requests held for scoring before the oldest are dropped. |
| `DOCUMENTATION_URL` | `https://docs.agentanywhere.ai/gateway/` | The documentation link in the start-up banner. Set it to your own docs site. |

## Tracing

Settings; the process environment wins over `.env`.

| Variable | Default | Description |
|---|---|---|
| `OTEL_TRACING_ENABLED` | `false` | Export spans over OTLP/HTTP. Spans are always kept in an in-process buffer for `/admin/observability/traces`. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | Collector endpoint, for example `http://otel-collector:4318/v1/traces`. |
| `OTEL_EXPORTER_OTLP_HEADERS` | unset | Comma-separated `key=value` headers for OTLP requests. |
| `OTEL_SERVICE_NAME` | `agentanywhere-gateway` | Service name on spans. |

## Log export

| Variable | Default | Description |
|---|---|---|
| `LOG_EXPORT_ENABLED` | `false` | Start the batching export worker at start-up when at least one exporter is configured. |
| `LOG_EXPORT_BATCH_SIZE` | `100` | **env only.** Entries per batch. |
| `LOG_EXPORT_FLUSH_INTERVAL` | `5.0` | **env only.** Seconds between flushes. |
| `LOG_EXPORT_MAX_QUEUE_SIZE` | `10000` | **env only.** Most entries held in the queue. |
| `ELASTICSEARCH_ENABLED` | `false` | Export to Elasticsearch. |
| `ELASTICSEARCH_URL` | unset | Elasticsearch URL. |
| `ELASTICSEARCH_INDEX` | `gateway-logs` | Index name. |
| `SPLUNK_ENABLED` | `false` | Export to Splunk HEC. |
| `SPLUNK_URL` | unset | Splunk HEC URL. |
| `SPLUNK_TOKEN` | unset | **secret.** Splunk HEC token. |
| `SPLUNK_INDEX` | `gateway` | Splunk index. |
| `S3_LOG_EXPORT_ENABLED` | `false` | Archive logs to S3 as gzipped NDJSON. Needs `boto3`, which is not in `requirements.txt`. |
| `S3_LOG_BUCKET` | unset | Bucket. |
| `S3_LOG_PREFIX` | `gateway-logs/` | Key prefix. |
| `S3_LOG_REGION` | `us-east-1` | Region. |
| `CLOUDWATCH_LOGS_ENABLED` | `false` | Export to CloudWatch Logs. Needs `boto3`. |
| `CLOUDWATCH_LOG_GROUP` | unset | Log group. |
| `CLOUDWATCH_LOG_STREAM` | unset | Log stream. |
| `CLOUDWATCH_REGION` | `us-east-1` | Region. |
| `AWS_ACCESS_KEY_ID` | unset | **secret.** For S3 and CloudWatch. Unset: the default AWS credential chain. |
| `AWS_SECRET_ACCESS_KEY` | unset | **secret.** As above. |
| `DATADOG_LOGS_ENABLED` | `false` | Export to Datadog. |
| `DATADOG_API_KEY` | unset | **secret.** Datadog API key. |
| `DATADOG_SITE` | `datadoghq.com` | Datadog site. |
| `DATADOG_SERVICE` | `agentanywhere-gateway` | Service tag. `env.example` sets `mcp-gateway`. |
| `DATADOG_SOURCE` | `python` | Source tag. |
| `DATADOG_TAGS` | empty | Comma-separated tags. |

## Email and Slack notifications

| Variable | Default | Description |
|---|---|---|
| `EMAIL_NOTIFICATIONS_ENABLED` | `false` | Send alert emails. Needs `SMTP_HOST`. |
| `SMTP_HOST` | unset | SMTP server. |
| `SMTP_PORT` | `587` | SMTP port. |
| `SMTP_USER` | unset | SMTP user name. |
| `SMTP_PASSWORD` | unset | **secret.** SMTP password. |
| `SMTP_USE_TLS` | `true` | Use STARTTLS. |
| `SMTP_FROM_EMAIL` | unset | Sender address. |
| `EMAIL_ALERT_RECIPIENTS` | empty | Comma-separated recipients. (This is a plain string, not a JSON list.) |
| `EMAIL_RATE_LIMIT_PER_HOUR` | `50` | Most alert emails per hour. |
| `SLACK_NOTIFICATIONS_ENABLED` | `false` | Send alerts to a Slack incoming webhook. Needs `SLACK_WEBHOOK_URL`. |
| `SLACK_WEBHOOK_URL` | unset | **secret.** Incoming webhook URL. |
| `SLACK_CHANNEL` | unset | Channel override. |
| `SLACK_USERNAME` | `AgentAnywhere Gateway` | Display name. |
| `SLACK_ICON_EMOJI` | `:rotating_light:` | **not read.** |
| `SLACK_RATE_LIMIT_PER_HOUR` | `60` | Most Slack messages per hour. |

## Alert thresholds

All are **live**.

| Variable | Default | Description |
|---|---|---|
| `ALERT_THRESHOLD_RATE_LIMIT` | `true` | Send an alert when a key exceeds its per-minute or per-hour limit. |
| `ALERT_THRESHOLD_CIRCUIT_BREAKER` | `true` | Send an alert when a circuit breaker opens or closes. |
| `ALERT_THRESHOLD_ANOMALY_SCORE` | `0.7` | Lowest anomaly score that triggers an alert. |
| `ALERT_THRESHOLD_ERROR_RATE` | `0.1` | Error rate that counts as high. |
| `ALERT_THRESHOLD_TRAFFIC_SPIKE` | `2.0` | Traffic multiple that counts as a spike. |
| `ALERT_THRESHOLD_MIN_INTERVAL_SECONDS` | `300` | Least time between two alerts of the same kind. |

## Plugins

| Variable | Default | Description |
|---|---|---|
| `PLUGINS_ENABLED` | `true` | Load plugins at start-up and run them on requests. |
| `PLUGIN_PATHS` | `[]` | List of directories to load plugin modules from. |
| `PLUGINS_DISABLED` | `[]` | List of plugin names not to load, for example `audit-logger,header-injector`. |
| `PLUGIN_CONFIGS_JSON` | unset | JSON object of plugin name to configuration object, for example `{"header-injector": {"enabled": true}}`. |
| `MCP_GATEWAY_PLUGIN_PATH` | empty | Extra plugin directories, separated by `:` (`;` on Windows). Read from `.env` or the environment. Legacy name, kept on purpose. |
| `MCP_GATEWAY_PLUGIN_DISABLED` | empty | Extra plugin names not to load, comma-separated. Read from `.env` or the environment. Legacy name, kept on purpose. |

## OpenID Connect and the agent edge

Off unless set. OpenID Connect sign-in turns on when `IDA_ISSUER`,
`IDA_BROWSER_ISSUER` or `IDA_ISSUER_BASE` is set.

| Variable | Default | Description |
|---|---|---|
| `IDA_ISSUER` | unset | Issuer URL of the OpenID Connect provider, for example `https://sso.example.com/realms/demo`. The admin API then also accepts RS256 access tokens from it. |
| `IDA_ISSUER_BASE` | unset | Base URL for several realms, for example `https://sso.example.com/realms`. |
| `IDA_BROWSER_ISSUER` | unset | Issuer URL as the browser sees it, if different from `IDA_ISSUER`. |
| `IDA_JWKS_BASE` | unset | Where the gateway fetches signing keys, if different from the issuer. |
| `IDA_JWKS_CACHE_SECONDS` | `3600` | How long signing keys are cached. |
| `IDA_LEEWAY_SECONDS` | `30` | **not read.** |
| `IDA_ADMIN_AUDIENCE` | unset, which means `gateway-admin` | Client id an admin token must be issued for. |
| `IDA_ADMIN_ROLES` | unset, which means `platform_admin,tenant_admin` | Realm roles, comma-separated or a JSON list; the user must hold one. An empty list (`[]`) denies every user. |
| `IDA_AUDIENCE` | unset | Default audience recorded for agent-edge routes (otherwise the slug). The gateway does not validate tokens on the agent edge. |
| `AGENT_EDGE_ENABLED` | `false` | Serve `/agents/<slug>/...`. Agent routes stored in the database also turn it on. |
| `AGENT_ROUTES_JSON` | unset | JSON object of slug to upstream base URL. Seeds the agent registry on first start. |
| `AGENT_PUBLIC_PATHS_JSON` | unset | JSON object of slug to a list of path prefixes, recorded with each agent. |

## Command-line tool

Read by `agentanywhere-gateway`, not by the gateway. All are **env only**.

| Variable | Default | Description |
|---|---|---|
| `AGENT_GATEWAY_URL` | `http://localhost:8000` | Gateway the CLI talks to. |
| `MCP_GATEWAY_URL` | unset | Legacy name for `AGENT_GATEWAY_URL`, used when that is unset. |
| `AGENT_GATEWAY_CONFIG` | `~/.config/agent-universal-gateway/config.json` | Where `login` caches the token. |
| `MCP_GATEWAY_CONFIG` | unset | Legacy name for `AGENT_GATEWAY_CONFIG`. |
| `MCP_GATEWAY_TOKEN` | unset | Admin credential, used when `--token` is not given. There is no `AGENT_GATEWAY_TOKEN`. |
| `NO_COLOR` | unset | Any value turns off coloured output. |

<!-- env-vars:end -->

## Variables read by other files, not by the gateway

These appear in `env.example` or `docker-compose.yml` but the gateway does not
read them.

| Variable | Read by | Meaning |
|---|---|---|
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_PORT` | `docker-compose.yml` (`postgres` service), `scripts/setup_postgres.sh` | Database name, user, password and host port. The `postgres` container refuses to start while `POSTGRES_PASSWORD` is empty or starts with `change-me`. The gateway reads only `DATABASE_URL`. |
| `GRAFANA_ADMIN_PASSWORD` | `docker-compose.yml` (`grafana` service) | Grafana admin password. The `grafana` container refuses to start while it is empty or `admin`. |
| `ADMIN_UI_PORT`, `ADMIN_UI_URL`, `NGINX_ENABLED`, `NGINX_HTTP_PORT`, `NGINX_HTTPS_PORT`, `SSL_CERT_PATH`, `SSL_KEY_PATH`, `PROMETHEUS_ENABLED`, `PROMETHEUS_PORT`, `GRAFANA_ENABLED`, `GRAFANA_PORT` | Nothing in this repository | Placeholders in `env.example` for your own tooling. |
| `BIND_ADDRESS` | `docker-compose.yml` (every published port except nginx's 80 and 443) | The address Compose publishes ports on; default `127.0.0.1`. `0.0.0.0` opens them to the network and bypasses nginx for the gateway. |
| `DOCKER_MCP_BACKEND_URL`, `DOCKER_A2A_BACKEND_URL`, `DOCKER_HTTP_BACKEND_URL`, `DOCKER_WEBSOCKET_BACKEND_URL`, `DOCKER_MQTT_BROKER_URL`, `DOCKER_GRPC_BACKEND_URL`, `DOCKER_GRAPHQL_BACKEND_URL` | `docker-compose.yml` (`gateway` service) | Backend URLs for the containerised gateway. Compose sets the gateway's `*_BACKEND_URL` from these, defaulting to the sample backends' service names, and ignores the `*_BACKEND_URL` lines of `.env`. |
| `POSTGRES_WAIT_SECONDS` | `scripts/setup_postgres.sh` | How long the script waits for PostgreSQL (default 60). |
| `GATEWAY_TEST_POSTGRES_URL` | `tests/test_migrations.py` | An empty, disposable PostgreSQL database for the migration tests (they drop every table in it). |
| `VITE_BASE_PATH`, `VITE_API_URL` | The admin console build (`admin-ui/`) | Path prefix the console is served under, and an optional API base URL. |
| `GATEWAY_URL`, `REQUIRE_INTEGRATION` | `tests/test_container_integration.py` | Where the live-server tests look for a gateway, and whether a missing gateway fails instead of skipping. |
