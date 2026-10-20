# AgentAnywhere Universal Gateway

[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](pyproject.toml)

An API gateway for agent and application traffic. It puts one layer of API-key
authentication, rate limiting, tenant quotas, circuit breaking and request
logging in front of backends that speak MCP, A2A, AG-UI, ACP, HTTP/REST,
GraphQL and MQTT. It is a FastAPI application, licensed under Apache-2.0.

This README only states what we ran and saw work. Where something is
incomplete, it says so.

## What works today

Each row was checked by hand against the sample backends in
[`samples/backends/`](samples/backends/), on Python 3.11.

| Protocol | Gateway route | Status |
|---|---|---|
| MCP | `/mcp/*` | Works. Requests are authenticated, limited and proxied to `MCP_BACKEND_URL`. |
| A2A | `/a2a/*` | Works. Proxied to `A2A_BACKEND_URL`. |
| AG-UI | `/agui/*` | Works. Server-Sent Events are streamed through from `AG_UI_BACKEND_URL`. |
| ACP | `/acp/*` | Works. Proxied to `ACP_BACKEND_URL`. |
| HTTP / REST | `/api/*`, `/rest/*`, `/http/*` | Works. Proxied to `HTTP_BACKEND_URL`. |
| GraphQL | `/graphql` | Works. Queries are proxied to `GRAPHQL_BACKEND_URL`. |
| MQTT | `/mqtt/publish` | Works. A published message reaches the broker at `MQTT_BROKER_URL`. |
| gRPC | `/grpc/*` | **Not working.** The JSON-to-gRPC bridge does not forward calls to a backend. With the stock requirements it answers HTTP 200 with `"status": "unsupported"`. |
| WebSocket | `/ws/*` | **Not working.** The route refuses every connection, including ones with a valid key, and has no backend relay. |

The rest of what we checked by hand:

- **API keys.** Created through the admin API. A request with no key, an
  unknown key, or the placeholder `SECRET_KEY` gets 401.
- **Rate limits.** A key limited to 5 requests a minute gets five 200s and then
  429 with `Retry-After: 60`. Checked with and without Redis.
- **Tenant quotas.** A tenant limited to 3 requests a minute gets three 200s and
  then 429. Checked with and without Redis.
- **Backend failure.** With the backend stopped, requests get 502. After five
  consecutive failures the circuit opens and requests get 503. Other protocols
  keep working. After `CIRCUIT_BREAKER_TIMEOUT` seconds the route recovers.
- **Dynamic routes.** A route registered with `POST /admin/routes` is used on
  the next request and appears in `GET /admin/routes` with `"source": "dynamic"`.
- **Admin API.** 132 of its 135 operations answer 401 without the admin
  credential. The three that do not are `/admin/health`, `/admin/auth/config`
  and `/admin/auth/login`.

The repository also contains a policy engine, role and IP-rule management, an
audit chain, request and response transformations, API versioning, webhooks,
log exporters, a plugin loader, SDK generation, a command-line tool and a React
admin console. The automated tests cover them. We have not checked each of them
by hand, so this README does not make claims about them.

## Known limitations

- **gRPC and WebSocket do not work** (see the table above).
- **API keys are stored in plaintext.** The database keeps each key next to its
  SHA-256 hash, and `GET /admin/api-keys` returns keys in full. With Redis
  enabled, keys also appear in Redis key names. Protect the database, Redis and
  the admin credential accordingly.
- **`SECRET_KEY` has three jobs.** It signs tokens, it is accepted as the admin
  credential when `ADMIN_TOKEN` is empty, and it is accepted as an API key. Set
  a separate `ADMIN_TOKEN` and treat both as secrets.
- **The analytics endpoints returned no analysis in our run.** The
  `/admin/ai/*` endpoints (anomalies, scaling, cost, forecast) answered with
  empty results or "Insufficient data" after real traffic. Treat them as
  experimental.
- **Tenant usage is counted after the response.** A burst of concurrent
  requests can pass a tenant quota by a few requests.
- **A WebSocket API key in the query string is written to the access log.**
- **Python 3.13 is not supported.** Several pinned dependencies do not build on it.

## Quick start

You need Python 3.10, 3.11 or 3.12 and `git`. Nothing else is required: without
Redis the gateway keeps rate-limit counters in memory, and the default database
is a local SQLite file.

```bash
git clone https://github.com/agentanywhere/universal-gateway.git
cd universal-gateway
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
python3 scripts/generate_secret_key.py --update-env
uvicorn app.main:app --port 8000
```

The `generate_secret_key.py` step matters. `env.example` ships a placeholder
`SECRET_KEY`, and the gateway refuses it: with the placeholder in place and no
`ADMIN_TOKEN`, the admin API answers 401 to everything. There is no default
password.

In a second terminal, start a sample backend:

```bash
cd universal-gateway
source .venv/bin/activate
python3 samples/backends/http/http_server.py
```

In a third terminal, create an API key and make a request:

```bash
cd universal-gateway
ADMIN=$(grep '^SECRET_KEY=' .env | cut -d= -f2-)
curl -s -X POST "http://localhost:8000/admin/api-keys?name=first-key" -H "Authorization: Bearer $ADMIN"
```

The response contains the new key in its `key` field. Copy it:

