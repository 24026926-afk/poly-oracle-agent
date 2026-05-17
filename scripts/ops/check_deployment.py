#!/usr/bin/env python3
"""
check_deployment.py — DigitalOcean Droplet deployment validation (stdlib-only).

Validates Compose service status, dry-run guard, /healthz, /readyz,
/metrics, and secret-free metrics labels.  Produces a JSON report to
stdout and exits non-zero on mandatory failures.

Usage:
    python3 scripts/ops/check_deployment.py [--allow-degraded]

Requirements: Python 3.12+, Docker Engine, Docker Compose plugin.
Zero third-party dependencies — uses only the Python standard library.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ── Constants ──────────────────────────────────────────────────────────────

_HTTP_TIMEOUT_SEC: float = 5.0
_SUBPROCESS_TIMEOUT_SEC: float = 15.0
# Labels that MUST NOT appear in /metrics output
_FORBIDDEN_LABEL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"(api[_-]?key|apikey|secret|private[_-]?key|wallet[_-]?key)", re.IGNORECASE
    ),
    re.compile(r"(token|chat[_-]?id|bot[_-]?token)", re.IGNORECASE),
    re.compile(r"0x[a-fA-F0-9]{40,}"),  # Ethereum addresses
    re.compile(r"\b[a-fA-F0-9]{64}\b"),  # private key hex
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),  # Anthropic API key pattern
    re.compile(r"xai-[a-zA-Z0-9]{20,}"),  # Grok API key pattern
]

# Enums as frozen string sets
_CHECK_PASS = "pass"
_CHECK_FAIL = "fail"
_CHECK_SKIPPED = "skipped"


# ── Logging (stdlib print to stderr) ───────────────────────────────────────


def _log(msg: str) -> None:
    """Log a message to stderr so stdout remains pure JSON."""
    print(f"[check_deployment] {msg}", file=sys.stderr)


# ── Main ───────────────────────────────────────────────────────────────────


def main() -> None:
    """Run all deployment checks and exit with the report exit code."""
    allow_degraded = "--allow-degraded" in sys.argv

    probes: list[dict[str, Any]] = []

    # 1. Docker / Compose availability
    probes.append(_check_docker_installed())
    probes.append(_check_compose_plugin())

    # 2. Compose service status
    probes.append(_check_compose_service())

    # 3. Dry-run guard
    probes.append(_check_dry_run_guard())

    # 4. HTTP probes
    probes.append(_probe_healthz())
    probes.append(_probe_readyz(allow_degraded=allow_degraded))
    probes.append(_probe_metrics())

    # 5. Metrics content inspection
    probes.append(_inspect_metrics_labels())

    # Determine overall status
    failures = [p for p in probes if p.get("status") == _CHECK_FAIL]
    overall = _CHECK_PASS if not failures else _CHECK_FAIL
    dry_run_verified = any(
        p.get("probe_name") == "dry_run_guard" and p.get("status") == _CHECK_PASS
        for p in probes
    )
    exit_code = 1 if failures else 0

    from datetime import datetime, timezone

    report_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report: dict[str, Any] = {
        "report_id": report_id,
        "overall_status": overall,
        "probes": probes,
        "exit_code": exit_code,
        "dry_run_verified": dry_run_verified,
    }

    # Emit JSON report to stdout
    print(json.dumps(report, indent=2))

    sys.exit(exit_code)


# ── Probe Helpers ──────────────────────────────────────────────────────────


def _make_probe(
    probe_name: str,
    status: str,
    *,
    failure_reason: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Build a deployment probe result dict."""
    result: dict[str, Any] = {
        "probe_name": probe_name,
        "status": status,
        "failure_reason": failure_reason,
        "detail": detail,
    }
    # Strip None values for clean output
    return {k: v for k, v in result.items() if v is not None}


# ── Docker / Compose Checks ────────────────────────────────────────────────


