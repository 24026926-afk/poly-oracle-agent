"""
src/backtesting/validation_report.py

WI-44 typed validation report schemas and builder — consumes BacktestReport,
emits BacktestValidationReport with live-readiness verdict.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, field_validator

from src.backtesting.live_readiness import LiveReadinessVerdict, derive_verdict
from src.schemas.execution import BacktestDecision, BacktestMarketStats, BacktestReport

_ZERO = Decimal("0")
_CALIBRATION_BUCKETS: list[tuple[Decimal, Decimal]] = [
    (Decimal("0.0"), Decimal("0.2")),
    (Decimal("0.2"), Decimal("0.4")),
    (Decimal("0.4"), Decimal("0.6")),
    (Decimal("0.6"), Decimal("0.8")),
    (Decimal("0.8"), Decimal("1.0")),
]


def _reject_float(value: Any) -> Any:
    if value is None:
        return value
    if isinstance(value, float):
        raise ValueError("Float financial values are forbidden; use Decimal")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _format_bucket_label(low: Decimal, high: Decimal) -> str:
    return f"{low.quantize(Decimal('0.1'))}-{high.quantize(Decimal('0.1'))}"


# ---------------------------------------------------------------------------
# Sub-schemas
# ---------------------------------------------------------------------------


class BacktestActionDistribution(BaseModel):
    """BUY / HOLD / SKIP counts."""

    buy: int = 0
    hold: int = 0
    skip: int = 0

    model_config = {"frozen": True}


class BacktestCalibrationBucket(BaseModel):
    """Confidence calibration for one bucket."""

    bucket_label: str
    low: Decimal
    high: Decimal
    count: int
    avg_confidence: Decimal
    observed_win_rate: Decimal
    deviation: Decimal

    @field_validator(
        "low", "high", "avg_confidence", "observed_win_rate", "deviation",
        mode="before",
    )
    @classmethod
    def _validate_financials(cls, value: Any) -> Any:
        return _reject_float(value)

    model_config = {"frozen": True}


class BacktestDataQualitySummary(BaseModel):
    """Data quality summary for the validation report."""

    total_loaded: int
    malformed_count: int = 0
    missing_fields_count: int = 0
    crossed_books_count: int = 0

    model_config = {"frozen": True}


class BacktestValidationReport(BaseModel):
    """Top-level WI-44 validation report (canonical name per business logic).

    Also aliased as BacktestValidationSummary for backward compatibility.
    """

    # Run metadata
    generated_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    data_dir: str
    verdict: LiveReadinessVerdict
    verdict_reason: str = ""

    # Data quality
    data_quality: BacktestDataQualitySummary

    # Core metrics
    total_snapshots_replayed: int
    total_decisions: int
    action_distribution: BacktestActionDistribution
    total_trades: int
    win_rate: Decimal
    net_pnl_usdc: Decimal
    max_drawdown_usdc: Decimal
    sharpe_ratio: Decimal
    average_ev: Decimal

    # Realized EV calibration — compares average EV against realized PnL per trade
    realized_ev_calibration: Decimal

    # Calibration
    confidence_calibration_buckets: list[BacktestCalibrationBucket]

    # Per-market stats (mirrors BacktestReport.per_market_stats)
    per_market_stats: dict[str, BacktestMarketStats]

    @field_validator(
        "win_rate",
        "net_pnl_usdc",
        "max_drawdown_usdc",
        "sharpe_ratio",
        "average_ev",
        "realized_ev_calibration",
        mode="before",
    )
    @classmethod
    def _validate_financials(cls, value: Any) -> Any:
        return _reject_float(value)

    model_config = {"frozen": True}


# Backward-compatible alias
BacktestValidationSummary = BacktestValidationReport


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_validation_report(
    report: BacktestReport,
    *,
    data_dir: str,
    total_snapshots_replayed: int,
    data_quality: BacktestDataQualitySummary | None = None,
) -> BacktestValidationReport:
    """Build a WI-44 validation report from a BacktestReport.

    Computes action distribution, calibration buckets, average EV,
    realized EV calibration, and derives the live-readiness verdict.
    """
    # Action distribution
    action_dist = _compute_action_distribution(report.decisions)

    # Average EV
    trade_decisions = [d for d in report.decisions if d.decision and d.action == "BUY"]
    if trade_decisions:
        average_ev = sum((d.ev for d in trade_decisions), _ZERO) / len(trade_decisions)
    else:
        average_ev = _ZERO

    # Realized EV calibration: deviation between avg EV and realized return
    # (both in unit-return / probability space)
    if report.total_trades > 0 and trade_decisions:
        avg_position = (
            sum((d.position_size_usdc for d in trade_decisions), _ZERO)
            / len(trade_decisions)
        )
        if avg_position > _ZERO:
            realized_per_trade = report.net_pnl_usdc / report.total_trades
            realized_return = realized_per_trade / avg_position
            realized_ev_calibration = abs(average_ev - realized_return)
        else:
            realized_ev_calibration = _ZERO
    else:
        realized_ev_calibration = _ZERO

    # Calibration buckets — uses realized_pnl_usdc > 0 for win determination
    calibration_buckets = _compute_calibration_buckets(
        report.decisions, buckets=_CALIBRATION_BUCKETS
    )

    # Data quality
    if data_quality is None:
        data_quality = BacktestDataQualitySummary(
            total_loaded=total_snapshots_replayed,
        )

    # Verdict — passes data-quality counts so malformed/crossed data can force
    # FAIL_DATA_QUALITY
    verdict = derive_verdict(
        report,
        total_loaded=data_quality.total_loaded,
        malformed_count=data_quality.malformed_count,
        missing_fields_count=data_quality.missing_fields_count,
        crossed_books_count=data_quality.crossed_books_count,
    )
    verdict_reason = _describe_verdict(verdict, report, data_quality)

    return BacktestValidationReport(
        data_dir=data_dir,
        verdict=verdict,
        verdict_reason=verdict_reason,
        data_quality=data_quality,
        total_snapshots_replayed=total_snapshots_replayed,
        total_decisions=len(report.decisions),
        action_distribution=action_dist,
        total_trades=report.total_trades,
        win_rate=report.win_rate,
        net_pnl_usdc=report.net_pnl_usdc,
        max_drawdown_usdc=report.max_drawdown_usdc,
        sharpe_ratio=report.sharpe_ratio,
        average_ev=average_ev,
        realized_ev_calibration=realized_ev_calibration,
        confidence_calibration_buckets=calibration_buckets,
        per_market_stats=report.per_market_stats,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_action_distribution(
    decisions: list[BacktestDecision],
) -> BacktestActionDistribution:
    buy = sum(1 for d in decisions if d.action == "BUY")
    hold = sum(1 for d in decisions if d.action == "HOLD")
    skip = sum(1 for d in decisions if d.action == "SKIP")
    return BacktestActionDistribution(buy=buy, hold=hold, skip=skip)


def _compute_calibration_buckets(
    decisions: list[BacktestDecision],
    *,
    buckets: list[tuple[Decimal, Decimal]],
) -> list[BacktestCalibrationBucket]:
    """Compute confidence calibration buckets.

    Uses realized_pnl_usdc > 0 as win indicator (not gatekeeper_result).
    """
    buy_decisions = [d for d in decisions if d.decision and d.action == "BUY"]
    result: list[BacktestCalibrationBucket] = []

    for low, high in buckets:
        bucket_d = [d for d in buy_decisions if low <= d.confidence <= high]
        count = len(bucket_d)

        if count == 0:
            result.append(
                BacktestCalibrationBucket(
                    bucket_label=_format_bucket_label(low, high),
                    low=low,
                    high=high,
                    count=0,
                    avg_confidence=_ZERO,
                    observed_win_rate=_ZERO,
                    deviation=_ZERO,
                )
            )
            continue

        avg_confidence = sum((d.confidence for d in bucket_d), _ZERO) / count
        wins = sum(1 for d in bucket_d if d.realized_pnl_usdc > _ZERO)
        observed_win_rate = Decimal(wins) / Decimal(count)
        deviation = abs(avg_confidence - observed_win_rate)

        result.append(
            BacktestCalibrationBucket(
                bucket_label=_format_bucket_label(low, high),
                low=low,
                high=high,
                count=count,
                avg_confidence=avg_confidence,
                observed_win_rate=observed_win_rate,
                deviation=deviation,
            )
        )

    return result


def _describe_verdict(
    verdict: LiveReadinessVerdict,
    report: BacktestReport,
    data_quality: BacktestDataQualitySummary,
) -> str:
    if verdict == LiveReadinessVerdict.PASS:
        return (
            f"All checks passed: {report.total_trades} trades, "
            f"net PnL={report.net_pnl_usdc} USDC, "
            f"drawdown={report.max_drawdown_usdc} USDC, "
            f"Sharpe={report.sharpe_ratio}"
        )
    if verdict == LiveReadinessVerdict.FAIL_DATA_QUALITY:
        return (
            f"Data quality insufficient: loaded={data_quality.total_loaded}, "
            f"malformed={data_quality.malformed_count}, "
            f"missing={data_quality.missing_fields_count}, "
            f"crossed={data_quality.crossed_books_count}, "
            f"trades={report.total_trades}"
        )
    if verdict == LiveReadinessVerdict.FAIL_INSUFFICIENT_TRADES:
        return f"Insufficient trade count: {report.total_trades}"
    if verdict == LiveReadinessVerdict.FAIL_NEGATIVE_PNL:
        return f"Negative net PnL: {report.net_pnl_usdc} USDC"
    if verdict == LiveReadinessVerdict.FAIL_DRAWDOWN:
        bankroll = report.config_snapshot.initial_bankroll_usdc
        pct = report.max_drawdown_usdc / bankroll * 100
        return (
            f"Excessive drawdown: {report.max_drawdown_usdc} USDC "
            f"({pct:.1f}% of {bankroll} USDC)"
        )
    if verdict == LiveReadinessVerdict.FAIL_WEAK_CALIBRATION:
        return "Confidence or EV calibration deviates from observed outcomes"
    return f"Unknown verdict: {verdict.value}"
