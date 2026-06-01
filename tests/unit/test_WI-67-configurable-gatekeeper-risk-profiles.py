"""WI-67 — Configurable Gatekeeper Risk Profiles.

The terminal Gatekeeper (``LLMEvaluationResponse``) historically enforced its
five risk thresholds — plus the Kelly fraction — via hardcoded module-level
constants in ``src.schemas.llm``. That made any operator "risk profile"
(e.g. a less-conservative dry-run experiment) a silent no-op: lowering
``MIN_CONFIDENCE`` in config never reached the terminal validator.

WI-67 lets those knobs be supplied at validation time through Pydantic
validation context. When context is absent or a key is missing, the validator
MUST fall back to the conservative module constants (fail-safe — never loosen a
gate by accident).

Construction in production always flows through ``model_validate_json`` (see
``claude_client`` and ``backtest_runner``), which supports ``context=`` and
propagates it into nested models such as ``ProbabilisticEstimate``.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.agents.evaluation.claude_client import ClaudeClient
from src.backtest_runner import BacktestRunner
from src.schemas.execution import BacktestConfig
from src.schemas.llm import (
    GatekeeperFilter,
    LLMEvaluationResponse,
    RecommendedAction,
)


def _payload(
    *,
    confidence: float = 0.85,
    p_true: float = 0.65,
    p_market: float = 0.45,
    best_bid: float = 0.45,
    best_ask: float = 0.455,
    midpoint: float = 0.4525,
    action: str = "BUY",
    decision: bool = True,
    days_to_resolution: int = 30,
) -> str:
    """Build a raw JSON string that passes structural validation.

    Mirrors the canonical builder in ``tests/conftest.py`` but exposes the
    individual knobs each gatekeeper filter keys off of.
    """
    end_date = (
        datetime.now(timezone.utc) + timedelta(days=days_to_resolution)
    ).isoformat()
    return json.dumps(
        {
            "market_context": {
                "condition_id": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
                "outcome_evaluated": "YES",
                "best_bid": best_bid,
                "best_ask": best_ask,
                "midpoint": midpoint,
                "market_end_date": end_date,
            },
            "probabilistic_estimate": {"p_true": p_true, "p_market": p_market},
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
                "Based on thorough analysis the true probability is estimated "
                "above the market-implied probability, creating a positive "
                "expected value opportunity with adequate confidence."
            ),
        }
    )


# ---------------------------------------------------------------------------
# Fail-safe: no / partial context preserves conservative behavior (regression)
# ---------------------------------------------------------------------------
class TestFailSafeDefaults:
    def test_no_context_preserves_conservative_confidence_gate(self) -> None:
        """A 0.675-confidence positive-EV candidate must still be held with no
        context — identical to today's hardcoded MIN_CONFIDENCE=0.75 behavior."""
        resp = LLMEvaluationResponse.model_validate_json(_payload(confidence=0.675))

        assert resp.recommended_action == RecommendedAction.HOLD
        assert resp.gatekeeper_audit is not None
        assert resp.gatekeeper_audit.all_filters_passed is False
        assert resp.gatekeeper_audit.triggered_filter == GatekeeperFilter.MIN_CONFIDENCE
        assert resp.position_size_pct == 0.0

    def test_partial_context_falls_back_for_missing_keys(self) -> None:
        """Context that omits min_confidence must still apply the conservative
        0.75 floor — a partial profile never loosens an unspecified gate."""
        resp = LLMEvaluationResponse.model_validate_json(
            _payload(confidence=0.675),
            context={"max_spread_pct": 0.05},  # unrelated key; no min_confidence
        )

        assert resp.recommended_action == RecommendedAction.HOLD
        assert resp.gatekeeper_audit.triggered_filter == GatekeeperFilter.MIN_CONFIDENCE

    def test_empty_context_is_conservative(self) -> None:
        resp = LLMEvaluationResponse.model_validate_json(
            _payload(confidence=0.675), context={}
        )
        assert resp.recommended_action == RecommendedAction.HOLD


# ---------------------------------------------------------------------------
# Each configurable gate
# ---------------------------------------------------------------------------
class TestConfigurableConfidence:
    def test_context_lowers_confidence_gate(self) -> None:
        """With min_confidence=0.65, the same 0.675 candidate now passes."""
        resp = LLMEvaluationResponse.model_validate_json(
            _payload(confidence=0.675),
            context={"min_confidence": 0.65},
        )

        assert resp.gatekeeper_audit.all_filters_passed is True
        assert resp.recommended_action == RecommendedAction.BUY
        assert resp.position_size_pct > 0.0


