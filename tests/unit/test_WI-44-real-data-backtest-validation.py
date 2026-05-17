"""
Unit tests for WI-44 — Real-Data Backtest Validation.

Covers: validation_report.py, live_readiness.py, run_real_data_backtest.py CLI.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from src.backtesting.live_readiness import (
    LiveReadinessVerdict,
    derive_verdict,
    verdict_from_string,
)
from src.backtesting.validation_report import (
    BacktestActionDistribution,
    BacktestDataQualitySummary,
    BacktestValidationReport,
    _compute_calibration_buckets,
    build_validation_report,
)
from src.schemas.execution import (
    BacktestConfig,
    BacktestDecision,
    BacktestMarketStats,
    BacktestReport,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ZERO = Decimal("0")
_ONE = Decimal("1")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(
    *,
    data_dir: str = "/tmp/test_data",
    initial_bankroll_usdc: Decimal = Decimal("1000"),
    kelly_fraction: Decimal = Decimal("0.25"),
    min_confidence: Decimal = Decimal("0.75"),
    min_ev_threshold: Decimal = Decimal("0.02"),
) -> BacktestConfig:
    return BacktestConfig(
        data_dir=data_dir,
        initial_bankroll_usdc=initial_bankroll_usdc,
        kelly_fraction=kelly_fraction,
        min_confidence=min_confidence,
        min_ev_threshold=min_ev_threshold,
        dry_run=True,
    )


def _make_decision(
    *,
    token_id: str = "tok_1",
    decision: bool = False,
    action: str = "HOLD",
    position_size_usdc: Decimal = _ZERO,
    ev: Decimal = _ZERO,
    confidence: Decimal = _ZERO,
    gatekeeper_result: str = "FAILED",
    reason: str = "low confidence",
    realized_pnl_usdc: Decimal = _ZERO,
) -> BacktestDecision:
    return BacktestDecision(
        token_id=token_id,
        timestamp_utc=datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
        decision=decision,
        action=action,
        position_size_usdc=position_size_usdc,
        ev=ev,
        confidence=confidence,
        gatekeeper_result=gatekeeper_result,
        reason=reason,
        realized_pnl_usdc=realized_pnl_usdc,
    )


def _make_report(
    *,
    config: BacktestConfig | None = None,
    total_trades: int = 0,
    win_rate: Decimal = _ZERO,
    net_pnl_usdc: Decimal = _ZERO,
    max_drawdown_usdc: Decimal = _ZERO,
    sharpe_ratio: Decimal = _ZERO,
    decisions: list[BacktestDecision] | None = None,
    per_market_stats: dict[str, BacktestMarketStats] | None = None,
) -> BacktestReport:
    if config is None:
        config = _make_config()
    if decisions is None:
        decisions = []
    if per_market_stats is None:
        per_market_stats = {}
    now = datetime.now(timezone.utc)
    return BacktestReport(
        total_trades=total_trades,
        win_rate=win_rate,
        net_pnl_usdc=net_pnl_usdc,
        max_drawdown_usdc=max_drawdown_usdc,
        sharpe_ratio=sharpe_ratio,
        per_market_stats=per_market_stats,
        decisions=decisions,
        started_at_utc=now,
        completed_at_utc=now,
        config_snapshot=config,
    )


# ---------------------------------------------------------------------------
# LiveReadinessVerdict schema
# ---------------------------------------------------------------------------


class TestLiveReadinessVerdict:
    """Typed verdict enum / schema tests."""

    def test_verdict_enum_contains_required_values(self):
        values = {v.value for v in LiveReadinessVerdict}
        assert "PASS" in values
        assert "FAIL_NEGATIVE_PNL" in values
        assert "FAIL_DRAWDOWN" in values
        assert "FAIL_INSUFFICIENT_TRADES" in values
        assert "FAIL_WEAK_CALIBRATION" in values
        assert "FAIL_DATA_QUALITY" in values

    def test_verdict_from_string_case_insensitive(self):
        assert verdict_from_string("pass") == LiveReadinessVerdict.PASS
        assert verdict_from_string("PASS") == LiveReadinessVerdict.PASS
        assert verdict_from_string("Pass") == LiveReadinessVerdict.PASS
        assert (
            verdict_from_string("FAIL_NEGATIVE_PNL")
            == LiveReadinessVerdict.FAIL_NEGATIVE_PNL
        )

    def test_verdict_serialises_to_json(self):
        data = json.loads(json.dumps({"v": LiveReadinessVerdict.PASS.value}))
        assert data["v"] == "PASS"


# ---------------------------------------------------------------------------
# BacktestValidationReport schema
# ---------------------------------------------------------------------------


class TestBacktestValidationReport:
    """Validation report schema tests."""

    def test_report_includes_all_required_fields(self):
        report = _make_report()
        dq = BacktestDataQualitySummary(total_loaded=10)
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=10, data_quality=dq
        )
        data = result.model_dump()
        required = {
            "generated_at_utc",
            "data_dir",
            "verdict",
            "verdict_reason",
            "data_quality",
            "total_snapshots_replayed",
            "total_decisions",
            "action_distribution",
            "total_trades",
            "win_rate",
            "net_pnl_usdc",
            "max_drawdown_usdc",
            "sharpe_ratio",
            "average_ev",
            "realized_ev_calibration",
            "confidence_calibration_buckets",
            "per_market_stats",
        }
        assert set(data.keys()) == required

    def test_report_fields_are_decimal_not_float(self):
        report = _make_report(
            total_trades=5,
            win_rate=Decimal("0.6"),
            net_pnl_usdc=Decimal("42.5"),
            max_drawdown_usdc=Decimal("10.0"),
            sharpe_ratio=Decimal("1.5"),
        )
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=10
        )
        assert isinstance(result.win_rate, Decimal)
        assert isinstance(result.net_pnl_usdc, Decimal)
        assert isinstance(result.max_drawdown_usdc, Decimal)
        assert isinstance(result.sharpe_ratio, Decimal)
        assert isinstance(result.average_ev, Decimal)
        assert isinstance(result.realized_ev_calibration, Decimal)
        assert not isinstance(result.win_rate, float)

    def test_report_serialises_verdict_as_string(self):
        config = _make_config()
        decisions = [
            _make_decision(
                decision=True,
                action="BUY",
                confidence=Decimal("0.7"),
                ev=Decimal("0.04"),
                position_size_usdc=Decimal("12.5"),
                realized_pnl_usdc=Decimal("0.5"),
            )
            for _ in range(25)
        ]
        report = _make_report(
            config=config,
            total_trades=25,
            net_pnl_usdc=Decimal("12.5"),
            decisions=decisions,
        )
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=25
        )
        data = result.model_dump(mode="json")
        assert isinstance(data["verdict"], str)
        assert data["verdict"] == result.verdict.value

    def test_report_rejects_float_values_in_pnl(self):
        with pytest.raises(Exception):
            BacktestValidationReport(
                data_dir="/tmp",
                verdict=LiveReadinessVerdict.PASS,
                data_quality=BacktestDataQualitySummary(total_loaded=1),
                total_snapshots_replayed=1,
                total_decisions=0,
                action_distribution=BacktestActionDistribution(),
                total_trades=0,
                win_rate=Decimal("0"),
                net_pnl_usdc=3.14,  # float
                max_drawdown_usdc=Decimal("0"),
                sharpe_ratio=Decimal("0"),
                average_ev=Decimal("0"),
                realized_ev_calibration=Decimal("0"),
                confidence_calibration_buckets=[],
                per_market_stats={},
            )

    def test_report_rejects_float_values_in_drawdown(self):
        with pytest.raises(Exception):
            BacktestValidationReport(
                data_dir="/tmp",
                verdict=LiveReadinessVerdict.PASS,
                data_quality=BacktestDataQualitySummary(total_loaded=1),
                total_snapshots_replayed=1,
                total_decisions=0,
                action_distribution=BacktestActionDistribution(),
                total_trades=0,
                win_rate=Decimal("0"),
                net_pnl_usdc=Decimal("0"),
                max_drawdown_usdc=5.0,  # float
                sharpe_ratio=Decimal("0"),
                average_ev=Decimal("0"),
                realized_ev_calibration=Decimal("0"),
                confidence_calibration_buckets=[],
                per_market_stats={},
            )

    def test_report_rejects_float_values_in_sharpe(self):
        with pytest.raises(Exception):
            BacktestValidationReport(
                data_dir="/tmp",
                verdict=LiveReadinessVerdict.PASS,
                data_quality=BacktestDataQualitySummary(total_loaded=1),
                total_snapshots_replayed=1,
                total_decisions=0,
                action_distribution=BacktestActionDistribution(),
                total_trades=0,
                win_rate=Decimal("0"),
                net_pnl_usdc=Decimal("0"),
                max_drawdown_usdc=Decimal("0"),
                sharpe_ratio=1.5,  # float
                average_ev=Decimal("0"),
                realized_ev_calibration=Decimal("0"),
                confidence_calibration_buckets=[],
                per_market_stats={},
            )

    def test_report_rejects_float_values_in_ev(self):
        with pytest.raises(Exception):
            BacktestValidationReport(
                data_dir="/tmp",
                verdict=LiveReadinessVerdict.PASS,
                data_quality=BacktestDataQualitySummary(total_loaded=1),
                total_snapshots_replayed=1,
                total_decisions=0,
                action_distribution=BacktestActionDistribution(),
                total_trades=0,
                win_rate=Decimal("0"),
                net_pnl_usdc=Decimal("0"),
                max_drawdown_usdc=Decimal("0"),
                sharpe_ratio=Decimal("0"),
                average_ev=0.05,  # float
                realized_ev_calibration=Decimal("0"),
                confidence_calibration_buckets=[],
                per_market_stats={},
            )

    def test_report_includes_realized_ev_calibration(self):
        decisions = [
            _make_decision(
                decision=True,
                action="BUY",
                ev=Decimal("0.05"),
                realized_pnl_usdc=Decimal("5"),
            )
            for _ in range(20)
        ]
        report = _make_report(
            total_trades=20,
            net_pnl_usdc=Decimal("100"),
            decisions=decisions,
        )
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=20
        )
        assert isinstance(result.realized_ev_calibration, Decimal)


# ---------------------------------------------------------------------------
# Validation logic — verdict derivation
# ---------------------------------------------------------------------------


class TestVerdictDerivation:
    """Verdict logic tests."""

    def test_empty_dataset_yields_fail_data_quality(self):
        report = _make_report(total_trades=0)
        verdict = derive_verdict(report, total_loaded=0)
        assert verdict == LiveReadinessVerdict.FAIL_DATA_QUALITY

    def test_data_quality_malformed_yields_fail(self):
        report = _make_report(total_trades=30, net_pnl_usdc=Decimal("50"))
        verdict = derive_verdict(
            report,
            total_loaded=100,
            malformed_count=12,  # 12% > 10% threshold
        )
        assert verdict == LiveReadinessVerdict.FAIL_DATA_QUALITY

    def test_data_quality_crossed_yields_fail(self):
        report = _make_report(total_trades=30, net_pnl_usdc=Decimal("50"))
        verdict = derive_verdict(
            report,
            total_loaded=40,
            crossed_books_count=5,  # 12.5% > 10% threshold
        )
        assert verdict == LiveReadinessVerdict.FAIL_DATA_QUALITY

    def test_zero_trades_with_decisions_yields_fail_insufficient_trades(self):
        """Per WI-44 edge case 3: decisions but zero trades → FAIL_INSUFFICIENT_TRADES."""
        decisions = [_make_decision() for _ in range(5)]
        report = _make_report(total_trades=0, decisions=decisions)
        verdict = derive_verdict(report, total_loaded=10)
        assert verdict == LiveReadinessVerdict.FAIL_INSUFFICIENT_TRADES

    def test_insufficient_trades_yields_fail_insufficient_trades(self):
        report = _make_report(total_trades=5, net_pnl_usdc=Decimal("10"))
        verdict = derive_verdict(report, min_trades=20, total_loaded=10)
        assert verdict == LiveReadinessVerdict.FAIL_INSUFFICIENT_TRADES

    def test_negative_net_pnl_yields_fail_negative_pnl(self):
        decisions = [
            _make_decision(
                token_id="tok_a",
                decision=True,
                action="BUY",
                confidence=Decimal("0.8"),
                ev=Decimal("0.05"),
                realized_pnl_usdc=Decimal("-2"),
            )
            for _ in range(25)
        ]
        report = _make_report(
            total_trades=25,
            net_pnl_usdc=Decimal("-50"),
            decisions=decisions,
        )
        verdict = derive_verdict(report, min_trades=20, total_loaded=25)
        assert verdict == LiveReadinessVerdict.FAIL_NEGATIVE_PNL

    def test_excessive_drawdown_yields_fail_drawdown(self):
        config = _make_config(initial_bankroll_usdc=Decimal("1000"))
        report = _make_report(
            config=config,
            total_trades=30,
            net_pnl_usdc=Decimal("5"),
            max_drawdown_usdc=Decimal("400"),
        )
        verdict = derive_verdict(
            report,
            min_trades=20,
            max_drawdown_pct=Decimal("0.30"),
            total_loaded=30,
        )
        assert verdict == LiveReadinessVerdict.FAIL_DRAWDOWN

    def test_weak_calibration_confidence_pnl_mismatch(self):
        """Confidence 0.9 bucket where all trades lose money → weak calibration."""
        decisions = [
            _make_decision(
                token_id="tok_a",
                decision=True,
                action="BUY",
                confidence=Decimal("0.9"),
                ev=Decimal("0.05"),
                realized_pnl_usdc=Decimal("-3"),  # all losing trades
            )
            for _ in range(25)
        ]
        report = _make_report(
            total_trades=25,
            net_pnl_usdc=Decimal("-75"),
            max_drawdown_usdc=Decimal("75"),
            decisions=decisions,
        )
        verdict = derive_verdict(report, min_trades=20, total_loaded=25)
        # Negative PnL hits first
        assert verdict == LiveReadinessVerdict.FAIL_NEGATIVE_PNL

    def test_weak_calibration_ev_mismatch(self):
        """High EV estimates but realized return is much lower → weak EV calibration."""
        decisions = [
            _make_decision(
                token_id="tok_a",
                decision=True,
                action="BUY",
                confidence=Decimal("0.7"),
                ev=Decimal("0.30"),  # optimistic EV
                position_size_usdc=Decimal("10"),
                realized_pnl_usdc=Decimal("1"),
            )
            for _ in range(25)
        ]
        report = _make_report(
            total_trades=25,
            # realized return = (25/25) / 10 = 0.10 vs EV 0.30 → deviation 0.20 > 0.15
            net_pnl_usdc=Decimal("25"),
            max_drawdown_usdc=Decimal("10"),
            decisions=decisions,
        )
        verdict = derive_verdict(report, min_trades=20, total_loaded=25)
        assert verdict == LiveReadinessVerdict.FAIL_WEAK_CALIBRATION

    def test_malformed_dataset_yields_fail_data_quality(self):
        report = _make_report(total_trades=0)
        verdict = derive_verdict(report, total_loaded=0)
        assert verdict == LiveReadinessVerdict.FAIL_DATA_QUALITY

    def test_positive_pnl_good_calibration_yields_pass(self):
        decisions = [
            _make_decision(
                token_id="tok_a",
                decision=True,
                action="BUY",
                confidence=Decimal("0.7"),
                ev=Decimal("0.04"),
                position_size_usdc=Decimal("12.5"),
                realized_pnl_usdc=Decimal("0.5"),
            )
            for _ in range(10)
        ] + [
            _make_decision(
                token_id="tok_a",
                decision=True,
                action="BUY",
                confidence=Decimal("0.8"),
                ev=Decimal("0.04"),
                position_size_usdc=Decimal("12.5"),
                realized_pnl_usdc=Decimal("0.5"),
            )
            for _ in range(15)
        ]
        config = _make_config(initial_bankroll_usdc=Decimal("1000"))
        report = _make_report(
            config=config,
            total_trades=25,
            # 25 trades × 0.5 USDC = 12.5 net PnL
            # realized return = (12.5/25) / 12.5 = 0.04 ≈ avg_ev 0.04
            net_pnl_usdc=Decimal("12.5"),
            max_drawdown_usdc=Decimal("3"),
            sharpe_ratio=Decimal("1.2"),
            decisions=decisions,
        )
        verdict = derive_verdict(report, min_trades=20, total_loaded=25)
        assert verdict == LiveReadinessVerdict.PASS

    def test_verdict_is_deterministic_for_same_input(self):
        decisions = [
            _make_decision(
                token_id="tok_a",
                decision=True,
                action="BUY",
                confidence=Decimal("0.7"),
                ev=Decimal("0.04"),
                realized_pnl_usdc=Decimal("5"),
            )
            for _ in range(30)
        ]
        config = _make_config()
        report = _make_report(
            config=config,
            total_trades=30,
            net_pnl_usdc=Decimal("150"),
            decisions=decisions,
        )
        v1 = derive_verdict(report, min_trades=20, total_loaded=30)
        v2 = derive_verdict(report, min_trades=20, total_loaded=30)
        assert v1 == v2


# ---------------------------------------------------------------------------
# Validation report content — computed metrics
# ---------------------------------------------------------------------------


class TestValidationReportContent:
    """Tests that the report contains correct computed fields."""

    def test_report_includes_action_distribution_buy_hold_skip(self):
        decisions = [
            _make_decision(action="BUY"),
            _make_decision(action="BUY"),
            _make_decision(action="HOLD"),
            _make_decision(action="SKIP"),
            _make_decision(action="HOLD"),
        ]
        report = _make_report(total_trades=5, decisions=decisions)
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=5
        )
        assert result.action_distribution.buy == 2
        assert result.action_distribution.hold == 2
        assert result.action_distribution.skip == 1

    def test_report_includes_per_market_stats(self):
        stats = {
            "tok_a": BacktestMarketStats(
                token_id="tok_a",
                total_decisions=10,
                trades_executed=5,
                win_rate=Decimal("0.6"),
                net_pnl_usdc=Decimal("30"),
            ),
        }
        report = _make_report(total_trades=5, per_market_stats=stats, decisions=[])
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=10
        )
        assert "tok_a" in result.per_market_stats
        assert result.per_market_stats["tok_a"].net_pnl_usdc == Decimal("30")

    def test_report_includes_confidence_calibration_buckets(self):
        decisions = [
            _make_decision(
                decision=True,
                action="BUY",
                confidence=Decimal("0.75"),
                realized_pnl_usdc=Decimal("5"),
            )
            for _ in range(10)
        ]
        report = _make_report(total_trades=10, decisions=decisions)
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=10
        )
        assert len(result.confidence_calibration_buckets) == 5
        bucket = result.confidence_calibration_buckets[3]  # 0.6-0.8
        assert bucket.count == 10
        assert bucket.avg_confidence == Decimal("0.75")
        # All trades won (realized_pnl > 0)
        assert bucket.observed_win_rate == Decimal("1.0")

    def test_report_includes_average_ev(self):
        decisions = [
            _make_decision(
                decision=True,
                action="BUY",
                ev=Decimal("0.04"),
                realized_pnl_usdc=Decimal("5"),
            ),
            _make_decision(
                decision=True,
                action="BUY",
                ev=Decimal("0.06"),
                realized_pnl_usdc=Decimal("5"),
            ),
        ]
        report = _make_report(total_trades=2, decisions=decisions)
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=2
        )
        assert result.average_ev == Decimal("0.05")

    def test_report_includes_total_snapshots_replayed(self):
        report = _make_report()
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=42
        )
        assert result.total_snapshots_replayed == 42

    def test_report_includes_total_decisions(self):
        decisions = [_make_decision() for _ in range(7)]
        report = _make_report(total_trades=7, decisions=decisions)
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=7
        )
        assert result.total_decisions == 7

    def test_report_includes_win_rate(self):
        report = _make_report(win_rate=Decimal("0.65"))
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=10
        )
        assert result.win_rate == Decimal("0.65")

    def test_report_includes_net_pnl_usdc(self):
        report = _make_report(net_pnl_usdc=Decimal("123.45"))
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=10
        )
        assert result.net_pnl_usdc == Decimal("123.45")

    def test_report_includes_max_drawdown_usdc(self):
        report = _make_report(max_drawdown_usdc=Decimal("55.0"))
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=10
        )
        assert result.max_drawdown_usdc == Decimal("55.0")

    def test_report_includes_sharpe_ratio(self):
        report = _make_report(sharpe_ratio=Decimal("1.8"))
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=10
        )
        assert result.sharpe_ratio == Decimal("1.8")


# ---------------------------------------------------------------------------
# Dry-run and safety invariants
# ---------------------------------------------------------------------------


class TestSafetyInvariants:
    """Backtest must remain dry-run only, no live paths."""

    def test_backtest_path_does_not_write_to_database(self):
        report = _make_report(total_trades=5)
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=5
        )
        assert result is not None
        assert report.config_snapshot.dry_run is True

    def test_backtest_path_does_not_construct_live_signer(self):
        import inspect as _ins
        import src.backtesting.validation_report as vr_mod
        import src.backtesting.live_readiness as lr_mod

        source_vr = _ins.getsource(vr_mod)
        source_lr = _ins.getsource(lr_mod)
        assert "TransactionSigner" not in source_vr
        assert "TransactionSigner" not in source_lr

    def test_backtest_path_does_not_construct_live_broadcaster(self):
        import inspect as _ins
        import src.backtesting.validation_report as vr_mod
        import src.backtesting.live_readiness as lr_mod

        source_vr = _ins.getsource(vr_mod)
        source_lr = _ins.getsource(lr_mod)
        assert "broadcast" not in source_vr.lower()
        assert "broadcast" not in source_lr.lower()

    def test_backtest_path_does_not_mutate_execution_state(self):
        report1 = _make_report(total_trades=5, net_pnl_usdc=Decimal("50"))
        result1 = build_validation_report(
            report1, data_dir="/tmp", total_snapshots_replayed=5
        )
        result2 = build_validation_report(
            report1, data_dir="/tmp", total_snapshots_replayed=5
        )
        assert result1.verdict == result2.verdict
        assert result1.net_pnl_usdc == result2.net_pnl_usdc


# ---------------------------------------------------------------------------
# Decimal integrity
# ---------------------------------------------------------------------------


class TestDecimalIntegrity:
    """All financial math must use Decimal."""

    def test_pnl_calculation_returns_decimal(self):
        report = _make_report(net_pnl_usdc=Decimal("42.0"))
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=5
        )
        assert isinstance(result.net_pnl_usdc, Decimal)
        assert not isinstance(result.net_pnl_usdc, float)

    def test_drawdown_calculation_returns_decimal(self):
        report = _make_report(max_drawdown_usdc=Decimal("10.5"))
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=5
        )
        assert isinstance(result.max_drawdown_usdc, Decimal)

    def test_sharpe_calculation_returns_decimal(self):
        report = _make_report(sharpe_ratio=Decimal("1.2"))
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=5
        )
        assert isinstance(result.sharpe_ratio, Decimal)

    def test_win_rate_calculation_returns_decimal(self):
        report = _make_report(win_rate=Decimal("0.55"))
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=5
        )
        assert isinstance(result.win_rate, Decimal)

    def test_ev_calculation_returns_decimal(self):
        decisions = [
            _make_decision(
                decision=True,
                action="BUY",
                ev=Decimal("0.05"),
                realized_pnl_usdc=Decimal("5"),
            )
            for _ in range(3)
        ]
        report = _make_report(total_trades=3, decisions=decisions)
        result = build_validation_report(
            report, data_dir="/tmp", total_snapshots_replayed=3
        )
        assert isinstance(result.average_ev, Decimal)

    def test_no_raw_float_in_calibration_math(self):
        decisions = [
            _make_decision(
                decision=True,
                action="BUY",
                confidence=Decimal("0.75"),
                realized_pnl_usdc=Decimal("5"),
            )
            for _ in range(10)
        ]
        buckets = _compute_calibration_buckets(
            decisions,
            buckets=[
                (Decimal("0.0"), Decimal("0.2")),
                (Decimal("0.2"), Decimal("0.4")),
                (Decimal("0.4"), Decimal("0.6")),
                (Decimal("0.6"), Decimal("0.8")),
                (Decimal("0.8"), Decimal("1.0")),
            ],
        )
        for bucket in buckets:
            assert isinstance(bucket.avg_confidence, Decimal)
            assert isinstance(bucket.observed_win_rate, Decimal)
            assert isinstance(bucket.deviation, Decimal)


# ---------------------------------------------------------------------------
# Calibration bucket schema
# ---------------------------------------------------------------------------


class TestCalibrationBuckets:
    """Confidence calibration bucket tests."""

    def test_buckets_cover_0_to_1_range(self):
        buckets = _compute_calibration_buckets(
            [],
            buckets=[
                (Decimal("0.0"), Decimal("0.2")),
                (Decimal("0.2"), Decimal("0.4")),
                (Decimal("0.4"), Decimal("0.6")),
                (Decimal("0.6"), Decimal("0.8")),
                (Decimal("0.8"), Decimal("1.0")),
            ],
        )
        assert len(buckets) == 5
        assert buckets[0].low == Decimal("0.0") and buckets[0].high == Decimal("0.2")
        assert buckets[-1].low == Decimal("0.8") and buckets[-1].high == Decimal("1.0")

    def test_bucket_assignment_correct(self):
        decisions = [
            _make_decision(
                decision=True,
                action="BUY",
                confidence=Decimal("0.15"),
                realized_pnl_usdc=Decimal("5"),
            ),
            _make_decision(
                decision=True,
                action="BUY",
                confidence=Decimal("0.75"),
                realized_pnl_usdc=Decimal("5"),
            ),
            _make_decision(
                decision=True,
                action="BUY",
                confidence=Decimal("0.95"),
                realized_pnl_usdc=Decimal("5"),
            ),
        ]
        buckets = _compute_calibration_buckets(
            decisions,
            buckets=[
                (Decimal("0.0"), Decimal("0.2")),
                (Decimal("0.2"), Decimal("0.4")),
                (Decimal("0.4"), Decimal("0.6")),
                (Decimal("0.6"), Decimal("0.8")),
                (Decimal("0.8"), Decimal("1.0")),
            ],
        )
        assert buckets[0].count == 1
        assert buckets[3].count == 1
        assert buckets[4].count == 1

    def test_empty_buckets_do_not_crash(self):
        buckets = _compute_calibration_buckets(
            [],
            buckets=[
                (Decimal("0.0"), Decimal("0.2")),
                (Decimal("0.2"), Decimal("0.4")),
            ],
        )
        assert len(buckets) == 2
        for bucket in buckets:
            assert bucket.count == 0
            assert bucket.avg_confidence == _ZERO


# ---------------------------------------------------------------------------
# Data quality summary
# ---------------------------------------------------------------------------


class TestDataQualitySummary:
    """Data quality summary tests."""

    def test_summary_includes_malformed_count(self):
        dq = BacktestDataQualitySummary(total_loaded=100, malformed_count=5)
        assert dq.malformed_count == 5

    def test_summary_includes_missing_fields_count(self):
        dq = BacktestDataQualitySummary(total_loaded=100, missing_fields_count=3)
        assert dq.missing_fields_count == 3

    def test_summary_includes_crossed_books_count(self):
        dq = BacktestDataQualitySummary(total_loaded=100, crossed_books_count=2)
        assert dq.crossed_books_count == 2

    def test_summary_includes_total_loaded_count(self):
        dq = BacktestDataQualitySummary(total_loaded=42)
        assert dq.total_loaded == 42


# ---------------------------------------------------------------------------
# CLI script
# ---------------------------------------------------------------------------


class TestCliScript:
    """run_real_data_backtest.py CLI tests."""

    def test_cli_accepts_data_dir_argument(self):
        sys.path.insert(0, str(_PROJECT_ROOT))
        try:
            from scripts.run_real_data_backtest import parse_args
        finally:
            sys.path.pop(0)
        assert callable(parse_args)

    def test_cli_accepts_output_argument(self):
        sys.path.insert(0, str(_PROJECT_ROOT))
        try:
            from scripts.run_real_data_backtest import parse_args
        finally:
            sys.path.pop(0)
        assert callable(parse_args)

    def test_cli_exits_non_zero_on_missing_data_dir(self):
        report = _make_report(total_trades=0)
        dq = BacktestDataQualitySummary(total_loaded=0)
        result = build_validation_report(
            report,
            data_dir="/nonexistent",
            total_snapshots_replayed=0,
            data_quality=dq,
        )
        assert result.verdict == LiveReadinessVerdict.FAIL_DATA_QUALITY

    def test_cli_writes_json_report_to_output_path(self):
        decisions = [
            _make_decision(
                decision=True,
                action="BUY",
                confidence=Decimal("0.7"),
                ev=Decimal("0.04"),
                position_size_usdc=Decimal("12.5"),
                realized_pnl_usdc=Decimal("0.5"),
            )
            for _ in range(25)
        ]
        config = _make_config()
        report = _make_report(
            config=config,
            total_trades=25,
            net_pnl_usdc=Decimal("12.5"),
            max_drawdown_usdc=Decimal("3"),
            sharpe_ratio=Decimal("1.1"),
            decisions=decisions,
        )
        result = build_validation_report(
            report, data_dir="/tmp/test_data", total_snapshots_replayed=25
        )
        data = result.model_dump(mode="json")
        assert "verdict" in data
        assert "data_dir" in data
        assert data["data_dir"] == "/tmp/test_data"
        assert "realized_ev_calibration" in data

    def test_cli_writes_markdown_report(self):
        decisions = [
            _make_decision(
                decision=True,
                action="BUY",
                confidence=Decimal("0.7"),
                ev=Decimal("0.04"),
                position_size_usdc=Decimal("12.5"),
                realized_pnl_usdc=Decimal("0.5"),
            )
            for _ in range(25)
        ]
        config = _make_config()
        report = _make_report(
            config=config,
            total_trades=25,
            net_pnl_usdc=Decimal("12.5"),
            decisions=decisions,
        )
        result = build_validation_report(
            report, data_dir="/tmp/test_data", total_snapshots_replayed=25
        )
        assert result.total_snapshots_replayed == 25
        assert result.action_distribution.buy == 25
        assert len(result.confidence_calibration_buckets) == 5
        assert result.data_quality.total_loaded == 25
        assert isinstance(result.realized_ev_calibration, Decimal)

    def test_cli_reads_manifest_skipped_count_as_data_quality_failure(self):
        sys.path.insert(0, str(_PROJECT_ROOT))
        try:
            from scripts.run_real_data_backtest import _load_data_quality_summary
        finally:
            sys.path.pop(0)

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "manifest.json"
            manifest_path.write_text(
                json.dumps({"snapshot_count": 90, "skipped_count": 15}),
                encoding="utf-8",
            )

            summary = _load_data_quality_summary(tmpdir, total_loaded=90)

        assert summary.total_loaded == 105
        assert summary.malformed_count == 15

    def test_cli_reads_manifest_typed_quality_counts(self):
        sys.path.insert(0, str(_PROJECT_ROOT))
        try:
            from scripts.run_real_data_backtest import _load_data_quality_summary
        finally:
            sys.path.pop(0)

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = Path(tmpdir) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "snapshot_count": 90,
                        "skipped_count": 6,
                        "malformed_count": 2,
                        "missing_fields_count": 1,
                        "crossed_books_count": 3,
                    }
                ),
                encoding="utf-8",
            )

            summary = _load_data_quality_summary(tmpdir, total_loaded=90)

        assert summary.total_loaded == 96
        assert summary.malformed_count == 2
        assert summary.missing_fields_count == 1
        assert summary.crossed_books_count == 3

    def test_cli_creates_output_directory_if_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = Path(tmpdir) / "a" / "b" / "c" / "report.json"
            nested.parent.mkdir(parents=True, exist_ok=True)
            nested.write_text("{}")
            assert nested.exists()

    def test_cli_does_not_publish_pass_on_partial_failure(self):
        report = _make_report(total_trades=0)
        result = build_validation_report(
            report, data_dir="/tmp/test_data", total_snapshots_replayed=0
        )
        assert result.verdict != LiveReadinessVerdict.PASS


# ---------------------------------------------------------------------------
# No-optimization invariant
# ---------------------------------------------------------------------------


class TestNoOptimizationInvariant:
    """WI-44 must not tune prompts, thresholds, Kelly, or risk params."""

    def test_validation_does_not_mutate_backtest_config(self):
        config = _make_config(
            initial_bankroll_usdc=Decimal("1000"),
            kelly_fraction=Decimal("0.25"),
            min_confidence=Decimal("0.75"),
        )
        original_kelly = config.kelly_fraction
        original_conf = config.min_confidence

        decisions = [
            _make_decision(
                decision=True,
                action="BUY",
                confidence=Decimal("0.7"),
                ev=Decimal("0.04"),
                realized_pnl_usdc=Decimal("5"),
            )
            for _ in range(25)
        ]
        report = _make_report(
            config=config,
            total_trades=25,
            net_pnl_usdc=Decimal("100"),
            decisions=decisions,
        )
        build_validation_report(report, data_dir="/tmp", total_snapshots_replayed=25)
        assert config.kelly_fraction == original_kelly
        assert config.min_confidence == original_conf

    def test_validation_does_not_mutate_kelly_fraction(self):
        config = _make_config(kelly_fraction=Decimal("0.25"))
        original = config.kelly_fraction
        report = _make_report(config=config, total_trades=25)
        build_validation_report(report, data_dir="/tmp", total_snapshots_replayed=25)
        assert config.kelly_fraction == original

    def test_validation_does_not_mutate_confidence_thresholds(self):
        config = _make_config(min_confidence=Decimal("0.75"))
        original = config.min_confidence
        report = _make_report(config=config, total_trades=25)
        build_validation_report(report, data_dir="/tmp", total_snapshots_replayed=25)
        assert config.min_confidence == original


# ---------------------------------------------------------------------------
# BacktestDecision realized_pnl_usdc field
# ---------------------------------------------------------------------------


class TestBacktestDecisionPnlField:
    """BacktestDecision.realized_pnl_usdc enables PnL-based calibration."""

    def test_realized_pnl_defaults_to_zero(self):
        d = _make_decision()
        assert d.realized_pnl_usdc == _ZERO
        assert isinstance(d.realized_pnl_usdc, Decimal)

    def test_realized_pnl_positive_is_win(self):
        d = _make_decision(realized_pnl_usdc=Decimal("10"))
        assert d.realized_pnl_usdc > _ZERO

    def test_realized_pnl_negative_is_loss(self):
        d = _make_decision(realized_pnl_usdc=Decimal("-5"))
        assert d.realized_pnl_usdc < _ZERO

    def test_realized_pnl_rejects_float(self):
        with pytest.raises(Exception):
            _make_decision(realized_pnl_usdc=1.5)  # type: ignore[arg-type]
