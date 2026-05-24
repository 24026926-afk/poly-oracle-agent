"""
src/db/engine.py

Async SQLAlchemy engine configuration and session management.
"""

from typing import AsyncGenerator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import get_config

# Load application configuration
config = get_config()

# Create the asynchronous engine
# echo=False prevents SQL query logging in production; enable for debugging.
engine = create_async_engine(
    config.database_url,
    echo=False,
    future=True,
)


def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Reduce reader/writer contention for the local SQLite audit store."""
    if not config.database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)

# Create a session factory bound to the async engine
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency generator for injecting database sessions.
    Yields an AsyncSession and ensures it's closed after use.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
