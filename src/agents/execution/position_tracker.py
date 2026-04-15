"""
src/agents/execution/position_tracker.py

WI-17 position tracker for persisting execution outcomes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import AppConfig
from src.db.models import Position
from src.db.repositories.position_repo import PositionRepository
from src.schemas.execution import (
    ExecutionAction,
    ExecutionResult,
    PositionRecord,
    PositionStatus,
)

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")


class PositionTracker:
    """Converts ExecutionResult into a persisted PositionRecord."""

    def __init__(
        self,
        config: AppConfig,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._config = config
        self._db_session_factory = db_session_factory

    async def record_execution(
        self,
        result: ExecutionResult,
        condition_id: str,
        token_id: str,
    ) -> PositionRecord | None:
        """Persist an execution outcome as a position record."""
        if result.action == ExecutionAction.SKIP:
            return None

        if result.action == ExecutionAction.EXECUTED and self._config.dry_run:
            logger.error(
                "position_tracker.unreachable_executed_in_dry_run",
                condition_id=condition_id,
                token_id=token_id,
            )
            return None

        if result.action == ExecutionAction.DRY_RUN and not self._config.dry_run:
            logger.error(
                "position_tracker.unreachable_dry_run_in_live",
                condition_id=condition_id,
                token_id=token_id,
            )
            return None

        status = self._derive_status(result.action)
        if status is None:
            return None

        entry_price = (
            result.midpoint_probability
            if result.midpoint_probability is not None
            else _ZERO
        )
        order_size_usdc = (
            result.order_size_usdc if result.order_size_usdc is not None else _ZERO
        )
        kelly_fraction = (
            result.kelly_fraction if result.kelly_fraction is not None else _ZERO
        )
        best_ask = result.best_ask if result.best_ask is not None else _ZERO
        bankroll_usdc = (
            result.bankroll_usdc if result.bankroll_usdc is not None else _ZERO
        )

        record = PositionRecord(
            id=str(uuid.uuid4()),
            condition_id=str(condition_id),
            token_id=str(token_id),
            status=status,
            side="BUY",
            entry_price=entry_price,
            order_size_usdc=order_size_usdc,
            kelly_fraction=kelly_fraction,
            best_ask_at_entry=best_ask,
            bankroll_usdc_at_entry=bankroll_usdc,
            execution_action=result.action,
            reason=result.reason,
            routed_at_utc=result.routed_at_utc,
            recorded_at_utc=datetime.now(timezone.utc),
        )

        if self._config.dry_run:
            logger.info(
                "position_tracker.dry_run_record",
                condition_id=record.condition_id,
                token_id=record.token_id,
                status=record.status.value,
                entry_price=str(record.entry_price),
                order_size_usdc=str(record.order_size_usdc),
                kelly_fraction=str(record.kelly_fraction),
                execution_action=record.execution_action.value,
                reason=record.reason,
            )
            return record

        if not callable(self._db_session_factory):
            logger.error(
                "position_tracker.session_factory_unavailable",
                condition_id=record.condition_id,
                token_id=record.token_id,
            )
            return record

        async with self._db_session_factory() as session:
            repo = PositionRepository(session)
            position_orm = Position(
                id=record.id,
                condition_id=record.condition_id,
                token_id=record.token_id,
                status=record.status.value,
                side=record.side,
                entry_price=record.entry_price,
                order_size_usdc=record.order_size_usdc,
                kelly_fraction=record.kelly_fraction,
                best_ask_at_entry=record.best_ask_at_entry,
                bankroll_usdc_at_entry=record.bankroll_usdc_at_entry,
                execution_action=record.execution_action.value,
                reason=record.reason,
                routed_at_utc=record.routed_at_utc,
                recorded_at_utc=record.recorded_at_utc,
            )
            await repo.insert_position(position_orm)
            await session.commit()

        logger.info(
            "position_tracker.position_recorded",
            position_id=record.id,
            condition_id=record.condition_id,
            token_id=record.token_id,
            status=record.status.value,
        )
        return record

    @staticmethod
    def _derive_status(action: ExecutionAction) -> PositionStatus | None:
        if action in (ExecutionAction.EXECUTED, ExecutionAction.DRY_RUN):
            return PositionStatus.OPEN
        if action == ExecutionAction.FAILED:
            return PositionStatus.FAILED
        return None
