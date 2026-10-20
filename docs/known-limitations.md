# Known limitations

This page lists what does not work, or does not work the way you might expect,
in the current code of AgentAnywhere Universal Gateway. Each entry says what
happens, where in the code it comes from, and what to do about it. Read it
before you put the gateway in front of anything that matters.

Entries marked **reproduced** were run against the code for this page (in
process, with FastAPI's test client, or with Alembic against a scratch SQLite
file). The others were traced by reading the code.

## Contents

- [Data and storage](#data-and-storage)
- [Security defaults](#security-defaults)
- [Protocols](#protocols)
- [Limits and quotas](#limits-and-quotas)
- [Observability](#observability)
- [Routing behaviour](#routing-behaviour)
- [Admin API, console and CLI](#admin-api-console-and-cli)
- [Platform](#platform)

## Data and storage

### Alembic migrations do not match the models

**Reproduced.** The two migrations in `alembic/versions/` describe an older
schema than `app/db_models.py`.

- On a database the gateway has already started against, `alembic upgrade head`
  fails with `table api_keys already exists`, because the gateway creates its
  own tables at start-up (`init_db()` runs `Base.metadata.create_all`).
- On an empty database, `alembic upgrade head` succeeds, but the gateway then
  cannot create an API key: the insert fails with
  `table api_keys has no column named key`.
- Seven tables in the models have no migration at all.

**Workaround.** Do not use Alembic. Let the gateway create its schema on first
start. To record a revision without changing tables, `alembic stamp head`
is enough. For a release that changes a column, alter the table yourself with
SQL. See [Upgrade the gateway](how-to/upgrade.md) and the
[database reference](reference/database.md).

### Two setup scripts run or recommend the broken migrations

`scripts/setup_production.sh` runs `alembic upgrade head` when
`USE_POSTGRES=true`, and `scripts/setup_postgres.sh` tells you to run it.

**Workaround.** Do not set `USE_POSTGRES=true` for `setup_production.sh`, and
skip the migration step that `setup_postgres.sh` prints. The gateway creates
its tables on first start.

### With Docker, the SQLite file and every API key are lost when the container is recreated

`docker-compose.yml` sets `DATABASE_URL` to `sqlite+aiosqlite:///./gateway.db`
unless `.env` says otherwise, which puts the file at `/app/gateway.db` inside
the container. The Compose file mounts only `./logs`. `docker compose down`
followed by `up`, or any rebuild that recreates the container, starts with an
empty database.

**Workaround.** Use PostgreSQL (see
[Deploy with Docker Compose](how-to/deploy-with-docker-compose.md#use-postgresql-instead-of-sqlite)),
or copy the file out before you recreate the container:
`docker compose cp gateway:/app/gateway.db ./gateway.db.bak`. See
[Back up and restore](how-to/back-up-and-restore.md).

### Configuration made through some admin endpoints is kept in memory only

Webhooks, transformation rules, API versions, OAuth2 providers and their
tokens, and log exporters added through the admin API live in the memory of the
process that handled the request. They are lost on restart and are not shared
between worker processes or replicas. API keys, routes, tenants, usage
counters, request logs, settings profiles, governance policies, IP rules, admin
roles, OpenAPI registrations and the agent registry are stored in the database.

**Workaround.** Re-apply in-memory configuration at start-up from your own
deployment tooling, and run one process if you depend on it.

### Every request writes a row to `request_logs`

The logging middleware stores one row per request (except a few admin polling
paths and `/metrics`). Nothing prunes the table.

**Workaround.** Prune it yourself, for example
`DELETE FROM request_logs WHERE timestamp < <cutoff>`, on a schedule.

## Security defaults

### API keys are stored in plaintext

**Reproduced.** The `api_keys` table keeps each key in the `key` column, next
to its SHA-256 hash. `GET /admin/api-keys` returns every key in full. With
Redis enabled, keys also appear in Redis key names (`api_key:<key>` for the
key cache and `rate_limit:<key>:60` for counters). The gateway also forwards
the caller's `X-API-Key` and `Authorization` headers to the backend.

**Workaround.** Treat the database, Redis, database backups and the admin
credential as secrets. Keep Redis on a private network. If a backend must not
see gateway keys, strip the header with a transform plugin
([Write a plugin](how-to/write-a-plugin.md)).

### `SECRET_KEY` has three jobs

**Reproduced.** When it is not the shipped placeholder, `SECRET_KEY` is
accepted as a bearer credential on every admin endpoint, **even when
`ADMIN_TOKEN` is set**, and as an API key on every data-plane route, with the
default rate limits. Setting `ADMIN_TOKEN` changes only the console's sign-in
password. `SECRET_KEY` is described as a token-signing key, but no code in this
release signs anything with it.

**Workaround.** Treat `SECRET_KEY` as the credential that opens everything:
keep it in your secret store, never hand it to a client or an operator who
should not have full admin access, and rotate it if it leaks. Still set a
separate `ADMIN_TOKEN` for operators. See the
[security model](explanation/security-model.md#the-secret_key-overload).

### The admin credential is returned by the API

**Reproduced.** `GET /admin/config` returns `ADMIN_TOKEN` in clear text,
because `admin_token` is not in the list of redacted fields in
`app/config.py`. `POST /admin/auth/login` returns the admin credential itself
as the access token, and the console keeps it in the browser.

**Workaround.** Anyone who can call the admin API already holds the
credential, so this does not widen access by itself, but do not paste
`/admin/config` output into tickets or logs, and rotate `ADMIN_TOKEN` if it
leaks.

### Wildcard CORS with credentials is the shipped default

The built-in default for `CORS_ORIGINS` is
`["*", "http://localhost:3001", "http://localhost:3000"]`, `env.example`
ships `["*"]`, and `CORS_ALLOW_CREDENTIALS` is `true` in both.

**Workaround.** Set `CORS_ORIGINS` to the exact origins of your front ends,
written as a JSON array. See
[Secure the gateway for production](how-to/secure-for-production.md).

### nginx leaves `/metrics`, `/health/detailed` and the API schema public

In `nginx/nginx.conf` the IP allow-list in the `/metrics` block is commented
out, and `/health/detailed`, `/openapi.json`, `/docs` and `/redoc` go through
the catch-all `location /`. The gateway itself needs no credential for any of
them.

**Workaround.** Uncomment and fill in the allow-list, and add the same for
`/health/detailed`. See [Run behind nginx with TLS](how-to/run-behind-nginx-tls.md).

### `/docs` and `/redoc` are forwarded to a backend without a key

**Reproduced.** The interactive docs are only served when `DEBUG=true`. With
the default `DEBUG=false`, `/docs` and `/redoc` are still on the list of paths
that skip authentication, so a request to them falls through to the catch-all
proxy and is sent, without any API key check, to `HTTP_BACKEND_URL` (or
`MCP_BACKEND_URL` when that is unset).

**Workaround.** Block `/docs` and `/redoc` at the reverse proxy, or make sure
the default backend has nothing at those paths.

### API key path scoping (`allowed_paths`) is stored but not enforced

`POST /admin/api-keys?allowed_paths=/mcp,/a2a` stores the list and returns it,
but no code checks it on requests. A key with `allowed_paths` can call every
route.

**Workaround.** Use separate gateways, IP rules or a policy in
`/admin/governance/policies` if you need to restrict what a key can reach, and
check the result with a test request.

### A revoked or disabled key keeps working for up to five minutes when Redis is enabled

When `REDIS_ENABLED=true` and Redis is reachable (the Compose default), a
validated key is cached in Redis for 300 seconds. Cached keys are accepted
without checking `is_active` or `expires_at`, and deleting or toggling a key
does not clear the cache.

**Workaround.** After you revoke a key, delete `api_key:<key>` from Redis
database 0, or wait five minutes. See
[Create, rotate and revoke API keys](how-to/manage-api-keys.md#revoke-a-key).

### The client address is taken from `X-Forwarded-For` as sent

The gateway uses the first entry of the `X-Forwarded-For` header as the client
address for IP rules, policies, logs and metrics. A client can set that header
itself. The shipped `nginx/nginx.conf` uses `$proxy_add_x_forwarded_for`, which
keeps whatever the client sent and appends the real address, so the first
entry is still the client's choice.

**Workaround.** In the proxy directly in front of the gateway, overwrite the
header: `proxy_set_header X-Forwarded-For $remote_addr;`. Do not rely on IP
rules when clients can reach the gateway without going through that proxy. See
[Run behind nginx with TLS](how-to/run-behind-nginx-tls.md).

### Admin roles are stored but not enforced

`/admin/governance/roles` maps tokens to roles such as `viewer` and
`operator`, but the admin API does not consult them. Any valid admin
credential can call every admin endpoint.

**Workaround.** Treat every admin credential as full access.

### An auth-provider plugin bypasses rate limits and quotas

If an `AuthProviderPlugin` authenticates a request, the middleware passes it on
immediately: API-key checks, rate limits, tenant quotas, IP rules and policies
are skipped.

**Workaround.** Only enable auth-provider plugins (including the built-in
`static-token-auth`) when that is what you want.

### Credentials in URLs may be logged

The access log line includes the query string. A WebSocket key passed as
`?api_key=` and an admin credential passed to `/admin/agents/stream?token=`
can end up in logs.

**Workaround.** Prefer headers. Restrict who can read logs.

## Protocols

The full status of each protocol is in the
[protocol support matrix](explanation/protocol-support.md).

### WebSocket pass-through does not work

**Reproduced.** `/ws/{path}` closes every connection. Without a key it closes
with code 1008 and reason `API key required`; with a valid key it closes with
1008 and `Authentication failed`, because the route passes a stand-in request
object to the authentication code and the governance step fails on it. The
examples in `examples/basic_usage.py`, `examples/javascript_example.js` and the
console's protocol test page do not work for WebSocket.

**Workaround.** Connect clients to the WebSocket backend directly, or use the
agent edge (`/agents/<slug>/...`), which relays WebSocket frames but applies no
gateway credential. See [Configure routes and backends](how-to/configure-routes-and-backends.md).

### gRPC pass-through does not work

The JSON-to-gRPC bridge under `/grpc` tries server reflection with a package
that is not in `requirements.txt`, and only knows the sample `Greeter`
service otherwise. With the stock requirements it answers HTTP 200 with
`"status": "unsupported"`. The gRPC examples do not work.

**Workaround.** Call the gRPC service directly, or put an HTTP facade in front
of it and route to that.

### The MQTT client connects once, at start-up

`MQTTHandler.connect()` runs when `app.main` is imported. If the broker is not
reachable then, the client is never started, and `/mqtt/publish` answers 503
`MQTT broker not connected` until the gateway restarts.

**Workaround.** Start the broker before the gateway.

### The sample GraphQL URL in `env.example` points at the wrong port

`env.example` sets `GRAPHQL_BACKEND_URL=http://localhost:4000/graphql`, but the
sample GraphQL backend listens on port 8006.

**Workaround.** Set `GRAPHQL_BACKEND_URL=http://localhost:8006/graphql` to use
the sample.

## Limits and quotas

### Several limit settings are accepted but not enforced

- `PROTOCOL_*_RATE_LIMIT_PER_MINUTE` and `PROTOCOL_*_RATE_LIMIT_PER_HOUR` (for
  MCP, A2A, HTTP, MQTT, WebSocket, gRPC, AG-UI and ACP) are only reported by
  `GET /admin/settings/protocols`. Requests are limited per key, not per
  protocol.
- A policy whose action is `rate_limit` is evaluated, but its limit is not
  applied.
- A tenant's `requests_per_day` quota is stored and counted but not checked;
  only `requests_per_minute` and `requests_per_hour` are.

**Workaround.** Use per-key limits and tenant minute and hour quotas. See
[Set rate limits and tenant quotas](how-to/rate-limits-and-quotas.md).

### In-memory counters are per process, and the window depends on the store

Without `RATE_LIMIT_REDIS_URL`, each process counts on its own, so N processes
allow up to N times the limit. In memory, a window starts at the first request
and lasts 60 or 3600 seconds. In Redis, the counter's expiry is renewed on
every allowed request, so it resets 60 or 3600 seconds after the last allowed
request.

**Workaround.** Set `RATE_LIMIT_REDIS_URL` when you run more than one process.

### Tenant usage is counted after the response

Usage rows are written after the response is sent, so a burst of concurrent
requests can pass a tenant quota by a few requests.

## Observability

### Rejected requests are not counted

**Reproduced.** Requests that the authentication middleware rejects (401, 403
and 429 on the data plane) are answered before the logging middleware runs.
They do not appear in `gateway_requests_total`, are not written to
`request_logs`, carry no `X-Request-ID` header and have `"request_id": null` in
the body.

**Workaround.** Count 401 and 429 at your reverse proxy.

### Rate-limit alerts are never sent

**Reproduced.** When a key exceeds its per-minute limit, the rate limiter
builds the `rate_limit_exceeded` webhook payload, and the email and Slack
alert, from the key's `id`. The key object it holds has no `id`, the error is
swallowed, and nothing is sent. The 429 itself works.

**Workaround.** Alert on 429 responses counted at your reverse proxy.

### Some exported metrics are never updated

`gateway_rate_limit_hits_total`, `gateway_circuit_breaker_state` and
`gateway_active_connections` are registered and exported but no code updates
them. `gateway_requests_total` and `gateway_request_duration_seconds` are
labelled with the raw request path, which can create many series. See the
[metrics reference](reference/metrics.md).

### The `/admin/ai/*` analytics endpoints are experimental

Anomalies, scaling recommendations, cost analysis and traffic forecasts are
computed from request history held in memory. The request middleware feeds one
`AIService` instance (`app/main.py`), but the admin endpoints read a second,
separate instance (`admin/api.py`) that never sees traffic, so they answer
with empty results or "Insufficient data". Do not base decisions on them.

## Routing behaviour

### A backend that is not configured answers 500

**Reproduced.** A request under `/agui`, `/acp`, `/ws` or a `/mqtt` path other
than `publish` and `subscribe`, when the matching backend variable is unset,
answers 500 `INTERNAL_ERROR`. `/graphql` and `/grpc` answer 503 with a clearer
message.

### Paths with no protocol prefix go to a default backend

**Reproduced.** Any authenticated path that does not start with a known prefix
is proxied to `HTTP_BACKEND_URL`, or to `MCP_BACKEND_URL` when that is unset.

## Admin API, console and CLI

### Some CLI commands send the wrong request

**Reproduced for `keys create`.** `agentanywhere-gateway keys create` sends a
JSON body, but `POST /admin/api-keys` takes query parameters, so it fails with
422. `webhooks create` has the same problem. `logs --status` sends `status`,
which the endpoint ignores (it expects `status_code`), and `routes create
--methods` sends a field that routes do not have.

**Workaround.** Use `curl` for those operations. See the
[CLI reference](reference/cli.md).

### Key ids are only shown once, and the console cannot revoke keys

**Reproduced.** `DELETE /admin/api-keys/{key_id}` and
`PUT /admin/api-keys/{key_id}/toggle` take the key's `id`. The `id` is in the
response to `POST /admin/api-keys`, but `GET /admin/api-keys` does not return
it. The console's delete and enable/disable buttons send the key itself in
place of the id, and the gateway answers 404 `API key not found`.

**Workaround.** Record the `id` when you create a key. To find it later, read
it from the database (`SELECT id, name FROM api_keys;`). Revoke with `curl`.
See [Create, rotate and revoke API keys](how-to/manage-api-keys.md).

### The admin console needs a proxy in front of it

The console calls `/admin/*` on its own origin. Its nginx image proxies that to
`gateway:8000`, which exists only on the Compose network, and the Vite dev
server proxies it to `localhost:8000`. Served any other way, it needs an
equivalent proxy.

## Platform

### Python 3.13 is not supported

`pyproject.toml` requires Python 3.10 to 3.12, and several pinned dependencies
do not build on 3.13.

### List settings must be JSON arrays

**Reproduced for `PLUGINS_DISABLED`.** Settings typed as lists
(`CORS_ORIGINS`, `CORS_ALLOW_METHODS`, `CORS_ALLOW_HEADERS`, `PLUGIN_PATHS`,
`PLUGINS_DISABLED`, `MCP_BOUNDARY_ALLOWED_METHODS`,
`MCP_BOUNDARY_BLOCKED_METHODS`, `MCP_BOUNDARY_INJECTION_PATTERNS`) must be
written as JSON arrays, for example `PLUGINS_DISABLED=["audit-logger"]`. A
comma-separated value stops the gateway at start-up with
`error parsing value for field`.
