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
    status: str
    opened_at_utc: datetime
    settled_at_utc: datetime | None

    @field_validator(
        "entry_price",
        "size_tokens",
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
    avg_hold_duration_hours: Decimal
    best_pnl: Decimal
    worst_pnl: Decimal
    entries: list[PositionLifecycleEntry]
    dry_run: bool

    @field_validator(
        "total_realized_pnl",
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
