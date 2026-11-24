# Upgrade the gateway

**Goal:** move an existing installation to a newer release without losing data,
and know what the version numbers promise.

**Before you start:** read the [CHANGELOG](../../CHANGELOG.md) entry for the
release you are moving to, and have a [backup](back-up-and-restore.md).

## Contents

- [Versioning](#versioning)
- [How the schema is managed](#how-the-schema-is-managed)
- [Steps](#steps)
- [Behaviour changes in this release](#behaviour-changes-in-this-release)
- [Moving the Compose database to the volume](#moving-the-compose-database-to-the-volume)
- [Databases built with the old migrations](#databases-built-with-the-old-migrations)
- [If it did not work](#if-it-did-not-work)

## Versioning

- The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
  and records changes in `CHANGELOG.md` in the
  [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
- The current version is `0.9.0`, the first release (tag `v0.9.0`).
- [SECURITY.md](../../SECURITY.md) lists which versions receive security fixes.
- The version a running gateway reports is `APP_VERSION` (default `0.9.0`),
  shown by `/health`. It is a setting, so check the git tag or the image tag
  to know what code is running.
- A release tag (`v*`) runs `docker-publish.yml`, which pushes the image to
  `ghcr.io/agentanywhere/agentanywhere-universal-gateway`. The PyPI package is
  not published yet (`pypi-publish.yml` runs only by hand).

## How the schema is managed

Two mechanisms create tables, and they agree:

- **At start-up**, `init_db()` runs SQLAlchemy's `create_all`, which creates
  every table the code defines **if it does not exist**. It never alters or
  drops an existing table.
- **Alembic** has one migration, the baseline `0001`
  (`alembic/versions/0001_baseline_schema.py`), which creates every table in
  `app/db_models.py` with the same columns, indexes and foreign keys.

How the baseline behaves:

- On an **empty database** it creates the full schema.
- On a database the **gateway already created**, it does not fail: existing
  tables are left in place, only missing tables, columns and indexes are
  added, and the database is recorded as being at `0001`. A missing `NOT NULL`
  column is added as nullable (the rows already there have no value for it),
  and Alembic logs a warning naming it. Running it again changes nothing.
- It never drops or alters anything on upgrade. `alembic downgrade base` drops
  every table, data included.
- It uses the gateway's own `DATABASE_URL` (from the environment or `.env`)
  and the same async drivers (`aiosqlite`, `asyncpg`); no extra driver is
  needed. For one command against another database:
  `alembic -x db_url=<url> upgrade head` (the password then lands in your
  shell history; prefer setting `DATABASE_URL`).

Both cases were run for this page against scratch SQLite files: an empty one
and one created by the gateway each ended at `0001 (head)`.
`tests/test_migrations.py` checks on every run that the migrated schema
matches the models exactly, so a model change cannot ship without a
migration. It runs against PostgreSQL only when `GATEWAY_TEST_POSTGRES_URL`
points at a disposable database, which CI does not set.

## Steps

1. Back up the database and `.env` ([Back up and restore](back-up-and-restore.md)).
2. Read the changelog, and [Behaviour changes in this release](#behaviour-changes-in-this-release)
   if you come from an earlier build.
3. Run the new version against a copy of the data first.
4. Install the new code:

   ```bash
   git fetch --tags
   git checkout <new tag or commit>
   pip install -r requirements.txt
   ```

   or, with Docker Compose, `docker compose up -d --build`.
5. Apply schema changes, with the same `DATABASE_URL` the gateway uses, from
   the repository root:

   ```bash
   alembic upgrade head
   alembic current          # 0001 (head)
   ```

   Under Compose: `docker compose exec gateway alembic upgrade head`.
   `scripts/setup_production.sh` runs this step when `USE_POSTGRES=true`, and
   stops if it fails.
6. Start (or restart) the gateway.

**Check:**

```bash
curl -s http://127.0.0.1:8000/health    # "version" is the expected APP_VERSION
curl -s http://127.0.0.1:8000/ready     # "status": "ready"
```

and make one request with an existing API key.

## Behaviour changes in this release

There is no schema change: API keys are hashed using the existing `key_hash`
column. These behaviours changed and may need action:

| Change | What to do |
|---|---|
| With `ADMIN_TOKEN` set, `SECRET_KEY` no longer authenticates on `/admin/*` (or on `/metrics` and `/health/detailed` with `OPS_ENDPOINTS_REQUIRE_ADMIN=true`). | Give operators and Prometheus `ADMIN_TOKEN`, or set `ADMIN_ACCEPT_SECRET_KEY=true` for the old behaviour. |
| `SECRET_KEY` is no longer accepted as an API key on the data plane (401). | Create API keys with `POST /admin/api-keys`. For a first boot with no keys, `ALLOW_SECRET_KEY_API_KEY=true` restores it; every use is counted, logged and audited. |
| Audit entries name the admin principal (`admin-token`, `secret-key` or `sso:<user>`) instead of the last eight characters of the credential. Existing rows keep their old actor. | Update any audit query that filters on `actor`. |
| Keys are stored as hashes. `GET /admin/api-keys` returns `id` and `key_prefix`, never the key. Existing keys keep working and lose their plaintext copy on first use. | Record new keys when you create them; they cannot be shown again. |
| `allowed_paths` is enforced: 403 outside it. | Check keys created with it. |
| `X-Forwarded-For` is ignored unless the connection comes from `TRUSTED_PROXIES` (empty by default). | Behind a proxy, list its address. |
| `/docs`, `/redoc` and `/openapi.json` answer 404 with `DEBUG=false`. | Use `DEBUG=true` locally. |
| Rate-limit counters are named after a hash of the key. | Current minute and hour windows start again once. |
| Per-minute tenant usage rows older than a day are deleted, at start-up and daily. Request logs are kept unless `REQUEST_LOG_RETENTION_DAYS` is set. | To prune request logs, set `REQUEST_LOG_RETENTION_DAYS`. |
| CORS defaults to the local admin console's origins; `*` turns credentials off. | Set `CORS_ORIGINS`. |
| Docker Compose keeps SQLite at `/app/data/gateway.db` on the `gateway_data` volume, and ignores `*_BACKEND_URL` from `.env` in favour of `DOCKER_*_BACKEND_URL`. | See the next section, and [Deploy with Docker Compose](deploy-with-docker-compose.md). |
| GraphQL requests are passed to the backend as they came (method, headers, query string, body) and its answer comes back whole. Before, the gateway sent only `query`, `variables` and `operationName`, without the caller's headers, refused a body without `query` with a 400, and turned every backend error into a 502. | Check that the GraphQL server expects the caller's headers (`X-API-Key`, `Authorization` and cookies now reach it), and that clients handle the server's own error statuses. |
| The `postgres` and `grafana` services refuse to start without `POSTGRES_PASSWORD` and `GRAFANA_ADMIN_PASSWORD`. | Set them in `.env`. |

## Moving the Compose database to the volume

Earlier Compose files kept SQLite at `/app/gateway.db` inside the container.
If the old container still exists:

1. Copy the file out: `docker compose cp gateway:/app/gateway.db ./gateway.db`.
2. Delete any `DATABASE_URL=sqlite+aiosqlite:///./gateway.db` line from
   `.env` (older `env.example` files had it); it would override the volume
   path.
3. Start the new version, then copy the file in and give it to the
   container's user:

   ```bash
   docker compose cp ./gateway.db gateway:/app/data/gateway.db
   docker compose exec -u root gateway sh -c 'chown gateway:gateway /app/data/gateway.db'
   docker compose restart gateway
   ```

   The container path is inside `sh -c '...'` because Git Bash on Windows
   rewrites an argument that starts with `/` before Docker sees it; see
   [Back up and restore](back-up-and-restore.md#back-up-sqlite).

## Databases built with the old migrations

Earlier builds shipped migrations `001` and `002`, which described an older
schema; a database built with them did not work with the gateway (creating a
key failed). They were replaced by `0001`. If you ran them,
`alembic upgrade head` stops with "Can't locate revision identified by '002'".
Do not re-stamp that database. Back it up, create an empty database, run
`alembic upgrade head` (or start the gateway) against it, and copy your rows
across.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| `Can't locate revision identified by '002'` | The database was built with the old migrations | See the section above. |
| A warning that a `NOT NULL` column was added as nullable | The table existed without that column | Fill the column for existing rows if your data needs it. |
| Operators get 401 on the admin API after the upgrade | They were using `SECRET_KEY` while `ADMIN_TOKEN` is set | Use `ADMIN_TOKEN`, or `ADMIN_ACCEPT_SECRET_KEY=true`. |
| Every client appears to come from one address | `TRUSTED_PROXIES` is empty behind a proxy | List the proxy. |
| API keys gone after a Docker upgrade | The database was inside the old container | Restore your copy as in [Moving the Compose database](#moving-the-compose-database-to-the-volume). |
| `pip install` fails | Python 3.13, or a missing build tool | Use Python 3.10 to 3.12. |