def _check_docker_installed() -> dict[str, Any]:
    try:
        subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            check=True,
        )
        return _make_probe("docker_installed", _CHECK_PASS)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _make_probe(
            "docker_installed",
            _CHECK_FAIL,
            failure_reason="docker_not_installed",
            detail="docker --version failed or timed out",
        )
    except subprocess.CalledProcessError:
        return _make_probe(
            "docker_installed",
            _CHECK_FAIL,
            failure_reason="docker_not_installed",
            detail="docker --version returned non-zero",
        )


def _check_compose_plugin() -> dict[str, Any]:
    try:
        subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            check=True,
        )
        return _make_probe("compose_plugin", _CHECK_PASS)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return _make_probe(
            "compose_plugin",
            _CHECK_FAIL,
            failure_reason="compose_plugin_not_installed",
            detail="docker compose version failed or timed out",
        )
    except subprocess.CalledProcessError:
        return _make_probe(
            "compose_plugin",
            _CHECK_FAIL,
            failure_reason="compose_plugin_not_installed",
            detail="docker compose version returned non-zero",
        )


# ── Compose Service Status ─────────────────────────────────────────────────


def _check_compose_service() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "orchestrator", "--format", "json"],
            capture_output=True,
            timeout=_SUBPROCESS_TIMEOUT_SEC,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return _make_probe(
            "compose_service",
            _CHECK_FAIL,
            failure_reason="timeout",
            detail="docker compose ps timed out",
        )
    except FileNotFoundError:
        return _make_probe(
            "compose_service",
            _CHECK_FAIL,
            failure_reason="compose_plugin_not_installed",
            detail="docker compose not found",
        )

    if result.returncode != 0:
        return _make_probe(
            "compose_service",
            _CHECK_FAIL,
            failure_reason="service_not_running",
            detail=f"docker compose ps failed: {result.stderr.strip()}",
        )

    try:
        services = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        services = []

    if not services:
        return _make_probe(
            "compose_service",
            _CHECK_FAIL,
            failure_reason="service_not_running",
            detail="orchestrator service not found in compose ps output",
        )

    svc = services[0] if isinstance(services, list) else services
    if isinstance(svc, dict):
        state = svc.get("State", "")
        status_str = svc.get("Status", "")
        running = state.lower() == "running"
        restart_count = 0

        if "restarting" in status_str.lower():
            running = False
            restart_match = re.search(
                r"Restarting\s*\((\d+)\)", status_str, re.IGNORECASE
            )
            if restart_match:
                restart_count = int(restart_match.group(1))

        if not running and restart_count > 0:
            return _make_probe(
                "compose_service",
                _CHECK_FAIL,
                failure_reason="container_restarting",
                detail=f"Container restarting (count={restart_count})",
            )

        if not running:
            return _make_probe(
                "compose_service",
                _CHECK_FAIL,
                failure_reason="service_not_running",
                detail=f"Container state: {state}",
            )

        return _make_probe("compose_service", _CHECK_PASS)

    return _make_probe(
        "compose_service",
        _CHECK_FAIL,
        failure_reason="service_not_running",
        detail="Unexpected compose ps output format",
    )


# ── Dry-Run Guard ──────────────────────────────────────────────────────────


def _check_dry_run_guard() -> dict[str, Any]:
    """Check that .env exists and contains DRY_RUN=true."""
    env_path = Path(".env")
    compose_path = Path("docker-compose.yml")

    if not env_path.exists() or not compose_path.exists():
        return _make_probe(
            "dry_run_guard",
            _CHECK_FAIL,
            failure_reason="env_file_absent",
            detail=".env or docker-compose.yml not found in working directory",
        )

    try:
        env_content = env_path.read_text()
    except OSError:
        return _make_probe(
            "dry_run_guard",
            _CHECK_FAIL,
            failure_reason="env_file_absent",
            detail="Could not read .env file",
        )

    dry_run_match = re.search(
        r"^DRY_RUN\s*=\s*(.+)$", env_content, re.MULTILINE | re.IGNORECASE
    )

    if not dry_run_match:
        return _make_probe(
            "dry_run_guard",
            _CHECK_FAIL,
            failure_reason="dry_run_missing",
            detail="DRY_RUN key not found in .env",
        )

    raw_value = _normalize_env_value(dry_run_match.group(1))

    if raw_value in ("true", "1", "yes"):
        return _make_probe("dry_run_guard", _CHECK_PASS)

    return _make_probe(
        "dry_run_guard",
        _CHECK_FAIL,
        failure_reason="dry_run_false",
        detail="DRY_RUN is not 'true' (value redacted)",
    )


