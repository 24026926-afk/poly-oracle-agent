#!/usr/bin/env python3
"""
collect_soak_evidence.py — Paper-trading soak-test evidence collector.

Collects health, readiness, metrics, service status, database persistence,
Telegram status, and restart/recovery evidence from the deployed dry-run
runtime. Produces secret-free markdown and JSON reports under
docs/operations/ and exits non-zero on mandatory gate failures.

Usage:
    python3 scripts/ops/collect_soak_evidence.py \\
        --soak-start "2026-05-04T00:00:00Z" \\
        [--target-host localhost] \\
        [--db-path /data/poly_oracle.db] \\
        --db-baseline-size <bytes-at-soak-start> \\
        [--telegram-enabled] \\
        [--recovery-tested "docker compose restart"]

Requirements: Python 3.12+ with project dependencies installed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402

from src.schemas.soak import SoakEvidenceReport  # noqa: E402

# ── Constants ──────────────────────────────────────────────────────────────

_HTTP_TIMEOUT_SEC: float = 5.0
_SUBPROCESS_TIMEOUT_SEC: float = 15.0
_SQLITE_TIMEOUT_SEC: float = 5.0
_MIN_SOAK_HOURS: float = 24.0
_OUTPUT_DIR: Path = Path("docs/operations")

# Status strings (stdlib enums)
_PASS = "pass"
_FAIL = "fail"
_SKIPPED = "skipped"
_INCOMPLETE = "incomplete"
_NOT_APPLICABLE = "not_applicable"

# Verdict strings
_VERDICT_PASS = "pass"
_VERDICT_FAIL = "fail"
_VERDICT_INCOMPLETE = "incomplete"

# Secrets redaction patterns
_REDACT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(r"(?:api[_-]?key|apikey)\s*[=:]\s*\S+", re.IGNORECASE)),
    (
        "private_key",
        re.compile(r"(?:private[_-]?key|wallet[_-]?key)\s*[=:]\s*\S+", re.IGNORECASE),
    ),
    ("telegram_token", re.compile(r"\b\d{8,10}:[a-zA-Z0-9_-]{35,}\b")),
    ("rpc_url_credentials", re.compile(r"https?://[^:@]+:[^@]+@")),
    ("token_id", re.compile(r"\b\d{10,}\b")),
    ("condition_id", re.compile(r"0x[a-fA-F0-9]{64}\b")),
    (
        "prompt_block",
        re.compile(
            r"(?:prompt_text|reasoning|system_prompt)\s*[=:]\s*.+", re.IGNORECASE
        ),
    ),
    (
        "private_ip",
        re.compile(
            r"\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)\.\d{1,3}\.\d{1,3}\b"
        ),
    ),
]

# Fields to scrub before writing reports
_REDACT_FIELD_NAMES: set[str] = {
    "api_key",
    "apikey",
    "api_secret",
    "secret",
    "password",
    "token",
    "private_key",
    "wallet_key",
    "wallet_private_key",
    "mnemonic",
    "passphrase",
    "chat_id",
    "bot_token",
}


def _redact_text(text: str) -> str:
    """Replace secret-like substrings with [REDACTED:category]."""
    for category, pattern in _REDACT_PATTERNS:
        text = pattern.sub(f"[REDACTED:{category}]", text)
    return text


def _redact_dict(obj: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact secret-keyed values in a dict."""
    result: dict[str, Any] = {}
    for k, v in obj.items():
        if k.lower() in _REDACT_FIELD_NAMES:
            result[k] = "[REDACTED]"
        elif isinstance(v, dict):
            result[k] = _redact_dict(v)
        elif isinstance(v, list):
            result[k] = [
                _redact_dict(item)
                if isinstance(item, dict)
                else _redact_text(item)
                if isinstance(item, str)
                else item
                for item in v
            ]
        elif isinstance(v, str):
            result[k] = _redact_text(v)
        else:
            result[k] = v
    return result


# ── Helpers ────────────────────────────────────────────────────────────────


def _log(msg: str) -> None:
    """Log to stderr so stdout remains clean."""
    print(f"[collect_soak_evidence] {msg}", file=sys.stderr)


def _make_probe(
    probe_name: str,
    status: str,
    *,
    detail: str = "",
    failure_reason: str | None = None,
) -> dict[str, Any]:
    """Build a probe result dict."""
    result: dict[str, Any] = {
        "probe_name": probe_name,
        "status": status,
        "detail": detail,
    }
    if failure_reason:
        result["failure_reason"] = failure_reason
    return result


