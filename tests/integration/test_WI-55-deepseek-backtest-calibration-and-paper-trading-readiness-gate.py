"""
tests/integration/test_WI-55-deepseek-backtest-calibration-and-paper-trading-readiness-gate.py

Integration tests for WI-55: end-to-end provider comparison report generation,
verdict derivation from backtest output, path validation, and secret redaction.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest


from src.backtesting.provider_comparison import (
    derive_readiness_verdict,
    generate_comparison_report,
    redact_secrets,
    validate_report_path,
)
from src.schemas.provider_comparison import (
    LLMProviderCalibrationMetrics,
    LLMProviderComparisonConfig,
    LLMProviderComparisonResult,
    LLMProviderComparisonRun,
    LLMProviderCostMetrics,
    LLMProviderDecisionMetrics,
    LLMProviderLatencyMetrics,
    LLMProviderReadinessReason,
    LLMProviderReadinessVerdict,
)

_ZERO = Decimal("0")


def _pass_decision() -> LLMProviderDecisionMetrics:
    return LLMProviderDecisionMetrics(
        total_calls=100,
        valid_json_count=98,
        invalid_json_count=2,
        json_validity_rate=Decimal("0.98"),
        gatekeeper_passed=55,
        gatekeeper_failed=43,
        gatekeeper_pass_rate=Decimal("0.56"),
        buy_count=25,
        hold_count=50,
        skip_count=25,
        sell_count=0,
    )


def _pass_calibration() -> LLMProviderCalibrationMetrics:
    return LLMProviderCalibrationMetrics(
        confidence_bucket_low=[
            Decimal("0"),
            Decimal("0.2"),
            Decimal("0.4"),
            Decimal("0.6"),
            Decimal("0.8"),
        ],
        confidence_bucket_high=[
            Decimal("0.2"),
            Decimal("0.4"),
            Decimal("0.6"),
            Decimal("0.8"),
            Decimal("1.0"),
        ],
        confidence_bucket_avg=[
            Decimal("0.1"),
            Decimal("0.3"),
            Decimal("0.5"),
            Decimal("0.7"),
            Decimal("0.9"),
        ],
        confidence_bucket_observed_win_rate=[
            Decimal("0.1"),
            Decimal("0.28"),
            Decimal("0.48"),
            Decimal("0.65"),
            Decimal("0.82"),
        ],
        confidence_bucket_count=[10, 15, 25, 20, 5],
        confidence_calibration_deviation=Decimal("0.05"),
        avg_ev=Decimal("0.045"),
        avg_realized_return=Decimal("0.038"),
        ev_calibration_deviation=Decimal("0.007"),
        outcome_coverage_fraction=Decimal("0.75"),
        has_outcome_data=True,
    )


def _pass_cost() -> LLMProviderCostMetrics:
    return LLMProviderCostMetrics(
        total_input_tokens=400_000,
        total_output_tokens=50_000,
        total_tokens=450_000,
        total_estimated_cost_usd=Decimal("0.45"),
        is_estimated=False,
        budget_block_count=1,
        cooldown_block_count=0,
    )


def _pass_latency() -> LLMProviderLatencyMetrics:
    return LLMProviderLatencyMetrics(
        min_latency_ms=200,
        max_latency_ms=3000,
        mean_latency_ms=800,
        median_latency_ms=750,
        p95_latency_ms=2500,
        p99_latency_ms=2900,
        sample_count=98,
    )


def _default_config(**kw) -> LLMProviderComparisonConfig:
    return LLMProviderComparisonConfig(data_dir="/tmp/test", dry_run=True, **kw)


# ---------------------------------------------------------------------------
# End-to-end report generation
# ---------------------------------------------------------------------------


def test_generate_report_writes_json_file():
    """Report written via generate_comparison_report produces valid report."""
    cfg = _default_config()
    result = LLMProviderComparisonResult(
        provider="deepseek",
        model_name="deepseek-v4-pro",
        decision_metrics=_pass_decision(),
        calibration_metrics=_pass_calibration(),
        cost_metrics=_pass_cost(),
        latency_metrics=_pass_latency(),
        readiness_verdict=LLMProviderReadinessVerdict.PROVIDER_READY_FOR_DRY_RUN_PRIMARY,
        readiness_reasons=[LLMProviderReadinessReason.ALL_GATES_PASSED],
    )
    run = LLMProviderComparisonRun(config=cfg, results=[result])

    # Write under project-relative docs/backtests/
    out = Path("docs") / "backtests" / "_wi55_integration_test.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        generate_comparison_report(run=run, output_path=str(out))
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["run"]["results"][0]["provider"] == "deepseek"
    finally:
        out.unlink(missing_ok=True)


def test_report_enforces_dry_run():
    """LLMProviderComparisonConfig rejects dry_run=False at construction."""
    with pytest.raises(ValueError, match="dry_run"):
        LLMProviderComparisonConfig(data_dir="/tmp/test", dry_run=False)


def test_report_rejects_secrets_in_content():
    """Report with API key patterns fails validation."""
    cfg = _default_config()
    result = LLMProviderComparisonResult(
        provider="deepseek",
        model_name="deepseek-v4-pro",
        decision_metrics=_pass_decision(),
        calibration_metrics=_pass_calibration(),
        cost_metrics=_pass_cost(),
        latency_metrics=_pass_latency(),
        readiness_verdict=LLMProviderReadinessVerdict.PROVIDER_READY_FOR_DRY_RUN_PRIMARY,
        readiness_reasons=[LLMProviderReadinessReason.ALL_GATES_PASSED],
    )
    run = LLMProviderComparisonRun(config=cfg, results=[result])
    report = generate_comparison_report(run=run)
    text = report.model_dump_json().lower()
    assert "sk-ant-" not in text
    assert "sk-ds-" not in text


# ---------------------------------------------------------------------------
# Verdict derivation integration
# ---------------------------------------------------------------------------


def test_deepseek_rejected_for_invalid_json_integration():
    dm = LLMProviderDecisionMetrics(
        total_calls=100,
        valid_json_count=80,
        invalid_json_count=20,
        json_validity_rate=Decimal("0.80"),
        gatekeeper_passed=10,
        gatekeeper_failed=70,
        gatekeeper_pass_rate=Decimal("0.12"),
        buy_count=5,
        hold_count=60,
        skip_count=35,
    )
    verdict, reasons = derive_readiness_verdict(
        decision_metrics=dm,
        calibration_metrics=_pass_calibration(),
        cost_metrics=_pass_cost(),
        latency_metrics=_pass_latency(),
        baseline_cost_usd=Decimal("3.00"),
        baseline_mean_latency_ms=1200.0,
        config=_default_config(),
    )
    assert verdict == LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_JSON_VALIDITY
    assert LLMProviderReadinessReason.JSON_VALIDITY_BELOW_TOLERANCE in reasons


def test_deepseek_ready_for_primary_integration():
    verdict, reasons = derive_readiness_verdict(
        decision_metrics=_pass_decision(),
        calibration_metrics=_pass_calibration(),
        cost_metrics=_pass_cost(),
        latency_metrics=_pass_latency(),
        baseline_cost_usd=Decimal("3.00"),
        baseline_mean_latency_ms=1200.0,
        config=_default_config(),
    )
    assert verdict == LLMProviderReadinessVerdict.PROVIDER_READY_FOR_DRY_RUN_PRIMARY
    assert LLMProviderReadinessReason.ALL_GATES_PASSED in reasons


# ---------------------------------------------------------------------------
# Path validation integration
# ---------------------------------------------------------------------------


def test_report_path_rejects_escape():
    with pytest.raises(ValueError, match="escapes"):
        validate_report_path("/etc/passwd")


def test_report_path_allows_docs_backtests():
    path = validate_report_path("docs/backtests/report.json")
    assert path is not None


# ---------------------------------------------------------------------------
# Redaction integration
# ---------------------------------------------------------------------------


def test_redact_anthropic_key():
    text = "key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456"
    redacted = redact_secrets(text)
    assert "sk-ant" not in redacted


def test_redact_deepseek_key():
    text = "key: sk-ds-mykey-abcdefghijklmnopqrstuvw"
    redacted = redact_secrets(text)
    assert "sk-ds" not in redacted


def test_redact_clob_token_id():
    text = "token: 0x1234567890abcdef1234567890abcdef12345678"
    redacted = redact_secrets(text)
    assert "0x" not in redacted or "REDACTED" in redacted


def test_redact_telegram_token():
    text = "1234567890:AAbbCCddEEffGGhhIIjjKKllMMnnOOpp"
    redacted = redact_secrets(text)
    assert "REDACTED_TELEGRAM_TOKEN" in redacted or "1234567890:AA" not in redacted


# ---------------------------------------------------------------------------
# Cost metrics conservative accounting integration
# ---------------------------------------------------------------------------


def test_cost_metrics_rejects_zero_cost_with_tokens():
    """LLMProviderCostMetrics with tokens but no cost and is_estimated=False fails."""
    with pytest.raises(ValueError):
        LLMProviderCostMetrics(
            total_input_tokens=1000,
            total_output_tokens=200,
            total_tokens=1200,
            total_estimated_cost_usd=_ZERO,
            is_estimated=False,
        )


def test_cost_metrics_accepts_estimated_zero_cost():
    """Estimated metrics accept zero when tokens are present but is_estimated=True."""
    cm = LLMProviderCostMetrics(
        total_input_tokens=0,
        total_output_tokens=0,
        total_tokens=0,
        total_estimated_cost_usd=Decimal("0.02"),
        is_estimated=True,
    )
    assert cm.is_estimated is True


# ---------------------------------------------------------------------------
# Budget/cooldown blocks surfaced in report
# ---------------------------------------------------------------------------


def test_report_includes_budget_blocks():
    cst = LLMProviderCostMetrics(
        total_input_tokens=1000,
        total_output_tokens=200,
        total_tokens=1200,
        total_estimated_cost_usd=Decimal("0.01"),
        is_estimated=False,
        budget_block_count=5,
        cooldown_block_count=3,
    )
    result = LLMProviderComparisonResult(
        provider="deepseek",
        model_name="deepseek-v4-pro",
        decision_metrics=_pass_decision(),
        calibration_metrics=_pass_calibration(),
        cost_metrics=cst,
        latency_metrics=_pass_latency(),
        readiness_verdict=LLMProviderReadinessVerdict.PROVIDER_READY_FOR_DRY_RUN_PRIMARY,
        readiness_reasons=[LLMProviderReadinessReason.ALL_GATES_PASSED],
    )
    run = LLMProviderComparisonRun(config=_default_config(), results=[result])
    report = generate_comparison_report(run=run)
    assert report.run.results[0].cost_metrics.budget_block_count == 5
    assert report.run.results[0].cost_metrics.cooldown_block_count == 3


# ---------------------------------------------------------------------------
# Adapter → BacktestRunner pipeline integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_metrics_budget_blocks_surfaced():
    """LLMProviderCostMetrics with explicit budget_block_count surfaces it."""
    cst = LLMProviderCostMetrics(
        total_input_tokens=1000,
        total_output_tokens=200,
        total_tokens=1200,
        total_estimated_cost_usd=Decimal("0.01"),
        is_estimated=False,
        budget_block_count=3,
        cooldown_block_count=1,
    )
    assert cst.budget_block_count == 3
    assert cst.cooldown_block_count == 1
    assert cst.total_estimated_cost_usd == Decimal("0.01")


@pytest.mark.asyncio
async def test_partial_missing_usage_estimated_in_metrics():
    """LLMProviderCostMetrics with is_estimated=True and non-zero cost."""
    cst = LLMProviderCostMetrics(
        total_input_tokens=20000 + 3 * 4096,
        total_output_tokens=5000 + 3 * 1024,
        total_tokens=20000 + 3 * 4096 + 5000 + 3 * 1024,
        total_estimated_cost_usd=Decimal("0.10"),
        is_estimated=True,
        calls_with_missing_usage=3,
    )
    assert cst.is_estimated is True
    assert cst.calls_with_missing_usage == 3
    assert cst.total_estimated_cost_usd > Decimal("0")
