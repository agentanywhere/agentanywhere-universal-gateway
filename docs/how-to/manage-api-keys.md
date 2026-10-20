# Create, rotate and revoke API keys

**Goal:** issue an API key to a client, replace it without downtime, and take
it away.

**Before you start:**

- The gateway is running, and you have the admin credential in a shell
  variable: `ADMIN=<your ADMIN_TOKEN>`. Set `URL=http://localhost:8000` (or
  your gateway's address).
- Use `curl` for these steps. The CLI's `keys create` does not work
  ([CLI reference](../reference/cli.md#commands-that-do-not-work-as-written)),
  and the console cannot delete or disable keys.

## Contents

- [Create a key](#create-a-key)
- [Find the id of an existing key](#find-the-id-of-an-existing-key)
- [Rotate a key](#rotate-a-key)
- [Revoke a key](#revoke-a-key)
- [Expire keys automatically](#expire-keys-automatically)
- [If it did not work](#if-it-did-not-work)

## Create a key

1. Choose a name and the limits. Parameters go in the query string:

   ```bash
   curl -s -X POST "$URL/admin/api-keys?name=orders-service&rate_limit_per_minute=120&rate_limit_per_hour=5000&expires_in_days=90" \
     -H "Authorization: Bearer $ADMIN"
   ```

   Use `expires_at=2027-01-01T00:00:00` instead of `expires_in_days` for a
   fixed date (not both).

2. From the response, give the `key` to the client over a secure channel.

3. **Record the `id`.** It is the only time the gateway shows it, and you need
   it to disable or revoke the key.

**Check:** the client's request succeeds:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "$URL/api/health" -H "X-API-Key: <the key>"
```

Any status other than 401 means the key was accepted (the backend decides the
rest).

Limits cannot be changed after creation; there is no update endpoint. To
change them, rotate the key.

## Find the id of an existing key

`GET /admin/api-keys` returns keys without their ids. Read the id from the
database:

```bash
sqlite3 gateway.db "SELECT id, name, is_active, expires_at FROM api_keys;"
```

For PostgreSQL, run the same `SELECT` with `psql`. Under Docker Compose with
the default SQLite file:

```bash
docker compose exec gateway python -c "import sqlite3; print(sqlite3.connect('gateway.db').execute('SELECT id, name FROM api_keys').fetchall())"
```

## Rotate a key

1. Create a new key with the same name and limits (as above).
2. Deploy the new key to the client.
3. Watch for traffic on the old key to stop. `last_used_at` in `api_keys` is
   updated on each request that is not served from the Redis cache.
4. Revoke the old key (next section).

The gateway has no grace period or key versioning; both keys are valid until
you revoke one.

## Revoke a key

1. Delete it by id:

   ```bash
   curl -s -X DELETE "$URL/admin/api-keys/$KEY_ID" -H "Authorization: Bearer $ADMIN"
   ```

   ```json
   {"message": "API key deleted"}
   ```

   To disable it without deleting, use
   `curl -s -X PUT "$URL/admin/api-keys/$KEY_ID/toggle" -H "Authorization: Bearer $ADMIN"`;
   the same call enables it again.

2. **If Redis is enabled** (`REDIS_ENABLED=true` with Redis reachable, which is
   the Docker Compose default), remove the cached entry, or the key keeps
   working for up to five minutes:

   ```bash
   redis-cli -n 0 DEL "api_key:<the revoked key>"
   ```

   Under Compose: `docker compose exec redis redis-cli -n 0 DEL "api_key:<the revoked key>"`.

**Check:** a request with the revoked key answers 401 `AUTHENTICATION_ERROR`
with `Invalid or inactive API key`.

## Expire keys automatically

Keys with `expires_at` in the past are refused on the database path. To mark
them inactive in the table as well:

```bash
curl -s "$URL/admin/governance/api-keys/expiring?within_days=14" -H "Authorization: Bearer $ADMIN"
curl -s -X POST "$URL/admin/governance/api-keys/sweep-expired" -H "Authorization: Bearer $ADMIN"
```

The sweep answers `{"deactivated_count": N}`. The Redis caveat above applies to
expiry too.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| 422 on create, `"location": ["query", "name"]` | Parameters sent as a JSON body | Put them in the query string. |
| 404 `API key not found` on toggle or delete | You passed the key instead of the id, or the id is wrong | Look the id up as shown above. |
| Revoked key still works | Redis key cache | Delete `api_key:<key>` from Redis database 0, or wait five minutes. |
| Keys vanished after a Docker restart | SQLite file inside the container | See [Back up and restore](back-up-and-restore.md). |

Keys are stored in plaintext. See the [security model](../explanation/security-model.md#how-api-keys-are-stored).
