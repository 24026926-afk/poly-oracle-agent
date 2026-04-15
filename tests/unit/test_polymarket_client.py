"""
tests/unit/test_polymarket_client.py

RED-phase unit tests for WI-14 PolymarketClient.
Validates: read-only initialization, fetch_order_book contract,
Decimal-only midpoint math, and graceful error handling.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from src.agents.execution.polymarket_client import MarketSnapshot, PolymarketClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_VALID_ORDER_BOOK = {
    "bids": [{"price": "0.45", "size": "100"}],
    "asks": [{"price": "0.55", "size": "80"}],
}

_EMPTY_BIDS_BOOK = {
    "bids": [],
    "asks": [{"price": "0.55", "size": "80"}],
}

_EMPTY_ASKS_BOOK = {
    "bids": [{"price": "0.45", "size": "100"}],
    "asks": [],
}

_CROSSED_BOOK = {
    "bids": [{"price": "0.60", "size": "100"}],
    "asks": [{"price": "0.40", "size": "80"}],
}

_TIGHT_SPREAD_BOOK = {
    "bids": [{"price": "0.500", "size": "200"}],
    "asks": [{"price": "0.502", "size": "150"}],
}


# ---------------------------------------------------------------------------
# 1. Read-only initialization
# ---------------------------------------------------------------------------


class TestPolymarketClientInit:
    """PolymarketClient must initialize without private-key or signer deps."""

    def test_init_read_only_no_private_key(self):
        """Client initializes with host only — no private key parameter."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        assert client is not None

    def test_init_has_no_signer_attribute(self):
        """Client must not expose a signer or private_key attribute."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        assert not hasattr(client, "signer")
        assert not hasattr(client, "private_key")

    def test_init_stores_host(self):
        """Host URL is stored for SDK connection."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        assert client.host == "https://clob.polymarket.com"


# ---------------------------------------------------------------------------
# 2. fetch_order_book — happy path
# ---------------------------------------------------------------------------


class TestFetchOrderBookHappyPath:
    """fetch_order_book must return a typed MarketSnapshot on valid books."""

    @pytest.mark.asyncio
    async def test_returns_market_snapshot(self):
        """Valid order book returns a MarketSnapshot instance."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert isinstance(result, MarketSnapshot)

    @pytest.mark.asyncio
    async def test_snapshot_contains_token_id(self):
        """Snapshot reflects the requested token_id."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert result.token_id == "tok-yes-001"

    @pytest.mark.asyncio
    async def test_snapshot_best_bid_is_decimal(self):
        """best_bid field is Decimal-typed."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert isinstance(result.best_bid, Decimal)
        assert result.best_bid == Decimal("0.45")

    @pytest.mark.asyncio
    async def test_snapshot_best_ask_is_decimal(self):
        """best_ask field is Decimal-typed."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert isinstance(result.best_ask, Decimal)
        assert result.best_ask == Decimal("0.55")

    @pytest.mark.asyncio
    async def test_snapshot_source_is_clob(self):
        """source field identifies order book origin."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert result.source == "clob_orderbook"

    @pytest.mark.asyncio
    async def test_snapshot_has_fetched_at_utc(self):
        """Snapshot must include a fetched_at_utc timestamp."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert result.fetched_at_utc is not None


# ---------------------------------------------------------------------------
# 3. Decimal midpoint math
# ---------------------------------------------------------------------------


