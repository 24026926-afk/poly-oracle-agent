"""
src/schemas/position.py

Typed position-tracking contracts for WI-17.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, field_validator

if TYPE_CHECKING:
    from src.schemas.execution import ExecutionAction


class PositionStatus(str, Enum):
    """Lifecycle status for persisted positions."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"
    FAILED = "FAILED"


class PositionRecord(BaseModel):
    """Immutable execution snapshot persisted by WI-17 PositionTracker."""

    id: str
    condition_id: str
    token_id: str
    status: PositionStatus
    side: str
    entry_price: Decimal
    order_size_usdc: Decimal
    kelly_fraction: Decimal
    best_ask_at_entry: Decimal
    bankroll_usdc_at_entry: Decimal
    execution_action: "ExecutionAction"
    reason: str | None = None
    routed_at_utc: datetime
    recorded_at_utc: datetime
    realized_pnl: Decimal | None = None
    exit_price: Decimal | None = None
    closed_at_utc: datetime | None = None
    gas_cost_usdc: Decimal | None = None
    fees_usdc: Decimal | None = None

    @field_validator(
        "entry_price",
        "order_size_usdc",
        "kelly_fraction",
        "best_ask_at_entry",
        "bankroll_usdc_at_entry",
        "realized_pnl",
        "exit_price",
        "gas_cost_usdc",
        "fees_usdc",
        mode="before",
    )
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = {"frozen": True}
