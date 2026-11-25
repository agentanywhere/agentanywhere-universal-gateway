# What the gateway is, and what it is not

## What it is

AgentAnywhere Universal Gateway is a reverse proxy, written in Python with
FastAPI, that you run in front of agent and application backends. It gives
every backend behind it the same front door:

- **one credential scheme**: API keys created through an admin API;
- **one set of limits**: per-key requests per minute and per hour, and
  per-tenant quotas;
- **one place to look**: Prometheus metrics, structured JSON logs, a request
  log table and health endpoints;
- **one place to steer traffic**: routing by path prefix, routes added at run
  time, a circuit breaker per backend, request and response transformations,
  and plugins.

It knows enough about MCP, A2A, AG-UI and ACP to validate their payloads,
strip prefixes and stream Server-Sent Events, and it can publish to an MQTT
broker and forward GraphQL queries. The
[protocol support matrix](protocol-support.md) says exactly what works.

It is licensed under Apache-2.0. Copyright ShepHertz Technologies Pvt. Ltd.

## Who it is for

- Teams that run several agent or API services and want one place to issue
  keys, apply limits and collect metrics.
- Developers who want to put an MCP, A2A, AG-UI or ACP server behind
  authentication without changing the server.
- People who are comfortable operating a Python service, a database and,
  for more than one process, Redis.

## Who it is not for

- Anyone who needs a gateway they can expose to the internet without reading
  the [known limitations](../known-limitations.md) first. Several settings that look like controls (admin roles,
  per-protocol limits) are not enforced yet.
- Anyone who needs gRPC without the experimental Rust data plane. The Python
  gateway on its own cannot accept gRPC clients.
- Anyone who needs a high-throughput edge proxy. The gateway buffers most
  responses in Python, writes a database row per request and has no published
  performance figures.

## What it does not do

- It does not terminate TLS. Use a reverse proxy; the repository includes an
  nginx configuration.
- It does not implement MCP, A2A or the other protocols as a server. It sits
  in front of servers that do.
- It does not enforce admin roles or apply per-protocol rate limits yet,
  although it stores settings for both.
- It does not ship Kubernetes manifests or a Helm chart.
- It does not provide user accounts. The admin console has a single shared
  credential, or OpenID Connect sign-in if you configure an issuer.

## How it relates to other AgentAnywhere software

The repository does not describe any commercial edition, hosted service or
other product that this code is part of. It is licensed under the Apache
License 2.0 (`LICENSE`); `NOTICE` lists the third-party dependencies that come
under their own licences. The optional OpenID Connect settings (`IDA_*`) were
written against Keycloak, according to the comments in `app/config.py` and
`app/ida_auth.py`; they validate RS256 access tokens from the issuer you
configure.

## Where to go next

- [Architecture](architecture.md): how a request moves through the gateway.
- [Security model](security-model.md): what authenticates what.
- [Design decisions and trade-offs](design-decisions.md): why it works the way
  it does, and what that costs.
- [Getting started](../tutorials/getting-started.md): run it.
