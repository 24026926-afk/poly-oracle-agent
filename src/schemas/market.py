"""
src/schemas/market.py

Pydantic V2 schemas for validating Polymarket CLOB WebSocket data
and Gamma REST API responses.
"""

import json
from datetime import datetime, timezone
from typing import Any, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.schemas.llm import MarketCategory


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PerMarketAggregatorState(BaseModel):
    """Tracks per-market subscription status, last-seen timestamp, and frame count."""

    model_config = ConfigDict(frozen=True)

    token_ids: list[str]
    subscription_status: str = "pending"
    last_seen_utc: datetime | None = None
    frame_count: int = 0


class CLOBTick(BaseModel):
    """
    Represents a single price level in the orderbook.
    Polymarket CLOB sends these as strings, so Pydantic will coerce them to floats.
    """

    price: float = Field(..., description="The limit price of the order")
    size: float = Field(..., description="The quantity available at this price")

    model_config = {
        "frozen": True,
    }


class CLOBMessage(BaseModel):
    """
    Message structure received from the Polymarket CLOB WebSocket.
    """

    event: str = Field(..., description="Event type, e.g., 'book' or 'price_change'")
    market: str = Field(..., description="The condition_id representing the market")
    bids: Optional[List[CLOBTick]] = Field(
        default=None, description="List of bid price levels"
    )
    asks: Optional[List[CLOBTick]] = Field(
        default=None, description="List of ask price levels"
    )

    model_config = {
        "frozen": True,
        # Allow extra fields since the WebSocket may send additional metadata
        # like timestamps or sequence numbers that we might not strictly need right now.
        "extra": "ignore",
    }


class MarketSnapshotSchema(BaseModel):
    """Validated snapshot built from a CLOB WebSocket frame.

    The ``midpoint`` is always auto-computed from ``best_bid`` and
    ``best_ask`` — we never trust externally-provided midpoints.
    """

    condition_id: str = Field(..., min_length=1)
    question: str = Field(default="")
    best_bid: float = Field(..., ge=0.0, le=1.0)
    best_ask: float = Field(..., ge=0.0, le=1.0)
    last_trade_price: Optional[float] = Field(default=None)
    midpoint: float = Field(default=0.0)
    outcome_token: str = Field(default="YES")
    raw_ws_payload: str = Field(..., min_length=1)
    yes_token_id: Optional[str] = Field(default=None)
    no_token_id: Optional[str] = Field(default=None)

    @model_validator(mode="after")
    def _compute_midpoint(self) -> "MarketSnapshotSchema":
        computed = (self.best_bid + self.best_ask) / 2.0
        object.__setattr__(self, "midpoint", round(computed, 6))
        return self

    model_config = {"frozen": True, "extra": "ignore"}


class MarketMetadata(BaseModel):
    """Market metadata returned by the Gamma REST API."""

    condition_id: str = Field(..., alias="conditionId", min_length=1)
    question: str = Field(default="")
    category: Optional[str] = Field(default=None, alias="category")
    tags: list[str] = Field(default_factory=list, alias="tags")
    token_ids: List[str] = Field(default_factory=list, alias="clobTokenIds")
    end_date_iso: Optional[str] = Field(default=None, alias="endDateIso")
    active: bool = Field(default=True)
    closed: bool = Field(default=False)
    volume_24h: Optional[float] = Field(default=None, alias="volume24hr")

    @field_validator("token_ids", mode="before")
    @classmethod
    def _parse_stringified_token_ids(cls, v: object) -> list:
        """Gamma API returns clobTokenIds as a JSON-encoded string."""
        if isinstance(v, str):
            return json.loads(v)
        return v

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, v: object) -> str | None:
        """Normalize Gamma category strings to canonical MarketCategory values."""
        if v is None:
            return None
        if isinstance(v, MarketCategory):
            return v.value
        if isinstance(v, dict):
            v = v.get("label") or v.get("name") or v.get("slug")
        if not isinstance(v, str):
            return None

        normalized = v.strip().upper().replace("-", "_").replace(" ", "_")
        if not normalized:
            return None
        try:
            return MarketCategory(normalized).value
        except ValueError:
            try:
                return MarketCategory[normalized].value
            except KeyError:
                return None

    @field_validator("tags", mode="before")
    @classmethod
    def _normalize_tags(cls, v: object) -> list[str]:
        """Gamma may return tags as strings, dicts, or JSON-encoded arrays."""
        if v is None:
            return []
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except json.JSONDecodeError:
                return [v]
            return cls._normalize_tags(parsed)
        if not isinstance(v, list):
            return []

        tags: list[str] = []
        for item in v:
            raw: Any = item
            if isinstance(item, dict):
                raw = item.get("label") or item.get("name") or item.get("slug")
            if raw is None:
                continue
            tag = str(raw).strip()
            if tag:
                tags.append(tag)
        return tags

    model_config = {"frozen": True, "populate_by_name": True, "extra": "ignore"}
