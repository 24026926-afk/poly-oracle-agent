"""
src/observability/runtime_audit.py

WI-61 — Periodic Runtime Audit service.

Deterministic, read-only, out-of-process auditor that probes the running
paper-trading deployment and produces typed JSON + markdown safety
evidence artifacts.  Alerts through Telegram on degradation when enabled.

Constraints:

* Read-only for all runtime state — never appends, updates, deletes,
  backfills, repairs, or acknowledges operational events.
* All database reads route through existing repositories.
* No raw SQL, no direct ``AsyncSession`` queries in audit logic.
* SQLite file probes use read-only URI semantics.
* Never calls Claude, DeepSeek, Grok, or any LLM from the auditor.
* The optional LLM reviewer is a separate function using direct httpx.
* All HTTP, Docker, Telegram, and reviewer paths have explicit timeouts.
* All precision-sensitive math uses ``Decimal`` end to end.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models import (
    OperationalEvent,
    Position,
)
from src.db.repositories.decision_repo import DecisionRepository
from src.db.repositories.execution_repo import ExecutionRepository
from src.db.repositories.market_repo import MarketRepository
from src.db.repositories.operational_event_repository import (
    OperationalEventRepository,
)
from src.db.repositories.position_repository import PositionRepository
from src.schemas.ops import (
    OperationalEventQuery,
    OperationalEventSeverity,
    OperationalEventType,
)
from src.schemas.runtime_audit import (
    RuntimeAuditArtifactWriteResult,
    RuntimeAuditDatabaseProbe,
    RuntimeAuditDecisionSummary,
    RuntimeAuditDockerProbe,
    RuntimeAuditDryRunPosture,
    RuntimeAuditExecutionSummary,
    RuntimeAuditExitCode,
    RuntimeAuditFailureReason,
    RuntimeAuditFinding,
    RuntimeAuditFindingType,
    RuntimeAuditForbiddenContentCheck,
    RuntimeAuditHealthProbe,
    RuntimeAuditLLMReviewRequest,
    RuntimeAuditLLMReviewResult,
    RuntimeAuditLLMReviewStatus,
    RuntimeAuditLedgerSummary,
    RuntimeAuditLogTailSummary,
    RuntimeAuditMarketSummary,
    RuntimeAuditMetricSample,
    RuntimeAuditMetricsProbe,
    RuntimeAuditPositionSummary,
    RuntimeAuditProbeStatus,
    RuntimeAuditReadinessProbe,
    RuntimeAuditReport,
    RuntimeAuditSeverity,
    RuntimeAuditStatus,
    RuntimeAuditTelegramAlert,
    RuntimeAuditTelegramResult,
)

logger = structlog.get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────

_DEFAULT_HTTP_TIMEOUT_SEC: float = 10.0
_DEFAULT_DOCKER_TIMEOUT_SEC: float = 15.0
_DEFAULT_LOG_BYTE_CAP: int = 65536
_DEFAULT_LOG_LINE_CAP: int = 1000
_DEFAULT_EVENT_LOOKBACK_HOURS: int = 24
_DEFAULT_EVENT_LIMIT: int = 1000
_DEFAULT_DECISION_LIMIT: int = 100
_DEFAULT_MARKET_LIMIT: int = 100
_DEFAULT_EXECUTION_LIMIT: int = 100
_ARTIFACT_DIR_NAME: str = "runtime_audits"
_REVIEW_DIR_NAME: str = "runtime_reviews"
_LATEST_JSON: str = "latest.json"
_LATEST_MD: str = "latest.md"
_TIMESTAMP_FMT: str = "%Y%m%dT%H%M%SZ"

_FORBIDDEN_METRIC_LABELS: frozenset[str] = frozenset({
    "condition_id", "token_id", "wallet_address", "prompt_text",
    "reasoning_text", "secret", "private_key", "api_key", "wallet",
})

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

_FORBIDDEN_CONTENT_SUBSTRINGS: list[str] = [
    "api_key", "private_key", "secret_key", "raw_prompt",
    "reasoning_text", "connection_string", "password=",
]

_PROM_LINE_RE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{([^}]*)\})?"
    r"\s+([^\s]+)(?:\s+\d+)?\s*$"
)
_PROM_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="([^"]*)"')


def _compute_cooldown_block_rate(
    ledger_summary: Optional[RuntimeAuditLedgerSummary],
    decision_summary: Optional[RuntimeAuditDecisionSummary],
) -> Optional[Decimal]:
    """Return the share of LLM-eval attempts blocked by the WI-52 cognitive
    breaker in the audit window.

    F2 (2026-05-23): rate = cooldowns / (cooldowns + decisions). Returns None
    when either summary is unavailable or both summaries are zero (no signal
    to compute over). All arithmetic is Decimal-only — no float coercion.
    Result is clipped to the [0, 1] domain enforced by the schema validator.
    """
    if ledger_summary is None or decision_summary is None:
        return None
    if not ledger_summary.available or not decision_summary.available:
        return None
    cooldowns = Decimal(ledger_summary.cooldown_block_count)
    decisions = Decimal(decision_summary.total_decisions)
    denom = cooldowns + decisions
    if denom == Decimal("0"):
        return None
    return (cooldowns / denom).quantize(Decimal("0.0001"))


def check_forbidden_content(text: str) -> RuntimeAuditForbiddenContentCheck:
    """Scan text for forbidden secrets and high-cardinality identifiers."""
    found: list[str] = []
    for label, pattern in _FORBIDDEN_CONTENT_PATTERNS:
        if pattern.search(text):
            found.append(label)
    for substr in _FORBIDDEN_CONTENT_SUBSTRINGS:
        if substr.lower() in text.lower():
            found.append(f"substring:{substr}")
    return RuntimeAuditForbiddenContentCheck(
        scanned=True, clean=len(found) == 0, forbidden_patterns_found=found,
    )


def parse_prometheus_text(
    raw: str,
) -> tuple[list[RuntimeAuditMetricSample], list[str], Optional[str]]:
    """Parse Prometheus text exposition. Returns (samples, forbidden_labels, error).
    
    Fails closed: any parse error returns error message even if some samples parsed.
    """
    samples: list[RuntimeAuditMetricSample] = []
    forbidden_labels: list[str] = []
    parse_errors: list[str] = []
    for line_num, line in enumerate(raw.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _PROM_LINE_RE.match(stripped)
        if not m:
            parse_errors.append(f"line {line_num}: unparseable")
            continue
        name, labels_str, value_str = m.group(1), m.group(2) or "", m.group(3)
        labels: dict[str, str] = {}
        for lm in _PROM_LABEL_RE.finditer(labels_str):
            key, val = lm.group(1), lm.group(2)
            labels[key] = val
            if key in _FORBIDDEN_METRIC_LABELS:
                forbidden_labels.append(f"{key}={val[:16]}")
            # Scan label values for forbidden content
            val_check = check_forbidden_content(val)
            if not val_check.clean:
                forbidden_labels.append(f"{key}_value_forbidden:{val_check.forbidden_patterns_found[0][:32]}")
        try:
            value = Decimal(value_str)
        except (InvalidOperation, ValueError):
            parse_errors.append(f"line {line_num}: invalid value")
            continue
        samples.append(RuntimeAuditMetricSample(name=name, value=value, labels=labels))
    # Fail closed: return error if ANY parse errors occurred
    error_msg = "; ".join(parse_errors[:5]) if parse_errors else None
    return samples, forbidden_labels, error_msg


async def probe_health(
    http_client: httpx.AsyncClient,
    base_url: str,
    timeout: float = _DEFAULT_HTTP_TIMEOUT_SEC,
) -> RuntimeAuditHealthProbe:
    """Probe the /healthz endpoint."""
    url = f"{base_url.rstrip('/')}/healthz"
    try:
        start = datetime.now(timezone.utc)
        resp = await http_client.get(url, timeout=timeout)
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        if resp.status_code == 200:
            return RuntimeAuditHealthProbe(
                status=RuntimeAuditProbeStatus.SUCCESS,
                reachable=True,
                response_time_ms=Decimal(str(round(elapsed * 1000, 2))),
            )
        return RuntimeAuditHealthProbe(
            status=RuntimeAuditProbeStatus.ERROR, reachable=True,
            message=f"HTTP {resp.status_code}",
        )
    except httpx.TimeoutException:
        return RuntimeAuditHealthProbe(
            status=RuntimeAuditProbeStatus.TIMEOUT, reachable=False,
            message=f"Timeout after {timeout}s",
        )
    except Exception as exc:
        return RuntimeAuditHealthProbe(
            status=RuntimeAuditProbeStatus.ERROR, reachable=False,
            message=f"Connection error: {type(exc).__name__}",
        )


async def probe_readiness(
    http_client: httpx.AsyncClient,
    base_url: str,
    timeout: float = _DEFAULT_HTTP_TIMEOUT_SEC,
) -> RuntimeAuditReadinessProbe:
    """Probe the /readyz endpoint."""
    url = f"{base_url.rstrip('/')}/readyz"
    try:
        resp = await http_client.get(url, timeout=timeout)
        if resp.status_code not in (200, 503):
            return RuntimeAuditReadinessProbe(
                status=RuntimeAuditProbeStatus.ERROR, reachable=True,
                message=f"Unexpected HTTP {resp.status_code}",
            )
        try:
            body = resp.json()
        except Exception:
            return RuntimeAuditReadinessProbe(
                status=RuntimeAuditProbeStatus.ERROR, reachable=True,
                message="Invalid JSON response",
            )
        status_str = body.get("status", "")
        ready = status_str == "READY"
        checks = body.get("checks", {})
        dry_run_val = body.get("dry_run")
        dry_run_posture: Optional[RuntimeAuditDryRunPosture] = None
        if dry_run_val is not None:
            dry_run_posture = RuntimeAuditDryRunPosture(
                dry_run_confirmed=dry_run_val is True,
                source="readyz", raw_value=str(dry_run_val),
            )
        probe_status = (
            RuntimeAuditProbeStatus.SUCCESS
            if resp.status_code == 200
            else RuntimeAuditProbeStatus.DEGRADED
        )
        return RuntimeAuditReadinessProbe(
            status=probe_status, reachable=True, ready=ready,
            dry_run_posture=dry_run_posture,
            checks={str(k): str(v) for k, v in checks.items()},
        )
    except httpx.TimeoutException:
        return RuntimeAuditReadinessProbe(
            status=RuntimeAuditProbeStatus.TIMEOUT, reachable=False,
            message=f"Timeout after {timeout}s",
        )
    except Exception as exc:
        return RuntimeAuditReadinessProbe(
            status=RuntimeAuditProbeStatus.ERROR, reachable=False,
            message=f"Connection error: {type(exc).__name__}",
        )


async def probe_metrics(
    http_client: httpx.AsyncClient,
    base_url: str,
    timeout: float = _DEFAULT_HTTP_TIMEOUT_SEC,
) -> tuple[RuntimeAuditMetricsProbe, list[RuntimeAuditMetricSample]]:
    """Probe /metrics and parse Prometheus text."""
    url = f"{base_url.rstrip('/')}/metrics"
    try:
        resp = await http_client.get(url, timeout=timeout)
        if resp.status_code != 200:
            return (RuntimeAuditMetricsProbe(
                status=RuntimeAuditProbeStatus.ERROR, reachable=True,
                message=f"HTTP {resp.status_code}",
            ), [])
        samples, forbidden, parse_err = parse_prometheus_text(resp.text)
        if forbidden:
            return (RuntimeAuditMetricsProbe(
                status=RuntimeAuditProbeStatus.ERROR, reachable=True,
                sample_count=len(samples), forbidden_labels_found=forbidden,
                message=f"Forbidden labels: {len(forbidden)}",
            ), samples)
        if parse_err:
            return (RuntimeAuditMetricsProbe(
                status=RuntimeAuditProbeStatus.ERROR, reachable=True,
                sample_count=len(samples),
                parse_error=parse_err[:512] if parse_err else None,
                message="Parse error",
            ), samples)
        return (RuntimeAuditMetricsProbe(
            status=RuntimeAuditProbeStatus.SUCCESS, reachable=True,
            sample_count=len(samples),
        ), samples)
    except httpx.TimeoutException:
        return (RuntimeAuditMetricsProbe(
            status=RuntimeAuditProbeStatus.TIMEOUT, reachable=False,
            message=f"Timeout after {timeout}s",
        ), [])
    except Exception as exc:
        return (RuntimeAuditMetricsProbe(
            status=RuntimeAuditProbeStatus.ERROR, reachable=False,
            message=f"Connection error: {type(exc).__name__}",
        ), [])


def check_dry_run_posture(
    readiness: RuntimeAuditReadinessProbe,
) -> Optional[RuntimeAuditFinding]:
    """Check dry_run=true. Returns safety-gate finding or None."""
    if readiness.dry_run_posture is None:
        return RuntimeAuditFinding(
            finding_type=RuntimeAuditFindingType.SAFETY_GATE,
            severity=RuntimeAuditSeverity.CRITICAL,
            message="Dry-run posture missing from readiness probe",
            source="readiness",
            failure_reason=RuntimeAuditFailureReason.DRY_RUN_POSTURE_MISSING,
        )
    if not readiness.dry_run_posture.dry_run_confirmed:
        return RuntimeAuditFinding(
            finding_type=RuntimeAuditFindingType.SAFETY_GATE,
            severity=RuntimeAuditSeverity.CRITICAL,
            message="dry_run=false detected — mandatory safety gate failed",
            source="readiness",
            failure_reason=RuntimeAuditFailureReason.DRY_RUN_FALSE,
        )
    return None


async def probe_database(
    db_path: Optional[str] = None,
    max_size_bytes: int = 500_000_000,
) -> RuntimeAuditDatabaseProbe:
    """Probe SQLite database file (read-only, no creation)."""
    if db_path is None:
        db_path = os.environ.get("SQLITE_DB_PATH", "poly_oracle.db")
    path = Path(db_path)
    if not path.exists():
        return RuntimeAuditDatabaseProbe(
            status=RuntimeAuditProbeStatus.UNAVAILABLE, file_exists=False,
            message=f"Database file not found: {path.name}",
        )
    try:
        size = path.stat().st_size
        status = RuntimeAuditProbeStatus.SUCCESS
        msg: Optional[str] = None
        if size > max_size_bytes:
            status = RuntimeAuditProbeStatus.DEGRADED
            msg = f"Database size {size} bytes exceeds threshold {max_size_bytes}"
        return RuntimeAuditDatabaseProbe(
            status=status, file_exists=True, file_size_bytes=size, message=msg,
        )
    except Exception as exc:
        return RuntimeAuditDatabaseProbe(
            status=RuntimeAuditProbeStatus.ERROR, file_exists=True,
            message=f"Stat error: {type(exc).__name__}",
        )


async def probe_docker(
    timeout: float = _DEFAULT_DOCKER_TIMEOUT_SEC,
) -> RuntimeAuditDockerProbe:
    """Probe Docker Compose service status (read-only)."""
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--format", "json"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return RuntimeAuditDockerProbe(
                status=RuntimeAuditProbeStatus.UNAVAILABLE,
                docker_available=False, message="docker compose ps failed",
            )
        services_running = 0
        services_total = 0
        max_restarts = 0
        for line in result.stdout.strip().splitlines():
            if not line.strip():
                continue
            try:
                svc = json.loads(line)
            except json.JSONDecodeError:
                continue
            services_total += 1
            state = svc.get("State", "")
            if state in ("running", "exited"):
                services_running += 1
            restarts = svc.get("RestartCount", 0)
            if isinstance(restarts, int) and restarts > max_restarts:
                max_restarts = restarts
        status = RuntimeAuditProbeStatus.SUCCESS
        if services_total == 0:
            status = RuntimeAuditProbeStatus.UNAVAILABLE
        elif services_running < services_total:
            status = RuntimeAuditProbeStatus.DEGRADED
        return RuntimeAuditDockerProbe(
            status=status, docker_available=True,
            services_running=services_running, services_total=services_total,
            max_restart_count=max_restarts,
        )
    except FileNotFoundError:
        return RuntimeAuditDockerProbe(
            status=RuntimeAuditProbeStatus.UNAVAILABLE,
            docker_available=False, message="Docker not installed",
        )
    except subprocess.TimeoutExpired:
        return RuntimeAuditDockerProbe(
            status=RuntimeAuditProbeStatus.TIMEOUT,
            docker_available=False, message=f"Timeout after {timeout}s",
        )
    except Exception as exc:
        return RuntimeAuditDockerProbe(
            status=RuntimeAuditProbeStatus.ERROR,
            docker_available=False, message=f"Docker error: {type(exc).__name__}",
        )


async def probe_log_tail(
    log_path: Optional[str] = None,
    byte_cap: int = _DEFAULT_LOG_BYTE_CAP,
    line_cap: int = _DEFAULT_LOG_LINE_CAP,
) -> RuntimeAuditLogTailSummary:
    """Bounded application log tail inspection."""
    if log_path is None:
        log_path = os.environ.get("APP_LOG_PATH", "")
    if not log_path:
        return RuntimeAuditLogTailSummary(
            status=RuntimeAuditProbeStatus.UNAVAILABLE,
            message="No log path configured",
        )
    path = Path(log_path)
    if not path.exists():
        return RuntimeAuditLogTailSummary(
            status=RuntimeAuditProbeStatus.UNAVAILABLE,
            message=f"Log file not found: {path.name}",
        )
    try:
        size = path.stat().st_size
        read_bytes = min(size, byte_cap)
        with open(path, "rb") as f:
            if size > byte_cap:
                f.seek(-byte_cap, 2)
            data = f.read(read_bytes)
        text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()[-line_cap:]
        error_count = sum(1 for l in lines if '"level": "error"' in l.lower() or "error" in l.lower()[:20])
        warning_count = sum(1 for l in lines if '"level": "warning"' in l.lower() or "warning" in l.lower()[:20])
        forbidden = check_forbidden_content(text)
        return RuntimeAuditLogTailSummary(
            status=RuntimeAuditProbeStatus.SUCCESS,
            bytes_scanned=len(data), lines_scanned=len(lines),
            forbidden_content_detected=not forbidden.clean,
            error_count=error_count, warning_count=warning_count,
        )
    except Exception as exc:
        return RuntimeAuditLogTailSummary(
            status=RuntimeAuditProbeStatus.ERROR,
            message=f"Log tail error: {type(exc).__name__}",
        )


async def summarize_ledger(
    session_factory: async_sessionmaker[AsyncSession],
    lookback_hours: int = _DEFAULT_EVENT_LOOKBACK_HOURS,
    limit: int = _DEFAULT_EVENT_LIMIT,
) -> RuntimeAuditLedgerSummary:
    """Bounded summary of the operational event ledger."""
    try:
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=lookback_hours)
        query = OperationalEventQuery(
            start_time_utc=start, end_time_utc=now,
            limit=limit, offset=0,
        )
        async with session_factory() as session:
            repo = OperationalEventRepository(session)
            window = await repo.read_window(query)
        events = window.events
        error_count = sum(1 for e in events if e.severity == OperationalEventSeverity.ERROR)
        warning_count = sum(1 for e in events if e.severity == OperationalEventSeverity.WARNING)
        ws_reconnect = sum(
            1 for e in events
            if e.event_type == OperationalEventType.WS_RECONNECT
        )
        budget_block = sum(
            1 for e in events
            if e.event_type in (OperationalEventType.BUDGET_BLOCK, OperationalEventType.LLM_CALL_BLOCKED)
        )
        provider_fail = sum(
            1 for e in events if e.event_type == OperationalEventType.PROVIDER_FAILURE
        )
        market_q = sum(
            1 for e in events
            if e.event_type in (OperationalEventType.MARKET_QUARANTINE, OperationalEventType.MARKET_REJECTED)
        )
        readiness = sum(
            1 for e in events if e.event_type == OperationalEventType.READY_STATE_CHANGED
        )
        alert = sum(
            1 for e in events if e.event_type == OperationalEventType.ALERT_SENT
        )
        recovery = sum(
            1 for e in events if e.event_type == OperationalEventType.ERROR_RECOVERED
        )
        # F2 (2026-05-23): WI-52 cognitive cooldown activations.
        cooldown = sum(
            1 for e in events if e.event_type == OperationalEventType.COOLDOWN_BLOCK
        )
        return RuntimeAuditLedgerSummary(
            total_events=len(events), error_count=error_count,
            warning_count=warning_count, ws_reconnect_count=ws_reconnect,
            budget_block_count=budget_block, provider_failure_count=provider_fail,
            market_quarantine_count=market_q, readiness_change_count=readiness,
            alert_count=alert, recovery_count=recovery,
            cooldown_block_count=cooldown, available=True,
        )
    except Exception as exc:
        logger.warning("runtime_audit.ledger_summary_failed", error=str(exc))
        return RuntimeAuditLedgerSummary(
            available=False, message=f"Ledger read failed: {type(exc).__name__}",
        )


async def summarize_decision_repository(
    session_factory: async_sessionmaker[AsyncSession],
    lookback_hours: int = _DEFAULT_EVENT_LOOKBACK_HOURS,
    limit: int = _DEFAULT_DECISION_LIMIT,
) -> RuntimeAuditDecisionSummary:
    """Bounded summary of recent decisions."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        async with session_factory() as session:
            repo = DecisionRepository(session)
            decisions = await repo.get_recent_decisions(cutoff, limit)
        buy = sell = hold = skip = 0
        for d in decisions:
            action = getattr(d.recommended_action, "value", str(d.recommended_action)).upper()
            if action == "BUY":
                buy += 1
            elif action == "SELL":
                sell += 1
            elif action == "HOLD":
                hold += 1
            else:
                skip += 1
        return RuntimeAuditDecisionSummary(
            total_decisions=len(decisions), buy_count=buy,
            sell_count=sell, hold_count=hold, skip_count=skip, available=True,
        )
    except Exception as exc:
        logger.warning("runtime_audit.decision_summary_failed", error=str(exc))
        return RuntimeAuditDecisionSummary(
            available=False, message=f"Decision read failed: {type(exc).__name__}",
        )


