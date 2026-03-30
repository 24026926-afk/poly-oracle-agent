"""
src/agents/execution/exit_strategy_engine.py

WI-19 exit strategy engine for evaluating lifecycle transitions of open positions.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agents.execution.polymarket_client import PolymarketClient
from src.core.config import AppConfig
from src.core.exceptions import ExitEvaluationError, ExitMutationError
from src.db.models import Position
from src.db.repositories.position_repository import PositionRepository
from src.schemas.execution import (
    ExecutionAction,
    ExitReason,
    ExitResult,
    ExitSignal,
    PositionRecord,
    PositionStatus,
)

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_HOUR_SECONDS = Decimal("3600")


class ExitStrategyEngine:
    """Async evaluator for WI-19 position lifecycle transitions."""

    def __init__(
        self,
        config: AppConfig,
        polymarket_client: PolymarketClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._config = config
        self._polymarket_client = polymarket_client
        self._db_session_factory = db_session_factory

    async def evaluate_position(self, signal: ExitSignal) -> ExitResult:
        """Evaluate one OPEN position against WI-19 exit criteria."""
        position = signal.position
        logger.info(
            "exit_engine.evaluating",
            position_id=position.id,
            condition_id=position.condition_id,
            entry_price=str(position.entry_price),
        )

        if position.status != PositionStatus.OPEN:
            logger.warning(
                "exit_engine.non_open_position",
                position_id=position.id,
                status=position.status.value,
            )
            return ExitResult(
                position_id=position.id,
                condition_id=position.condition_id,
                should_exit=False,
                exit_reason=ExitReason.ERROR,
                entry_price=position.entry_price,
                current_midpoint=signal.current_midpoint,
                current_best_bid=signal.current_best_bid,
                position_age_hours=_ZERO,
                unrealized_edge=_ZERO,
                evaluated_at_utc=signal.evaluated_at_utc,
            )

        position_age_hours = self._calculate_position_age_hours(
            position.routed_at_utc,
            signal.evaluated_at_utc,
        )
        unrealized_edge = signal.current_midpoint - position.entry_price

        stop_loss_triggered = unrealized_edge <= -self._to_decimal(
            self._config.exit_stop_loss_drop
        )
        time_decay_triggered = position_age_hours >= self._to_decimal(
            self._config.exit_position_max_age_hours
        )
        no_edge_triggered = unrealized_edge <= _ZERO
        take_profit_triggered = unrealized_edge >= self._to_decimal(
            self._config.exit_take_profit_gain
        )

        should_exit = (
            stop_loss_triggered
            or time_decay_triggered
            or no_edge_triggered
            or take_profit_triggered
        )
        exit_reason = self._resolve_exit_reason(
            stop_loss_triggered=stop_loss_triggered,
            time_decay_triggered=time_decay_triggered,
            no_edge_triggered=no_edge_triggered,
            take_profit_triggered=take_profit_triggered,
        )

        result = ExitResult(
            position_id=position.id,
            condition_id=position.condition_id,
            should_exit=should_exit,
            exit_reason=exit_reason,
            entry_price=position.entry_price,
            current_midpoint=signal.current_midpoint,
            current_best_bid=signal.current_best_bid,
            position_age_hours=position_age_hours,
            unrealized_edge=unrealized_edge,
            evaluated_at_utc=signal.evaluated_at_utc,
        )

        if result.should_exit:
            logger.info(
                "exit_engine.exit_triggered",
                position_id=result.position_id,
                exit_reason=result.exit_reason.value,
                unrealized_edge=str(result.unrealized_edge),
                position_age_hours=str(result.position_age_hours),
            )
        else:
            logger.info(
                "exit_engine.hold",
                position_id=result.position_id,
                unrealized_edge=str(result.unrealized_edge),
                position_age_hours=str(result.position_age_hours),
            )

        if self._config.dry_run:
            if result.should_exit:
                logger.info(
                    "exit_engine.dry_run_exit",
                    position_id=result.position_id,
                    condition_id=result.condition_id,
                    should_exit=result.should_exit,
                    exit_reason=result.exit_reason.value,
                    entry_price=str(result.entry_price),
                    current_midpoint=str(result.current_midpoint),
                    current_best_bid=str(result.current_best_bid),
                    position_age_hours=str(result.position_age_hours),
                    unrealized_edge=str(result.unrealized_edge),
                    evaluated_at_utc=result.evaluated_at_utc.isoformat(),
                )
            return result

        if not result.should_exit:
            return result

        await self._close_position(
            position_id=result.position_id,
            condition_id=result.condition_id,
            exit_reason=result.exit_reason,
        )
        logger.info(
            "exit_engine.position_closed",
            position_id=result.position_id,
            condition_id=result.condition_id,
            exit_reason=result.exit_reason.value,
        )
        return result

    async def scan_open_positions(self) -> list[ExitResult]:
        """Scan all OPEN positions and evaluate each against fresh market data."""
        try:
            async with self._db_session_factory() as session:
                repo = PositionRepository(session)
                open_positions = await repo.get_open_positions()
        except Exception as exc:
            raise ExitEvaluationError(
                reason="open_position_scan_failed",
                cause=exc,
            ) from exc

        results: list[ExitResult] = []
        errors = 0
        scan_time = datetime.now(timezone.utc)

        for position_row in open_positions:
            try:
                position_record = self._to_position_record(position_row)
                snapshot = await self._polymarket_client.fetch_order_book(
                    position_record.token_id
                )

                if snapshot is None:
                    logger.warning(
                        "exit_engine.stale_market",
                        position_id=position_record.id,
                        token_id=position_record.token_id,
                    )
                    stale_result = ExitResult(
                        position_id=position_record.id,
                        condition_id=position_record.condition_id,
                        should_exit=True,
                        exit_reason=ExitReason.STALE_MARKET,
                        entry_price=position_record.entry_price,
                        current_midpoint=_ZERO,
                        current_best_bid=_ZERO,
                        position_age_hours=self._calculate_position_age_hours(
                            position_record.routed_at_utc,
                            scan_time,
                        ),
                        unrealized_edge=_ZERO,
                        evaluated_at_utc=scan_time,
                    )
                    results.append(stale_result)

                    if not self._config.dry_run:
                        await self._close_position(
                            position_id=stale_result.position_id,
                            condition_id=stale_result.condition_id,
                            exit_reason=stale_result.exit_reason,
                        )
                        logger.info(
                            "exit_engine.position_closed",
                            position_id=stale_result.position_id,
                            condition_id=stale_result.condition_id,
                            exit_reason=stale_result.exit_reason.value,
                        )
                    continue

                signal = ExitSignal(
                    position=position_record,
                    current_midpoint=snapshot.midpoint_probability,
                    current_best_bid=snapshot.best_bid,
                    evaluated_at_utc=scan_time,
                )
                result = await self.evaluate_position(signal)
                results.append(result)
            except Exception as exc:
                errors += 1
                logger.error(
                    "exit_engine.evaluation_error",
                    position_id=getattr(position_row, "id", None),
                    condition_id=getattr(position_row, "condition_id", None),
                    error=str(exc),
                )
                results.append(
                    ExitResult(
                        position_id=str(getattr(position_row, "id", "unknown")),
                        condition_id=str(
                            getattr(position_row, "condition_id", "unknown")
                        ),
                        should_exit=False,
                        exit_reason=ExitReason.ERROR,
                        entry_price=self._to_decimal(
                            getattr(position_row, "entry_price", _ZERO)
                        ),
                        current_midpoint=_ZERO,
                        current_best_bid=_ZERO,
                        position_age_hours=_ZERO,
                        unrealized_edge=_ZERO,
                        evaluated_at_utc=scan_time,
                    )
                )

        exits = sum(1 for result in results if result.should_exit)
        holds = len(results) - exits
        logger.info(
            "exit_engine.scan_complete",
            total=len(results),
            exits=exits,
            holds=holds,
            errors=errors,
        )
        return results

    async def _close_position(
        self,
        *,
        position_id: str,
        condition_id: str,
        exit_reason: ExitReason,
    ) -> None:
        try:
            async with self._db_session_factory() as session:
                repo = PositionRepository(session)
                updated = await repo.update_status(
                    position_id=position_id,
                    new_status=PositionStatus.CLOSED.value,
                )
                if updated is None:
                    raise ExitMutationError(
                        reason="position_not_found_for_close",
                        position_id=position_id,
                        condition_id=condition_id,
                    )
                await session.commit()
        except ExitMutationError:
            logger.error(
                "exit_engine.mutation_failed",
                position_id=position_id,
                condition_id=condition_id,
                exit_reason=exit_reason.value,
                error="position_not_found_for_close",
            )
            raise
        except Exception as exc:
            logger.error(
                "exit_engine.mutation_failed",
                position_id=position_id,
                condition_id=condition_id,
                exit_reason=exit_reason.value,
                error=str(exc),
            )
            raise ExitMutationError(
                reason="position_close_failed",
                position_id=position_id,
                condition_id=condition_id,
                cause=exc,
            ) from exc

    @staticmethod
    def _resolve_exit_reason(
        *,
        stop_loss_triggered: bool,
        time_decay_triggered: bool,
        no_edge_triggered: bool,
        take_profit_triggered: bool,
    ) -> ExitReason:
        if stop_loss_triggered:
            return ExitReason.STOP_LOSS
        if time_decay_triggered:
            return ExitReason.TIME_DECAY
        if no_edge_triggered:
            return ExitReason.NO_EDGE
        if take_profit_triggered:
            return ExitReason.TAKE_PROFIT
        return ExitReason.NO_EDGE

    @staticmethod
    def _calculate_position_age_hours(
        routed_at_utc: datetime,
        evaluated_at_utc: datetime,
    ) -> Decimal:
        routed = ExitStrategyEngine._as_utc(routed_at_utc)
        evaluated = ExitStrategyEngine._as_utc(evaluated_at_utc)
        age_seconds = Decimal(
            str((evaluated - routed).total_seconds())
        )
        return age_seconds / _HOUR_SECONDS

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @classmethod
    def _to_position_record(cls, position: Position) -> PositionRecord:
        return PositionRecord(
            id=str(position.id),
            condition_id=str(position.condition_id),
            token_id=str(position.token_id),
            status=PositionStatus(str(position.status)),
            side=str(position.side),
            entry_price=cls._to_decimal(position.entry_price),
            order_size_usdc=cls._to_decimal(position.order_size_usdc),
            kelly_fraction=cls._to_decimal(position.kelly_fraction),
            best_ask_at_entry=cls._to_decimal(position.best_ask_at_entry),
            bankroll_usdc_at_entry=cls._to_decimal(position.bankroll_usdc_at_entry),
            execution_action=ExecutionAction(str(position.execution_action)),
            reason=position.reason,
            routed_at_utc=position.routed_at_utc,
            recorded_at_utc=position.recorded_at_utc,
        )

    @staticmethod
    def _to_decimal(value: object) -> Decimal:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
