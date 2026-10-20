# Secure the gateway for production

**Goal:** change the shipped defaults that are unsafe outside a laptop, and
know what the gateway still cannot protect.

**Before you start:** read the [security model](../explanation/security-model.md)
and the [known limitations](../known-limitations.md). Nothing in the
repository checks this list for you, and following it does not make the
gateway audited or certified.

## Contents

- [The checklist](#the-checklist)
- [Secrets](#secrets)
- [Network exposure](#network-exposure)
- [CORS](#cors)
- [Data at rest](#data-at-rest)
- [API keys](#api-keys)
- [Docker Compose defaults](#docker-compose-defaults)
- [What you cannot fix with configuration](#what-you-cannot-fix-with-configuration)
- [Check your work](#check-your-work)

## The checklist

Each item links to the details below.

- [ ] `SECRET_KEY` and `ADMIN_TOKEN` are two different random values. ([Secrets](#secrets))
- [ ] `SECRET_KEY` is not given to any client or operator; it opens both planes. ([Secrets](#secrets))
- [ ] TLS is terminated in front of the gateway, and port 8000 is not reachable from outside. ([Network exposure](#network-exposure))
- [ ] `/metrics`, `/health/detailed`, `/openapi.json`, `/docs` and `/redoc` are blocked or allow-listed at the proxy. ([Network exposure](#network-exposure))
- [ ] `/admin` is reachable only from operators' networks. ([Network exposure](#network-exposure))
- [ ] The proxy overwrites `X-Forwarded-For`. ([Network exposure](#network-exposure))
- [ ] `CORS_ORIGINS` lists exact origins, not `*`. ([CORS](#cors))
- [ ] The database is PostgreSQL, or SQLite on a volume that is backed up. ([Data at rest](#data-at-rest))
- [ ] Database files, backups and Redis are treated as secrets, because they contain API keys in plaintext. ([Data at rest](#data-at-rest))
- [ ] `request_logs` is pruned on a schedule. ([Data at rest](#data-at-rest))
- [ ] Keys have expiry dates, and you know how to revoke one, including the Redis cache step. ([API keys](#api-keys))
- [ ] You do not rely on `allowed_paths`, admin roles or per-protocol limits. ([What you cannot fix](#what-you-cannot-fix-with-configuration))
- [ ] Redis and PostgreSQL are not reachable from outside their network; default passwords are changed. ([Docker Compose defaults](#docker-compose-defaults))
- [ ] Sample backends are not running. ([Docker Compose defaults](#docker-compose-defaults))
- [ ] Auth-provider plugins are off unless you mean to bypass key checks. ([What you cannot fix](#what-you-cannot-fix-with-configuration))
- [ ] Log rotation is in place (`scripts/rotate_logs.sh` or your platform's), and log access is restricted.

## Secrets

1. Generate two values:

   ```bash
   python3 scripts/generate_secret_key.py
   python3 scripts/generate_secret_key.py
   ```

2. Set one as `SECRET_KEY` and the other as `ADMIN_TOKEN`, in your secret
   store or `.env` (which is git-ignored).

Why two: operators get `ADMIN_TOKEN`, which is not valid on the data plane.
`SECRET_KEY` is accepted on **both** planes (as an API key, and as an admin
bearer token even when `ADMIN_TOKEN` is set), so keep it in your secret store
and give it to no one.

`GET /admin/config` and the CLI's `config show` print `ADMIN_TOKEN` in clear
text. Do not paste their output anywhere public. If `ADMIN_TOKEN` leaks, change
it and restart; the console and CLI need to sign in again.

## Network exposure

1. Put a TLS-terminating proxy in front:
   [Run behind nginx with TLS](run-behind-nginx-tls.md).
2. Block, or allow-list, the paths that need no credential: `/metrics`,
   `/health/detailed`, `/openapi.json`, `/docs`, `/redoc`. With `DEBUG=false`
   (the default), `/docs` and `/redoc` are forwarded to your default backend
   without an API key check.
3. Allow-list `/admin` to your operators' networks, if you can.
4. Overwrite `X-Forwarded-For` at the first proxy
   (`proxy_set_header X-Forwarded-For $remote_addr;`), so IP rules and logs see
   the real client address.
5. Keep `DEBUG=false`.

## CORS

Set the exact origins of the browser applications that call the gateway, as a
JSON array:

```bash
CORS_ORIGINS=["https://app.example.com","https://console.example.com"]
```

If no browser calls the gateway across origins, set `CORS_ORIGINS=[]`.
Consider `CORS_ALLOW_CREDENTIALS=false` unless a front end needs cookies.

## Data at rest

- Use PostgreSQL for anything you cannot afford to lose
  ([Deploy with Docker Compose](deploy-with-docker-compose.md#use-postgresql-instead-of-sqlite)).
  With SQLite under Docker, the file is lost when the container is recreated.
- The `api_keys` table holds every key in plaintext. Restrict database
  access, encrypt backups, and keep Redis (which holds keys in key names) on a
  private network with a password.
- Prune `request_logs` regularly:

  ```sql
  DELETE FROM request_logs WHERE timestamp < now() - interval '30 days';
  ```

  (PostgreSQL syntax; for SQLite use `datetime('now', '-30 days')`.)

## API keys

- Create keys with `expires_in_days` and rotate them
  ([Create, rotate and revoke API keys](manage-api-keys.md)).
- Record each key's `id` when you create it; you need it to revoke.
- After revoking, delete `api_key:<key>` from Redis database 0, or the key
  works for up to five minutes more.
- Remember the gateway forwards `X-API-Key` and `Authorization` to backends. A
  compromised backend learns every caller's key. If that matters, strip the
  header in a transform plugin ([Write a plugin](write-a-plugin.md)).

## Docker Compose defaults

| Service (profile) | Default as shipped | Change |
|---|---|---|
| `gateway` | Port 8000 on all interfaces; SQLite inside the container | Bind to `127.0.0.1` or firewall it; use PostgreSQL. |
| `redis` | No password; `127.0.0.1:6379` | Keep it on the Compose network; add a password if it must be reachable. |
| `postgres` (`production`) | `POSTGRES_PASSWORD` falls back to `change-me-in-production`; `127.0.0.1:5432` | Set `POSTGRES_PASSWORD`. |
| `grafana` (`monitoring`) | User `admin`, password `admin` unless `GRAFANA_ADMIN_PASSWORD`; port 3000 on all interfaces | Set the password; restrict the port. |
| `prometheus` (`monitoring`) | No authentication; port 9090 on all interfaces | Restrict the port. |
| `admin-ui` | Port 3001 on all interfaces | Put it behind the proxy; restrict access. |
| Sample backends (`backends`) | No authentication; ports 8001 to 8003, 8080, 1883, 9001 and 50051 on all interfaces | Do not run them in production. |

## What you cannot fix with configuration

These need code changes. Plan around them:

- `allowed_paths` on keys is not enforced. A key can call every route.
- Admin roles are not enforced. Every admin credential has full access.
- `PROTOCOL_*_RATE_LIMIT_*`, policy `rate_limit` actions and tenant
  `requests_per_day` are not enforced.
- API keys are stored in plaintext and returned in full by the admin API.
- An enabled auth-provider plugin (such as `static-token-auth`) bypasses key
  checks, limits, quotas, IP rules and policies for the requests it accepts.
- Rejected requests are not counted in metrics; count 401 and 429 at the proxy.

## Check your work

From outside your network:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://gateway.example.com/metrics            # expect 403
curl -s -o /dev/null -w "%{http_code}\n" https://gateway.example.com/health/detailed    # expect 403
curl -s -o /dev/null -w "%{http_code}\n" https://gateway.example.com/docs               # expect 403
curl -s -o /dev/null -w "%{http_code}\n" https://gateway.example.com/api/anything       # expect 401
curl -s -o /dev/null -w "%{http_code}\n" http://gateway.example.com:8000/health          # expect a timeout or refusal
```

There is no check that `SECRET_KEY` is refused on `/admin`: it is always
accepted there. Keep it secret instead.