async def summarize_market_repository(
    session_factory: async_sessionmaker[AsyncSession],
    lookback_hours: int = _DEFAULT_EVENT_LOOKBACK_HOURS,
    limit: int = _DEFAULT_MARKET_LIMIT,
) -> RuntimeAuditMarketSummary:
    """Bounded summary of recent market snapshots."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        async with session_factory() as session:
            repo = MarketRepository(session)
            snapshots = await repo.get_recent_snapshots(cutoff, limit)
        now = datetime.now(timezone.utc)
        stale_threshold = timedelta(minutes=30)
        stale = 0
        freshest: Optional[Decimal] = None
        for s in snapshots:
            age = now - s.captured_at
            if age > stale_threshold:
                stale += 1
            age_sec = Decimal(str(age.total_seconds()))
            if freshest is None or age_sec < freshest:
                freshest = age_sec
        return RuntimeAuditMarketSummary(
            total_snapshots=len(snapshots), stale_count=stale,
            freshness_seconds=freshest, available=True,
        )
    except Exception as exc:
        logger.warning("runtime_audit.market_summary_failed", error=str(exc))
        return RuntimeAuditMarketSummary(
            available=False, message=f"Market read failed: {type(exc).__name__}",
        )


async def summarize_position_repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> RuntimeAuditPositionSummary:
    """Bounded summary of positions."""
    try:
        async with session_factory() as session:
            pos_repo = PositionRepository(session)
            open_positions = await pos_repo.get_open_positions()
            settled = await pos_repo.get_settled_positions()
            exposure = await pos_repo.get_total_open_exposure_usdc()
        return RuntimeAuditPositionSummary(
            open_count=len(open_positions), settled_count=len(settled),
            total_open_exposure_usdc=exposure, available=True,
        )
    except Exception as exc:
        logger.warning("runtime_audit.position_summary_failed", error=str(exc))
        return RuntimeAuditPositionSummary(
            open_count=0, settled_count=0,
            total_open_exposure_usdc=Decimal("0"),
            available=False, message=f"Position read failed: {type(exc).__name__}",
        )


async def summarize_execution_repository(
    session_factory: async_sessionmaker[AsyncSession],
    lookback_hours: int = _DEFAULT_EVENT_LOOKBACK_HOURS,
    limit: int = _DEFAULT_EXECUTION_LIMIT,
) -> RuntimeAuditExecutionSummary:
    """Bounded summary of execution records."""
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        async with session_factory() as session:
            repo = ExecutionRepository(session)
            executions = await repo.get_recent_executions(cutoff, limit)
        dry_run_count = 0
        live_count = 0
        for e in executions:
            status_val = getattr(e.status, "value", str(e.status)).upper()
            if "DRY" in status_val:
                dry_run_count += 1
            else:
                live_count += 1
        return RuntimeAuditExecutionSummary(
            total_executions=len(executions), dry_run_count=dry_run_count,
            live_count=live_count, available=True,
        )
    except Exception as exc:
        logger.warning("runtime_audit.execution_summary_failed", error=str(exc))
        return RuntimeAuditExecutionSummary(
            available=False, message=f"Execution read failed: {type(exc).__name__}",
        )


def _validate_output_path(path: Path, project_root: Path) -> None:
    """Fail closed on path traversal, symlink escape, or absolute outside root."""
    resolved = path.resolve()
    root_resolved = project_root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise ValueError(
            f"Output path {resolved} is outside project root {root_resolved}"
        )


def _render_markdown(report: RuntimeAuditReport) -> str:
    """Render a deterministic markdown artifact from the typed report."""
    lines: list[str] = []
    lines.append(f"# Runtime Audit Report")
    lines.append("")
    lines.append(f"- **Status:** {report.status.value}")
    lines.append(f"- **Exit Code:** {report.exit_code.value}")
    lines.append(f"- **Generated:** {report.generated_at_utc.isoformat()}")
    lines.append("")
    if report.health_probe:
        lines.append(f"## Health Probe")
        lines.append(f"- Status: {report.health_probe.status.value}")
        lines.append(f"- Reachable: {report.health_probe.reachable}")
        lines.append("")
    if report.readiness_probe:
        lines.append(f"## Readiness Probe")
        lines.append(f"- Status: {report.readiness_probe.status.value}")
        lines.append(f"- Ready: {report.readiness_probe.ready}")
        if report.readiness_probe.dry_run_posture:
            lines.append(
                f"- Dry Run: {report.readiness_probe.dry_run_posture.dry_run_confirmed}"
            )
        lines.append("")
    if report.metrics_probe:
        lines.append(f"## Metrics Probe")
        lines.append(f"- Status: {report.metrics_probe.status.value}")
        lines.append(f"- Samples: {report.metrics_probe.sample_count}")
        lines.append("")
    if report.ledger_summary:
        lines.append(f"## Ledger Summary")
        lines.append(f"- Total Events: {report.ledger_summary.total_events}")
        lines.append(f"- Errors: {report.ledger_summary.error_count}")
        lines.append(f"- Warnings: {report.ledger_summary.warning_count}")
        lines.append("")
    if report.findings:
        lines.append(f"## Findings ({len(report.findings)})")
        for f in report.findings:
            lines.append(f"- [{f.severity.value}] {f.finding_type.value}: {f.message}")
        lines.append("")
    return "\n".join(lines)


def write_audit_artifacts(
    report: RuntimeAuditReport,
    output_dir: Optional[Path] = None,
    project_root: Optional[Path] = None,
) -> RuntimeAuditArtifactWriteResult:
    """Write JSON + markdown artifacts with atomic latest swap."""
    if project_root is None:
        project_root = Path.cwd()
    if output_dir is None:
        output_dir = project_root / "docs" / "operations" / _ARTIFACT_DIR_NAME

    # Constrain output to docs/operations/runtime_audits/
    expected_dir = project_root / "docs" / "operations" / _ARTIFACT_DIR_NAME
    if output_dir.resolve() != expected_dir.resolve():
        return RuntimeAuditArtifactWriteResult(
            success=False,
            failure_reason=f"Output directory must be docs/operations/{_ARTIFACT_DIR_NAME}/",
        )

    try:
        _validate_output_path(output_dir, project_root)
    except ValueError as exc:
        return RuntimeAuditArtifactWriteResult(
            success=False, failure_reason=str(exc),
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = report.generated_at_utc.strftime(_TIMESTAMP_FMT)
    json_name = f"runtime-audit-{ts}.json"
    md_name = f"runtime-audit-{ts}.md"
    json_path = output_dir / json_name
    md_path = output_dir / md_name

    json_text = report.model_dump_json(indent=2)
    md_text = _render_markdown(report)

    json_check = check_forbidden_content(json_text)
    md_check = check_forbidden_content(md_text)
    if not json_check.clean or not md_check.clean:
        combined = json_check.forbidden_patterns_found + md_check.forbidden_patterns_found
        return RuntimeAuditArtifactWriteResult(
            success=False,
            failure_reason=f"Forbidden content detected: {combined[:5]}",
            forbidden_content_check=RuntimeAuditForbiddenContentCheck(
                scanned=True, clean=False, forbidden_patterns_found=combined,
            ),
        )

    try:
        json_path.write_text(json_text, encoding="utf-8")
        md_path.write_text(md_text, encoding="utf-8")
    except Exception as exc:
        return RuntimeAuditArtifactWriteResult(
            success=False, failure_reason=f"Write error: {type(exc).__name__}",
        )

    latest_json = output_dir / _LATEST_JSON
    latest_md = output_dir / _LATEST_MD
    latest_json_ok = False
    latest_md_ok = False
    latest_failure: Optional[str] = None
    try:
        tmp_j = latest_json.with_suffix(".tmp")
        tmp_j.write_text(json_text, encoding="utf-8")
        tmp_j.replace(latest_json)
        latest_json_ok = True
    except Exception as exc:
        latest_failure = f"latest.json swap failed: {type(exc).__name__}"
    try:
        tmp_m = latest_md.with_suffix(".tmp")
        tmp_m.write_text(md_text, encoding="utf-8")
        tmp_m.replace(latest_md)
        latest_md_ok = True
    except Exception as exc:
        detail = f"latest.md swap failed: {type(exc).__name__}"
        latest_failure = (
            f"{latest_failure}; {detail}" if latest_failure else detail
        )

    if not latest_json_ok or not latest_md_ok:
        return RuntimeAuditArtifactWriteResult(
            success=False,
            json_path=str(json_path.relative_to(project_root)),
            md_path=str(md_path.relative_to(project_root)),
            latest_json_updated=latest_json_ok,
            latest_md_updated=latest_md_ok,
            failure_reason=latest_failure,
            forbidden_content_check=RuntimeAuditForbiddenContentCheck(
                scanned=True, clean=True, forbidden_patterns_found=[],
            ),
        )

    return RuntimeAuditArtifactWriteResult(
        success=True,
        json_path=str(json_path.relative_to(project_root)),
        md_path=str(md_path.relative_to(project_root)),
        latest_json_updated=latest_json_ok,
        latest_md_updated=latest_md_ok,
        forbidden_content_check=RuntimeAuditForbiddenContentCheck(
            scanned=True, clean=True, forbidden_patterns_found=[],
        ),
    )


async def send_telegram_alert(
    notifier: Any,
    report: RuntimeAuditReport,
    enabled: bool = False,
) -> RuntimeAuditTelegramResult:
    """Send Telegram alert for exit code >= 1 when enabled."""
    if not enabled:
        return RuntimeAuditTelegramResult(sent=False, reason="disabled")
    if report.exit_code.value < 1:
        return RuntimeAuditTelegramResult(sent=False, reason="exit_code_0_no_alert")
    if notifier is None:
        return RuntimeAuditTelegramResult(sent=False, reason="notifier_unavailable")

    finding_count = len(report.findings)
    text = (
        f"[Runtime Audit] {report.status.value} (exit {report.exit_code.value})\n"
        f"Findings: {finding_count}\n"
        f"Time: {report.generated_at_utc.isoformat()}"
    )
    if len(text) > 1024:
        text = text[:1021] + "..."

    forbidden = check_forbidden_content(text)
    if not forbidden.clean:
        return RuntimeAuditTelegramResult(
            sent=False, reason="forbidden_content_in_payload",
        )

    try:
        sent = await notifier.try_send_execution_event(text, dry_run=True)
        if sent:
            return RuntimeAuditTelegramResult(sent=True)
        return RuntimeAuditTelegramResult(sent=False, reason="send_failed")
    except Exception as exc:
        return RuntimeAuditTelegramResult(
            sent=False, reason="send_error",
            failure_detail=type(exc).__name__,
        )


async def run_audit(
    *,
    http_client: httpx.AsyncClient,
    base_url: str = "http://127.0.0.1:8080",
    session_factory: Optional[async_sessionmaker[AsyncSession]] = None,
    db_path: Optional[str] = None,
    notifier: Any = None,
    telegram_enabled: bool = False,
    output_dir: Optional[Path] = None,
    project_root: Optional[Path] = None,
    http_timeout: float = _DEFAULT_HTTP_TIMEOUT_SEC,
    skip_docker: bool = True,
    skip_log_tail: bool = True,
) -> RuntimeAuditReport:
    """Run the full audit pipeline and return the typed report."""
    findings: list[RuntimeAuditFinding] = []
    now = datetime.now(timezone.utc)

    health = await probe_health(http_client, base_url, timeout=http_timeout)
    if health.status != RuntimeAuditProbeStatus.SUCCESS:
        if not health.reachable:
            findings.append(RuntimeAuditFinding(
                finding_type=RuntimeAuditFindingType.PROBE_ERROR,
                severity=RuntimeAuditSeverity.ERROR,
                message=f"Health probe unreachable: {health.message}",
                source="health", failure_reason=RuntimeAuditFailureReason.HEALTH_PROBE_ERROR,
            ))
        else:
            findings.append(RuntimeAuditFinding(
                finding_type=RuntimeAuditFindingType.PROBE_ERROR,
                severity=RuntimeAuditSeverity.ERROR,
                message=f"Health probe returned non-200: {health.message}",
                source="health", failure_reason=RuntimeAuditFailureReason.HEALTH_PROBE_ERROR,
            ))

    readiness = await probe_readiness(http_client, base_url, timeout=http_timeout)
    if readiness.status in (RuntimeAuditProbeStatus.ERROR, RuntimeAuditProbeStatus.TIMEOUT):
        if not readiness.reachable:
            findings.append(RuntimeAuditFinding(
                finding_type=RuntimeAuditFindingType.PROBE_ERROR,
                severity=RuntimeAuditSeverity.ERROR,
                message=f"Readiness probe unreachable: {readiness.message}",
                source="readiness",
                failure_reason=RuntimeAuditFailureReason.READINESS_PROBE_ERROR,
            ))
    elif readiness.status == RuntimeAuditProbeStatus.DEGRADED:
        findings.append(RuntimeAuditFinding(
            finding_type=RuntimeAuditFindingType.DEGRADATION,
            severity=RuntimeAuditSeverity.WARNING,
            message="Readiness probe reports degraded state",
            source="readiness",
        ))

    dry_run_finding = check_dry_run_posture(readiness)
    if dry_run_finding is not None:
        findings.append(dry_run_finding)

    metrics_probe, _ = await probe_metrics(http_client, base_url, timeout=http_timeout)
    if metrics_probe.forbidden_labels_found:
        findings.append(RuntimeAuditFinding(
            finding_type=RuntimeAuditFindingType.SAFETY_GATE,
            severity=RuntimeAuditSeverity.CRITICAL,
            message=f"Forbidden metric labels: {len(metrics_probe.forbidden_labels_found)}",
            source="metrics",
            failure_reason=RuntimeAuditFailureReason.FORBIDDEN_METRIC_LABEL,
        ))
    elif metrics_probe.status != RuntimeAuditProbeStatus.SUCCESS:
        if not metrics_probe.reachable:
            findings.append(RuntimeAuditFinding(
                finding_type=RuntimeAuditFindingType.PROBE_ERROR,
                severity=RuntimeAuditSeverity.ERROR,
                message=f"Metrics probe unreachable: {metrics_probe.message}",
                source="metrics",
                failure_reason=RuntimeAuditFailureReason.METRICS_PROBE_ERROR,
            ))
        else:
            findings.append(RuntimeAuditFinding(
                finding_type=RuntimeAuditFindingType.PROBE_ERROR,
                severity=RuntimeAuditSeverity.ERROR,
                message=f"Metrics probe error: {metrics_probe.message or metrics_probe.parse_error}",
                source="metrics",
                failure_reason=RuntimeAuditFailureReason.METRICS_PROBE_ERROR,
            ))

    db_probe = await probe_database(db_path)

    ledger_summary: Optional[RuntimeAuditLedgerSummary] = None
    decision_summary: Optional[RuntimeAuditDecisionSummary] = None
    market_summary: Optional[RuntimeAuditMarketSummary] = None
    position_summary: Optional[RuntimeAuditPositionSummary] = None
    execution_summary: Optional[RuntimeAuditExecutionSummary] = None

    if session_factory is not None:
        ledger_summary = await summarize_ledger(session_factory)
        decision_summary = await summarize_decision_repository(session_factory)
        market_summary = await summarize_market_repository(session_factory)
        position_summary = await summarize_position_repository(session_factory)
        execution_summary = await summarize_execution_repository(session_factory)

    docker_probe: Optional[RuntimeAuditDockerProbe] = None
    if not skip_docker:
        docker_probe = await probe_docker()

    log_tail: Optional[RuntimeAuditLogTailSummary] = None
    if not skip_log_tail:
        log_tail = await probe_log_tail()

    safety_gate_findings = [
        f for f in findings if f.finding_type == RuntimeAuditFindingType.SAFETY_GATE
    ]
    probe_error_findings = [
        f for f in findings if f.finding_type == RuntimeAuditFindingType.PROBE_ERROR
        and f.severity == RuntimeAuditSeverity.ERROR
    ]
    warning_findings = [
        f for f in findings
        if f.finding_type not in (RuntimeAuditFindingType.SAFETY_GATE,)
        and f.severity in (RuntimeAuditSeverity.WARNING, RuntimeAuditSeverity.INFO)
    ]

    if safety_gate_findings:
        status = RuntimeAuditStatus.SAFETY_GATE_FAILED
        exit_code = RuntimeAuditExitCode.SAFETY_GATE_FAILED
    elif probe_error_findings:
        status = RuntimeAuditStatus.PROBE_ERROR
        exit_code = RuntimeAuditExitCode.PROBE_ERROR
    elif any(f.severity in (RuntimeAuditSeverity.ERROR, RuntimeAuditSeverity.WARNING) for f in findings):
        status = RuntimeAuditStatus.DEGRADED
        exit_code = RuntimeAuditExitCode.DEGRADED
    else:
        status = RuntimeAuditStatus.HEALTHY
        exit_code = RuntimeAuditExitCode.HEALTHY

    cooldown_block_rate = _compute_cooldown_block_rate(
        ledger_summary, decision_summary
    )

    report = RuntimeAuditReport(
        status=status, exit_code=exit_code, generated_at_utc=now,
        findings=findings, health_probe=health, readiness_probe=readiness,
        metrics_probe=metrics_probe, ledger_summary=ledger_summary,
        decision_summary=decision_summary, market_summary=market_summary,
        position_summary=position_summary, execution_summary=execution_summary,
        database_probe=db_probe, docker_probe=docker_probe,
        log_tail_summary=log_tail,
        cognitive_cooldown_block_rate=cooldown_block_rate,
    )

    artifact_result = write_audit_artifacts(report, output_dir, project_root)
    
    # Re-derive status/exit_code if artifact write failed
    if not artifact_result.success:
        status = RuntimeAuditStatus.PROBE_ERROR
        exit_code = RuntimeAuditExitCode.PROBE_ERROR
        findings.append(RuntimeAuditFinding(
            finding_type=RuntimeAuditFindingType.PROBE_ERROR,
            severity=RuntimeAuditSeverity.ERROR,
            message=f"Artifact write failed: {artifact_result.failure_reason}",
            source="artifact",
            failure_reason=RuntimeAuditFailureReason.ARTIFACT_WRITE_ERROR,
        ))
        report = RuntimeAuditReport(
            status=status, exit_code=exit_code, generated_at_utc=now,
            findings=findings, health_probe=health, readiness_probe=readiness,
            metrics_probe=metrics_probe, ledger_summary=ledger_summary,
            decision_summary=decision_summary, market_summary=market_summary,
            position_summary=position_summary, execution_summary=execution_summary,
            database_probe=db_probe, docker_probe=docker_probe,
            log_tail_summary=log_tail,
            cognitive_cooldown_block_rate=cooldown_block_rate,
        )

    report = report.model_copy(update={"artifact_result": artifact_result})

    telegram_result = await send_telegram_alert(notifier, report, enabled=telegram_enabled)
    report = report.model_copy(update={"telegram_result": telegram_result})

    return report


async def run_llm_review(
    request: RuntimeAuditLLMReviewRequest,
    api_key: Optional[str] = None,
    output_dir: Optional[Path] = None,
    project_root: Optional[Path] = None,
    enabled: bool = False,
) -> RuntimeAuditLLMReviewResult:
    """Optional advisory LLM reviewer using direct httpx to Moonshot/Kimi."""
    if not enabled:
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.DISABLED,
        )
    if not api_key:
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.CONFIG_ERROR,
            failure_reason="MOONSHOT_API_KEY not configured",
        )

    if project_root is None:
        project_root = Path.cwd()
    if output_dir is None:
        output_dir = project_root / "docs" / "operations" / _REVIEW_DIR_NAME

    artifact_path = project_root / request.audit_artifact_path
    try:
        _validate_output_path(artifact_path, project_root)
    except ValueError as exc:
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.CONFIG_ERROR,
            failure_reason=f"Artifact path outside project: {exc}",
        )
    
    # Constrain to docs/operations/runtime_audits/ directory
    allowed_dir = project_root / "docs" / "operations" / _ARTIFACT_DIR_NAME
    try:
        artifact_path.resolve().relative_to(allowed_dir.resolve())
    except ValueError:
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.CONFIG_ERROR,
            failure_reason=f"Artifact path must be under docs/operations/{_ARTIFACT_DIR_NAME}/",
        )
    
    if not artifact_path.exists():
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.CONFIG_ERROR,
            failure_reason=f"Audit artifact not found: {request.audit_artifact_path}",
        )

    try:
        artifact_text = artifact_path.read_text(encoding="utf-8")
    except Exception as exc:
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.CONFIG_ERROR,
            failure_reason=f"Cannot read artifact: {type(exc).__name__}",
        )

    # Pre-send scan: reject artifact if it contains forbidden content
    artifact_check = check_forbidden_content(artifact_text)
    if not artifact_check.clean:
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.FORBIDDEN_CONTENT,
            failure_reason=f"Artifact contains forbidden content: {artifact_check.forbidden_patterns_found[:3]}",
            forbidden_content_check=artifact_check,
        )

    if len(artifact_text) > 32000:
        artifact_text = artifact_text[:32000]

    payload = {
        "model": request.model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an advisory runtime reviewer. Analyze the audit "
                    "artifact and provide observations about safety posture. "
                    "Do not include secrets, API keys, wallet addresses, or "
                    "high-cardinality identifiers in your response."
                ),
            },
            {"role": "user", "content": artifact_text},
        ],
        "max_tokens": 2048,
    }

    timeout_sec = float(request.timeout_seconds)
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.moonshot.ai/v1/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=timeout_sec,
            )
            resp.raise_for_status()
            body = resp.json()
    except httpx.TimeoutException:
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.TIMEOUT,
            model_used=request.model,
            failure_reason=f"Timeout after {timeout_sec}s",
        )
    except Exception as exc:
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.HTTP_ERROR,
            model_used=request.model,
            failure_reason=f"HTTP error: {type(exc).__name__}",
        )

    choices = body.get("choices", [])
    if not choices:
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.HTTP_ERROR,
            model_used=request.model,
            failure_reason="No choices in response",
        )

    review_text = choices[0].get("message", {}).get("content", "")
    forbidden = check_forbidden_content(review_text)
    if not forbidden.clean:
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.FORBIDDEN_CONTENT,
            model_used=request.model,
            failure_reason=f"Forbidden content in review: {forbidden.forbidden_patterns_found[:3]}",
            forbidden_content_check=forbidden,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime(_TIMESTAMP_FMT)
    review_name = f"runtime-review-{ts}.md"
    review_path = output_dir / review_name

    try:
        _validate_output_path(review_path, project_root)
        review_path.write_text(
            f"# Runtime Review (Advisory)\n\n"
            f"- Model: {request.model}\n"
            f"- Artifact: {request.audit_artifact_path}\n\n"
            f"{review_text}\n",
            encoding="utf-8",
        )
    except Exception as exc:
        return RuntimeAuditLLMReviewResult(
            status=RuntimeAuditLLMReviewStatus.HTTP_ERROR,
            model_used=request.model,
            failure_reason=f"Write error: {type(exc).__name__}",
        )

    return RuntimeAuditLLMReviewResult(
        status=RuntimeAuditLLMReviewStatus.SUCCESS,
        review_path=str(review_path.relative_to(project_root)),
        model_used=request.model,
    )
