"""
src/backtesting/schemas.py

WI-43 Pydantic schemas for historical Polymarket dataset pipeline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Skip reason — typed audit of every skipped/rejected record
# ---------------------------------------------------------------------------


class SkipReasonCode(str, Enum):
    MISSING_TOKEN_ID = "MISSING_TOKEN_ID"
    MISSING_CONDITION_ID = "MISSING_CONDITION_ID"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    NON_POSITIVE_BID = "NON_POSITIVE_BID"
    NON_POSITIVE_ASK = "NON_POSITIVE_ASK"
    NON_POSITIVE_MIDPOINT = "NON_POSITIVE_MIDPOINT"
    CROSSED_BOOK = "CROSSED_BOOK"
    UNRESOLVED_MARKET = "UNRESOLVED_MARKET"
    AMBIGUOUS_OUTCOME = "AMBIGUOUS_OUTCOME"
    MALFORMED_RECORD = "MALFORMED_RECORD"
    DUPLICATE_SNAPSHOT = "DUPLICATE_SNAPSHOT"
    FLOAT_REJECTED = "FLOAT_REJECTED"
    SOURCE_ERROR = "SOURCE_ERROR"


class HistoricalDataSkipReason(BaseModel):
    """Typed skip reason for auditability — every rejected row gets one."""

    code: SkipReasonCode
    token_id: str | None = None
    condition_id: str | None = None
    message: str
    record_index: int | None = None

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Point-in-time snapshot — observable fields only (NO outcome data)
# ---------------------------------------------------------------------------


def _reject_float_financial(value: Any) -> Any:
    """Validator: reject raw float for monetary/Decimal fields."""
    if value is None:
        return value
    if isinstance(value, float):
        raise ValueError("Float financial values are forbidden; use Decimal")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class HistoricalSnapshotRecord(BaseModel):
    """Point-in-time market snapshot with NO resolution/outcome data.

    Designed to be compatible with BacktestDataLoader output format.
    Outcome fields live on HistoricalMarketRecord, kept separate to
    prevent lookahead leakage into pre-resolution prompts.
    """

    token_id: str
    timestamp_utc: datetime
    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    spread: Decimal
    volume_24h: Decimal | None = None

    @field_validator("timestamp_utc", mode="before")
    @classmethod
    def _normalize_timestamp(cls, value: Any) -> Any:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value
        return value

    @field_validator(
        "best_bid",
        "best_ask",
        "midpoint",
        "spread",
        "volume_24h",
        mode="before",
    )
    @classmethod
    def _validate_monetary_fields(cls, value: Any) -> Any:
        return _reject_float_financial(value)

    @model_validator(mode="after")
    def _validate_book_integrity(self) -> HistoricalSnapshotRecord:
        if self.best_bid <= Decimal("0"):
            raise ValueError("best_bid must be positive")
        if self.best_ask <= Decimal("0"):
            raise ValueError("best_ask must be positive")
        if self.midpoint <= Decimal("0"):
            raise ValueError("midpoint must be positive")
        if self.best_bid > self.best_ask:
            raise ValueError(
                f"Crossed book: best_bid={self.best_bid} > best_ask={self.best_ask}"
            )
        return self

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Market-level record — contains outcome/resolution data kept separate from
# point-in-time snapshots
# ---------------------------------------------------------------------------


class HistoricalMarketRecord(BaseModel):
    """Market metadata and resolution/outcome data for a single market.

    Snapshots contain only point-in-time observable fields.
    Resolution/outcome fields (resolved_outcome, resolved_outcome_price,
    realized_pnl_usdc) live here and must NOT be injected into prompts
    before simulated market resolution.
    """

    token_id: str
    condition_id: str
    market_end_date: datetime | None = None
    snapshots: list[HistoricalSnapshotRecord] = Field(default_factory=list)
    resolved_outcome: str | None = None
    resolved_outcome_price: Decimal | None = None
    realized_pnl_usdc: Decimal | None = None

    @field_validator(
        "resolved_outcome_price",
        "realized_pnl_usdc",
        mode="before",
    )
    @classmethod
    def _validate_monetary_fields(cls, value: Any) -> Any:
        return _reject_float_financial(value)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Dataset manifest — build-time metadata
# ---------------------------------------------------------------------------


class HistoricalDatasetManifest(BaseModel):
    """Metadata written alongside generated historical dataset files."""

    market_count: int
    snapshot_count: int
    skipped_count: int
    start_date: str
    end_date: str
    source_identifiers: list[str] = Field(default_factory=list)
    generated_at_utc: str
    output_dir: str

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Build result — returned by the builder, includes manifest + skip log
# ---------------------------------------------------------------------------


class HistoricalDatasetBuildResult(BaseModel):
    """Result of a dataset build run, including manifest and skip audit."""

    manifest: HistoricalDatasetManifest
    skipped: list[HistoricalDataSkipReason] = Field(default_factory=list)
    source_failure: bool = False

    @property
    def success(self) -> bool:
        """Build succeeded if no source failures and at least one market or
        zero eligible is valid."""
        return not self.source_failure

    model_config = {"frozen": True}
