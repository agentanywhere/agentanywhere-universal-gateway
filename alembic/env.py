"""Alembic environment configuration.

Which database is migrated, in order of precedence:

1. ``alembic -x db_url=<url> ...`` on the command line;
2. ``sqlalchemy.url`` when a caller sets it (alembic.ini leaves it empty);
3. the gateway's own database URL, ``app.database.database_url``: that is
   ``DATABASE_URL`` from the environment or ``.env``, or the default
   ``sqlite+aiosqlite:///./gateway.db``.

The URL is used exactly as the gateway uses it. Async URLs
(``postgresql+asyncpg://``, ``sqlite+aiosqlite://``) run through an async
engine, so no extra synchronous driver is needed; a synchronous URL
(``postgresql://`` with psycopg2 installed, ``sqlite://``) also works.
"""
import asyncio
import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

# Make the repository importable when alembic is run from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db_models import Base  # noqa: E402  (registers every model on Base)

config = context.config

# Tests set configure_logger=False so running a migration does not reset the
# process-wide logging configuration.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("db_url")
    if not url:
        url = config.get_main_option("sqlalchemy.url")
    if not url:
        from app.database import database_url

        url = database_url
    return url


def _is_async(url: str) -> bool:
    return bool(getattr(make_url(url).get_dialect(), "is_async", False))


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        compare_type=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``)."""
    _configure(
        url=_database_url(),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection: Connection) -> None:
    _configure(
        connection=connection,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async(url: str) -> None:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_with_connection)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    url = _database_url()
    if _is_async(url):
        asyncio.run(_run_async(url))
        return
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        with engine.connect() as connection:
            _run_with_connection(connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
