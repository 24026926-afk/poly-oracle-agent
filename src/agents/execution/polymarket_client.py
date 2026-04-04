"""
src/agents/execution/polymarket_client.py

Read-only Polymarket CLOB market data client (WI-14).
Fetches order book snapshots via the official pyclob SDK and returns
Decimal-typed pricing for the cognitive evaluation path.

NO signing, NO private keys, NO order execution.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog
from pydantic import BaseModel, field_validator

logger = structlog.get_logger(__name__)


class MarketSnapshot(BaseModel):
    """Typed, Decimal-safe order book snapshot for downstream evaluation."""

    token_id: str
    best_bid: Decimal
    best_ask: Decimal
    midpoint_probability: Decimal
    spread: Decimal
    fetched_at_utc: datetime
    source: str

    @field_validator("best_bid", "best_ask")
    @classmethod
    def _validate_positive(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Price must be positive (zero is non-tradable)")
        return v


class PolymarketClient:
    """Read-only market data client for Polymarket CLOB order books.

    Initializes without private key or signer — strictly public market data.
    All pricing fields use ``Decimal`` to preserve deterministic precision.
    """

    _FETCH_TIMEOUT: float = 0.5  # 500 ms budget per pyclob call

    def __init__(self, host: str) -> None:
        self.host = host

    # ------------------------------------------------------------------
    # Raw SDK layer (mocked in tests)
    # ------------------------------------------------------------------

    async def _fetch_raw_order_book(self, token_id: str) -> dict[str, Any]:
        """Fetch raw order book from pyclob SDK with strict timeout."""
        from py_clob_client.client import ClobClient  # lazy import

        clob = ClobClient(self.host)
        loop = asyncio.get_running_loop()
        book = await asyncio.wait_for(
            loop.run_in_executor(None, clob.get_order_book, token_id),
            timeout=self._FETCH_TIMEOUT,
        )
        return book

    # ------------------------------------------------------------------
    # Public contract
    # ------------------------------------------------------------------

    async def fetch_order_book(self, token_id: str) -> MarketSnapshot | None:
        """Fetch order book and return a typed snapshot, or ``None`` on failure.

        Any SDK/network error results in ``None`` (conservative non-tradable).
        """
        try:
            raw = await self._fetch_raw_order_book(token_id)
        except asyncio.TimeoutError:
            logger.warning(
                "Order book fetch timed out.",
                token_id=token_id,
                timeout_s=self._FETCH_TIMEOUT,
            )
            return None
        except ConnectionError as exc:
            logger.warning(
                "Connection error fetching order book.",
                token_id=token_id,
                error=str(exc),
            )
            return None
        except Exception as exc:
            logger.error(
                "Unexpected error fetching order book.",
                token_id=token_id,
                error=str(exc),
            )
            return None

        return self._parse_order_book(token_id, raw)

    # ------------------------------------------------------------------
    # Internal parsing / Decimal math
    # ------------------------------------------------------------------

    def _parse_order_book(
        self, token_id: str, raw: dict[str, Any] | Any
    ) -> MarketSnapshot | None:
        """Parse raw order book dict or SDK dataclass into a ``MarketSnapshot``.

        Returns ``None`` for missing sides, crossed books, or invalid data.
        Handles both dict responses and SDK ``OrderBookSummary`` dataclasses.
        """
        # Normalise SDK dataclass to dict for uniform access
        if hasattr(raw, "model_dump"):
            raw = raw.model_dump()
        elif not isinstance(raw, dict) and hasattr(raw, "__dict__"):
            from dataclasses import asdict, fields

            if hasattr(raw, "__dataclass_fields__"):
                raw = asdict(raw)
            else:
                raw = vars(raw)

        bids = raw.get("bids", [])
        asks = raw.get("asks", [])

        if not bids or not asks:
            logger.warning(
                "Missing bids or asks in order book.",
                token_id=token_id,
                has_bids=bool(bids),
                has_asks=bool(asks),
            )
            return None

        try:
            # Support both dict entries and dataclass entries (OrderSummary)
            bid_0 = bids[0]
            ask_0 = asks[0]
            bid_price = bid_0["price"] if isinstance(bid_0, dict) else bid_0.price
            ask_price = ask_0["price"] if isinstance(ask_0, dict) else ask_0.price
            best_bid = Decimal(str(bid_price))
            best_ask = Decimal(str(ask_price))
        except (KeyError, TypeError, ArithmeticError) as exc:
            logger.warning(
                "Malformed top-of-book price field.",
                token_id=token_id,
                error=str(exc),
            )
            return None

        # Reject crossed book
        if best_ask < best_bid:
            logger.warning(
                "Crossed book rejected.",
                token_id=token_id,
                best_bid=str(best_bid),
                best_ask=str(best_ask),
            )
            return None

        midpoint = (best_bid + best_ask) / Decimal("2")
        spread = best_ask - best_bid

        return MarketSnapshot(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            midpoint_probability=midpoint,
            spread=spread,
            fetched_at_utc=datetime.now(timezone.utc),
            source="clob_orderbook",
        )
