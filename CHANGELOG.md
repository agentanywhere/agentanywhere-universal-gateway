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

- Container commands in the backup and upgrade how-tos work in Git Bash on
  Windows: container paths are passed inside `sh -c '...'` (Git Bash rewrote
  a standalone `/app/...` argument, so `rm` and `chown` failed), and the
  whole-volume backup streams the archive to the host instead of mounting a
  host folder.
- Documentation reorganised into tutorials, how-to guides, reference and
  explanation, with a map in `docs/README.md`.
- `scripts/check_docs.py`, `scripts/check_env_vars.py` and
  `scripts/generate_http_reference.py --check` run in CI.
- The two-backends tutorial's tenant step works when re-run: after creating
  `acme` it sets the quota with `PUT /admin/tenants/acme/quota`, so a tenant
  left over from an earlier run (whose create answers 409) is put back to 3
  requests a minute.

### Fixed

- Proxy: a query that gives a key more than once (`?tag=a&tag=b`) now reaches
  the backend with every value. Only the last value was sent on before. A
  transformation rule or plugin that sets, changes or removes such a key
  still decides what is sent for it.
- Proxy: a header a caller gives on more than one line (two `Accept-Language`
  lines, say) now reaches the backend with every line. Only one was sent on
  before: the first, or the last on the way to an agent. A transformation rule
  or plugin still sees one value and, if it sets, changes or removes the
  header, decides what is sent for it. `Host`, `Content-Length` and the
  hop-by-hop headers are sent on one line, as before.
- Proxy: a request whose body came without a length (`Transfer-Encoding:
  chunked`) is sent to the backend with its length and without that header.
  The caller's `Transfer-Encoding` was passed on beside the `Content-Length`
  the gateway adds, which is not a valid request: a backend may refuse it, or
  read where the body ends differently from a proxy in front of it.
- API versions: a sunset date given with a UTC offset (`2027-01-01T00:00:00+00:00`,
  or one ending in `Z`) now works. Comparing it with the current time raised
  an error, so the admin call that set the date failed and so did every
  request under that version.
- Transformations: a rule that sets a header which cannot be written is now
  refused when it is made (400), instead of failing every request it applies
  to (502). That is a header name that is not a token, or a value that starts
  or ends with a space, holds a line break or another control character, or
  holds characters outside ASCII on a request or outside Latin-1 on an
  answer.
- Transformations: a rule that changes a request's body now works. The new
  body was sent with the length the caller declared for the old one, so the
  request never reached the backend and the caller got a 502. Three related
  defects are fixed with it: a rule that sets a field no longer fails on a
  body that is JSON but not an object (such a body is left as it came), text
  that a template makes of a body is sent as that text rather than as a
  quoted JSON string, and a value a rule sets is no longer shared between
  requests, where a later rule could leave one caller's data in it for the
  next.
- MQTT: the gateway's client now survives the broker dropping the connection.
  Before, the first disconnect ended the client's network thread, so it never
  reconnected: `/mqtt/publish` answered 500 `Failed to publish message` until
  the gateway was restarted, while still reporting itself connected. Now it
  answers 503 `MQTT broker not connected` while the broker is away and
  publishes again once it is back.
- Security: a request whose path decodes to `?`, `#` or a control character
  (`/api/x%3F/../admin`) is refused with a 400 (`INVALID_PATH`). The key's
  allowed paths, the policies and the agent edge read the path only up to a
  decoded `?` or `#`, while the proxy forwarded all of it: the backend was sent
  `/api/x?/../admin`, with a query no rule had seen, and an agent was asked for
  less than the caller named.
- Proxy: a request path that decodes to `?`, `#` or a control character is
  forwarded with that character escaped, as path data. A caller's
  `/api/a%3Fadmin=1` reached the backend as `/api/a?admin=1`, a query
  parameter no rule or check had seen; a `%23` cut the rest of the path off;
  and a `%0A` made the request fail.
- `GET /admin/webhooks`: each row's `event_type` is the event that webhook was
  registered for. Without an `event_type` filter every row said `all`, which
  is not an event the gateway sends. The filter still works.
- Admin console: the Protocols page no longer reports every backend as "not
  configured", and the Settings page fills in the backend URLs, environment and
  log level. `GET /admin/config` groups settings into sections; the console now
  reads them from there instead of looking for flat fields.
- `pytest` passes with the `.env` the getting-started tutorial writes:
  `tests/conftest.py` sets an empty `ADMIN_TOKEN` (and
  `ADMIN_ACCEPT_SECRET_KEY=false`) in the environment, which takes precedence
  over `.env`, so the admin tests' `SECRET_KEY` login is accepted (they
  answered 401). `docs/how-to/run-the-tests.md` says so.
