"""
tests/unit/test_data_aggregator.py

Unit tests for DataAggregator — market snapshot processing and queue safety.
"""

import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from src.agents.context.aggregator import DataAggregator
from src.db.models import MarketSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_snapshot(
    condition_id: str = "0xcond123",
    best_bid: float = 0.45,
    best_ask: float = 0.55,
    midpoint: float = 0.50,
) -> MarketSnapshot:
    """Build a MarketSnapshot ORM object matching what ws_client puts on queue."""
    return MarketSnapshot(
        condition_id=condition_id,
        question="Test?",
        best_bid=best_bid,
        best_ask=best_ask,
        last_trade_price=0.50,
        midpoint=midpoint,
        outcome_token="YES",
        raw_ws_payload="{}",
    )


# ---------------------------------------------------------------------------
# Bug 1: Aggregator must handle MarketSnapshot objects (not CLOBMessage)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_message_reads_best_bid_ask_from_market_snapshot():
    """DataAggregator must read best_bid/best_ask directly from MarketSnapshot
    ORM objects — NOT access .bids[0].price which doesn't exist."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    agg = DataAggregator(input_queue=in_q, output_queue=out_q, condition_id="0xcond123")

    snap = _make_snapshot(best_bid=0.40, best_ask=0.60)
    await agg._process_message(snap)

    assert agg.best_bid == 0.40
    assert agg.best_ask == 0.60


@pytest.mark.asyncio
async def test_process_message_ignores_different_condition_id():
    """Messages for other markets are silently dropped."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    agg = DataAggregator(input_queue=in_q, output_queue=out_q, condition_id="0xmine")

    snap = _make_snapshot(condition_id="0xother", best_bid=0.99, best_ask=0.99)
    await agg._process_message(snap)

    # Internal state should NOT have been updated
    assert agg.best_bid == 0.0
    assert agg.best_ask == 0.0


# ---------------------------------------------------------------------------
# Bug 2: task_done() must not be called when get() was cancelled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consume_queue_no_task_done_on_cancel():
    """When the consume loop is cancelled during get(), task_done must NOT
    be called — otherwise asyncio raises 'task_done() called too many times'."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()

    config = MagicMock()
    config.anthropic_api_key = MagicMock()
    config.anthropic_api_key.get_secret_value = MagicMock(return_value="fake-key")
    config.grok_api_key = ""
    config.grok_base_url = ""
    config.grok_model = ""
    config.grok_mocked = True
    config.clob_rest_url = "https://fake.clob"

    from src.agents.evaluation.claude_client import ClaudeClient

    client = ClaudeClient(
        in_queue=in_q,
        out_queue=out_q,
        config=config,
        db_session_factory=None,
    )

    client._running = True

    # Start the consume loop — it will block on get() with empty queue
    task = asyncio.create_task(client._consume_queue())

    # Let it block on get()
    await asyncio.sleep(0.05)

    # Cancel while blocked on get() — must NOT raise ValueError from
    # a spurious task_done() call inside the finally block.
    task.cancel()

    exc = task.exception() if task.done() else None
    try:
        await task
    except asyncio.CancelledError:
        pass
    except ValueError:
        pytest.fail(
            "Cancelling during get() caused ValueError from spurious task_done()"
        )


@pytest.mark.asyncio
async def test_consume_queue_calls_task_done_after_processing():
    """After successfully processing an item, task_done must be called exactly once."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()

    config = MagicMock()
    config.anthropic_api_key = MagicMock()
    config.anthropic_api_key.get_secret_value = MagicMock(return_value="fake-key")
    config.grok_api_key = ""
    config.grok_base_url = ""
    config.grok_model = ""
    config.grok_mocked = True
    config.clob_rest_url = "https://fake.clob"

    from src.agents.evaluation.claude_client import ClaudeClient

    client = ClaudeClient(
        in_queue=in_q,
        out_queue=out_q,
        config=config,
        db_session_factory=None,
    )
    client._running = True
    # Mock _process_evaluation to avoid actual LLM calls
    client._process_evaluation = AsyncMock()

    # Put an item and start consume
    await in_q.put({"snapshot_id": "test1", "state": {}})

    task = asyncio.create_task(client._consume_queue())
    await asyncio.sleep(0.05)

    # Item was processed; task_done should have been called.
    # Calling task_done again must raise (proves exactly 1 call happened).
    with pytest.raises(ValueError, match="task_done"):
        in_q.task_done()

    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Throttling: aggregator emits at 30s intervals / 1% price change
# ---------------------------------------------------------------------------

def test_aggregator_time_interval_is_30_seconds():
    """Evaluation trigger interval must be 30s to avoid flooding the LLM."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    agg = DataAggregator(input_queue=in_q, output_queue=out_q, condition_id="0x1")

    assert agg.TIME_INTERVAL_SEC == 30.0


def test_aggregator_price_threshold_is_1_percent():
    """Price volatility trigger must be 1% (0.01) for meaningful LLM calls."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    agg = DataAggregator(input_queue=in_q, output_queue=out_q, condition_id="0x1")

    assert agg.PRICE_CHANGE_THRESHOLD == 0.01


@pytest.mark.asyncio
async def test_aggregator_suppresses_emit_within_interval():
    """After an emit, a new message within the interval must NOT trigger another emit
    unless the price moved beyond the threshold."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    agg = DataAggregator(input_queue=in_q, output_queue=out_q, condition_id="0xcond123")

    # First message — should emit (last_emit_time is 0 which means interval elapsed)
    snap1 = _make_snapshot(best_bid=0.45, best_ask=0.55)
    await agg._process_message(snap1)
    first_emit_count = out_q.qsize()

    # Second message immediately — same price, within interval — should NOT emit
    snap2 = _make_snapshot(best_bid=0.45, best_ask=0.55)
    await agg._process_message(snap2)

    assert out_q.qsize() == first_emit_count, "must not emit again within throttle interval"
