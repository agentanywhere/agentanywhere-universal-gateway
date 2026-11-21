# Security model

This page states plainly what authenticates what in AgentAnywhere Universal
Gateway, how credentials are stored, and what the gateway does not protect. It
is written from `app/middleware/auth.py`, `app/api_key_cache.py`,
`app/client_ip.py`, `admin/api.py`, `app/config.py` and `app/ida_auth.py`. To
report a vulnerability, follow [SECURITY.md](../../SECURITY.md).

## Contents

- [The credentials](#the-credentials)
- [What authenticates what](#what-authenticates-what)
- [How API keys are stored](#how-api-keys-are-stored)
- [Key scope, expiry and revocation](#key-scope-expiry-and-revocation)
- [The admin credential](#the-admin-credential)
- [The SECRET_KEY break-glass credential](#the-secret_key-break-glass-credential)
- [The client address](#the-client-address)
- [What reaches the backend](#what-reaches-the-backend)
- [Paths that need no credential](#paths-that-need-no-credential)
- [What the gateway does not do](#what-the-gateway-does-not-do)

## The credentials

| Credential | Who holds it | Where it is set |
|---|---|---|
| **API key** (`sk-...`) | Clients of the data plane | Created with `POST /admin/api-keys`; stored as a hash in `api_keys`. |
| **Admin credential** | Operators, the admin console, the CLI | `ADMIN_TOKEN`. |
| **OIDC access token** (optional) | Operators signing in through an OpenID Connect provider | Accepted on the admin plane when `IDA_ISSUER` is set. |
| **OAuth2 access token** (optional) | Clients of the data plane | Issued through `/oauth2/token` from a provider registered with `POST /admin/oauth2/providers`. The client-credentials grant is given only to a caller that presents that provider's `client_id` and `client_secret` (HTTP Basic or in the body). |
| **`SECRET_KEY`** | The operator, kept in a secret store | `.env` or the environment. A break-glass credential (see below). |

## What authenticates what

| Request | Accepted credentials | Checked by |
|---|---|---|
| Data plane (`/mcp/*`, `/api/*`, the catch-all and the others) | An API key in `X-API-Key` (the header name is `API_KEY_HEADER`) or `Authorization: Bearer`; a gateway OAuth2 access token; `SECRET_KEY` if it is not the placeholder; anything an enabled auth-provider plugin accepts. | Authentication middleware |
| Admin plane (`/admin/*`) | `Authorization: Bearer` with `ADMIN_TOKEN`; an OIDC access token if `IDA_ISSUER` is set; `SECRET_KEY` only while `ADMIN_TOKEN` is empty (or always, with `ADMIN_ACCEPT_SECRET_KEY=true`). | `verify_admin_token` on each endpoint |
| `/metrics` and `/health/detailed` | Nothing by default. With `OPS_ENDPOINTS_REQUIRE_ADMIN=true`, the admin credential, checked the same way. | The endpoints |
| `/admin/agents/stream` | The admin credential as `?token=` or as a bearer header. | The endpoint itself |
| `/ws/{path}` | Intended: an API key as `?api_key=` or `X-API-Key`. As shipped every connection is closed. | The WebSocket route |
| `/agents/<slug>/...` (agent edge) | Nothing at the gateway. `Authorization` and cookies are forwarded and the upstream must authenticate. | The upstream |
| `/oauth2/*`, `/health`, `/live`, `/ready`, discovery documents | Nothing. | |

An API key is never accepted on the admin plane, and `ADMIN_TOKEN` is not
accepted on the data plane. `SECRET_KEY` is accepted on the data plane always,
and on the admin plane as described above.

Credentials are compared in constant time (`app/secure_compare.py`). A value
containing non-ASCII characters is rejected with 401.

## How API keys are stored

When you create a key, the gateway generates `sk-` followed by 32 random bytes
in URL-safe base64 and returns it **once**, in the response to
`POST /admin/api-keys`. It stores:

- `key_hash`: the SHA-256 hash of the key, used for lookups;
- the first characters of the key (shown as `key_prefix`), the name, limits,
  optional expiry, optional `allowed_paths` and timestamps.

The key itself is not stored, so it cannot be shown again: `GET
/admin/api-keys` returns each key's `id` and `key_prefix`, never the key.

Rows written by earlier builds still hold the key in the `key` column. They
keep working, and the plaintext copy is removed the first time the key
authenticates. Keys that are never used again keep their copy, and so do
backups taken before the upgrade.

With Redis enabled (`REDIS_ENABLED=true`), validated keys are cached for up to
five minutes under `api_key:<hash>`; the cache entry holds no key. Rate-limit
counters are named after a hash of the key too. Tenant associations store only
the hash.

## Key scope, expiry and revocation

- **Scope.** A key created with `allowed_paths` gets 403
  `AUTHORIZATION_ERROR` for any other path. Each entry is a path prefix
  matched on whole segments (`/mcp` allows `/mcp` and `/mcp/tools`, not
  `/mcpx`); an entry ending in `*` is a plain string prefix. Paths with `.` or
  `..` segments are refused for scoped keys. An empty list allows every path.
- **Expiry.** A key past its `expires_at` answers 401. Cache entries never
  outlive the key's expiry.
- **Revocation.** Deleting or disabling a key drops its cache entry, so the
  next request is refused, also with Redis shared between processes.

## The admin credential

- `ADMIN_TOKEN` is empty by default. While it is empty, a `SECRET_KEY` that
  does not start with `change-me` (the placeholder in `env.example`) is the
  admin credential. With both unset or placeholder, the admin API answers 401
  to everything. There is no default password.
- The console's sign-in form takes a user name (`ADMIN_USERNAME`, default
  `admin`) and a password: `ADMIN_TOKEN`, or `SECRET_KEY` while `ADMIN_TOKEN`
  is empty. `POST /admin/auth/login` returns that same credential as the
  access token, and the console sends it as a bearer token on every call.
  There is no session, no expiry and no second credential.
- `GET /admin/config`, `/admin/config/diff` and the config audit log redact
  `SECRET_KEY`, `ADMIN_TOKEN`, every setting whose name ends in a secret word
  (`_TOKEN`, `_SECRET`, `_PASSWORD`, `_API_KEY` and similar),
  `PLUGIN_CONFIGS_JSON`, `OTEL_EXPORTER_OTLP_HEADERS`, the Redis and database
  URLs, and the password of any `user:password@` URL. `?redacted=false`
  returns raw values to an admin.
- Admin roles (`/admin/governance/roles`) are stored but not enforced yet.
  Every valid admin credential has full access.
- With `IDA_ISSUER` set, the admin plane also accepts RS256 access tokens from
  that issuer, for the audience `IDA_ADMIN_AUDIENCE` (default `gateway-admin`)
  and with one of the realm roles in `IDA_ADMIN_ROLES` (default
  `platform_admin,tenant_admin`). The local credential keeps working.

## The SECRET_KEY break-glass credential

`SECRET_KEY` sits next to `ALGORITHM` and `ACCESS_TOKEN_EXPIRE_MINUTES` in the
settings, but nothing in this release signs anything with it, and those two
settings are not read. What it does:

1. It is accepted as an API key on every data-plane route, with the default
   rate limits (`RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_PER_HOUR`), so that a new
   installation can make a request before any key exists. This is still true
   when `ADMIN_TOKEN` is set.
2. It is the admin credential while `ADMIN_TOKEN` is empty. Once `ADMIN_TOKEN`
   is set, the admin API refuses it, unless `ADMIN_ACCEPT_SECRET_KEY=true`
   (the behaviour of earlier builds).

The placeholder value (anything starting with `change-me`) is refused for
both. A request authenticated with `SECRET_KEY` as an API key has no key id,
so the access log records the key itself as `api_key_id`
([known limitations](../known-limitations.md#using-secret_key-as-an-api-key-writes-it-to-the-logs)). With `ENVIRONMENT=production` and the placeholder, the gateway prints a
warning to stderr and still starts.

What to do: set `SECRET_KEY` and `ADMIN_TOKEN` to two different random values
(`python3 scripts/generate_secret_key.py` prints one), give operators
`ADMIN_TOKEN`, keep `SECRET_KEY` in your secret store, and never give it to a
client.

## The client address

IP rules, policies, rate limits for unauthenticated principals, logs and
metrics use the client address from `app/client_ip.py`:

- With `TRUSTED_PROXIES` empty (the default), `X-Forwarded-For` is ignored and
  the client address is the connection's peer.
- When the peer is in `TRUSTED_PROXIES` (addresses or CIDR ranges), the header
  is read from the right, and the last entry that is not itself a trusted
  proxy is the client. The left part, which the client chooses, is never used.

Behind nginx or a load balancer, list its address; otherwise every request
appears to come from the proxy. The shipped `nginx/nginx.conf` overwrites
`X-Forwarded-For` with `$remote_addr`.

## What reaches the backend

The gateway forwards the caller's request headers, including `X-API-Key`,
`Authorization` and cookies, to the backend. It removes only headers that start
with `X-Gateway-`. If a backend must not see gateway keys, remove the header in
a transform plugin ([Write a plugin](../how-to/write-a-plugin.md)).

The gateway does not add an identity header for the authenticated key.

## Paths that need no credential

`/health`, `/live`, `/ready`, `/.well-known/agent-card.json`,
`/.well-known/ag-ui.json`, `/oauth2/*`, `/admin/health`, `/admin/auth/config`
and `/admin/auth/login`; `/metrics` and `/health/detailed` unless
`OPS_ENDPOINTS_REQUIRE_ADMIN=true`; and, only with `DEBUG=true`, `/docs`,
`/redoc` and `/openapi.json`. With `DEBUG=false` (the default) those three
answer 404 from the gateway.

`/metrics` and `/health/detailed` disclose request paths and counts,
dependency status and the version. The shipped nginx serves them to loopback
and private addresses only. The Compose file publishes the gateway's own port
on `127.0.0.1` by default (set by `BIND_ADDRESS`); see [Secure the gateway for production](../how-to/secure-for-production.md).

## What the gateway does not do

- It does not terminate TLS. Put a reverse proxy in front of it
  ([Run behind nginx with TLS](../how-to/run-behind-nginx-tls.md)).
- It does not encrypt anything at rest.
- It does not enforce admin roles yet.
- It does not check that a backend is who it claims to be beyond what `httpx`
  does for `https://` URLs.

The full list, with workarounds, is on the
[known limitations](../known-limitations.md) page.
