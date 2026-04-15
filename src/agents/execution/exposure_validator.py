"""
src/agents/execution/exposure_validator.py

WI-30 portfolio exposure gate.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog

from src.core.config import AppConfig
from src.db.models import Position
from src.db.repositories.position_repository import PositionRepository
from src.schemas.llm import MarketCategory
from src.schemas.risk import ExposureSummary

log = structlog.get_logger(__name__)
_ZERO = Decimal("0")


class ExposureValidator:
    """Validate proposed entry size against global and per-category caps."""

    def __init__(
        self,
        config: AppConfig,
        position_repo: PositionRepository | None = None,
    ) -> None:
        self.config = config
        self._position_repo = position_repo
        # WI-30 pre-flight: current Position ORM has no category column.
        self._supports_category_exposure = hasattr(Position, "category")
        self._log = log.bind(component="ExposureValidator")

    async def validate_new_trade(
        self,
        *,
        new_trade_size_usdc: Decimal,
        category: MarketCategory,
        bankroll_usdc: Decimal | None = None,
    ) -> tuple[bool, ExposureSummary]:
        """Async convenience entrypoint that reads open positions from repository."""
        if self._position_repo is None:
            raise ValueError("PositionRepository is required for validate_new_trade().")

        open_positions = await self._position_repo.get_open_positions()
        effective_bankroll = (
            bankroll_usdc
            if bankroll_usdc is not None
            else Decimal(str(self.config.initial_bankroll_usdc))
        )
        return self.validate_entry(
            bankroll_usdc=effective_bankroll,
            proposed_size_usdc=new_trade_size_usdc,
            category=category,
            open_positions=open_positions,
        )

    def validate_entry(
        self,
        *,
        bankroll_usdc: Decimal,
        proposed_size_usdc: Decimal,
        category: MarketCategory,
        open_positions: list[Any],
    ) -> tuple[bool, ExposureSummary]:
        """Synchronous WI-30 validation using pre-fetched open positions."""
        bankroll = self._coerce_decimal(bankroll_usdc)
        proposed_size = self._coerce_decimal(proposed_size_usdc)
        max_exposure_pct = self._coerce_decimal(
            getattr(self.config, "max_exposure_pct", Decimal("0.03"))
        )
        max_category_exposure_pct = self._coerce_decimal(
            getattr(self.config, "max_category_exposure_pct", Decimal("0.015"))
        )

        aggregate_exposure = self._compute_aggregate_exposure(open_positions)
        category_exposure = self._compute_category_exposure(
            category=category,
            positions=open_positions,
        )
        global_limit = bankroll * max_exposure_pct
        category_limit = bankroll * max_category_exposure_pct

        aggregate_check_passed = (
            aggregate_exposure + proposed_size <= global_limit
        )
        category_check_passed = (
            category_exposure + proposed_size <= category_limit
        )
        validation_passed = aggregate_check_passed and category_check_passed

        category_exposures = {
            market_category.value: self._compute_category_exposure(
                category=market_category,
                positions=open_positions,
            )
            for market_category in MarketCategory
        }
        category_headroom = {
            category_name: max(_ZERO, category_limit - exposure)
            for category_name, exposure in category_exposures.items()
        }

        summary = ExposureSummary(
            aggregate_exposure_usdc=aggregate_exposure,
            category_exposures=category_exposures,
            proposed_size_usdc=proposed_size,
            bankroll_usdc=bankroll,
            global_limit_usdc=global_limit,
            category_limit_usdc=category_limit,
            available_headroom_usdc=max(_ZERO, global_limit - aggregate_exposure),
            category_headroom=category_headroom,
            aggregate_check_passed=aggregate_check_passed,
            category_check_passed=category_check_passed,
            validation_passed=validation_passed,
        )

        if validation_passed:
            self._log.info(
                "exposure.validated",
                aggregate_exposure_usdc=str(summary.aggregate_exposure_usdc),
                available_headroom_usdc=str(summary.available_headroom_usdc),
            )
        else:
            breach_type = (
                "aggregate" if not aggregate_check_passed else "category"
            )
            self._log.warning(
                "exposure.limit_exceeded",
                breach_type=breach_type,
                aggregate_exposure_usdc=str(summary.aggregate_exposure_usdc),
                available_headroom_usdc=str(summary.available_headroom_usdc),
            )

        return validation_passed, summary

    def _compute_aggregate_exposure(self, positions: list[Any]) -> Decimal:
        """Return SUM(order_size_usdc) across all supplied OPEN positions."""
        if not positions:
            return _ZERO
        return sum(
            (
                self._coerce_decimal(getattr(position, "order_size_usdc", _ZERO))
                for position in positions
            ),
            _ZERO,
        )

    def _compute_category_exposure(
        self,
        *,
        category: MarketCategory,
        positions: list[Any],
    ) -> Decimal:
        """Return category exposure; falls back to zero when category data is unavailable."""
        if not self._supports_category_exposure:
            return _ZERO

        matching_positions = [
            position
            for position in positions
            if getattr(position, "category", None) == category.value
        ]
        if not matching_positions:
            return _ZERO
        return sum(
            (
                self._coerce_decimal(getattr(position, "order_size_usdc", _ZERO))
                for position in matching_positions
            ),
            _ZERO,
        )

    @staticmethod
    def _coerce_decimal(value: Any) -> Decimal:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal.")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))
