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
    """Minimal aiohttp response for REST client tests."""

    def __init__(self, status: int, body: object) -> None:
        self.status = status
        self._body = body

    async def json(self) -> object:
        return self._body

    async def text(self) -> str:
        return json.dumps(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass


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
    http.get = MagicMock(return_value=_FakeResponse(200, body))

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
    http.get = MagicMock(return_value=_FakeResponse(200, body))

    client = GammaRESTClient(_mock_config(), http)

    first = await client.get_active_markets()
    # Second call should use cache — http.get should be called only once
    second = await client.get_active_markets()

    assert first == second
    # aiohttp context manager: the .get mock returns _FakeResponse which is
    # used as a context manager once. If cache works, .get is called once.
    assert http.get.call_count == 1


@pytest.mark.asyncio
async def test_gamma_404_returns_none():
    http = MagicMock()
    http.get = MagicMock(return_value=_FakeResponse(404, {}))

    client = GammaRESTClient(_mock_config(), http)
    result = await client.get_market_by_condition_id("nonexistent")

    assert result is None
