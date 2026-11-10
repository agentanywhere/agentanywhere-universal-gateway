"""Database configuration and session management."""
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from app.config import get_settings
import os

settings = get_settings()

# Determine database URL
database_url = settings.database_url or os.getenv("DATABASE_URL")
if not database_url:
    # Default to SQLite for development
    database_url = "sqlite+aiosqlite:///./gateway.db"

# Create async engine
engine = create_async_engine(
    database_url,
    echo=settings.debug,
    future=True,
)

def sqlite_pragmas(cfg) -> list:
    """The PRAGMA statements the SQLITE_* settings ask for (none by default)."""
    pragmas = []
    mode = (cfg.sqlite_journal_mode or "").upper()
    if mode in ("WAL", "DELETE", "TRUNCATE", "PERSIST", "MEMORY", "OFF"):
        pragmas.append(f"PRAGMA journal_mode={mode}")
    sync = (cfg.sqlite_synchronous or "").upper()
    if sync in ("OFF", "NORMAL", "FULL", "EXTRA"):
        pragmas.append(f"PRAGMA synchronous={sync}")
    if cfg.sqlite_busy_timeout_ms > 0:
        pragmas.append(f"PRAGMA busy_timeout={int(cfg.sqlite_busy_timeout_ms)}")
    return pragmas


def install_sqlite_pragmas(target_engine, pragmas) -> None:
    """Run ``pragmas`` on every new connection of ``target_engine``."""
    if not pragmas:
        return
    from sqlalchemy import event

    @event.listens_for(target_engine.sync_engine, "connect")
    def _apply(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        try:
            for statement in pragmas:
                cursor.execute(statement)
        finally:
            cursor.close()


if database_url.startswith("sqlite"):
    install_sqlite_pragmas(engine, sqlite_pragmas(settings))

# Create session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# Base class for models
Base = declarative_base()


async def get_db() -> AsyncSession:
    """Dependency to get database session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db():
    """Initialize database tables.

    Several worker processes start at once against one database and each runs this. They
    can all see a table as missing and then collide creating it, which used to stop the
    losing workers with "already exists" and leave the gateway running on fewer workers
    than were asked for. A worker that loses the race checks again, finds the tables, and
    carries on.
    """
    import asyncio

    for attempt in range(5):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            return
        except Exception as exc:
            if "already exists" not in str(exc).lower() or attempt == 4:
                raise
            await asyncio.sleep(0.2 * (attempt + 1))


async def close_db():
    """Close database connections."""
    await engine.dispose()

