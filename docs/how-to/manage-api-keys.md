# Create, rotate and revoke API keys

**Goal:** issue an API key to a client, limit what it can reach, replace it
without downtime, and take it away.

**Before you start:** the gateway is running, and you have the admin
credential in a shell variable: `ADMIN=<your ADMIN_TOKEN>`. Set
`URL=http://127.0.0.1:8000` (or your gateway's address). The admin console's
API Keys page and the CLI (`agentanywhere-gateway keys ...`) do the same
things; this page uses `curl`.

## Contents

- [Create a key](#create-a-key)
- [Restrict a key to some paths](#restrict-a-key-to-some-paths)
- [List keys](#list-keys)
- [Rotate a key](#rotate-a-key)
- [Disable or revoke a key](#disable-or-revoke-a-key)
- [Expire keys automatically](#expire-keys-automatically)
- [If it did not work](#if-it-did-not-work)

## Create a key

1. Choose a name and the limits. Send them as query parameters or as a JSON
   body:

   ```bash
   curl -s -X POST "$URL/admin/api-keys" -H "Authorization: Bearer $ADMIN" \
     -H "Content-Type: application/json" \
     -d '{"name": "orders-service", "rate_limit_per_minute": 120, "rate_limit_per_hour": 5000, "expires_in_days": 90}'
   ```

   Use `expires_at` (`"2027-01-01T00:00:00"`) instead of `expires_in_days` for
   a fixed date (not both).

2. Give the `key` from the response to the client over a secure channel.
   **It is shown only this once**: the gateway stores a hash of it, not the
   key.

The response, from an in-process run (your values differ):

```json
{
  "id": "37a9faca-3c3c-4f12-940c-d5671d720671",
  "name": "first-key",
  "key": "sk-fyR0Gj_aKRMbpMmMpCKbz2NpJpWskKOmX583PpC70p0",
  "key_prefix": "sk-fyR0Gj_a",
  "rate_limit_per_minute": 60,
  "rate_limit_per_hour": 1000,
  "is_active": true,
  "created_at": "2026-10-20T19:28:40.389319",
  "expires_at": null,
  "allowed_paths": []
}
```

With the CLI:

```bash
agentanywhere-gateway keys create --name orders-service --rate-limit-minute 120 --rate-limit-hour 5000
```

**Check:** the client's request is accepted:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "$URL/api/health" -H "X-API-Key: <the key>"
```

Any status other than 401 means the key was accepted (the backend decides the
rest). Limits cannot be changed after creation; to change them, rotate the key.

## Restrict a key to some paths

Create the key with `allowed_paths`:

```bash
curl -s -X POST "$URL/admin/api-keys" -H "Authorization: Bearer $ADMIN" \
  -H "Content-Type: application/json" \
  -d '{"name": "mcp-only", "allowed_paths": ["/mcp"]}'
```

As a query parameter, write it comma-separated (`allowed_paths=/mcp,/a2a`).
Each entry matches whole path segments: `/mcp` allows `/mcp` and
`/mcp/tools`, not `/mcpx`. An entry ending in `*` is a plain prefix
(`/api/v*` allows `/api/v1` and `/api/v2`). Paths with `.` or `..` segments
are refused for scoped keys.

**Check:** a request outside the scope answers 403 (reproduced):

```json
{
  "error": "AUTHORIZATION_ERROR",
  "message": "This API key is not allowed to access this path",
  "request_id": "d86c6318-2199-4609-90bf-0474f4e47842",
  "timestamp": "2026-10-20T19:28:40.729176",
  "details": {
    "path": "/api/users",
    "allowed_paths": ["/mcp"],
    "hint": "The key was created with allowed_paths. Use a key whose scope covers this path."
  }
}
```

## List keys

```bash
curl -s "$URL/admin/api-keys" -H "Authorization: Bearer $ADMIN"
```

Each entry has the `id` (which disable and delete take), `name`, `key_prefix`
(to recognise a key), limits, `allowed_paths`, `is_active`, `created_at`,
`expires_at` and `last_used_at`. The key itself is never listed.

## Rotate a key

1. Create a new key with the same name, limits and scope.
2. Deploy the new key to the client.
3. Watch `last_used_at` of the old key in the list stop changing.
4. Revoke the old key (next section).

The gateway has no grace period or key versioning; both keys are valid until
you revoke one.

## Disable or revoke a key

Disable it (the same call enables it again):

```bash
curl -s -X PUT "$URL/admin/api-keys/$KEY_ID/toggle" -H "Authorization: Bearer $ADMIN"
```

```json
{"id": "37a9faca-3c3c-4f12-940c-d5671d720671", "name": "first-key", "is_active": false}
```

Or delete it:

```bash
curl -s -X DELETE "$URL/admin/api-keys/$KEY_ID" -H "Authorization: Bearer $ADMIN"
```

```json
{"message": "API key deleted"}
```

Both take effect on the next request, also when the Redis key cache is in
use: the gateway drops the cache entry.

**Check:** a request with the key answers 401 `AUTHENTICATION_ERROR` with
`Invalid or inactive API key` (reproduced for a disabled key).

## Expire keys automatically

Keys whose `expires_at` has passed answer 401. To mark them inactive in the
table as well:

```bash
curl -s "$URL/admin/governance/api-keys/expiring?within_days=14" -H "Authorization: Bearer $ADMIN"
curl -s -X POST "$URL/admin/governance/api-keys/sweep-expired" -H "Authorization: Bearer $ADMIN"
```

The sweep answers `{"deactivated_count": N}`.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| 422 on create | `name` missing, or a value of the wrong type | Send `name`; numbers as numbers. |
| 400 on create | `expires_at` in the past or not ISO 8601, `expires_in_days` not positive, or both given | Fix the expiry. |
| 404 `API key not found` on toggle or delete | Wrong id (the id is not the key) | Take the `id` from `GET /admin/api-keys`. |
| 403 `This API key is not allowed to access this path` | The key's `allowed_paths` does not cover the path | Use a key with a wider scope. |
| You lost a key | Keys are shown only once | Create a new key and revoke the old one. |

How keys are stored: [security model](../explanation/security-model.md#how-api-keys-are-stored).
