"""
src/schemas/llm.py

Pydantic V2 schemas for the LLM Evaluation Node output.
This module IS the Gatekeeper. It sits between the LLM response and the
Web3 Execution Node and enforces all risk rules as validator logic.
No downstream code is responsible for re-validating these invariants.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Annotated, Optional

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator


# ---------------------------------------------------------------------------
# Decimal safety helpers
# ---------------------------------------------------------------------------


def _recursive_float_to_decimal(obj: object) -> object:
    """Recursively traverse a dict/list structure and convert every ``float``
    to ``Decimal(str(val))`` so that no raw float survives into money-path
    arithmetic.  Non-float primitives (str, int, bool, None) pass through."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _recursive_float_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_recursive_float_to_decimal(item) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Risk Parameter Constants
# ---------------------------------------------------------------------------
KELLY_FRACTION: float = 0.25  # Quarter-Kelly multiplier
MIN_CONFIDENCE: float = 0.75  # Filter 1: LLM epistemic confidence floor
MAX_SPREAD_PCT: float = 0.015  # Filter 2: Max bid-ask spread (1.5%)
MAX_EXPOSURE_PCT: float = 0.03  # Filter 3: Max bankroll fraction per trade (3%)
MIN_EV_THRESHOLD: float = 0.02  # Filter 4: Min positive EV (2% edge floor)
MIN_TTR_HOURS: float = 4.0  # Filter 5: Min hours until market resolution


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
    POLITICS = "POLITICS"
    SPORTS = "SPORTS"
    CRYPTO = "CRYPTO"
    ESPORTS = "ESPORTS"
    IRAN = "IRAN"
    FINANCE = "FINANCE"
    GEOPOLITICS = "GEOPOLITICS"
    TECH = "TECH"
    CULTURE = "CULTURE"
    ECONOMY = "ECONOMY"
    WEATHER = "WEATHER"
    MENTIONS = "MENTIONS"
    ELECTIONS = "ELECTIONS"


# Categories where X/Twitter discourse provides actionable Grok sentiment signal.
# This is the authoritative eligibility set — both ClaudeClient and GrokClient
# must import from here to avoid drift.
GROK_ELIGIBLE_CATEGORIES: frozenset["MarketCategory"] = frozenset(
    {
        MarketCategory.CRYPTO,
        MarketCategory.POLITICS,
        MarketCategory.ELECTIONS,
        MarketCategory.GEOPOLITICS,
        MarketCategory.FINANCE,
        MarketCategory.TECH,
        MarketCategory.IRAN,
        MarketCategory.ECONOMY,
        MarketCategory.CULTURE,
    }
)


class ReflectionVerdict(str, Enum):
    APPROVED = "APPROVED"
    ADJUSTED = "ADJUSTED"
    REJECTED = "REJECTED"


# ---------------------------------------------------------------------------
# Sub-schema: ReflectionResponse (Stage C — Reflection Auditor output)
# ---------------------------------------------------------------------------
class ReflectionResponse(BaseModel):
    """Validated Stage C reflection audit verdict.
    This is an upstream quality-control signal only — LLMEvaluationResponse
    remains the terminal Gatekeeper gate."""

    verdict: ReflectionVerdict
    bias_flags: list[str] = Field(default_factory=list)
    consistency_flags: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    audit_note: str
    correction_instructions: Optional[str] = None
    corrected_candidate_json: Optional[dict] = None
    latency_ms: int = Field(default=0, ge=0)

    @field_validator("corrected_candidate_json", mode="before")
    @classmethod
    def _sanitize_floats_to_decimal(cls, v: object) -> object:
        """Convert every float in the corrected candidate to Decimal(str(val))
        so that no raw float leaks into money-path re-serialization."""
        if v is None:
            return v
        return _recursive_float_to_decimal(v)

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Sub-schema: SentimentResponse (Stage A — Grok Oracle output)
# ---------------------------------------------------------------------------
class SentimentResponse(BaseModel):
    """Validated Stage A sentiment artifact from the Grok Sentiment Oracle.
    This is an upstream cognitive signal only — it does NOT replace or modify
    the Gatekeeper (LLMEvaluationResponse) terminal validation gate."""

    sentiment_score: Decimal = Field(ge=Decimal("-1.0"), le=Decimal("1.0"))
    tweet_volume_delta: int = Field(ge=-10000, le=10000)
    top_narrative_summary: str = Field(min_length=10, max_length=320)

    @field_validator("sentiment_score", mode="before")
    @classmethod
    def _parse_decimal(cls, v: object) -> Decimal:
        if isinstance(v, float):
            raise ValueError(
                f"Float financial values are forbidden at schema boundary; "
                f"got float {v}. Use Decimal or str."
            )
        return Decimal(str(v))

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# WI-45: Grok Live Sentiment typed models
# ---------------------------------------------------------------------------


