# Troubleshoot 401, 403, 404, 502 and 429

**Goal:** find out why a request through the gateway fails with one of the
most common errors, and fix it.

**Before you start:** have the full response body (`curl -s`, not `-o
/dev/null`), the gateway's logs, and the admin credential in `ADMIN`. Every
error body has an `error` code; the [errors reference](../reference/errors.md)
lists them all. Every error response carries an `X-Request-ID` header and the
same value as `request_id` in the body; search the gateway log for it.

## Contents

- [401](#401)
- [403](#403)
- [404 for /docs, /redoc or /openapi.json](#404-for-docs-redoc-or-openapijson)
- [502](#502)
- [429](#429)
- [Still stuck](#still-stuck)

## 401

1. Read `error` and `message`.

   | `error` / `message` | Plane | Meaning |
   |---|---|---|
   | `AUTHENTICATION_ERROR` / `API key required` | Data | No key in the request. |
   | `AUTHENTICATION_ERROR` / `Invalid or inactive API key...` | Data | Key unknown, disabled or expired. |
   | `HTTP_ERROR` / `Authentication required` | Admin | No `Authorization: Bearer` header. |
   | `HTTP_ERROR` / `Invalid admin token` | Admin | Wrong admin credential. |

2. **Data plane, no key:** send it as `X-API-Key: <key>` (or the header named
   by `API_KEY_HEADER`) or `Authorization: Bearer <key>`. Check for an empty
   shell variable: `echo "[$API_KEY]"`.

3. **Data plane, invalid key:**
   - Is the key in this gateway's database? List the keys:
     `curl -s "$URL/admin/api-keys" -H "Authorization: Bearer $ADMIN"`.
     Find yours by `key_prefix` (the list never shows the key itself). If the
     list is empty after a restart, the database was replaced (a deleted
     Compose volume, or a different working directory).
   - Is it active and unexpired? The list shows `is_active` and `expires_at`.
   - Did you send the `id` instead of the `key`? Keys start with `sk-`.

4. **Admin plane:** the credential is `ADMIN_TOKEN`. `SECRET_KEY` (if it does
   not start with `change-me`) works only while `ADMIN_TOKEN` is empty, unless
   `ADMIN_ACCEPT_SECRET_KEY=true`. If both are unset or placeholders, every
   admin call answers 401 until you set one and restart. `.env` is read from
   the directory the gateway was started in.

5. **`/metrics` or `/health/detailed`:** with `OPS_ENDPOINTS_REQUIRE_ADMIN=true`
   they need the admin credential as a bearer token.

6. **The console rejects your sign-in:** user name `ADMIN_USERNAME` (default
   `admin`); password `ADMIN_TOKEN`, or `SECRET_KEY` when `ADMIN_TOKEN` is
   empty.

**Check:** the same request with the corrected credential answers something
other than 401.

## 403

1. Read `message`:
   - `This API key is not allowed to access this path`: the key was created
     with `allowed_paths`, and `details` lists them. Use a key whose scope
     covers the path. Paths with `.` or `..` segments are refused for scoped
     keys.
   - `Request blocked by IP rule` or a policy's reason: check
     `GET /admin/governance/ip-rules` and `GET /admin/governance/policies`.
     The client address is the connection's unless it comes from an address in
     `TRUSTED_PROXIES`; behind a proxy without that setting, every request
     comes from the proxy.
2. A plain 403 page from nginx on `/metrics` or `/health/detailed` means the
   client is outside nginx's allow-list.

## 404 for /docs, /redoc or /openapi.json

With `DEBUG=false` (the default), the gateway answers 404 for `/docs`, `/redoc`
and `/openapi.json`. Set `DEBUG=true` (on a development machine) to serve them.
The [generated endpoint list](../reference/http-endpoints.md) describes the API
without them.

## 502

`BACKEND_ERROR` with `Backend service unavailable` means the gateway could not
connect to the backend, or the backend did not answer within
`DEFAULT_TIMEOUT` seconds (30 by default).

1. Find which backend the path goes to. `/api/*`, `/rest/*`, `/http/*` and
   unknown prefixes use `HTTP_BACKEND_URL`; `/mcp/*` uses `MCP_BACKEND_URL`;
   and so on ([Configure routes and backends](configure-routes-and-backends.md)).
   A dynamic route overrides this: check `GET /admin/routes`.

2. Find the URL the gateway is actually using:

   ```bash
   curl -s "$URL/admin/config?section=backends" -H "Authorization: Bearer $ADMIN"
   ```

3. Test that URL **from where the gateway runs**:

   ```bash
   curl -s http://localhost:8003/health                          # gateway on your machine
   docker compose exec gateway wget -qO- http://http-backend:8003/health   # gateway in Compose
   ```

4. Fix the cause:
   - **Under Docker Compose.** The gateway uses the sample backends' service
     names unless you set `DOCKER_*_BACKEND_URL`. Start the samples with
     `docker compose --profile backends up -d`, or point the `DOCKER_` variable
     at an address the container can reach (inside it, `localhost` is the
     gateway itself); see [Deploy with Docker Compose](deploy-with-docker-compose.md).
   - **The backend is not running.** Start it.
   - **The backend is slow.** Raise `DEFAULT_TIMEOUT`, or for AG-UI and ACP
     ask for streaming (`Accept: text/event-stream`).

5. After the fix, if you now get **503** `SERVICE_UNAVAILABLE` with
   `circuit open`, the breaker for that backend opened after
   `CIRCUIT_BREAKER_FAILURE_THRESHOLD` (5) failures. It lets a request through
   again `CIRCUIT_BREAKER_TIMEOUT` (60) seconds after it opened. Each process
   has its own breakers; restarting the gateway also closes them.

**Check:** the request returns the backend's own response. The gateway log has
no new `backend_unavailable` lines.

Related: a **500** `INTERNAL_ERROR` on `/agui`, `/acp`, `/ws` or `/mqtt/...`
usually means that protocol's backend URL is not set.

## 429

1. Read `details`:
   - `{"limit", "window", "retry_after"}` only: the **key's** per-minute or
     per-hour limit.
   - with `tenant_id` and `quota`: the **tenant's** quota.

2. Wait `Retry-After` seconds, or change the limit:
   - Key limit: limits cannot be edited. Create a key with higher limits and
     revoke the old one ([Create, rotate and revoke API keys](manage-api-keys.md#rotate-a-key)).
   - Tenant quota:
     `curl -s -X PUT "$URL/admin/tenants/<id>/quota" -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" -d '{"requests_per_minute": 600}'`.

3. If the limit is hit sooner than expected:
   - several clients share the key, or the key is used for several backends
     (the limit counts all of them);
   - the key belongs to a tenant whose quota is lower than the key's limit;
   - with Redis, the counter resets 60 seconds after the last **allowed**
     request, not after the first.

4. If the limit is **never** hit with several workers, counters are in memory
   per process; set `RATE_LIMIT_REDIS_URL`.

**Check:** after the window, requests answer 200 again. Use
`GET /admin/tenants/<id>/usage?window=minute` to watch tenant counters.

## Still stuck

- Search the gateway log, and `GET /admin/logs`, for the `request_id` of the
  failed request.
- See the [troubleshooting page](../troubleshooting.md) for other symptoms and
  the [known limitations](../known-limitations.md).
- Ask for help as described in [Support](../support.md).
