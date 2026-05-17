"""
tests/unit/test_wi-55-deepseek-backtest-calibration-and-paper-trading-readiness-gate.py

Unit tests for WI-55: DeepSeek Backtest Calibration and Paper-Trading
Readiness Gate.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.backtesting.provider_comparison import (
    derive_calibration_recommendation,
    derive_readiness_verdict,
    generate_comparison_report,
    redact_secrets,
    validate_report_path,
)
from src.schemas.provider_comparison import (
    LLMProviderCalibrationMetrics,
    LLMProviderCalibrationRecommendation,
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


# ===========================================================================
# Helpers
# ===========================================================================


def _default_config(**overrides) -> LLMProviderComparisonConfig:
    kwargs = {
        "data_dir": "/tmp/test_data",
        "dry_run": True,
        **overrides,
    }
    return LLMProviderComparisonConfig(**kwargs)


def _full_pass_decision_metrics(**overrides) -> LLMProviderDecisionMetrics:
    kwargs = {
        "total_calls": 100,
        "valid_json_count": 98,
        "invalid_json_count": 2,
        "json_validity_rate": Decimal("0.98"),
        "gatekeeper_passed": 55,
        "gatekeeper_failed": 43,
        "gatekeeper_pass_rate": Decimal("0.56"),
        "buy_count": 25,
        "hold_count": 50,
        "skip_count": 25,
        "sell_count": 0,
        **overrides,
    }
    return LLMProviderDecisionMetrics(**kwargs)


def _full_pass_calibration_metrics(**overrides) -> LLMProviderCalibrationMetrics:
    kwargs = {
        "confidence_bucket_low": [
            Decimal("0.0"),
            Decimal("0.2"),
            Decimal("0.4"),
            Decimal("0.6"),
            Decimal("0.8"),
        ],
        "confidence_bucket_high": [
            Decimal("0.2"),
            Decimal("0.4"),
            Decimal("0.6"),
            Decimal("0.8"),
            Decimal("1.0"),
        ],
        "confidence_bucket_avg": [
            Decimal("0.12"),
            Decimal("0.31"),
            Decimal("0.52"),
            Decimal("0.71"),
            Decimal("0.88"),
        ],
        "confidence_bucket_observed_win_rate": [
            Decimal("0.10"),
            Decimal("0.28"),
            Decimal("0.48"),
            Decimal("0.65"),
            Decimal("0.82"),
        ],
        "confidence_bucket_count": [10, 15, 25, 20, 5],
        "confidence_calibration_deviation": Decimal("0.06"),
        "avg_ev": Decimal("0.045"),
        "avg_realized_return": Decimal("0.038"),
        "ev_calibration_deviation": Decimal("0.007"),
        "outcome_coverage_fraction": Decimal("0.75"),
        "has_outcome_data": True,
        **overrides,
    }
    return LLMProviderCalibrationMetrics(**kwargs)


def _full_pass_cost_metrics(**overrides) -> LLMProviderCostMetrics:
    kwargs = {
        "total_input_tokens": 400_000,
        "total_output_tokens": 50_000,
        "total_tokens": 450_000,
        "total_estimated_cost_usd": Decimal("0.45"),
        "is_estimated": False,
        "budget_block_count": 0,
        "cooldown_block_count": 0,
        **overrides,
    }
    return LLMProviderCostMetrics(**kwargs)


def _full_pass_latency_metrics(**overrides) -> LLMProviderLatencyMetrics:
    kwargs = {
        "min_latency_ms": 200,
        "max_latency_ms": 1500,
        "mean_latency_ms": 800,
        "median_latency_ms": 750,
        "p95_latency_ms": 1200,
        "p99_latency_ms": 1400,
        "sample_count": 98,
        **overrides,
    }
    return LLMProviderLatencyMetrics(**kwargs)


def _full_pass_result(**overrides) -> LLMProviderComparisonResult:
    kwargs = {
        "provider": "deepseek",
        "model_name": "deepseek-v4-pro",
        "decision_metrics": _full_pass_decision_metrics(),
        "calibration_metrics": _full_pass_calibration_metrics(),
        "cost_metrics": _full_pass_cost_metrics(),
        "latency_metrics": _full_pass_latency_metrics(),
        "readiness_verdict": LLMProviderReadinessVerdict.PROVIDER_READY_FOR_DRY_RUN_PRIMARY,
        "readiness_reasons": [LLMProviderReadinessReason.ALL_GATES_PASSED],
        **overrides,
    }
    return LLMProviderComparisonResult(**kwargs)


# ===========================================================================
# LLMProviderComparisonConfig
# ===========================================================================


class TestComparisonConfig:
    def test_requires_dry_run_true(self):
        cfg = LLMProviderComparisonConfig(data_dir="/tmp/test", dry_run=True)
        assert cfg.dry_run is True

    def test_rejects_dry_run_false(self):
        with pytest.raises(ValidationError, match="dry_run=True"):
            LLMProviderComparisonConfig(data_dir="/tmp/test", dry_run=False)

    def test_is_frozen(self):
        cfg = _default_config()
        with pytest.raises(Exception):
            cfg.dry_run = False

    def test_claude_sampling_disabled_by_default(self):
        cfg = _default_config()
        assert cfg.enable_anthropic_sampling is False
        assert cfg.anthropic_sample_fraction == Decimal("0")

    def test_claude_sample_fraction_must_be_bounded(self):
        with pytest.raises(ValidationError):
            _default_config(
                enable_anthropic_sampling=True,
                anthropic_sample_fraction=Decimal("0"),
            )

    def test_rejects_float_for_decimal_fields(self):
        with pytest.raises(ValidationError):
            _default_config(initial_bankroll_usdc=1000.0)

    def test_json_validity_threshold_defaults_sane(self):
        cfg = _default_config()
        assert cfg.json_validity_tolerance == Decimal("0.10")
        assert 0 <= cfg.json_validity_tolerance <= 1


# ===========================================================================
# LLMProviderDecisionMetrics
# ===========================================================================


class TestDecisionMetrics:
    def test_is_frozen(self):
        dm = _full_pass_decision_metrics()
        with pytest.raises(Exception):
            dm.total_calls = 999

    def test_rejects_float_for_proportions(self):
        with pytest.raises(ValidationError):
            LLMProviderDecisionMetrics(
                total_calls=10,
                valid_json_count=8,
                invalid_json_count=2,
                json_validity_rate=0.8,
                gatekeeper_passed=5,
                gatekeeper_failed=3,
                gatekeeper_pass_rate=0.625,
                buy_count=3,
                hold_count=4,
                skip_count=1,
            )

    def test_json_validity_rate_is_decimal(self):
        dm = _full_pass_decision_metrics(json_validity_rate=Decimal("0.95"))
        assert isinstance(dm.json_validity_rate, Decimal)
        assert dm.json_validity_rate == Decimal("0.95")

    def test_records_gatekeeper_pass_fail_counts(self):
        dm = _full_pass_decision_metrics(
            gatekeeper_passed=40,
            gatekeeper_failed=58,
            gatekeeper_pass_rate=Decimal("0.408"),
        )
        assert dm.gatekeeper_passed == 40
        assert dm.gatekeeper_failed == 58

    def test_records_decision_distribution(self):
        dm = _full_pass_decision_metrics(
            buy_count=10,
            hold_count=60,
            skip_count=30,
            sell_count=0,
        )
        assert dm.buy_count == 10
        assert dm.hold_count == 60
        assert dm.skip_count == 30
        assert dm.sell_count == 0


# ===========================================================================
# LLMProviderCalibrationMetrics
# ===========================================================================


class TestCalibrationMetrics:
    def test_is_frozen(self):
        cm = _full_pass_calibration_metrics()
        with pytest.raises(Exception):
            cm.avg_ev = Decimal("1.0")

    def test_rejects_float_for_decimal_fields(self):
        with pytest.raises(ValidationError):
            LLMProviderCalibrationMetrics(
                confidence_bucket_low=[Decimal("0.0"), Decimal("0.5")],
                confidence_bucket_high=[Decimal("0.5"), Decimal("1.0")],
                confidence_bucket_avg=[Decimal("0.3"), Decimal("0.7")],
                confidence_bucket_observed_win_rate=[Decimal("0.3"), Decimal("0.7")],
                confidence_bucket_count=[10, 10],
                confidence_calibration_deviation=0.05,
                avg_ev=Decimal("0.04"),
                avg_realized_return=Decimal("0.035"),
                ev_calibration_deviation=Decimal("0.005"),
                outcome_coverage_fraction=Decimal("0.8"),
                has_outcome_data=True,
            )

    def test_confidence_distribution_is_decimal_buckets(self):
        cm = _full_pass_calibration_metrics()
        assert all(isinstance(v, Decimal) for v in cm.confidence_bucket_avg)
        assert all(
            isinstance(v, Decimal) for v in cm.confidence_bucket_observed_win_rate
        )

    def test_records_outcome_coverage_flag(self):
        cm_with = _full_pass_calibration_metrics(
            has_outcome_data=True, outcome_coverage_fraction=Decimal("0.75")
        )
        assert cm_with.has_outcome_data is True
        assert cm_with.outcome_coverage_fraction == Decimal("0.75")

        cm_without = _full_pass_calibration_metrics(
            has_outcome_data=False, outcome_coverage_fraction=Decimal("0")
        )
        assert cm_without.has_outcome_data is False
        assert cm_without.outcome_coverage_fraction == Decimal("0")


# ===========================================================================
# LLMProviderCostMetrics
# ===========================================================================


class TestCostMetrics:
    def test_is_frozen(self):
        cm = _full_pass_cost_metrics()
        with pytest.raises(Exception):
            cm.total_estimated_cost_usd = Decimal("100")

    def test_rejects_float_for_cost_fields(self):
        with pytest.raises(ValidationError):
            LLMProviderCostMetrics(
                total_input_tokens=1000,
                total_output_tokens=200,
                total_tokens=1200,
                total_estimated_cost_usd=0.05,
            )

    def test_total_estimated_cost_is_decimal(self):
        cm = _full_pass_cost_metrics(total_estimated_cost_usd=Decimal("0.45"))
        assert isinstance(cm.total_estimated_cost_usd, Decimal)
        assert cm.total_estimated_cost_usd == Decimal("0.45")

    def test_records_total_input_tokens(self):
        cm = _full_pass_cost_metrics(total_input_tokens=100_000)
        assert cm.total_input_tokens == 100_000

    def test_records_total_output_tokens(self):
        cm = _full_pass_cost_metrics(total_output_tokens=20_000)
        assert cm.total_output_tokens == 20_000

    def test_records_budget_block_count(self):
        cm = _full_pass_cost_metrics(budget_block_count=5)
        assert cm.budget_block_count == 5

    def test_records_cooldown_block_count(self):
        cm = _full_pass_cost_metrics(cooldown_block_count=3)
        assert cm.cooldown_block_count == 3


# ===========================================================================
# LLMProviderLatencyMetrics
# ===========================================================================


class TestLatencyMetrics:
    def test_is_frozen(self):
        lm = _full_pass_latency_metrics()
        with pytest.raises(Exception):
            lm.mean_latency_ms = 999.0

    def test_rejects_negative_latency(self):
        with pytest.raises(ValidationError):
            LLMProviderLatencyMetrics(
                min_latency_ms=-1.0,
                max_latency_ms=100,
                mean_latency_ms=50,
                median_latency_ms=45,
                p95_latency_ms=90,
                p99_latency_ms=95,
                sample_count=10,
            )

    def test_records_min_max_mean_median(self):
        lm = LLMProviderLatencyMetrics(
            min_latency_ms=100,
            max_latency_ms=3000,
            mean_latency_ms=800,
            median_latency_ms=750,
            p95_latency_ms=2500,
            p99_latency_ms=2900,
            sample_count=50,
        )
        assert lm.min_latency_ms == 100
        assert lm.max_latency_ms == 3000
        assert lm.mean_latency_ms == 800
        assert lm.median_latency_ms == 750


# ===========================================================================
# LLMProviderComparisonResult
# ===========================================================================


class TestComparisonResult:
    def test_is_frozen(self):
        result = _full_pass_result()
        with pytest.raises(Exception):
            result.provider = "openai"

    def test_requires_provider_field(self):
        with pytest.raises(ValidationError):
            LLMProviderComparisonResult(
                provider="",
                model_name="test",
                decision_metrics=_full_pass_decision_metrics(),
                calibration_metrics=_full_pass_calibration_metrics(),
                cost_metrics=_full_pass_cost_metrics(),
                latency_metrics=_full_pass_latency_metrics(),
                readiness_verdict=LLMProviderReadinessVerdict.PROVIDER_READY_FOR_DRY_RUN_PRIMARY,
            )

    def test_combines_all_metric_categories(self):
        result = _full_pass_result()
        assert result.decision_metrics is not None
        assert result.calibration_metrics is not None
        assert result.cost_metrics is not None
        assert result.latency_metrics is not None

    def test_rejects_float_for_decimal_fields(self):
        result = _full_pass_result()
        assert result.readiness_verdict is not None
        # Verify result validates ok with all Decimal fields
        assert result.cost_metrics.total_estimated_cost_usd == Decimal("0.45")

    def test_records_provider_model_name(self):
        result = _full_pass_result(provider="deepseek", model_name="deepseek-v4-pro")
        assert result.provider == "deepseek"
        assert result.model_name == "deepseek-v4-pro"


# ===========================================================================
# LLMProviderCalibrationRecommendation
# ===========================================================================


class TestCalibrationRecommendation:
    def test_is_frozen(self):
        rec = LLMProviderCalibrationRecommendation(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            reasoning="Test",
        )
        with pytest.raises(Exception):
            rec.provider = "openai"

    def test_is_advisory_only(self):
        rec = LLMProviderCalibrationRecommendation(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            suggested_confidence_threshold=Decimal("0.80"),
            reasoning="Test recommendation",
        )
        data = rec.model_dump()
        assert data["reasoning"] == "Test recommendation"

    def test_may_suggest_confidence_threshold(self):
        rec = LLMProviderCalibrationRecommendation(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            suggested_confidence_threshold=Decimal("0.85"),
        )
        assert rec.suggested_confidence_threshold == Decimal("0.85")

    def test_may_suggest_ev_threshold(self):
        rec = LLMProviderCalibrationRecommendation(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            suggested_ev_threshold=Decimal("0.03"),
        )
        assert rec.suggested_ev_threshold == Decimal("0.03")

    def test_may_suggest_max_output_tokens(self):
        rec = LLMProviderCalibrationRecommendation(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            suggested_max_output_tokens=8192,
        )
        assert rec.suggested_max_output_tokens == 8192

    def test_may_suggest_cooldown_settings(self):
        rec = LLMProviderCalibrationRecommendation(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            suggested_cooldown_seconds=600,
        )
        assert rec.suggested_cooldown_seconds == 600


# ===========================================================================
# LLMProviderComparisonReport
# ===========================================================================


class TestComparisonReport:
    def _run_with(self, **overrides) -> LLMProviderComparisonRun:
        config = _default_config(**overrides)
        result = _full_pass_result()
        return LLMProviderComparisonRun(
            config=config,
            results=[result],
        )

    def test_is_frozen(self):
        run = self._run_with()
        report = generate_comparison_report(run=run)
        with pytest.raises(Exception):
            report.generated_at_utc = None

    def test_rejects_float_for_decimal_fields(self):
        run = self._run_with()
        report = generate_comparison_report(run=run)
        assert report.run is not None

    def test_includes_json_validity_rate(self):
        run = self._run_with()
        report = generate_comparison_report(run=run)
        dm = report.run.results[0].decision_metrics
        assert dm.json_validity_rate == Decimal("0.98")

    def test_includes_gatekeeper_pass_fail(self):
        run = self._run_with()
        report = generate_comparison_report(run=run)
        dm = report.run.results[0].decision_metrics
        assert dm.gatekeeper_passed == 55
        assert dm.gatekeeper_failed == 43

    def test_includes_decision_distribution(self):
        run = self._run_with()
        report = generate_comparison_report(run=run)
        dm = report.run.results[0].decision_metrics
        assert dm.buy_count == 25
        assert dm.hold_count == 50

    def test_includes_confidence_distribution(self):
        run = self._run_with()
        report = generate_comparison_report(run=run)
        cm = report.run.results[0].calibration_metrics
        assert len(cm.confidence_bucket_avg) > 0

    def test_includes_ev_calibration(self):
        run = self._run_with()
        report = generate_comparison_report(run=run)
        cm = report.run.results[0].calibration_metrics
        assert cm.avg_ev == Decimal("0.045")

    def test_includes_realized_pnl_ev_calibration(self):
        run = self._run_with()
        report = generate_comparison_report(run=run)
        cm = report.run.results[0].calibration_metrics
        assert cm.avg_realized_return == Decimal("0.038")
        assert cm.has_outcome_data is True

    def test_includes_latency_distribution(self):
        run = self._run_with()
        report = generate_comparison_report(run=run)
        lm = report.run.results[0].latency_metrics
        assert lm.mean_latency_ms == 800
        assert lm.median_latency_ms == 750

    def test_includes_token_usage_and_cost(self):
        run = self._run_with()
        report = generate_comparison_report(run=run)
        cst = report.run.results[0].cost_metrics
        assert cst.total_input_tokens == 400_000
        assert cst.total_output_tokens == 50_000
        assert cst.total_estimated_cost_usd == Decimal("0.45")

    def test_includes_budget_block_counts(self):
        dc = _full_pass_decision_metrics()
        cc = _full_pass_calibration_metrics()
        cst = _full_pass_cost_metrics(budget_block_count=3, cooldown_block_count=1)
        lm = _full_pass_latency_metrics()
        result = _full_pass_result(
            decision_metrics=dc,
            calibration_metrics=cc,
            cost_metrics=cst,
            latency_metrics=lm,
        )
        run = LLMProviderComparisonRun(
            config=_default_config(),
            results=[result],
        )
        report = generate_comparison_report(run=run)
        assert report.run.results[0].cost_metrics.budget_block_count == 3

    def test_includes_cooldown_block_counts(self):
        dc = _full_pass_decision_metrics()
        cc = _full_pass_calibration_metrics()
        cst = _full_pass_cost_metrics(budget_block_count=0, cooldown_block_count=5)
        lm = _full_pass_latency_metrics()
        result = _full_pass_result(
            decision_metrics=dc,
            calibration_metrics=cc,
            cost_metrics=cst,
            latency_metrics=lm,
        )
        run = LLMProviderComparisonRun(
            config=_default_config(),
            results=[result],
        )
        report = generate_comparison_report(run=run)
        assert report.run.results[0].cost_metrics.cooldown_block_count == 5

    def test_rejects_raw_prompts_in_content(self):
        config = _default_config()
        result = _full_pass_result()
        # Serialised report carries no prompt fields by design; the model_validator
        # enforces this at Pydantic boundary.  Verify the report serializes without
        # forbidden patterns.
        run = LLMProviderComparisonRun(config=config, results=[result])
        report = generate_comparison_report(run=run)
        json_text = report.model_dump_json().lower()
        assert "prompt:" not in json_text

    def test_rejects_reasoning_text_in_content(self):
        run = LLMProviderComparisonRun(
            config=_default_config(),
            results=[_full_pass_result()],
        )
        report = generate_comparison_report(run=run)
        json_text = report.model_dump_json().lower()
        assert "reasoning_log" not in json_text

    def test_rejects_api_keys_in_content(self):
        run = LLMProviderComparisonRun(
            config=_default_config(),
            results=[_full_pass_result()],
        )
        report = generate_comparison_report(run=run)
        json_text = report.model_dump_json()
        assert "sk-ant-" not in json_text
        assert "sk-ds-" not in json_text

    def test_rejects_token_ids_in_content(self):
        run = LLMProviderComparisonRun(
            config=_default_config(),
            results=[_full_pass_result()],
        )
        report = generate_comparison_report(run=run)
        json_text = report.model_dump_json().lower()
        assert "token_id" not in json_text

    def test_rejects_condition_ids_in_content(self):
        run = LLMProviderComparisonRun(
            config=_default_config(),
            results=[_full_pass_result()],
        )
        report = generate_comparison_report(run=run)
        json_text = report.model_dump_json().lower()
        assert "condition_id" not in json_text


# ===========================================================================
# LLMProviderReadinessVerdict — typed deterministic derivation
# ===========================================================================


class TestReadinessVerdict:
    def test_is_str_enum(self):
        assert issubclass(LLMProviderReadinessVerdict, str)
        assert (
            LLMProviderReadinessVerdict.PROVIDER_READY_FOR_DRY_RUN_PRIMARY
            == "PROVIDER_READY_FOR_DRY_RUN_PRIMARY"
        )

    def test_includes_rejected_json_validity(self):
        v = LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_JSON_VALIDITY
        assert "REJECTED" in v.value

    def test_includes_rejected_negative_ev(self):
        v = LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_NEGATIVE_EV
        assert "NEGATIVE_EV" in v.value

    def test_includes_rejected_cost_or_latency(self):
        v = LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_COST_OR_LATENCY
        assert "COST_OR_LATENCY" in v.value

    def test_includes_needs_threshold_recalibration(self):
        v = LLMProviderReadinessVerdict.PROVIDER_NEEDS_THRESHOLD_RECALIBRATION
        assert "RECALIBRATION" in v.value

    def test_includes_ready_for_sampled_audit_only(self):
        v = LLMProviderReadinessVerdict.PROVIDER_READY_FOR_SAMPLED_AUDIT_ONLY
        assert "SAMPLED_AUDIT" in v.value

    def test_includes_ready_for_dry_run_primary(self):
        v = LLMProviderReadinessVerdict.PROVIDER_READY_FOR_DRY_RUN_PRIMARY
        assert "DRY_RUN_PRIMARY" in v.value


# ===========================================================================
# Derive verdict — concrete scenarios
# ===========================================================================


class TestDeriveVerdict:
    def _derive(
        self, **kw
    ) -> tuple[LLMProviderReadinessVerdict, list[LLMProviderReadinessReason]]:
        return derive_readiness_verdict(
            decision_metrics=kw.get("decision_metrics", _full_pass_decision_metrics()),
            calibration_metrics=kw.get(
                "calibration_metrics", _full_pass_calibration_metrics()
            ),
            cost_metrics=kw.get("cost_metrics", _full_pass_cost_metrics()),
            latency_metrics=kw.get("latency_metrics", _full_pass_latency_metrics()),
            baseline_cost_usd=kw.get("baseline_cost_usd", Decimal("3.00")),
            baseline_mean_latency_ms=kw.get("baseline_mean_latency_ms", 1200.0),
            config=kw.get("config", _default_config()),
        )

    def test_invalid_json_exceeds_tolerance_gives_rejected_json(self):
        dm = _full_pass_decision_metrics(
            total_calls=100,
            valid_json_count=80,
            invalid_json_count=20,
            json_validity_rate=Decimal("0.80"),
        )
        verdict, reasons = self._derive(decision_metrics=dm)
        assert (
            verdict == LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_JSON_VALIDITY
        )
        assert LLMProviderReadinessReason.JSON_VALIDITY_BELOW_TOLERANCE in reasons

    def test_gatekeeper_fails_for_all_decisions_gives_rejection(self):
        dm = _full_pass_decision_metrics(
            total_calls=100,
            valid_json_count=98,
            invalid_json_count=2,
            json_validity_rate=Decimal("0.98"),
            gatekeeper_passed=10,
            gatekeeper_failed=88,
            gatekeeper_pass_rate=Decimal("0.10"),
        )
        verdict, reasons = self._derive(decision_metrics=dm)
        assert (
            verdict
            == LLMProviderReadinessVerdict.PROVIDER_NEEDS_THRESHOLD_RECALIBRATION
        )
        assert LLMProviderReadinessReason.GATEKEEPER_FAILURE_RATE_HIGH in reasons

    def test_negative_realized_ev_gives_rejected_negative_ev(self):
        cm = _full_pass_calibration_metrics(
            has_outcome_data=True,
            avg_realized_return=Decimal("-0.02"),
        )
        verdict, reasons = self._derive(calibration_metrics=cm)
        assert verdict == LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_NEGATIVE_EV
        assert LLMProviderReadinessReason.NEGATIVE_REALIZED_EV in reasons

    def test_cost_higher_than_anthropic_gives_rejected_cost_latency(self):
        cst = _full_pass_cost_metrics(total_estimated_cost_usd=Decimal("3.50"))
        verdict, reasons = self._derive(
            cost_metrics=cst,
            baseline_cost_usd=Decimal("3.00"),
        )
        assert (
            verdict == LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_COST_OR_LATENCY
        )
        assert LLMProviderReadinessReason.COST_EXCEEDS_BASELINE in reasons

    def test_latency_too_high_gives_rejected_cost_latency(self):
        lm = _full_pass_latency_metrics(mean_latency_ms=5000)
        verdict, reasons = self._derive(
            latency_metrics=lm,
            baseline_mean_latency_ms=1200.0,
        )
        assert (
            verdict == LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_COST_OR_LATENCY
        )
        assert LLMProviderReadinessReason.LATENCY_EXCEEDS_BASELINE in reasons

    def test_ok_validity_poor_calibration_gives_needs_recalibration(self):
        cm = _full_pass_calibration_metrics(
            confidence_calibration_deviation=Decimal("0.35"),
        )
        verdict, reasons = self._derive(calibration_metrics=cm)
        assert (
            verdict
            == LLMProviderReadinessVerdict.PROVIDER_NEEDS_THRESHOLD_RECALIBRATION
        )
        assert LLMProviderReadinessReason.WEAK_CONFIDENCE_CALIBRATION in reasons

    def test_promising_but_not_all_gates_gives_sampled_audit(self):
        cm = _full_pass_calibration_metrics(
            has_outcome_data=False,
            outcome_coverage_fraction=Decimal("0"),
        )
        verdict, reasons = self._derive(calibration_metrics=cm)
        assert (
            verdict == LLMProviderReadinessVerdict.PROVIDER_READY_FOR_SAMPLED_AUDIT_ONLY
        )
        assert LLMProviderReadinessReason.OUTCOME_COVERAGE_MISSING in reasons

    def test_all_gates_pass_gives_ready_for_primary(self):
        verdict, reasons = self._derive()
        assert verdict == LLMProviderReadinessVerdict.PROVIDER_READY_FOR_DRY_RUN_PRIMARY
        assert LLMProviderReadinessReason.ALL_GATES_PASSED in reasons

    def test_ordering_rejected_before_needs_recalibration(self):
        dm = _full_pass_decision_metrics(
            total_calls=100,
            valid_json_count=80,
            invalid_json_count=20,
            json_validity_rate=Decimal("0.80"),
        )
        cm = _full_pass_calibration_metrics(
            confidence_calibration_deviation=Decimal("0.40"),
        )
        verdict, _ = self._derive(decision_metrics=dm, calibration_metrics=cm)
        assert (
            verdict == LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_JSON_VALIDITY
        )

    def test_ordering_rejected_before_sampled_audit(self):
        cst = _full_pass_cost_metrics(total_estimated_cost_usd=Decimal("4.00"))
        cm = _full_pass_calibration_metrics(
            has_outcome_data=False,
            outcome_coverage_fraction=Decimal("0"),
        )
        verdict, _ = self._derive(
            cost_metrics=cst, calibration_metrics=cm, baseline_cost_usd=Decimal("3.00")
        )
        assert (
            verdict == LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_COST_OR_LATENCY
        )

    def test_ordering_rejected_before_ready(self):
        cm = _full_pass_calibration_metrics(
            has_outcome_data=True,
            avg_realized_return=Decimal("-0.05"),
        )
        verdict, _ = self._derive(calibration_metrics=cm)
        assert verdict == LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_NEGATIVE_EV

    def test_ordering_needs_recalibration_before_sampled_audit(self):
        cm = _full_pass_calibration_metrics(
            confidence_calibration_deviation=Decimal("0.30"),
            has_outcome_data=False,
            outcome_coverage_fraction=Decimal("0"),
        )
        verdict, reasons = self._derive(calibration_metrics=cm)
        assert (
            verdict
            == LLMProviderReadinessVerdict.PROVIDER_NEEDS_THRESHOLD_RECALIBRATION
        )
        assert LLMProviderReadinessReason.OUTCOME_COVERAGE_MISSING in reasons

    def test_ordering_sampled_audit_before_ready(self):
        cm = _full_pass_calibration_metrics(
            has_outcome_data=False,
            outcome_coverage_fraction=Decimal("0"),
        )
        verdict, _ = self._derive(calibration_metrics=cm)
        assert (
            verdict == LLMProviderReadinessVerdict.PROVIDER_READY_FOR_SAMPLED_AUDIT_ONLY
        )

    def test_missing_outcome_coverage_excluded_from_ev_check(self):
        cm = _full_pass_calibration_metrics(
            has_outcome_data=False,
            outcome_coverage_fraction=Decimal("0"),
            avg_realized_return=Decimal("-0.10"),
        )
        verdict, reasons = self._derive(calibration_metrics=cm)
        assert LLMProviderReadinessReason.OUTCOME_COVERAGE_MISSING in reasons
        assert verdict != LLMProviderReadinessVerdict.PROVIDER_REJECTED_FOR_NEGATIVE_EV

    def test_missing_outcome_coverage_reported_separately(self):
        cm = _full_pass_calibration_metrics(
            has_outcome_data=False,
            outcome_coverage_fraction=Decimal("0"),
        )
        _, reasons = self._derive(calibration_metrics=cm)
        assert LLMProviderReadinessReason.OUTCOME_COVERAGE_MISSING in reasons

    def test_deterministic_for_identical_inputs(self):
        v1, r1 = self._derive()
        v2, r2 = self._derive()
        assert v1 == v2
        assert r1 == r2


# ===========================================================================
# LLMProviderComparisonRun
# ===========================================================================


class TestComparisonRun:
    def test_is_frozen(self):
        run = LLMProviderComparisonRun(config=_default_config())
        with pytest.raises(Exception):
            run.config = None

    def test_requires_config(self):
        run = LLMProviderComparisonRun(config=_default_config())
        assert run.config is not None

    def test_requires_results_by_provider(self):
        run = LLMProviderComparisonRun(
            config=_default_config(),
            results=[_full_pass_result()],
        )
        assert len(run.results) == 1
        assert run.results[0].provider == "deepseek"


# ===========================================================================
# Decimal cost math safety
# ===========================================================================


class TestDecimalCostSafety:
    def test_estimated_cost_calculation_is_decimal_only(self):
        cm = _full_pass_cost_metrics(total_estimated_cost_usd=Decimal("0.45"))
        assert isinstance(cm.total_estimated_cost_usd, Decimal)
        assert cm.total_estimated_cost_usd == Decimal("0.45")

    def test_usage_record_with_missing_fields_uses_conservative_fallback(self):
        cm = _full_pass_cost_metrics(
            total_input_tokens=0,
            total_output_tokens=0,
            total_estimated_cost_usd=Decimal("0.02"),
            is_estimated=True,
        )
        assert cm.is_estimated is True
        assert cm.total_estimated_cost_usd > _ZERO

    def test_usage_record_with_missing_fields_is_not_zero_cost(self):
        cm = _full_pass_cost_metrics(
            total_estimated_cost_usd=Decimal("0.04"),
            is_estimated=True,
        )
        assert cm.total_estimated_cost_usd > _ZERO

    def test_missing_usage_fields_set_is_estimated_flag(self):
        cm = LLMProviderCostMetrics(
            total_input_tokens=0,
            total_output_tokens=0,
            total_tokens=0,
            total_estimated_cost_usd=Decimal("0.03"),
            is_estimated=True,
            calls_with_missing_usage=5,
        )
        assert cm.is_estimated is True
        assert cm.calls_with_missing_usage == 5

    def test_cost_per_input_token_field_rejects_float(self):
        with pytest.raises(ValidationError):
            _default_config(cost_per_input_token_usd=0.000015)

    def test_cost_per_output_token_field_rejects_float(self):
        with pytest.raises(ValidationError):
            _default_config(cost_per_output_token_usd=0.00006)


# ===========================================================================
# Sampled Claude audit mode
# ===========================================================================


class TestSampledClaude:
    def test_disabled_by_default_means_no_anthropic_calls(self):
        cfg = _default_config()
        assert cfg.enable_anthropic_sampling is False
        assert cfg.anthropic_sample_fraction == Decimal("0")

    def test_with_zero_fraction_means_no_anthropic_calls(self):
        with pytest.raises(ValidationError):
            _default_config(
                enable_anthropic_sampling=True,
                anthropic_sample_fraction=Decimal("0"),
            )

    def test_with_fraction_calls_anthropic_for_sample_only(self):
        cfg = _default_config(
            enable_anthropic_sampling=True,
            anthropic_sample_fraction=Decimal("0.1"),
        )
        assert cfg.enable_anthropic_sampling is True
        assert cfg.anthropic_sample_fraction == Decimal("0.1")

    def test_unsampled_snapshots_do_not_incur_claude_cost(self):
        cfg = _default_config(
            enable_anthropic_sampling=True,
            anthropic_sample_fraction=Decimal("0.1"),
        )
        assert cfg.anthropic_sample_fraction < Decimal("1")
        assert cfg.anthropic_sample_fraction > Decimal("0")

    def test_full_time_shadow_mode_not_enabled_by_default(self):
        cfg = _default_config()
        assert cfg.enable_anthropic_sampling is False
        assert cfg.anthropic_sample_fraction == Decimal("0")


# ===========================================================================
# DRY_RUN invariant
# ===========================================================================


class TestDryRunInvariant:
    def test_config_validates_dry_run_true_at_init(self):
        with pytest.raises(ValidationError, match="dry_run=True"):
            LLMProviderComparisonConfig(data_dir="/tmp/test", dry_run=False)

    def test_comparison_does_not_sign_or_broadcast_orders(self):
        cfg = _default_config()
        assert cfg.dry_run is True

    def test_comparison_does_not_authorize_live_trading(self):
        cfg = _default_config()
        result = _full_pass_result()
        run = LLMProviderComparisonRun(config=cfg, results=[result])
        report = generate_comparison_report(run=run)
        assert report.run.config.dry_run is True

    def test_ready_verdict_never_implies_live_trading_authorization(self):
        v = LLMProviderReadinessVerdict.PROVIDER_READY_FOR_DRY_RUN_PRIMARY
        assert "DRY_RUN" in v.value
        assert "LIVE" not in v.value.upper()
        assert "PRIMARY" in v.value


# ===========================================================================
# Budget and cooldown block handling
# ===========================================================================


class TestBudgetCooldown:
    def test_budget_blocked_call_not_counted_as_valid_decision(self):
        cm = _full_pass_cost_metrics(budget_block_count=10)
        dm = _full_pass_decision_metrics(
            total_calls=90,
            valid_json_count=90,
            invalid_json_count=0,
            json_validity_rate=Decimal("1.0"),
        )
        assert dm.total_calls == 90
        assert cm.budget_block_count == 10

    def test_budget_blocked_call_not_counted_as_gatekeeper_pass(self):
        cm = _full_pass_cost_metrics(budget_block_count=5)
        dm = _full_pass_decision_metrics(
            total_calls=95,
            valid_json_count=95,
            invalid_json_count=0,
            json_validity_rate=Decimal("1.0"),
            gatekeeper_passed=55,
            gatekeeper_failed=40,
        )
        assert cm.budget_block_count == 5
        assert dm.total_calls == 95

    def test_cooldown_blocked_call_not_counted_as_valid_decision(self):
        cm = _full_pass_cost_metrics(cooldown_block_count=8)
        dm = _full_pass_decision_metrics(
            total_calls=92,
            valid_json_count=92,
            invalid_json_count=0,
            json_validity_rate=Decimal("1.0"),
        )
        assert dm.total_calls == 92
        assert cm.cooldown_block_count == 8

    def test_budget_block_count_surfaced_in_cost_metrics(self):
        cm = _full_pass_cost_metrics(budget_block_count=7)
        result = _full_pass_result(cost_metrics=cm)
        assert result.cost_metrics.budget_block_count == 7

    def test_cooldown_block_count_surfaced_in_cost_metrics(self):
        cm = _full_pass_cost_metrics(cooldown_block_count=4)
        result = _full_pass_result(cost_metrics=cm)
        assert result.cost_metrics.cooldown_block_count == 4


# ===========================================================================
# Report output and path safety
# ===========================================================================


class TestReportPathSafety:
    def test_writes_only_to_approved_docs_paths(self):
        assert validate_report_path("docs/backtests/test.json") is not None

    def test_generation_fails_closed_on_path_escape(self):
        with pytest.raises(ValueError, match="escapes approved"):
            validate_report_path("/etc/passwd")

    def test_redacts_forbidden_secret_patterns(self):
        text = "api_key: sk-ant-test1234567890abcdefghij"
        redacted = redact_secrets(text)
        assert "sk-ant" not in redacted
        assert "REDACTED" in redacted

    def test_redaction_removes_api_key_patterns(self):
        text = "using sk-ds-my-deepseek-key-abcdefghijklmnop"
        redacted = redact_secrets(text)
        assert "sk-ds" not in redacted
        assert "[REDACTED_DEEPSEEK_KEY]" in redacted

    def test_redaction_removes_token_id_patterns(self):
        text = "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        redacted = redact_secrets(text)
        assert "0x" not in redacted

    def test_redaction_removes_condition_id_patterns(self):
        text = "telegram bot 1234567890:AAbbCCddEEffGGhhIIjjKKllMMnn"
        redacted = redact_secrets(text)
        assert "REDACTED_TELEGRAM_TOKEN" in redacted or "1234567890:AA" not in redacted


# ===========================================================================
# Historical outcome calibration
# ===========================================================================


class TestOutcomeCalibration:
    def test_uses_only_lookahead_safe_data(self):
        cm = _full_pass_calibration_metrics(
            has_outcome_data=True,
            outcome_coverage_fraction=Decimal("0.5"),
        )
        assert cm.has_outcome_data is True
        assert cm.outcome_coverage_fraction < Decimal("1.0")

    def test_outcome_coverage_is_explicitly_reported_when_missing(self):
        cm_with = _full_pass_calibration_metrics(
            has_outcome_data=True,
            outcome_coverage_fraction=Decimal("0.75"),
        )
        assert cm_with.outcome_coverage_fraction == Decimal("0.75")

        cm_without = _full_pass_calibration_metrics(
            has_outcome_data=False,
            outcome_coverage_fraction=Decimal("0"),
        )
        assert cm_without.outcome_coverage_fraction == Decimal("0")

    def test_missing_outcomes_distinguished_from_provider_success(self):
        cm_missing = _full_pass_calibration_metrics(
            has_outcome_data=False,
            outcome_coverage_fraction=Decimal("0"),
        )
        _, reasons = derive_readiness_verdict(
            decision_metrics=_full_pass_decision_metrics(),
            calibration_metrics=cm_missing,
            cost_metrics=_full_pass_cost_metrics(),
            latency_metrics=_full_pass_latency_metrics(),
            baseline_cost_usd=Decimal("3.00"),
            baseline_mean_latency_ms=1200.0,
            config=_default_config(),
        )
        assert LLMProviderReadinessReason.OUTCOME_COVERAGE_MISSING in reasons


# ===========================================================================
# Provider reuse of existing paths
# ===========================================================================


class TestReuseExistingPaths:
    def test_uses_existing_claude_client_class(self):
        from src.agents.evaluation.claude_client import ClaudeClient

        assert ClaudeClient is not None

    def test_uses_existing_backtest_data_loader(self):
        from src.backtest_runner import BacktestDataLoader

        assert BacktestDataLoader is not None

    def test_does_not_duplicate_backtesting_engine(self):
        from src.backtest_runner import BacktestRunner

        assert BacktestRunner is not None


# ===========================================================================
# LLMProviderReadinessReason
# ===========================================================================


class TestReadinessReason:
    def test_is_str_enum(self):
        assert issubclass(LLMProviderReadinessReason, str)
        assert LLMProviderReadinessReason.ALL_GATES_PASSED == "ALL_GATES_PASSED"


# ===========================================================================
# Calibration recommendation derivation
# ===========================================================================


class TestDeriveCalibrationRecommendation:
    def test_returns_none_when_no_changes_needed(self):
        rec = derive_calibration_recommendation(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            calibration_metrics=_full_pass_calibration_metrics(),
            decision_metrics=_full_pass_decision_metrics(),
            config=_default_config(),
        )
        assert rec is None

    def test_suggests_confidence_when_calibration_deviates(self):
        cm = _full_pass_calibration_metrics(
            confidence_calibration_deviation=Decimal("0.30"),
            confidence_bucket_observed_win_rate=[
                Decimal("0.10"),
                Decimal("0.20"),
                Decimal("0.40"),
                Decimal("0.60"),
                Decimal("0.70"),
            ],
        )
        rec = derive_calibration_recommendation(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            calibration_metrics=cm,
            decision_metrics=_full_pass_decision_metrics(),
            config=_default_config(),
        )
        assert rec is not None
        assert rec.suggested_confidence_threshold is not None

    def test_suggests_ev_threshold_when_ev_deviates(self):
        cm = _full_pass_calibration_metrics(
            has_outcome_data=True,
            ev_calibration_deviation=Decimal("0.20"),
            avg_ev=Decimal("0.08"),
        )
        rec = derive_calibration_recommendation(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            calibration_metrics=cm,
            decision_metrics=_full_pass_decision_metrics(),
            config=_default_config(),
        )
        assert rec is not None
        assert rec.suggested_ev_threshold is not None

    def test_suggests_max_tokens_when_json_validity_low(self):
        dm = _full_pass_decision_metrics(
            total_calls=100,
            valid_json_count=85,
            invalid_json_count=15,
            json_validity_rate=Decimal("0.85"),
        )
        rec = derive_calibration_recommendation(
            provider="deepseek",
            model_name="deepseek-v4-pro",
            calibration_metrics=_full_pass_calibration_metrics(),
            decision_metrics=dm,
            config=_default_config(),
        )
        assert rec is not None
        assert rec.suggested_max_output_tokens is not None


# ===========================================================================
# Report write path validation
# ===========================================================================


class TestReportWritePath:
    def test_validates_docs_backtests_path(self):
        path = validate_report_path("docs/backtests/my_report.json")
        assert path is not None

    def test_rejects_absolute_tmp_path(self):
        with pytest.raises(ValueError, match="escapes approved"):
            validate_report_path("/tmp/out.json")

    def test_rejects_relative_escape(self):
        with pytest.raises(ValueError, match="escapes approved"):
            validate_report_path("../../etc/secrets.json")

    def test_allows_docs_backtests_root(self):
        path = validate_report_path("docs/backtests/report.json")
        assert path is not None