class GrokFailureReason(str, Enum):  # noqa: H312 (intentional str mixin)
    """Typed failure reasons for Grok sentiment fallback audit trail."""

    TIMEOUT = "timeout"
    HTTP_401 = "http_401"
    HTTP_403 = "http_403"
    HTTP_429 = "http_429"
    HTTP_5XX = "http_5xx"
    SCHEMA_ERROR = "schema_error"
    MISSING_KEY = "missing_key"
    LIVE_DISABLED = "live_disabled"
    SAFETY_REFUSAL = "safety_refusal"
    UNEXPECTED = "unexpected"


class GrokLiveConfig(BaseModel):
    """Live-mode configuration capsule for GrokClient."""

    live_enabled: bool = False
    timeout_seconds: float = Field(default=2.0, gt=0.0, le=30.0)
    max_retries: int = Field(default=2, ge=0, le=5)

    model_config = {"frozen": True}


class GrokRequestEnvelope(BaseModel):
    """Typed request capsule for a live Grok API call."""

    headers: dict[str, str]
    body: dict

    model_config = {"frozen": True}


class GrokResponseEnvelope(BaseModel):
    """Typed response envelope from a Grok API call before sentiment parsing."""

    raw_text: str
    status_code: int

    model_config = {"frozen": True}


# ---------------------------------------------------------------------------
# Sub-schema: MarketContext
# ---------------------------------------------------------------------------
class MarketContext(BaseModel):
    condition_id: str = Field(..., min_length=10)
    yes_token_id: Optional[str] = None
    outcome_evaluated: OutcomeLabel
    best_bid: Annotated[float, Field(gt=0.0, lt=1.0)]
    best_ask: Annotated[float, Field(gt=0.0, lt=1.0)]
    midpoint: Annotated[float, Field(gt=0.0, lt=1.0)]
    market_end_date: Optional[datetime] = None

    @model_validator(mode="after")
    def validate_bid_ask_ordering(self) -> "MarketContext":
        if self.best_ask < self.best_bid:
            raise ValueError(
                f"best_ask ({self.best_ask}) must be >= best_bid ({self.best_bid})"
            )
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
        status = (
            "PASS"
            if self.all_filters_passed
            else f"HOLD | filter={self.triggered_filter.value}"
        )
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
# WI-52: LLM Cost Guard and Cognitive Circuit Breaker schemas
# ---------------------------------------------------------------------------


class LLMProviderName(str, Enum):
    """Supported LLM provider identifiers."""

    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"


class LLMBudgetBlockReason(str, Enum):
    """Typed reasons for LLM budget blocks."""

    HOURLY_CALL_LIMIT_EXHAUSTED = "hourly_call_limit_exhausted"
    REFLECTION_HOURLY_CALL_LIMIT_EXHAUSTED = "reflection_hourly_call_limit_exhausted"
    DAILY_CALL_LIMIT_EXHAUSTED = "daily_call_limit_exhausted"
    DAILY_TOKEN_LIMIT_EXHAUSTED = "daily_token_limit_exhausted"
    DAILY_COST_LIMIT_EXHAUSTED = "daily_cost_limit_exhausted"
    PER_MARKET_HOURLY_LIMIT_EXHAUSTED = "per_market_hourly_limit_exhausted"


class MarketCooldownReason(str, Enum):
    """Typed reasons for per-market cognitive cooldown."""

    REPEATED_HOLD = "repeated_hold"
    REPEATED_INVALID_JSON = "repeated_invalid_json"
    REPEATED_LOW_CONFIDENCE = "repeated_low_confidence"
    REPEATED_SKIP = "repeated_skip"
    REPEATED_PROVIDER_ERROR = "repeated_provider_error"


