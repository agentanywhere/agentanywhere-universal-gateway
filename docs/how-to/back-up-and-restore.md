# Back up and restore

**Goal:** keep a copy of everything the gateway needs to come back after a
loss, and put it back.

**Before you start:** know which database you use (`DATABASE_URL`) and where
your `.env` or secret store lives. Backups contain every API key in plaintext:
encrypt them and restrict who can read them.

## What to back up

| What | Where | Why |
|---|---|---|
| The database | The SQLite file (default `./gateway.db` in the working directory, `/app/gateway.db` in the container) or your PostgreSQL database | API keys, tenants and usage, dynamic routes, request logs, settings profiles, governance rules and audit log, OpenAPI registrations, agent registry. |
| Configuration | `.env`, or the environment and `CONFIG_FILE` | `SECRET_KEY`, `ADMIN_TOKEN`, backend URLs, everything else. |
| Certificates | `nginx/ssl/` | If you use the bundled nginx. |
| Your run-time admin calls | Your own scripts | Webhooks, transformation rules, API versions, OAuth2 providers and log exporters added through the admin API are only in memory and are lost on every restart. Keep the calls that create them in a script. |

Redis does not need a backup: it holds rate-limit counters and a five-minute
key cache, both rebuilt from traffic.

## Back up SQLite

On the host:

```bash
sqlite3 gateway.db ".backup 'gateway-$(date +%Y%m%d).db'"
```

`.backup` takes a consistent copy while the gateway is running. A plain `cp`
of a file that is being written can be inconsistent.

From the Compose container (the image has no `sqlite3` tool, so copy the file
out):

```bash
docker compose cp gateway:/app/gateway.db ./gateway-$(date +%Y%m%d).db
```

Take this copy **before** anything that recreates the container.

## Back up PostgreSQL

```bash
pg_dump --format=custom --file=gateway-$(date +%Y%m%d).dump "postgresql://gateway_user:<password>@<host>:5432/gateway"
```

With the Compose `postgres` service:

```bash
docker compose exec -T postgres pg_dump -U gateway_user --format=custom gateway > gateway-$(date +%Y%m%d).dump
```

## Restore

1. Stop the gateway.
2. Put the database back:
   - SQLite: copy the backup to the path in `DATABASE_URL`
     (`docker compose cp ./gateway-20261020.db gateway:/app/gateway.db` for
     the container, then restart it).
   - PostgreSQL: `pg_restore --clean --if-exists --dbname=<url> gateway-20261020.dump`.
3. Restore `.env` or the environment. Use the **same** `SECRET_KEY` and
   `ADMIN_TOKEN` if clients or operators hold them.
4. Start the gateway. It creates any tables missing from the backup and leaves
   the others alone.
5. Re-run your script of in-memory admin calls (webhooks and the rest).

## Check that it worked

```bash
curl -s http://localhost:8000/ready
curl -s http://localhost:8000/admin/tenants -H "Authorization: Bearer $ADMIN"
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/api/anything -H "X-API-Key: <a key from before the loss>"
```

`/ready` answers `"status": "ready"`, your tenants are listed, and the old key
gets anything but 401.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| Old keys answer 401 | The gateway is reading a different database file (relative SQLite path, different working directory) | Use an absolute path in `DATABASE_URL`. |
| `table ... has no column named ...` | The backup comes from an older schema, or was built with Alembic | See [Upgrade the gateway](upgrade.md). |
| `/ready` answers 503 | The database is not reachable | Check `DATABASE_URL` and the database server. |
