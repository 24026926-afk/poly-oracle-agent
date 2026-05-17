"""
src/schemas/provider_comparison.py

WI-55 Pydantic V2 schemas for LLM provider comparison, readiness verdict
derivation, and paper-trading calibration reports.

All money, token-cost, EV, PnL, and provider spend fields use ``Decimal``.
Raw ``float`` is rejected at the Pydantic boundary.
All models are frozen and secret-free.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")


def _reject_float_decimal(value: Any) -> Any:
    """Reject raw float; coerce str/int to Decimal."""
    if value is None:
        return value
    if isinstance(value, float):
        raise ValueError("Float financial values are forbidden; use Decimal")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Readiness Enums
# ---------------------------------------------------------------------------


class LLMProviderReadinessVerdict(str, Enum):
    """Typed, deterministic DeepSeek readiness verdict.

    Ordered from most severe rejection to least severe recommendation.
    Derivation must check in this order and return the FIRST match.
    """

    PROVIDER_REJECTED_FOR_JSON_VALIDITY = "PROVIDER_REJECTED_FOR_JSON_VALIDITY"
    PROVIDER_REJECTED_FOR_NEGATIVE_EV = "PROVIDER_REJECTED_FOR_NEGATIVE_EV"
    PROVIDER_REJECTED_FOR_COST_OR_LATENCY = "PROVIDER_REJECTED_FOR_COST_OR_LATENCY"
    PROVIDER_NEEDS_THRESHOLD_RECALIBRATION = "PROVIDER_NEEDS_THRESHOLD_RECALIBRATION"
    PROVIDER_READY_FOR_SAMPLED_AUDIT_ONLY = "PROVIDER_READY_FOR_SAMPLED_AUDIT_ONLY"
    PROVIDER_READY_FOR_DRY_RUN_PRIMARY = "PROVIDER_READY_FOR_DRY_RUN_PRIMARY"


class LLMProviderReadinessReason(str, Enum):
    """Typed reason codes for readiness verdict derivation."""

    JSON_VALIDITY_BELOW_TOLERANCE = "JSON_VALIDITY_BELOW_TOLERANCE"
    GATEKEEPER_FAILURE_RATE_HIGH = "GATEKEEPER_FAILURE_RATE_HIGH"
    NEGATIVE_REALIZED_EV = "NEGATIVE_REALIZED_EV"
    COST_EXCEEDS_BASELINE = "COST_EXCEEDS_BASELINE"
    LATENCY_EXCEEDS_BASELINE = "LATENCY_EXCEEDS_BASELINE"
    WEAK_CONFIDENCE_CALIBRATION = "WEAK_CONFIDENCE_CALIBRATION"
    WEAK_EV_CALIBRATION = "WEAK_EV_CALIBRATION"
    ALL_GATES_PASSED = "ALL_GATES_PASSED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    OUTCOME_COVERAGE_MISSING = "OUTCOME_COVERAGE_MISSING"


# ---------------------------------------------------------------------------
# Comparison Config
# ---------------------------------------------------------------------------


class LLMProviderComparisonConfig(BaseModel):
    """Frozen configuration for a provider comparison run.

    ``dry_run`` must be ``True`` — the config fails validation otherwise.
    """

    dry_run: bool = True
    data_dir: str = Field(..., min_length=1)
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    initial_bankroll_usdc: Decimal = Field(default=Decimal("1000"), gt=0)
    enable_anthropic_sampling: bool = False
    anthropic_sample_fraction: Decimal = Field(default=Decimal("0"), ge=0, le=1)
    json_validity_tolerance: Decimal = Field(
        default=Decimal("0.10"),
        ge=0,
        le=1,
        description="Max fraction of invalid JSON responses tolerated",
    )
    calibration_deviation_max: Decimal = Field(
        default=Decimal("0.25"),
        ge=0,
        le=1,
    )
    ev_deviation_max: Decimal = Field(
        default=Decimal("0.15"),
        ge=0,
        le=1,
    )
    cost_reduction_required: Decimal = Field(
        default=Decimal("0.20"),
        ge=0,
        le=1,
        description="Min cost reduction fraction vs Anthropic for READY verdict",
    )
    latency_multiplier_max: Decimal = Field(
        default=Decimal("3.0"),
        ge=Decimal("1.0"),
        description="Max latency multiplier vs Anthropic baseline",
    )
    min_trades_for_verdict: int = Field(default=20, ge=1)
    confidence_buckets: list[Decimal] = Field(
        default_factory=lambda: [
            Decimal("0.0"),
            Decimal("0.2"),
            Decimal("0.4"),
            Decimal("0.6"),
            Decimal("0.8"),
            Decimal("1.0"),
        ],
    )
    confidence_min_populated: int = Field(default=2, ge=1)
    anthropic_model: str = Field(default="claude-sonnet-4-20250514", min_length=1)
    deepseek_model: str = Field(default="deepseek-chat", min_length=1)
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com/anthropic", min_length=1
    )
    fallback_tokens_per_call: int = Field(default=4096, gt=0)
    cost_per_input_token_usd: Decimal = Field(
        default=Decimal("0.0000015"),
        ge=0,
        description="Cost per input token in USD (conservative default ~$1.50/M)",
    )
    cost_per_output_token_usd: Decimal = Field(
        default=Decimal("0.000006"),
        ge=0,
        description="Cost per output token in USD (conservative default ~$6.00/M)",
    )
    anthropic_baseline_cost_usd: Decimal = Field(
        default=Decimal("3.00"),
        gt=0,
        description="Audit-grade Anthropic baseline cost in USD for cost comparison",
    )
    anthropic_baseline_latency_ms: float = Field(
        default=1200.0,
        gt=0,
        description="Audit-grade Anthropic baseline mean latency in ms",
    )
    report_output_dir: str = Field(default="docs/backtests", min_length=1)

    @field_validator(
        "initial_bankroll_usdc",
        "anthropic_sample_fraction",
        "json_validity_tolerance",
        "calibration_deviation_max",
        "ev_deviation_max",
        "cost_reduction_required",
        "latency_multiplier_max",
        "cost_per_input_token_usd",
        "cost_per_output_token_usd",
        "anthropic_baseline_cost_usd",
        mode="before",
    )
    @classmethod
    def _reject_float_decimal_fields(cls, value: Any) -> Any:
        return _reject_float_decimal(value)

    @field_validator("confidence_buckets", mode="before")
    @classmethod
    def _coerce_bucket_floats(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        return [Decimal(str(v)) for v in value]

    @model_validator(mode="after")
    def _validate_dry_run_required(self) -> "LLMProviderComparisonConfig":
        if self.dry_run is not True:
            raise ValueError(
                "LLMProviderComparisonConfig requires dry_run=True. "
                "Live trading comparison is not supported."
            )
        return self

    @model_validator(mode="after")
    def _validate_sampling_bounded(self) -> "LLMProviderComparisonConfig":
        if self.enable_anthropic_sampling and self.anthropic_sample_fraction <= _ZERO:
            raise ValueError(
                "anthropic_sample_fraction must be > 0 when enable_anthropic_sampling is True"
            )
        return self

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Decision Metrics
# ---------------------------------------------------------------------------


class LLMProviderDecisionMetrics(BaseModel):
    """Per-provider decision-level metrics from a comparison run.

    All rate/ratio fields are Decimal and bounded [0,1].
    """

    total_calls: int = Field(default=0, ge=0)
    valid_json_count: int = Field(default=0, ge=0)
    invalid_json_count: int = Field(default=0, ge=0)
    json_validity_rate: Decimal = Field(default=_ZERO, ge=0, le=1)
    gatekeeper_passed: int = Field(default=0, ge=0)
    gatekeeper_failed: int = Field(default=0, ge=0)
    gatekeeper_pass_rate: Decimal = Field(default=_ZERO, ge=0, le=1)
    buy_count: int = Field(default=0, ge=0)
    hold_count: int = Field(default=0, ge=0)
    skip_count: int = Field(default=0, ge=0)
    sell_count: int = Field(default=0, ge=0)

    @field_validator(
        "json_validity_rate",
        "gatekeeper_pass_rate",
        mode="before",
    )
    @classmethod
    def _reject_float_rates(cls, value: Any) -> Any:
        return _reject_float_decimal(value)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Calibration Metrics
# ---------------------------------------------------------------------------


class LLMProviderCalibrationMetrics(BaseModel):
    """Per-provider calibration quality metrics.

    Confidence calibration is measured per bucket; EV calibration compares
    average EV against realized PnL where historical outcomes exist.
    """

    confidence_bucket_low: list[Decimal] = Field(default_factory=list)
    confidence_bucket_high: list[Decimal] = Field(default_factory=list)
    confidence_bucket_avg: list[Decimal] = Field(default_factory=list)
    confidence_bucket_observed_win_rate: list[Decimal] = Field(default_factory=list)
    confidence_bucket_count: list[int] = Field(default_factory=list)
    confidence_calibration_deviation: Decimal = Field(default=_ZERO, ge=0)
    avg_ev: Decimal = Field(default=_ZERO)
    avg_realized_return: Decimal = Field(default=_ZERO)
    ev_calibration_deviation: Decimal = Field(default=_ZERO, ge=0)
    outcome_coverage_fraction: Decimal = Field(
        default=_ZERO,
        ge=0,
        le=1,
        description="Fraction of decisions with available historical outcomes",
    )
    has_outcome_data: bool = Field(default=False)

    @field_validator(
        "confidence_bucket_avg",
        "confidence_bucket_observed_win_rate",
        "confidence_calibration_deviation",
        "avg_ev",
        "avg_realized_return",
        "ev_calibration_deviation",
        "outcome_coverage_fraction",
        mode="before",
    )
    @classmethod
    def _reject_float_decimal_intensive(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [Decimal(str(v)) for v in value]
        return _reject_float_decimal(value)

    @field_validator(
        "confidence_bucket_low",
        "confidence_bucket_high",
        mode="before",
    )
    @classmethod
    def _coerce_bucket_edges(cls, value: Any) -> Any:
        if isinstance(value, list):
            return [Decimal(str(v)) for v in value]
        return value

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Cost Metrics
# ---------------------------------------------------------------------------


class LLMProviderCostMetrics(BaseModel):
    """Per-provider cost and token usage metrics.

    All cost fields are Decimal; budget/cooldown blocks are captured as
    counts (not treated as valid provider responses).

    **Conservative accounting invariant:** When ``total_tokens > 0``,
    either ``is_estimated`` must be ``True`` (conservative fallback was
    used for missing/malformed usage data) or ``total_estimated_cost_usd``
    must be ``> 0`` (actual cost data is available).  This prevents
    missing usage from silently becoming zero-cost accounting.
    """

    total_input_tokens: int = Field(default=0, ge=0)
    total_output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    total_estimated_cost_usd: Decimal = Field(default=_ZERO, ge=0)
    is_estimated: bool = Field(default=False)
    budget_block_count: int = Field(default=0, ge=0)
    cooldown_block_count: int = Field(default=0, ge=0)
    calls_with_missing_usage: int = Field(default=0, ge=0)

    @field_validator("total_estimated_cost_usd", mode="before")
    @classmethod
    def _reject_float_cost(cls, value: Any) -> Any:
        return _reject_float_decimal(value)

    @model_validator(mode="after")
    def _enforce_conservative_cost_accounting(self) -> "LLMProviderCostMetrics":
        if self.calls_with_missing_usage > 0 and not self.is_estimated:
            raise ValueError(
                "calls_with_missing_usage > 0 but is_estimated is False. "
                "Missing usage data must use conservative fallback accounting."
            )
        if self.total_tokens > 0 and self.total_estimated_cost_usd <= _ZERO:
            if not self.is_estimated:
                raise ValueError(
                    "total_tokens > 0 but total_estimated_cost_usd is zero "
                    "and is_estimated is False. Missing cost data must either "
                    "report a non-zero estimated cost or set is_estimated=True."
                )
        return self

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Latency Metrics
# ---------------------------------------------------------------------------


class LLMProviderLatencyMetrics(BaseModel):
    """Per-provider latency distribution metrics in milliseconds."""

    min_latency_ms: float = Field(default=0.0, ge=0)
    max_latency_ms: float = Field(default=0.0, ge=0)
    mean_latency_ms: float = Field(default=0.0, ge=0)
    median_latency_ms: float = Field(default=0.0, ge=0)
    p95_latency_ms: float = Field(default=0.0, ge=0)
    p99_latency_ms: float = Field(default=0.0, ge=0)
    sample_count: int = Field(default=0, ge=0)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Calibration Recommendation
# ---------------------------------------------------------------------------


class LLMProviderCalibrationRecommendation(BaseModel):
    """Provider-specific, advisory calibration recommendations.

    These are suggestions for the operator — they do NOT mutate runtime
    configuration automatically. Operator action is required.
    """

    provider: str = Field(..., min_length=1)
    model_name: str = Field(..., min_length=1)
    suggested_confidence_threshold: Optional[Decimal] = Field(default=None, ge=0, le=1)
    suggested_ev_threshold: Optional[Decimal] = Field(default=None, ge=0)
    suggested_max_output_tokens: Optional[int] = Field(default=None, gt=0)
    suggested_cooldown_seconds: Optional[int] = Field(default=None, gt=0)
    suggested_budget_daily_cost_limit_usd: Optional[Decimal] = Field(default=None, ge=0)
    reasoning: str = Field(default="", min_length=0)

    @field_validator(
        "suggested_confidence_threshold",
        "suggested_ev_threshold",
        "suggested_budget_daily_cost_limit_usd",
        mode="before",
    )
    @classmethod
    def _reject_float_suggestions(cls, value: Any) -> Any:
        return _reject_float_decimal(value)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Comparison Result (per-provider)
# ---------------------------------------------------------------------------


class LLMProviderComparisonResult(BaseModel):
    """Per-provider result container for a single comparison run."""

    provider: str = Field(..., min_length=1)
    model_name: str = Field(..., min_length=1)
    decision_metrics: LLMProviderDecisionMetrics
    calibration_metrics: LLMProviderCalibrationMetrics
    cost_metrics: LLMProviderCostMetrics
    latency_metrics: LLMProviderLatencyMetrics
    readiness_verdict: LLMProviderReadinessVerdict
    readiness_reasons: list[LLMProviderReadinessReason] = Field(default_factory=list)
    calibration_recommendation: Optional[LLMProviderCalibrationRecommendation] = None

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Comparison Run container
# ---------------------------------------------------------------------------


class LLMProviderComparisonRun(BaseModel):
    """Container for a complete provider comparison execution run."""

    config: LLMProviderComparisonConfig
    started_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    completed_at_utc: Optional[datetime] = None
    results: list[LLMProviderComparisonResult] = Field(default_factory=list)
    error_message: Optional[str] = None

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Comparison Report (top-level)
# ---------------------------------------------------------------------------

_FORBIDDEN_SECRET_PATTERNS: list[str] = [
    "sk-ant-",
    "sk-or-",
    "sk-ds-",
    "ankr-",  # API key prefixes
    "xai-",
    "grok-",  # Grok key prefixes
    "condition_id",
    "token_id",  # high-cardinality ID field names
    "prompt:",
    "reasoning:",
    "reasoning_log",  # raw prompt/reasoning field names
]

# Regex patterns for CLOB token/condition IDs in free-form text — these are
# long hex strings (0x + 40+ hex chars) that represent on-chain identifiers.
_HEX_ID_PATTERN = r"0x[0-9a-fA-F]{40,}"


def _contains_forbidden_pattern(text: str) -> bool:
    """Check if text contains any known secret or high-cardinality patterns."""
    import re

    lower = text.lower()
    for pattern in _FORBIDDEN_SECRET_PATTERNS:
        if pattern.lower() in lower:
            return True
    # Check for long CLOB hex identifiers embedded in free-form text
    if re.search(_HEX_ID_PATTERN, text, re.IGNORECASE):
        return True
    return False


class LLMProviderComparisonReport(BaseModel):
    """Top-level frozen report contract for WI-55 provider comparison.

    All money, EV, PnL, and cost fields are Decimal. The report is
    secret-free — raw prompts, reasoning text, API keys, wallet keys,
    token IDs, and condition IDs are forbidden.
    """

    run: LLMProviderComparisonRun
    generated_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    @field_validator("run")
    @classmethod
    def _validate_run_config_dry_run(cls, value: LLMProviderComparisonRun) -> Any:
        if not value.config.dry_run:
            raise ValueError("Report requires comparison config with dry_run=True")
        return value

    @model_validator(mode="after")
    def _validate_secret_free(self) -> "LLMProviderComparisonReport":
        serialized = self.model_dump_json()
        if _contains_forbidden_pattern(serialized):
            raise ValueError(
                "Comparison report contains forbidden secret or "
                "high-cardinality patterns (API keys, token IDs, "
                "condition IDs, raw prompts, reasoning text)."
            )
        return self

    model_config = {"frozen": True}
