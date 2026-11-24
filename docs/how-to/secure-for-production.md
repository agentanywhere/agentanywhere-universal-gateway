# Secure the gateway for production

**Goal:** change the shipped defaults that are not right outside a laptop, and
know what the gateway still cannot protect.

**Before you start:** read the [security model](../explanation/security-model.md)
and the [known limitations](../known-limitations.md). Nothing in the
repository checks this list for you, and following it does not make the
gateway audited or certified.

## Contents

- [The checklist](#the-checklist)
- [Secrets](#secrets)
- [Network exposure](#network-exposure)
- [Client addresses](#client-addresses)
- [CORS](#cors)
- [Data at rest](#data-at-rest)
- [API keys](#api-keys)
- [Docker Compose defaults](#docker-compose-defaults)
- [What you cannot fix with configuration](#what-you-cannot-fix-with-configuration)
- [Check your work](#check-your-work)

## The checklist

- [ ] `SECRET_KEY` and `ADMIN_TOKEN` are two different random values, and
      `ADMIN_ACCEPT_SECRET_KEY` is false. ([Secrets](#secrets))
- [ ] `SECRET_KEY` is in your secret store and given to no client or operator. ([Secrets](#secrets))
- [ ] TLS is terminated in front of the gateway, and port 8000 is not reachable from outside. ([Network exposure](#network-exposure))
- [ ] `/metrics` and `/health/detailed` are not reachable from the internet:
      `OPS_ENDPOINTS_REQUIRE_ADMIN=true`, or port 8000 firewalled and the nginx
      allow-list matching your monitoring network. ([Network exposure](#network-exposure))
- [ ] `DEBUG=false`, so `/docs`, `/redoc` and `/openapi.json` answer 404. ([Network exposure](#network-exposure))
- [ ] `/admin` is reachable only from operators' networks. ([Network exposure](#network-exposure))
- [ ] `TRUSTED_PROXIES` lists your reverse proxy, and nothing else. ([Client addresses](#client-addresses))
- [ ] `CORS_ORIGINS` lists the exact origins of your front ends, not `*`. ([CORS](#cors))
- [ ] The database is PostgreSQL, or SQLite on a volume that is backed up. ([Data at rest](#data-at-rest))
- [ ] Backups taken before the key-hashing upgrade are protected or destroyed. ([Data at rest](#data-at-rest))
- [ ] `REQUEST_LOG_RETENTION_DAYS` matches how long you may keep request logs. ([Data at rest](#data-at-rest))
- [ ] Keys have expiry dates and, where useful, `allowed_paths`. ([API keys](#api-keys))
- [ ] Redis and PostgreSQL are not reachable from outside their network; the Grafana and PostgreSQL passwords are generated values. ([Docker Compose defaults](#docker-compose-defaults))
- [ ] Sample backends are not running. ([Docker Compose defaults](#docker-compose-defaults))
- [ ] Auth-provider plugins are off unless you mean to bypass key checks. ([What you cannot fix](#what-you-cannot-fix-with-configuration))
- [ ] Log rotation is in place, and log access is restricted.

## Secrets

1. Generate two values:

   ```bash
   python3 scripts/generate_secret_key.py
   python3 scripts/generate_secret_key.py
   ```

2. Set one as `SECRET_KEY` and the other as `ADMIN_TOKEN`, in your secret
   store or `.env` (which is git-ignored). Leave `ADMIN_ACCEPT_SECRET_KEY`
   unset or `false`.

With `ADMIN_TOKEN` set, operators use `ADMIN_TOKEN` and the admin API refuses
`SECRET_KEY`. Leave `ALLOW_SECRET_KEY_API_KEY` unset or `false`, so that
`SECRET_KEY` is not an API key on the data plane either, and keep it secret
anyway.

If `ADMIN_TOKEN` leaks, change it and restart; the console and CLI need to
sign in again.

## Network exposure

1. Put a TLS-terminating proxy in front:
   [Run behind nginx with TLS](run-behind-nginx-tls.md).
2. Close port 8000 to everything except the proxy. `docker-compose.yml`
   publishes it on all interfaces; firewall it or bind it to `127.0.0.1`.
3. Keep `/metrics` and `/health/detailed` private. The shipped nginx serves
   them only to loopback and private addresses. For a check that does not
   depend on the network path, set `OPS_ENDPOINTS_REQUIRE_ADMIN=true` and give
   Prometheus the admin token (see the commented `authorization:` block in
   `prometheus.yml`).
4. Keep `DEBUG=false`. Then `/docs`, `/redoc` and `/openapi.json` answer 404.
5. Allow-list `/admin` to your operators' networks at the proxy, if you can.

## Client addresses

IP rules, policies, logs and metrics use the connection's address unless it
comes from an address in `TRUSTED_PROXIES`. Behind a proxy, list the proxy's
address:

```bash
TRUSTED_PROXIES=10.0.0.5
```

Under Docker Compose, use the `gateway-network` subnet
(`docker network inspect <project>_gateway-network`). Do not list ranges that
clients can send from.

## CORS

The default allows the local admin console only
(`["http://localhost:3001","http://127.0.0.1:3001"]`). Set the exact origins
of the browser applications that call the gateway:

```bash
CORS_ORIGINS=https://app.example.com,https://console.example.com
```

(A JSON array works too.) If the list contains `*`, the gateway turns
credentials off and logs `cors_wildcard_credentials_disabled` at start-up.

## Data at rest

- Use PostgreSQL for anything you cannot afford to lose
  ([Deploy with Docker Compose](deploy-with-docker-compose.md#use-postgresql-instead-of-sqlite)).
  With SQLite under Compose, the file is on the `gateway_data` volume; back it
  up ([Back up and restore](back-up-and-restore.md)).
- New keys are stored as hashes. Rows from earlier builds keep their
  plaintext copy until the key is next used, and backups taken before the
  upgrade contain them: protect or destroy those.
- Keep Redis on a private network with a password.
- `request_logs` keeps every row unless `REQUEST_LOG_RETENTION_DAYS` is set;
  rows older than that many days are then deleted automatically. Set it to the
  period your policy allows. The governance audit log is never pruned.

## API keys

- Create keys with `expires_in_days` and rotate them
  ([Create, rotate and revoke API keys](manage-api-keys.md)).
- Give a key `allowed_paths` when it should reach only some routes; other
  paths answer 403.
- The gateway forwards `X-API-Key` and `Authorization` to backends. If a
  backend must not see callers' keys, strip the header in a transform plugin
  ([Write a plugin](write-a-plugin.md)).

## Docker Compose defaults

| Service (profile) | Default as shipped | Change |
|---|---|---|
| `gateway` | Port 8000 on all interfaces; SQLite on the `gateway_data` volume | Bind to `127.0.0.1` or firewall it; use PostgreSQL or back up the volume. |
| `redis` | No password; `127.0.0.1:6379` | Keep it on the Compose network; add a password if it must be reachable. |
| `postgres` (`production`) | Refuses to start while `POSTGRES_PASSWORD` is empty or starts with `change-me`; `127.0.0.1:5432` | Set a generated `POSTGRES_PASSWORD`. PostgreSQL applies it only when the volume is first initialised; change it later with `ALTER USER`. |
| `grafana` (`monitoring`) | User `admin`; refuses to start while `GRAFANA_ADMIN_PASSWORD` is empty or `admin`; port 3000 on all interfaces | Set a generated password; restrict the port. |
| `prometheus` (`monitoring`) | No authentication; port 9090 on all interfaces | Restrict the port. |
| `admin-ui` | Port 3001 on all interfaces | Put it behind the proxy; restrict access. |
| `nginx` (`production`) | `/metrics` and `/health/detailed` for loopback and private addresses only; overwrites `X-Forwarded-For` | Adjust the `allow` lines to your monitoring network. |
| Sample backends (`backends`) | No authentication; ports 8001 to 8003, 8080, 1883, 9001 and 50051 on all interfaces | Do not run them in production. |

## What you cannot fix with configuration

These need code changes. Plan around them:

- Admin roles are not enforced yet. Every admin credential has full access.
- `PROTOCOL_*_RATE_LIMIT_*`, policy `rate_limit` actions and tenant
  `requests_per_day` are not enforced yet.
- An enabled auth-provider plugin (such as `static-token-auth`) bypasses key
  checks, limits, quotas, IP rules and policies for the requests it accepts.

## Check your work

From outside your network:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://gateway.example.com/metrics            # expect 403 (nginx)
curl -s -o /dev/null -w "%{http_code}\n" https://gateway.example.com/health/detailed    # expect 403 (nginx)
curl -s -o /dev/null -w "%{http_code}\n" https://gateway.example.com/docs               # expect 404
curl -s -o /dev/null -w "%{http_code}\n" https://gateway.example.com/api/anything       # expect 401
curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $SECRET_KEY" \
  https://gateway.example.com/admin/tenants                                             # expect 401 with ADMIN_TOKEN set
curl -s -o /dev/null -w "%{http_code}\n" -H "X-API-Key: $SECRET_KEY" \
  https://gateway.example.com/api/anything                                              # expect 401
curl -s -o /dev/null -w "%{http_code}\n" --max-time 5 http://gateway.example.com:8000/health   # expect a timeout or refusal
```
