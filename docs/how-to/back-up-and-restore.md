# Back up and restore

**Goal:** keep a copy of everything the gateway needs to come back after a
loss, and put it back.

**Before you start:** know which database you use (`DATABASE_URL`) and where
your `.env` or secret store lives. New API keys are stored as hashes, but
rows created by earlier builds keep the key in plaintext until it is next
used, and backups contain everything else the gateway knows: encrypt them and
restrict who can read them.

## What to back up

| What | Where | Why |
|---|---|---|
| The database | The SQLite file (default `./gateway.db` in the working directory; under Docker Compose `/app/data/gateway.db` on the `gateway_data` volume) or your PostgreSQL database | API keys, tenants and usage, dynamic routes, request logs, settings profiles, governance rules and audit log, OpenAPI registrations, agent registry. |
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

Under Docker Compose the file is on the `gateway_data` volume, which survives
`docker compose down`, rebuilds and container recreation, but not
`docker compose down -v` or `make clean`. An online copy, using SQLite's
backup API from Python inside the container (the image has no `sqlite3`
tool):

```bash
docker compose exec gateway python -c "import sqlite3; s=sqlite3.connect('/app/data/gateway.db'); d=sqlite3.connect('/app/data/backup.db'); s.backup(d); d.close(); s.close()"
docker compose cp gateway:/app/data/backup.db ./gateway-backup-$(date +%F).db
docker compose exec gateway sh -c 'rm /app/data/backup.db'
```

> **Git Bash on Windows** rewrites a command-line argument that starts with
> `/` into a path under the Git installation before Docker sees it, so
> `docker compose exec gateway rm /app/data/backup.db` would look for a file
> that does not exist. The commands on this page therefore pass container
> paths inside `sh -c '...'`, or as part of a longer argument such as
> `gateway:/app/data/backup.db`, which Git Bash leaves alone. If you type
> such a command yourself, wrap it the same way or start the line with
> `MSYS_NO_PATHCONV=1`. Linux, macOS and WSL are unaffected.

The whole volume, with the gateway stopped (Compose prefixes the volume with
the project name; find it with `docker volume ls | grep gateway_data`):

```bash
docker compose stop gateway
docker run --rm -v <project>_gateway_data:/data alpine \
  sh -c 'tar czf - -C /data .' > gateway-data-$(date +%F).tgz
docker compose start gateway
```

The archive is written to standard output and saved on the host by the
shell, so no host folder is mounted into the container.

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
   - SQLite: copy the backup to the path in `DATABASE_URL`. For the Compose
     volume, either extract the archive into it the same way
     (`docker run --rm -i -v <project>_gateway_data:/data alpine sh -c 'tar xzf - -C /data' < gateway-data-<date>.tgz`),
     or copy a single file in with
     `docker compose cp ./gateway-backup.db gateway:/app/data/gateway.db`
     (this works on the stopped container); after step 4, run
     `docker compose exec -u root gateway sh -c 'chown gateway:gateway /app/data/gateway.db'`
     and `docker compose restart gateway`.
   - PostgreSQL: `pg_restore --clean --if-exists --dbname=<url> gateway-20261020.dump`.
3. Restore `.env` or the environment. Use the **same** `SECRET_KEY` and
   `ADMIN_TOKEN` if clients or operators hold them.
4. Start the gateway. It creates any tables missing from the backup and leaves
   the others alone.
5. Re-run your script of in-memory admin calls (webhooks and the rest).

## Check that it worked

```bash
curl -s http://127.0.0.1:8000/ready
curl -s http://127.0.0.1:8000/admin/tenants -H "Authorization: Bearer $ADMIN"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/anything -H "X-API-Key: <a key from before the loss>"
```

`/ready` answers `"status": "ready"`, your tenants are listed, and the old key
gets anything but 401.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| Old keys answer 401 | The gateway is reading a different database file (relative SQLite path, different working directory) | Use an absolute path in `DATABASE_URL`. |
| `table ... has no column named ...` | The backup comes from an older schema, or was built with Alembic | See [Upgrade the gateway](upgrade.md). |
| `/ready` answers 503 | The database is not reachable | Check `DATABASE_URL` and the database server. |