def _http_get(
    url: str, *, timeout: float = _HTTP_TIMEOUT_SEC
) -> tuple[int, str, dict[str, str]]:
    """HTTP GET returning (status_code, body_text, headers_dict)."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
            headers = {k.lower(): v for k, v in dict(resp.headers).items()}
            return status, body, headers
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        headers = {k.lower(): v for k, v in dict(e.headers).items()}
        return e.code, body, headers


def _parse_iso_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp string to a UTC datetime."""
    value = value.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _normalize_env_value(raw: str) -> str:
    """Normalize .env value: strip quotes, inline comments, whitespace."""
    v = raw.strip()
    if " #" in v:
        v = v.split(" #", 1)[0].strip()
    return v.strip("\"'").lower()


# ── Probe: Dry-Run Gate (local .env + runtime /readyz) ─────────────────────


def _probe_dry_run(target_host: str) -> tuple[bool, dict[str, Any]]:
    """Check .env DRY_RUN=true AND runtime dry-run status from /readyz.

    Returns (dry_run_ok, probe). The probe combines both checks:
      - .env must contain DRY_RUN=true (static config)
      - /readyz must report dry_run=true (runtime state)
    """
    reasons: list[str] = []

    # Check 1: .env static config
    env_path = Path(".env")
    env_dry_run_ok = False
    if not env_path.exists():
        reasons.append(".env file not found")
    else:
        try:
            content = env_path.read_text()
        except OSError:
            reasons.append("Cannot read .env")
        else:
            m = re.search(
                r"^DRY_RUN\s*=\s*(.+)$", content, re.MULTILINE | re.IGNORECASE
            )
            if not m:
                reasons.append("DRY_RUN key not found in .env")
            else:
                value = _normalize_env_value(m.group(1))
                if value in ("true", "1", "yes"):
                    env_dry_run_ok = True
                else:
                    reasons.append("DRY_RUN is not true in .env")

    # Check 2: Runtime dry-run from /readyz. Static .env proof is not enough
    # for an audit-grade soak because the running process can diverge.
    runtime_dry_run_ok = False
    try:
        _, body, _ = _http_get(f"http://{target_host}:8080/readyz")
        data = json.loads(body)
        dry_run_value = data.get("dry_run")
        if dry_run_value is True:
            runtime_dry_run_ok = True
        elif dry_run_value is False:
            reasons.append("Runtime /readyz reports dry_run=false")
        else:
            reasons.append("Runtime /readyz does not confirm dry_run=true")
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        reasons.append("Runtime /readyz unavailable or malformed")

    if not env_dry_run_ok:
        return False, _make_probe(
            "dry_run_guard",
            _FAIL,
            detail="; ".join(reasons) if reasons else "DRY_RUN not confirmed",
            failure_reason="dry_run_false"
            if "not true" in str(reasons)
            else "dry_run_missing",
        )

    if not runtime_dry_run_ok:
        runtime_reason = (
            "dry_run_false"
            if any("dry_run=false" in reason for reason in reasons)
            else "runtime_dry_run_unconfirmed"
        )
        return False, _make_probe(
            "dry_run_guard",
            _FAIL,
            detail="; ".join(reasons) if reasons else "Runtime dry_run not confirmed",
            failure_reason=runtime_reason,
        )

    return True, _make_probe(
        "dry_run_guard",
        _PASS,
        detail="DRY_RUN=true confirmed in .env and runtime /readyz",
    )


# ── Probe: Soak Duration ───────────────────────────────────────────────────


def _probe_duration(soak_start: datetime) -> tuple[float, dict[str, Any]]:
    """Check soak duration >= 24h. Returns (duration_hours, probe)."""
    now = datetime.now(timezone.utc)
    duration_hours = (now - soak_start).total_seconds() / 3600.0
    if duration_hours < _MIN_SOAK_HOURS:
        return duration_hours, _make_probe(
            "soak_duration",
            _FAIL,
            detail=f"Soak duration {duration_hours:.1f}h below minimum {_MIN_SOAK_HOURS}h",
            failure_reason="duration_too_short",
        )
    return duration_hours, _make_probe(
        "soak_duration",
        _PASS,
        detail=f"Soak duration: {duration_hours:.1f}h",
    )


# ── Probe: Compose Service Status ──────────────────────────────────────────


