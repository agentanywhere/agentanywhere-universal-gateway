# Frequently asked questions

## What is this, in one sentence?

A Python reverse proxy that puts API keys, rate limits, tenant quotas, routing
and metrics in front of MCP, A2A, AG-UI, ACP, HTTP, GraphQL and MQTT backends.
More in [What the gateway is, and what it is not](explanation/overview.md).

## What is an AI agent gateway?

A gateway that sits between AI agents and the tools, models and services they
call. Instead of each service doing its own sign-in, limiting and logging, the
gateway does it once: it checks the API key, applies limits and policy, checks
the protocol message, forwards the request and records what happened. See
[What the gateway is, and what it is not](explanation/overview.md).

## Which protocols does it support?

MCP (HTTP transport), A2A (HTTP and Server-Sent Events), AG-UI, ACP, HTTP/REST,
GraphQL and MQTT. The WebSocket route closes every connection and the gRPC
bridge answers `"status": "unsupported"`. The
[protocol support matrix](explanation/protocol-support.md) says what works for
each protocol and which tests show it.

## Does it support the Model Context Protocol (MCP)?

Yes, over the HTTP transport. Requests under `/mcp/` are authenticated and rate
limited, checked for JSON-RPC shape, size, allowed methods and a simple
prompt-injection pattern filter, and forwarded to `MCP_BACKEND_URL`.

## How do I run it with Docker?

Copy `env.example` to `.env`, generate the secrets, and run
`docker compose --profile backends up -d --build`. The gateway listens on
`127.0.0.1:8000` and the admin console on `127.0.0.1:3001`. The steps and the
optional PostgreSQL, nginx and monitoring profiles are in
[Deploy with Docker Compose](how-to/deploy-with-docker-compose.md).

## Where is the admin console?

Port 3001: started by Docker Compose, or by `npm run dev` in `admin-ui/`. Sign
in as `admin` with your `ADMIN_TOKEN`. There is no default password. The
[README](../README.md#see-your-gateway) shows each page.

## How do I measure its performance?

There are no published figures. [Test performance](how-to/performance-testing.md)
shows how to measure your own set-up with the bundled load tester.

## Is it ready for production?

It has an automated test suite that continuous integration runs on every
push, and the repository includes a Dockerfile, a Compose file and an nginx
configuration.
It also has settings that look like controls but are not enforced yet (admin
roles, per-protocol rate limits). Read the
[known limitations](known-limitations.md) and work through
[Secure the gateway for production](how-to/secure-for-production.md), then
decide. Version 1.0.0 is marked unreleased in the changelog.

## Do I need Redis?

No. Without Redis, rate-limit counters are kept in memory, which is correct
for one process. Use Redis (`RATE_LIMIT_REDIS_URL`) when you run several
processes or replicas.

## Do I need PostgreSQL?

No. The default is a SQLite file. Use PostgreSQL when you run more than one
process or host, or when you deploy with Docker and want your keys to survive
a container rebuild.

## Why does my Docker quick start answer 502?

Under Compose the gateway sends traffic to the sample backends by service
name. Start them with `docker compose --profile backends up -d --build`, or
set `DOCKER_HTTP_BACKEND_URL` and the other `DOCKER_` variables to your own
backends. See [Deploy with Docker Compose](how-to/deploy-with-docker-compose.md#point-the-gateway-at-your-own-backends).

## Where is the default admin password?

There is none. Set `ADMIN_TOKEN` (and a real `SECRET_KEY`). Until you do, the
admin API refuses everything.

## Why do I have both `SECRET_KEY` and `ADMIN_TOKEN`?

`ADMIN_TOKEN` is the operators' credential. `SECRET_KEY` is a break-glass
credential: accepted on the admin API only while `ADMIN_TOKEN` is empty, and
as an API key on the data plane only with `ALLOW_SECRET_KEY_API_KEY=true`. Keep it in your secret store. See
[the SECRET_KEY break-glass credential](explanation/security-model.md#the-secret_key-break-glass-credential).

## Can I restrict a key to some paths?

Yes. Create the key with `allowed_paths` (for example `["/mcp"]`); other
paths answer 403. See [Create, rotate and revoke API keys](how-to/manage-api-keys.md#restrict-a-key-to-some-paths).

## Can I change a key's rate limit?

Not in place. Create a new key with the new limits and revoke the old one.

## I lost an API key. Can the gateway show it again?

No. Keys are stored as a hash and shown once, when they are created. Create a
new key, and revoke the old one by the `id` that `GET /admin/api-keys` lists
next to its `key_prefix`. Revocation takes effect on the next request. See
[Create, rotate and revoke API keys](how-to/manage-api-keys.md).

## Does it work with WebSocket or gRPC?

No. Both routes exist but do not pass traffic. See the
[protocol support matrix](explanation/protocol-support.md).

## Is the MCP support a full MCP server?

No. The gateway forwards HTTP requests to an MCP server and validates
JSON-RPC bodies on the way. It does not implement MCP tools or sessions
itself.

## Does the prompt-injection check protect me?

It blocks a few well-known phrases in MCP request bodies. It is easy to evade
and is not a defence against prompt injection.

## Can several workers share one gateway configuration?

Keys, tenants, routes and governance rules are in the database and are shared
if every process uses the same database server. Rate-limit counters are shared
only through Redis. Webhooks, transformations, API versions, OAuth2 providers
and log exporters added through the admin API stay in the process that handled
the call. See [Running more than one worker](explanation/design-decisions.md#running-more-than-one-worker-or-replica).

## How fast is it?

The repository publishes no benchmark, and none was run for this
documentation. Most responses are read fully into memory before they are
returned, and each request writes a database row.

## Should I use the Alembic migrations?

Yes, for upgrades. There is one baseline migration, `0001`, which matches the
models and is safe both on an empty database and on one the gateway already
created. On a new install you can also just start the gateway, which creates
its tables. See [Upgrade the gateway](how-to/upgrade.md).

## Why is the command called `mcp-gateway` in some places?

The project used to be called MCP Gateway. The old names still work; they are
listed once, in the [CLI reference](reference/cli.md#names-and-aliases).

## Is there a hosted or commercial version?

The repository does not describe one. Everything in it is under the Apache
License 2.0.

## Where do I report a security problem?

Privately, as described in [SECURITY.md](../SECURITY.md). Not in a public
issue.