class TestDecimalMidpointMath:
    """Midpoint probability must use Decimal-only arithmetic."""

    @pytest.mark.asyncio
    async def test_midpoint_is_decimal(self):
        """midpoint_probability must be Decimal-typed."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert isinstance(result.midpoint_probability, Decimal)

    @pytest.mark.asyncio
    async def test_midpoint_correct_value(self):
        """midpoint = (best_bid + best_ask) / 2 using Decimal."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        expected = (Decimal("0.45") + Decimal("0.55")) / Decimal("2")
        assert result.midpoint_probability == expected

    @pytest.mark.asyncio
    async def test_spread_is_decimal(self):
        """spread must be Decimal-typed."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert isinstance(result.spread, Decimal)

    @pytest.mark.asyncio
    async def test_spread_correct_value(self):
        """spread = best_ask - best_bid."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        expected = Decimal("0.55") - Decimal("0.45")
        assert result.spread == expected

    @pytest.mark.asyncio
    async def test_tight_spread_precision(self):
        """Tight spread (0.002) must be precise with Decimal."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _TIGHT_SPREAD_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert result.best_bid == Decimal("0.500")
        assert result.best_ask == Decimal("0.502")
        assert result.midpoint_probability == Decimal("0.501")
        assert result.spread == Decimal("0.002")

    @pytest.mark.asyncio
    async def test_no_float_in_snapshot_money_fields(self):
        """No float type in any money-path field of the snapshot."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        for field_name in ("best_bid", "best_ask", "midpoint_probability", "spread"):
            val = getattr(result, field_name)
            assert isinstance(val, Decimal), (
                f"{field_name} is {type(val)}, expected Decimal"
            )
            assert not isinstance(val, float), f"{field_name} must not be float"


# ---------------------------------------------------------------------------
# 4. Invalid order book → non-tradable
# ---------------------------------------------------------------------------


class TestInvalidOrderBook:
    """Invalid/incomplete books must produce non-tradable outcomes."""

    @pytest.mark.asyncio
    async def test_missing_bids_returns_none(self):
        """Empty bids list → non-tradable (None)."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _EMPTY_BIDS_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert result is None

    @pytest.mark.asyncio
    async def test_missing_asks_returns_none(self):
        """Empty asks list → non-tradable (None)."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _EMPTY_ASKS_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert result is None

    @pytest.mark.asyncio
    async def test_crossed_book_returns_none(self):
        """best_ask < best_bid (crossed book) → rejected, returns None."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _CROSSED_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_book_returns_none(self):
        """Completely empty order book → non-tradable."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = {"bids": [], "asks": []}
            result = await client.fetch_order_book("tok-yes-001")
        assert result is None


