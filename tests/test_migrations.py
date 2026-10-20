"""Alembic migrations against the models.

Each test runs ``alembic upgrade head`` through the real ``alembic/env.py``
against a throwaway database:

* SQLite always (a temporary file);
* PostgreSQL too when ``GATEWAY_TEST_POSTGRES_URL`` is set to an
  ``postgresql+asyncpg://`` URL of an empty, disposable database. The tests
  drop every table in it.

The drift test is the guard for future changes: it fails when
``app/db_models.py`` has a table, column, index or foreign key that the
migrations do not create.
"""
import asyncio
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import Base
import app.db_models  # noqa: F401  (registers every model on Base)

REPO = Path(__file__).resolve().parent.parent
PG_URL = os.environ.get("GATEWAY_TEST_POSTGRES_URL")

BACKENDS = ["sqlite"] + (["postgresql"] if PG_URL else [])


def _alembic_config(url: str) -> Config:
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


def _run(coro):
    return asyncio.run(coro)


async def _with_connection(url, fn):
    engine = create_async_engine(url, poolclass=sa.pool.NullPool)
    try:
        async with engine.begin() as conn:
            return await conn.run_sync(fn)
    finally:
        await engine.dispose()


def _drop_everything(conn):
    md = sa.MetaData()
    md.reflect(conn)
    md.drop_all(conn)


def _create_all(conn):
    Base.metadata.create_all(conn)


def _diffs(conn):
    mc = MigrationContext.configure(conn, opts={"compare_type": True})
    out = []
    for diff in compare_metadata(mc, Base.metadata):
        if "alembic_version" in repr(diff):
            continue
        # SQLite has no IDENTITY: it never reflects one, so the comparator
        # reports every Identity() primary key as a changed default, even on
        # a create_all database. Not a real difference.
        if (conn.dialect.name == "sqlite" and isinstance(diff, list)
                and all(d[0] == "modify_default" and isinstance(d[-1], sa.Identity)
                        for d in diff)):
            continue
        out.append(diff)
    return out


def _snapshot(conn):
    """Tables -> (columns with type/nullability, indexes, foreign keys)."""
    insp = sa.inspect(conn)
    out = {}
    for table in insp.get_table_names():
        if table == "alembic_version":
            continue
        cols = tuple(
            (c["name"], type(c["type"]).__name__, c["nullable"])
            for c in insp.get_columns(table)
        )
        idx = tuple(sorted(
            (i["name"], tuple(i["column_names"]), bool(i["unique"]))
            for i in insp.get_indexes(table)
        ))
        fks = tuple(sorted(
            (tuple(f["constrained_columns"]), f["referred_table"],
             tuple(f["referred_columns"]), (f.get("options") or {}).get("ondelete"))
            for f in insp.get_foreign_keys(table)
        ))
        out[table] = (tuple(sorted(cols)), idx, fks)
    return out


def _current_revision(conn):
    return MigrationContext.configure(conn).get_current_revision()


@pytest.fixture(params=BACKENDS)
def db_url(request, tmp_path):
    if request.param == "sqlite":
        yield f"sqlite+aiosqlite:///{tmp_path / 'migrate.db'}"
        return
    _run(_with_connection(PG_URL, _drop_everything))
    yield PG_URL
    _run(_with_connection(PG_URL, _drop_everything))


def _head() -> str:
    return ScriptDirectory.from_config(_alembic_config("sqlite://")).get_current_head()


def test_single_head():
    script = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    assert len(script.get_heads()) == 1


def test_upgrade_on_empty_database_creates_full_schema(db_url):
    command.upgrade(_alembic_config(db_url), "head")

    tables = _run(_with_connection(db_url, lambda c: set(sa.inspect(c).get_table_names())))
    assert set(Base.metadata.tables) <= tables
    assert _run(_with_connection(db_url, _current_revision)) == _head()


def test_models_and_migrations_do_not_drift(db_url):
    """Fails when app/db_models.py has schema the migrations do not create.

    If this fails after a model change, add a new Alembic revision for it.
    """
    command.upgrade(_alembic_config(db_url), "head")
    diffs = _run(_with_connection(db_url, _diffs))
    assert diffs == [], f"models and migrations differ: {diffs}"


def test_upgrade_on_create_all_database_is_safe(db_url, tmp_path):
    # The gateway's own startup path: init_db() -> create_all.
    _run(_with_connection(db_url, _create_all))
    before = _run(_with_connection(db_url, _snapshot))

    command.upgrade(_alembic_config(db_url), "head")  # must not raise

    after = _run(_with_connection(db_url, _snapshot))
    assert after == before
    assert _run(_with_connection(db_url, _diffs)) == []
    assert _run(_with_connection(db_url, _current_revision)) == _head()

    # Running it again is a no-op.
    command.upgrade(_alembic_config(db_url), "head")
    assert _run(_with_connection(db_url, _snapshot)) == before


def test_create_all_schema_equals_migrated_schema(db_url, tmp_path):
    command.upgrade(_alembic_config(db_url), "head")
    migrated = _run(_with_connection(db_url, _snapshot))

    if db_url.startswith("sqlite"):
        other = f"sqlite+aiosqlite:///{tmp_path / 'create_all.db'}"
    else:
        _run(_with_connection(db_url, _drop_everything))
        other = db_url
    _run(_with_connection(other, _create_all))
    created = _run(_with_connection(other, _snapshot))

    assert migrated == created


def test_upgrade_adds_missing_tables_and_columns(db_url):
    """A database created by an older create_all: a table and a column missing."""
    def make_old_schema(conn):
        Base.metadata.create_all(conn)
        conn.execute(sa.text("DROP TABLE governance_ip_rules"))
        if conn.dialect.name == "sqlite":
            conn.execute(sa.text("DROP INDEX ix_api_keys_key"))
        conn.execute(sa.text("ALTER TABLE api_keys DROP COLUMN last_used_at"))
        conn.execute(sa.text("ALTER TABLE api_keys DROP COLUMN key"))

    _run(_with_connection(db_url, make_old_schema))
    command.upgrade(_alembic_config(db_url), "head")

    def check(conn):
        insp = sa.inspect(conn)
        cols = {c["name"] for c in insp.get_columns("api_keys")}
        idx = {i["name"] for i in insp.get_indexes("api_keys")}
        return ("governance_ip_rules" in insp.get_table_names(),
                {"last_used_at", "key"} <= cols, "ix_api_keys_key" in idx)

    assert _run(_with_connection(db_url, check)) == (True, True, True)


def test_downgrade_to_base_removes_tables(db_url):
    cfg = _alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    tables = _run(_with_connection(db_url, lambda c: set(sa.inspect(c).get_table_names())))
    assert tables & set(Base.metadata.tables) == set()


def test_offline_sql_covers_every_table():
    import contextlib
    import io

    buf = io.StringIO()
    cfg = _alembic_config("postgresql+asyncpg://u@localhost/db")
    cfg.output_buffer = buf
    with contextlib.redirect_stdout(buf):
        command.upgrade(cfg, "head", sql=True)
    sql = buf.getvalue()
    for name in Base.metadata.tables:
        assert f"CREATE TABLE {name} " in sql, name