def _inspect_restart_count(container_ref: str) -> tuple[int | None, str | None]:
    """Return Docker restart count for a concrete container id/name."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.RestartCount}}", container_ref],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            text=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, "restart_count_unavailable"

    if result.returncode != 0:
        return None, "restart_count_unavailable"

    raw_count = result.stdout.strip()
    if not raw_count.isdigit():
        return None, "restart_count_unavailable"
    return int(raw_count), None


def _probe_compose_service() -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Probe Compose service status. Returns (service_dict, probe)."""
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "orchestrator", "--format", "json"],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return None, _make_probe(
            "compose_service",
            _FAIL,
            detail="docker compose ps timed out",
            failure_reason="timeout",
        )
    except FileNotFoundError:
        return None, _make_probe(
            "compose_service",
            _SKIPPED,
            detail="docker compose not available",
        )

    if result.returncode != 0 or not result.stdout.strip():
        return None, _make_probe(
            "compose_service",
            _FAIL,
            detail="orchestrator service not running",
            failure_reason="service_not_running",
        )

    try:
        services = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None, _make_probe(
            "compose_service",
            _FAIL,
            detail="Could not parse compose ps output",
            failure_reason="parse_error",
        )

    svc = services[0] if isinstance(services, list) else services
    service_info: dict[str, Any] = {
        "service_name": "orchestrator",
        "running": False,
        "restart_count": 0,
    }
    if isinstance(svc, dict):
        state = svc.get("State", "")
        status_str = svc.get("Status", "")
        running = state.lower() == "running"
        service_info["running"] = running
        restart_count: int | None = None
        container_ref = svc.get("ID") or svc.get("Name")
        if container_ref:
            service_info["container_ref"] = container_ref
            restart_count, restart_failure = _inspect_restart_count(str(container_ref))
            if restart_count is None and restart_failure:
                service_info["restart_count_source"] = restart_failure
        if "restarting" in status_str.lower():
            rm = re.search(r"Restarting\s*\((\d+)\)", status_str, re.IGNORECASE)
            if rm:
                restart_count = int(rm.group(1))
                service_info["restart_count_source"] = "compose_status"
        if restart_count is None:
            service_info["restart_count"] = 0
            return service_info, _make_probe(
                "compose_service",
                _FAIL,
                detail="Could not collect Docker restart count",
                failure_reason="restart_count_unavailable",
            )
        service_info["restart_count"] = restart_count
        service_info.setdefault("restart_count_source", "docker_inspect")
        if not running and restart_count == 0:
            service_info["exit_code"] = svc.get("ExitCode")

    probe_status = _PASS if service_info["running"] else _FAIL
    return service_info, _make_probe(
        "compose_service",
        probe_status,
        detail=f"Running={service_info['running']}, RestartCount={service_info['restart_count']}",
    )


# ── Probe: Health & Readiness ──────────────────────────────────────────────


def _probe_health(target_host: str) -> dict[str, Any]:
    """Probe /healthz and /readyz. Returns health evidence dict."""
    evidence: dict[str, Any] = {
        "healthz_reachable": False,
        "healthz_status_code": None,
        "readyz_reachable": False,
        "readyz_status_code": None,
        "readyz_status": None,
        "degraded_reason": None,
    }

    # /healthz
    try:
        status, _, _ = _http_get(f"http://{target_host}:8080/healthz")
        evidence["healthz_reachable"] = status == 200
        evidence["healthz_status_code"] = status
    except urllib.error.URLError:
        evidence["healthz_reachable"] = False

    # /readyz
    try:
        status, body, _ = _http_get(f"http://{target_host}:8080/readyz")
        evidence["readyz_reachable"] = True
        evidence["readyz_status_code"] = status
        try:
            data = json.loads(body)
            raw_status = data.get("status", "UNKNOWN")
            evidence["readyz_status"] = str(raw_status).upper()
            if evidence["readyz_status"] == "DEGRADED":
                checks = data.get("checks", {})
                evidence["degraded_reason"] = _redact_text(
                    json.dumps(checks) if checks else "no checks detail"
                )
        except json.JSONDecodeError:
            evidence["readyz_status"] = "MALFORMED"
    except urllib.error.URLError:
        evidence["readyz_reachable"] = False

    # Determine probe status
    if not evidence["healthz_reachable"]:
        probe_status = _FAIL
        detail = "Health endpoint unreachable"
        failure_reason = "healthz_unreachable"
    elif evidence["readyz_status"] == "NOT_READY":
        probe_status = _FAIL
        detail = "Readiness is NOT_READY"
        failure_reason = "readyz_not_ready"
    elif evidence["readyz_status"] == "DEGRADED":
        detail = f"Readiness degraded: {evidence.get('degraded_reason', '')}"
        probe_status = _FAIL
        failure_reason = "readyz_degraded"
    elif evidence["readyz_status"] == "MALFORMED" or not evidence["readyz_reachable"]:
        probe_status = _FAIL
        detail = "Readiness endpoint unreachable or malformed"
        failure_reason = "readyz_unreachable"
    elif evidence["readyz_status"] == "READY" and evidence["readyz_status_code"] == 200:
        probe_status = _PASS
        detail = "Health and readiness OK"
        failure_reason = None
    else:
        probe_status = _FAIL
        detail = f"Readiness returned unknown status: {evidence['readyz_status']}"
        failure_reason = "readyz_unknown_status"

    evidence["health_probe"] = _make_probe(
        "health",
        probe_status,
        detail=detail,
        failure_reason=failure_reason,
    )
    return evidence