# ---------------------------------------------------------------------------
# 5. Network/SDK errors → graceful conservative behavior
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """pyclob connection errors and timeouts must be handled gracefully."""

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        """SDK timeout → returns None (non-tradable), no crash."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = asyncio.TimeoutError()
            result = await client.fetch_order_book("tok-yes-001")
        assert result is None

    @pytest.mark.asyncio
    async def test_connection_error_returns_none(self):
        """Connection error → returns None (non-tradable), no crash."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = ConnectionError("CLOB unreachable")
            result = await client.fetch_order_book("tok-yes-001")
        assert result is None

    @pytest.mark.asyncio
    async def test_generic_exception_returns_none(self):
        """Any unexpected exception → returns None (non-tradable), no crash."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.side_effect = RuntimeError("SDK internal error")
            result = await client.fetch_order_book("tok-yes-001")
        assert result is None


# ---------------------------------------------------------------------------
# 6. MarketSnapshot schema validation
# ---------------------------------------------------------------------------


class TestMarketSnapshotSchema:
    """MarketSnapshot Pydantic model validates correct field types."""

    def test_valid_snapshot_construction(self):
        """MarketSnapshot can be constructed with valid Decimal fields."""
        from datetime import datetime, timezone

        snapshot = MarketSnapshot(
            token_id="tok-yes-001",
            best_bid=Decimal("0.45"),
            best_ask=Decimal("0.55"),
            midpoint_probability=Decimal("0.50"),
            spread=Decimal("0.10"),
            fetched_at_utc=datetime.now(timezone.utc),
            source="clob_orderbook",
        )
        assert snapshot.token_id == "tok-yes-001"
        assert snapshot.best_bid == Decimal("0.45")

    def test_snapshot_rejects_negative_bid(self):
        """Negative best_bid must be rejected by validation."""
        from datetime import datetime, timezone

        with pytest.raises(Exception):
            MarketSnapshot(
                token_id="tok-yes-001",
                best_bid=Decimal("-0.10"),
                best_ask=Decimal("0.55"),
                midpoint_probability=Decimal("0.225"),
                spread=Decimal("0.65"),
                fetched_at_utc=datetime.now(timezone.utc),
                source="clob_orderbook",
            )

    def test_snapshot_rejects_zero_bid(self):
        """Zero best_bid is non-tradable and must be rejected."""
        from datetime import datetime, timezone

        with pytest.raises(Exception):
            MarketSnapshot(
                token_id="tok-yes-001",
                best_bid=Decimal("0"),
                best_ask=Decimal("0.55"),
                midpoint_probability=Decimal("0.275"),
                spread=Decimal("0.55"),
                fetched_at_utc=datetime.now(timezone.utc),
                source="clob_orderbook",
            )

    def test_snapshot_rejects_zero_ask(self):
        """Zero best_ask is non-tradable and must be rejected."""
        from datetime import datetime, timezone

        with pytest.raises(Exception):
            MarketSnapshot(
                token_id="tok-yes-001",
                best_bid=Decimal("0.45"),
                best_ask=Decimal("0"),
                midpoint_probability=Decimal("0.225"),
                spread=Decimal("0.45"),
                fetched_at_utc=datetime.now(timezone.utc),
                source="clob_orderbook",
            )


# ---------------------------------------------------------------------------
# 7. Malformed price field handling
# ---------------------------------------------------------------------------


class TestMalformedPriceFields:
    """Malformed or missing price fields must return None, not raise."""

    @pytest.mark.asyncio
    async def test_missing_price_key_returns_none(self):
        """Order book entry without 'price' key → None."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        book = {
            "bids": [{"size": "100"}],
            "asks": [{"price": "0.55", "size": "80"}],
        }
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = book
            result = await client.fetch_order_book("tok-yes-001")
        assert result is None

    @pytest.mark.asyncio
    async def test_none_price_returns_none(self):
        """Price value of None → None."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        book = {
            "bids": [{"price": None, "size": "100"}],
            "asks": [{"price": "0.55", "size": "80"}],
        }
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = book
            result = await client.fetch_order_book("tok-yes-001")
        assert result is None


# ---------------------------------------------------------------------------
# 8. SDK OrderBookSummary dataclass compatibility
# ---------------------------------------------------------------------------


class TestOrderBookSummaryDataclassInput:
    """_parse_order_book must handle SDK OrderBookSummary dataclass, not just dicts."""

    @pytest.mark.asyncio
    async def test_parse_handles_orderbooksummary_dataclass(self):
        """SDK returns OrderBookSummary dataclass — must not crash on .get()."""
        from dataclasses import dataclass

        @dataclass
        class OrderSummary:
            price: str = None
            size: str = None

        @dataclass
        class OrderBookSummary:
            market: str = None
            asset_id: str = None
            bids: list = None
            asks: list = None

        sdk_response = OrderBookSummary(
            market="0xabc",
            asset_id="tok-yes-001",
            bids=[OrderSummary(price="0.45", size="100")],
            asks=[OrderSummary(price="0.55", size="80")],
        )

        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = sdk_response
            result = await client.fetch_order_book("tok-yes-001")

        assert isinstance(result, MarketSnapshot)
        assert result.best_bid == Decimal("0.45")
        assert result.best_ask == Decimal("0.55")

    @pytest.mark.asyncio
    async def test_parse_still_works_with_dict_input(self):
        """Dict input must continue to work after dataclass support is added."""
        client = PolymarketClient(host="https://clob.polymarket.com")
        with patch.object(
            client, "_fetch_raw_order_book", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = _VALID_ORDER_BOOK
            result = await client.fetch_order_book("tok-yes-001")
        assert isinstance(result, MarketSnapshot)
        assert result.best_bid == Decimal("0.45")
