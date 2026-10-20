# Set rate limits and tenant quotas

**Goal:** cap how many requests each client, and each customer account, can
make.

**Before you start:** the gateway is running, `ADMIN` holds the admin
credential and `URL` the gateway address. Read
[the two kinds of limit](#the-two-kinds-of-limit) first.

## Contents

- [The two kinds of limit](#the-two-kinds-of-limit)
- [Set a per-key limit](#set-a-per-key-limit)
- [Run more than one process](#run-more-than-one-process)
- [Set a tenant quota](#set-a-tenant-quota)
- [Things to know](#things-to-know)
- [If it did not work](#if-it-did-not-work)

## The two kinds of limit

| | Per-key rate limit | Tenant quota |
|---|---|---|
| Applies to | One API key | Every key associated with the tenant |
| Windows | Per minute and per hour (see [things to know](#things-to-know)) | Per minute and per hour, calendar-aligned (the minute or hour starting at :00) |
| Counted in | Redis (`RATE_LIMIT_REDIS_URL`) or process memory | The `tenant_usage` database table |
| Set with | Query parameters when the key is created | The tenant's `quota` |
| Over the limit | 429, `Retry-After: 60` or `3600` | 429, `Retry-After: 60` or `3600`, with `tenant_id` and `quota` in `details` |

Both apply: a request must pass the key's limit first, then the tenant's
quota. A request that the key's limit rejects is not counted against the
tenant. A request that the tenant quota rejects has already been counted
against the key's limit.

Settings that look like limits but are **not enforced**:
`PROTOCOL_*_RATE_LIMIT_*`, a policy with a `rate_limit` action, and a tenant's
`requests_per_day`.

## Set a per-key limit

1. Create the key with its limits:

   ```bash
   curl -s -X POST "$URL/admin/api-keys?name=web-app&rate_limit_per_minute=5&rate_limit_per_hour=200" \
     -H "Authorization: Bearer $ADMIN"
   ```

2. To change the limits of an existing key, create a new key and revoke the
   old one ([rotation](manage-api-keys.md#rotate-a-key)). There is no update
   endpoint.

`RATE_LIMIT_PER_MINUTE` and `RATE_LIMIT_PER_HOUR` apply only to principals
without their own limits: `SECRET_KEY` used as a key, and OAuth2 tokens.
`RATE_LIMIT_ENABLED=false` turns per-key limiting off (and can be changed at
run time with `PUT /admin/config`).

**Check:**

```bash
for i in 1 2 3 4 5 6; do
  curl -s -o /dev/null -w "%{http_code}\n" "$URL/api/users" -H "X-API-Key: $WEB_KEY"
done
```

The sixth request answers 429. The
[end-to-end tutorial](../tutorials/two-backends-end-to-end.md#4-hit-the-per-key-rate-limit)
shows the output.

## Run more than one process

With in-memory counters each process counts on its own. Point every process at
the same Redis:

```bash
RATE_LIMIT_REDIS_URL=redis://redis.internal:6379/1
```

If Redis is unreachable, the gateway falls back to in-memory counters without
an error, so watch Redis health.

## Set a tenant quota

1. Create the tenant with its quota:

   ```bash
   curl -s -X POST "$URL/admin/tenants" -H "Authorization: Bearer $ADMIN" \
     -H "Content-Type: application/json" \
     -d '{"tenant_id": "acme", "name": "Acme Corp", "quota": {"requests_per_minute": 300, "requests_per_hour": 10000}}'
   ```

   Quota keys you leave out take the defaults: 1000 a minute and 10000 an
   hour. A value of 0 or less removes the limit for that window.

2. Associate each of the tenant's keys. You need both the id and the key:

   ```bash
   curl -s -X POST "$URL/admin/tenants/acme/associate-api-key" -H "Authorization: Bearer $ADMIN" \
     -H "Content-Type: application/json" \
     -d "{\"api_key_id\": \"$KEY_ID\", \"api_key\": \"$KEY\"}"
   ```

3. To change the quota later, send only the keys you want to change; they are
   merged:

   ```bash
   curl -s -X PUT "$URL/admin/tenants/acme/quota" -H "Authorization: Bearer $ADMIN" \
     -H "Content-Type: application/json" -d '{"requests_per_minute": 600}'
   ```

**Check:** read the tenant's usage:

```bash
curl -s "$URL/admin/tenants/acme/usage?window=minute&lookback=10" -H "Authorization: Bearer $ADMIN"
```

`current.minute.request_count` grows with each successful request. Responses
for a tenant's key also carry `X-Tenant-ID: acme`.

To stop a tenant's traffic without deleting it,
`POST /admin/tenants/acme/deactivate`: its keys then pass the quota check
without a tenant (the per-key limit still applies). To block the keys, revoke
them.

## Things to know

- Tenant usage is written after the response, in the background. A burst of
  concurrent requests can exceed the quota by a few.
- Requests rejected with 401 or 429 are not counted in usage or metrics.
- Per-key windows in memory start at the key's first request and last 60 or
  3600 seconds. With Redis, the counter's expiry is renewed on every allowed
  request, so the count resets 60 or 3600 seconds after the last allowed
  request instead. Tenant windows start on the minute or the hour.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| The limit is never reached with several workers | In-memory counters per process | Set `RATE_LIMIT_REDIS_URL`. |
| The tenant quota is never reached | The key is not associated, or the tenant is inactive | Associate the key; check `GET /admin/tenants/acme`. |
| 429 sooner than expected | The tenant quota is lower than the key's limit, or several clients share the key | Check `details` in the 429 body: tenant errors include `tenant_id`. |
| `requests_per_day` has no effect | Not enforced | Use the hour quota. |
