# Upgrading and the database schema

> Reference material. The Alembic commands on this page are exercised by
> `tests/test_migrations.py` against SQLite on every test run, and were run
> against PostgreSQL 16 for this release. Still try an upgrade on a copy of
> your data first.

This page explains how the gateway's database tables are created, what that
means when you move to a newer release, and how Alembic fits in. If you only
need a fresh install, you do not have to do anything: start the gateway and it
creates its tables.

## What happens at startup

When the gateway starts, `app/main.py` calls `init_db()` (in `app/database.py`),
which runs SQLAlchemy's `Base.metadata.create_all`. That creates every table
the current code defines **if the table does not exist**. It never adds a column
to a table that already exists, never changes a column type and never drops
anything.

So on a fresh database you get the full current schema. On an existing database,
tables that earlier releases already created stay as they were, and only tables
that are new in this release are added.

## Alembic

The `alembic/` directory has one migration, a baseline:

| Revision | Creates |
|---|---|
| `0001` | Every table in `app/db_models.py`: `api_keys`, `routes`, `agent_registry`, `tenants`, `request_logs`, `settings_profiles`, `tenant_api_keys`, `tenant_usage`, `governance_audit_log`, `admin_roles`, `governance_policies`, `governance_ip_rules`, `openapi_registrations`, with the same columns, indexes and foreign keys that `create_all` makes. |

How it behaves:

- **On an empty database** it creates the full schema.
- **On a database the gateway already created** (the gateway has started
  against it at least once, so `create_all` ran), it does not fail. Tables that
  exist are left in place; only tables, columns and indexes that are missing
  are added, and the database is then recorded as being at `0001`. A missing
  `NOT NULL` column is added as nullable, because the rows already in the table
  have no value for it, and Alembic logs a warning naming the column. Running
  `alembic upgrade head` a second time changes nothing.
- **Nothing is dropped or altered on upgrade.** `alembic downgrade base` drops
  every table, data included.
- **It works on SQLite and PostgreSQL.** There are no database-specific
  defaults; timestamps are filled in by the gateway, as with `create_all`.
- **Which database:** `alembic/env.py` uses the gateway's own database URL
  (`DATABASE_URL` from the environment or `.env`, default
  `sqlite+aiosqlite:///./gateway.db`) unchanged, through the same async driver
  the gateway uses (`asyncpg` or `aiosqlite`). No synchronous PostgreSQL
  driver is needed, so none was added to `requirements.txt`. To target another
  database for one command, use `alembic -x db_url=<url> upgrade head`; note
  that a password on the command line ends up in your shell history, so
  prefer setting `DATABASE_URL` in the environment.

`tests/test_migrations.py` runs the migration on an empty database and on a
`create_all` database and checks that the result matches the models exactly. It
fails when a model gains a table, column, index or foreign key that no
migration creates, so a schema change cannot ship without a migration. Set
`GATEWAY_TEST_POSTGRES_URL` to a `postgresql+asyncpg://` URL of an empty,
disposable database to run the same tests against PostgreSQL (they drop every
table in it).

### Earlier migrations `001` and `002`

Releases up to this one shipped two migrations, `001` and `002`, that described
an older schema: they did not create seven of the current tables, and their
`api_keys` and `request_logs` columns did not match the models. They have been
removed and replaced by `0001`. We assume no deployment ran them, because a
database built by them did not work with the gateway: for example its
`api_keys` table lacked the `key`, `allowed_paths`, `expires_at` and
`last_used_at` columns the gateway writes, so creating an API key failed.

If you did run them, `alembic upgrade head` stops with "Can't locate revision
identified by '002'". Do not just re-stamp that database: its `api_keys` and
`request_logs` tables have the wrong shape. Back it up, create a new empty
database, run `alembic upgrade head` (or start the gateway) against it, and
copy your rows across.

## What to run

**New install, any database:** either start the gateway, or run
`alembic upgrade head` first. Both give the same schema.

**Existing database created by the gateway:** run `alembic upgrade head` once.
It adds anything missing and records revision `0001`. From then on, new releases
that change the schema ship a new revision, and `alembic upgrade head` applies
it.

```bash
# from the repository root, with the same DATABASE_URL the gateway uses
alembic upgrade head
alembic current          # shows 0001 (head)
```

`scripts/setup_production.sh` runs `alembic upgrade head` when
`USE_POSTGRES=true`; that is safe on a new database and on one the gateway has
already created.

**Upgrading to a release that only adds tables:** starting the new version is
enough (`create_all` adds them); `alembic upgrade head` also works.

**Upgrading to a release that changes an existing table:** run
`alembic upgrade head` before you start the new version. `create_all` cannot
alter a table that exists.

## SQLite and more than one process

The default database is a SQLite file, `./gateway.db`, relative to the working
directory.

- SQLite is a single file on one host. It is fine for one gateway process. If
  you run several worker processes (`uvicorn --workers N`) or several replicas,
  use PostgreSQL. Replicas cannot share a SQLite file across hosts, and writes
  from several processes to one file are serialised and can fail with "database
  is locked".
- With the repository's `docker-compose.yml`, the SQLite file is
  `/app/data/gateway.db` on the named volume `gateway_data`, so it survives the
  container being recreated. Earlier versions of the Compose file kept it at
  `/app/gateway.db` inside the container. If you are moving from one of those
  and the old container still exists, copy the file out before you recreate it
  (`docker compose cp gateway:/app/gateway.db ./gateway.db`), then, after the
  new container is up, copy it in (`docker compose cp ./gateway.db
  gateway:/app/data/gateway.db`), give it to the container's user
(`docker compose exec -u root gateway chown gateway:gateway /app/data/gateway.db`)
and restart the gateway. Also delete the
  `DATABASE_URL=sqlite+aiosqlite:///./gateway.db` line from a `.env` made from
  an older `env.example`; it would override the volume path. Backups: see
  [DEPLOYMENT.md](DEPLOYMENT.md#backing-up-the-database-volume).
- Other state is also per process: with no `RATE_LIMIT_REDIS_URL`, rate-limit
  counters live in each process's memory. See [CONFIGURATION.md](CONFIGURATION.md).

## Before any upgrade

1. Back up the database (the SQLite file, or `pg_dump` for PostgreSQL).
2. Read [CHANGELOG.md](../CHANGELOG.md) for the release you are moving to.
3. Run the new version against a copy of the data first.
4. Check `GET /ready` after the start. It reports 503 if the database check fails.