# ── Probe: Metrics ─────────────────────────────────────────────────────────


_PROM_METRIC_LINE_RE = re.compile(
    r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})?\s+[+-]?(\d+(\.\d*)?([eE][+-]?\d+)?|\.\d+([eE][+-]?\d+)?|NaN|\+?Inf|-Inf)"
)


def _is_valid_prometheus(text: str) -> bool:
    if not text or not text.strip():
        return False
    if text.strip().startswith("# Metrics unavailable"):
        return False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("# HELP ") or stripped.startswith("# TYPE "):
            return True
        if _PROM_METRIC_LINE_RE.match(stripped):
            return True
    return False


def _probe_metrics(target_host: str) -> dict[str, Any]:
    """Probe /metrics endpoint. Returns metrics evidence dict."""
    evidence: dict[str, Any] = {
        "metrics_reachable": False,
        "metrics_status_code": None,
        "prometheus_format_valid": False,
    }
    try:
        status, body, headers = _http_get(f"http://{target_host}:8081/metrics")
        evidence["metrics_reachable"] = True
        evidence["metrics_status_code"] = status
        content_type = headers.get("content-type", "")
        if (
            status == 200
            and "text/plain" in content_type
            and _is_valid_prometheus(body)
        ):
            evidence["prometheus_format_valid"] = True
    except urllib.error.URLError:
        evidence["metrics_reachable"] = False

    if not evidence["metrics_reachable"]:
        evidence["metrics_probe"] = _make_probe(
            "metrics",
            _FAIL,
            detail="Metrics endpoint unreachable",
            failure_reason="metrics_unreachable",
        )
    elif not evidence["prometheus_format_valid"]:
        evidence["metrics_probe"] = _make_probe(
            "metrics",
            _FAIL,
            detail="Metrics endpoint not serving valid Prometheus format",
            failure_reason="metrics_invalid",
        )
    else:
        evidence["metrics_probe"] = _make_probe(
            "metrics",
            _PASS,
            detail="Metrics endpoint healthy",
        )
    return evidence


# ── Probe: Database Persistence ────────────────────────────────────────────


def _probe_database(
    db_path: str, soak_start: datetime, baseline_size: int = 0
) -> dict[str, Any]:
    """Probe SQLite file and collect persistence evidence. Read-only.

    Uses baseline_size (bytes at soak start) to compute real growth delta.
    Queries decisions/snapshots created during the soak period only.
    """
    evidence: dict[str, Any] = {
        "db_file_exists": False,
        "db_file_path": db_path,
        "db_file_size_bytes": 0,
        "db_growth_bytes": 0,
        "db_grew": False,
        "recent_decision_count": 0,
        "recent_snapshot_count": 0,
        "sqlite_locked": False,
    }

    path = Path(db_path)
    if not path.exists() or not path.is_file():
        evidence["db_probe"] = _make_probe(
            "database",
            _FAIL,
            detail=f"SQLite file not found: {db_path}",
            failure_reason="sqlite_missing",
        )
        return evidence

    evidence["db_file_exists"] = True
    current_size = path.stat().st_size
    evidence["db_file_size_bytes"] = current_size
    evidence["db_growth_bytes"] = max(0, current_size - baseline_size)
    evidence["db_grew"] = evidence["db_growth_bytes"] > 0

    # Read-only: query decisions and snapshots from the soak period. SQLAlchemy
    # SQLite DateTime values are stored with a space separator, so compare via
    # SQLite datetime() rather than raw lexicographic ISO strings.
    soak_start_sqlite = soak_start.strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = sqlite3.connect(
            f"file:{db_path}?mode=ro", uri=True, timeout=_SQLITE_TIMEOUT_SEC
        )
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Count decisions created during soak period
        try:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM agent_decision_logs "
                "WHERE datetime(evaluated_at) >= datetime(?)",
                (soak_start_sqlite,),
            )
            row = cur.fetchone()
            if row:
                evidence["recent_decision_count"] = row["cnt"]
        except sqlite3.OperationalError:
            # Table may not exist yet in a partially initialized deployment.
            pass

        # Count snapshots created during soak period
        try:
            cur.execute(
                "SELECT COUNT(*) as cnt FROM market_snapshots "
                "WHERE datetime(captured_at) >= datetime(?)",
                (soak_start_sqlite,),
            )
            row = cur.fetchone()
            if row:
                evidence["recent_snapshot_count"] = row["cnt"]
        except sqlite3.OperationalError:
            pass

        conn.close()
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower() or "database is locked" in str(e).lower():
            evidence["sqlite_locked"] = True
            evidence["db_probe"] = _make_probe(
                "database",
                _FAIL,
                detail="SQLite database is locked",
                failure_reason="sqlite_locked",
            )
            return evidence
        evidence["db_probe"] = _make_probe(
            "database",
            _FAIL,
            detail=f"SQLite error: {e}",
            failure_reason="sqlite_error",
        )
        return evidence

    # Determine probe status
    if not evidence["db_grew"]:
        evidence["db_probe"] = _make_probe(
            "database",
            _FAIL,
            detail="SQLite file exists but shows no growth during soak",
            failure_reason="no_persistence_activity",
        )
    elif (
        evidence["recent_decision_count"] == 0
        and evidence["recent_snapshot_count"] == 0
    ):
        evidence["db_probe"] = _make_probe(
            "database",
            _INCOMPLETE,
            detail="DB file exists and grew but no soak-period decisions or snapshots found",
            failure_reason="no_agent_activity",
        )
    else:
        evidence["db_probe"] = _make_probe(
            "database",
            _PASS,
            detail=f"DB OK: {evidence['db_file_size_bytes']} bytes (+{evidence['db_growth_bytes']}), "
            f"{evidence['recent_decision_count']} soak decisions, "
            f"{evidence['recent_snapshot_count']} soak snapshots",
        )
    return evidence


