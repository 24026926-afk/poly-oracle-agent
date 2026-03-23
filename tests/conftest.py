"""
tests/conftest.py

Shared async test fixtures for poly-oracle-agent.
Provides an in-memory SQLite database with per-test rollback isolation.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.db.models import Base


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture()
async def async_engine():
    """Create an in-memory async SQLite engine and provision all tables."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        echo=False,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture()
async def async_session(async_engine):
    """Yield an AsyncSession that rolls back after each test."""
    session_factory = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as session:
        async with session.begin():
            yield session
            # Rollback on exit to keep tests isolated
            await session.rollback()
