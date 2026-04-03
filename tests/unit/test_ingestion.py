"""
tests/unit/test_ingestion.py

Async unit tests for Module 1 — Market Ingestion Engine.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.ingestion.rest_client import GammaRESTClient
from src.agents.ingestion.ws_client import CLOBWebSocketClient
from src.schemas.market import MarketMetadata, MarketSnapshotSchema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _book_frame(**overrides: object) -> str:
    """Return a valid CLOB book frame as raw JSON."""
    base = {
        "event": "book",
        "market": "0xcondition123",
        "best_bid": 0.45,
        "best_ask": 0.55,
        "last_trade_price": 0.50,
        "outcome_token": "YES",
        "question": "Will it rain?",
    }
    base.update(overrides)
    return json.dumps(base)


def _mock_db_factory() -> MagicMock:
    session = MagicMock()
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.commit = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=session)
    factory._last_session = session
    return factory


def _mock_config() -> MagicMock:
    cfg = MagicMock()
    cfg.clob_ws_url = "wss://fake.ws/market"
    cfg.gamma_api_url = "https://gamma-api.fake.com"
    return cfg


class _FakeResponse:
    """Minimal httpx response mock for REST client tests."""

    def __init__(self, status_code: int, body: object) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        return self._body

    @property
    def text(self) -> str:
        return json.dumps(self._body)


# ---------------------------------------------------------------------------
# WebSocket Client Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ws_message_valid_book_frame_enqueues_snapshot():
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    await client._handle_message(_book_frame())

    assert queue.qsize() == 1
    row = queue.get_nowait()
    assert row.condition_id == "0xcondition123"
    assert row.midpoint == 0.5


@pytest.mark.asyncio
async def test_ws_unknown_message_type_is_ignored():
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    await client._handle_message(json.dumps({"event": "heartbeat_ack"}))

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_ws_invalid_json_does_not_crash():
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    # Should not raise
    await client._handle_message("this is not json {{{")

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_ws_validation_error_skips_frame():
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    # best_bid = 5.0 exceeds le=1.0 constraint → ValidationError
    bad_frame = _book_frame(best_bid=5.0)
    await client._handle_message(bad_frame)

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_ws_midpoint_computed_not_trusted():
    """Midpoint must be (bid + ask) / 2, never the externally-provided value."""
    schema = MarketSnapshotSchema(
        condition_id="abc",
        best_bid=0.40,
        best_ask=0.60,
        midpoint=0.99,  # garbage — must be overwritten
        raw_ws_payload="{}",
    )
    assert schema.midpoint == 0.5


# ---------------------------------------------------------------------------
# Gamma REST Client Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gamma_get_active_markets_returns_list():
    body = [
        {
            "conditionId": "cond1",
            "question": "Q1",
            "clobTokenIds": ["t1", "t2"],
            "active": True,
            "closed": False,
        }
    ]
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)
    result = await client.get_active_markets()

    assert len(result) == 1
    assert result[0].condition_id == "cond1"


@pytest.mark.asyncio
async def test_gamma_cache_returns_stale_within_60s():
    body = [
        {
            "conditionId": "cond1",
            "question": "Q1",
            "clobTokenIds": [],
            "active": True,
            "closed": False,
        }
    ]
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)

    first = await client.get_active_markets()
    # Second call should use cache — http.get should be called only once
    second = await client.get_active_markets()

    assert first == second
    # httpx coroutine: if cache works, .get is awaited only once.
    assert http.get.call_count == 1


@pytest.mark.asyncio
async def test_gamma_404_returns_none():
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(404, {}))

    client = GammaRESTClient(_mock_config(), http)
    result = await client.get_market_by_condition_id("nonexistent")

    assert result is None


# ---------------------------------------------------------------------------
# Gamma query parameter validation — ensures robust market discovery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gamma_query_includes_limit_and_volume_sort():
    """The active-markets URL must request a capped, volume-sorted page
    so the Gamma API returns the most liquid markets (not an empty page)."""
    body = [
        {
            "conditionId": "cond1",
            "question": "Q1",
            "clobTokenIds": ["t1"],
            "active": True,
            "closed": False,
            "volume24hr": 50000.0,
        }
    ]
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)
    await client.get_active_markets()

    called_url: str = http.get.call_args[0][0]

    # Must include pagination limit
    assert "limit=" in called_url, "query must include a limit parameter"
    # Must sort by 24h volume descending for liquidity
    assert "order=volume24hr" in called_url, "query must sort by volume24hr"
    assert "ascending=false" in called_url, "query must use descending order"
    # Must still filter for active, non-closed
    assert "active=true" in called_url
    assert "closed=false" in called_url


@pytest.mark.asyncio
async def test_gamma_query_has_no_restrictive_tag_or_category_filters():
    """The active-markets URL must NOT contain tag= or category= params
    that could exclude valid high-volume markets."""
    body = []
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)
    await client.get_active_markets()

    called_url: str = http.get.call_args[0][0]

    assert "tag=" not in called_url, "must not filter by tag"
    assert "category=" not in called_url, "must not filter by category"


# ---------------------------------------------------------------------------
# WebSocket subscription format
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ws_subscription_uses_assets_ids_and_logs_message():
    """The subscription message must use 'assets_ids' (token IDs),
    not 'market_ids', and the ws_client must accept token IDs at init."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    token_ids = ["tok_yes_123", "tok_no_456"]
    client = CLOBWebSocketClient(_mock_config(), queue, db, assets_ids=token_ids)

    # Build the subscription message the client would send
    sub_msg = client._build_subscription_message()
    parsed = json.loads(sub_msg)

    assert parsed["type"] == "subscribe"
    assert "assets_ids" in parsed, "must use 'assets_ids', not 'market_ids'"
    assert parsed["assets_ids"] == token_ids
    assert "market_ids" not in parsed, "must NOT contain 'market_ids'"


