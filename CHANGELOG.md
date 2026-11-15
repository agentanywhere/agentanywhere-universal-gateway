# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - Unreleased

First public release, under the Apache License 2.0.

### What is in it

- A gateway that authenticates, rate-limits and proxies requests to MCP, A2A,
  AG-UI, ACP, HTTP/REST and GraphQL backends, and publishes to an MQTT broker.
- API keys with per-key limits, expiry and a path scope (`allowed_paths`).
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

### Deployment

- Compose publishes every port except nginx's 80 and 443 on `127.0.0.1` (change with `BIND_ADDRESS`), so the gateway's own port no longer bypasses nginx.
- The admin console's production build works (`admin-ui/package.json` is an ES module) and CI builds it.

### Known limitations

- The gRPC bridge and the WebSocket route do not work.
- API keys created by earlier builds stay in plaintext in the database until
  they are next used (new keys are stored as a hash only).
- Per-protocol rate limits (`PROTOCOL_*_RATE_LIMIT_*`), the `rate_limit`
  policy action, the tenant `requests_per_day` quota and admin roles are
  stored or reported but not enforced yet. `ALGORITHM` and
  `ACCESS_TOKEN_EXPIRE_MINUTES` are not used: nothing signs tokens yet.
- The `/admin/ai/*` analytics endpoints are experimental.

See [docs/known-limitations.md](docs/known-limitations.md) for the full list.

### Documentation

- Documentation reorganised into tutorials, how-to guides, reference and
  explanation, with a map in `docs/README.md`.
- `scripts/check_docs.py`, `scripts/check_env_vars.py` and
  `scripts/generate_http_reference.py --check` run in CI.

### Fixed

- A backend that sets more than one cookie has each `Set-Cookie` line passed on as its own line. They were
  joined into one line by commas, which a browser does not read as cookies.
- Database migrations: the Alembic migrations `001` and `002` were replaced by
  one baseline, `0001`, that creates the full current schema. It is safe to
  run on a database the gateway already created at startup, works on SQLite
  and PostgreSQL, and needs no extra driver. A test fails when the models and
  the migrations disagree. See docs/how-to/upgrade.md.
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
- Requests rejected by the auth layer (401, 403, 429) now carry
  `X-Request-ID`, have `request_id` in the body, are counted in
  `gateway_requests_total` (path label reduced to its first segment or
  `/other`) and are written to `request_logs` with the error code.
  `gateway_rate_limit_hits_total` is now incremented.
- The `rate_limit_exceeded` webhook and the email/Slack rate-limit alert are
  sent (they failed on a missing key id), for the per-hour limit too.
- `GET /admin/api-keys` returns each key's `id` and a `key_prefix`, never the
  key; the admin console's delete and enable/disable buttons use the id
  (they answered 404). The console shows a new key once, after creating it.
- `POST /admin/api-keys` and `POST /admin/webhooks` also accept a JSON body;
  `DELETE /admin/webhooks/{id}` no longer needs `event_type`. The CLI's
  `keys create`, `webhooks create` and `webhooks delete` work (they failed
  with 422), and `logs --status` filters (it sent an ignored parameter).
- Every list setting accepts a comma-separated value as well as a JSON array
  (`PLUGINS_DISABLED=a,b` stopped start-up).
- `MCP_GATEWAY_PLUGIN_PATH`, `MCP_GATEWAY_PLUGIN_DISABLED` and `OTEL_*` are
  read from `.env` too, not only from the process environment.
- `scripts/setup_production.sh` stops when PostgreSQL was requested but
  cannot be set up, when Alembic is missing, or when `DATABASE_URL` is not a
  PostgreSQL URL; it no longer replaces a `SECRET_KEY` already set in `.env`.
  `scripts/setup_postgres.sh` gives up after `POSTGRES_WAIT_SECONDS` (60) and
  no longer prints the database password.
- `admin-ui/src/lib/utils.ts` was never committed (the root `.gitignore`
  ignored every `lib/`), so the console did not type-check or build from a
  clean clone.

### Changed (may need action on upgrade; see docs/how-to/upgrade.md)

- With `ADMIN_TOKEN` set, `SECRET_KEY` is no longer an admin credential
  (`ADMIN_ACCEPT_SECRET_KEY=true` restores it).
- `GET /admin/api-keys` no longer returns keys; new keys are shown once.
- `allowed_paths` is enforced (403 outside it).
- `X-Forwarded-For` is ignored unless `TRUSTED_PROXIES` lists the peer.
- `/docs`, `/redoc`, `/openapi.json` answer 404 with `DEBUG=false`.

### Security

- The admin API accepts only the configured `ADMIN_TOKEN`, or `SECRET_KEY`
  when it is not the placeholder from `env.example`. There is no default
  admin password, and no credential is accepted because of its prefix or length.
- API-key authentication accepts only keys that exist in the database, plus
  `SECRET_KEY` when it is not the placeholder.
- A credential containing non-ASCII characters is rejected with 401.
- `SECRET_KEY` is accepted on the admin API only while `ADMIN_TOKEN` is empty
  (it used to be accepted even when `ADMIN_TOKEN` was set). New
  `ADMIN_ACCEPT_SECRET_KEY` (default false) restores the old behaviour.
- `GET /admin/config`, `/admin/config/diff` and the config audit log redact
  `ADMIN_TOKEN`, every setting whose name ends in a secret word (`_TOKEN`,
  `_SECRET`, `_PASSWORD`, `_API_KEY`, ...), `PLUGIN_CONFIGS_JSON`,
  `OTEL_EXPORTER_OTLP_HEADERS` and the password of any `user:password@` URL.
- An API key's `allowed_paths` is enforced: requests outside it answer 403
  (prefixes match whole path segments; paths with `.`/`..` segments are
  refused for scoped keys). It used to be stored and ignored.
- Deleting or disabling a key takes effect at once with the Redis key cache:
  the entry is dropped. Cache entries are keyed by the key's hash, hold no key,
  and never outlive the key's expiry.
- API keys are stored as a SHA-256 hash only. The full key is returned once,
  on creation. Rows written by earlier builds keep working and lose their
  plaintext copy on first use; no schema change or migration is needed.
  Rate-limit counters are named after a hash of the key, not the key.
- With `DEBUG=false` (the default), `/docs`, `/redoc` and `/openapi.json`
  answer 404 from the gateway. They used to skip authentication and be
  proxied to the default backend, and `/openapi.json` described every route.
- New `TRUSTED_PROXIES` (default empty): `X-Forwarded-For` is ignored unless
  the connection comes from a listed proxy, and is then read from the right.
  The client address used to be the header's first entry, which the client
  chooses. `nginx/nginx.conf` now overwrites the header with `$remote_addr`.


[1.0.0]: https://github.com/agentanywhere/agentanywhere-universal-gateway/releases/tag/v1.0.0
