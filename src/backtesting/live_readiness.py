"""
src/backtesting/live_readiness.py

WI-44 typed live-readiness verdict and derivation logic.

Conservative: if any criterion fails, the verdict is non-live-ready.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from src.schemas.execution import BacktestDecision, BacktestReport

_ZERO = Decimal("0")


class LiveReadinessVerdict(str, Enum):
    """Conservative live-readiness verdict.

    Only PASS permits further consideration of DRY_RUN=False.
    Any other verdict is a phase-level kill criterion.
    """

    PASS = "PASS"
    FAIL_NEGATIVE_PNL = "FAIL_NEGATIVE_PNL"
    FAIL_DRAWDOWN = "FAIL_DRAWDOWN"
    FAIL_INSUFFICIENT_TRADES = "FAIL_INSUFFICIENT_TRADES"
    FAIL_WEAK_CALIBRATION = "FAIL_WEAK_CALIBRATION"
    FAIL_DATA_QUALITY = "FAIL_DATA_QUALITY"


# Conservative defaults — no optimization allowed in WI-44
_MIN_TRADES: int = 20
_MAX_DRAWDOWN_PCT: Decimal = Decimal("0.30")
_CALIBRATION_BUCKETS: list[tuple[Decimal, Decimal]] = [
    (Decimal("0.0"), Decimal("0.2")),
    (Decimal("0.2"), Decimal("0.4")),
    (Decimal("0.4"), Decimal("0.6")),
    (Decimal("0.6"), Decimal("0.8")),
    (Decimal("0.8"), Decimal("1.0")),
]
_CALIBRATION_MAX_DEVIATION: Decimal = Decimal("0.25")
_CALIBRATION_MIN_POPULATED_BUCKETS: int = 2
_EV_MAX_DEVIATION: Decimal = Decimal("0.15")
# Minimum malformed/crossed/missing fraction to trigger data-quality failure
_DATA_QUALITY_MIN_BAD_FRACTION: Decimal = Decimal("0.10")


def derive_verdict(
    report: BacktestReport,
    *,
    min_trades: int = _MIN_TRADES,
    max_drawdown_pct: Decimal = _MAX_DRAWDOWN_PCT,
    calibration_buckets: list[tuple[Decimal, Decimal]] | None = None,
    calibration_max_deviation: Decimal = _CALIBRATION_MAX_DEVIATION,
    calibration_min_populated: int = _CALIBRATION_MIN_POPULATED_BUCKETS,
    ev_max_deviation: Decimal = _EV_MAX_DEVIATION,
    total_loaded: int = 0,
    malformed_count: int = 0,
    missing_fields_count: int = 0,
    crossed_books_count: int = 0,
    data_quality_min_bad_fraction: Decimal = _DATA_QUALITY_MIN_BAD_FRACTION,
) -> LiveReadinessVerdict:
    """Derive a conservative live-readiness verdict from a BacktestReport.

    Checks applied in priority order; the first failure wins.
    """
    if calibration_buckets is None:
        calibration_buckets = _CALIBRATION_BUCKETS

    # -- data quality: excessive malformed/crossed/missing data
    if total_loaded > 0:
        bad_total = malformed_count + missing_fields_count + crossed_books_count
        if Decimal(bad_total) / Decimal(total_loaded) > data_quality_min_bad_fraction:
            return LiveReadinessVerdict.FAIL_DATA_QUALITY

    # -- data quality: no snapshots loaded at all
    if total_loaded <= 0 and report.total_trades <= 0:
        return LiveReadinessVerdict.FAIL_DATA_QUALITY

    # -- insufficient trade count (includes zero-trades-with-decisions, per WI-44
    #    business logic edge case 3: "Dataset produces decisions but zero trades:
    #    return FAIL_INSUFFICIENT_TRADES")
    if report.total_trades < min_trades:
        return LiveReadinessVerdict.FAIL_INSUFFICIENT_TRADES

    # -- negative net PnL
    if report.net_pnl_usdc <= _ZERO:
        return LiveReadinessVerdict.FAIL_NEGATIVE_PNL

    # -- excessive drawdown relative to bankroll
    bankroll = report.config_snapshot.initial_bankroll_usdc
    if bankroll > _ZERO and report.max_drawdown_usdc > max_drawdown_pct * bankroll:
        return LiveReadinessVerdict.FAIL_DRAWDOWN

    # -- confidence calibration check
    if not _confidence_calibration_acceptable(
        report.decisions,
        buckets=calibration_buckets,
        max_deviation=calibration_max_deviation,
        min_populated=calibration_min_populated,
    ):
        return LiveReadinessVerdict.FAIL_WEAK_CALIBRATION

    # -- realized EV calibration: compare average EV against realized PnL per trade
    if not _ev_calibration_acceptable(report, max_deviation=ev_max_deviation):
        return LiveReadinessVerdict.FAIL_WEAK_CALIBRATION

    return LiveReadinessVerdict.PASS


def _confidence_calibration_acceptable(
    decisions: list[BacktestDecision],
    *,
    buckets: list[tuple[Decimal, Decimal]],
    max_deviation: Decimal,
    min_populated: int,
) -> bool:
    """Check whether confidence calibration is within acceptable bounds.

    For each confidence bucket, compare the average confidence to the
    observed win rate among BUY decisions, using realized_pnl_usdc > 0
    as the win indicator (not gatekeeper_result).
    """
    if len(decisions) == 0:
        return True

    buy_decisions = [d for d in decisions if d.decision and d.action == "BUY"]
    if not buy_decisions:
        return True

    populated = 0

    for low, high in buckets:
        bucket_d = [d for d in buy_decisions if low <= d.confidence <= high]
        if not bucket_d:
            continue

        populated += 1
        avg_confidence = sum(
            (d.confidence for d in bucket_d), _ZERO
        ) / len(bucket_d)
        wins = sum(1 for d in bucket_d if d.realized_pnl_usdc > _ZERO)
        observed_win_rate = Decimal(wins) / Decimal(len(bucket_d))

        deviation = abs(avg_confidence - observed_win_rate)
        if deviation > max_deviation:
            return False

    return populated >= min_populated


def _ev_calibration_acceptable(
    report: BacktestReport,
    *,
    max_deviation: Decimal,
) -> bool:
    """Check whether average EV aligns with realized return.

    Normalizes both sides to unit-return space:
      - avg_ev is already a probability-space edge
      - realized return = (net_pnl_per_trade / avg_position_size_usdc)

    If the deviation between the two exceeds *max_deviation*, EV calibration
    is weak (per WI-44 business logic edge case 7).
    """
    if report.total_trades <= 0:
        return True

    buy_decisions = [d for d in report.decisions if d.decision and d.action == "BUY"]
    if not buy_decisions:
        return True

    avg_ev = sum((d.ev for d in buy_decisions), _ZERO) / len(buy_decisions)
    avg_position = (
        sum((d.position_size_usdc for d in buy_decisions), _ZERO)
        / len(buy_decisions)
    )
    if avg_position <= _ZERO:
        return True

    realized_per_trade = report.net_pnl_usdc / report.total_trades
    realized_return = realized_per_trade / avg_position

    deviation = abs(avg_ev - realized_return)
    return deviation <= max_deviation


# ---------------------------------------------------------------------------
# Verdict deterministic test helper — deterministic for identical input
# ---------------------------------------------------------------------------


def verdict_from_string(value: str) -> LiveReadinessVerdict:
    """Case-insensitive lookup of a LiveReadinessVerdict from a string."""
    upper = value.upper()
    for member in LiveReadinessVerdict:
        if member.value == upper:
            return member
    raise ValueError(
        f"Unknown verdict '{value}'. "
        f"Valid: {[v.value for v in LiveReadinessVerdict]}"
    )
