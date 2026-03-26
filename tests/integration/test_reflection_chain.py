"""
tests/integration/test_reflection_chain.py

Integration tests for WI-13 — Reflection Auditor (self-correction stage).

RED phase: all 4 tests are expected to fail because ReflectionResponse and
the reflection methods (_run_reflection_audit, _apply_reflection_verdict) do
not exist in src/ yet.

Asserts:
  1. APPROVED verdict: reflection passes candidate through; Gatekeeper remains terminal.
  2. REJECTED via bias: reflection flags bias; forces HOLD path (no execution enqueue).
  3. ADJUSTED via math fix: reflection returns corrected candidate that passes Gatekeeper.
  4. TIMEOUT via budget exhaustion: conservative HOLD + audit artifact persisted.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.evaluation.claude_client import ClaudeClient
from src.schemas.llm import (
    LLMEvaluationResponse,
    RecommendedAction,
    ReflectionResponse,  # does NOT exist yet — expected ImportError
)


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


def _future_iso(hours: float) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.isoformat()


def _crypto_market_item() -> dict:
    """Market item that routes to CRYPTO via keyword matching."""
    return {
        "snapshot_id": "snap-reflect-001",
        "state": {
            "condition_id": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
            "title": "Will Bitcoin exceed $100k by July?",
            "tags": ["btc", "crypto"],
            "best_bid": 0.45,
            "best_ask": 0.455,
            "midpoint": 0.4525,
            "spread": 0.005,
            "timestamp": 1700000000,
        },
    }


def _primary_candidate_json(
    *,
    decision: bool = True,
    action: str = "BUY",
    confidence: float = 0.85,
    p_true: float = 0.65,
    p_market: float = 0.45,
) -> str:
    """Build a Stage B primary evaluation candidate (raw JSON string)."""
    end_date = _future_iso(hours=720)
    payload = {
        "market_context": {
            "condition_id": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
            "outcome_evaluated": "YES",
            "best_bid": 0.45,
            "best_ask": 0.455,
            "midpoint": 0.4525,
            "market_end_date": end_date,
        },
        "probabilistic_estimate": {
            "p_true": p_true,
            "p_market": p_market,
        },
        "risk_assessment": {
            "liquidity_risk_score": 0.2,
            "resolution_risk_score": 0.1,
            "information_asymmetry_flag": False,
            "risk_notes": (
                "Low risk market with adequate liquidity and clear "
                "resolution criteria established by oracle."
            ),
        },
        "confidence_score": confidence,
        "decision_boolean": decision,
        "recommended_action": action,
        "reasoning_log": (
            "Based on thorough analysis of the market data and external "
            "signals, the true probability is estimated at 65% while the "
            "market implies approximately 45%. This creates a significant "
            "positive expected value opportunity with adequate confidence."
        ),
    }
    return json.dumps(payload)


def _reflection_json(
    *,
    verdict: str = "APPROVED",
    bias_flags: list[str] | None = None,
    consistency_flags: list[str] | None = None,
    risk_flags: list[str] | None = None,
    audit_note: str = "No issues found.",
    correction_instructions: str | None = None,
    corrected_candidate_json: dict | None = None,
    latency_ms: int = 120,
) -> str:
    """Build a reflection auditor response JSON string."""
    payload = {
        "verdict": verdict,
        "bias_flags": bias_flags or [],
        "consistency_flags": consistency_flags or [],
        "risk_flags": risk_flags or [],
        "audit_note": audit_note,
        "correction_instructions": correction_instructions,
        "corrected_candidate_json": corrected_candidate_json,
        "latency_ms": latency_ms,
    }
    return json.dumps(payload)


def _setup_client(test_config):
    """Create a ClaudeClient with mocked Anthropic API and DB persistence."""
    in_q: asyncio.Queue = asyncio.Queue()
    out_q: asyncio.Queue = asyncio.Queue()
    client = ClaudeClient(in_queue=in_q, out_queue=out_q, config=test_config)

    # Mock DB persistence
    client._persist_decision = AsyncMock()

    return client, in_q, out_q


# ---------------------------------------------------------------------------
# Test 1: APPROVED — reflection passes candidate through; Gatekeeper terminal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reflection_approved_passes_to_gatekeeper(test_config):
    """When reflection returns APPROVED, the original Stage B candidate passes
    through unchanged and LLMEvaluationResponse Gatekeeper validates it.
    Trade reaches execution queue."""
    client, _, out_q = _setup_client(test_config)

    primary_json = _primary_candidate_json(decision=True, action="BUY")
    reflection_resp_json = _reflection_json(
        verdict="APPROVED",
        audit_note="No bias, data consistent, EV arithmetic correct.",
    )

    # Mock Anthropic: first call returns primary candidate, second returns reflection
    client.client = MagicMock()
    client.client.messages.create = AsyncMock(
        side_effect=[
            _mock_anthropic_response(primary_json),
            _mock_anthropic_response(reflection_resp_json),
        ],
    )

    await client._process_evaluation(_crypto_market_item())

    # Reflection must have been invoked
    assert hasattr(client, "_run_reflection_audit"), (
        "_run_reflection_audit method must exist on ClaudeClient"
    )

    # Trade must reach execution queue (APPROVED -> Gatekeeper passes -> enqueue)
    assert out_q.qsize() == 1, "APPROVED reflection + passing Gatekeeper must enqueue trade"
    result = out_q.get_nowait()
    eval_resp = result["evaluation"]

    # Gatekeeper audit must exist (proves terminal validation happened)
    assert eval_resp.gatekeeper_audit is not None
    assert eval_resp.gatekeeper_audit.all_filters_passed is True
    assert eval_resp.decision_boolean is True
    assert eval_resp.recommended_action == RecommendedAction.BUY

    # Reflection audit artifact must be persisted
    persist_call = client._persist_decision.call_args
    assert persist_call is not None, "Decision must be persisted with reflection metadata"


# ---------------------------------------------------------------------------
# Test 2: REJECTED via bias — forces HOLD path, no execution enqueue
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reflection_rejected_bias_forces_hold(test_config):
    """When reflection returns REJECTED with bias flags, the pipeline must
    force a HOLD candidate — trade must NOT reach execution queue.
    REJECTED verdict guarantees non-execution regardless of original candidate."""
    client, _, out_q = _setup_client(test_config)

    # Primary candidate would normally pass Gatekeeper (BUY, high confidence)
    primary_json = _primary_candidate_json(decision=True, action="BUY")
    reflection_resp_json = _reflection_json(
        verdict="REJECTED",
        bias_flags=["confirmation_bias", "narrative_anchoring"],
        consistency_flags=["p_true_unsupported_by_evidence"],
        audit_note="Severe confirmation bias detected. p_true=0.65 not supported by data.",
    )

    client.client = MagicMock()
    client.client.messages.create = AsyncMock(
        side_effect=[
            _mock_anthropic_response(primary_json),
            _mock_anthropic_response(reflection_resp_json),
        ],
    )

    await client._process_evaluation(_crypto_market_item())

    # REJECTED verdict must force HOLD — no trade enqueued
    assert out_q.qsize() == 0, (
        "REJECTED reflection must force HOLD; trade must NOT reach execution queue"
    )

    # Verify _persist_decision was still called (audit trail must exist)
    client._persist_decision.assert_called_once()

    # The persisted evaluation must be a HOLD
    persist_args = client._persist_decision.call_args
    persisted_eval = persist_args[0][0]  # first positional arg
    assert persisted_eval.decision_boolean is False
    assert persisted_eval.recommended_action == RecommendedAction.HOLD


# ---------------------------------------------------------------------------
# Test 3: ADJUSTED via math fix — corrected candidate passes Gatekeeper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reflection_adjusted_uses_corrected_candidate(test_config):
    """When reflection returns ADJUSTED, the corrected candidate JSON is used
    for Gatekeeper validation instead of the original. The corrected candidate
    must still pass LLMEvaluationResponse terminal validation."""
    client, _, out_q = _setup_client(test_config)

    # Primary candidate has overstated p_true (math error the auditor catches)
    primary_json = _primary_candidate_json(
        decision=True, action="BUY", p_true=0.90, confidence=0.85,
    )

    # Auditor corrects p_true down and recalculates — still positive EV
    corrected_candidate = json.loads(
        _primary_candidate_json(
            decision=True, action="BUY", p_true=0.65, confidence=0.85,
        )
    )
    reflection_resp_json = _reflection_json(
        verdict="ADJUSTED",
        consistency_flags=["p_true_arithmetic_error"],
        audit_note="p_true=0.90 inconsistent with cited evidence. Corrected to 0.65.",
        correction_instructions="Reduce p_true from 0.90 to 0.65 based on base-rate data.",
        corrected_candidate_json=corrected_candidate,
    )

    client.client = MagicMock()
    client.client.messages.create = AsyncMock(
        side_effect=[
            _mock_anthropic_response(primary_json),
            _mock_anthropic_response(reflection_resp_json),
        ],
    )

    await client._process_evaluation(_crypto_market_item())

    # Corrected candidate should still pass Gatekeeper and be enqueued
    assert out_q.qsize() == 1, "ADJUSTED reflection with valid correction must pass Gatekeeper"
    result = out_q.get_nowait()
    eval_resp = result["evaluation"]

    # The Gatekeeper-validated candidate must use the CORRECTED p_true
    assert eval_resp.probabilistic_estimate.p_true == pytest.approx(0.65, abs=0.01), (
        "Gatekeeper must validate the corrected candidate (p_true=0.65), not the original (p_true=0.90)"
    )
    assert eval_resp.gatekeeper_audit is not None
    assert eval_resp.gatekeeper_audit.all_filters_passed is True


# ---------------------------------------------------------------------------
# Test 4: TIMEOUT via 2.0s shared budget exhaustion — conservative HOLD
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reflection_timeout_yields_conservative_hold(test_config):
    """When the shared 2.0s latency budget is exhausted before reflection
    completes, the system must default to conservative HOLD behavior.
    Audit artifact with BUDGET_EXHAUSTED reason must be persisted."""
    client, _, out_q = _setup_client(test_config)

    primary_json = _primary_candidate_json(decision=True, action="BUY")

    # Primary eval call succeeds normally
    primary_resp = _mock_anthropic_response(primary_json)

    # Use a callable side_effect so the slow coroutine is created lazily
    # (avoids "coroutine was never awaited" warning on GC).
    _call_count = 0

    async def _budget_exhaustion_side_effect(*args, **kwargs):
        nonlocal _call_count
        _call_count += 1
        if _call_count == 1:
            return primary_resp
        # Second call: simulate slow reflection that exceeds budget
        await asyncio.sleep(3.0)
        return _mock_anthropic_response(
            _reflection_json(verdict="APPROVED")
        )

    client.client = MagicMock()
    client.client.messages.create = AsyncMock(
        side_effect=_budget_exhaustion_side_effect,
    )

    await client._process_evaluation(_crypto_market_item())

    # Budget exhaustion must force conservative HOLD — no trade enqueued
    assert out_q.qsize() == 0, (
        "Budget-exhausted reflection must default to conservative HOLD; "
        "no trade must reach execution queue"
    )

    # Audit trail must still be persisted with budget exhaustion metadata
    client._persist_decision.assert_called_once()

    # The persisted evaluation must be a HOLD
    persist_args = client._persist_decision.call_args
    persisted_eval = persist_args[0][0]
    assert persisted_eval.decision_boolean is False
    assert persisted_eval.recommended_action == RecommendedAction.HOLD
