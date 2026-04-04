"""
tests/unit/test_evaluation_budget.py

Unit tests for the shared budget enforcement in ClaudeClient.

Red Phase: assert that dry_run=False with slow responses SKIPS evaluation.
Green Phase: assert that dry_run=True bypasses the budget and completes.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.evaluation.claude_client import _CHAIN_BUDGET, ClaudeClient
from tests.conftest import APPROVED_REFLECTION_JSON


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_anthropic_response(raw_json: str, delay: float = 0.0):
    """Build a mock Anthropic message response object with optional delay."""
    content_block = MagicMock()
    content_block.text = raw_json

    usage = MagicMock()
    usage.input_tokens = 150
    usage.output_tokens = 350

    resp = MagicMock()
    resp.content = [content_block]
    resp.usage = usage
    return resp


def _approved_ev_json() -> str:
    """Return a valid LLMEvaluationResponse JSON for the primary candidate."""
    return json.dumps({
        "market_context": {
            "condition_id": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
            "outcome_evaluated": "YES",
            "best_bid": 0.45,
            "best_ask": 0.455,
            "midpoint": 0.4525,
            "market_end_date": "2026-06-01T00:00:00+00:00",
        },
        "probabilistic_estimate": {
            "p_true": 0.65,
            "p_market": 0.45,
        },
        "risk_assessment": {
            "liquidity_risk_score": 0.2,
            "resolution_risk_score": 0.1,
            "information_asymmetry_flag": False,
            "risk_notes": "Low risk market with adequate liquidity and resolution.",
        },
        "confidence_score": 0.85,
        "decision_boolean": True,
        "recommended_action": "BUY",
        "reasoning_log": "Based on thorough analysis of the market data and external signals, the true probability is estimated at 65%.",
    })


class _DryRunConfig:
    """Minimal config wrapper so dry_run is a real bool (not MagicMock)."""

    def __init__(self, *, dry_run: bool):
        self.dry_run = dry_run
        self.anthropic_api_key = MagicMock()
        self.anthropic_api_key.get_secret_value.return_value = "sk-test"
        self.anthropic_model = "claude-test"
        self.grok_api_key = MagicMock()
        self.grok_api_key.get_secret_value.return_value = "grok-test"
        self.grok_base_url = "http://localhost"
        self.grok_model = "grok-test"
        self.grok_mocked = True
        self.clob_rest_url = "http://localhost"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_exhausted_dry_run_false_skips_evaluation(
    mock_polymarket,
):
    """Red Phase: dry_run=False + slow response → evaluation SKIPS."""
    cfg = _DryRunConfig(dry_run=False)

    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=cfg)

    # Make the primary call take 3s (> _CHAIN_BUDGET of 2s)
    call_count = 0

    async def slow_primary(**kw):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(3.0)  # exceed 2s budget
        return _mock_anthropic_response(_approved_ev_json())

    client.client = MagicMock()
    client.client.messages.create = slow_primary
    client._persist_decision = AsyncMock()

    item = {
        "snapshot_id": "snap-budget-red",
        "yes_token_id": "tok-yes-001",
        "state": {
            "condition_id": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
            "best_bid": 0.45,
            "best_ask": 0.455,
            "midpoint": 0.4525,
            "spread": 0.005,
            "tags": [],
            "title": "Will ETH exceed $5000?",
            "timestamp": 1700000000,
        },
    }
    await client._process_evaluation(item)

    # Evaluation should have SKIPPED due to budget — nothing on output queue
    assert out_q.qsize() == 0


@pytest.mark.asyncio
async def test_budget_bypassed_dry_run_true_completes(
    mock_polymarket,
):
    """Green Phase: dry_run=True + slow response → evaluation COMPLETES."""
    cfg = _DryRunConfig(dry_run=True)

    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=cfg)

    # Slow primary (3s — would exceed 2s production budget) + fast reflection
    call_count = 0

    async def slow_primary_then_fast(**kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            await asyncio.sleep(3.0)  # slow primary — dry_run budget allows it
            return _mock_anthropic_response(_approved_ev_json())
        # Second call is reflection — return immediately
        return _mock_anthropic_response(APPROVED_REFLECTION_JSON)

    client.client = MagicMock()
    client.client.messages.create = slow_primary_then_fast
    client._persist_decision = AsyncMock()

    item = {
        "snapshot_id": "snap-budget-green",
        "yes_token_id": "tok-yes-001",
        "state": {
            "condition_id": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
            "best_bid": 0.45,
            "best_ask": 0.455,
            "midpoint": 0.4525,
            "spread": 0.005,
            "tags": [],
            "title": "Will ETH exceed $5000?",
            "timestamp": 1700000000,
        },
    }
    await client._process_evaluation(item)

    # dry_run=True should have bypassed the budget → trade enqueued
    assert out_q.qsize() == 1
    result = out_q.get_nowait()
    assert result["evaluation"].decision_boolean is True


def _approved_ev_json_minimal() -> str:
    """Return a valid LLMEvaluationResponse JSON with strings long enough for Pydantic."""
    return json.dumps({
        "market_context": {
            "condition_id": "0x1234567890abcdef",
            "outcome_evaluated": "YES",
            "best_bid": 0.45,
            "best_ask": 0.455,
            "midpoint": 0.4525,
            "market_end_date": "2026-06-01T00:00:00+00:00",
        },
        "probabilistic_estimate": {
            "p_true": 0.65,
            "p_market": 0.45,
        },
        "risk_assessment": {
            "liquidity_risk_score": 0.2,
            "resolution_risk_score": 0.1,
            "information_asymmetry_flag": False,
            "risk_notes": "Low risk market with adequate liquidity and resolution criteria.",
        },
        "confidence_score": 0.85,
        "decision_boolean": True,
        "recommended_action": "BUY",
        "reasoning_log": "Based on thorough analysis of the market data and external sentiment signals, the true probability is estimated at 65% while the market implies approximately 45%. This creates a positive expected value opportunity.",
    })


@pytest.mark.asyncio
async def test_grok_skipped_claude_proceeds_with_neutral(
    mock_polymarket,
):
    """Grok fallback for GENERAL category → Claude still evaluates."""
    cfg = _DryRunConfig(dry_run=True)

    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=cfg)

    client.client = MagicMock()
    client.client.messages.create = AsyncMock(
        side_effect=[
            _mock_anthropic_response(_approved_ev_json_minimal()),
            _mock_anthropic_response(APPROVED_REFLECTION_JSON),
        ]
    )
    client._persist_decision = AsyncMock()

    # Use a GENERAL market (not CRYPTO/POLITICS/SPORTS)
    item = {
        "snapshot_id": "snap-neutral-general",
        "yes_token_id": "tok-yes-001",
        "state": {
            "condition_id": "0x1234567890abcdef",
            "best_bid": 0.45,
            "best_ask": 0.455,
            "midpoint": 0.4525,
            "spread": 0.005,
            "tags": [],
            "title": "Will the weather be sunny tomorrow in London?",
            "timestamp": 1700000000,
        },
    }
    await client._process_evaluation(item)

    # Grok is skipped for GENERAL category → Claude proceeds with neutral sentiment
    assert out_q.qsize() == 1
    result = out_q.get_nowait()
    assert result["evaluation"].decision_boolean is True
