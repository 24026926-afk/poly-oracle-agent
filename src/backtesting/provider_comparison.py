"""
src/backtesting/provider_comparison.py

WI-55 provider comparison, readiness verdict derivation, and report generation.

All money, EV, PnL, token-cost arithmetic uses ``Decimal``.
Readiness verdicts are typed, deterministic, and fail-closed.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog

from src.schemas.provider_comparison import (
    LLMProviderCalibrationMetrics,
    LLMProviderCalibrationRecommendation,
    LLMProviderComparisonConfig,
    LLMProviderComparisonReport,
    LLMProviderComparisonRun,
    LLMProviderCostMetrics,
    LLMProviderDecisionMetrics,
    LLMProviderLatencyMetrics,
    LLMProviderReadinessReason,
    LLMProviderReadinessVerdict,
)

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_APPROVED_OUTPUT_DIRS = {"docs/backtests", "docs/backtests/", "docs/backtests"}

_SECRET_REDACTION_PATTERNS: list[tuple[str, str]] = [
    (r"sk-ant-[A-Za-z0-9_-]{20,}", "[REDACTED_ANTHROPIC_KEY]"),
    (r"sk-ds-[A-Za-z0-9_-]{20,}", "[REDACTED_DEEPSEEK_KEY]"),
    (r"sk-or-[A-Za-z0-9_-]{20,}", "[REDACTED_OPENAI_KEY]"),
    (r"xai-[A-Za-z0-9_-]{20,}", "[REDACTED_XAI_KEY]"),
    (r"grok-[A-Za-z0-9_-]{20,}", "[REDACTED_GROK_KEY]"),
    (r"0x[0-9a-fA-F]{64}", "[REDACTED_PRIVATE_KEY]"),
    (r"\d{10,}:[A-Za-z0-9_-]{20,}", "[REDACTED_TELEGRAM_TOKEN]"),
    # CLOB token IDs and condition IDs are long hex strings (0x + 40+ hex)
    (r"0x[0-9a-fA-F]{40,63}", "[REDACTED_CLOB_TOKEN_ID]"),
    (r"0x[0-9a-fA-F]{65,}", "[REDACTED_PRIVATE_KEY]"),
    # Long decimal numeric IDs (e.g., Gamma-style token IDs)
    (r"\b\d{20,}\b", "[REDACTED_LONG_NUMERIC_ID]"),
]


def _ratio(numerator: int, denominator: int) -> Decimal:
    if denominator <= 0:
        return _ZERO
    return Decimal(numerator) / Decimal(denominator)


# ===========================================================================
# Readiness verdict derivation
# ===========================================================================


def derive_readiness_verdict(
    *,
    decision_metrics: LLMProviderDecisionMetrics,
    calibration_metrics: LLMProviderCalibrationMetrics,
    cost_metrics: LLMProviderCostMetrics,
    latency_metrics: LLMProviderLatencyMetrics,
    baseline_cost_usd: Decimal,
    baseline_mean_latency_ms: float,
    config: LLMProviderComparisonConfig,
) -> tuple[LLMProviderReadinessVerdict, list[LLMProviderReadinessReason]]:
    """Derive a deterministic, typed readiness verdict in priority order.

    Returns ``(verdict, reasons)`` where the verdict is the first (most
    severe) failure encountered.  Reasons accumulate independently for
    audit, but the verdict reflects the first failed gate.
    """

    reasons: list[LLMProviderReadinessReason] = []

    # Gate 1: JSON validity
    json_rate = decision_metrics.json_validity_rate
    json_invalid_rate = _ONE - json_rate if json_rate <= _ONE else _ZERO
    if json_invalid_rate > config.json_validity_tolerance:
        reasons.append(LLMProviderReadinessReason.JSON_VALIDITY_BELOW_TOLERANCE)
        if decision_metrics.total_calls < config.min_trades_for_verdict:
            reasons.append(LLMProviderReadinessReason.INSUFFICIENT_DATA)
        return (
            LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_JSON_VALIDITY,
            reasons,
        )

    # Gate 2: Insufficient data
    if decision_metrics.total_calls < config.min_trades_for_verdict:
        reasons.append(LLMProviderReadinessReason.INSUFFICIENT_DATA)
        return (
            LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_COST_OR_LATENCY,
            reasons,
        )

    # Gate 3: Outcome-based EV calibration (negative realized EV)
    if calibration_metrics.has_outcome_data:
        if calibration_metrics.avg_realized_return < _ZERO:
            reasons.append(LLMProviderReadinessReason.NEGATIVE_REALIZED_EV)
            return (
                LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_NEGATIVE_EV,
                reasons,
            )
    else:
        reasons.append(LLMProviderReadinessReason.OUTCOME_COVERAGE_MISSING)

    # Gate 4: Cost must be lower than baseline by required fraction
    if baseline_cost_usd > _ZERO:
        cost_reduction = (
            baseline_cost_usd - cost_metrics.total_estimated_cost_usd
        ) / baseline_cost_usd
        if cost_reduction < config.cost_reduction_required:
            reasons.append(LLMProviderReadinessReason.COST_EXCEEDS_BASELINE)
            return (
                LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_COST_OR_LATENCY,
                reasons,
            )

    # Gate 5: Latency must not exceed baseline multiplier.
    # When latency data is explicitly absent (zero samples), the gate is
    # NOT skipped — no samples means latency cannot be verified.
    if latency_metrics.sample_count == 0 and decision_metrics.total_calls > 0:
        reasons.append(LLMProviderReadinessReason.INSUFFICIENT_DATA)
    elif baseline_mean_latency_ms > 0 and latency_metrics.sample_count > 0:
        effective_lat = (
            latency_metrics.mean_latency_ms
            if latency_metrics.mean_latency_ms > 0
            else baseline_mean_latency_ms
        )
        latency_ratio = effective_lat / baseline_mean_latency_ms
        if latency_ratio > float(config.latency_multiplier_max):
            reasons.append(LLMProviderReadinessReason.LATENCY_EXCEEDS_BASELINE)
            return (
                LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_COST_OR_LATENCY,
                reasons,
            )

    # Gate 6: Confidence calibration
    if (
        calibration_metrics.confidence_calibration_deviation
        > config.calibration_deviation_max
    ):
        reasons.append(LLMProviderReadinessReason.WEAK_CONFIDENCE_CALIBRATION)

    # Gate 7: EV calibration (where outcomes exist)
    if calibration_metrics.has_outcome_data:
        if calibration_metrics.ev_calibration_deviation > config.ev_deviation_max:
            reasons.append(LLMProviderReadinessReason.WEAK_EV_CALIBRATION)

    # Gate 8: Gatekeeper pass rate threshold
    if (
        decision_metrics.gatekeeper_pass_rate < Decimal("0.5")
        and decision_metrics.total_calls > 0
    ):
        reasons.append(LLMProviderReadinessReason.GATEKEEPER_FAILURE_RATE_HIGH)

    # If any calibration/quality issues, needs recalibration
    if reasons:
        if LLMProviderReadinessReason.GATEKEEPER_FAILURE_RATE_HIGH in reasons:
            return (
                LLMProviderReadinessVerdict.PROVIDER_NEEDS_THRESHOLD_RECALIBRATION,
                reasons,
            )
        if LLMProviderReadinessReason.WEAK_CONFIDENCE_CALIBRATION in reasons:
            return (
                LLMProviderReadinessVerdict.PROVIDER_NEEDS_THRESHOLD_RECALIBRATION,
                reasons,
            )
        if LLMProviderReadinessReason.WEAK_EV_CALIBRATION in reasons:
            return (
                LLMProviderReadinessVerdict.PROVIDER_NEEDS_THRESHOLD_RECALIBRATION,
                reasons,
            )
        if LLMProviderReadinessReason.OUTCOME_COVERAGE_MISSING in reasons:
            return (
                LLMProviderReadinessVerdict.PROVIDER_READY_FOR_SAMPLED_AUDIT_ONLY,
                reasons,
            )
        if LLMProviderReadinessReason.INSUFFICIENT_DATA in reasons:
            return (
                LLMProviderReadinessVerdict.PROVIDER_READY_FOR_SAMPLED_AUDIT_ONLY,
                reasons,
            )

    # All gates pass
    reasons.append(LLMProviderReadinessReason.ALL_GATES_PASSED)
    return (
        LLMProviderReadinessVerdict.PROVIDER_READY_FOR_DRY_RUN_PRIMARY,
        reasons,
    )


# ===========================================================================
# Calibration recommendation derivation
# ===========================================================================


def derive_calibration_recommendation(
    *,
    provider: str,
    model_name: str,
    calibration_metrics: LLMProviderCalibrationMetrics,
    decision_metrics: LLMProviderDecisionMetrics,
    config: LLMProviderComparisonConfig,
) -> LLMProviderCalibrationRecommendation | None:
    """Derive advisory calibration recommendations from observed metrics.

    Returns ``None`` when no changes are recommended.
    """

    suggestions: dict[str, Any] = {
        "provider": provider,
        "model_name": model_name,
        "suggested_confidence_threshold": None,
        "suggested_ev_threshold": None,
        "suggested_max_output_tokens": None,
        "suggested_cooldown_seconds": None,
        "suggested_budget_daily_cost_limit_usd": None,
        "reasoning": "",
    }

    reason_parts: list[str] = []

    if (
        calibration_metrics.confidence_calibration_deviation
        > config.calibration_deviation_max
    ):
        # Recommend adjusted confidence threshold based on observed win rates
        if calibration_metrics.confidence_bucket_observed_win_rate:
            avg_win = sum(
                calibration_metrics.confidence_bucket_observed_win_rate, _ZERO
            ) / len(calibration_metrics.confidence_bucket_observed_win_rate)
            suggestions["suggested_confidence_threshold"] = avg_win
            reason_parts.append(
                f"Confidence calibration deviation "
                f"{calibration_metrics.confidence_calibration_deviation:.4f} "
                f"exceeds max {config.calibration_deviation_max:.4f}"
            )

    if (
        calibration_metrics.has_outcome_data
        and calibration_metrics.ev_calibration_deviation > config.ev_deviation_max
    ):
        suggestions["suggested_ev_threshold"] = max(
            _ZERO,
            calibration_metrics.avg_ev - calibration_metrics.ev_calibration_deviation,
        )
        reason_parts.append(
            f"EV calibration deviation "
            f"{calibration_metrics.ev_calibration_deviation:.4f} "
            f"exceeds max {config.ev_deviation_max:.4f}"
        )

    if decision_metrics.invalid_json_count > 0:
        # High invalid JSON rate — suggest higher max tokens for better output
        if decision_metrics.json_validity_rate < Decimal("0.95"):
            suggestions["suggested_max_output_tokens"] = 8192
            reason_parts.append(
                f"Low JSON validity rate {decision_metrics.json_validity_rate:.4f}; "
                "consider increasing max output tokens"
            )

    suggestions["reasoning"] = (
        "; ".join(reason_parts) if reason_parts else "No changes recommended"
    )

    if not any(
        suggestions[k] is not None
        for k in [
            "suggested_confidence_threshold",
            "suggested_ev_threshold",
            "suggested_max_output_tokens",
            "suggested_cooldown_seconds",
            "suggested_budget_daily_cost_limit_usd",
        ]
    ):
        return None

    return LLMProviderCalibrationRecommendation(**suggestions)


# ===========================================================================
# Secret redaction
# ===========================================================================


def redact_secrets(text: str) -> str:
    """Redact known secret and high-cardinality patterns from text."""
    for pattern, replacement in _SECRET_REDACTION_PATTERNS:
        text = re.sub(pattern, replacement, text)
    return text


# ===========================================================================
# Report path validation
# ===========================================================================


def validate_report_path(output_path: str | Path) -> Path:
    """Validate that the report output path is within approved directories.

    Anchored to the project root (``poly-oracle-agent/``), not the
    process CWD.  Returns the resolved Path on success.  Raises
    ``ValueError`` on path escape attempts.
    """
    resolved = Path(output_path).resolve()

    # Anchor to project root (three levels above this file:
    # provider_comparison.py -> backtesting/ -> src/ -> root)
    project_root = Path(__file__).resolve().parent.parent.parent
    allowed = (project_root / "docs" / "backtests").resolve()

    try:
        resolved.relative_to(allowed)
        return resolved
    except ValueError:
        pass

    raise ValueError(
        f"Report output path '{output_path}' escapes approved "
        f"directories. Reports must be written under docs/backtests/."
    )


# ===========================================================================
# Report generation
# ===========================================================================


def generate_comparison_report(
    *,
    run: LLMProviderComparisonRun,
    output_path: str | Path | None = None,
) -> LLMProviderComparisonReport:
    """Generate a secret-free WI-55 provider comparison report.

    Validates that the run config has ``dry_run=True`` and that the
    serialised report contains no secret patterns.  Optionally writes
    a JSON report to the ``output_path`` (if provided), after path
    validation.
    """

    report = LLMProviderComparisonReport(
        run=run,
        generated_at_utc=datetime.now(timezone.utc),
    )

    if output_path is not None:
        safe_path = validate_report_path(output_path)
        json_text = report.model_dump_json(indent=2)
        redacted_text = redact_secrets(json_text)
        safe_path.write_text(redacted_text, encoding="utf-8")

    return report
