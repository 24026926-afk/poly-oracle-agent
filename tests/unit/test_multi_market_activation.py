"""
Unit tests for multi-market activation, subscription, and routing.

Covers:
- Orchestrator activates multiple markets at startup (not just eligible[0])
- WS client subscribes to all token IDs from all active markets
- Token→condition routing map covers every subscribed token
- DataAggregator accepts messages from any active market
- last_trade_price frames without book data are skipped
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.context.aggregator import DataAggregator
from src.agents.ingestion.ws_client import CLOBWebSocketClient
from src.core.config import AppConfig
from src.orchestrator import Orchestrator
from src.schemas.market import MarketMetadata


def _patch_heavy_deps() -> dict:
    mock_w3 = MagicMock()
    mock_w3.eth = MagicMock()
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=0)
    return {
        "AsyncWeb3": MagicMock(return_value=mock_w3),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }


def _market(**overrides: object) -> MarketMetadata:
    data = {
        "conditionId": "0xdefault000000000000000000000000000000000001",
        "question": "Test market?",
        "category": "Crypto",
        "tags": ["test"],
        "clobTokenIds": ["yes-token", "no-token"],
        "endDateIso": "2026-12-31T00:00:00+00:00",
    }
    data.update(overrides)
    return MarketMetadata.model_validate(data)


# ---------------------------------------------------------------------------
# Multi-market activation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_activate_markets_multiple(test_config):
    """_activate_markets registers all token IDs and builds routing map."""
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    m1 = _market(
        conditionId="0xmarket001",
        clobTokenIds=["yes-1", "no-1"],
    )
    m2 = _market(
        conditionId="0xmarket002",
        clobTokenIds=["yes-2", "no-2"],
    )
    m3 = _market(
        conditionId="0xmarket003",
        clobTokenIds=["yes-3", "no-3"],
    )

    await orch._activate_markets([m1, m2, m3])

    assert len(orch.active_markets) == 3
    assert orch.active_condition_id == "0xmarket001"

    # All 6 token IDs should be subscribed
    assert set(orch.ws_client._assets_ids) == {
        "yes-1", "no-1", "yes-2", "no-2", "yes-3", "no-3",
    }

    # ws_client._token_id_mapping maps tokens → yes_token_id (for snapshot enrichment)
    assert orch.ws_client._token_id_mapping["yes-1"] == "yes-1"
    assert orch.ws_client._token_id_mapping["no-1"] == "yes-1"
    assert orch.ws_client._token_id_mapping["yes-2"] == "yes-2"
    assert orch.ws_client._token_id_mapping["no-2"] == "yes-2"
    assert orch.ws_client._token_id_mapping["yes-3"] == "yes-3"
    assert orch.ws_client._token_id_mapping["no-3"] == "yes-3"

    # orchestrator._condition_by_token maps tokens → condition_id (for routing)
    cbt = orch._condition_by_token
    assert cbt["yes-1"] == "0xmarket001"
    assert cbt["no-1"] == "0xmarket001"
    assert cbt["yes-2"] == "0xmarket002"
    assert cbt["no-2"] == "0xmarket002"
    assert cbt["yes-3"] == "0xmarket003"
    assert cbt["no-3"] == "0xmarket003"
    assert cbt["0xmarket001"] == "0xmarket001"
    assert cbt["0xmarket002"] == "0xmarket002"
    assert cbt["0xmarket003"] == "0xmarket003"

    # Unique conditions should be 3
    assert len(set(cbt.values())) == 3


@pytest.mark.asyncio
async def test_activate_markets_empty(test_config):
    """_activate_markets with empty list should be a no-op."""
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    await orch._activate_markets([])
    assert len(orch.active_markets) == 0


@pytest.mark.asyncio
async def test_activate_market_legacy_wrapper(test_config):
    """_activate_market (single) should wrap _activate_markets correctly."""
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(test_config)

    m = _market(conditionId="0xsingle001", clobTokenIds=["yes-s", "no-s"])
    await orch._activate_market(m)

    assert len(orch.active_markets) == 1
    assert orch.active_condition_id == "0xsingle001"
    assert set(orch.ws_client._assets_ids) == {"yes-s", "no-s"}


# ---------------------------------------------------------------------------
# DataAggregator multi-market routing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregator_accepts_any_active_market(test_config):
    """DataAggregator should accept messages from any active condition_id.

    WI-32: Per-market state isolation — market002 frame updates market002's
    state, not the primary market's.  The global best_bid property delegates
    to the primary market, so we check _markets directly.
    """
    agg = DataAggregator(
        input_queue=asyncio.Queue(),
        output_queue=asyncio.Queue(),
        condition_id="0xmarket001",
        condition_by_token={
            "yes-1": "0xmarket001",
            "no-1": "0xmarket001",
            "yes-2": "0xmarket002",
            "no-2": "0xmarket002",
            "0xmarket001": "0xmarket001",
            "0xmarket002": "0xmarket002",
        },
    )

    # Message from market002 should be accepted and update market002's state
    msg = MagicMock()
    msg.condition_id = "0xmarket002"
    msg.yes_token_id = None
    msg.best_bid = 0.40
    msg.best_ask = 0.60

    await agg._process_message(msg)
    # Per-market state: market002 has the new values
    assert agg._markets["0xmarket002"].best_bid == 0.40
    assert agg._markets["0xmarket002"].best_ask == 0.60
    # Primary market (0xmarket001) is unchanged
    assert agg._markets.get("0xmarket001") is None or agg._markets["0xmarket001"].best_bid == 0.0


@pytest.mark.asyncio
async def test_aggregator_rejects_unknown_market(test_config):
    """DataAggregator should reject messages from non-active condition_id."""
    agg = DataAggregator(
        input_queue=asyncio.Queue(),
        output_queue=asyncio.Queue(),
        condition_id="0xmarket001",
        condition_by_token={
            "yes-1": "0xmarket001",
            "0xmarket001": "0xmarket001",
        },
    )
    agg.best_bid = 0.40
    agg.best_ask = 0.60

    msg = MagicMock()
    msg.condition_id = "0xunknown999"
    msg.yes_token_id = None
    msg.best_bid = 0.99
    msg.best_ask = 1.01

    await agg._process_message(msg)
    # Should NOT have been updated
    assert agg.best_bid == 0.40
    assert agg.best_ask == 0.60


@pytest.mark.asyncio
async def test_aggregator_fallback_to_primary_when_no_map(test_config):
    """Without condition_by_token, aggregator falls back to primary condition_id."""
    agg = DataAggregator(
        input_queue=asyncio.Queue(),
        output_queue=asyncio.Queue(),
        condition_id="0xprimary",
    )
    agg.best_bid = 0.30
    agg.best_ask = 0.70

    # Same condition → accepted
    msg1 = MagicMock()
    msg1.condition_id = "0xprimary"
    msg1.yes_token_id = None
    msg1.best_bid = 0.45
    msg1.best_ask = 0.55

    await agg._process_message(msg1)
    assert agg.best_bid == 0.45

    # Different condition → rejected
    msg2 = MagicMock()
    msg2.condition_id = "0xother"
    msg2.yes_token_id = None
    msg2.best_bid = 0.99
    msg2.best_ask = 1.01

    await agg._process_message(msg2)
    assert agg.best_bid == 0.45  # unchanged


# ---------------------------------------------------------------------------
# WS client last_trade_price guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_client_drops_stale_asset_id_without_condition_id(
    test_config, db_session_factory
):
    """Frames with stale asset_id and no condition_id must be dropped.

    This is the gap identified in the MAAP review: when _condition_by_token
    is populated but the frame has asset_id only (no market/condition_id),
    the stale asset_id must still be rejected.
    """
    queue: asyncio.Queue = asyncio.Queue()
    client = CLOBWebSocketClient(
        config=test_config,
        queue=queue,
        db_session_factory=db_session_factory,
        token_id_to_yes_token_id={"tok-yes": "tok-yes"},
    )
    # Set up condition_by_token with only active tokens
    client.set_condition_by_token({
        "tok-yes": "0xactive",
        "tok-no": "0xactive",
        "0xactive": "0xactive",
    })

    # Frame with stale asset_id and no condition_id
    frame = {
        "event_type": "book",
        "asset_id": "tok-stale",  # not in active set
        "bids": [{"price": "0.45"}],
        "asks": [{"price": "0.55"}],
    }

    await client._process_event(frame, json.dumps(frame))
    assert queue.empty(), "Stale asset_id frame should be dropped"


@pytest.mark.asyncio
async def test_ws_client_skips_last_trade_without_book(test_config, db_session_factory):
    """last_trade_price frames with no bid/ask should be skipped."""
    queue: asyncio.Queue = asyncio.Queue()
    client = CLOBWebSocketClient(
        config=test_config,
        queue=queue,
        db_session_factory=db_session_factory,
        token_id_to_yes_token_id={"tok-yes": "tok-yes"},
    )

    # last_trade_price with no book data
    frame = {
        "event_type": "last_trade_price",
        "market": "0xtest123",
        "asset_id": "tok-yes",
        "price": 0.55,
    }

    await client._process_event(frame, json.dumps(frame))
    # Queue should be empty — snapshot was skipped
    assert queue.empty()


@pytest.mark.asyncio
async def test_ws_client_processes_book_frame(test_config, db_session_factory):
    """book frames with bids/asks should produce a snapshot."""
    queue: asyncio.Queue = asyncio.Queue()
    client = CLOBWebSocketClient(
        config=test_config,
        queue=queue,
        db_session_factory=db_session_factory,
        token_id_to_yes_token_id={"tok-yes": "tok-yes"},
    )

    frame = {
        "event_type": "book",
        "market": "0xtest123",
        "asset_id": "tok-yes",
        "bids": [{"price": "0.45"}],
        "asks": [{"price": "0.55"}],
    }

    await client._process_event(frame, json.dumps(frame))
    # Queue should have one snapshot
    assert queue.qsize() == 1
    snapshot = queue.get_nowait()
    assert snapshot.condition_id == "0xtest123"
    assert snapshot.best_bid == 0.45
    assert snapshot.best_ask == 0.55


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aggregator_emits_correct_market_context(test_config):
    """Market B frames must emit market B's condition_id/question/category.

    This is the critical cross-market contamination test.  Before WI-32 fix,
    a frame from market B would update global best_bid/best_ask but emit
    with market A's condition_id, question, and category.
    """
    out_queue: asyncio.Queue = asyncio.Queue()
    agg = DataAggregator(
        input_queue=asyncio.Queue(),
        output_queue=out_queue,
        condition_id="0xmarketA",
        condition_by_token={
            "yes-A": "0xmarketA",
            "no-A": "0xmarketA",
            "yes-B": "0xmarketB",
            "no-B": "0xmarketB",
            "0xmarketA": "0xmarketA",
            "0xmarketB": "0xmarketB",
        },
    )

    # Register both markets with distinct metadata
    agg.register_market(
        condition_id="0xmarketA",
        question="Will A happen?",
        category="CRYPTO",
        tags=["a"],
        yes_token_id="yes-A",
    )
    agg.register_market(
        condition_id="0xmarketB",
        question="Will B happen?",
        category="SPORTS",
        tags=["b"],
        yes_token_id="yes-B",
    )

    # Send a frame from market B
    msg_b = MagicMock()
    msg_b.condition_id = "0xmarketB"
    msg_b.yes_token_id = "yes-B"
    msg_b.best_bid = 0.30
    msg_b.best_ask = 0.70

    await agg._process_message(msg_b)

    # The emitted payload must have market B's context
    assert out_queue.qsize() == 1, "Expected one emit from market B trigger"
    payload = out_queue.get_nowait()
    state = payload["state"]

    assert state["condition_id"] == "0xmarketB", (
        f"Expected market B condition_id, got {state['condition_id']}"
    )
    assert state["question"] == "Will B happen?", (
        f"Expected market B question, got {state['question']}"
    )
    assert state["category"] == "SPORTS", (
        f"Expected market B category, got {state['category']}"
    )
    assert state["tags"] == ["b"]
    assert state["best_bid"] == 0.30
    assert state["best_ask"] == 0.70
    assert payload["yes_token_id"] == "yes-B"


def test_config_defaults_support_multi_market():
    """Config defaults should enable market tracking with reasonable concurrency."""
    # Use _env_file=None to avoid .env file loading in tests
    import os
    for var in [
        "ANTHROPIC_API_KEY", "POLYGON_RPC_URL", "WALLET_ADDRESS",
        "WALLET_PRIVATE_KEY", "GROK_API_KEY",
    ]:
        os.environ.pop(var, None)

    cfg = AppConfig(
        _env_file=None,
        anthropic_api_key="sk-ant-test",
        polygon_rpc_url="http://localhost:8545",
        wallet_address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        wallet_private_key="0x" + "a1" * 32,
        grok_api_key="grok-test",
    )
    assert cfg.max_concurrent_markets >= 10
    assert cfg.enable_market_tracking is True