```bash
API_KEY=paste-the-key-here
curl -s -X POST http://localhost:8000/api/users -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"name": "Ada", "email": "ada@example.com"}'
```

You should get the created user back as JSON. The same request without the
`X-API-Key` header gets 401.

Interactive API documentation is at <http://localhost:8000/docs>.

## Docker

```bash
cp env.example .env
python3 scripts/generate_secret_key.py --update-env
docker compose up -d --build
curl http://localhost:8000/health
```

This starts the gateway on port 8000, Redis, and the admin console on port
3001. `docker compose --profile backends up -d --build` also starts the sample
backends. Before you start, check that nothing else is using ports 8000, 3001
and 6379.

Under Compose the gateway's backend URLs point at the sample backends by
service name (`http://http-backend:8003` and so on), whatever `.env` says for
`*_BACKEND_URL`; set `DOCKER_HTTP_BACKEND_URL` and the other `DOCKER_` variants
in `.env` to use your own backends. The SQLite database is kept on the
`gateway_data` volume at `/app/data/gateway.db`, so API keys survive
`docker compose down` and rebuilds (not `docker compose down -v`, which deletes
the volume). See [Quick start: if something goes wrong](docs/QUICKSTART.md#if-something-goes-wrong)
and, for backups, [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#backing-up-the-database-volume).

**Security defaults in the Compose file.** Redis has no password and, like
PostgreSQL, is published on `127.0.0.1` only. If you start the `monitoring`
profile, Grafana's admin password is `admin` unless you set
`GRAFANA_ADMIN_PASSWORD`. If you start the `production` profile, set
`POSTGRES_PASSWORD`; the fallback is a public placeholder. `CORS_ORIGINS`
allows every origin until you change it, and `/metrics` and `/health/detailed`
need no credential. The full list, and what to change, is in
[docs/PRODUCTION_SETUP.md](docs/PRODUCTION_SETUP.md).

## Admin console

The console is a React application in [`admin-ui/`](admin-ui/). With Docker
Compose it is served at <http://localhost:3001>. Sign in with the user name
`admin` and, as the password, the gateway's `ADMIN_TOKEN` (or its `SECRET_KEY`
if `ADMIN_TOKEN` is empty).

Signing in through an OpenID Connect provider is optional and off by default.
It turns on when `IDA_ISSUER` is set. A path-prefix reverse proxy for
`/agents/<slug>/...` is also included and off by default; it does not apply
gateway API keys. Both are described, with what was and was not checked, in
[docs/CONFIGURATION.md](docs/CONFIGURATION.md).

## Configuration

Configuration is read from environment variables and from `.env`.
[`env.example`](env.example) lists every setting. The ones you will touch first:

| Variable | What it does | Default |
|---|---|---|
| `SECRET_KEY` | Signs tokens. Also the admin credential when `ADMIN_TOKEN` is empty. | a placeholder that is refused |
| `ADMIN_TOKEN` | Credential for the admin API and console. | empty |
| `DATABASE_URL` | SQLAlchemy URL. PostgreSQL is supported through `asyncpg`. | `sqlite+aiosqlite:///./gateway.db` |
| `REDIS_URL`, `REDIS_ENABLED` | Shared rate-limit counters and the API-key cache. | `redis://localhost:6379/0`, `true` |
| `MCP_BACKEND_URL`, `A2A_BACKEND_URL` | Backends for `/mcp/*` and `/a2a/*`. | `http://localhost:8001`, `http://localhost:8002` |
| `HTTP_BACKEND_URL`, `AG_UI_BACKEND_URL`, `ACP_BACKEND_URL`, `GRAPHQL_BACKEND_URL`, `MQTT_BROKER_URL` | Backends for the other routes. | not set in code; `env.example` points some at the sample backends |
| `RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_PER_HOUR` | Limits for callers whose key does not set its own. | `60`, `1000` |
| `CIRCUIT_BREAKER_FAILURE_THRESHOLD`, `CIRCUIT_BREAKER_TIMEOUT` | Failures before a backend's circuit opens, and seconds before it is tried again. | `5`, `60` |

## Tests

```bash
pip install -r requirements.txt
pytest
```

The suite has 691 passing tests and 2 skipped, with 75% line coverage of `app`
and `admin`. One skip is a live-server module that only runs when a gateway
answers at `GATEWAY_URL` (default `http://localhost:8000`). If something else
is listening on that port, set `GATEWAY_URL` to an unused one.

Continuous integration runs the same lint, test and build steps on Python 3.10,
3.11 and 3.12. See [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## Documentation

- [Quick start](docs/QUICKSTART.md)
- [Protocols](docs/PROTOCOLS.md), with a request example and sample backend for each
- [Configuration](docs/CONFIGURATION.md)
- [API reference](docs/API_REFERENCE.md)
- [Command-line tool](docs/CLI.md)
- [Plugins](docs/PLUGINS.md)
- [Deployment](docs/DEPLOYMENT.md): container, Compose, reverse proxy, more than one process
- [Production checklist](docs/PRODUCTION_SETUP.md): defaults to change before you expose the gateway
- [Upgrading and the database schema](docs/UPGRADING.md)

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) and the
[code of conduct](CODE_OF_CONDUCT.md). Report vulnerabilities privately as
described in [SECURITY.md](SECURITY.md), not in a public issue.

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 ShepHertz Technologies Pvt. Ltd.
