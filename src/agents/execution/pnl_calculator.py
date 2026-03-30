"""
src/agents/execution/pnl_calculator.py

WI-21 realized PnL calculator and settlement persistence.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import AppConfig
from src.core.exceptions import PnLCalculationError
from src.db.repositories.position_repository import PositionRepository
from src.schemas.execution import PnLRecord, PositionRecord

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")


class PnLCalculator:
    """Computes and persists WI-21 realized PnL for closed positions."""

    def __init__(
        self,
        config: AppConfig,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._config = config
        self._db_session_factory = db_session_factory

    async def settle(self, position: PositionRecord, exit_price: Decimal) -> PnLRecord:
        """Compute and persist realized PnL settlement for a closed position."""
        entry_price = Decimal(str(position.entry_price))
        order_size_usdc = Decimal(str(position.order_size_usdc))
        exit_price_decimal = Decimal(str(exit_price))

        if entry_price == _ZERO:
            position_size_tokens = _ZERO
            logger.warning(
                "pnl.degenerate_entry_price",
                position_id=position.id,
                condition_id=position.condition_id,
                entry_price=str(entry_price),
            )
        else:
            position_size_tokens = order_size_usdc / entry_price

        realized_pnl = (exit_price_decimal - entry_price) * position_size_tokens
        closed_at_utc = datetime.now(timezone.utc)

        pnl_record = PnLRecord(
            position_id=str(position.id),
            condition_id=str(position.condition_id),
            entry_price=entry_price,
            exit_price=exit_price_decimal,
            order_size_usdc=order_size_usdc,
            position_size_tokens=position_size_tokens,
            realized_pnl=realized_pnl,
            closed_at_utc=closed_at_utc,
        )

        logger.info(
            "pnl.calculated",
            position_id=pnl_record.position_id,
            condition_id=pnl_record.condition_id,
            entry_price=str(pnl_record.entry_price),
            exit_price=str(pnl_record.exit_price),
            order_size_usdc=str(pnl_record.order_size_usdc),
            position_size_tokens=str(pnl_record.position_size_tokens),
            realized_pnl=str(pnl_record.realized_pnl),
            closed_at_utc=pnl_record.closed_at_utc.isoformat(),
        )

        if self._config.dry_run:
            logger.info(
                "pnl.dry_run_settlement",
                position_id=pnl_record.position_id,
                condition_id=pnl_record.condition_id,
                realized_pnl=str(pnl_record.realized_pnl),
                exit_price=str(pnl_record.exit_price),
            )
            return pnl_record

        try:
            async with self._db_session_factory() as session:
                repo = PositionRepository(session)
                settled = await repo.record_settlement(
                    position_id=pnl_record.position_id,
                    realized_pnl=pnl_record.realized_pnl,
                    exit_price=pnl_record.exit_price,
                    closed_at_utc=pnl_record.closed_at_utc,
                )
                if settled is None:
                    logger.error(
                        "pnl.position_not_found",
                        position_id=pnl_record.position_id,
                        condition_id=pnl_record.condition_id,
                    )
                    raise PnLCalculationError(
                        reason="position_not_found_for_settlement",
                        position_id=pnl_record.position_id,
                        condition_id=pnl_record.condition_id,
                    )
                await session.commit()
        except PnLCalculationError:
            raise
        except Exception as exc:
            logger.error(
                "pnl.persistence_failed",
                position_id=pnl_record.position_id,
                condition_id=pnl_record.condition_id,
                error=str(exc),
            )
            raise PnLCalculationError(
                reason="settlement_persistence_failed",
                position_id=pnl_record.position_id,
                condition_id=pnl_record.condition_id,
                cause=exc,
            ) from exc

        logger.info(
            "pnl.persisted",
            position_id=pnl_record.position_id,
            condition_id=pnl_record.condition_id,
            realized_pnl=str(pnl_record.realized_pnl),
            exit_price=str(pnl_record.exit_price),
            closed_at_utc=pnl_record.closed_at_utc.isoformat(),
        )
        return pnl_record