- The start-up banner's documentation link (`DOCUMENTATION_URL`) now defaults
  to https://docs.agentanywhere.ai/gateway/ instead of the repository's
  `docs/` folder.

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
- `scripts/generate_ssl_cert.sh` works in Git Bash on Windows: it turns off
  Git Bash's path rewriting (`MSYS_NO_PATHCONV=1`) for the `openssl req`
  call, which had turned the `-subj "/C=US/..."` subject into a Windows path,
  so only `key.pem` was written and nginx would not start.
- New `OPS_ENDPOINTS_REQUIRE_ADMIN` (default false) requires the admin
  credential on `/metrics` and `/health/detailed`.
- CORS: a `"*"` origin now turns credentials off, with a startup warning, and
  the default origins are the local admin console's instead of `"*"`.
- `examples/basic_usage.py`: the HTTP example sends the body the sample HTTP
  backend accepts (`name` and `email`) instead of one it rejected with 422,
  and the error-handling example sends no API key, so it shows the 401 its
  comment describes (it sent `{}` with a valid key and got 422). The gateway
  address can be set with `GATEWAY_URL` (default `http://127.0.0.1:8000`).
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
- Transformation rules: an answer's body is read and written again only when
  a rule that changes bodies applies to the request. Before, registering any
  rule had every JSON answer rewritten (other spacing and escaping), a JSON
  string answer lost its quotes, and a rule for headers or the status was
  skipped when the body was not UTF-8 text.
- With `REDIS_ENABLED=true` and no Redis listening (the `env.example`
  defaults), each proxied request waited for several Redis connection
  attempts, because the rate limiter tried Redis for both the per-minute and
  the per-hour check before falling back to its in-memory counters. After a
  failed Redis call the rate limiter now uses the in-memory counters for 30
  seconds without trying Redis, then tries again, as the API-key cache does
  (#84).
- `tests/test_followup_fixes.py::test_quickstart_script_picks_python_with_requests`
  copies the helper out of `scripts/quickstart_test.sh` in Python instead of
  running `sed`, so it no longer crashes on Git for Windows, which puts
  `bash` on `PATH` but not `sed`.

### Changed (may need action on upgrade; see docs/how-to/upgrade.md)

- `SECRET_KEY` on the data plane now gets 401. Create API keys, or set
  `ALLOW_SECRET_KEY_API_KEY=true` for a first boot with no keys.
- With `ADMIN_TOKEN` set, `SECRET_KEY` is no longer an admin credential
  (`ADMIN_ACCEPT_SECRET_KEY=true` restores it).
- `GET /admin/api-keys` no longer returns keys; new keys are shown once.
- `allowed_paths` is enforced (403 outside it).
- `X-Forwarded-For` is ignored unless `TRUSTED_PROXIES` lists the peer.
- `/docs`, `/redoc`, `/openapi.json` answer 404 with `DEBUG=false`.
- GraphQL requests are passed through. The gateway used to build a new request
  from `query`, `variables` and `operationName`, so the caller's headers
  (`Authorization` among them), `extensions`, `GET` queries and batched bodies
  never reached the server, and every answer that was not a 2xx came back as a
  502. Now the method, headers, query string and body go to the server as they
  came and its status, headers and body come back. A body without `query` is
  no longer refused by the gateway (400); the server answers it. A dynamic
  route that matches a `/graphql` path now applies, as it does elsewhere.

### Security

- Dependencies with known advisories are upgraded (FastAPI, Starlette, anyio,
  cryptography, Authlib, python-multipart, Jinja2, protobuf, gRPC, requests,
  httpx, uvicorn, websockets, pydantic); `pip-audit` reports none for
  `requirements.txt`. Starlette and anyio are now pinned directly.
- Single sign-on tokens are verified with PyJWT instead of python-jose, which
  has an advisory with no fix. The verifier picks the signing key by `kid`,
  accepts RS256 only, checks audience and issuer, and allows 30 seconds of
  clock skew.
- The runtime container image no longer contains pip, setuptools or wheel.
- `SECRET_KEY` is no longer accepted as an API key on the data plane unless
  `ALLOW_SECRET_KEY_API_KEY=true` (off by default). When it is on, every use
  is counted (`gateway_secret_key_api_key_uses_total`), logged and written to
  the governance audit log.
- Audit entries name the admin principal (`admin-token`, `secret-key` or
  `sso:<user>`), never the last characters of the credential.
- Concurrent writes to the governance audit log can no longer fork its hash
  chain.
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
