# AgentAnywhere Universal Gateway

A Python (FastAPI) gateway that puts API keys, rate limits, tenant quotas,
routing and Prometheus metrics in front of MCP, A2A, AG-UI, ACP, HTTP/REST,
GraphQL and MQTT backends. Apache-2.0.

- **For:** teams that run several agent or API services and want one place to
  issue keys, apply limits and watch traffic, without changing the services.
- **Not for:** anyone who needs WebSocket or gRPC pass-through, role-based
  admin access, per-protocol rate limits or published performance figures. See
  [what it does not do](#what-it-does-not-do).

**Status:** version 1.0.0, not yet released (see the [changelog](CHANGELOG.md)).
Read the [known limitations](docs/known-limitations.md) before you deploy it.

## Contents

- [Quick start](#quick-start)
- [What you get](#what-you-get)
- [What it does not do](#what-it-does-not-do)
- [Documentation](#documentation)
- [Contributing, support and security](#contributing-support-and-security)
- [Licence](#licence)

## Quick start

You need Python 3.10, 3.11 or 3.12, `git` and `curl`. Redis, PostgreSQL and
Docker are not needed. Most of the time goes into `pip install`.

```bash
git clone https://github.com/agentanywhere/agentanywhere-universal-gateway.git
cd agentanywhere-universal-gateway
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp env.example .env
python3 scripts/generate_secret_key.py --update-env
echo "ADMIN_TOKEN=$(python3 scripts/generate_secret_key.py 2>/dev/null)" >> .env
uvicorn app.main:app --port 8000
```

In a second terminal, start the sample HTTP backend (port 8003, where
`HTTP_BACKEND_URL` in `env.example` points):

```bash
cd agentanywhere-universal-gateway && source .venv/bin/activate
python3 samples/backends/http/http_server.py
```

In a third terminal, create an API key with the admin credential and call the
backend through the gateway:

```bash
cd agentanywhere-universal-gateway
ADMIN=$(grep '^ADMIN_TOKEN=' .env | tail -n 1 | cut -d= -f2-)
API_KEY=$(curl -s -X POST "http://localhost:8000/admin/api-keys?name=first-key" \
  -H "Authorization: Bearer $ADMIN" | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')
curl -s -X POST http://localhost:8000/api/users -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" -d '{"name": "Ada", "email": "ada@example.com"}'
```

You get the created user back from the backend:

```json
{"id": "user_1", "name": "Ada", "email": "ada@example.com", "metadata": null, "created_at": "2026-10-20T19:29:25.848555+00:00"}
```

Without the `X-API-Key` header, the gateway answers 401 and the backend never
sees the request. The [getting-started tutorial](docs/tutorials/getting-started.md)
walks through the same steps with explanations; the output above comes from
replaying them in process (see that page for how).

**Using Docker Compose instead?** `docker compose --profile backends up -d --build`
starts the gateway, Redis, the console and the sample backends; the gateway
reaches the samples by service name and keeps its SQLite database on the
`gateway_data` volume. Read
[Deploy with Docker Compose](docs/how-to/deploy-with-docker-compose.md) first:
published ports listen on `127.0.0.1` unless you set `BIND_ADDRESS`.

## What you get

- **API keys** stored as hashes and shown once, with per-key limits per
  minute and per hour, optional expiry and path scope, created and revoked
  through an admin API (revocation takes effect on the next request).
- **Tenant quotas** shared by groups of keys, with usage counters.
- **Routing** by path prefix to one backend per protocol, plus routes added at
  run time, a circuit breaker per backend, and Server-Sent Events streaming for
  AG-UI, ACP and A2A.
- **Protocol checks** for MCP (JSON-RPC shape, size, method lists, a simple
  prompt-injection pattern filter), A2A, ACP and AG-UI bodies.
- **Observability:** Prometheus metrics, JSON logs, a request log table,
  health and readiness endpoints, W3C trace context and optional OTLP export.
- **Operations:** an admin API, a React admin console, a command-line tool,
  plugins, webhooks, IP rules and policies, a Dockerfile, a Compose file and
  an nginx TLS configuration.

What works for each protocol is in the
[protocol support matrix](docs/explanation/protocol-support.md).

## What it does not do

- **WebSocket and gRPC pass-through do not work.** The WebSocket route closes
  every connection, and the gRPC bridge answers `"status": "unsupported"`.
- **Some settings are not enforced yet:** admin roles, per-protocol rate
  limits, the policy `rate_limit` action and tenant `requests_per_day`.
- **`SECRET_KEY` is a break-glass credential** accepted as an API key on every
  data-plane route (and on the admin API while `ADMIN_TOKEN` is empty).
- **Defaults to review:** `/metrics` and `/health/detailed` need no credential
  unless `OPS_ENDPOINTS_REQUIRE_ADMIN=true`; nginx limits them to private
  addresses, but the Compose file publishes port 8000 directly.
- **Keys created by earlier builds** stay in plaintext in the database until
  they are next used.
- It does not terminate TLS, ship Kubernetes manifests, or publish benchmarks.

Every item, with its workaround: [known limitations](docs/known-limitations.md).

## Documentation

The [documentation map](docs/README.md) lists every page. The main entry
points:

- **Learn:** [getting started](docs/tutorials/getting-started.md),
  [two backends with limits, quotas and metrics](docs/tutorials/two-backends-end-to-end.md)
- **Do:** [API keys](docs/how-to/manage-api-keys.md),
  [routes and backends](docs/how-to/configure-routes-and-backends.md),
  [production checklist](docs/how-to/secure-for-production.md),
  [Docker Compose](docs/how-to/deploy-with-docker-compose.md),
  [troubleshooting 401, 403, 404, 502 and 429](docs/how-to/troubleshoot-401-502-429.md)
- **Look up:** [configuration](docs/reference/configuration.md),
  [HTTP API](docs/reference/http-api.md), [CLI](docs/reference/cli.md),
  [errors](docs/reference/errors.md), [metrics](docs/reference/metrics.md)
- **Understand:** [architecture](docs/explanation/architecture.md),
  [security model](docs/explanation/security-model.md),
  [design decisions](docs/explanation/design-decisions.md)

## Contributing, support and security

- [CONTRIBUTING.md](CONTRIBUTING.md): how to contribute, with a tour of the
  codebase and how to run the tests.
- [Support](docs/support.md): where to ask and what to include.
- [SECURITY.md](SECURITY.md): report vulnerabilities privately, not in a
  public issue.
- [Code of conduct](CODE_OF_CONDUCT.md).

## Licence

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 ShepHertz Technologies Pvt. Ltd.
