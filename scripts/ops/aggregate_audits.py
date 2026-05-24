#!/usr/bin/env python3
"""
scripts/ops/aggregate_audits.py

WI-62 — Server Runtime Review: Deterministic Audit Aggregator.

Aggregates WI-61 RuntimeAuditReport JSON artifacts over a rolling
lookback window using Decimal-safe arithmetic. Produces a JSON summary
to stdout for consumption by the openclaude skill or manual inspection.

Exit codes:
  0 — success (artifacts found and aggregated)
  1 — no artifacts in window or artifact directory missing
  2 — configuration or argument error

All arithmetic uses Decimal end-to-end. No float coercion.
All output is scrubbed of secrets and high-cardinality identifiers.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import structlog

# Configure structlog to output to stderr, not stdout
logging.basicConfig(
    format="%(message)s",
    stream=sys.stderr,
    level=logging.INFO,
)
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer(),
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)

# ── Forbidden content patterns (mirrors WI-61 runtime_audit.py) ───────────

_FORBIDDEN_CONTENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key_hex", re.compile(r"\b[0-9a-fA-F]{64}\b")),
    ("private_key_0x", re.compile(r"0x[0-9a-fA-F]{64}\b")),
    ("telegram_token", re.compile(r"\b\d{8,10}:[a-zA-Z0-9_-]{35,}\b")),
    ("wallet_address", re.compile(r"0x[a-fA-F0-9]{40}\b")),
    ("api_key_sk", re.compile(r"sk-[a-zA-Z0-9_-]{20,}\b")),
    ("api_key_pk", re.compile(r"pk-[a-zA-Z0-9_-]{20,}\b")),
    ("condition_id_long", re.compile(r"0x[0-9a-fA-F]{60,}\b")),
    ("token_id_digits", re.compile(r"\b\d{15,}\b")),
]

def _scrub_text(text: str) -> str:
    """Redact forbidden patterns from text."""
    result = text
    for label, pattern in _FORBIDDEN_CONTENT_PATTERNS:
        result = pattern.sub(f"[REDACTED:{label}]", result)
    return result


def _scrub_json_string(json_str: str) -> str:
    """Scrub a JSON string for forbidden content."""
    return _scrub_text(json_str)


def _to_decimal(value: Any) -> Decimal | None:
    """Safely convert a value to Decimal, returning None on failure."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        # Reject float — convert via string representation
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _parse_timestamp(ts_str: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string to a timezone-aware datetime."""
    if not ts_str:
        return None
    try:
        # Try parsing with timezone info
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Treat naive timestamps as UTC per WI-61 convention
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _extract_artifact_metrics(artifact: dict[str, Any]) -> dict[str, Any]:
    """Extract metrics from a single artifact for aggregation."""
    metrics: dict[str, Any] = {
        "errors": 0,
        "warnings": 0,
        "budget_blocks": 0,
        "provider_failures": 0,
        "critical_safety_gates": 0,
        "response_times": [],
        "exposures": [],
        "file_size_bytes": 0,
        "dry_run": True,
        "decisions": {"buy": 0, "sell": 0, "hold": 0, "skip": 0},
        "timestamp": None,
    }

    # Count findings by severity
    findings = artifact.get("findings", [])
    for finding in findings:
        severity = finding.get("severity", "")
        finding_type = finding.get("finding_type", "")
        if severity == "ERROR":
            metrics["errors"] += 1
        elif severity == "WARNING":
            metrics["warnings"] += 1
        elif severity == "CRITICAL":
            metrics["errors"] += 1  # Critical counts as error
        if finding_type == "SAFETY_GATE":
            metrics["critical_safety_gates"] += 1

    # Extract ledger summary metrics
    ledger = artifact.get("ledger_summary", {})
    if ledger:
        metrics["budget_blocks"] += ledger.get("budget_block_count", 0)
        metrics["provider_failures"] += ledger.get("provider_failure_count", 0)

    # Extract health probe response time
    health = artifact.get("health_probe", {})
    if health and health.get("response_time_ms") is not None:
        rt = _to_decimal(health["response_time_ms"])
        if rt is not None:
            metrics["response_times"].append(rt)

    # Extract position exposure
    position = artifact.get("position_summary", {})
    if position and position.get("total_open_exposure_usdc") is not None:
        exp = _to_decimal(position["total_open_exposure_usdc"])
        if exp is not None:
            metrics["exposures"].append(exp)

    # Extract database file size
    db_probe = artifact.get("database_probe", {})
    if db_probe:
        metrics["file_size_bytes"] = db_probe.get("file_size_bytes", 0)

    # Extract dry_run posture
    readiness = artifact.get("readiness_probe", {})
    if readiness:
        dry_run_posture = readiness.get("dry_run_posture", {})
        if dry_run_posture:
            metrics["dry_run"] = dry_run_posture.get("dry_run_confirmed", True)

    # Extract decision summary
    decisions = artifact.get("decision_summary", {})
    if decisions:
        metrics["decisions"]["buy"] = decisions.get("buy_count", 0)
        metrics["decisions"]["sell"] = decisions.get("sell_count", 0)
        metrics["decisions"]["hold"] = decisions.get("hold_count", 0)
        metrics["decisions"]["skip"] = decisions.get("skip_count", 0)

    # Extract timestamp
    metrics["timestamp"] = artifact.get("generated_at_utc")

    return metrics


def aggregate_artifacts(
    artifact_dir: Path,
    hours: int,
) -> dict[str, Any]:
    """
    Aggregate WI-61 audit artifacts over a rolling lookback window.

    Returns a dict with aggregation results or an error dict.
    """
    # Check directory existence
    if not artifact_dir.exists():
        return {
            "error": "artifact_directory_not_found",
            "artifact_dir": str(artifact_dir),
        }

    if not artifact_dir.is_dir():
        return {
            "error": "artifact_dir_not_a_directory",
            "artifact_dir": str(artifact_dir),
        }

    # Compute lookback cutoff
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    # Find artifact files
    artifact_files = sorted(artifact_dir.glob("runtime-audit-*.json"))

    # Streaming aggregation with accumulators
    scanned_files = 0
    skipped_artifacts = 0
    total_errors = 0
    total_warnings = 0
    budget_blocks = 0
    provider_failures = 0
    critical_safety_gates = 0
    response_times: list[Decimal] = []
    exposures: list[Decimal] = []
    first_file_size: int | None = None
    last_file_size: int | None = None
    dry_run_values: list[bool] = []
    decision_totals = {"buy": 0, "sell": 0, "hold": 0, "skip": 0}
    artifacts_in_window = 0

    for artifact_path in artifact_files:
        # Parse timestamp from filename or file content
        # Try filename first: runtime-audit-YYYYMMDD-HHMMSS.json
        file_ts = None
        stem = artifact_path.stem
        if "runtime-audit-" in stem:
            ts_part = stem.replace("runtime-audit-", "")
            try:
                # Try YYYYMMDD-HHMMSS format
                file_ts = datetime.strptime(ts_part, "%Y%m%d-%H%M%S").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass

        # If filename parsing fails, read the file to get timestamp
        if file_ts is None:
            try:
                with artifact_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                ts_str = data.get("generated_at_utc", "")
                file_ts = _parse_timestamp(ts_str)
            except (json.JSONDecodeError, OSError):
                skipped_artifacts += 1
                logger.warning(
                    "skipped_malformed_artifact",
                    path=str(artifact_path),
                    reason="parse_error",
                )
                continue

        # Check if artifact is within lookback window
        if file_ts is None or file_ts < cutoff:
            continue

        # Read and parse artifact
        try:
            with artifact_path.open("r", encoding="utf-8") as f:
                artifact = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            skipped_artifacts += 1
            logger.warning(
                "skipped_malformed_artifact",
                path=str(artifact_path),
                reason=str(e),
            )
            continue

        # Extract metrics
        metrics = _extract_artifact_metrics(artifact)

        # Accumulate
        scanned_files += 1
        artifacts_in_window += 1
        total_errors += metrics["errors"]
        total_warnings += metrics["warnings"]
        budget_blocks += metrics["budget_blocks"]
        provider_failures += metrics["provider_failures"]
        critical_safety_gates += metrics["critical_safety_gates"]
        response_times.extend(metrics["response_times"])
        exposures.extend(metrics["exposures"])
        dry_run_values.append(metrics["dry_run"])

        # Track file sizes for DB growth delta
        file_size = metrics["file_size_bytes"]
        if first_file_size is None:
            first_file_size = file_size
        last_file_size = file_size

        # Accumulate decisions
        for key in decision_totals:
            decision_totals[key] += metrics["decisions"][key]

    # Check for zero artifacts in window
    if artifacts_in_window == 0:
        return {
            "error": "no_artifacts_in_window",
            "hours": hours,
            "cutoff_utc": cutoff.isoformat(),
            "scanned_files": scanned_files,
            "skipped_artifacts": skipped_artifacts,
        }

    # Compute aggregates using Decimal
    avg_response_time_ms: Decimal | None = None
    if response_times:
        total_rt = sum(response_times, Decimal("0"))
        avg_response_time_ms = (total_rt / Decimal(str(len(response_times)))).quantize(
            Decimal("0.01")
        )

    max_exposure_usdc: Decimal | None = None
    if exposures:
        max_exposure_usdc = max(exposures)

    db_growth_bytes_delta: Decimal = Decimal("0")
    if first_file_size is not None and last_file_size is not None:
        db_growth_bytes_delta = Decimal(str(last_file_size)) - Decimal(
            str(first_file_size)
        )

    # Determine dry_run posture
    dry_run_posture = all(dry_run_values) if dry_run_values else True
    dry_run_inconsistent = len(set(dry_run_values)) > 1 if dry_run_values else False

    # Determine fix_plan_required based on explicit thresholds
    fix_plan_required = (
        critical_safety_gates > 0 or total_errors > 50 or budget_blocks > 10
    )

    # Build result
    result = {
        "scanned_files": scanned_files,
        "skipped_artifacts": skipped_artifacts,
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "budget_blocks": budget_blocks,
        "provider_failures": provider_failures,
        "critical_safety_gates": critical_safety_gates,
        "avg_response_time_ms": str(avg_response_time_ms)
        if avg_response_time_ms is not None
        else "unavailable",
        "max_exposure_usdc": str(max_exposure_usdc)
        if max_exposure_usdc is not None
        else "unavailable",
        "db_growth_bytes_delta": str(db_growth_bytes_delta),
        "dry_run_posture": dry_run_posture,
        "dry_run_inconsistent": dry_run_inconsistent,
        "decision_distribution": decision_totals,
        "fix_plan_required": fix_plan_required,
        "hours": hours,
        "cutoff_utc": cutoff.isoformat(),
        "generated_at_utc": now.isoformat(),
    }

    return result


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Aggregate WI-61 runtime audit artifacts over a rolling window."
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=72,
        help="Lookback window in hours (default: 72)",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path("docs/operations/runtime_audits"),
        help="Directory containing runtime-audit-*.json artifacts",
    )
    args = parser.parse_args()

    if args.hours <= 0:
        logger.error("invalid_hours", hours=args.hours)
        print(
            json.dumps({"error": "invalid_hours", "hours": args.hours}),
            file=sys.stderr,
        )
        return 2

    result = aggregate_artifacts(args.artifact_dir, args.hours)

    # Check for error conditions
    if "error" in result:
        error_type = result["error"]
        result_json = json.dumps(result, indent=2)
        scrubbed_json = _scrub_json_string(result_json)
        print(scrubbed_json)
        if error_type in ("artifact_directory_not_found", "artifact_dir_not_a_directory"):
            logger.error(error_type, artifact_dir=str(args.artifact_dir))
            return 1
        elif error_type == "no_artifacts_in_window":
            logger.warning(error_type, hours=args.hours)
            return 1

    # Scrub output for secrets
    result_json = json.dumps(result, indent=2)
    scrubbed_json = _scrub_json_string(result_json)

    print(scrubbed_json)
    logger.info(
        "aggregation_complete",
        scanned_files=result.get("scanned_files", 0),
        fix_plan_required=result.get("fix_plan_required", False),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