class TestConfigurableEv:
    def test_context_lowers_ev_threshold(self) -> None:
        """EV ~0.01 (p_true 0.505 vs p_market 0.50) is below the 0.02 default
        but clears a configured 0.005 floor."""
        payload = _payload(
            p_true=0.505,
            p_market=0.50,
            best_bid=0.50,
            best_ask=0.505,
            midpoint=0.5025,
        )

        held = LLMEvaluationResponse.model_validate_json(payload)
        assert held.recommended_action == RecommendedAction.HOLD
        assert held.gatekeeper_audit.triggered_filter == (
            GatekeeperFilter.MIN_EV_THRESHOLD
        )

        passed = LLMEvaluationResponse.model_validate_json(
            payload, context={"min_ev_threshold": 0.005}
        )
        assert passed.gatekeeper_audit.all_filters_passed is True
        assert passed.recommended_action == RecommendedAction.BUY

    def test_non_positive_ev_always_blocked_regardless_of_context(self) -> None:
        """The EV>0 hard floor is not configurable; a zero/negative-edge
        candidate is held even with a permissive profile."""
        payload = _payload(
            p_true=0.45,
            p_market=0.50,
            best_bid=0.50,
            best_ask=0.505,
            midpoint=0.5025,
        )
        resp = LLMEvaluationResponse.model_validate_json(
            payload, context={"min_ev_threshold": -1.0}
        )
        assert resp.recommended_action == RecommendedAction.HOLD


class TestConfigurableSpread:
    def test_context_widens_spread_gate(self) -> None:
        """A ~2.2% spread fails the 1.5% default but clears a 3% profile."""
        payload = _payload(best_bid=0.45, best_ask=0.46, midpoint=0.455)

        held = LLMEvaluationResponse.model_validate_json(payload)
        assert held.gatekeeper_audit.triggered_filter == GatekeeperFilter.MAX_SPREAD

        passed = LLMEvaluationResponse.model_validate_json(
            payload, context={"max_spread_pct": 0.03}
        )
        assert passed.gatekeeper_audit.all_filters_passed is True


class TestConfigurableTtr:
    def test_context_lowers_min_ttr(self) -> None:
        """A market resolving in ~2h fails the 4h default but clears a 1h
        profile."""
        # ~2 hours to resolution.
        end_date = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        base = json.loads(_payload())
        base["market_context"]["market_end_date"] = end_date
        payload = json.dumps(base)

        held = LLMEvaluationResponse.model_validate_json(payload)
        assert held.gatekeeper_audit.triggered_filter == (
            GatekeeperFilter.MIN_TIME_TO_RESOLUTION
        )

        passed = LLMEvaluationResponse.model_validate_json(
            payload, context={"min_ttr_hours": 1.0}
        )
        assert passed.gatekeeper_audit.all_filters_passed is True


class TestConfigurableExposureCap:
    def test_context_raises_exposure_cap(self) -> None:
        """A high-edge candidate (kelly_quarter ~0.15) is capped at 0.03 by
        default but can reach 0.10 under an aggressive profile."""
        payload = _payload(
            p_true=0.80,
            p_market=0.50,
            best_bid=0.50,
            best_ask=0.505,
            midpoint=0.5025,
        )

        default = LLMEvaluationResponse.model_validate_json(payload)
        assert default.gatekeeper_audit.all_filters_passed is True
        assert default.position_size_pct == pytest.approx(0.03, abs=1e-9)

        aggressive = LLMEvaluationResponse.model_validate_json(
            payload, context={"max_exposure_pct": 0.10}
        )
        assert aggressive.position_size_pct == pytest.approx(0.10, abs=1e-9)


class TestConfigurableKellyFraction:
    def test_context_scales_kelly_quarter(self) -> None:
        """kelly_fraction must flow into the nested ProbabilisticEstimate so
        the raw kelly sizing doubles when the fraction doubles (0.25 -> 0.50)."""
        payload = _payload(
            p_true=0.80,
            p_market=0.50,
            best_bid=0.50,
            best_ask=0.505,
            midpoint=0.5025,
        )

        default = LLMEvaluationResponse.model_validate_json(payload)
        scaled = LLMEvaluationResponse.model_validate_json(
            payload, context={"kelly_fraction": 0.50}
        )

        pe_default = default.probabilistic_estimate
        pe_scaled = scaled.probabilistic_estimate
        assert pe_scaled.kelly_quarter == pytest.approx(
            pe_default.kelly_quarter * 2.0, rel=1e-6
        )
        # Sanity: kelly_full (unscaled) is unchanged by the fraction.
        assert pe_scaled.kelly_full == pytest.approx(pe_default.kelly_full, rel=1e-6)