@pytest.mark.asyncio
async def test_ws_handles_non_json_server_error_gracefully():
    """Non-JSON server responses like 'INVALID OPERATION' must be logged
    as server errors, not generic invalid_json."""
    queue: asyncio.Queue = asyncio.Queue()
    db = _mock_db_factory()
    client = CLOBWebSocketClient(_mock_config(), queue, db)

    with patch("src.agents.ingestion.ws_client.logger") as mock_logger:
        await client._handle_message("INVALID OPERATION")

        # Should log as a server error, not just invalid_json
        mock_logger.warning.assert_called_once()
        call_args = mock_logger.warning.call_args
        assert call_args[0][0] == "ws_client.server_error"


# ---------------------------------------------------------------------------
# Gamma API real response shape — clobTokenIds is a JSON-encoded string
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gamma_parses_stringified_clob_token_ids():
    """The real Gamma API returns clobTokenIds as a JSON-encoded STRING,
    e.g. '["tok1", "tok2"]', NOT a native list. Markets must still parse."""
    body = [
        {
            "conditionId": "0xabc123",
            "question": "Will X happen?",
            "clobTokenIds": '["tok_yes", "tok_no"]',  # STRING, not list
            "endDateIso": "2026-06-01",
            "active": True,
            "closed": False,
            "volume24hr": 1000000.0,
        }
    ]
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)
    result = await client.get_active_markets()

    assert len(result) == 1, "stringified clobTokenIds must not silently drop markets"
    assert result[0].condition_id == "0xabc123"
    assert result[0].token_ids == ["tok_yes", "tok_no"]


@pytest.mark.asyncio
async def test_gamma_parses_native_list_clob_token_ids():
    """Backwards compat: if clobTokenIds is already a native list, still works."""
    body = [
        {
            "conditionId": "0xdef456",
            "question": "Will Y happen?",
            "clobTokenIds": ["tok_a", "tok_b"],  # native list
            "endDateIso": "2026-06-01",
            "active": True,
            "closed": False,
            "volume24hr": 500000.0,
        }
    ]
    http = MagicMock()
    http.get = AsyncMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)
    result = await client.get_active_markets()

    assert len(result) == 1
    assert result[0].token_ids == ["tok_a", "tok_b"]
