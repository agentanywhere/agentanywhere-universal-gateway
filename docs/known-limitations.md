# Known limitations

This page lists what does not work, or does not work the way you might expect,
in the current code of AgentAnywhere Universal Gateway. Each entry says what
happens, where it comes from, and what to do about it. Read it before you put
the gateway in front of anything that matters.

Entries marked **reproduced** were run against the code for this page (in
process, with FastAPI's test client, or with Alembic against a scratch SQLite
file). The others were traced by reading the code and the deployment files.

Several problems listed in earlier editions of this page are fixed; the
[changelog](../CHANGELOG.md) lists them, and [Upgrade the gateway](how-to/upgrade.md#behaviour-changes-in-this-release)
lists the behaviour changes that may need action.

## Contents

- [Protocols](#protocols)
- [Credentials and keys](#credentials-and-keys)
- [Settings that are not enforced yet](#settings-that-are-not-enforced-yet)
- [Deployment files](#deployment-files)
- [Data and storage](#data-and-storage)
- [Limits and quotas](#limits-and-quotas)
- [Observability](#observability)
- [Routing behaviour](#routing-behaviour)
- [Admin API, console and CLI](#admin-api-console-and-cli)
- [Tests and platform](#tests-and-platform)

## Protocols

The full status of each protocol is in the
[protocol support matrix](explanation/protocol-support.md).

### WebSocket pass-through does not work

**Reproduced.** `/ws/{path}` closes every connection. Without a key it closes
with code 1008 and reason `API key required`; with a valid key it closes with
1008 and `Authentication failed`. The WebSocket examples in `examples/` and the
console's protocol test page do not work.

**Workaround.** Connect clients to the WebSocket backend directly, or use the
agent edge (`/agents/<slug>/...`), which relays WebSocket frames but applies no
gateway credential. See [Configure routes and backends](how-to/configure-routes-and-backends.md#the-agent-edge).

### gRPC pass-through does not work

The JSON-to-gRPC bridge under `/grpc` tries server reflection with a package
that is not in `requirements.txt`, and only knows the sample `Greeter` service
otherwise. With the stock requirements it answers HTTP 200 with
`"status": "unsupported"`. gRPC clients cannot connect to the gateway.

**Workaround.** Call the gRPC service directly, or put an HTTP facade in front
of it and route to that.

### The MQTT client connects once, at start-up

If the broker is not reachable when the gateway starts, `/mqtt/publish` answers
503 `MQTT broker not connected` until the gateway restarts. Messages received
on `/mqtt/subscribe` subscriptions are only written to the log.

**Workaround.** Start the broker before the gateway.

### The sample GraphQL URL in `env.example` points at the wrong port

`env.example` sets `GRAPHQL_BACKEND_URL=http://localhost:4000/graphql`; the
sample GraphQL backend listens on 8006. (Docker Compose uses the HTTP sample's
`/graphql` instead and is not affected.)

**Workaround.** `GRAPHQL_BACKEND_URL=http://localhost:8006/graphql`.

## Credentials and keys

### `SECRET_KEY` can be switched on as an API key

**By design, off by default.** With `ALLOW_SECRET_KEY_API_KEY=true`,
`SECRET_KEY` (when it is not the `change-me` placeholder) authenticates on
every data-plane route with the default rate limits, whether or not
`ADMIN_TOKEN` is set. Each use is counted
(`gateway_secret_key_api_key_uses_total`), logged and written to the
governance audit log. On the admin API `SECRET_KEY` is accepted only while
`ADMIN_TOKEN` is empty, unless `ADMIN_ACCEPT_SECRET_KEY=true`. It signs nothing.

**Workaround.** Leave `ALLOW_SECRET_KEY_API_KEY` off and create API keys. Keep
`SECRET_KEY` in your secret store and give it to no client. Set `ADMIN_TOKEN`
so that `SECRET_KEY` stops opening the admin API. See
the [security model](explanation/security-model.md#the-secret_key-break-glass-credential).

### Keys created by earlier builds stay in plaintext until they are used

New keys are stored as a SHA-256 hash only. A row written by an earlier build
keeps its plaintext copy until the key next authenticates; keys that are never
used again keep it, and so do database backups taken before the upgrade.

**Workaround.** Revoke keys you no longer use, and protect or destroy old
backups as you would the keys themselves.

### An auth-provider plugin bypasses limits and quotas

If an `AuthProviderPlugin` (including the built-in `static-token-auth`)
accepts a request, API-key validation, rate limits, tenant quotas, IP rules
and policies are skipped for it.

**Workaround.** Enable auth-provider plugins only when that is what you want.

### Credentials in URLs may be logged

The access log includes the query string. A WebSocket key passed as
`?api_key=` and an admin credential passed to `/admin/agents/stream?token=` can
end up in logs.

**Workaround.** Prefer headers. Restrict who can read logs.

## Settings that are not enforced yet

These are accepted, stored or reported, but no code acts on them:

| Setting or field | What happens |
|---|---|
| `PROTOCOL_*_RATE_LIMIT_PER_MINUTE`, `PROTOCOL_*_RATE_LIMIT_PER_HOUR` | Reported by `GET /admin/settings/protocols`; requests are limited per key. |
| A policy with a `rate_limit` action | Evaluated, but the limit is not applied. |
| Tenant quota `requests_per_day`, `storage_mb` | Stored and shown; only `requests_per_minute` and `requests_per_hour` are checked. |
| Admin roles (`/admin/governance/roles`) | Stored; every valid admin credential has full access. |
| `ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES` | Not read: nothing issues or signs tokens. |
| A dynamic route's `requires_auth`, `rate_limit` and `headers` | Stored; the route still needs an API key and uses the key's limits. |

## Deployment files

### Published ports bind to localhost by default

Every port `docker-compose.yml` publishes (the gateway, the admin console,
Prometheus, Grafana, the sample backends) listens on `127.0.0.1` unless you set
`BIND_ADDRESS` in `.env`. Only nginx's 80 and 443 listen on every interface.
So on a host with the `production` profile, clients reach the gateway through
nginx and its TLS, and its allow-list for `/metrics` and `/health/detailed`.

If you set `BIND_ADDRESS=0.0.0.0` (to reach the gateway or the console from
another machine without nginx) the port is open to that network and bypasses
nginx: firewall it, and set `OPS_ENDPOINTS_REQUIRE_ADMIN=true`. See
[Run behind nginx with TLS](how-to/run-behind-nginx-tls.md).

### The nginx healthcheck probably reports unhealthy

The `nginx` service's healthcheck requests `http://localhost/health`, and the
port-80 server answers every request with a redirect to HTTPS. Not run for
this page.

**Workaround.** Ignore the status, or change the healthcheck to request
`https://localhost/health` with certificate checking off.

### The password checks refuse placeholders, not weak passwords

The `postgres` and `grafana` containers refuse to start while
`POSTGRES_PASSWORD` is empty or starts with `change-me`, and while
`GRAFANA_ADMIN_PASSWORD` is empty or `admin`. Any other value is accepted.

## Data and storage

### Databases built with the old migrations must be rebuilt

The migrations `001` and `002` of earlier builds were replaced by one baseline,
`0001`. A database built by the old ones stops `alembic upgrade head` with
"Can't locate revision identified by '002'", and its `api_keys` and
`request_logs` tables have the wrong shape. A database the gateway created
itself is fine.

**Workaround.** See [Upgrade the gateway](how-to/upgrade.md#databases-built-with-the-old-migrations).

### Configuration made through some admin endpoints is kept in memory only

Webhooks, transformation rules, API versions, OAuth2 providers and their
tokens, and log exporters added through the admin API live in the memory of
the process that handled the request. They are lost on restart and are not
shared between processes.

**Workaround.** Re-apply them at start-up from your deployment tooling.

### `request_logs` is never pruned

Every request, including rejected ones, writes a row. The `protocol` column is
always null.

**Workaround.** Delete old rows on a schedule.

### `docker compose down -v` deletes the database

The default SQLite file is on the `gateway_data` volume, which survives
`down`, `up --build` and container recreation, but not `down -v` or
`make clean`.

**Workaround.** [Back up the volume](how-to/back-up-and-restore.md), or use
PostgreSQL.

## Limits and quotas

### In-memory counters are per process, and the window depends on the store

Without `RATE_LIMIT_REDIS_URL`, each process counts on its own, so N processes
allow up to N times the limit. In memory, a window starts at the first request
and lasts 60 or 3600 seconds. In Redis, the counter's expiry is renewed on
every allowed request, so it resets 60 or 3600 seconds after the last allowed
request.

**Workaround.** Set `RATE_LIMIT_REDIS_URL` when you run more than one process.

### Tenant usage is counted after the response

A burst of concurrent requests can pass a tenant quota by a few requests.

## Observability

### Two metrics are never updated

`gateway_circuit_breaker_state` and `gateway_active_connections` are exported
but no code changes them. `gateway_requests_total` and
`gateway_request_duration_seconds` label accepted requests with the raw path,
which can create many series (rejected requests are labelled with the first
path segment, or `/other`). See the [metrics reference](reference/metrics.md).

### The `/admin/ai/*` analytics endpoints are experimental

The request middleware feeds one `AIService` instance (`app/main.py`), but the
admin endpoints read a second, separate instance (`admin/api.py`) that never
sees traffic, so they answer with empty results or "Insufficient data".

## Routing behaviour

### A backend that is not configured answers 500

**Reproduced.** A request under `/agui`, `/acp`, `/ws` or a `/mqtt` path other
than `publish` and `subscribe`, when the matching backend variable is unset,
answers 500 `INTERNAL_ERROR`. `/graphql` and `/grpc` answer 503.

### Paths with no protocol prefix go to a default backend

**Reproduced.** Any authenticated path that does not start with a known prefix
is proxied to `HTTP_BACKEND_URL`, or to `MCP_BACKEND_URL` when that is unset.

### The caller's headers reach the backend

The gateway forwards `X-API-Key`, `Authorization` and cookies to the backend.

**Workaround.** Strip them in a transform plugin if a backend must not see
them ([Write a plugin](how-to/write-a-plugin.md)).

## Admin API, console and CLI

### Unknown webhook event names are accepted

**Reproduced.** `POST /admin/webhooks` registers a webhook for any event name.
Names the gateway does not know are listed in the response's
`unknown_events`, and those webhooks never fire.

**Workaround.** Check `unknown_events`, or `GET /admin/webhooks/events`.

### `routes create --methods` is ignored

The CLI sends a `methods` field; routes have none, and every method is routed.

### The admin console needs a proxy in front of it

The console calls `/admin/*` on its own origin. Its nginx image proxies that
to `gateway:8000` on the Compose network, and the Vite dev server proxies it to
`localhost:8000`. Served any other way, it needs an equivalent proxy (or a
build with `VITE_API_URL` and a matching `CORS_ORIGINS`).

## Tests and platform

### PostgreSQL tests run only when a database is provided

The migration tests run against SQLite every time. They run against PostgreSQL
only when `GATEWAY_TEST_POSTGRES_URL` is set, which CI does not do by default.

### Python 3.13 is not supported

`pyproject.toml` requires Python 3.10 to 3.12, and several pinned dependencies
do not build on 3.13.
