"""
src/agents/execution/pnl_calculator.py

WI-21 realized PnL calculator and settlement persistence.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import AppConfig
from src.core.exceptions import PnLCalculationError
from src.db.repositories.position_repository import PositionRepository
from src.schemas.execution import PnLRecord, PositionRecord

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")


def _coerce_persisted_decimal(value: object, *, fallback: Decimal) -> Decimal:
    if value is None:
        return fallback
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return fallback


class PnLCalculator:
    """Computes and persists WI-21 realized PnL for closed positions."""

    def __init__(
        self,
        config: AppConfig,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._config = config
        self._db_session_factory = db_session_factory

    async def settle(
        self,
        position: PositionRecord,
        exit_price: Decimal,
        gas_cost_usdc: Decimal | None = None,
        fees_usdc: Decimal | None = None,
    ) -> PnLRecord:
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
        normalized_gas_cost = (
            Decimal(str(gas_cost_usdc)) if gas_cost_usdc is not None else _ZERO
        )
        normalized_fees = Decimal(str(fees_usdc)) if fees_usdc is not None else _ZERO
        net_realized_pnl = realized_pnl - normalized_gas_cost - normalized_fees
        closed_at_utc = datetime.now(timezone.utc)

        pnl_record = PnLRecord(
            position_id=str(position.id),
            condition_id=str(position.condition_id),
            entry_price=entry_price,
            exit_price=exit_price_decimal,
            order_size_usdc=order_size_usdc,
            position_size_tokens=position_size_tokens,
            realized_pnl=realized_pnl,
            gas_cost_usdc=normalized_gas_cost,
            fees_usdc=normalized_fees,
            net_realized_pnl=net_realized_pnl,
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
            gas_cost_usdc=str(pnl_record.gas_cost_usdc),
            fees_usdc=str(pnl_record.fees_usdc),
            net_realized_pnl=str(pnl_record.net_realized_pnl),
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
                    gas_cost_usdc=pnl_record.gas_cost_usdc,
                    fees_usdc=pnl_record.fees_usdc,
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
                refresh_result = session.refresh(settled)
                if inspect.isawaitable(refresh_result):
                    await refresh_result
                persisted_realized_pnl = _coerce_persisted_decimal(
                    getattr(settled, "realized_pnl", None),
                    fallback=pnl_record.realized_pnl,
                )
                persisted_gas_cost = _coerce_persisted_decimal(
                    getattr(settled, "gas_cost_usdc", None),
                    fallback=pnl_record.gas_cost_usdc,
                )
                persisted_fees = _coerce_persisted_decimal(
                    getattr(settled, "fees_usdc", None),
                    fallback=pnl_record.fees_usdc,
                )
                pnl_record = pnl_record.model_copy(
                    update={
                        "realized_pnl": persisted_realized_pnl,
                        "gas_cost_usdc": persisted_gas_cost,
                        "fees_usdc": persisted_fees,
                        "net_realized_pnl": (
                            persisted_realized_pnl - persisted_gas_cost - persisted_fees
                        ),
                    }
                )
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
