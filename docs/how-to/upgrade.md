# Upgrade the gateway

**Goal:** move an existing installation to a newer release without losing data,
and know what the version numbers promise.

**Before you start:** read the [CHANGELOG](../../CHANGELOG.md) entry for the
release you are moving to, and have a [backup](back-up-and-restore.md).

## Contents

- [Versioning](#versioning)
- [How the schema is managed](#how-the-schema-is-managed)
- [Steps](#steps)
- [When a release changes a table](#when-a-release-changes-a-table)
- [Alembic](#alembic)
- [If it did not work](#if-it-did-not-work)

## Versioning

- The project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
  and records changes in `CHANGELOG.md` in the
  [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
- The current version is `1.0.0`, marked **Unreleased** in the changelog. No
  release tag has been published yet.
- [SECURITY.md](../../SECURITY.md) lists which versions receive security fixes.
- The version a running gateway reports is `APP_VERSION` (default `1.0.0`),
  shown by `/health`. It is a setting, so it says what the configuration
  claims, not what code is running; check the git tag or the image tag to be
  sure.
- Release tags (`v*`) trigger two workflows: `docker-publish.yml` pushes an
  image to Docker Hub, and `pypi-publish.yml` publishes the package. Neither
  has run yet.

## How the schema is managed

When the gateway starts, `init_db()` runs SQLAlchemy's `create_all`. That
creates every table the code defines **if it does not exist**. It never adds a
column to an existing table, changes a type or drops anything.

So:

- a release that only **adds tables** needs nothing from you;
- a release that **changes an existing table** needs SQL from you (below);
- the Alembic migrations in the repository do not help (below).

## Steps

1. Back up the database and `.env` ([Back up and restore](back-up-and-restore.md)).
2. Read the changelog for schema changes and changed settings.
3. Run the new version against a copy of the data first.
4. Install the new version:

   ```bash
   git fetch --tags
   git checkout <new tag or commit>
   pip install -r requirements.txt
   ```

   or, with Docker Compose, `docker compose up -d --build` (with SQLite inside
   the container, copy the file out first: this recreates the container).
5. Start the gateway.

**Check:**

```bash
curl -s http://localhost:8000/health    # "version" is the expected APP_VERSION
curl -s http://localhost:8000/ready     # "status": "ready"
```

and make one request with an existing API key.

## When a release changes a table

`create_all` will not apply it. Apply the change yourself with SQL, for
example:

```sql
ALTER TABLE api_keys ADD COLUMN last_used_at TIMESTAMP;
```

Compare the table with `app/db_models.py` in the new release, or create a
fresh database with the new release and compare the two schemas:

```bash
sqlite3 gateway.db .schema > old.sql
sqlite3 fresh.db .schema > new.sql
diff old.sql new.sql
```

For PostgreSQL, use `pg_dump --schema-only` on both.

## Alembic

`alembic/` has two migrations, `001` and `002`, that describe an older schema.
For this page they were run with `python -m alembic upgrade head` against
scratch SQLite files:

- on a database the gateway had already created, it stopped with
  `table api_keys already exists`;
- on an empty database it completed, and the gateway, started on that
  database, then failed to create an API key with
  `table api_keys has no column named key`.

They have also not been checked against PostgreSQL, and `alembic/env.py` turns
`postgresql+asyncpg://` into `postgresql://`, which needs a synchronous driver
(such as `psycopg2-binary`) that is not in `requirements.txt`.

What to do:

- Do not run `alembic upgrade head`, and do not let
  `scripts/setup_production.sh` run it (it does when `USE_POSTGRES=true`).
- If your tooling needs Alembic to consider the database current,
  `alembic stamp head` records the revision without touching tables.
- Fixing this needs new, tested migrations, which is a code change.

## If it did not work

| Symptom | Cause | Fix |
|---|---|---|
| `no such column` or `has no column named` errors after the upgrade | The release changed a table that already existed | Add the column with SQL, as above. |
| `table ... already exists` from Alembic | The gateway created the tables | Use `alembic stamp head`, or do not use Alembic. |
| API keys gone after a Docker upgrade | SQLite inside the recreated container | Restore your copy; move to PostgreSQL. |
| `pip install` fails | Python 3.13, or a missing build tool | Use Python 3.10 to 3.12. |
