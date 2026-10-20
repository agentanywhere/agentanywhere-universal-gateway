# Frequently asked questions

## What is this, in one sentence?

A Python reverse proxy that puts API keys, rate limits, tenant quotas, routing
and metrics in front of MCP, A2A, AG-UI, ACP, HTTP, GraphQL and MQTT backends.
More in [What the gateway is, and what it is not](explanation/overview.md).

## Is it ready for production?

It has an automated test suite that continuous integration runs on every
push, and the repository includes a Dockerfile, a Compose file and an nginx
configuration.
It also stores API keys in plaintext, ships open CORS defaults, and has
settings that look like controls but are not enforced. Read the
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

`.env` copied from `env.example` points the backends at `localhost`, which
inside the container is the gateway itself. Use service names. See
[Deploy with Docker Compose](how-to/deploy-with-docker-compose.md#the-three-traps).

## Where is the default admin password?

There is none. Set `ADMIN_TOKEN` (and a real `SECRET_KEY`). Until you do, the
admin API refuses everything.

## Why do I have both `SECRET_KEY` and `ADMIN_TOKEN`?

`ADMIN_TOKEN` is the operators' credential. `SECRET_KEY` is accepted on both
the admin API and the data plane, so it should stay in your secret store. See
[the SECRET_KEY overload](explanation/security-model.md#the-secret_key-overload).

## Can I restrict a key to some paths?

Not today. `allowed_paths` is stored but not enforced. Use separate gateways,
IP rules or policies, and test the result.

## Can I change a key's rate limit?

Not in place. Create a new key with the new limits and revoke the old one.

## How do I revoke a key if the list does not show its id?

Read the id from the `api_keys` table, then `DELETE /admin/api-keys/{id}`. If
Redis is enabled, also delete `api_key:<key>` from Redis. See
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

No. They do not match the current tables. The gateway creates its tables when
it starts. See [Upgrade the gateway](how-to/upgrade.md).

## Why is the command called `mcp-gateway` in some places?

The project used to be called MCP Gateway. The old names still work; they are
listed once, in the [CLI reference](reference/cli.md#names-and-aliases).

## Is there a hosted or commercial version?

The repository does not describe one. Everything in it is under the Apache
License 2.0.

## Where do I report a security problem?

Privately, as described in [SECURITY.md](../SECURITY.md). Not in a public
issue.
