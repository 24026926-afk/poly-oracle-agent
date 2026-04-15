"""
src/schemas/execution.py

Typed execution-routing result contracts for WI-16.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

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


class ExitReason(str, Enum):
    """Categorized reason for exiting (or holding) an open position."""

    NO_EDGE = "NO_EDGE"
    STOP_LOSS = "STOP_LOSS"
    TIME_DECAY = "TIME_DECAY"
    TAKE_PROFIT = "TAKE_PROFIT"
    STALE_MARKET = "STALE_MARKET"
    ERROR = "ERROR"


class ExitSignal(BaseModel):
    """Typed input for a single position exit evaluation."""

    position: PositionRecord
    current_midpoint: Decimal
    current_best_bid: Decimal
    evaluated_at_utc: datetime

    @field_validator("current_midpoint", "current_best_bid", mode="before")
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = {"frozen": True}


class ExitResult(BaseModel):
    """Typed output for a single position exit evaluation."""

    position_id: str
    condition_id: str
    should_exit: bool
    exit_reason: ExitReason
    entry_price: Decimal
    current_midpoint: Decimal
    current_best_bid: Decimal
    position_age_hours: Decimal
    unrealized_edge: Decimal
    evaluated_at_utc: datetime

    @field_validator(
        "entry_price",
        "current_midpoint",
        "current_best_bid",
        "position_age_hours",
        "unrealized_edge",
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


class ExitOrderAction(str, Enum):
    """Exit order router outcomes."""

    SELL_ROUTED = "SELL_ROUTED"
    DRY_RUN = "DRY_RUN"
    FAILED = "FAILED"
    SKIP = "SKIP"


class ExitOrderResult(BaseModel):
    """Typed outcome returned by ``ExitOrderRouter.route_exit()``."""

    position_id: str
    condition_id: str
    action: ExitOrderAction
    reason: str | None = None
    order_payload: OrderData | None = None
    signed_order: SignedOrder | None = None
    exit_price: Decimal | None = None
    order_size_usdc: Decimal | None = None
    routed_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @field_validator("exit_price", "order_size_usdc", mode="before")
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


class PnLRecord(BaseModel):
    """Typed realized PnL outcome returned by ``PnLCalculator.settle()``."""

    position_id: str
    condition_id: str
    entry_price: Decimal
    exit_price: Decimal
    order_size_usdc: Decimal
    position_size_tokens: Decimal
    realized_pnl: Decimal
    gas_cost_usdc: Decimal = Decimal("0")
    fees_usdc: Decimal = Decimal("0")
    net_realized_pnl: Decimal | None = None
    closed_at_utc: datetime

    @model_validator(mode="before")
    @classmethod
    def _default_net_pnl_fields(cls, data: Any) -> Any:
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

        realized_pnl = _coerce_decimal(data.get("realized_pnl"), default=Decimal("0"))
        gas_cost_usdc = _coerce_decimal(
            data.get("gas_cost_usdc"),
            default=Decimal("0"),
        )
        fees_usdc = _coerce_decimal(data.get("fees_usdc"), default=Decimal("0"))

        data["gas_cost_usdc"] = gas_cost_usdc
        data["fees_usdc"] = fees_usdc
        if data.get("net_realized_pnl") is None:
            data["net_realized_pnl"] = realized_pnl - gas_cost_usdc - fees_usdc
        return data

    @field_validator(
        "entry_price",
        "exit_price",
        "order_size_usdc",
        "position_size_tokens",
        "realized_pnl",
        "gas_cost_usdc",
        "fees_usdc",
        "net_realized_pnl",
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


class BacktestConfig(BaseModel):
    """Frozen run configuration for WI-33 offline replay."""

    data_dir: str
    start_date: date | None = None
    end_date: date | None = None
    initial_bankroll_usdc: Decimal
    kelly_fraction: Decimal = Decimal("0.25")
    min_confidence: Decimal = Decimal("0.75")
    min_ev_threshold: Decimal = Decimal("0.02")
    dry_run: bool = True

    @field_validator(
        "initial_bankroll_usdc",
        "kelly_fraction",
        "min_confidence",
        "min_ev_threshold",
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


class BacktestDecision(BaseModel):
    """Per-snapshot replay audit record."""

    token_id: str
    timestamp_utc: datetime
    decision: bool
    action: str
    position_size_usdc: Decimal
    ev: Decimal
    confidence: Decimal
    gatekeeper_result: str
    reason: str

    @field_validator("position_size_usdc", "ev", "confidence", mode="before")
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = {"frozen": True}


class BacktestMarketStats(BaseModel):
    """Per-market summary block for WI-33 report output."""

    token_id: str
    total_decisions: int
    trades_executed: int
    win_rate: Decimal
    net_pnl_usdc: Decimal

    @field_validator("win_rate", "net_pnl_usdc", mode="before")
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = {"frozen": True}


class BacktestReport(BaseModel):
    """Frozen top-level report contract for WI-33 backtest runs."""

    total_trades: int
    win_rate: Decimal
    net_pnl_usdc: Decimal
    max_drawdown_usdc: Decimal
    sharpe_ratio: Decimal
    per_market_stats: dict[str, BacktestMarketStats]
    decisions: list[BacktestDecision]
    started_at_utc: datetime
    completed_at_utc: datetime
    config_snapshot: BacktestConfig

    @field_validator(
        "win_rate",
        "net_pnl_usdc",
        "max_drawdown_usdc",
        "sharpe_ratio",
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
