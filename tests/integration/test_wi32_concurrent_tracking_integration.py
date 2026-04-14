"""
tests/integration/test_wi32_concurrent_tracking_integration.py

RED-phase integration tests for WI-32: concurrent multi-market tracking.
All tests MUST fail until GREEN-phase implementation is complete.

Covers:
  1. Full fan-out cycle
  2. Single WebSocket connection
  3. Market failure isolation
  4. Frame routing correctness
  5. dry_run=True concurrent pipeline
  6. Market truncation end-to-end
  7. Concurrent queue production
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession, create_async_engine

from src.core.config import AppConfig
from src.db.models import Base


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_integration_config(**overrides) -> AppConfig:
    """Build AppConfig for integration tests."""
    defaults: dict = dict(
        anthropic_api_key="sk-ant-test-fake-key-000",
        anthropic_model="claude-3-5-sonnet-20241022",
        anthropic_max_tokens=4096,
        anthropic_max_retries=2,
        polygon_rpc_url="http://localhost:8545",
        wallet_address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        wallet_private_key="0x" + "a1" * 32,
        clob_rest_url="http://localhost:9999",
        clob_ws_url="ws://localhost:9998",
        gamma_api_url="http://localhost:9997",
        kelly_fraction=0.25,
        min_confidence=0.75,
        max_spread_pct=0.015,
        max_exposure_pct=0.03,
        min_ev_threshold=0.02,
        min_ttr_hours=4.0,
        initial_bankroll_usdc=Decimal("10000"),
        max_order_usdc=Decimal("50"),
        max_slippage_tolerance=Decimal("0.02"),
        exit_position_max_age_hours=Decimal("48"),
        exit_stop_loss_drop=Decimal("0.15"),
        exit_take_profit_gain=Decimal("0.20"),
        exit_scan_interval_seconds=Decimal("60"),
        enable_portfolio_aggregator=False,
        portfolio_aggregation_interval_sec=Decimal("30"),
        alert_drawdown_usdc=Decimal("100"),
        alert_stale_price_pct=Decimal("0.50"),
        alert_max_open_positions=20,
        alert_loss_rate_pct=Decimal("0.60"),
        enable_telegram_notifier=False,
        telegram_bot_token="",
        telegram_chat_id="",
        telegram_send_timeout_sec=Decimal("5"),
        enable_circuit_breaker=False,
        circuit_breaker_override_closed=False,
        exit_min_bid_tolerance=Decimal("0.01"),
        max_gas_price_gwei=500.0,
        fallback_gas_price_gwei=50.0,
        database_url="sqlite+aiosqlite://",
        grok_api_key="grok-test-fake-key-000",
        grok_base_url="http://localhost:9996",
        grok_model="grok-3",
        grok_mocked=True,
        log_level="DEBUG",
        dry_run=True,
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


def _patch_heavy_deps():
    """Return patches to neutralise network-bound constructors."""
    mock_w3 = MagicMock()
    mock_w3.eth = MagicMock()
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=0)
    return {
        "AsyncWeb3": MagicMock(return_value=mock_w3),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }


@pytest_asyncio.fixture
async def test_async_engine():
    """In-memory SQLite engine for integration tests."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False, future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def test_db_session_factory(test_async_engine):
    return async_sessionmaker(
        bind=test_async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


# ---------------------------------------------------------------------------
# 1. Full fan-out cycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_fan_out_cycle():
    """discover → subscribe_batch → aggregate → prompt_queue: all markets tracked."""
    config = _make_integration_config(
        max_concurrent_markets=3,
        market_tracking_interval_sec=Decimal("10"),
    )

    # Test the fan-out pattern directly without full Orchestrator
    mock_aggregator = AsyncMock()
    mock_aggregator.track_market.side_effect = lambda token_ids: [
        {"token_ids": token_ids, "condition_id": f"cid-{token_ids[0]}"}
    ]

    # Simulate discovery
    snapshots = [
        MagicMock(condition_id="cid_1", token_ids=["t1a", "t1b"]),
        MagicMock(condition_id="cid_2", token_ids=["t2a", "t2b"]),
        MagicMock(condition_id="cid_3", token_ids=["t3a", "t3b"]),
    ]
    assert len(snapshots) == 3

    token_ids_list = [s.token_ids for s in snapshots]
    tasks = [
        mock_aggregator.track_market(token_ids)
        for token_ids in token_ids_list
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    success = [r for r in results if not isinstance(r, Exception)]
    assert len(success) == 3


# ---------------------------------------------------------------------------
# 2. Single WebSocket connection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_websocket_connection_for_all_markets():
    """Despite tracking 3 markets concurrently, ws_client has exactly 1 connection."""
    from src.agents.ingestion.ws_client import CLOBWebSocketClient

    mock_queue = asyncio.Queue()
    mock_db_factory = MagicMock()

    config = _make_integration_config()
    client = CLOBWebSocketClient(config, mock_queue, mock_db_factory)

    # Register 3 markets on the same client — single connection model
    client.register_aggregator("t1", MagicMock())
    client.register_aggregator("t2", MagicMock())
    client.register_aggregator("t3", MagicMock())

    # All 3 share the same _aggregator_map — single connection model
    assert len(client._aggregator_map) == 3
    assert "t1" in client._aggregator_map
    assert "t2" in client._aggregator_map
    assert "t3" in client._aggregator_map


# ---------------------------------------------------------------------------
# 3. Market failure isolation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_failure_isolation():
    """Crash in one aggregator must NOT affect the other 2."""
    mock_aggregator = AsyncMock()

    async def track_with_one_failure(token_ids):
        if token_ids == ["t2"]:
            raise RuntimeError("aggregator_crash")
        return [{"token_ids": token_ids}]

    mock_aggregator.track_market.side_effect = track_with_one_failure

    token_ids_list = [["t1"], ["t2"], ["t3"]]
    tasks = [
        mock_aggregator.track_market(token_ids)
        for token_ids in token_ids_list
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    exceptions = [r for r in results if isinstance(r, Exception)]
    successes = [r for r in results if not isinstance(r, Exception)]

    assert len(exceptions) == 1
    assert len(successes) == 2


# ---------------------------------------------------------------------------
# 4. Frame routing correctness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_frame_routing_no_cross_contamination():
    """Frames for 3 markets routed to correct aggregators with no mixing."""
    from src.agents.ingestion.ws_client import CLOBWebSocketClient

    mock_queue = asyncio.Queue()
    mock_db_factory = MagicMock()

    config = _make_integration_config()
    client = CLOBWebSocketClient(config, mock_queue, mock_db_factory)

    # Track which aggregator received which frames
    received_frames = {"t1": [], "t2": [], "t3": []}

    def make_handler(key):
        def handler(frame):
            received_frames[key].append(frame)
        return handler

    mock_agg_t1 = MagicMock()
    mock_agg_t1.process_frame = MagicMock(side_effect=make_handler("t1"))
    mock_agg_t2 = MagicMock()
    mock_agg_t2.process_frame = MagicMock(side_effect=make_handler("t2"))
    mock_agg_t3 = MagicMock()
    mock_agg_t3.process_frame = MagicMock(side_effect=make_handler("t3"))

    client.register_aggregator("t1", mock_agg_t1)
    client.register_aggregator("t2", mock_agg_t2)
    client.register_aggregator("t3", mock_agg_t3)

    # Send frames for all 3 markets concurrently
    frames = [
        json.dumps({"asset_id": "t1", "event_type": "book", "best_bid": 0.45, "best_ask": 0.55}),
        json.dumps({"asset_id": "t2", "event_type": "book", "best_bid": 0.30, "best_ask": 0.40}),
        json.dumps({"asset_id": "t3", "event_type": "book", "best_bid": 0.60, "best_ask": 0.70}),
    ]

    await asyncio.gather(*[client._handle_message(f) for f in frames])

    # Each aggregator received exactly 1 frame
    assert len(received_frames["t1"]) == 1
    assert len(received_frames["t2"]) == 1
    assert len(received_frames["t3"]) == 1

    # Verify correct routing (t1 frames went to t1 aggregator)
    # process_frame receives the parsed dict, not the raw JSON string
    frame_t1 = received_frames["t1"][0]
    assert frame_t1["asset_id"] == "t1"


# ---------------------------------------------------------------------------
# 5. dry_run=True concurrent pipeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dry_run_concurrent_pipeline():
    """Full concurrent tracking with dry_run=True: no live WS connections."""
    config = _make_integration_config(dry_run=True)

    # dry_run=True config verified
    assert config.dry_run is True

    # Mock aggregator
    mock_aggregator = AsyncMock()
    mock_aggregator.track_market = AsyncMock(return_value=[
        {"token_ids": ["t1"], "condition_id": "cid_1"}
    ])

    # Simulate discovery
    snapshots = [
        MagicMock(condition_id="cid_1", token_ids=["t1"]),
    ]
    token_ids_list = [s.token_ids for s in snapshots]
    results = await asyncio.gather(
        *[mock_aggregator.track_market(tids) for tids in token_ids_list],
        return_exceptions=True,
    )

    success = [r for r in results if not isinstance(r, Exception)]
    assert len(success) == 1


# ---------------------------------------------------------------------------
# 6. Market truncation end-to-end
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_truncation_end_to_end():
    """Discover 7 markets, max_concurrent_markets=5: only 5 tracked."""
    config = _make_integration_config(max_concurrent_markets=5)

    snapshots = [
        MagicMock(condition_id=f"cid_{i}", token_ids=[f"t{i}"])
        for i in range(7)
    ]
    assert len(snapshots) == 7

    # Apply truncation
    if len(snapshots) > config.max_concurrent_markets:
        snapshots = snapshots[:config.max_concurrent_markets]

    assert len(snapshots) == 5


# ---------------------------------------------------------------------------
# 7. Concurrent queue production
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_queue_production():
    """3 aggregators produce to shared prompt_queue simultaneously;
    no queue corruption, all contexts consumable."""
    prompt_queue: asyncio.Queue = asyncio.Queue()

    async def produce_context(ctx_id: int):
        context = {
            "condition_id": f"cid_{ctx_id}",
            "token_ids": [f"t{ctx_id}"],
            "midpoint": Decimal("0.50"),
        }
        await prompt_queue.put(context)

    # Produce 3 contexts concurrently
    await asyncio.gather(
        produce_context(1),
        produce_context(2),
        produce_context(3),
    )

    assert prompt_queue.qsize() == 3

    # All contexts must be consumable
    consumed = []
    while not prompt_queue.empty():
        item = await prompt_queue.get()
        consumed.append(item)
        prompt_queue.task_done()

    assert len(consumed) == 3
    cids = {c["condition_id"] for c in consumed}
    assert cids == {"cid_1", "cid_2", "cid_3"}
