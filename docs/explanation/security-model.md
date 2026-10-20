# Security model

This page states plainly what authenticates what in AgentAnywhere Universal
Gateway, how credentials are stored, and what the gateway does not protect. It
is written from `app/middleware/auth.py`, `admin/api.py`, `app/config.py` and
`app/ida_auth.py`. To report a vulnerability, follow [SECURITY.md](../../SECURITY.md).

## Contents

- [The credentials](#the-credentials)
- [What authenticates what](#what-authenticates-what)
- [How API keys are stored](#how-api-keys-are-stored)
- [The admin credential](#the-admin-credential)
- [The SECRET_KEY overload](#the-secret_key-overload)
- [What reaches the backend](#what-reaches-the-backend)
- [Paths that need no credential](#paths-that-need-no-credential)
- [What the gateway does not do](#what-the-gateway-does-not-do)

## The credentials

| Credential | Who holds it | Where it is set |
|---|---|---|
| **API key** (`sk-...`) | Clients of the data plane | Created with `POST /admin/api-keys`; stored in the `api_keys` table. |
| **Admin credential** | Operators, the admin console, the CLI | `ADMIN_TOKEN`. `SECRET_KEY` is also accepted (see below). |
| **OIDC access token** (optional) | Operators signing in through an OpenID Connect provider | Accepted on the admin plane when `IDA_ISSUER` is set. |
| **OAuth2 access token** (optional) | Clients of the data plane | Issued through `/oauth2/token` from a provider registered with `POST /admin/oauth2/providers`. |
| **`SECRET_KEY`** | The operator | `.env` or the environment. |

## What authenticates what

| Request | Accepted credentials | Checked by |
|---|---|---|
| Data plane (`/mcp/*`, `/api/*`, the catch-all and the others) | An API key in `X-API-Key` (the header name is `API_KEY_HEADER`) or `Authorization: Bearer`; a gateway OAuth2 access token; `SECRET_KEY` if it is not the placeholder; anything an enabled auth-provider plugin accepts. | Authentication middleware |
| Admin plane (`/admin/*`) | `Authorization: Bearer` with an OIDC access token (if `IDA_ISSUER` is set), `ADMIN_TOKEN`, or `SECRET_KEY` if it is not the placeholder. | `verify_admin_token` on each endpoint |
| `/admin/agents/stream` | The admin credential as `?token=` or as a bearer header. | The endpoint itself |
| `/ws/{path}` | Intended: an API key as `?api_key=` or `X-API-Key`. As shipped every connection is closed. | The WebSocket route |
| `/agents/<slug>/...` (agent edge) | Nothing at the gateway. `Authorization` and cookies are forwarded and the upstream must authenticate. | The upstream |
| `/oauth2/*`, health, metrics, discovery documents | Nothing. | |

An API key is never accepted on the admin plane, and `ADMIN_TOKEN` is not
accepted on the data plane. `SECRET_KEY` is accepted on both.

Credentials are compared in constant time (`app/secure_compare.py`). A value
containing non-ASCII characters is rejected with 401.

## How API keys are stored

When you create a key, the gateway generates `sk-` followed by 32 random bytes
in URL-safe base64, and stores a row in `api_keys` with:

- `key`: the key itself, **in plaintext**;
- `key_hash`: its SHA-256 hash, used for lookups;
- the name, limits, optional expiry, optional `allowed_paths`, and timestamps.

Consequences:

- Anyone who can read the database, or a backup of it, has every key.
- `GET /admin/api-keys` returns every key in full to any admin.
- With Redis enabled, keys appear in Redis key names: `api_key:<key>` (the
  five-minute cache in database 0) and `rate_limit:<key>:60` and
  `rate_limit:<key>:3600` (counters in the `RATE_LIMIT_REDIS_URL` database).
- Tenant associations store only the hash (`tenant_api_keys.api_key_hash`).

Revocation and expiry are checked on the database path. A key served from the
Redis cache is accepted for up to 300 seconds without those checks; see
[known limitations](../known-limitations.md#a-revoked-or-disabled-key-keeps-working-for-up-to-five-minutes-when-redis-is-enabled).

`allowed_paths` is stored but not enforced.

## The admin credential

- The admin API accepts, as a bearer token, `ADMIN_TOKEN` (if set) **or**
  `SECRET_KEY` (if it does not start with `change-me`, the placeholder in
  `env.example`). Both are accepted at the same time: setting `ADMIN_TOKEN`
  does not stop `SECRET_KEY` from working. With an empty `ADMIN_TOKEN` and the
  placeholder `SECRET_KEY`, **no** credential is accepted and the admin API
  answers 401 to everything. There is no default password.
- The console's sign-in form takes a user name (`ADMIN_USERNAME`, default
  `admin`) and a password: `ADMIN_TOKEN` when it is set, otherwise
  `SECRET_KEY`.
  `POST /admin/auth/login` returns that same credential as the access token,
  and the console sends it as a bearer token on every call. There is no
  session, no expiry and no second credential.
- `GET /admin/config` returns `ADMIN_TOKEN` in clear text (it is not in the
  redacted fields), while it redacts `SECRET_KEY`, database and Redis URLs and
  third-party tokens.
- Admin roles (`/admin/governance/roles`) are stored but not enforced. Every
  valid admin credential has full access.
- With `IDA_ISSUER` set, the admin plane also accepts RS256 access tokens from
  that issuer, for the audience `IDA_ADMIN_AUDIENCE` (default `gateway-admin`)
  and with one of the realm roles in `IDA_ADMIN_ROLES` (default
  `platform_admin,tenant_admin`). The local credential keeps working.

## The SECRET_KEY overload

`SECRET_KEY` is described in `env.example` and `app/config.py` as a signing
key, next to `ALGORITHM` and `ACCESS_TOKEN_EXPIRE_MINUTES`. In this release no
code signs anything with it, and those two settings are not read. What it
actually does:

1. It is accepted as an admin credential on every admin endpoint, whether or
   not `ADMIN_TOKEN` is set, and it is the console password when
   `ADMIN_TOKEN` is empty.
2. It is accepted as an API key on every data-plane route, with the default
   rate limits (`RATE_LIMIT_PER_MINUTE`, `RATE_LIMIT_PER_HOUR`), so that a new
   installation can make a request before any key exists.

The placeholder value (anything starting with `change-me`) is refused for
both. With `ENVIRONMENT=production` and the placeholder, the gateway prints
a warning to stderr and still starts.

So `SECRET_KEY` opens both planes. What to do: set `SECRET_KEY` and
`ADMIN_TOKEN` to two different random values
(`python3 scripts/generate_secret_key.py` prints one), give operators
`ADMIN_TOKEN` only, keep `SECRET_KEY` in your secret store, and never give it
to a client.

## What reaches the backend

The gateway forwards the caller's request headers, including `X-API-Key`,
`Authorization` and cookies, to the backend. It removes only headers that start
with `X-Gateway-`. A backend therefore sees the gateway API key of every
caller. If that is not acceptable, remove the header in a transform plugin
([Write a plugin](../how-to/write-a-plugin.md)).

The gateway does not add an identity header for the authenticated key. A
backend that needs to know the caller must use its own mechanism.

## Paths that need no credential

`/health`, `/live`, `/ready`, `/health/detailed`, `/metrics`, `/openapi.json`,
`/docs`, `/redoc`, `/.well-known/agent-card.json`, `/.well-known/ag-ui.json`,
`/oauth2/*`, and on the admin plane `/admin/health`, `/admin/auth/config` and
`/admin/auth/login`.

- `/metrics` and `/health/detailed` disclose operational detail: request paths
  and counts, dependency status and the version.
- `/openapi.json` describes every endpoint, including the admin API.
- `/docs` and `/redoc` are only served when `DEBUG=true`. Otherwise they fall
  through to the catch-all proxy **without** an API key check and are sent to
  the default backend.

Block these at your reverse proxy unless you want them public; see
[Secure the gateway for production](../how-to/secure-for-production.md).

## What the gateway does not do

- It does not terminate TLS. Put a reverse proxy in front of it
  ([Run behind nginx with TLS](../how-to/run-behind-nginx-tls.md)).
- It does not encrypt anything at rest.
- It does not scope keys to paths, even with `allowed_paths`.
- It does not enforce admin roles.
- It does not check that a backend is who it claims to be beyond what `httpx`
  does for `https://` URLs.
- CORS defaults allow every origin with credentials; set `CORS_ORIGINS`.

The full list, with workarounds, is on the
[known limitations](../known-limitations.md) page.