# ── Probe: Telegram Status ─────────────────────────────────────────────────


def _probe_telegram(telegram_enabled: bool) -> dict[str, Any]:
    """Record Telegram alert status."""
    if not telegram_enabled:
        return {
            "enabled": False,
            "status": _NOT_APPLICABLE,
            "detail": "Telegram alerts are disabled",
            "telegram_probe": _make_probe(
                "telegram",
                _NOT_APPLICABLE,
                detail="Telegram is not enabled",
            ),
        }
    env_path = Path(".env")
    token_present = False
    chat_id_present = False
    if env_path.exists():
        try:
            content = env_path.read_text()
            token_present = bool(
                re.search(r"^TELEGRAM_BOT_TOKEN\s*=\s*\S+", content, re.MULTILINE)
            )
            chat_id_present = bool(
                re.search(r"^TELEGRAM_CHAT_ID\s*=\s*\S+", content, re.MULTILINE)
            )
        except OSError:
            pass

    if token_present and chat_id_present:
        return {
            "enabled": True,
            "status": _PASS,
            "detail": "Telegram configuration present (token/chat_id redacted)",
            "telegram_probe": _make_probe(
                "telegram", _PASS, detail="Telegram configured"
            ),
        }
    return {
        "enabled": True,
        "status": _INCOMPLETE,
        "detail": "Telegram enabled but configuration incomplete",
        "telegram_probe": _make_probe(
            "telegram",
            _INCOMPLETE,
            detail="Telegram enabled but token or chat_id missing",
        ),
    }


# ── Probe: Recovery Evidence ───────────────────────────────────────────────


