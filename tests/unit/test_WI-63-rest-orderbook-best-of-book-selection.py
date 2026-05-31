"""
tests/unit/test_WI-63-rest-orderbook-best-of-book-selection.py

WI-63: REST order book best-of-book selection.

The Polymarket CLOB REST order book returns price levels ordered worst -> best.
``PolymarketClient._parse_order_book`` must select the *maximum* positive bid
price and the *minimum* positive ask price across the full ``bids``/``asks``
arrays, mirroring the WebSocket helpers (``_best_bid_from_levels`` /
``_best_ask_from_levels``). Reading ``bids[0]`` / ``asks[0]`` selects the worst
level on each side and fabricates a ~99.8% spread on liquid markets.

These tests assert the corrected behavior and fail against the index-[0] bug.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.agents.execution.polymarket_client import MarketSnapshot, PolymarketClient


@pytest.fixture
def client() -> PolymarketClient:
    return PolymarketClient(host="http://test.invalid")


def _dict_book(bid_prices, ask_prices) -> dict:
    """Build a raw order book with dict entries (worst->best as provided)."""
    return {
        "bids": [{"price": str(p), "size": "10"} for p in bid_prices],
        "asks": [{"price": str(p), "size": "10"} for p in ask_prices],
    }


def _dataclass_book(bid_prices, ask_prices) -> dict:
    """Build a raw order book with SDK-style dataclass entries (.price/.size)."""
    return {
        "bids": [SimpleNamespace(price=str(p), size="10") for p in bid_prices],
        "asks": [SimpleNamespace(price=str(p), size="10") for p in ask_prices],
    }


# ---------------------------------------------------------------------------
# Core bug: best-of-book selection across worst->best ordered arrays
# ---------------------------------------------------------------------------


def test_best_bid_is_max_when_bids_ordered_worst_to_best(client):
    # bids ascending (worst first): best bid is the highest = 0.61
    raw = _dict_book(["0.001", "0.30", "0.61"], ["0.63", "0.80", "0.999"])
    snap = client._parse_order_book("tok", raw)
    assert snap is not None
    assert snap.best_bid == Decimal("0.61")


def test_best_ask_is_min_when_asks_ordered_worst_to_best(client):
    # asks descending (worst first): best ask is the lowest = 0.63
    raw = _dict_book(["0.001", "0.30", "0.61"], ["0.999", "0.80", "0.63"])
    snap = client._parse_order_book("tok", raw)
    assert snap is not None
    assert snap.best_ask == Decimal("0.63")


def test_selection_is_order_independent(client):
    # A shuffled level set must yield the same best_bid/best_ask.
    raw = _dict_book(["0.30", "0.61", "0.001"], ["0.80", "0.63", "0.999"])
    snap = client._parse_order_book("tok", raw)
    assert snap is not None
    assert snap.best_bid == Decimal("0.61")
    assert snap.best_ask == Decimal("0.63")


def test_tight_book_yields_narrow_spread(client):
    # Real liquid book: best bid 0.985, best ask 0.986 -> 0.001 spread.
    raw = _dict_book(["0.001", "0.50", "0.985"], ["0.999", "0.99", "0.986"])
    snap = client._parse_order_book("tok", raw)
    assert snap is not None
    assert snap.best_bid == Decimal("0.985")
    assert snap.best_ask == Decimal("0.986")
    assert snap.spread == Decimal("0.001")


def test_midpoint_computed_from_best_of_book(client):
    raw = _dict_book(["0.001", "0.40", "0.44"], ["0.999", "0.50", "0.46"])
    snap = client._parse_order_book("tok", raw)
    assert snap is not None
    # midpoint = (0.44 + 0.46) / 2 = 0.45
    assert snap.midpoint_probability == Decimal("0.45")


def test_dataclass_entries_select_best_of_book(client):
    raw = _dataclass_book(["0.001", "0.30", "0.61"], ["0.999", "0.80", "0.63"])
    snap = client._parse_order_book("tok", raw)
    assert snap is not None
    assert snap.best_bid == Decimal("0.61")
    assert snap.best_ask == Decimal("0.63")


def test_dict_and_dataclass_entries_agree(client):
    dict_snap = client._parse_order_book(
        "tok", _dict_book(["0.001", "0.61"], ["0.999", "0.63"])
    )
    dc_snap = client._parse_order_book(
        "tok", _dataclass_book(["0.001", "0.61"], ["0.999", "0.63"])
    )
    assert dict_snap is not None and dc_snap is not None
    assert dict_snap.best_bid == dc_snap.best_bid == Decimal("0.61")
    assert dict_snap.best_ask == dc_snap.best_ask == Decimal("0.63")


# ---------------------------------------------------------------------------
# Non-positive level filtering
# ---------------------------------------------------------------------------


def test_non_positive_bid_levels_excluded(client):
    # A leading non-positive bid must not be selected; best positive bid wins.
    raw = _dict_book(["-1", "0", "0.61"], ["0.999", "0.63"])
    snap = client._parse_order_book("tok", raw)
    assert snap is not None
    assert snap.best_bid == Decimal("0.61")


def test_non_positive_ask_levels_excluded(client):
    raw = _dict_book(["0.001", "0.61"], ["0", "0.63", "-2"])
    snap = client._parse_order_book("tok", raw)
    assert snap is not None
    assert snap.best_ask == Decimal("0.63")


def test_no_positive_bid_returns_none(client):
    raw = _dict_book(["-1", "0"], ["0.63", "0.80"])
    assert client._parse_order_book("tok", raw) is None


def test_no_positive_ask_returns_none(client):
    raw = _dict_book(["0.50", "0.61"], ["0", "-2"])
    assert client._parse_order_book("tok", raw) is None


# ---------------------------------------------------------------------------
# Preserved fail-closed behavior (regression guards)
# ---------------------------------------------------------------------------


def test_single_level_book(client):
    raw = _dict_book(["0.42"], ["0.43"])
    snap = client._parse_order_book("tok", raw)
    assert snap is not None
    assert snap.best_bid == Decimal("0.42")
    assert snap.best_ask == Decimal("0.43")


def test_empty_bids_returns_none(client):
    raw = {"bids": [], "asks": [{"price": "0.63", "size": "10"}]}
    assert client._parse_order_book("tok", raw) is None


def test_empty_asks_returns_none(client):
    raw = {"bids": [{"price": "0.61", "size": "10"}], "asks": []}
    assert client._parse_order_book("tok", raw) is None


def test_crossed_book_returns_none(client):
    # After correct selection best_bid 0.70 > best_ask 0.60 -> crossed -> None.
    raw = _dict_book(["0.50", "0.70"], ["0.60", "0.99"])
    assert client._parse_order_book("tok", raw) is None


def test_all_prices_are_decimal(client):
    raw = _dict_book(["0.001", "0.61"], ["0.999", "0.63"])
    snap = client._parse_order_book("tok", raw)
    assert snap is not None
    assert isinstance(snap.best_bid, Decimal)
    assert isinstance(snap.best_ask, Decimal)
    assert isinstance(snap.midpoint_probability, Decimal)
    assert isinstance(snap.spread, Decimal)


def test_snapshot_source_unchanged(client):
    raw = _dict_book(["0.001", "0.61"], ["0.999", "0.63"])
    snap = client._parse_order_book("tok", raw)
    assert snap is not None
    assert isinstance(snap, MarketSnapshot)
    assert snap.source == "clob_orderbook"
