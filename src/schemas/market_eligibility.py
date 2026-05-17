"""
src/schemas/market_eligibility.py

Pydantic V2 schemas for WI-53: Market Eligibility Preflight, Evaluation
Deduplication, and Prompt Queue Backpressure.

All financial comparisons use Decimal.  No float is permitted in price,
midpoint, spread, or delta fields.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ── Helpers ───────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _reject_float_for_financial(value: Any, field_name: str) -> Decimal:
    """Centralised float-rejection for financial fields."""
    if isinstance(value, float):
        raise ValueError(f"Float values are forbidden for '{field_name}'; use Decimal")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# ── Enums: Preflight ─────────────────────────────────────────────────────


class MarketEligibilityStatus(str, Enum):
    """Eligibility status for a market candidate."""

    ELIGIBLE = "ELIGIBLE"
    SKIPPED = "SKIPPED"
    QUARANTINED = "QUARANTINED"


class MarketEligibilitySkipReason(str, Enum):
    """Typed skip reasons for preflight failures."""

    MISSING_TOKEN_CONTEXT = "MISSING_TOKEN_CONTEXT"
    ORDER_BOOK_UNAVAILABLE = "ORDER_BOOK_UNAVAILABLE"
    NON_POSITIVE_QUOTE = "NON_POSITIVE_QUOTE"
    CROSSED_BOOK = "CROSSED_BOOK"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    PREFLIGHT_TIMEOUT = "PREFLIGHT_TIMEOUT"


# ── Enums: Quarantine ────────────────────────────────────────────────────


class MarketQuarantineReason(str, Enum):
    """Reason a market was placed in quarantine."""

    REPEATED_PREFLIGHT_FAILURE = "REPEATED_PREFLIGHT_FAILURE"


# ── Enums: Dedupe ────────────────────────────────────────────────────────


class MarketEvaluationDedupeReason(str, Enum):
    """Reason a market evaluation was deduped or emitted."""

    UNCHANGED_STATE = "UNCHANGED_STATE"
    INSUFFICIENT_ELAPSED_TIME = "INSUFFICIENT_ELAPSED_TIME"
    MIDPOINT_MOVED = "MIDPOINT_MOVED"
    SPREAD_MOVED = "SPREAD_MOVED"


# ── Enums: Backpressure ──────────────────────────────────────────────────


class PromptQueueBackpressureReason(str, Enum):
    """Reason for prompt queue backpressure action."""

    QUEUE_FULL = "QUEUE_FULL"
    COALESCED = "COALESCED"
    STALE_DROPPED = "STALE_DROPPED"


class StaleContextSkipReason(str, Enum):
    """Reason a stale context was skipped or dropped."""

    QUEUE_FULL_NO_MATCH = "QUEUE_FULL_NO_MATCH"
    COALESCED_REPLACED = "COALESCED_REPLACED"


# ── Schemas: Preflight ───────────────────────────────────────────────────


class MarketEligibilityPreflightResult(BaseModel):
    """Result of a single market's preflight check."""

    condition_id: str = Field(..., min_length=1)
    status: MarketEligibilityStatus = Field(...)
    skip_reason: Optional[MarketEligibilitySkipReason] = Field(
        default=None,
    )
    midpoint: Optional[Decimal] = Field(default=None)
    spread: Optional[Decimal] = Field(default=None)

    @field_validator("midpoint", "spread", mode="before")
    @classmethod
    def _reject_float_midpoint_spread(cls, v: Any) -> Decimal | None:
        if v is None:
            return None
        if isinstance(v, float):
            raise ValueError("Float values are forbidden; use Decimal")
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    model_config = {"frozen": True}


# ── Schemas: Quarantine ──────────────────────────────────────────────────


class MarketQuarantineDecision(BaseModel):
    """Decision to quarantine a market."""

    condition_id: str = Field(..., min_length=1)
    reason: MarketQuarantineReason = Field(...)
    quarantined_at_utc: datetime = Field(default_factory=_utc_now)
    expires_at_utc: datetime = Field(...)

    model_config = {"frozen": True}


# ── Schemas: Dedupe ──────────────────────────────────────────────────────


class MarketEvaluationFingerprint(BaseModel):
    """Fingerprint representing material market state for dedupe."""

    condition_id: str = Field(..., min_length=1)
    midpoint: Decimal = Field(...)
    spread: Decimal = Field(...)
    captured_at_utc: datetime = Field(default_factory=_utc_now)

    @field_validator("midpoint", "spread", mode="before")
    @classmethod
    def _reject_float_fingerprint(cls, v: Any) -> Decimal:
        if isinstance(v, float):
            raise ValueError("Float values are forbidden; use Decimal")
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))

    model_config = {"frozen": True}


class MarketEvaluationDedupeDecision(BaseModel):
    """Dedupe decision for a single market evaluation."""

    condition_id: str = Field(..., min_length=1)
    emit: bool = Field(...)
    reason: MarketEvaluationDedupeReason = Field(...)

    model_config = {"frozen": True}


# ── Schemas: Backpressure ────────────────────────────────────────────────


class PromptQueueBackpressureDecision(BaseModel):
    """Backpressure decision when prompt queue is full."""

    action: str = Field(..., min_length=1)  # "enqueue", "coalesce", "drop"
    reason: PromptQueueBackpressureReason = Field(...)
    queue_depth: int = Field(..., ge=0)
    condition_id: Optional[str] = Field(default=None)

    model_config = {"frozen": True}


class PromptQueueDepthSnapshot(BaseModel):
    """Snapshot of prompt queue depth for metrics."""

    current_depth: int = Field(..., ge=0)
    max_size: int = Field(..., ge=0)
    captured_at_utc: datetime = Field(default_factory=_utc_now)

    model_config = {"frozen": True}
