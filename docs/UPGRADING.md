# Upgrading and the database schema

> Reference material, written from reading the code and the migration files.
> The commands on this page were not run against a live PostgreSQL server for
> this release. Try them on a copy of your data first.

This page explains how the gateway's database tables are created, what that
means when you move to a newer release, and what Alembic does and does not do
here. If you only need a fresh install, you do not have to do anything: start
the gateway and it creates its tables.

## What happens at startup

When the gateway starts, `app/main.py` calls `init_db()` (in `app/database.py`),
which runs SQLAlchemy's `Base.metadata.create_all`. That creates every table
the current code defines **if the table does not exist**. It never adds a column
to a table that already exists, never changes a column type and never drops
anything.

So on a fresh database you get the full current schema. On an existing database,
tables that earlier releases already created stay as they were, and only tables
that are new in this release are added.

## What Alembic covers

The `alembic/` directory has two migrations:

| Revision | Creates |
|---|---|
| `001` | `api_keys`, `request_logs`, `settings_profiles`, `protocol_settings` |
| `002` | `tenants`, `tenant_api_keys`, `tenant_usage` |

`alembic.ini` points at `alembic/`, and `alembic/env.py` takes the database URL
from the gateway's `DATABASE_URL` setting (default `sqlite:///./gateway.db`),
after removing `+asyncpg` or `+aiosqlite` from it.

Read these limits before you use it:

- **The migrations do not describe the current schema.** The tables defined in
  `app/db_models.py` that no migration creates are `routes`, `agent_registry`,
  `governance_audit_log`, `admin_roles`, `governance_policies`,
  `governance_ip_rules` and `openapi_registrations`. For two tables that both
  have, the columns differ: the current `api_keys` model has `key`,
  `allowed_paths`, `expires_at` and `last_used_at`, which `001` does not create,
  and the current `request_logs` model has `timestamp`, `user_agent` and
  `tenant_id` (and an integer id), where `001` creates `created_at`, `metadata`
  and a string id. `protocol_settings` is created by `001` but has no model in
  `app/db_models.py`. A database built only from these migrations would not
  match what the code expects, and `create_all` will not repair it, because the
  tables already exist.
- **There are no migrations for later changes.** No migration exists for any
  schema change after `002`.
- **They are written for PostgreSQL.** They use `server_default` of `now()`.
  They have not been checked against SQLite.
- **No synchronous PostgreSQL driver is in `requirements.txt`.** The URL is
  rewritten to `postgresql://`, which SQLAlchemy opens with `psycopg2` unless
  you say otherwise. Install a driver yourself if you run Alembic against
  PostgreSQL.
- **Order matters.** `001` creates tables without checking whether they exist.
  If the gateway has already started once against the database (so `create_all`
  made the tables), `alembic upgrade head` fails with "already exists".

Because of this, we do not recommend `alembic upgrade head` for a new install
today. This is a known gap and not something these notes can paper over; the
fix is new, tested migrations, which is a code change.

## What to run

**New install, any database:** do nothing. Start the gateway.

**Upgrading to a release that only adds tables:** start the new version. The
new tables appear; existing tables are untouched.

**Upgrading to a release that changes an existing table** (a new column, for
example): `create_all` will not apply it. The release notes in
[CHANGELOG.md](../CHANGELOG.md) should say so. In that case you have to alter
the table yourself with SQL, or create a new database and copy the data across.
Take a backup first. Remember that `api_keys` holds keys in plaintext.

**If you do want to try Alembic** (on a copy, and only on a database that the
gateway has never started against):

```bash
pip install psycopg2-binary      # or another synchronous driver for your database
alembic upgrade head             # creates the tables listed above
```

and then start the gateway, which adds the tables that no migration creates.
Compare the result with `app/db_models.py` before you trust it. If the gateway
has already created the schema and you only want Alembic to record a revision,
`alembic stamp head` marks the database as up to date without running anything;
it changes no tables.

## SQLite and more than one process

The default database is a SQLite file, `./gateway.db`, relative to the working
directory.

- SQLite is a single file on one host. It is fine for one gateway process. If
  you run several worker processes (`uvicorn --workers N`) or several replicas,
  use PostgreSQL. Replicas cannot share a SQLite file across hosts, and writes
  from several processes to one file are serialised and can fail with "database
  is locked".
- With the repository's `docker-compose.yml`, the gateway container writes the
  SQLite file inside the container (`/app/gateway.db`). The Compose file mounts
  only `./logs`, so the file is lost when the container is removed and
  recreated, and with it every API key. Set `DATABASE_URL` to PostgreSQL, or add
  a volume for the file, before you rely on the data.
- Other state is also per process: with no `RATE_LIMIT_REDIS_URL`, rate-limit
  counters live in each process's memory. See [CONFIGURATION.md](CONFIGURATION.md).

## Before any upgrade

1. Back up the database (the SQLite file, or `pg_dump` for PostgreSQL).
2. Read [CHANGELOG.md](../CHANGELOG.md) for the release you are moving to.
3. Run the new version against a copy of the data first.
4. Check `GET /ready` after the start. It reports 503 if the database check fails.
