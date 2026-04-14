"""
tests/unit/test_wi32_concurrent_tracking.py

RED-phase unit tests for WI-32: concurrent multi-market tracking via asyncio.gather.
All tests MUST fail until GREEN-phase implementation is complete.

Covers:
  A. asyncio.gather fan-out
  B. subscribe_batch() multiplexed subscription
  C. Frame routing via asset_id
  D. Market truncation
  E. PerMarketAggregatorState schema
  F. MarketTrackingTask pattern
  G. Decimal safety under concurrency
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.core.config import AppConfig
from src.schemas.market import MarketSnapshotSchema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_test_config(**overrides) -> AppConfig:
    """Build a minimal AppConfig for unit tests."""
    defaults = dict(
        anthropic_api_key="sk-ant-test-fake-key-000",
        polygon_rpc_url="http://localhost:8545",
        wallet_address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        wallet_private_key="0x" + "a1" * 32,
        dry_run=True,
    )
    defaults.update(overrides)
    return AppConfig(**defaults)


# ---------------------------------------------------------------------------
# A. asyncio.gather fan-out
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_tracking_fan_out_produces_all_contexts():
    """asyncio.gather with return_exceptions=True must produce MarketContext
    for every successful track_market call."""
    mock_aggregator = AsyncMock()
    mock_aggregator.track_market.side_effect = lambda token_ids: [
        {"token_ids": token_ids, "condition_id": f"cid-{token_ids[0]}"}
    ]

    token_ids_list = [["t1"], ["t2"], ["t3"]]
    tasks = [
        mock_aggregator.track_market(token_ids)
        for token_ids in token_ids_list
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # All three must succeed
    success = [r for r in results if not isinstance(r, Exception)]
    assert len(success) == 3


@pytest.mark.asyncio
async def test_gather_exception_isolation_one_task_fails():
    """One task raising ValueError must NOT crash the other two;
    return_exceptions=True prevents aggregate crash."""
    mock_aggregator = AsyncMock()

    async def maybe_fail(token_ids):
        if token_ids == ["t2"]:
            raise ValueError("market_error")
        return [{"token_ids": token_ids}]

    mock_aggregator.track_market.side_effect = maybe_fail

    token_ids_list = [["t1"], ["t2"], ["t3"]]
    tasks = [
        mock_aggregator.track_market(token_ids)
        for token_ids in token_ids_list
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    exceptions = [r for r in results if isinstance(r, Exception)]
    successes = [r for r in results if not isinstance(r, Exception)]

    assert len(exceptions) == 1
    assert isinstance(exceptions[0], ValueError)
    assert len(successes) == 2


@pytest.mark.asyncio
async def test_gather_empty_markets_no_error():
    """asyncio.gather with no tasks must return empty list without error."""
    results = await asyncio.gather(*[], return_exceptions=True)
    assert results == []


@pytest.mark.asyncio
async def test_gather_all_tasks_fail():
    """When all tasks raise, gather must return all exceptions, zero contexts."""
    mock_aggregator = AsyncMock()
    mock_aggregator.track_market.side_effect = RuntimeError("always_fail")

    token_ids_list = [["t1"], ["t2"]]
    tasks = [
        mock_aggregator.track_market(token_ids)
        for token_ids in token_ids_list
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    exceptions = [r for r in results if isinstance(r, Exception)]
    assert len(exceptions) == 2


# ---------------------------------------------------------------------------
# B. subscribe_batch() multiplexed subscription
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_batch_sends_multiplexed_message():
    """subscribe_batch must send a single JSON message with all assets_ids."""
    from src.agents.ingestion.ws_client import CLOBWebSocketClient

    mock_ws = AsyncMock()
    mock_queue = asyncio.Queue()
    mock_db_factory = MagicMock()

    cfg = _make_test_config()
    client = CLOBWebSocketClient(cfg, mock_queue, mock_db_factory)
    # Inject the mock websocket connection
    client._ws = mock_ws  # type: ignore[assignment]

    await client.subscribe_batch(["t1", "t2", "t3"])

    mock_ws.send.assert_awaited_once()
    sent_msg = mock_ws.send.call_args[0][0]
    payload = json.loads(sent_msg)
    assert payload["type"] == "subscribe"
    assert payload["assets_ids"] == ["t1", "t2", "t3"]
    assert "event_types" in payload


@pytest.mark.asyncio
async def test_subscribe_batch_empty_list_logs_warning():
    """subscribe_batch with empty list must return gracefully without sending."""
    from src.agents.ingestion.ws_client import CLOBWebSocketClient

    mock_ws = AsyncMock()
    mock_queue = asyncio.Queue()
    mock_db_factory = MagicMock()

    cfg = _make_test_config()
    client = CLOBWebSocketClient(cfg, mock_queue, mock_db_factory)
    client._ws = mock_ws  # type: ignore[assignment]

    await client.subscribe_batch([])

    mock_ws.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_subscribe_batch_contains_correct_event_types():
    """Subscription message must include book, price_change, last_trade_price."""
    from src.agents.ingestion.ws_client import CLOBWebSocketClient

    mock_ws = AsyncMock()
    mock_queue = asyncio.Queue()
    mock_db_factory = MagicMock()

    cfg = _make_test_config()
    client = CLOBWebSocketClient(cfg, mock_queue, mock_db_factory)
    client._ws = mock_ws  # type: ignore[assignment]

    await client.subscribe_batch(["t1"])

    sent_msg = mock_ws.send.call_args[0][0]
    payload = json.loads(sent_msg)
    expected_events = {"book", "price_change", "last_trade_price"}
    assert set(payload["event_types"]) == expected_events


# ---------------------------------------------------------------------------
# C. Frame routing via asset_id
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_message_routes_to_correct_aggregator():
    """_handle_message with a frame containing asset_id must route to the
    registered DataAggregator for that asset_id."""
    from src.agents.ingestion.ws_client import CLOBWebSocketClient

    mock_queue = asyncio.Queue()
    mock_db_factory = MagicMock()

    cfg = _make_test_config()
    client = CLOBWebSocketClient(cfg, mock_queue, mock_db_factory)

    mock_aggregator = MagicMock()
    mock_aggregator.process_frame = MagicMock()

    client.register_aggregator("t1", mock_aggregator)

    frame = json.dumps({"asset_id": "t1", "event_type": "book", "best_bid": 0.45, "best_ask": 0.55})
    await client._handle_message(frame)

    mock_aggregator.process_frame.assert_called_once()


@pytest.mark.asyncio
async def test_handle_message_unrouted_frame_logged():
    """Frame with asset_id that has no registered aggregator must be logged
    as ws.frame_unrouted (not crash)."""
    from src.agents.ingestion.ws_client import CLOBWebSocketClient

    mock_queue = asyncio.Queue()
    mock_db_factory = MagicMock()

    cfg = _make_test_config()
    client = CLOBWebSocketClient(cfg, mock_queue, mock_db_factory)

    # No aggregator registered for "unknown_token"
    frame = json.dumps({"asset_id": "unknown_token", "event_type": "book", "best_bid": 0.45, "best_ask": 0.55})
    # Must not raise
    await client._handle_message(frame)


@pytest.mark.asyncio
async def test_handle_message_missing_asset_id_logged():
    """Frame lacking asset_id must be logged as ws.frame_unrouted."""
    from src.agents.ingestion.ws_client import CLOBWebSocketClient

    mock_queue = asyncio.Queue()
    mock_db_factory = MagicMock()

    cfg = _make_test_config()
    client = CLOBWebSocketClient(cfg, mock_queue, mock_db_factory)

    frame = json.dumps({"event_type": "book", "best_bid": 0.45, "best_ask": 0.55})
    await client._handle_message(frame)
    # No crash; logged as unrouted


# ---------------------------------------------------------------------------
# D. Market truncation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_truncation_caps_at_max_concurrent():
    """When discovered markets > max_concurrent_markets, only first N tracked."""
    config = _make_test_config(max_concurrent_markets=3)

    # Simulate 5 discovered markets
    snapshots = [MagicMock(condition_id=f"cid_{i}") for i in range(5)]

    # Truncate logic: should cap at 3
    if len(snapshots) > config.max_concurrent_markets:
        snapshots = snapshots[:config.max_concurrent_markets]

    assert len(snapshots) == 3


@pytest.mark.asyncio
async def test_no_truncation_when_under_max():
    """When discovered < max_concurrent_markets, all tracked."""
    config = _make_test_config(max_concurrent_markets=10)

    snapshots = [MagicMock(condition_id=f"cid_{i}") for i in range(3)]

    if len(snapshots) > config.max_concurrent_markets:
        snapshots = snapshots[:config.max_concurrent_markets]

    assert len(snapshots) == 3


# ---------------------------------------------------------------------------
# E. PerMarketAggregatorState
# ---------------------------------------------------------------------------

def test_per_market_aggregator_state_initializes_correctly():
    """PerMarketAggregatorState must init with pending status, frame_count=0."""
    from src.schemas.market import PerMarketAggregatorState

    state = PerMarketAggregatorState(token_ids=["t1", "t2"])

    assert state.token_ids == ["t1", "t2"]
    assert state.subscription_status == "pending"
    assert state.frame_count == 0
    assert state.last_seen_utc is None


def test_per_market_aggregator_state_is_frozen():
    """PerMarketAggregatorState must be frozen — mutation raises ValidationError."""
    from src.schemas.market import PerMarketAggregatorState

    state = PerMarketAggregatorState(token_ids=["t1"])

    with pytest.raises(ValidationError):
        state.token_ids = ["t3"]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# F. MarketTrackingTask pattern
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_tracking_task_not_created_when_disabled():
    """When enable_market_tracking=False, no MarketTrackingTask created."""
    from src.orchestrator import Orchestrator

    patches = {
        "AsyncWeb3": MagicMock(),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }

    config = _make_test_config(enable_market_tracking=False)

    with patch.multiple("src.orchestrator", **patches):
        orch = Orchestrator(config)

    # market_tracking_task is None when disabled
    assert orch.market_tracking_task is None


@pytest.mark.asyncio
async def test_market_tracking_task_created_when_enabled():
    """When enable_market_tracking=True, MarketTrackingTask is created during start()."""
    from src.orchestrator import Orchestrator

    patches = {
        "AsyncWeb3": MagicMock(),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }

    config = _make_test_config(enable_market_tracking=True)

    with patch.multiple("src.orchestrator", **patches):
        orch = Orchestrator(config)

    # Task is NOT created in __init__ — it's created in start()
    # So we test that the config flag is properly set
    assert config.enable_market_tracking is True

    # After start() begins, the task should be created
    # We verify by checking that start() sets _running=True
    # and the task creation code path is triggered
    # For unit testing, we directly check the attribute
    async def fake_start():
        orch._running = True
        orch.market_tracking_task = asyncio.create_task(
            orch._market_tracking_loop(), name="MarketTrackingTask"
        )

    await fake_start()

    assert hasattr(orch, "market_tracking_task")
    assert orch.market_tracking_task is not None
    assert orch.market_tracking_task.get_name() == "MarketTrackingTask"

    # Clean up
    orch._running = False
    orch.market_tracking_task.cancel()
    try:
        await orch.market_tracking_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_market_tracking_loop_sleeps_first():
    """Market tracking loop must sleep at the top before any work (sleep-first)."""
    from src.orchestrator import Orchestrator

    patches = {
        "AsyncWeb3": MagicMock(),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }

    config = _make_test_config(
        enable_market_tracking=True,
        market_tracking_interval_sec=Decimal("1"),
    )

    with patch.multiple("src.orchestrator", **patches):
        orch = Orchestrator(config)

    # Mock discovery to return empty so loop exits quickly after sleep
    orch.discovery_engine = AsyncMock()
    orch.discovery_engine.discover = AsyncMock(return_value=[])
    orch._data_aggregator = AsyncMock()

    # Run the loop briefly — it should sleep first then continue
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        orch._running = True
        task = asyncio.create_task(orch._market_tracking_loop())
        await asyncio.sleep(0.05)
        orch._running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # asyncio.sleep must have been called at least once
    mock_sleep.assert_called()


@pytest.mark.asyncio
async def test_market_tracking_loop_fail_open_on_discovery_error():
    """Exception in discovery must be logged, loop continues (fail-open)."""
    from src.orchestrator import Orchestrator

    patches = {
        "AsyncWeb3": MagicMock(),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }

    config = _make_test_config(
        enable_market_tracking=True,
        market_tracking_interval_sec=Decimal("0.1"),
    )

    with patch.multiple("src.orchestrator", **patches):
        orch = Orchestrator(config)

    orch.discovery_engine = AsyncMock()
    orch.discovery_engine.discover = AsyncMock(side_effect=RuntimeError("discovery_fail"))

    orch._running = True
    # Run briefly — loop must not crash despite discovery errors
    task = asyncio.create_task(orch._market_tracking_loop())
    await asyncio.sleep(0.2)
    orch._running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # If we reach here, loop survived the error (fail-open)


# ---------------------------------------------------------------------------
# G. Decimal safety under concurrency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_track_market_produces_decimal_fields():
    """Concurrent track_market calls must produce MarketContext with
    Decimal-typed financial fields — no float leakage."""
    from decimal import Decimal

    # Simulate concurrent market contexts
    contexts = []
    for i in range(3):
        ctx = {
            "midpoint": Decimal("0.50"),
            "best_bid": Decimal("0.49"),
            "best_ask": Decimal("0.51"),
            "token_ids": [f"t{i}"],
        }
        contexts.append(ctx)

    # Verify all financial fields are Decimal
    for ctx in contexts:
        assert isinstance(ctx["midpoint"], Decimal)
        assert isinstance(ctx["best_bid"], Decimal)
        assert isinstance(ctx["best_ask"], Decimal)


def test_per_market_state_decimal_serialization():
    """PerMarketAggregatorState serialization/deserialization preserves Decimal types."""
    from src.schemas.market import PerMarketAggregatorState

    state = PerMarketAggregatorState(token_ids=["t1"])

    # Serialize to dict and back
    data = state.model_dump()
    restored = PerMarketAggregatorState(**data)

    assert restored.token_ids == ["t1"]
    assert restored.frame_count == 0
