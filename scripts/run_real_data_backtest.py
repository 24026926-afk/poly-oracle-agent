#!/usr/bin/env python3
"""
scripts/run_real_data_backtest.py

WI-44 CLI entrypoint — runs BacktestRunner against WI-43 historical dataset
and emits a typed validation report with live-readiness verdict.

Usage:
    python scripts/run_real_data_backtest.py \\
        --data-dir data/historical \\
        --output docs/backtests/phase13_baseline.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import structlog

from src.backtest_runner import BacktestDataLoader, BacktestRunner
from src.backtesting.live_readiness import LiveReadinessVerdict
from src.backtesting.validation_report import (
    BacktestDataQualitySummary,
    BacktestValidationReport,
    build_validation_report,
)
from src.schemas.execution import BacktestConfig, BacktestReport

logger = structlog.get_logger(__name__)

# Enforce output under docs/backtests/
_ALLOWED_OUTPUT_PARENT = Path("docs") / "backtests"
_ZERO = Decimal("0")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="WI-44 real-data backtest validation runner"
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Directory of WI-43 historical JSON snapshot files",
    )
    parser.add_argument(
        "--output",
        required=True,
        help=(
            "Output JSON report path. Must be under docs/backtests/ "
            "(e.g. docs/backtests/phase13_baseline.json)"
        ),
    )
    parser.add_argument(
        "--initial-bankroll-usdc",
        type=str,
        default="1000",
        help="Initial bankroll in USDC for the backtest run (default: 1000)",
    )
    return parser.parse_args()


def _write_markdown_report(
    md_path: Path,
    report: BacktestValidationReport,
) -> None:
    """Write a human-readable markdown summary alongside the JSON report."""
    lines: list[str] = []

    lines.append("# Phase 13 — Real-Data Backtest Validation Report\n")
    lines.append(f"**Generated:** {report.generated_at_utc.isoformat()}\n")
    lines.append(f"**Data directory:** `{report.data_dir}`\n")
    lines.append(f"**Verdict:** `{report.verdict.value}`\n")
    lines.append(f"**Reason:** {report.verdict_reason}\n")
    lines.append("")

    lines.append("## Data Quality\n")
    dq = report.data_quality
    lines.append(f"- Total loaded: {dq.total_loaded}")
    lines.append(f"- Malformed: {dq.malformed_count}")
    lines.append(f"- Missing fields: {dq.missing_fields_count}")
    lines.append(f"- Crossed books: {dq.crossed_books_count}")
    lines.append("")

    lines.append("## Core Metrics\n")
    lines.append(f"- Snapshots replayed: {report.total_snapshots_replayed}")
    lines.append(f"- Total decisions: {report.total_decisions}")
    lines.append(f"- Total trades: {report.total_trades}")
    lines.append(f"- Win rate: {report.win_rate}")
    lines.append(f"- Net PnL: {report.net_pnl_usdc} USDC")
    lines.append(f"- Max drawdown: {report.max_drawdown_usdc} USDC")
    lines.append(f"- Sharpe ratio: {report.sharpe_ratio}")
    lines.append(f"- Average EV: {report.average_ev}")
    lines.append(f"- Realized EV calibration: {report.realized_ev_calibration}")
    lines.append("")

    lines.append("## Action Distribution\n")
    ad = report.action_distribution
    lines.append(f"- BUY: {ad.buy}")
    lines.append(f"- HOLD: {ad.hold}")
    lines.append(f"- SKIP: {ad.skip}")
    lines.append("")

    lines.append("## Confidence Calibration\n")
    if report.confidence_calibration_buckets:
        lines.append(
            "| Bucket | Count | Avg Confidence | Observed Win Rate | Deviation |"
        )
        lines.append(
            "|--------|-------|----------------|-------------------|-----------|"
        )
        for bucket in report.confidence_calibration_buckets:
            lines.append(
                f"| {bucket.bucket_label} "
                f"| {bucket.count} "
                f"| {bucket.avg_confidence} "
                f"| {bucket.observed_win_rate} "
                f"| {bucket.deviation} |"
            )
    else:
        lines.append("_No calibration data available._")
    lines.append("")

    if report.per_market_stats:
        lines.append("## Per-Market Stats\n")
        lines.append("| Token ID | Decisions | Trades | Win Rate | Net PnL (USDC) |")
        lines.append("|----------|-----------|--------|----------|----------------|")
        for token_id, stats in sorted(report.per_market_stats.items()):
            lines.append(
                f"| {token_id} "
                f"| {stats.total_decisions} "
                f"| {stats.trades_executed} "
                f"| {stats.win_rate} "
                f"| {stats.net_pnl_usdc} |"
            )

    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _serialize_report(report: BacktestValidationReport) -> dict:
    """Serialize validation report to JSON-safe dict."""
    data = report.model_dump(mode="json")
    data["verdict"] = report.verdict.value
    return data


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _load_data_quality_summary(
    data_dir: str,
    *,
    total_loaded: int,
) -> BacktestDataQualitySummary:
    """Load WI-43 manifest quality counts when present.

    WI-43 currently guarantees ``skipped_count`` in ``manifest.json``. If a
    future manifest includes typed buckets, preserve them; otherwise account
    for skipped records as malformed for a conservative FAIL_DATA_QUALITY path.
    """
    manifest_path = Path(data_dir) / "manifest.json"
    if not manifest_path.exists():
        return BacktestDataQualitySummary(total_loaded=total_loaded)

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("manifest.read_failed", path=str(manifest_path), error=str(exc))
        return BacktestDataQualitySummary(
            total_loaded=max(total_loaded, 1),
            malformed_count=1,
        )

    snapshot_count = _safe_int(manifest.get("snapshot_count"), total_loaded)
    skipped_count = _safe_int(manifest.get("skipped_count"), 0)
    malformed_count = _safe_int(manifest.get("malformed_count"), 0)
    missing_fields_count = _safe_int(manifest.get("missing_fields_count"), 0)
    crossed_books_count = _safe_int(manifest.get("crossed_books_count"), 0)

    accounted_skips = malformed_count + missing_fields_count + crossed_books_count
    if skipped_count > accounted_skips:
        malformed_count += skipped_count - accounted_skips

    quality_total = max(total_loaded, snapshot_count) + skipped_count
    return BacktestDataQualitySummary(
        total_loaded=quality_total,
        malformed_count=malformed_count,
        missing_fields_count=missing_fields_count,
        crossed_books_count=crossed_books_count,
    )


def _empty_backtest_report(config: BacktestConfig) -> BacktestReport:
    now = datetime.now(timezone.utc)
    return BacktestReport(
        total_trades=0,
        win_rate=_ZERO,
        net_pnl_usdc=_ZERO,
        max_drawdown_usdc=_ZERO,
        sharpe_ratio=_ZERO,
        per_market_stats={},
        decisions=[],
        started_at_utc=now,
        completed_at_utc=now,
        config_snapshot=config,
    )


def _write_report_artifacts(
    output_path: Path,
    report: BacktestValidationReport,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(_serialize_report(report), indent=2),
        encoding="utf-8",
    )
    _write_markdown_report(output_path.with_suffix(".md"), report)


def _write_data_quality_failure_report(
    *,
    output_path: Path,
    config: BacktestConfig,
    data_dir: str,
    total_loaded: int,
    reason: str,
) -> BacktestValidationReport:
    data_quality = _load_data_quality_summary(
        data_dir,
        total_loaded=total_loaded,
    )
    if (
        data_quality.malformed_count
        + data_quality.missing_fields_count
        + data_quality.crossed_books_count
    ) == 0:
        data_quality = BacktestDataQualitySummary(
            total_loaded=max(data_quality.total_loaded, 1),
            malformed_count=1,
        )

    validation_report = build_validation_report(
        _empty_backtest_report(config),
        data_dir=data_dir,
        total_snapshots_replayed=total_loaded,
        data_quality=data_quality,
    ).model_copy(
        update={
            "verdict": LiveReadinessVerdict.FAIL_DATA_QUALITY,
            "verdict_reason": f"Data quality failure: {reason}",
        }
    )
    _write_report_artifacts(output_path, validation_report)
    return validation_report


async def main() -> int:
    args = parse_args()

    data_dir = args.data_dir
    output_path = Path(args.output)
    initial_bankroll = Decimal(args.initial_bankroll_usdc)

    # Enforce output under docs/backtests/
    try:
        output_path.resolve().relative_to(
            Path.cwd() / _ALLOWED_OUTPUT_PARENT
        )
    except ValueError:
        logger.error(
            "output.path_rejected",
            path=str(output_path),
            required_parent=str(_ALLOWED_OUTPUT_PARENT),
        )
        print(
            f"ERROR: --output must be under {_ALLOWED_OUTPUT_PARENT}/",
            file=sys.stderr,
        )
        return 1

    # Build config — dry_run is forced True
    config = BacktestConfig(
        data_dir=data_dir,
        initial_bankroll_usdc=initial_bankroll,
        dry_run=True,
    )

    # Load data — capture quality metrics
    loader = BacktestDataLoader(config)
    try:
        snapshots = loader.load_all()
    except Exception as exc:
        logger.error("data_load.failed", error=str(exc))
        _write_data_quality_failure_report(
            output_path=output_path,
            config=config,
            data_dir=data_dir,
            total_loaded=0,
            reason=str(exc),
        )
        return 1

    total_loaded = len(snapshots)
    if total_loaded == 0:
        logger.error("data_load.empty", data_dir=data_dir)
    logger.info("data_loaded", count=total_loaded, data_dir=data_dir)

    data_quality = _load_data_quality_summary(data_dir, total_loaded=total_loaded)

    # Run backtest
    runner = BacktestRunner(config=config, data_loader=loader)
    try:
        backtest_report = await runner.run()
    except Exception as exc:
        logger.error("backtest.failed", error=str(exc))
        _write_data_quality_failure_report(
            output_path=output_path,
            config=config,
            data_dir=data_dir,
            total_loaded=total_loaded,
            reason=str(exc),
        )
        return 1

    # Build validation report
    validation_report = build_validation_report(
        backtest_report,
        data_dir=data_dir,
        total_snapshots_replayed=total_loaded,
        data_quality=data_quality,
    )

    _write_report_artifacts(output_path, validation_report)
    logger.info(
        "report.written",
        path=str(output_path),
        verdict=validation_report.verdict.value,
    )
    md_path = output_path.with_suffix(".md")
    logger.info("markdown.written", path=str(md_path))

    # Summary to stdout
    print(
        f"Backtest complete: {backtest_report.total_trades} trades, "
        f"net PnL={backtest_report.net_pnl_usdc} USDC, "
        f"verdict={validation_report.verdict.value}"
    )

    # Exit code reflects verdict
    if validation_report.verdict != LiveReadinessVerdict.PASS:
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