class LLMUsageRecord(BaseModel):
    """Provider usage record for a single LLM call.

    All cost/token fields use Decimal — raw float is forbidden.
    """

    model_config = {"frozen": True}

    provider: LLMProviderName
    model_name: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    is_estimated: bool = Field(
        default=False,
        description="True when usage fields were missing/malformed and fallback was used",
    )
    timestamp_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("estimated_cost_usd", mode="before")
    @classmethod
    def _reject_float_cost(cls, v: object) -> Decimal:
        if isinstance(v, float):
            raise ValueError("Float cost values are forbidden; use Decimal")
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class LLMBudgetConfig(BaseModel):
    """Configuration for LLM budget enforcement.

    All limits use Decimal for cost/token values.
    """

    model_config = {"frozen": True}

    enabled: bool = Field(default=False)
    hourly_call_limit: int = Field(
        default=0, ge=0, description="Max LLM calls per hour (0=no calls allowed)"
    )
    daily_call_limit: int = Field(
        default=0, ge=0, description="Max LLM calls per day (0=no calls allowed)"
    )
    daily_token_limit: int = Field(
        default=0, ge=0, description="Max total tokens per day (0=no calls allowed)"
    )
    daily_cost_limit_usd: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        description="Max estimated cost per day in USD (0=no calls allowed)",
    )
    per_market_hourly_call_limit: int = Field(
        default=0,
        ge=0,
        description="Max calls per market per hour (0=no calls allowed)",
    )
    fallback_tokens_per_call: int = Field(
        default=4096,
        gt=0,
        description="Conservative fallback token count when provider usage is missing",
    )
    cost_per_input_token_usd: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        description="Estimated cost per input token in USD",
    )
    cost_per_output_token_usd: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        description="Estimated cost per output token in USD",
    )

    @field_validator(
        "daily_cost_limit_usd",
        "cost_per_input_token_usd",
        "cost_per_output_token_usd",
        mode="before",
    )
    @classmethod
    def _reject_float_decimal_fields(cls, v: object) -> Decimal:
        if isinstance(v, float):
            raise ValueError("Float values are forbidden; use Decimal")
        if isinstance(v, Decimal):
            return v
        try:
            return Decimal(str(v))
        except Exception:
            raise ValueError(f"Cannot coerce to Decimal: {type(v).__name__}")


class LLMBudgetWindow(BaseModel):
    """Tracks call counts within hourly and daily windows."""

    model_config = {"frozen": False}

    hourly_calls: int = Field(default=0, ge=0)
    primary_hourly_calls: int = Field(default=0, ge=0)
    reflection_hourly_calls: int = Field(default=0, ge=0)
    daily_calls: int = Field(default=0, ge=0)
    hourly_window_start_utc: Optional[datetime] = None
    daily_window_start_utc: Optional[datetime] = None


class LLMBudgetState(BaseModel):
    """Current budget state — whether a call is allowed."""

    model_config = {"frozen": True}

    allowed: bool
    block_reason: Optional[LLMBudgetBlockReason] = None
    remaining_calls_hourly: Optional[int] = None
    remaining_calls_daily: Optional[int] = None
    remaining_tokens_daily: Optional[int] = None
    remaining_cost_daily_usd: Optional[Decimal] = None

    @field_validator("remaining_cost_daily_usd", mode="before")
    @classmethod
    def _reject_float_cost(cls, v: object) -> Optional[Decimal]:
        if v is None:
            return None
        if isinstance(v, float):
            raise ValueError("Float cost values are forbidden; use Decimal")
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class LLMBudgetDecision(BaseModel):
    """Decision from budget guard: allow or block."""

    model_config = {"frozen": True}

    allowed: bool
    block_reason: Optional[LLMBudgetBlockReason] = None
    call_type: str = Field(
        default="primary",
        description="primary or reflection",
    )
    emit_audit_event: bool = Field(
        default=True,
        description="Whether callers should emit a human-facing log/ledger event",
    )
    suppressed_since_last_emit: int = Field(
        default=0,
        ge=0,
        description="Number of equivalent budget blocks suppressed since last emit",
    )
    retry_after_utc: Optional[datetime] = Field(
        default=None,
        description="When the blocking budget window is expected to reopen",
    )


class MarketCognitiveState(BaseModel):
    """Tracks repeated outcomes for a single market."""

    model_config = {"frozen": False}

    market_key: str
    consecutive_non_actionable: int = Field(default=0, ge=0)
    consecutive_invalid: int = Field(default=0, ge=0)
    last_outcome_type: Optional[str] = None
    cooldown_active_until_utc: Optional[datetime] = None
    cooldown_reason: Optional[MarketCooldownReason] = None


class MarketCooldownDecision(BaseModel):
    """Whether a market is currently in cooldown."""

    model_config = {"frozen": True}

    in_cooldown: bool
    cooldown_reason: Optional[MarketCooldownReason] = None
    expires_at_utc: Optional[datetime] = None


class LLMCostGuardSnapshot(BaseModel):
    """Audit snapshot of cost guard state at evaluation time.

    Secret-free — no prompts, keys, or high-cardinality identifiers.
    """

    model_config = {"frozen": True}

    budget_enabled: bool
    budget_allowed: bool
    budget_block_reason: Optional[LLMBudgetBlockReason] = None
    cooldown_in_effect: bool = False
    cooldown_market_key: Optional[str] = None
    cooldown_reason: Optional[MarketCooldownReason] = None
    estimated_spend_usd: Decimal = Field(default=Decimal("0"), ge=0)

    @field_validator("estimated_spend_usd", mode="before")
    @classmethod
    def _reject_float_spend(cls, v: object) -> Decimal:
        if isinstance(v, float):
            raise ValueError("Float cost values are forbidden; use Decimal")
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


