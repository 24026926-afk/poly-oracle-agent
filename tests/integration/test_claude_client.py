"""
tests/integration/test_claude_client.py

Integration tests for the ClaudeClient evaluation loop — Anthropic API mocking,
queue routing, DB persistence, and retry behaviour.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from src.agents.evaluation.claude_client import ClaudeClient
from src.db.models import AgentDecisionLog, MarketSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_anthropic_response(raw_json: str):
    """Build a mock Anthropic message response object."""
    content_block = MagicMock()
    content_block.text = raw_json

    usage = MagicMock()
    usage.input_tokens = 150
    usage.output_tokens = 350

    resp = MagicMock()
    resp.content = [content_block]
    resp.usage = usage
    return resp


def _make_snapshot_row(snapshot_id: str) -> MarketSnapshot:
    """Create a minimal MarketSnapshot DB row for FK satisfaction."""
    return MarketSnapshot(
        id=snapshot_id,
        condition_id="0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
        question="Test market",
        best_bid=0.45,
        best_ask=0.455,
        midpoint=0.4525,
        outcome_token="YES",
        raw_ws_payload="{}",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluation_approved_trade_reaches_execution_queue(
    test_config, mock_anthropic_buy_json
):
    """An approved BUY evaluation must land on the execution queue."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

    # Patch the Anthropic client
    client.client = MagicMock()
    client.client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(mock_anthropic_buy_json)
    )

    # Bypass DB persistence to avoid FK constraints
    client._persist_decision = AsyncMock()

    item = {"prompt": "Evaluate this market", "snapshot_id": "snap-buy-001"}
    await client._process_evaluation(item)

    assert out_q.qsize() == 1
    result = out_q.get_nowait()
    assert "evaluation" in result
    assert result["evaluation"].decision_boolean is True


@pytest.mark.asyncio
async def test_evaluation_rejected_trade_not_enqueued(
    test_config, mock_anthropic_hold_json
):
    """A HOLD evaluation (low confidence) must NOT reach the execution queue."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

    client.client = MagicMock()
    client.client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(mock_anthropic_hold_json)
    )
    client._persist_decision = AsyncMock()

    item = {"prompt": "Evaluate this market", "snapshot_id": "snap-hold-001"}
    await client._process_evaluation(item)

    # Gatekeeper overrides low confidence to HOLD → not enqueued
    assert out_q.qsize() == 0


@pytest.mark.asyncio
async def test_evaluation_persists_decision_log(
    test_config, mock_anthropic_buy_json, async_engine, db_session_factory
):
    """After evaluation, an AgentDecisionLog row must be persisted."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

    client.client = MagicMock()
    client.client.messages.create = AsyncMock(
        return_value=_mock_anthropic_response(mock_anthropic_buy_json)
    )

    # Pre-insert a MarketSnapshot row so the FK is satisfied
    snapshot_id = "snap-persist-001"
    async with db_session_factory() as session:
        session.add(_make_snapshot_row(snapshot_id))
        await session.commit()

    # Patch get_db_session to use our test DB
    async def _test_db_session():
        async with db_session_factory() as session:
            yield session

    with patch("src.agents.evaluation.claude_client.get_db_session", _test_db_session):
        item = {"prompt": "Evaluate this market", "snapshot_id": snapshot_id}
        await client._process_evaluation(item)

    # Verify decision log row
    async with db_session_factory() as session:
        result = await session.execute(select(AgentDecisionLog))
        rows = result.scalars().all()
        assert len(rows) == 1
        assert rows[0].snapshot_id == snapshot_id
        assert rows[0].llm_model_id == client.model


@pytest.mark.asyncio
async def test_evaluation_retries_on_validation_error(
    test_config, mock_anthropic_buy_json
):
    """First call returns invalid JSON; retry returns valid JSON → success."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

    bad_resp = _mock_anthropic_response('{"invalid": "schema"}')
    good_resp = _mock_anthropic_response(mock_anthropic_buy_json)

    client.client = MagicMock()
    client.client.messages.create = AsyncMock(
        side_effect=[bad_resp, good_resp]
    )
    client._persist_decision = AsyncMock()

    item = {"prompt": "Evaluate this market", "snapshot_id": "snap-retry-001"}
    await client._process_evaluation(item)

    # Despite the first failure, evaluation succeeds on retry
    assert out_q.qsize() == 1
    assert client.client.messages.create.call_count == 2