# ---------------------------------------------------------------------------
# Wiring: ClaudeClient sources the context from config (closes the no-op bug)
# ---------------------------------------------------------------------------
class TestClaudeClientWiring:
    def _stub(self, **overrides) -> SimpleNamespace:
        cfg = SimpleNamespace(
            min_confidence=0.75,
            min_ev_threshold=0.02,
            max_spread_pct=0.015,
            max_exposure_pct=0.03,
            min_ttr_hours=4.0,
            kelly_fraction=0.25,
        )
        for key, value in overrides.items():
            setattr(cfg, key, value)
        return SimpleNamespace(config=cfg)

    def test_context_mirrors_config(self) -> None:
        """The context dict must reflect config exactly — config is finally the
        single source of truth for the terminal Gatekeeper."""
        ctx = ClaudeClient._risk_profile_context(
            self._stub(min_confidence=0.65, kelly_fraction=0.50)
        )
        assert ctx == {
            "min_confidence": 0.65,
            "min_ev_threshold": 0.02,
            "max_spread_pct": 0.015,
            "max_exposure_pct": 0.03,
            "min_ttr_hours": 4.0,
            "kelly_fraction": 0.50,
        }

    def test_context_omits_absent_config_fields(self) -> None:
        """A config missing a field yields a context without that key, so the
        schema falls back to its conservative constant (fail-safe — and the
        evaluation pipeline never crashes on a partial config)."""
        partial = SimpleNamespace(config=SimpleNamespace(min_confidence=0.65))
        ctx = ClaudeClient._risk_profile_context(partial)
        assert ctx == {"min_confidence": 0.65}

    def test_config_context_unblocks_borderline_candidate(self) -> None:
        """End-to-end contract: a 0.675 candidate that HOLDs under the default
        config is routed when config sets min_confidence=0.65."""
        default_ctx = ClaudeClient._risk_profile_context(self._stub())
        held = LLMEvaluationResponse.model_validate_json(
            _payload(confidence=0.675), context=default_ctx
        )
        assert held.recommended_action == RecommendedAction.HOLD

        aggressive_ctx = ClaudeClient._risk_profile_context(
            self._stub(min_confidence=0.65)
        )
        passed = LLMEvaluationResponse.model_validate_json(
            _payload(confidence=0.675), context=aggressive_ctx
        )
        assert passed.recommended_action == RecommendedAction.BUY


# ---------------------------------------------------------------------------
# Wiring: BacktestRunner exercises the same risk profile through the Gatekeeper
# ---------------------------------------------------------------------------
class TestBacktestWiring:
    def _cfg(self, **overrides) -> BacktestConfig:
        base = dict(
            data_dir="/tmp/unused-by-these-tests",
            initial_bankroll_usdc=Decimal("1000"),
        )
        base.update(overrides)
        return BacktestConfig(**base)

    def test_config_defaults_match_conservative_constants(self) -> None:
        cfg = self._cfg()
        assert cfg.max_spread_pct == Decimal("0.015")
        assert cfg.max_exposure_pct == Decimal("0.03")
        assert cfg.min_ttr_hours == Decimal("4.0")

    def test_config_rejects_float_threshold(self) -> None:
        """Financial-integrity guard extends to the new fields — floats are
        forbidden; thresholds must be Decimal."""
        with pytest.raises(ValidationError):
            self._cfg(max_spread_pct=0.05)

    def test_context_mirrors_config_as_floats(self) -> None:
        cfg = self._cfg(min_confidence=Decimal("0.65"))
        ctx = BacktestRunner._risk_profile_context(SimpleNamespace(config=cfg))
        assert ctx["min_confidence"] == 0.65
        assert ctx["max_spread_pct"] == 0.015
        assert all(isinstance(v, float) for v in ctx.values())

    def test_aggressive_profile_unblocks_candidate(self) -> None:
        aggressive = self._cfg(min_confidence=Decimal("0.65"))
        ctx = BacktestRunner._risk_profile_context(SimpleNamespace(config=aggressive))
        resp = LLMEvaluationResponse.model_validate_json(
            _payload(confidence=0.675), context=ctx
        )
        assert resp.recommended_action == RecommendedAction.BUY
