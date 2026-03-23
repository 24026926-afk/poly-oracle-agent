"""
src/db/repositories/decision_repo.py

Async repository for AgentDecisionLog persistence.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from src.db.models import AgentDecisionLog, MarketSnapshot

logger = structlog.get_logger(__name__)


class DecisionRepository:
    """Encapsulates all AgentDecisionLog database operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_decision(
        self, decision: AgentDecisionLog
    ) -> AgentDecisionLog:
        """Persist a new decision log and return it with a populated PK."""
        self._session.add(decision)
        await self._session.flush()
        logger.debug(
            "decision_inserted",
            decision_id=decision.id,
            action=decision.recommended_action.value,
        )
        return decision

    async def get_recent_by_market(
        self, condition_id: str, limit: int = 10
    ) -> list[AgentDecisionLog]:
        """Return the most recent decisions for a market, newest first.

        Joins through MarketSnapshot to filter by condition_id.
        """
        stmt = (
            select(AgentDecisionLog)
            .join(
                MarketSnapshot,
                AgentDecisionLog.snapshot_id == MarketSnapshot.id,
            )
            .where(MarketSnapshot.condition_id == condition_id)
            .order_by(AgentDecisionLog.evaluated_at.desc())
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
