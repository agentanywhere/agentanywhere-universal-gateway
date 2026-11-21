# Configure routes and backends

**Goal:** send each kind of traffic to the right backend, using the per-protocol
settings, routes added at run time, or the agent edge.

**Before you start:** know the address of each backend **as the gateway sees
it**. On your machine that is `localhost`; inside Docker Compose it is the
service name (for example `http://http-backend:8003`), because `localhost` is
the gateway container itself.

## Contents

- [Choose a method](#choose-a-method)
- [Per-protocol settings](#per-protocol-settings)
- [Dynamic routes](#dynamic-routes)
- [The agent edge](#the-agent-edge)
- [If it did not work](#if-it-did-not-work)

## Choose a method

| You want | Use |
|---|---|
| One backend per protocol | [Per-protocol settings](#per-protocol-settings) |
| Several backends for the same protocol, or a path that should go somewhere else | [Dynamic routes](#dynamic-routes) |
| A browser application or agent with its own authentication, under `/agents/<slug>/` | [The agent edge](#the-agent-edge) |
| WebSocket or gRPC pass-through | Not possible as shipped; see the [protocol support matrix](../explanation/protocol-support.md) |

## Per-protocol settings

1. Set the backend URLs in `.env` (or the environment):

   ```bash
   HTTP_BACKEND_URL=http://localhost:8003
   MCP_BACKEND_URL=http://localhost:8001
   A2A_BACKEND_URL=http://localhost:8002
   AG_UI_BACKEND_URL=http://localhost:8004
   ACP_BACKEND_URL=http://localhost:8005
   GRAPHQL_BACKEND_URL=http://localhost:8006/graphql
   MQTT_BROKER_URL=mqtt://localhost:1883
   ```

   These are the ports of the sample backends in `samples/backends/`. Leave a
   line out to leave that protocol unrouted.

   Under the repository's `docker-compose.yml`, set the `DOCKER_` variants
   instead (`DOCKER_HTTP_BACKEND_URL`, `DOCKER_MCP_BACKEND_URL` and so on, for
   every protocol except AG-UI and ACP): Compose ignores the `*_BACKEND_URL`
   lines of `.env` and defaults to the sample backends' service names. See
   [Deploy with Docker Compose](deploy-with-docker-compose.md#point-the-gateway-at-your-own-backends).

2. Restart the gateway. Start the MQTT broker first, if you use one: the
   gateway connects to it only at start-up.

What each setting routes:

| Setting | Paths | The backend receives |
|---|---|---|
| `HTTP_BACKEND_URL` | `/api/*`, `/rest/*`, `/http/*`, and any path with no known prefix | The full path, for example `/api/users` |
| `MCP_BACKEND_URL` | `/mcp/*`, `/api/mcp/*` (and unknown paths when `HTTP_BACKEND_URL` is unset) | The full path |
| `A2A_BACKEND_URL` | `/a2a/*`, `/api/a2a/*` | The full path |
| `AG_UI_BACKEND_URL` | `/agui/*`, `/api/agui/*` | The path without `/agui` (`POST /agui` arrives as `POST /`) |
| `ACP_BACKEND_URL` | `/acp/*`, `/api/acp/*` | The path without `/acp` |
| `GRAPHQL_BACKEND_URL` | `/graphql/*`, `/api/graphql/*` | The path without `/api`; the URL may end in `/graphql` or not |
| `MQTT_BROKER_URL` | `POST /mqtt/publish`, `POST /mqtt/subscribe` | MQTT messages |

Prefixes are checked in this order: `/mcp`, `/a2a`, `/agui`, `/acp`, `/mqtt`,
`/ws`, `/grpc`, `/graphql`, then `/api`, `/rest`, `/http`. So `/api/mcp/x`
goes to the MCP backend, and `/api/graphql` goes to the GraphQL backend.

**Check:** `GET /admin/routes` lists the static routes with
`"source": "static"`, and a request through each prefix answers with the
backend's response instead of 502:

```bash
curl -s "$URL/admin/routes" -H "Authorization: Bearer $ADMIN"
curl -s -o /dev/null -w "%{http_code}\n" "$URL/mcp/query" -X POST \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"query": "ping"}'
```

## Dynamic routes

A dynamic route sends a path, or a path prefix, to any backend, without a
restart. It takes precedence over the per-protocol settings.

1. Register it:

   ```bash
   curl -s -X POST "$URL/admin/routes" -H "Authorization: Bearer $ADMIN" \
     -H "Content-Type: application/json" \
     -d '{"path": "/billing/*", "backend_url": "http://billing.internal:9000", "protocol": "http", "timeout": 10}'
   ```

   The response has `"active": true` when the route is live in this process.

2. If you run more than one process, wait up to
   `DYNAMIC_ROUTES_REFRESH_SECONDS` (15 by default) for the others.

How matching works:

- A path ending in `*` is a prefix: `/billing/*` matches `/billing/invoices`,
  not `/billing`.
- An exact match beats any prefix, and a longer prefix beats a shorter one.
- The backend receives the **full** request path (`/billing/invoices`), or
  `rewrite_path` if you set one, which replaces the whole path.
- Requests through a dynamic route still need an API key, and still count
  against the key's limits. The route's `requires_auth`, `rate_limit` and
  `headers` fields are stored but not applied.

**Check:** `GET /admin/routes` shows the route with `"source": "dynamic"`, and
a request under the prefix reaches the new backend.

To remove a dynamic route, there is no endpoint. Delete the row from the
`routes` table (or set `is_active` to false) and wait for the refresh.

To turn dynamic routes off entirely, set `DYNAMIC_ROUTES_ENABLED=false`.

## The agent edge

The agent edge forwards `/agents/<slug>/...` to a per-slug upstream, for HTTP
and WebSocket, with the `/agents/<slug>` prefix removed. **It applies no
gateway API key, rate limit or quota**, and forwards `Authorization` and
cookies; use it only for upstreams that authenticate their own callers.

1. Map slugs to upstreams:

   ```bash
   AGENT_EDGE_ENABLED=true
   AGENT_ROUTES_JSON={"orders-agent":"http://10.0.0.10:8090"}
   ```

   or register them at run time with `POST /admin/agents` (see the
   [endpoint list](../reference/http-endpoints.md#admin-adminagents)).

2. Restart the gateway.

**Check:** `GET /agents/orders-agent/health` reaches the upstream's `/health`.
An unknown slug answers 404 `AGENT_NOT_FOUND`.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| 502 `BACKEND_ERROR` | The gateway cannot reach the URL. Under Docker, `localhost` is the container, and Compose uses the `DOCKER_*_BACKEND_URL` values. | Use an address the gateway can reach. See [Troubleshoot 401, 403, 404, 502 and 429](troubleshoot-401-502-429.md). |
| 500 `INTERNAL_ERROR` on `/agui` or `/acp` | `AG_UI_BACKEND_URL` or `ACP_BACKEND_URL` is unset | Set it and restart. |
| 503 `GraphQL backend is not configured` | `GRAPHQL_BACKEND_URL` is unset | Set it and restart. |
| 503 `MQTT broker not connected` | The broker was not up when the gateway started | Start the broker, then restart the gateway. |
| 404 from the backend for every path | The backend expects a path without the gateway prefix | Use a dynamic route with `rewrite_path`, or serve the prefix in the backend. |
| A route you added has no effect | `DYNAMIC_ROUTES_ENABLED=false`, or another process has not refreshed yet | Check the setting; wait for the refresh. |
