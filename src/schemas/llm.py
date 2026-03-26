"""
src/schemas/llm.py

Pydantic V2 schemas for the LLM Evaluation Node output.
This module IS the Gatekeeper. It sits between the LLM response and the
Web3 Execution Node and enforces all risk rules as validator logic.
No downstream code is responsible for re-validating these invariants.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Risk Parameter Constants
# ---------------------------------------------------------------------------
KELLY_FRACTION: float = 0.25       # Quarter-Kelly multiplier
MIN_CONFIDENCE: float = 0.75       # Filter 1: LLM epistemic confidence floor
MAX_SPREAD_PCT: float = 0.015      # Filter 2: Max bid-ask spread (1.5%)
MAX_EXPOSURE_PCT: float = 0.03     # Filter 3: Max bankroll fraction per trade (3%)
MIN_EV_THRESHOLD: float = 0.02     # Filter 4: Min positive EV (2% edge floor)
MIN_TTR_HOURS: float = 4.0         # Filter 5: Min hours until market resolution

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------
class RecommendedAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"

class OutcomeLabel(str, Enum):
    YES = "YES"
    NO = "NO"

class GatekeeperFilter(str, Enum):
    EV_NON_POSITIVE = "EV_NON_POSITIVE"
    MIN_CONFIDENCE = "MIN_CONFIDENCE"
    MAX_SPREAD = "MAX_SPREAD"
    MAX_EXPOSURE = "MAX_EXPOSURE"
    MIN_EV_THRESHOLD = "MIN_EV_THRESHOLD"
    MIN_TIME_TO_RESOLUTION = "MIN_TIME_TO_RESOLUTION"

class MarketCategory(str, Enum):
    CRYPTO = "CRYPTO"
    POLITICS = "POLITICS"
    SPORTS = "SPORTS"
    GENERAL = "GENERAL"

# ---------------------------------------------------------------------------
# Sub-schema: MarketContext
# ---------------------------------------------------------------------------
class MarketContext(BaseModel):
    condition_id: str = Field(..., min_length=10)
    outcome_evaluated: OutcomeLabel
    best_bid: Annotated[float, Field(gt=0.0, lt=1.0)]
    best_ask: Annotated[float, Field(gt=0.0, lt=1.0)]
    midpoint: Annotated[float, Field(gt=0.0, lt=1.0)]
    market_end_date: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_bid_ask_ordering(self) -> "MarketContext":
        if self.best_ask < self.best_bid:
            raise ValueError(f"best_ask ({self.best_ask}) must be >= best_bid ({self.best_bid})")
        return self

    @property
    def spread_pct(self) -> float:
        return (self.best_ask - self.best_bid) / self.best_ask

    @property
    def hours_to_resolution(self) -> Optional[float]:
        if self.market_end_date is None:
            return None
        now = datetime.now(timezone.utc)
        delta = self.market_end_date - now
        return delta.total_seconds() / 3600.0

    model_config = {"frozen": True}

# ---------------------------------------------------------------------------
# Sub-schema: ProbabilisticEstimate
# ---------------------------------------------------------------------------
class ProbabilisticEstimate(BaseModel):
    p_true: Annotated[float, Field(ge=0.01, le=0.99)]
    p_market: Annotated[float, Field(gt=0.0, lt=1.0)]

    net_odds_b: Optional[float] = Field(default=None)
    expected_value: Optional[float] = Field(default=None)
    kelly_full: Optional[float] = Field(default=None)
    kelly_quarter: Optional[float] = Field(default=None)

    @model_validator(mode="after")
    def compute_kelly_and_ev(self) -> "ProbabilisticEstimate":
        p: float = self.p_true
        q: float = 1.0 - p
        p_mkt: float = self.p_market

        b: float = (1.0 - p_mkt) / p_mkt
        ev: float = p * b - q
        f_star: float = max(0.0, (b * p - q) / b)
        f_quarter: float = KELLY_FRACTION * f_star

        object.__setattr__(self, "net_odds_b", round(b, 6))
        object.__setattr__(self, "expected_value", round(ev, 6))
        object.__setattr__(self, "kelly_full", round(f_star, 6))
        object.__setattr__(self, "kelly_quarter", round(f_quarter, 6))

        return self

    model_config = {"frozen": True}

# ---------------------------------------------------------------------------
# Sub-schema: RiskAssessment
# ---------------------------------------------------------------------------
class RiskAssessment(BaseModel):
    liquidity_risk_score: Annotated[float, Field(ge=0.0, le=1.0)]
    resolution_risk_score: Annotated[float, Field(ge=0.0, le=1.0)]
    information_asymmetry_flag: bool
    risk_notes: str = Field(..., min_length=20)

    model_config = {"frozen": True}

# ---------------------------------------------------------------------------
# Sub-schema: GatekeeperAudit
# ---------------------------------------------------------------------------
class GatekeeperAudit(BaseModel):
    all_filters_passed: bool
    triggered_filter: Optional[GatekeeperFilter] = None
    computed_ev: float
    computed_kelly_full: float
    computed_kelly_quarter: float
    computed_spread_pct: float
    final_position_size_pct: float
    override_applied: bool = False

    def to_log_prefix(self) -> str:
        status = "PASS" if self.all_filters_passed else f"HOLD | filter={self.triggered_filter.value}"
        return (
            f"[GATEKEEPER] {status} | "
            f"ev={self.computed_ev:.4f} | "
            f"kelly_q={self.computed_kelly_quarter:.4f} | "
            f"spread={self.computed_spread_pct:.4f} | "
            f"pos_size={self.final_position_size_pct:.4f} | "
            f"override={self.override_applied}"
        )

    model_config = {"frozen": True}

# ---------------------------------------------------------------------------
# Primary Schema: LLMEvaluationResponse  (THE GATEKEEPER)
# ---------------------------------------------------------------------------
class LLMEvaluationResponse(BaseModel):
    market_context: MarketContext
    probabilistic_estimate: ProbabilisticEstimate
    risk_assessment: RiskAssessment

    confidence_score: Annotated[float, Field(ge=0.0, le=1.0)]
    decision_boolean: bool
    recommended_action: RecommendedAction
    reasoning_log: str = Field(..., min_length=50)

    expected_value: float = Field(default=0.0)
    position_size_pct: float = Field(default=0.0)
    gatekeeper_audit: Optional[GatekeeperAudit] = Field(default=None)

    @model_validator(mode="after")
    def _compute_ev_and_kelly(self) -> "LLMEvaluationResponse":
        pe = self.probabilistic_estimate
        object.__setattr__(self, "expected_value", pe.expected_value)
        return self

    @model_validator(mode="after")
    def _apply_gatekeeper_filters(self) -> "LLMEvaluationResponse":
        pe = self.probabilistic_estimate
        mc = self.market_context
        ra = self.risk_assessment

        ev: float = pe.expected_value  # type: ignore[assignment]
        kelly_full: float = pe.kelly_full  # type: ignore[assignment]
        kelly_q: float = pe.kelly_quarter  # type: ignore[assignment]
        spread_pct: float = mc.spread_pct
        ttr: Optional[float] = mc.hours_to_resolution

        triggered: Optional[GatekeeperFilter] = None
        all_passed: bool = True

        if ev <= 0.0:
            triggered = GatekeeperFilter.EV_NON_POSITIVE
            all_passed = False
        elif ev < MIN_EV_THRESHOLD:
            triggered = GatekeeperFilter.MIN_EV_THRESHOLD
            all_passed = False
        elif self.confidence_score < MIN_CONFIDENCE:
            triggered = GatekeeperFilter.MIN_CONFIDENCE
            all_passed = False
        elif spread_pct > MAX_SPREAD_PCT:
            triggered = GatekeeperFilter.MAX_SPREAD
            all_passed = False
        elif ttr is not None and ttr < MIN_TTR_HOURS:
            triggered = GatekeeperFilter.MIN_TIME_TO_RESOLUTION
            all_passed = False

        if ra.information_asymmetry_flag and all_passed:
            kelly_q = kelly_q * 0.5

        final_pos = min(kelly_q, MAX_EXPOSURE_PCT) if all_passed else 0.0
        final_pos = max(0.0, final_pos)

        audit = GatekeeperAudit(
            all_filters_passed=all_passed,
            triggered_filter=triggered,
            computed_ev=round(ev, 6),
            computed_kelly_full=round(kelly_full, 6),
            computed_kelly_quarter=round(kelly_q, 6),
            computed_spread_pct=round(spread_pct, 6),
            final_position_size_pct=round(final_pos, 6),
            override_applied=False,
        )

        object.__setattr__(self, "gatekeeper_audit", audit)
        object.__setattr__(self, "position_size_pct", final_pos)
        return self

    @model_validator(mode="after")
    def _enforce_decision_override(self) -> "LLMEvaluationResponse":
        audit = self.gatekeeper_audit
        assert audit is not None

        llm_wanted_to_trade = self.decision_boolean or (self.recommended_action != RecommendedAction.HOLD)
        should_hold = not audit.all_filters_passed or self.position_size_pct == 0.0

        if should_hold:
            override_flag = llm_wanted_to_trade
            updated_audit = GatekeeperAudit(
                all_filters_passed=audit.all_filters_passed,
                triggered_filter=audit.triggered_filter,
                computed_ev=audit.computed_ev,
                computed_kelly_full=audit.computed_kelly_full,
                computed_kelly_quarter=audit.computed_kelly_quarter,
                computed_spread_pct=audit.computed_spread_pct,
                final_position_size_pct=0.0,
                override_applied=override_flag,
            )

            enriched_log = f"{updated_audit.to_log_prefix()}\n\n{self.reasoning_log}"

            object.__setattr__(self, "decision_boolean", False)
            object.__setattr__(self, "recommended_action", RecommendedAction.HOLD)
            object.__setattr__(self, "position_size_pct", 0.0)
            object.__setattr__(self, "gatekeeper_audit", updated_audit)
            object.__setattr__(self, "reasoning_log", enriched_log)
        else:
            enriched_log = f"{audit.to_log_prefix()}\n\n{self.reasoning_log}"
            object.__setattr__(self, "reasoning_log", enriched_log)

        return self

    @model_validator(mode="after")
    def _validate_final_consistency(self) -> "LLMEvaluationResponse":
        if self.decision_boolean and self.recommended_action == RecommendedAction.HOLD:
            raise AssertionError("[BUG] decision_boolean=True but recommended_action=HOLD after override.")
        if not self.decision_boolean and self.position_size_pct > 0.0:
            raise AssertionError(f"[BUG] decision_boolean=False but position_size_pct={self.position_size_pct} > 0.")
        if self.decision_boolean and self.expected_value <= 0.0:
            raise AssertionError(f"[BUG] decision_boolean=True but EV={self.expected_value} <= 0 slipped through.")
        return self

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": False,
        "frozen": True,
    }