# Design decisions and trade-offs

This page explains the choices visible in the code of AgentAnywhere Universal
Gateway, what each one buys, and what it costs. It describes the current
release; it is not a roadmap.

## Contents

- [One Python process does everything](#one-python-process-does-everything)
- [Routing by path prefix, with optional dynamic routes](#routing-by-path-prefix-with-optional-dynamic-routes)
- [Authentication in a middleware, admin auth per endpoint](#authentication-in-a-middleware-admin-auth-per-endpoint)
- [The database creates itself, and Alembic agrees](#the-database-creates-itself-and-alembic-agrees)
- [Redis is optional](#redis-is-optional)
- [Running more than one worker or replica](#running-more-than-one-worker-or-replica)
- [Buffer by default, stream when asked](#buffer-by-default-stream-when-asked)
- [Fail open on side work, fail closed on credentials](#fail-open-on-side-work-fail-closed-on-credentials)
- [Configuration from the environment](#configuration-from-the-environment)
- [Legacy names kept on purpose](#legacy-names-kept-on-purpose)

## One Python process does everything

The data plane, the admin API, metrics, health checks and background tasks all
run in one FastAPI application.

- **Buys:** one thing to deploy, one port, one configuration, and an admin API
  that can change the running process (routes, plugins, logging levels).
- **Costs:** a slow admin call or a busy event loop affects proxied traffic,
  and the admin API is reachable wherever the data plane is unless you block
  `/admin` at a proxy. Throughput is that of a Python ASGI application that
  reads most responses fully into memory; the repository publishes no
  benchmark.

## Routing by path prefix, with optional dynamic routes

The backend is chosen from the start of the path (`/mcp`, `/a2a`, `/api`, and
so on), with one URL per protocol in the settings. A route registered through
`POST /admin/routes` takes precedence: exact matches beat wildcards, and the
longest wildcard prefix wins.

- **Buys:** zero-configuration routing for one backend per protocol, and
  run-time routes without a restart.
- **Costs:** one backend per protocol unless you add dynamic routes; unknown
  paths fall through to the HTTP (or MCP) backend instead of 404; and an unset
  backend gives 500 for some prefixes.

## Authentication in a middleware, admin auth per endpoint

The data plane is protected by one HTTP middleware that runs before
everything else. The admin plane is protected by a FastAPI dependency on each
endpoint.

- **Buys:** every proxied path is covered by default, including paths no one
  thought of.
- **Costs:** the middleware has to know which paths are public, and must
  answer the ones that are not served (`/docs`, `/redoc` and `/openapi.json`
  with `DEBUG=false`) itself, or they would fall through to the catch-all
  proxy. Its rejections have to be marked (`_auth_reject`) so that the logging
  middleware, which runs outside it, records them with bounded metric labels.
  WebSocket routes are outside HTTP middleware and must authenticate
  themselves, which `/ws` does not manage to do.

## The database creates itself, and Alembic agrees

At start-up the gateway calls SQLAlchemy's `create_all`, which creates missing
tables and leaves existing ones alone. The Alembic baseline creates the same
schema and is safe on a database `create_all` already built; a test fails when
the two disagree.

- **Buys:** a new installation works with no migration step, on SQLite or
  PostgreSQL, and upgrades that change a table have a migration path.
- **Costs:** two mechanisms to keep in step (the test enforces it), and you
  must remember to run `alembic upgrade head` when a release changes a table:
  `create_all` alone will not. See [Upgrade the gateway](../how-to/upgrade.md).

## Redis is optional

Without Redis, rate-limit counters live in process memory and there is no
API-key cache. With Redis, counters are shared and validated keys are cached
for up to five minutes (the entry is dropped when a key is deleted or
disabled). If Redis is configured but unreachable, rate limiting falls
back to memory instead of failing.

- **Buys:** the gateway runs on a laptop with nothing else installed, and a
  Redis outage does not take traffic down.
- **Costs:** silent degradation: with Redis down, limits become per process.

## Running more than one worker or replica

What changes when you run `uvicorn --workers N` or several containers:

| Concern | One process | Several processes |
|---|---|---|
| Per-key rate limits | Exact, in memory | Up to N times the limit, unless `RATE_LIMIT_REDIS_URL` is set |
| Tenant quotas | Database counters | Correct with a shared database (minus the small burst allowance from counting after the response); SQLite across hosts is not shared |
| Circuit breakers | One per backend | One per backend **per process**: each process needs its own run of failures to open |
| API keys, tenants, routes | Database | Shared with PostgreSQL; with SQLite only on one host, and writes can fail with "database is locked" |
| Dynamic routes | Applied at once | Applied at once in the process that took the write, after up to `DYNAMIC_ROUTES_REFRESH_SECONDS` in the others |
| Webhooks, transformations, API versions, OAuth2 providers, log exporters added through the admin API | In memory | Only in the process that handled the admin call |
| MQTT client | One connection | One connection per process |
| Analytics, traces, SLOs | In memory | Per process |
| Prometheus metrics | One `/metrics` | Each process has its own counters; with `--workers N`, each scrape reaches one worker (the client library is not set up for multiprocess mode) |

The container image runs one uvicorn process. `WORKERS` is used only when the
gateway is started with `python -m app.main` and `ENVIRONMENT=production`.

## Buffer by default, stream when asked

Ordinary responses are read in full, transformed, and returned. AG-UI and
ACP run requests are streamed chunk by chunk when the client asks for
Server-Sent Events; A2A `message:stream` paths and agent-edge responses are
always streamed.

- **Buys:** response transformations and plugins can see and rewrite whole
  bodies.
- **Costs:** large responses are held in memory, and long responses on
  non-streaming paths are bounded by `DEFAULT_TIMEOUT` (30 seconds).
  Response plugins do not run on streamed responses.

## Fail open on side work, fail closed on credentials

Webhooks, notifications, tenant usage writes, request-log writes, analytics,
plugins and most start-up steps are wrapped so that a failure is logged and the
request continues. Credentials go the other way: a placeholder `SECRET_KEY` is
refused, an empty `ADMIN_TOKEN` with a placeholder `SECRET_KEY` leaves the
admin API closed, and there is no default password.

- **Buys:** a broken Slack webhook or log exporter does not cause 500s.
- **Costs:** side features can stop working without anyone noticing, unless
  you watch the logs for warnings such as `tenant_usage_record_failed` or
  `plugin_bootstrap_failed`.

## Configuration from the environment

All settings are fields of one pydantic-settings class (`app/config.py`), read
from built-in defaults, then an optional `CONFIG_FILE` (YAML, JSON or TOML),
then `.env`, then the environment. A small set of fields can be changed on a
running gateway with `PUT /admin/config`.

- **Buys:** the twelve-factor style that containers expect, with type checking
  at start-up.
- **Costs:** settings are read once, so
  most changes need a restart; and some settings are accepted without being
  used. The [configuration reference](../reference/configuration.md) marks
  them.

## Legacy names kept on purpose

The project was first called MCP Gateway. The names `MCP_GATEWAY_PLUGIN_PATH`,
`MCP_GATEWAY_PLUGIN_DISABLED`, `MCP_GATEWAY_URL`, `MCP_GATEWAY_CONFIG`,
`MCP_GATEWAY_TOKEN`, the `mcp_gateway.plugins` entry-point group, the
`mcp-gateway` and `agent-universal-gateway` commands and `scripts/mcp-gateway`
are read under those exact names, so existing installations keep working.
`docker-compose.yml` also still names the gateway container
`mcp_a2a_gateway`. The canonical names are listed once, in the
[CLI reference](../reference/cli.md#names-and-aliases).
