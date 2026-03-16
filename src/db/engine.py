"""
src/db/engine.py

Async SQLAlchemy engine configuration and session management.
"""

from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import AppConfig

# Load application configuration
config = AppConfig()

# Create the asynchronous engine
# echo=False prevents SQL query logging in production; enable for debugging.
engine = create_async_engine(
    config.database_url,
    echo=False,
    future=True,
)

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