def _normalize_env_value(raw_value: str) -> str:
    """Normalize a .env value without exposing the original secret material."""
    value = raw_value.strip()
    if " #" in value:
        value = value.split(" #", 1)[0].strip()
    return value.strip("\"'").lower()


# ── HTTP Probes (stdlib urllib) ────────────────────────────────────────────


def _http_get(
    url: str, *, timeout: float = _HTTP_TIMEOUT_SEC
) -> tuple[int, str, dict[str, str]]:
    """Perform an HTTP GET and return (status_code, body_text, headers_dict).

    Raises urllib.error.URLError on connection/timeout failures.
    """
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


def _probe_healthz() -> dict[str, Any]:
    url = "http://127.0.0.1:8080/healthz"
    try:
        status, _, _ = _http_get(url)
        if status == 200:
            return _make_probe("healthz_probe", _CHECK_PASS)
        return _make_probe(
            "healthz_probe",
            _CHECK_FAIL,
            failure_reason="healthz_unreachable",
            detail=f"HTTP {status}",
        )
    except urllib.error.URLError as e:
        return _make_probe(
            "healthz_probe",
            _CHECK_FAIL,
            failure_reason="healthz_unreachable",
            detail=f"Connection failed: {e.reason}",
        )


def _probe_readyz(*, allow_degraded: bool = False) -> dict[str, Any]:
    url = "http://127.0.0.1:8080/readyz"
    try:
        status, body_text, _ = _http_get(url)
        if status != 200:
            return _make_probe(
                "readyz_probe",
                _CHECK_FAIL,
                failure_reason="readyz_unreachable",
                detail=f"HTTP {status}",
            )

        # Validate JSON body
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError:
            return _make_probe(
                "readyz_probe",
                _CHECK_FAIL,
                failure_reason="readyz_malformed",
                detail="Response is not valid JSON",
            )

        status_value = body.get("status")
        if status_value not in ("ready", "degraded", "not_ready"):
            return _make_probe(
                "readyz_probe",
                _CHECK_FAIL,
                failure_reason="readyz_malformed",
                detail=f"Unknown status: {status_value}",
            )

        # not_ready always fails — service is unhealthy
        if status_value == "not_ready":
            checks_detail = body.get("checks", {})
            return _make_probe(
                "readyz_probe",
                _CHECK_FAIL,
                failure_reason="readyz_unreachable",
                detail=f"Service not ready — checks: {checks_detail}",
            )

        # Degraded is only acceptable when explicitly opted in
        if status_value == "degraded" and not allow_degraded:
            checks_detail = body.get("checks", {})
            return _make_probe(
                "readyz_probe",
                _CHECK_FAIL,
                failure_reason="readyz_malformed",
                detail=f"Readiness degraded without --allow-degraded flag: {checks_detail}",
            )

        # Degraded with --allow-degraded: must include typed checks detail
        if status_value == "degraded" and allow_degraded:
            checks_detail = body.get("checks", {})
            if not checks_detail or not isinstance(checks_detail, dict):
                return _make_probe(
                    "readyz_probe",
                    _CHECK_FAIL,
                    failure_reason="readyz_malformed",
                    detail="Degraded readiness accepted but checks payload missing",
                )
            # Verify checks dict has expected keys
            if "database" not in checks_detail and "websocket" not in checks_detail:
                return _make_probe(
                    "readyz_probe",
                    _CHECK_FAIL,
                    failure_reason="readyz_malformed",
                    detail="Degraded readiness accepted but checks missing expected keys (database/websocket)",
                )

        return _make_probe("readyz_probe", _CHECK_PASS)
    except urllib.error.URLError as e:
        return _make_probe(
            "readyz_probe",
            _CHECK_FAIL,
            failure_reason="readyz_unreachable",
            detail=f"Connection failed: {e.reason}",
        )


