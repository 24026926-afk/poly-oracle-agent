"""
src/db/repositories/position_repository.py

Async repository for Position persistence.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from src.db.models import Position

logger = structlog.get_logger(__name__)


class PositionRepository:
    """Encapsulates all Position database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_position(self, position: Position) -> Position:
        """Persist a new position record and return the flushed ORM row."""
        self._session.add(position)
        await self._session.flush()
        logger.debug(
            "position.inserted",
            position_id=position.id,
            condition_id=position.condition_id,
            status=position.status,
        )
        return position

    async def get_by_id(self, position_id: str) -> Position | None:
        """Return a position by primary key, or None."""
        stmt = select(Position).where(Position.id == position_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_open_by_condition_id(self, condition_id: str) -> list[Position]:
        """Return all OPEN positions for one condition."""
        stmt = select(Position).where(
            Position.condition_id == condition_id,
            Position.status == "OPEN",
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_open_positions(self) -> list[Position]:
        """Return all OPEN positions across all markets."""
        stmt = select(Position).where(Position.status == "OPEN")
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_all_positions(self) -> list[Position]:
        """Return all positions regardless of status."""
        stmt = select(Position)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_settled_positions(self) -> list[Position]:
        """Return CLOSED positions with non-null realized_pnl."""
        stmt = select(Position).where(
            Position.status == "CLOSED",
            Position.realized_pnl.isnot(None),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_positions_by_status(self, status: str) -> list[Position]:
        """Return all positions matching one status string."""
        stmt = select(Position).where(Position.status == status)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def update_status(
        self,
        position_id: str,
        *,
        new_status: str,
    ) -> Position | None:
        """Transition a position to a new status and flush."""
        position = await self.get_by_id(position_id)
        if position is None:
            return None

        position.status = str(new_status)
        await self._session.flush()
        logger.debug(
            "position.updated",
            position_id=position.id,
            new_status=position.status,
        )
        return position

    async def record_settlement(
        self,
        *,
        position_id: str,
        realized_pnl: Decimal,
        exit_price: Decimal,
        closed_at_utc: datetime,
    ) -> Position | None:
        """Persist WI-21 settlement columns on an existing position row."""
        position = await self.get_by_id(position_id)
        if position is None:
            return None

        if position.realized_pnl is not None:
            logger.warning(
                "position.settlement_already_recorded",
                position_id=position.id,
                condition_id=position.condition_id,
                existing_realized_pnl=str(position.realized_pnl),
            )
            return position

        position.realized_pnl = realized_pnl
        position.exit_price = exit_price
        position.closed_at_utc = closed_at_utc
        await self._session.flush()
        logger.debug(
            "position.settlement_recorded",
            position_id=position.id,
            condition_id=position.condition_id,
            realized_pnl=str(position.realized_pnl),
            exit_price=str(position.exit_price),
            closed_at_utc=(
                position.closed_at_utc.isoformat()
                if position.closed_at_utc is not None
                else None
            ),
        )
        return position
