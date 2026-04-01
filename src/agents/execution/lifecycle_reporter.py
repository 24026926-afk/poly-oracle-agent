"""
src/agents/execution/lifecycle_reporter.py

WI-24 read-only position lifecycle reporting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import AppConfig
from src.db.models import Position
from src.db.repositories.position_repository import PositionRepository
from src.schemas.risk import LifecycleReport, PositionLifecycleEntry

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_SECONDS_PER_HOUR = Decimal("3600")


class PositionLifecycleReporter:
    """Generate read-only lifecycle performance reports over positions."""

    def __init__(
        self,
        config: AppConfig,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._config = config
        self._db_session_factory = db_session_factory

    async def generate_report(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> LifecycleReport:
        """Compute a typed lifecycle report over all matching positions."""
        report_at_utc = datetime.now(timezone.utc)

        async with self._db_session_factory() as session:
            repo = PositionRepository(session)
            all_positions = await repo.get_all_positions()

        if start_date is not None and end_date is not None and start_date > end_date:
            logger.warning(
                "lifecycle.invalid_date_range",
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
        else:
            if start_date is not None:
                all_positions = [
                    position
                    for position in all_positions
                    if position.routed_at_utc >= start_date
                ]
            if end_date is not None:
                all_positions = [
                    position
                    for position in all_positions
                    if position.routed_at_utc <= end_date
                ]

        if not all_positions:
            logger.info("lifecycle.report_empty", dry_run=self._config.dry_run)
            return LifecycleReport(
                report_at_utc=report_at_utc,
                total_settled_count=0,
                winning_count=0,
                losing_count=0,
                breakeven_count=0,
                total_realized_pnl=_ZERO,
                avg_hold_duration_hours=_ZERO,
                best_pnl=_ZERO,
                worst_pnl=_ZERO,
                entries=[],
                dry_run=self._config.dry_run,
            )

        settled_positions = [
            position
            for position in all_positions
            if position.status == "CLOSED" and position.realized_pnl is not None
        ]

        lifecycle_entries = self._build_lifecycle_entries(all_positions)
        (
            total_settled_count,
            winning_count,
            losing_count,
            breakeven_count,
            total_realized_pnl,
            avg_hold_duration_hours,
            best_pnl,
            worst_pnl,
        ) = self._compute_aggregate_statistics(settled_positions)

        report = LifecycleReport(
            report_at_utc=report_at_utc,
            total_settled_count=total_settled_count,
            winning_count=winning_count,
            losing_count=losing_count,
            breakeven_count=breakeven_count,
            total_realized_pnl=total_realized_pnl,
            avg_hold_duration_hours=avg_hold_duration_hours,
            best_pnl=best_pnl,
            worst_pnl=worst_pnl,
            entries=lifecycle_entries,
            dry_run=self._config.dry_run,
        )
        logger.info(
            "lifecycle.report_generated",
            total_settled_count=report.total_settled_count,
            winning_count=report.winning_count,
            losing_count=report.losing_count,
            breakeven_count=report.breakeven_count,
            total_realized_pnl=str(report.total_realized_pnl),
            avg_hold_duration_hours=str(report.avg_hold_duration_hours),
            best_pnl=str(report.best_pnl),
            worst_pnl=str(report.worst_pnl),
            entry_count=len(report.entries),
            dry_run=report.dry_run,
        )
        return report

    @staticmethod
    def _build_lifecycle_entries(
        positions: list[Position],
    ) -> list[PositionLifecycleEntry]:
        entries: list[PositionLifecycleEntry] = []
        for position in positions:
            entry_price_d = Decimal(str(position.entry_price))
            order_size_usdc_d = Decimal(str(position.order_size_usdc))
            if entry_price_d == _ZERO:
                size_tokens = _ZERO
            else:
                size_tokens = order_size_usdc_d / entry_price_d

            entries.append(
                PositionLifecycleEntry(
                    position_id=str(position.id),
                    slug=position.condition_id,
                    entry_price=entry_price_d,
                    exit_price=(
                        Decimal(str(position.exit_price))
                        if position.exit_price is not None
                        else None
                    ),
                    size_tokens=size_tokens,
                    realized_pnl=(
                        Decimal(str(position.realized_pnl))
                        if position.realized_pnl is not None
                        else None
                    ),
                    status=str(position.status),
                    opened_at_utc=position.routed_at_utc,
                    settled_at_utc=position.closed_at_utc,
                )
            )
        return entries

    @staticmethod
    def _compute_aggregate_statistics(
        settled_positions: list[Position],
    ) -> tuple[int, int, int, int, Decimal, Decimal, Decimal, Decimal]:
        if not settled_positions:
            return (0, 0, 0, 0, _ZERO, _ZERO, _ZERO, _ZERO)

        total_settled_count = len(settled_positions)
        winning_count = 0
        losing_count = 0
        breakeven_count = 0
        total_hold_seconds = _ZERO
        hold_duration_count = 0
        pnl_values: list[Decimal] = []

        for position in settled_positions:
            pnl_value = Decimal(str(position.realized_pnl))
            pnl_values.append(pnl_value)

            if pnl_value > _ZERO:
                winning_count += 1
            elif pnl_value < _ZERO:
                losing_count += 1
            else:
                breakeven_count += 1

            if position.closed_at_utc is not None and position.routed_at_utc is not None:
                hold_seconds = Decimal(
                    str((position.closed_at_utc - position.routed_at_utc).total_seconds())
                )
                total_hold_seconds += hold_seconds
                hold_duration_count += 1

        total_realized_pnl = sum(pnl_values, _ZERO)
        best_pnl = max(pnl_values)
        worst_pnl = min(pnl_values)
        if hold_duration_count == 0:
            avg_hold_duration_hours = _ZERO
        else:
            avg_hold_duration_hours = (
                total_hold_seconds / Decimal(str(hold_duration_count)) / _SECONDS_PER_HOUR
            )

        return (
            total_settled_count,
            winning_count,
            losing_count,
            breakeven_count,
            total_realized_pnl,
            avg_hold_duration_hours,
            best_pnl,
            worst_pnl,
        )
