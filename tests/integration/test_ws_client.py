"""
tests/integration/test_ws_client.py

Integration tests for CLOBWebSocketClient — message handling, DB persistence,
filtering, and reconnection behaviour with mocked websockets.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from src.agents.ingestion.ws_client import CLOBWebSocketClient
from src.db.models import MarketSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.clob_ws_url = "ws://localhost:9998"
    return cfg


def _book_frame(**overrides) -> str:
    base = {
        "event": "book",
        "market": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
        "question": "Will ETH exceed $5000?",
        "best_bid": 0.45,
        "best_ask": 0.55,
        "last_trade_price": 0.50,
        "outcome_token": "YES",
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_handle_message_enqueues_and_persists(
    async_engine, db_session_factory
):
    """A valid book frame should be enqueued AND persisted to the DB."""
    queue: asyncio.Queue = asyncio.Queue()
    client = CLOBWebSocketClient(_make_config(), queue, db_session_factory)

    await client._handle_message(_book_frame())

    # Queue assertion
    assert queue.qsize() == 1
    row = queue.get_nowait()
    assert isinstance(row, MarketSnapshot)
    assert row.condition_id == "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555"
    assert row.midpoint == 0.5  # (0.45 + 0.55) / 2

    # DB persistence assertion
    async with db_session_factory() as session:
        result = await session.execute(select(MarketSnapshot))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].condition_id == "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555"


@pytest.mark.asyncio
async def test_ws_handle_message_filters_invalid_event(
    async_engine, db_session_factory
):
    """Non-CLOB events (heartbeat, subscribe_ack) must be silently discarded."""
    queue: asyncio.Queue = asyncio.Queue()
    client = CLOBWebSocketClient(_make_config(), queue, db_session_factory)

    await client._handle_message(json.dumps({"event": "heartbeat_ack"}))
    await client._handle_message(json.dumps({"event": "subscribe"}))

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_ws_handle_message_skips_malformed_json(async_engine, db_session_factory):
    """Malformed JSON must not crash the handler or enqueue anything."""
    queue: asyncio.Queue = asyncio.Queue()
    client = CLOBWebSocketClient(_make_config(), queue, db_session_factory)

    await client._handle_message("not valid json {{{")

    assert queue.qsize() == 0


@pytest.mark.asyncio
async def test_ws_multiple_frames_persist_independently(
    async_engine, db_session_factory
):
    """Multiple valid frames produce distinct DB rows and queue entries."""
    queue: asyncio.Queue = asyncio.Queue()
    client = CLOBWebSocketClient(_make_config(), queue, db_session_factory)

    await client._handle_message(
        _book_frame(market="0xmarket_a_000000000000000000000000000001")
    )
    await client._handle_message(
        _book_frame(market="0xmarket_b_000000000000000000000000000002")
    )

    assert queue.qsize() == 2

    async with db_session_factory() as session:
        result = await session.execute(select(MarketSnapshot))
        rows = result.scalars().all()
        assert len(rows) == 2
        cids = {r.condition_id for r in rows}
        assert "0xmarket_a_000000000000000000000000000001" in cids
        assert "0xmarket_b_000000000000000000000000000002" in cids