# Prometheus metric line pattern: metric_name{labels} numeric_value [timestamp]
# Numeric value: integer, float, +Inf, -Inf, NaN
_PROM_METRIC_LINE_RE = re.compile(
    r"^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[^}]*\})?\s+[+-]?(\d+(\.\d*)?([eE][+-]?\d+)?|\.\d+([eE][+-]?\d+)?|NaN|\+?Inf|-Inf)"
)


def _is_valid_prometheus_text(text: str) -> bool:
    """Check that text resembles Prometheus exposition format.

    Returns True if the text contains at least one HELP line, TYPE line,
    or metric line matching the Prometheus format.
    """
    if not text or not text.strip():
        return False
    # "Metrics unavailable" error marker from metrics_server.py
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


def _probe_metrics() -> dict[str, Any]:
    url = "http://127.0.0.1:8081/metrics"
    try:
        status, body_text, headers = _http_get(url)
        if status != 200:
            return _make_probe(
                "metrics_probe",
                _CHECK_FAIL,
                failure_reason="metrics_unreachable",
                detail=f"HTTP {status}",
            )

        content_type = headers.get("content-type", "")
        if "text/plain" not in content_type:
            return _make_probe(
                "metrics_probe",
                _CHECK_FAIL,
                failure_reason="metrics_unreachable",
                detail=f"Expected text/plain Content-Type, got: {content_type}",
            )

        # Validate Prometheus text format: must contain at least one metric line
        # (HELP, TYPE, or a metric name followed by value)
        if not _is_valid_prometheus_text(body_text):
            return _make_probe(
                "metrics_probe",
                _CHECK_FAIL,
                failure_reason="metrics_unreachable",
                detail="Response is not valid Prometheus text exposition format",
            )

        return _make_probe("metrics_probe", _CHECK_PASS)
    except urllib.error.URLError as e:
        return _make_probe(
            "metrics_probe",
            _CHECK_FAIL,
            failure_reason="metrics_unreachable",
            detail=f"Connection failed: {e.reason}",
        )


# ── Metrics Inspection ─────────────────────────────────────────────────────


def _inspect_metrics_labels() -> dict[str, Any]:
    url = "http://127.0.0.1:8081/metrics"
    try:
        status, text, headers = _http_get(url)
        if status != 200:
            return _make_probe(
                "metrics_inspection",
                _CHECK_FAIL,
                failure_reason="metrics_unreachable",
                detail=f"HTTP {status}",
            )

        content_type = headers.get("content-type", "")
        if "text/plain" not in content_type:
            return _make_probe(
                "metrics_inspection",
                _CHECK_FAIL,
                failure_reason="metrics_unreachable",
                detail=f"Expected text/plain Content-Type, got: {content_type}",
            )

        if not _is_valid_prometheus_text(text):
            return _make_probe(
                "metrics_inspection",
                _CHECK_FAIL,
                failure_reason="metrics_unreachable",
                detail="Response is not valid Prometheus text exposition format",
            )

        forbidden: list[str] = []
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            for pattern in _FORBIDDEN_LABEL_PATTERNS:
                if pattern.search(line):
                    forbidden.append(pattern.pattern)
                    break

        if forbidden:
            return _make_probe(
                "metrics_inspection",
                _CHECK_FAIL,
                failure_reason="metrics_forbidden_label",
                detail=f"Forbidden label patterns detected: {list(set(forbidden))}",
            )

        return _make_probe("metrics_inspection", _CHECK_PASS)
    except urllib.error.URLError as e:
        return _make_probe(
            "metrics_inspection",
            _CHECK_FAIL,
            failure_reason="metrics_unreachable",
            detail=f"Connection failed: {e.reason}",
        )


# ── Entry Point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
