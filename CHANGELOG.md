# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - Unreleased

First public release, under the Apache License 2.0.

### What is in it

- A gateway that authenticates, rate-limits and proxies requests to MCP, A2A,
  AG-UI, ACP, HTTP/REST and GraphQL backends, and publishes to an MQTT broker.
- API keys with per-key limits and expiry. A path list (`allowed_paths`) is
  stored but not enforced.
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
- The Alembic migrations do not match the models.

See [docs/known-limitations.md](docs/known-limitations.md) for the full list.

### Documentation

- Documentation reorganised into tutorials, how-to guides, reference and
  explanation, with a map in `docs/README.md`.
- `scripts/check_docs.py`, `scripts/check_env_vars.py` and
  `scripts/generate_http_reference.py --check` run in CI.

### Security

- The admin API accepts only the configured `ADMIN_TOKEN`, or `SECRET_KEY`
  when it is not the placeholder from `env.example`. There is no default
  admin password, and no credential is accepted because of its prefix or length.
- API-key authentication accepts only keys that exist in the database, plus
  `SECRET_KEY` when it is not the placeholder.
- A credential containing non-ASCII characters is rejected with 401.

[1.0.0]: https://github.com/agentanywhere/agentanywhere-universal-gateway/releases/tag/v1.0.0
