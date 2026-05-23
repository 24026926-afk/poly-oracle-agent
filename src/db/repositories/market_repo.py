"""
src/db/repositories/market_repo.py

Async repository for MarketSnapshot persistence.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from src.db.models import MarketSnapshot

logger = structlog.get_logger(__name__)


class MarketRepository:
    """Encapsulates all MarketSnapshot database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_snapshot(self, snapshot: MarketSnapshot) -> MarketSnapshot:
        """Persist a new market snapshot and return it with a populated PK."""
        self._session.add(snapshot)
        await self._session.flush()
        logger.debug(
            "market_snapshot_inserted",
            snapshot_id=snapshot.id,
            condition_id=snapshot.condition_id,
        )
        return snapshot

    async def get_latest_by_condition_id(
        self, condition_id: str
    ) -> MarketSnapshot | None:
        """Return the most recent snapshot for a given condition_id, or None."""
        stmt = (
            select(MarketSnapshot)
            .where(MarketSnapshot.condition_id == condition_id)
            .order_by(MarketSnapshot.captured_at.desc())
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_recent_snapshots(
        self, cutoff: datetime, limit: int = 100
    ) -> list[MarketSnapshot]:
        """Return recent snapshots since cutoff, newest first, bounded by limit."""
        stmt = (
            select(MarketSnapshot)
            .where(MarketSnapshot.captured_at >= cutoff)
            .order_by(MarketSnapshot.captured_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
