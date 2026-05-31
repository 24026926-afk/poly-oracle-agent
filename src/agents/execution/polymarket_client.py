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


def _level_price(entry: Any) -> Decimal:
    """Extract a Decimal price from a dict or dataclass order-book level.

    Supports both dict entries (``{"price": ...}``) and SDK dataclass entries
    (``OrderSummary`` with ``.price``). Raises on missing/malformed price so the
    caller's ``(KeyError, TypeError, ArithmeticError)`` guard handles it.
    """
    raw_price = entry["price"] if isinstance(entry, dict) else entry.price
    return Decimal(str(raw_price))


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
        host = self.host

        def _sync_fetch() -> dict[str, Any]:
            # Import and constructor both run in the thread pool so the event
            # loop is never blocked by pyclob module-level side effects or
            # any synchronous I/O the ClobClient constructor might perform.
            from py_clob_client.client import ClobClient  # noqa: PLC0415

            clob = ClobClient(host)
            return clob.get_order_book(token_id)

        loop = asyncio.get_running_loop()
        book = await asyncio.wait_for(
            loop.run_in_executor(None, _sync_fetch),
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
            from dataclasses import asdict

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
            # CLOB returns levels ordered worst -> best, so index [0] is the
            # worst level on each side and would fabricate a near-empty book
            # (e.g. bid 0.001 / ask 0.999). Select best-of-book by value: the
            # maximum positive bid and the minimum positive ask. Order-
            # independent, mirroring the WS client's _best_bid_from_levels /
            # _best_ask_from_levels.
            bid_prices = [p for p in map(_level_price, bids) if p > 0]
            ask_prices = [p for p in map(_level_price, asks) if p > 0]
        except (KeyError, TypeError, ArithmeticError) as exc:
            logger.warning(
                "Malformed top-of-book price field.",
                token_id=token_id,
                error=str(exc),
            )
            return None

        if not bid_prices or not ask_prices:
            logger.warning(
                "No positive bid/ask levels in order book.",
                token_id=token_id,
                has_positive_bid=bool(bid_prices),
                has_positive_ask=bool(ask_prices),
            )
            return None

        best_bid = max(bid_prices)
        best_ask = min(ask_prices)

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
