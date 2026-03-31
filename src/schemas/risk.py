"""
src/schemas/risk.py

Risk/analytics schemas for WI-23+.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, field_validator


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
