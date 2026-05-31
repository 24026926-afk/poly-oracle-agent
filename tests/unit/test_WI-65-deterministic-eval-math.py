"""
tests/unit/test_WI-65-deterministic-eval-math.py

WI-65 — Deterministic Eval Math (fix A).

The LLM supplies judgment only (p_true, confidence, reasoning, qualitative
risk). The system owns all market facts and all arithmetic:

1. The primary prompt no longer instructs the model to calculate EV or apply
   numeric EV/spread/confidence thresholds.
2. The reflection prompt no longer asks the auditor to verify EV or
   bid/ask/spread arithmetic; it states those values are system-computed.
3. Before terminal Gatekeeper validation, code overrides the candidate's
   market_context (best_bid/best_ask/midpoint) and probabilistic_estimate
   (p_market) with the authoritative ``wi14_snapshot`` values, so the
   Gatekeeper's spread/EV can never be moved by an LLM echo.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.agents.context.prompt_factory import PromptFactory
from src.agents.evaluation.claude_client import ClaudeClient
from src.agents.execution.polymarket_client import MarketSnapshot
from src.schemas.llm import LLMEvaluationResponse


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------
def _market_state() -> dict:
    return {
        "condition_id": "0x" + "a" * 40,
        "best_bid": 0.42,
        "best_ask": 0.44,
        "midpoint": 0.43,
        "spread": 0.02,
        "timestamp": 1234567890.0,
    }


def _authoritative_snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        token_id="tok-1",
        best_bid=Decimal("0.42"),
        best_ask=Decimal("0.44"),
        midpoint_probability=Decimal("0.43"),
        spread=Decimal("0.02"),
        fetched_at_utc=datetime.now(timezone.utc),
        source="clob_orderbook",
    )


def _llm_candidate_with_wrong_facts() -> dict:
    """A structurally valid candidate whose market facts are WRONG (LLM echo)."""
    return {
        "market_context": {
            "condition_id": "0x" + "a" * 40,
            "outcome_evaluated": "YES",
            "best_bid": 0.001,
            "best_ask": 0.999,
            "midpoint": 0.5,
        },
        "probabilistic_estimate": {"p_true": 0.6, "p_market": 0.5},
        "risk_assessment": {
            "liquidity_risk_score": 0.2,
            "resolution_risk_score": 0.2,
            "information_asymmetry_flag": False,
            "risk_notes": "Sufficiently long risk note for schema validation.",
        },
        "confidence_score": 0.8,
        "decision_boolean": True,
        "recommended_action": "BUY",
        "reasoning_log": "Reasoning log long enough to satisfy the minimum length constraint.",
    }


# ---------------------------------------------------------------------------
# Primary prompt — no arithmetic obligations
# ---------------------------------------------------------------------------
def test_primary_prompt_omits_ev_calculation_instruction():
    prompt = PromptFactory.build_evaluation_prompt(_market_state())
    assert "Calculate the Expected Value" not in prompt
    assert "EV = (True Probability" not in prompt


def test_primary_prompt_omits_numeric_filter_thresholds():
    prompt = PromptFactory.build_evaluation_prompt(_market_state())
    assert "EV > 2%" not in prompt
    assert "Spread < 1.5%" not in prompt


def test_primary_prompt_states_system_computes_arithmetic():
    prompt = PromptFactory.build_evaluation_prompt(_market_state()).lower()
    assert "system computes" in prompt
    assert "do not calculate" in prompt


def test_primary_prompt_still_requests_judgment():
    prompt = PromptFactory.build_evaluation_prompt(_market_state())
    assert "True Probability" in prompt
    assert "reasoning" in prompt.lower()


# ---------------------------------------------------------------------------
# Reflection prompt — no arithmetic re-audit
# ---------------------------------------------------------------------------
def _reflection_prompt() -> str:
    return PromptFactory.build_reflection_prompt(
        market_state=_market_state(),
        sentiment=None,
        primary_candidate_json="{}",
        snapshot_id="snap-1",
    )


def test_reflection_prompt_omits_ev_arithmetic_audit_question():
    text = _reflection_prompt()
    assert "EV arithmetic internally consistent" not in text


def test_reflection_prompt_omits_spread_arithmetic_audit_question():
    text = _reflection_prompt()
    assert "bid/ask/midpoint/spread relationships coherent" not in text


def test_reflection_prompt_states_arithmetic_is_system_computed():
    text = _reflection_prompt().lower()
    assert "computed deterministically" in text
    assert "do not recompute" in text


def test_reflection_prompt_still_checks_bias_and_evidence():
    text = _reflection_prompt()
    assert "bias" in text.lower()
    assert "p_true" in text


# ---------------------------------------------------------------------------
# Authoritative market-fact override
# ---------------------------------------------------------------------------
def test_override_replaces_market_context_facts():
    candidate = _llm_candidate_with_wrong_facts()
    out_json = ClaudeClient._apply_authoritative_market_facts(
        json.dumps(candidate), _authoritative_snapshot()
    )
    out = json.loads(out_json)
    assert float(out["market_context"]["best_bid"]) == pytest.approx(0.42)
    assert float(out["market_context"]["best_ask"]) == pytest.approx(0.44)
    assert float(out["market_context"]["midpoint"]) == pytest.approx(0.43)


def test_override_replaces_p_market():
    candidate = _llm_candidate_with_wrong_facts()
    out_json = ClaudeClient._apply_authoritative_market_facts(
        json.dumps(candidate), _authoritative_snapshot()
    )
    out = json.loads(out_json)
    assert float(out["probabilistic_estimate"]["p_market"]) == pytest.approx(0.43)


def test_override_validated_response_uses_authoritative_spread_and_ev():
    candidate = _llm_candidate_with_wrong_facts()
    out_json = ClaudeClient._apply_authoritative_market_facts(
        json.dumps(candidate), _authoritative_snapshot()
    )
    resp = LLMEvaluationResponse.model_validate_json(out_json)

    # spread from authoritative bid/ask, not the 0.001/0.999 echo
    expected_spread = round((0.44 - 0.42) / 0.44, 6)
    assert resp.gatekeeper_audit is not None
    assert resp.gatekeeper_audit.computed_spread_pct == pytest.approx(
        expected_spread, abs=1e-6
    )

    # EV from p_true=0.6 and authoritative p_market=0.43
    b = (1.0 - 0.43) / 0.43
    expected_ev = 0.6 * b - 0.4
    assert resp.expected_value == pytest.approx(expected_ev, abs=1e-6)


def test_override_returns_input_when_candidate_not_a_dict():
    # Defensive: a "null"/non-dict candidate (e.g. ADJUSTED with no correction)
    # is returned unchanged so terminal validation reports the real error.
    out = ClaudeClient._apply_authoritative_market_facts(
        "null", _authoritative_snapshot()
    )
    assert out == "null"


def test_override_does_not_mutate_input_string():
    candidate = _llm_candidate_with_wrong_facts()
    original = json.dumps(candidate)
    _ = ClaudeClient._apply_authoritative_market_facts(
        original, _authoritative_snapshot()
    )
    # original JSON still carries the wrong echo (pure function, no aliasing)
    reparsed = json.loads(original)
    assert float(reparsed["market_context"]["best_bid"]) == pytest.approx(0.001)
