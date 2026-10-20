# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - Unreleased

First public release, under the Apache License 2.0.

### What is in it

- A gateway that authenticates, rate-limits and proxies requests to MCP, A2A,
  AG-UI, ACP, HTTP/REST and GraphQL backends, and publishes to an MQTT broker.
- API keys with per-key limits, expiry and path scoping.
- Rate limiting that works with or without Redis.
- Tenants with per-tenant request quotas and usage counters.
- A circuit breaker per backend. A backend that cannot be reached answers 502,
  and 503 once its circuit is open.
- Routes that can be added at run time through the admin API.
- An admin API and a React admin console, with local sign-in and optional
  OpenID Connect sign-in.
- A policy engine, roles, IP rules, an audit chain, transformations, API
  versioning, webhooks, log exporters, a plugin loader, SDK generation and a
  command-line tool. These are covered by the automated tests.
- Sample backends for every protocol, a Dockerfile and a Compose file.

### Known limitations

- The gRPC bridge and the WebSocket route do not work.
- API keys are stored in plaintext next to their hash, and the admin API
  returns them in full.
- The `/admin/ai/*` analytics endpoints are experimental.

See the README for the full list.

### Fixed

- Database migrations: the Alembic migrations `001` and `002` were replaced by
  one baseline, `0001`, that creates the full current schema. It is safe to
  run on a database the gateway already created at startup, works on SQLite
  and PostgreSQL, and needs no extra driver. A test fails when the models and
  the migrations disagree. See docs/UPGRADING.md.
- Docker Compose: the SQLite database is kept on the `gateway_data` volume
  (`/app/data/gateway.db`) instead of the container's filesystem, so API keys
  survive the container being recreated.
- Docker Compose: the gateway container reaches the sample backends by service
  name; the `localhost` backend URLs from `env.example` no longer leak into it
  (they made every request answer 502). Use `DOCKER_*_BACKEND_URL` for other
  backends.
- Docker Compose: the `postgres` and `grafana` containers refuse to start while
  `POSTGRES_PASSWORD` / `GRAFANA_ADMIN_PASSWORD` are empty or placeholders.
- nginx: `nginx/nginx.conf` is installed as `conf.d/default.conf` (as the main
  `nginx.conf` it did not load), and `/metrics` and `/health/detailed` are
  served to loopback and private addresses only.
- New `OPS_ENDPOINTS_REQUIRE_ADMIN` (default false) requires the admin
  credential on `/metrics` and `/health/detailed`.
- CORS: a `"*"` origin now turns credentials off, with a startup warning, and
  the default origins are the local admin console's instead of `"*"`.

### Security

- The admin API accepts only the configured `ADMIN_TOKEN`, or `SECRET_KEY`
  when it is not the placeholder from `env.example`. There is no default
  admin password, and no credential is accepted because of its prefix or length.
- API-key authentication accepts only keys that exist in the database, plus
  `SECRET_KEY` when it is not the placeholder.
- A credential containing non-ASCII characters is rejected with 401.

[1.0.0]: https://github.com/agentanywhere/universal-gateway/releases/tag/v1.0.0