# ---------------------------------------------------------------------------
# WI-54: Configurable DeepSeek Provider schemas
# ---------------------------------------------------------------------------


class LLMProvider(str, Enum):
    """Operator-selectable LLM evaluation provider.

    Lowercase values for env-var ergonomics; distinct from internal
    ``LLMProviderName`` (which is uppercase for compatibility with WI-52).
    """

    ANTHROPIC = "anthropic"
    DEEPSEEK = "deepseek"


class LLMProviderSelectionReason(str, Enum):
    """Typed reason for which provider was selected."""

    CONFIGURED = "configured"
    FALLBACK = "fallback"


class LLMProviderConfigErrorReason(str, Enum):
    """Typed reasons a provider configuration is invalid at startup."""

    MISSING_API_KEY = "missing_api_key"
    UNKNOWN_PROVIDER = "unknown_provider"
    MALFORMED_BASE_URL = "malformed_base_url"


class LLMProviderConfig(BaseModel):
    """Resolved provider configuration with runtime-usable fields.

    Validated at config-load time; secret values are stripped before
    serialisation so this model never leaks keys.
    """

    model_config = {"frozen": True}

    provider: LLMProvider
    api_key_value: SecretStr = Field(..., min_length=1)
    base_url: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    max_tokens: int = Field(default=4096, gt=0)
    max_retries: int = Field(default=2, ge=0)

    @field_validator("base_url")
    @classmethod
    def _require_valid_scheme_and_host(cls, v: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(v.strip())
        if parsed.scheme not in {"http", "https"}:
            raise ValueError(
                f"Invalid base_url scheme '{parsed.scheme}'; must be http or https"
            )
        if not parsed.netloc:
            raise ValueError("base_url must include a host")
        return v


class LLMProviderRuntimeContext(BaseModel):
    """Provider runtime context emitted in audit records and logs.

    Secret-free — only provider name, model name, and base URL host.
    """

    model_config = {"frozen": True}

    provider: str = Field(..., min_length=1)
    model_name: str = Field(..., min_length=1)
    base_url_host: str = Field(..., min_length=1)

    @field_validator("base_url_host")
    @classmethod
    def _host_only(cls, v: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(v if "://" in v else f"https://{v}")
        return parsed.netloc or v


class LLMProviderUsage(BaseModel):
    """Provider usage record with Decimal-native cost accounting.

    All cost/token-price fields are Decimal; float is rejected at boundary.
    When usage fields are missing or malformed, conservative configured
    defaults are used and ``is_estimated`` is set to ``True``.
    """

    model_config = {"frozen": True}

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: Decimal = Field(default=Decimal("0"), ge=0)
    is_estimated: bool = Field(
        default=False,
        description="True when usage fields were missing/malformed and fallback was used",
    )

    @field_validator("estimated_cost_usd", mode="before")
    @classmethod
    def _reject_float_cost(cls, v: object) -> Decimal:
        if isinstance(v, float):
            raise ValueError("Float cost values are forbidden; use Decimal")
        if isinstance(v, Decimal):
            return v
        return Decimal(str(v))


class LLMProviderMetadata(BaseModel):
    """Provider metadata for audit/log records.

    Secret-free — provider name, model name, and base URL host only.
    """

    model_config = {"frozen": True}

    provider: str = Field(..., min_length=1)
    model_name: str = Field(..., min_length=1)
    base_url_host: str = Field(..., min_length=1)

    @field_validator("base_url_host")
    @classmethod
    def _host_only(cls, v: str) -> str:
        from urllib.parse import urlparse

        parsed = urlparse(v if "://" in v else f"https://{v}")
        return parsed.netloc or v


class LLMProviderSelectionDecision(BaseModel):
    """Records the provider selection decision for audit trail."""

    model_config = {"frozen": True}

    selected_provider: LLMProvider
    reason: LLMProviderSelectionReason


class LLMProviderConfigError(BaseModel):
    """Typed configuration error emitted when provider config is invalid."""

    model_config = {"frozen": True}

    reason: LLMProviderConfigErrorReason
    message: str = Field(..., min_length=1)


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

        llm_wanted_to_trade = self.decision_boolean or (
            self.recommended_action != RecommendedAction.HOLD
        )
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
            raise AssertionError(
                "[BUG] decision_boolean=True but recommended_action=HOLD after override."
            )
        if not self.decision_boolean and self.position_size_pct > 0.0:
            raise AssertionError(
                f"[BUG] decision_boolean=False but position_size_pct={self.position_size_pct} > 0."
            )
        if self.decision_boolean and self.expected_value <= 0.0:
            raise AssertionError(
                f"[BUG] decision_boolean=True but EV={self.expected_value} <= 0 slipped through."
            )
        return self

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": False,
        "frozen": True,
    }
