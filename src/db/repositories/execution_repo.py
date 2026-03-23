"""
src/db/repositories/execution_repo.py

Async repository for ExecutionTx persistence.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from src.db.models import ExecutionTx, TxStatus

logger = structlog.get_logger(__name__)


class ExecutionRepository:
    """Encapsulates all ExecutionTx database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_execution(self, execution: ExecutionTx) -> ExecutionTx:
        """Persist a new execution record and return it with a populated PK."""
        self._session.add(execution)
        await self._session.flush()
        logger.debug(
            "execution_inserted",
            execution_id=execution.id,
            decision_id=execution.decision_id,
            status=execution.status.value,
        )
        return execution

    async def get_by_decision_id(
        self, decision_id: str
    ) -> ExecutionTx | None:
        """Return the execution record linked to a decision, or None."""
        stmt = select(ExecutionTx).where(
            ExecutionTx.decision_id == decision_id
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_aggregate_exposure(self, condition_id: str) -> Decimal:
        """Sum size_usdc for PENDING + CONFIRMED executions on a market.

        Returns Decimal('0') when no matching rows exist.
        The float result from SQLite/Postgres is cast through str() to
        avoid float-to-Decimal precision contamination.
        """
        stmt = (
            select(func.sum(ExecutionTx.size_usdc))
            .where(ExecutionTx.condition_id == condition_id)
            .where(
                ExecutionTx.status.in_(
                    [TxStatus.PENDING, TxStatus.CONFIRMED]
                )
            )
        )
        result = await self._session.execute(stmt)
        raw = result.scalar_one_or_none()

        if raw is None:
            return Decimal("0")

        # Cast via str to avoid float → Decimal precision loss
        return Decimal(str(raw))
