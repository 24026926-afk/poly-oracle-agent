"""
src/schemas/risk.py

Risk/analytics schemas for WI-23+.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, field_validator, model_validator

_ZERO = Decimal("0")


class PortfolioSnapshot(BaseModel):
    """Typed aggregate portfolio state at a point in time."""

    snapshot_at_utc: datetime
    position_count: int
    total_notional_usdc: Decimal
    total_unrealized_pnl: Decimal
    total_locked_collateral_usdc: Decimal
    positions_with_stale_price: int
    dry_run: bool

    @field_validator(
        "total_notional_usdc",
        "total_unrealized_pnl",
        "total_locked_collateral_usdc",
        mode="before",
    )
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = {"frozen": True}


class PositionLifecycleEntry(BaseModel):
    """Per-position detail record for lifecycle reporting."""

    position_id: str
    slug: str
    entry_price: Decimal
    exit_price: Decimal | None
    size_tokens: Decimal
    realized_pnl: Decimal | None
    gas_cost_usdc: Decimal = Decimal("0")
    fees_usdc: Decimal = Decimal("0")
    net_realized_pnl: Decimal | None = None
    status: str
    opened_at_utc: datetime
    settled_at_utc: datetime | None

    @model_validator(mode="before")
    @classmethod
    def _normalize_cost_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        def _coerce_decimal(value: Any, *, default: Decimal) -> Decimal:
            if value is None:
                return default
            if isinstance(value, float):
                raise ValueError("Float financial values are forbidden; use Decimal")
            if isinstance(value, Decimal):
                return value
            return Decimal(str(value))

        realized_pnl = data.get("realized_pnl")
        gas_cost_usdc = _coerce_decimal(
            data.get("gas_cost_usdc"),
            default=_ZERO,
        )
        fees_usdc = _coerce_decimal(data.get("fees_usdc"), default=_ZERO)

        data["gas_cost_usdc"] = gas_cost_usdc
        data["fees_usdc"] = fees_usdc
        if data.get("net_realized_pnl") is None:
            if realized_pnl is None:
                data["net_realized_pnl"] = None
            else:
                realized_pnl_decimal = _coerce_decimal(realized_pnl, default=_ZERO)
                data["net_realized_pnl"] = (
                    realized_pnl_decimal - gas_cost_usdc - fees_usdc
                )
        return data

    @field_validator(
        "entry_price",
        "size_tokens",
        "gas_cost_usdc",
        "fees_usdc",
        mode="before",
    )
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @field_validator(
        "exit_price",
        "realized_pnl",
        "net_realized_pnl",
        mode="before",
    )
    @classmethod
    def _reject_float_nullable_financials(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = {"frozen": True}


class LifecycleReport(BaseModel):
    """Typed aggregate lifecycle performance report."""

    report_at_utc: datetime
    total_settled_count: int
    winning_count: int
    losing_count: int
    breakeven_count: int
    total_realized_pnl: Decimal
    total_gas_cost_usdc: Decimal = Decimal("0")
    total_fees_usdc: Decimal = Decimal("0")
    total_net_realized_pnl: Decimal = Decimal("0")
    avg_hold_duration_hours: Decimal
    best_pnl: Decimal
    worst_pnl: Decimal
    entries: list[PositionLifecycleEntry]
    dry_run: bool

    @field_validator(
        "total_realized_pnl",
        "total_gas_cost_usdc",
        "total_fees_usdc",
        "total_net_realized_pnl",
        "avg_hold_duration_hours",
        "best_pnl",
        "worst_pnl",
        mode="before",
    )
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @model_validator(mode="after")
    def _validate_settled_count_invariant(self) -> "LifecycleReport":
        if (
            self.winning_count + self.losing_count + self.breakeven_count
            != self.total_settled_count
        ):
            raise ValueError(
                "winning_count + losing_count + breakeven_count must equal total_settled_count"
            )
        return self

    model_config = {"frozen": True}


class ExposureSummary(BaseModel):
    """Point-in-time exposure snapshot for WI-30 entry validation."""

    aggregate_exposure_usdc: Decimal
    category_exposures: dict[str, Decimal]
    proposed_size_usdc: Decimal
    bankroll_usdc: Decimal
    global_limit_usdc: Decimal
    category_limit_usdc: Decimal
    available_headroom_usdc: Decimal
    category_headroom: dict[str, Decimal]
    aggregate_check_passed: bool
    category_check_passed: bool
    validation_passed: bool

    @field_validator(
        "aggregate_exposure_usdc",
        "proposed_size_usdc",
        "bankroll_usdc",
        "global_limit_usdc",
        "category_limit_usdc",
        "available_headroom_usdc",
        mode="before",
    )
    @classmethod
    def _reject_float_decimal_fields(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @field_validator("category_exposures", "category_headroom", mode="before")
    @classmethod
    def _reject_float_decimal_maps(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("Expected mapping for category exposure fields")

        coerced: dict[str, Decimal] = {}
        for key, raw_amount in value.items():
            if isinstance(raw_amount, float):
                raise ValueError("Float financial values are forbidden; use Decimal")
            if isinstance(raw_amount, Decimal):
                coerced[str(key)] = raw_amount
            else:
                coerced[str(key)] = Decimal(str(raw_amount))
        return coerced

    model_config = {"frozen": True}


class AlertSeverity(str, Enum):
    """Severity classification for alert events."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertEvent(BaseModel):
    """Typed immutable alert event emitted by AlertEngine."""

    alert_at_utc: datetime
    severity: AlertSeverity
    rule_name: str
    message: str
    threshold_value: Decimal
    actual_value: Decimal
    dry_run: bool

    @field_validator(
        "threshold_value",
        "actual_value",
        mode="before",
    )
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = {"frozen": True}
