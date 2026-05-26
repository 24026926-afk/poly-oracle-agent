#!/usr/bin/env python3
"""
scripts/ops/aggregate_audits.py

WI-62 — Iterates over WI-61 runtime audit artifacts to produce a
deterministic, Decimal-safe JSON summary for the headless
server-runtime-review skill.

Exit codes:
    0 — artifacts found and aggregated successfully.
    1 — zero artifacts in the requested window (no data to report).
    2 — configuration or argument error.

Output is a single JSON document on stdout.  All arithmetic uses
Decimal — no float coercion.  Output is scrubbed of secrets and
high-cardinality identifiers before emission.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

# Allow running from project root without install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.schemas.runtime_audit import (
    RuntimeAuditFindingType,
    RuntimeAuditReport,
    RuntimeAuditSeverity,
    RuntimeAuditStatus,
)

# ── Forbidden-content scrubbing (mirrors runtime_audit.py patterns) ──────

_FORBIDDEN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key_hex", re.compile(r"\b[0-9a-fA-F]{64}\b")),
    ("private_key_0x", re.compile(r"0x[0-9a-fA-F]{64}\b")),
    ("telegram_token", re.compile(r"\b\d{8,10}:[a-zA-Z0-9_-]{35,}\b")),
    ("wallet_address", re.compile(r"0x[a-fA-F0-9]{40}\b")),
    ("api_key_sk", re.compile(r"sk-[a-zA-Z0-9_-]{20,}\b")),
    ("api_key_pk", re.compile(r"pk-[a-zA-Z0-9_-]{20,}\b")),
    ("condition_id_long", re.compile(r"0x[0-9a-fA-F]{60,}\b")),
    ("token_id_digits", re.compile(r"\b\d{15,}\b")),
]

# ── Fix Plan thresholds (explicit, not subjective) ────────────────────────

_FIX_PLAN_CRITICAL_SAFETY_GATES: int = 0  # any safety gate failure triggers
_FIX_PLAN_TOTAL_ERRORS: int = 50  # cumulative errors threshold
_FIX_PLAN_BUDGET_BLOCKS: int = 10  # cumulative budget blocks threshold


def _scrub_text(text: str) -> str:
    """Replace forbidden patterns with [REDACTED]."""
    result = text
    for _label, pattern in _FORBIDDEN_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


def _decimal_to_str(value: Decimal, places: int = 2) -> str:
    """Convert Decimal to string with fixed decimal places."""
    return str(round(value, places))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Aggregate WI-61 runtime audit artifacts for server-runtime-review."
    )
    parser.add_argument(
        "--hours", type=int, default=72, help="Lookback window in hours (default: 72)"
    )
    parser.add_argument(
        "--project-root",
        type=str,
        default=".",
        help="Root of the repository (default: current directory)",
    )
    parser.add_argument(
        "--artifact-dir",
        type=str,
        default=None,
        help=(
            "Directory to scan for runtime-audit-*.json artifacts. "
            "Overrides --project-root when set."
        ),
    )
    args = parser.parse_args()

    if args.artifact_dir is not None:
        audits_dir = Path(args.artifact_dir).resolve()
    else:
        root_path = Path(args.project_root).resolve()
        audits_dir = root_path / "docs" / "operations" / "runtime_audits"

    if not audits_dir.exists():
        error_output = {
            "error": "directory_not_found",
            "detail": f"Audit artifacts directory not found: {audits_dir}",
            "scanned_files": 0,
        }
        print(json.dumps(error_output, indent=2))
        return 1

    cutoff_time = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    # ── Accumulators (Decimal-safe, iterative) ──────────────────────────

    scanned_files = 0
    total_errors = 0
    total_warnings = 0
    budget_blocks = 0
    provider_failures = 0
    critical_safety_gates = 0
    ws_reconnects = 0
    cooldown_blocks = 0
    market_quarantines = 0

    total_response_time = Decimal("0")
    health_samples = 0
    max_exposure_usdc = Decimal("0")
    position_observed = False

    # Decision distribution accumulators
    total_decisions = 0
    buy_count = 0
    sell_count = 0
    hold_count = 0
    skip_count = 0

    # DB growth delta: track first and last file_size_bytes
    first_db_size: int | None = None
    last_db_size: int | None = None
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None

    dry_run_posture: bool | None = None
    dry_run_changed = False

    # ── Iterative processing (no load-all) ───────────────────────────────

    skipped_artifacts = 0
    for filepath in sorted(audits_dir.glob("runtime-audit-*.json")):
        if filepath.name == "latest.json":
            continue

        try:
            content = filepath.read_text(encoding="utf-8")
            payload = json.loads(content)
            # Coerce timezone-naive ISO timestamps to UTC. Pydantic rejects
            # naive datetimes when the schema field is tz-aware; operators
            # occasionally produce naive timestamps from older serializers.
            ts = payload.get("generated_at_utc")
            if isinstance(ts, str) and ts:
                try:
                    parsed_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    if parsed_ts.tzinfo is None:
                        payload["generated_at_utc"] = ts + "+00:00"
                except ValueError:
                    pass  # let pydantic surface the parse error below
            report = RuntimeAuditReport.model_validate(payload)
        except Exception:
            # Skip malformed or schema-incompatible artifacts but count them
            # so operators can detect upstream corruption / serializer drift.
            skipped_artifacts += 1
            continue

        if report.generated_at_utc < cutoff_time:
            continue

        scanned_files += 1

        # Track timestamps for window reporting
        if first_timestamp is None or report.generated_at_utc < first_timestamp:
            first_timestamp = report.generated_at_utc
        if last_timestamp is None or report.generated_at_utc > last_timestamp:
            last_timestamp = report.generated_at_utc

        # Dry-run posture (document as context, flag if it changed)
        if report.readiness_probe and report.readiness_probe.dry_run_posture:
            current_dry_run = report.readiness_probe.dry_run_posture.dry_run_confirmed
            if dry_run_posture is None:
                dry_run_posture = current_dry_run
            elif current_dry_run != dry_run_posture:
                dry_run_changed = True

        # Safety gate failures — count both report-level status and
        # individual findings flagged CRITICAL/SAFETY_GATE so artifacts that
        # carry the failure as a finding (not just a status) are still tallied.
        if report.status == RuntimeAuditStatus.SAFETY_GATE_FAILED:
            critical_safety_gates += 1
        for finding in report.findings:
            if (
                finding.severity == RuntimeAuditSeverity.CRITICAL
                and finding.finding_type == RuntimeAuditFindingType.SAFETY_GATE
            ):
                critical_safety_gates += 1

        # Ledger summary accumulators
        if report.ledger_summary and report.ledger_summary.available:
            total_errors += report.ledger_summary.error_count
            total_warnings += report.ledger_summary.warning_count
            budget_blocks += report.ledger_summary.budget_block_count
            provider_failures += report.ledger_summary.provider_failure_count
            ws_reconnects += report.ledger_summary.ws_reconnect_count
            cooldown_blocks += report.ledger_summary.cooldown_block_count
            market_quarantines += report.ledger_summary.market_quarantine_count

        # Decision distribution
        if report.decision_summary and report.decision_summary.available:
            total_decisions += report.decision_summary.total_decisions
            buy_count += report.decision_summary.buy_count
            sell_count += report.decision_summary.sell_count
            hold_count += report.decision_summary.hold_count
            skip_count += report.decision_summary.skip_count

        # Position exposure (track max)
        if report.position_summary and report.position_summary.available:
            position_observed = True
            if report.position_summary.total_open_exposure_usdc > max_exposure_usdc:
                max_exposure_usdc = report.position_summary.total_open_exposure_usdc

        # Health probe response time
        if report.health_probe and report.health_probe.response_time_ms is not None:
            total_response_time += report.health_probe.response_time_ms
            health_samples += 1

        # DB size tracking (first and last for delta)
        if report.database_probe and report.database_probe.file_exists:
            db_size = report.database_probe.file_size_bytes
            if first_db_size is None:
                first_db_size = db_size
            last_db_size = db_size

    # ── Zero-artifact detection ──────────────────────────────────────────

    if scanned_files == 0:
        error_output = {
            "error": "no_artifacts_in_window",
            "detail": f"No valid audit artifacts found in the last {args.hours} hours",
            "cutoff_utc": cutoff_time.isoformat(),
            "artifacts_directory": str(audits_dir),
            "scanned_files": 0,
            "skipped_artifacts": skipped_artifacts,
        }
        print(json.dumps(error_output, indent=2))
        return 1

    # ── Derived metrics ──────────────────────────────────────────────────

    avg_response_time_ms = (
        total_response_time / Decimal(str(health_samples))
        if health_samples > 0
        else Decimal("0")
    )

    db_growth_bytes = 0
    if first_db_size is not None and last_db_size is not None:
        db_growth_bytes = last_db_size - first_db_size

    # Fix Plan threshold evaluation (explicit, not subjective)
    fix_plan_required = (
        critical_safety_gates > _FIX_PLAN_CRITICAL_SAFETY_GATES
        or total_errors > _FIX_PLAN_TOTAL_ERRORS
        or budget_blocks > _FIX_PLAN_BUDGET_BLOCKS
    )

    # ── Build output ─────────────────────────────────────────────────────

    # "unavailable" sentinels when no observation exists. Tests assert on the
    # exact string; downstream consumers treat it as a non-numeric placeholder.
    avg_response_time_out: str = (
        _decimal_to_str(avg_response_time_ms) if health_samples > 0 else "unavailable"
    )
    max_exposure_out: str = (
        _decimal_to_str(max_exposure_usdc) if position_observed else "unavailable"
    )

    summary = {
        "scanned_files": scanned_files,
        "skipped_artifacts": skipped_artifacts,
        "window_start_utc": first_timestamp.isoformat() if first_timestamp else None,
        "window_end_utc": last_timestamp.isoformat() if last_timestamp else None,
        "lookback_hours": args.hours,
        "hours": args.hours,
        # Safety and errors
        "critical_safety_gates": critical_safety_gates,
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "budget_blocks": budget_blocks,
        "provider_failures": provider_failures,
        "ws_reconnects": ws_reconnects,
        "cooldown_blocks": cooldown_blocks,
        "market_quarantines": market_quarantines,
        # Performance
        "avg_response_time_ms": avg_response_time_out,
        "health_samples": health_samples,
        # Exposure
        "max_exposure_usdc": max_exposure_out,
        # Database
        "db_growth_bytes": db_growth_bytes,
        "db_growth_bytes_delta": str(db_growth_bytes),
        # Dry-run posture (context, not a finding unless changed)
        "dry_run_posture": dry_run_posture,
        "dry_run_changed": dry_run_changed,
        "dry_run_inconsistent": dry_run_changed,
        # Decision distribution — exact-shape contract: only buy/sell/hold/skip.
        # Aggregate total surfaced as `total_decisions` at the top level.
        "decision_distribution": {
            "buy": buy_count,
            "sell": sell_count,
            "hold": hold_count,
            "skip": skip_count,
        },
        "total_decisions": total_decisions,
        # Fix Plan trigger
        "fix_plan_required": fix_plan_required,
        "fix_plan_triggers": {
            "critical_safety_gates_exceeded": critical_safety_gates
            > _FIX_PLAN_CRITICAL_SAFETY_GATES,
            "total_errors_exceeded": total_errors > _FIX_PLAN_TOTAL_ERRORS,
            "budget_blocks_exceeded": budget_blocks > _FIX_PLAN_BUDGET_BLOCKS,
        },
        "fix_plan_thresholds": {
            "critical_safety_gates": f"> {_FIX_PLAN_CRITICAL_SAFETY_GATES}",
            "total_errors": f"> {_FIX_PLAN_TOTAL_ERRORS}",
            "budget_blocks": f"> {_FIX_PLAN_BUDGET_BLOCKS}",
        },
    }

    # Scrub output for secrets before emission
    output_json = json.dumps(summary, indent=2)
    scrubbed_output = _scrub_text(output_json)

    print(scrubbed_output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
