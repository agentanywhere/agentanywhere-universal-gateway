# Glossary

Terms as they are used in the AgentAnywhere Universal Gateway documentation.

**A2A (Agent-to-Agent).** A protocol for agents to exchange messages. The
gateway proxies it under `/a2a/*` to `A2A_BACKEND_URL` and checks
send-message bodies.

**ACP (Agent Communication Protocol).** A REST protocol for agent runs. The
gateway proxies it under `/acp/*`, with the `/acp` prefix removed, to
`ACP_BACKEND_URL`.

**Admin credential.** The bearer token accepted on the admin plane:
`ADMIN_TOKEN`, a non-placeholder `SECRET_KEY`, or an OpenID Connect access
token when `IDA_ISSUER` is set. Also the console password. See the
[security model](explanation/security-model.md).

**Admin plane.** Everything under `/admin`: the API used by the console and the
CLI to manage the gateway.

**Agent edge.** An optional reverse proxy under `/agents/<slug>/...` that
forwards HTTP and WebSocket traffic to a per-slug upstream without gateway
credentials.

**AG-UI (Agent-User Interaction protocol).** An event-based protocol between
agents and user interfaces, often over Server-Sent Events. Proxied under
`/agui/*` with the prefix removed.

**API key.** A credential (`sk-...`) issued with `POST /admin/api-keys` and
sent by clients in `X-API-Key` or `Authorization: Bearer`. Each key has its own
rate limits and optional path scope. Stored as a SHA-256 hash and shown only
once, when it is created.

**Backend.** A service the gateway forwards requests to. Also called an
upstream.

**Boundary check.** Validation the gateway applies to MCP, A2A, ACP and AG-UI
request bodies before forwarding them.

**Catch-all proxy.** The route `/{path:path}` that handles every data-plane
path without a dedicated handler, and forwards it to a backend.

**Circuit breaker.** A per-backend, per-process switch that stops sending
requests to a backend after `CIRCUIT_BREAKER_FAILURE_THRESHOLD` failures in a
row, answering 503 instead, and tries again after `CIRCUIT_BREAKER_TIMEOUT`
seconds.

**Data plane.** The routes your clients call through the gateway, protected by
API keys.

**Dynamic route.** A route registered at run time with `POST /admin/routes`,
stored in the database. It takes precedence over prefix routing.

**Fixed window.** A rate-limit counter that counts requests in a period of set
length and resets at the end of it, as opposed to a sliding window.

**MCP (Model Context Protocol).** A protocol through which AI applications use
tools and data sources. The gateway proxies the HTTP transport of MCP servers
under `/mcp/*`.

**Placeholder secret.** A `SECRET_KEY` that starts with `change-me`, like the
one in `env.example`. The gateway treats it as unset.

**Plugin.** A Python class loaded at start-up that can transform requests and
responses, authenticate requests, answer a custom path prefix, or run hooks.
See [Write a plugin](how-to/write-a-plugin.md).

**Prefix routing.** Choosing the backend from the first path segment (`/mcp`,
`/a2a`, `/api` and so on), using one backend URL per protocol.

**Rate limit.** The number of requests an API key may make per minute and per
hour.

**SSE (Server-Sent Events).** A one-way stream of events over HTTP
(`text/event-stream`), used by AG-UI, ACP runs and A2A streaming.

**Tenant.** A group of API keys that share a request quota, usually one
customer account.

**Tenant quota.** The number of requests all of a tenant's keys together may
make per minute and per hour.

**Transformation rule.** A declarative rewrite of requests or responses
registered with `/admin/transformations`. Kept in memory.