def _probe_recovery(
    recovery_tested: bool,
    recovery_method: str | None,
    *,
    health_evidence: dict[str, Any] | None = None,
    service_info: dict[str, Any] | None = None,
    db_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record host/container restart recovery evidence.

    When recovery was tested, the probe status passes only if post-recovery
    health, service, and DB evidence show the runtime came back.
    When not tested, status is INCOMPLETE.

    Allowed recovery methods: 'docker compose restart', 'host reboot'.
    """
    _ALLOWED_RECOVERY_METHODS = {"docker compose restart", "host reboot"}

    if not recovery_tested:
        return {
            "recovery_tested": False,
            "recovery_method": None,
            "service_recovered": None,
            "recovery_probe": _make_probe(
                "recovery",
                _INCOMPLETE,
                detail="Host reboot or container restart recovery was not tested",
            ),
        }

    if recovery_method and recovery_method not in _ALLOWED_RECOVERY_METHODS:
        return {
            "recovery_tested": True,
            "recovery_method": recovery_method,
            "service_recovered": None,
            "recovery_probe": _make_probe(
                "recovery",
                _FAIL,
                detail=f"Unknown recovery method: {recovery_method}. "
                f"Allowed: {', '.join(sorted(_ALLOWED_RECOVERY_METHODS))}",
                failure_reason="invalid_recovery_method",
            ),
        }

    health_ok = (
        health_evidence is not None
        and health_evidence.get("health_probe", {}).get("status") == _PASS
    )
    service_ok = service_info is not None and service_info.get("running") is True
    db_ok = db_evidence is not None and db_evidence.get("db_file_exists") is True

    if not (health_ok and service_ok and db_ok):
        missing = []
        if not health_ok:
            missing.append("health/readiness not healthy")
        if not service_ok:
            missing.append("compose service not running")
        if not db_ok:
            missing.append("sqlite persistence not present")
        return {
            "recovery_tested": True,
            "recovery_method": recovery_method,
            "service_recovered": False,
            "recovery_probe": _make_probe(
                "recovery",
                _FAIL,
                detail="Recovery not verified: " + "; ".join(missing),
                failure_reason="recovery_not_verified",
            ),
        }

    return {
        "recovery_tested": True,
        "recovery_method": recovery_method,
        "service_recovered": True,
        "recovery_probe": _make_probe(
            "recovery",
            _PASS,
            detail=f"Recovery verified after {recovery_method}",
        ),
    }


# ── Schema Validation ──────────────────────────────────────────────────────


def _validate_report(report: dict[str, Any]) -> dict[str, Any]:
    """Validate report dict against SoakEvidenceReport schema.

    Returns the dict unchanged if valid. Raises ValueError on schema mismatch.
    Schema validation is best-effort via dict shape check when pydantic
    is not available (stdlib-only constraint).
    """
    required_fields = {
        "report_id",
        "target_host",
        "duration_hours",
        "dry_run_confirmed",
        "verdict",
        "verdict_reason",
        "probes",
        "live_trading_authorized",
        "exit_code",
    }
    missing = required_fields - set(report.keys())
    if missing:
        raise ValueError(f"Report missing required fields: {missing}")

    try:
        SoakEvidenceReport.model_validate(report)
    except ValidationError as exc:
        raise ValueError(f"SoakEvidenceReport validation failed: {exc}") from exc

    return report


# ── Report Writing ─────────────────────────────────────────────────────────


def _compute_verdict(report: dict[str, Any]) -> tuple[str, str]:
    """Compute soak verdict from evidence. Returns (verdict, reason)."""
    probes: list[dict[str, Any]] = report["probes"]
    probe_map = {p["probe_name"]: p for p in probes}

    # Mandatory probe names
    mandatory = ["dry_run_guard", "soak_duration", "health", "metrics", "database"]

    for name in mandatory:
        probe = probe_map.get(name)
        if probe is None:
            return _VERDICT_FAIL, f"Missing mandatory probe: {name}"
        if probe.get("status") != _PASS:
            return _VERDICT_FAIL, f"{name} check failed: {probe.get('detail', '')}"

    recovery = next((p for p in probes if p["probe_name"] == "recovery"), None)
    if recovery and recovery["status"] == _INCOMPLETE:
        return _VERDICT_INCOMPLETE, "Recovery testing not completed"

    compose = next((p for p in probes if p["probe_name"] == "compose_service"), None)
    if compose and compose["status"] == _FAIL:
        return (
            _VERDICT_FAIL,
            f"Compose service check failed: {compose.get('detail', '')}",
        )

    return _VERDICT_PASS, "All mandatory evidence gates passed"


def _write_markdown(report: dict[str, Any], path: Path) -> None:
    """Write secret-free markdown soak report."""
    redacted = _redact_dict(report)
    verdict = report.get("verdict", _VERDICT_INCOMPLETE)

    lines = [
        "# Phase 14 Paper-Trading Soak Report",
        "",
        f"**Report ID:** `{redacted['report_id']}`",
        f"**Target Host:** `{redacted['target_host']}`",
        f"**Soak Start:** {redacted.get('soak_start_utc', 'N/A')}",
        f"**Evidence Collected:** {redacted.get('soak_end_utc', 'N/A')}",
        f"**Duration:** {redacted.get('duration_hours', 0):.1f} hours",
        f"**Dry Run Confirmed:** {redacted.get('dry_run_confirmed', False)}",
        f"**Verdict:** **{verdict.upper()}**",
        f"**Verdict Reason:** {redacted.get('verdict_reason', '')}",
        f"**Live Trading Authorized:** **{redacted.get('live_trading_authorized', False)}**",
        "",
        "---",
        "",
        "## ⚠️ Important",
        "",
        "- This soak report is an **audit artifact only**.",
        "- It does **NOT** authorize `DRY_RUN=false`.",
        "- It does **NOT** authorize live trading.",
        "- All secrets, private keys, tokens, and credentials have been redacted.",
        "",
        "---",
        "",
        "## Evidence Summary",
        "",
        "| Probe | Status | Detail |",
        "|---|---|---|",
    ]

    for p in redacted.get("probes", []):
        status = p.get("status", "unknown")
        detail = (p.get("detail", "") or "")[:120]
        lines.append(f"| `{p['probe_name']}` | {status} | {detail} |")

    ss = redacted.get("service_status")
    if ss:
        lines.extend(
            [
                "",
                "## Service Status",
                "",
                f"- **Service:** {ss.get('service_name', 'N/A')}",
                f"- **Running:** {ss.get('running', False)}",
                f"- **Restart Count:** {ss.get('restart_count', 0)}",
            ]
        )

    db = redacted.get("database_evidence")
    if db:
        lines.extend(
            [
                "",
                "## Database Persistence",
                "",
                f"- **File:** `{db.get('db_file_path', 'N/A')}`",
                f"- **Size:** {db.get('db_file_size_bytes', 0)} bytes",
                f"- **Grew:** {db.get('db_grew', False)} (+{db.get('db_growth_bytes', 0)} bytes)",
                f"- **Soak Decisions:** {db.get('recent_decision_count', 0)}",
                f"- **Soak Snapshots:** {db.get('recent_snapshot_count', 0)}",
                f"- **Locked:** {db.get('sqlite_locked', False)}",
            ]
        )

    he = redacted.get("health_evidence")
    if he:
        lines.extend(
            [
                "",
                "## Health & Readiness",
                "",
                f"- **/healthz reachable:** {he.get('healthz_reachable', False)}",
                f"- **/readyz reachable:** {he.get('readyz_reachable', False)}",
                f"- **Readiness status:** {he.get('readyz_status', 'N/A')}",
            ]
        )

    me = redacted.get("metrics_evidence")
    if me:
        lines.extend(
            [
                "",
                "## Metrics",
                "",
                f"- **/metrics reachable:** {me.get('metrics_reachable', False)}",
                f"- **Prometheus format valid:** {me.get('prometheus_format_valid', False)}",
            ]
        )

    re_ev = redacted.get("recovery_evidence")
    if re_ev:
        lines.extend(
            [
                "",
                "## Recovery",
                "",
                f"- **Recovery tested:** {re_ev.get('recovery_tested', False)}",
                f"- **Method:** {re_ev.get('recovery_method', 'N/A')}",
                f"- **Service recovered:** {re_ev.get('service_recovered', 'N/A')}",
            ]
        )

    tg_status = redacted.get("telegram_status", "N/A")
    tg_enabled = redacted.get("telegram_enabled", False)
    lines.extend(
        [
            "",
            "## Telegram Alerts",
            "",
            f"- **Enabled:** {tg_enabled}",
            f"- **Status:** {tg_status}",
        ]
    )

    lines.extend(
        [
            "",
            "---",
            "",
            "*Report generated by `scripts/ops/collect_soak_evidence.py`*",
            "",
        ]
    )

    path.write_text("\n".join(lines) + "\n")


def _write_json(report: dict[str, Any], path: Path) -> None:
    """Write secret-free JSON soak report."""
    redacted = _redact_dict(report)
    serializable = json.loads(json.dumps(redacted, default=str))
    path.write_text(json.dumps(serializable, indent=2) + "\n")


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Collect paper-trading soak-test evidence."
    )
    parser.add_argument(
        "--soak-start",
        required=True,
        help="ISO-8601 soak start timestamp (e.g. 2026-05-04T00:00:00Z)",
    )
    parser.add_argument(
        "--target-host",
        default="127.0.0.1",
        help="Target host for HTTP probes (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--db-path",
        default="/data/poly_oracle.db",
        help="Path to the SQLite database (default: /data/poly_oracle.db)",
    )
    parser.add_argument(
        "--db-baseline-size",
        type=int,
        required=True,
        help="SQLite file size in bytes at soak start (for growth delta calculation)",
    )
    parser.add_argument(
        "--telegram-enabled",
        action="store_true",
        help="Indicate Telegram alerting is enabled",
    )
    parser.add_argument(
        "--recovery-tested",
        default=None,
        help="Recovery method if tested (e.g. 'docker compose restart', 'host reboot')",
    )
    args = parser.parse_args()

    # Parse soak start
    try:
        soak_start = _parse_iso_utc(args.soak_start)
    except (ValueError, OSError) as e:
        _log(f"ERROR: Cannot parse --soak-start: {e}")
        sys.exit(2)

    soak_end = datetime.now(timezone.utc)

    # Collect probes
    probes: list[dict[str, Any]] = []

    # 1. Dry-run gate (MUST pass — checks .env + runtime /readyz)
    dry_run_ok, dry_run_probe = _probe_dry_run(args.target_host)
    probes.append(dry_run_probe)
    if not dry_run_ok:
        _log("FATAL: DRY_RUN is not true — exiting without producing report")
        report = {
            "report_id": soak_end.strftime("soak-%Y%m%dT%H%M%SZ"),
            "target_host": args.target_host,
            "soak_start_utc": soak_start.isoformat(),
            "soak_end_utc": soak_end.isoformat(),
            "duration_hours": (soak_end - soak_start).total_seconds() / 3600.0,
            "dry_run_confirmed": False,
            "verdict": _VERDICT_FAIL,
            "verdict_reason": "DRY_RUN is not true — soak aborted",
            "probes": probes,
            "live_trading_authorized": False,
            "exit_code": 1,
        }
        _write_report(report)
        sys.exit(1)

    # 2. Soak duration
    duration_hours, duration_probe = _probe_duration(soak_start)
    probes.append(duration_probe)

    # 3. Compose service
    service_info, compose_probe = _probe_compose_service()
    probes.append(compose_probe)

    # 4. Health & readiness
    health_evidence = _probe_health(args.target_host)
    probes.append(health_evidence["health_probe"])

    # 5. Metrics
    metrics_evidence = _probe_metrics(args.target_host)
    probes.append(metrics_evidence["metrics_probe"])

    # 6. Database (with baseline for real growth delta)
    db_evidence = _probe_database(args.db_path, soak_start, args.db_baseline_size)
    probes.append(db_evidence["db_probe"])

    # 7. Telegram
    telegram_evidence = _probe_telegram(args.telegram_enabled)
    probes.append(telegram_evidence["telegram_probe"])

    # 8. Recovery
    recovery_evidence = _probe_recovery(
        args.recovery_tested is not None,
        args.recovery_tested,
        health_evidence=health_evidence,
        service_info=service_info,
        db_evidence=db_evidence,
    )
    probes.append(recovery_evidence["recovery_probe"])

    # Build report
    report: dict[str, Any] = {
        "report_id": soak_end.strftime("soak-%Y%m%dT%H%M%SZ"),
        "target_host": args.target_host,
        "soak_start_utc": soak_start.isoformat(),
        "soak_end_utc": soak_end.isoformat(),
        "duration_hours": duration_hours,
        "dry_run_confirmed": dry_run_ok,
        "service_status": service_info,
        "health_evidence": health_evidence,
        "metrics_evidence": metrics_evidence,
        "database_evidence": db_evidence,
        "recovery_evidence": recovery_evidence,
        "telegram_enabled": telegram_evidence.get("enabled", False),
        "telegram_status": telegram_evidence.get("status", _NOT_APPLICABLE),
        "probes": probes,
        "live_trading_authorized": False,
    }

    verdict, verdict_reason = _compute_verdict(report)
    report["verdict"] = verdict
    report["verdict_reason"] = verdict_reason
    exit_code = 0 if verdict == _VERDICT_PASS else 1
    report["exit_code"] = exit_code

    # Validate report shape before writing
    try:
        _validate_report(report)
    except ValueError as e:
        _log(f"FATAL: Report validation failed: {e}")
        sys.exit(1)

    _write_report(report)
    sys.exit(exit_code)


def _resolve_output_dir() -> Path:
    """Return the project-root docs/operations directory and nothing else."""
    output_dir = (_PROJECT_ROOT / _OUTPUT_DIR).resolve()
    expected = (_PROJECT_ROOT / "docs" / "operations").resolve()
    if output_dir != expected:
        raise ValueError("output directory must be project docs/operations")
    return output_dir


def _write_report(report: dict[str, Any]) -> None:
    """Write markdown and JSON reports to project docs/operations/."""
    output_dir = _resolve_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)

    md_path = output_dir / "phase14-soak-report.md"
    json_path = output_dir / "phase14-soak-report.json"

    _write_markdown(report, md_path)
    _write_json(report, json_path)

    _log(f"Markdown report: {md_path}")
    _log(f"JSON report:     {json_path}")
    _log(
        f"Verdict: {report.get('verdict', 'N/A')} — {report.get('verdict_reason', '')}"
    )


if __name__ == "__main__":
    main()
