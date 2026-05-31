"""
tests/unit/test_WI-66-reflection-verdict-calibration.py

WI-66 — Reflection Verdict Calibration (fix B).

A REJECT justified only by soft bias flags is downgraded to a confidence
PENALTY (let the terminal Gatekeeper decide) instead of a forced HOLD. Hard
integrity flags and infrastructure failures stay fail-closed (HOLD).

Safety rests on LLMEvaluationResponse being the unconditional terminal
Gatekeeper: a penalized candidate still passes through MIN_CONFIDENCE / EV /
spread / TTR / exposure enforcement.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from src.agents.evaluation.claude_client import ClaudeClient
from src.core.config import AppConfig
from src.schemas.llm import (
    LLMEvaluationResponse,
    RecommendedAction,
    ReflectionResponse,
    ReflectionSeverity,
    ReflectionVerdict,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _reflection(verdict, *, bias=None, consistency=None, risk=None, note="audit"):
    return ReflectionResponse(
        verdict=verdict,
        bias_flags=bias or [],
        consistency_flags=consistency or [],
        risk_flags=risk or [],
        audit_note=note,
    )


def _primary_candidate(confidence: float) -> dict:
    """Structurally valid candidate with passing EV/spread and tunable confidence."""
    return {
        "market_context": {
            "condition_id": "0x" + "a" * 40,
            "outcome_evaluated": "YES",
            "best_bid": 0.499,
            "best_ask": 0.500,
            "midpoint": 0.4995,
        },
        "probabilistic_estimate": {"p_true": 0.7, "p_market": 0.4995},
        "risk_assessment": {
            "liquidity_risk_score": 0.2,
            "resolution_risk_score": 0.2,
            "information_asymmetry_flag": False,
            "risk_notes": "Sufficiently long risk note for schema validation.",
        },
        "confidence_score": confidence,
        "decision_boolean": True,
        "recommended_action": "BUY",
        "reasoning_log": "Reasoning log long enough to satisfy the minimum length constraint.",
    }


def _client(factor: str = "0.90") -> ClaudeClient:
    """Lightweight ClaudeClient with only the config attribute the method needs."""
    client = ClaudeClient.__new__(ClaudeClient)
    client.config = SimpleNamespace(
        reflection_soft_flag_confidence_factor=Decimal(factor)
    )
    return client


# ---------------------------------------------------------------------------
# Severity classifier
# ---------------------------------------------------------------------------
def test_reflection_severity_enum_has_three_levels():
    assert {ReflectionSeverity.HARD, ReflectionSeverity.SOFT, ReflectionSeverity.NONE}


@pytest.mark.parametrize(
    "note", ["BUDGET_EXHAUSTED", "REFLECTION_ERROR: boom", "ADJUSTED_MISSING_PAYLOAD"]
)
def test_classify_infra_failure_is_hard(note):
    r = _reflection(ReflectionVerdict.REJECTED, bias=["overconfidence"], note=note)
    assert ClaudeClient._classify_reflection_severity(r) == ReflectionSeverity.HARD


@pytest.mark.parametrize(
    "flag", ["fabricated_data", "stale_market_data", "crossed_book"]
)
def test_classify_hard_integrity_flag_is_hard(flag):
    r = _reflection(ReflectionVerdict.REJECTED, risk=[flag])
    assert ClaudeClient._classify_reflection_severity(r) == ReflectionSeverity.HARD


def test_classify_soft_bias_only_is_soft():
    r = _reflection(
        ReflectionVerdict.REJECTED,
        bias=["overconfidence_unsupported", "narrative_anchoring"],
    )
    assert ClaudeClient._classify_reflection_severity(r) == ReflectionSeverity.SOFT


def test_classify_unknown_flag_is_soft():
    r = _reflection(ReflectionVerdict.REJECTED, consistency=["some_novel_flag"])
    assert ClaudeClient._classify_reflection_severity(r) == ReflectionSeverity.SOFT


def test_classify_no_flags_is_none():
    r = _reflection(ReflectionVerdict.REJECTED, note="conservative hold")
    assert ClaudeClient._classify_reflection_severity(r) == ReflectionSeverity.NONE


def test_classify_mixed_soft_and_hard_is_hard():
    r = _reflection(
        ReflectionVerdict.REJECTED,
        bias=["overconfidence"],
        risk=["fabricated_data"],
    )
    assert ClaudeClient._classify_reflection_severity(r) == ReflectionSeverity.HARD


# ---------------------------------------------------------------------------
# Confidence-penalized candidate builder
# ---------------------------------------------------------------------------
def test_penalized_builder_multiplies_confidence():
    out = ClaudeClient._build_confidence_penalized_candidate(
        json.dumps(_primary_candidate(0.9)), Decimal("0.9")
    )
    parsed = json.loads(out)
    assert float(parsed["confidence_score"]) == pytest.approx(0.81)


def test_penalized_builder_non_dict_returns_safely():
    # Must not raise on a degenerate non-dict candidate; result is non-tradeable.
    out = ClaudeClient._build_confidence_penalized_candidate("null", Decimal("0.9"))
    assert out == "null"


# ---------------------------------------------------------------------------
# Verdict application
# ---------------------------------------------------------------------------
def test_apply_verdict_soft_reject_penalizes_confidence():
    client = _client("0.90")
    r = _reflection(ReflectionVerdict.REJECTED, bias=["overconfidence_unsupported"])
    out = client._apply_reflection_verdict(r, json.dumps(_primary_candidate(0.95)))
    parsed = json.loads(out)
    assert float(parsed["confidence_score"]) == pytest.approx(0.855)


def test_apply_verdict_hard_reject_holds():
    client = _client("0.90")
    r = _reflection(ReflectionVerdict.REJECTED, risk=["fabricated_data"])
    out = client._apply_reflection_verdict(r, json.dumps(_primary_candidate(0.95)))
    parsed = json.loads(out)
    assert float(parsed["confidence_score"]) == 0.0
    assert parsed["recommended_action"] == "HOLD"


def test_apply_verdict_infra_reject_holds():
    client = _client("0.90")
    r = _reflection(ReflectionVerdict.REJECTED, note="BUDGET_EXHAUSTED")
    out = client._apply_reflection_verdict(r, json.dumps(_primary_candidate(0.95)))
    parsed = json.loads(out)
    assert float(parsed["confidence_score"]) == 0.0


def test_apply_verdict_approved_unchanged():
    client = _client("0.90")
    r = _reflection(ReflectionVerdict.APPROVED)
    primary = json.dumps(_primary_candidate(0.95))
    out = client._apply_reflection_verdict(r, primary)
    assert json.loads(out)["confidence_score"] == 0.95


# ---------------------------------------------------------------------------
# Terminal Gatekeeper still governs the penalized candidate
# ---------------------------------------------------------------------------
def test_penalized_strong_candidate_passes_gatekeeper():
    client = _client("0.90")
    r = _reflection(ReflectionVerdict.REJECTED, bias=["overconfidence_unsupported"])
    out = client._apply_reflection_verdict(r, json.dumps(_primary_candidate(0.95)))
    resp = LLMEvaluationResponse.model_validate_json(out)  # 0.95 * 0.9 = 0.855
    assert resp.gatekeeper_audit is not None
    assert resp.gatekeeper_audit.all_filters_passed is True
    assert resp.recommended_action == RecommendedAction.BUY


def test_penalized_marginal_candidate_holds_on_min_confidence():
    client = _client("0.90")
    r = _reflection(ReflectionVerdict.REJECTED, bias=["overconfidence_unsupported"])
    out = client._apply_reflection_verdict(r, json.dumps(_primary_candidate(0.80)))
    resp = LLMEvaluationResponse.model_validate_json(out)  # 0.80 * 0.9 = 0.72 < 0.75
    assert resp.recommended_action == RecommendedAction.HOLD
    assert resp.gatekeeper_audit.all_filters_passed is False


# ---------------------------------------------------------------------------
# Config field
# ---------------------------------------------------------------------------
def test_config_factor_default_is_0_90():
    cfg = AppConfig()
    assert cfg.reflection_soft_flag_confidence_factor == Decimal("0.90")


@pytest.mark.parametrize("bad", [Decimal("1.5"), Decimal("0")])
def test_config_factor_rejects_out_of_range(bad):
    with pytest.raises(Exception):
        AppConfig(reflection_soft_flag_confidence_factor=bad)
