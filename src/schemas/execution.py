"""
src/schemas/execution.py

Typed execution-routing result contracts for WI-16.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.schemas.position import PositionRecord, PositionStatus
from src.schemas.web3 import OrderData, SignedOrder


class ExecutionAction(str, Enum):
    """Execution router outcomes."""

    SKIP = "SKIP"
    DRY_RUN = "DRY_RUN"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class ExecutionResult(BaseModel):
    """Typed outcome returned by ``ExecutionRouter.route()``."""

    action: ExecutionAction
    reason: str | None = None
    order_payload: OrderData | None = None
    signed_order: SignedOrder | None = None
    kelly_fraction: Decimal | None = None
    order_size_usdc: Decimal | None = None
    midpoint_probability: Decimal | None = None
    best_ask: Decimal | None = None
    bankroll_usdc: Decimal | None = None
    routed_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @field_validator(
        "kelly_fraction",
        "order_size_usdc",
        "midpoint_probability",
        "best_ask",
        "bankroll_usdc",
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


# Resolve the forward reference to ExecutionAction in PositionRecord.
PositionRecord.model_rebuild(_types_namespace={"ExecutionAction": ExecutionAction})
