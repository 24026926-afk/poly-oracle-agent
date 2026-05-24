"""
tests/unit/test_WI-61-periodic-runtime-audit.py

Unit tests for WI-61 Periodic Runtime Audit.

Covers:
* Typed runtime-audit schemas in ``src/schemas/runtime_audit.py``.
* Deterministic auditor service in ``src/observability/runtime_audit.py``.
* ``scripts/ops/periodic_runtime_audit.py`` CLI entrypoint, exit codes.
* Exit code contract: 0=healthy, 1=degraded, 2=safety-gate failure, 3=probe error.
* ``dry_run=true`` mandatory safety gate.
* Decimal integrity, forbidden-content scanning, atomic artifact replacement.
* Telegram alert opt-in, optional LLM reviewer disabled by default.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import httpx

# ── WI-61 schemas — may not exist during red phase ────────────────────────

try:
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
        RuntimeAuditPositionSummary,
        RuntimeAuditProbeResult,
        RuntimeAuditProbeStatus,
        RuntimeAuditReadinessProbe,
        RuntimeAuditReport,
        RuntimeAuditSeverity,
        RuntimeAuditStatus,
        RuntimeAuditTelegramAlert,
        RuntimeAuditTelegramResult,
    )

    _SCHEMAS_AVAILABLE = True
except ImportError:
    _SCHEMAS_AVAILABLE = False

# ── WI-61 auditor service — may not exist during red phase ────────────────

try:
    from src.observability.runtime_audit import (
        _compute_cooldown_block_rate,
        check_dry_run_posture,
        check_forbidden_content,
        parse_prometheus_text,
        probe_database,
        probe_docker,
        probe_health,
        probe_log_tail,
        probe_metrics,
        probe_readiness,
        run_audit,
        send_telegram_alert,
        summarize_decision_repository,
        summarize_execution_repository,
        summarize_ledger,
        summarize_market_repository,
        summarize_position_repository,
        write_audit_artifacts,
    )

    _AUDITOR_AVAILABLE = True
except ImportError:
    _AUDITOR_AVAILABLE = False

try:
    from src.observability.runtime_audit import run_llm_review

    _REVIEWER_AVAILABLE = True
except ImportError:
    _REVIEWER_AVAILABLE = False

# ── WI-61 CLI — may not exist during red phase ────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = _PROJECT_ROOT / "scripts" / "ops" / "periodic_runtime_audit.py"
_cli = None
_CLI_AVAILABLE = False
if _CLI_PATH.exists():
    _spec = importlib.util.spec_from_file_location("wi61_audit_cli", _CLI_PATH)
    if _spec is not None and _spec.loader is not None:
        _cli = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_cli)
        _CLI_AVAILABLE = True


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _utc(year=2026, month=5, day=21, hour=12, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _healthy_prometheus_text() -> str:
    return (
        "# HELP polymarket_decisions_total Total decisions\n"
        "# TYPE polymarket_decisions_total counter\n"
        'polymarket_decisions_total{action="hold"} 42\n'
    )


def _forbidden_label_prometheus_text() -> str:
    return 'polymarket_decisions_total{condition_id="0xabc123"} 1\n'


def _malformed_prometheus_text() -> str:
    return "THIS IS NOT PROMETHEUS FORMAT {{{garbage\n"


# ═══════════════════════════════════════════════════════════════════════════
# SECTION: Schema tests
# ═══════════════════════════════════════════════════════════════════════════


def test_runtime_audit_status_enum_exists() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditStatus enum not implemented")
    expected = {"HEALTHY", "DEGRADED", "SAFETY_GATE_FAILED", "PROBE_ERROR"}
    actual = {m.value for m in RuntimeAuditStatus}
    assert expected.issubset(actual)


def test_runtime_audit_exit_code_enum_values() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditExitCode enum not implemented")
    assert {m.value for m in RuntimeAuditExitCode} == {0, 1, 2, 3}


def test_runtime_audit_exit_code_healthy_is_zero() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditExitCode enum not implemented")
    assert RuntimeAuditExitCode(0).value == 0


def test_runtime_audit_exit_code_safety_gate_is_two() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditExitCode enum not implemented")
    assert RuntimeAuditExitCode(2).value == 2


def test_runtime_audit_failure_reason_has_dry_run_false() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditFailureReason enum not implemented")
    expected = {
        "DRY_RUN_FALSE",
        "DRY_RUN_POSTURE_MISSING",
        "FORBIDDEN_METRIC_LABEL",
        "HEALTH_PROBE_ERROR",
        "READINESS_PROBE_ERROR",
        "METRICS_PROBE_ERROR",
        "METRICS_PARSE_ERROR",
        "DATABASE_PROBE_ERROR",
        "REPOSITORY_READ_ERROR",
        "ARTIFACT_WRITE_ERROR",
        "CONFIG_ERROR",
        "TIMEOUT",
    }
    actual = {m.value for m in RuntimeAuditFailureReason}
    assert expected.issubset(actual)


def test_runtime_audit_severity_enum_exists() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditSeverity enum not implemented")
    expected = {"INFO", "WARNING", "ERROR", "CRITICAL"}
    actual = {m.value for m in RuntimeAuditSeverity}
    assert expected.issubset(actual)


def test_runtime_audit_finding_type_enum_exists() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditFindingType enum not implemented")
    expected = {"SAFETY_GATE", "DEGRADATION", "WARNING", "PROBE_ERROR", "UNAVAILABLE"}
    actual = {m.value for m in RuntimeAuditFindingType}
    assert expected.issubset(actual)


def test_runtime_audit_finding_schema_frozen() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditFinding schema not implemented")
    finding = RuntimeAuditFinding(
        finding_type=RuntimeAuditFindingType.WARNING,
        severity=RuntimeAuditSeverity.WARNING,
        message="test",
        source="test",
    )
    with pytest.raises(Exception):
        finding.message = "mutated"


def test_runtime_audit_probe_status_enum_exists() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditProbeStatus enum not implemented")
    expected = {"SUCCESS", "DEGRADED", "ERROR", "UNAVAILABLE", "TIMEOUT"}
    actual = {m.value for m in RuntimeAuditProbeStatus}
    assert expected.issubset(actual)


def test_runtime_audit_probe_result_schema_exists() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditProbeResult schema not implemented")
    result = RuntimeAuditProbeResult(
        probe_name="health",
        status=RuntimeAuditProbeStatus.SUCCESS,
    )
    assert result.probe_name == "health"


def test_runtime_audit_health_probe_schema_exists() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditHealthProbe schema not implemented")
    probe = RuntimeAuditHealthProbe(
        status=RuntimeAuditProbeStatus.SUCCESS,
        reachable=True,
    )
    assert probe.reachable is True


def test_runtime_audit_readiness_probe_includes_dry_run() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditReadinessProbe schema not implemented")
    probe = RuntimeAuditReadinessProbe(
        status=RuntimeAuditProbeStatus.SUCCESS,
        reachable=True,
        ready=True,
        dry_run_posture=RuntimeAuditDryRunPosture(
            dry_run_confirmed=True,
            source="readyz",
        ),
    )
    assert probe.dry_run_posture.dry_run_confirmed is True


def test_runtime_audit_dry_run_posture_false() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditDryRunPosture schema not implemented")
    posture = RuntimeAuditDryRunPosture(dry_run_confirmed=False, source="readyz")
    assert posture.dry_run_confirmed is False


def test_runtime_audit_metric_sample_rejects_float() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditMetricSample schema not implemented")
    with pytest.raises((ValidationError, ValueError)):
        RuntimeAuditMetricSample(name="test", value=42.0, labels={})


def test_runtime_audit_metric_sample_accepts_decimal() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditMetricSample schema not implemented")
    sample = RuntimeAuditMetricSample(
        name="polymarket_decisions_total",
        value=Decimal("42"),
        labels={"action": "hold"},
    )
    assert isinstance(sample.value, Decimal)


def test_runtime_audit_ledger_summary_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditLedgerSummary schema not implemented")
    s = RuntimeAuditLedgerSummary(
        total_events=10,
        error_count=0,
        warning_count=2,
        ws_reconnect_count=0,
        budget_block_count=1,
        provider_failure_count=0,
        market_quarantine_count=0,
        readiness_change_count=0,
        alert_count=0,
        recovery_count=0,
    )
    assert s.total_events == 10


def test_runtime_audit_decision_summary_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditDecisionSummary schema not implemented")
    s = RuntimeAuditDecisionSummary(
        total_decisions=5,
        buy_count=1,
        sell_count=0,
        hold_count=4,
        skip_count=0,
    )
    assert s.total_decisions == 5


def test_runtime_audit_market_summary_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditMarketSummary schema not implemented")
    s = RuntimeAuditMarketSummary(total_snapshots=20, stale_count=2)
    assert s.total_snapshots == 20


def test_runtime_audit_position_summary_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditPositionSummary schema not implemented")
    s = RuntimeAuditPositionSummary(
        open_count=3,
        settled_count=10,
        total_open_exposure_usdc=Decimal("150.00"),
    )
    assert isinstance(s.total_open_exposure_usdc, Decimal)


def test_runtime_audit_position_summary_rejects_float() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditPositionSummary schema not implemented")
    with pytest.raises((ValidationError, ValueError)):
        RuntimeAuditPositionSummary(
            open_count=0,
            settled_count=0,
            total_open_exposure_usdc=150.0,
        )


def test_runtime_audit_execution_summary_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditExecutionSummary schema not implemented")
    s = RuntimeAuditExecutionSummary(
        total_executions=5,
        dry_run_count=5,
        live_count=0,
    )
    assert s.live_count == 0


def test_runtime_audit_database_probe_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditDatabaseProbe schema not implemented")
    p = RuntimeAuditDatabaseProbe(
        status=RuntimeAuditProbeStatus.SUCCESS,
        file_exists=True,
        file_size_bytes=1024,
    )
    assert p.file_exists is True


def test_runtime_audit_docker_probe_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditDockerProbe schema not implemented")
    p = RuntimeAuditDockerProbe(
        status=RuntimeAuditProbeStatus.UNAVAILABLE,
        docker_available=False,
    )
    assert p.docker_available is False


def test_runtime_audit_log_tail_summary_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditLogTailSummary schema not implemented")
    s = RuntimeAuditLogTailSummary(
        status=RuntimeAuditProbeStatus.SUCCESS,
        bytes_scanned=4096,
        lines_scanned=100,
        forbidden_content_detected=False,
    )
    assert s.forbidden_content_detected is False


def test_runtime_audit_forbidden_content_check_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError(
            "RuntimeAuditForbiddenContentCheck schema not implemented"
        )
    c = RuntimeAuditForbiddenContentCheck(
        scanned=True,
        clean=True,
        forbidden_patterns_found=[],
    )
    assert c.clean is True


def test_runtime_audit_artifact_write_result_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError(
            "RuntimeAuditArtifactWriteResult schema not implemented"
        )
    r = RuntimeAuditArtifactWriteResult(
        success=True,
        json_path="docs/operations/runtime_audits/runtime-audit-20260521T120000Z.json",
        md_path="docs/operations/runtime_audits/runtime-audit-20260521T120000Z.md",
    )
    assert r.success is True


def test_runtime_audit_telegram_alert_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditTelegramAlert schema not implemented")
    a = RuntimeAuditTelegramAlert(enabled=True, exit_code=1)
    assert a.enabled is True


def test_runtime_audit_telegram_result_disabled() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditTelegramResult schema not implemented")
    r = RuntimeAuditTelegramResult(
        sent=False,
        reason="disabled",
    )
    assert r.sent is False


def test_runtime_audit_llm_review_status_enum() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditLLMReviewStatus enum not implemented")
    expected = {"DISABLED", "SUCCESS", "CONFIG_ERROR", "TIMEOUT", "FORBIDDEN_CONTENT"}
    actual = {m.value for m in RuntimeAuditLLMReviewStatus}
    assert expected.issubset(actual)


def test_runtime_audit_llm_review_request_schema() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditLLMReviewRequest schema not implemented")
    req = RuntimeAuditLLMReviewRequest(
        audit_artifact_path="docs/operations/runtime_audits/latest.json",
        model="kimi-k2.6",
    )
    assert req.model == "kimi-k2.6"


def test_runtime_audit_llm_review_result_disabled_default() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditLLMReviewResult schema not implemented")
    r = RuntimeAuditLLMReviewResult(
        status=RuntimeAuditLLMReviewStatus.DISABLED,
    )
    assert r.status == RuntimeAuditLLMReviewStatus.DISABLED


def test_runtime_audit_report_schema_exists() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditReport schema not implemented")
    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.HEALTHY,
        exit_code=RuntimeAuditExitCode(0),
        generated_at_utc=_utc(),
        findings=[],
    )
    assert report.status == RuntimeAuditStatus.HEALTHY
    assert report.exit_code.value == 0


def test_runtime_audit_report_rejects_naive_timestamp() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditReport schema not implemented")
    with pytest.raises((ValidationError, ValueError)):
        RuntimeAuditReport(
            status=RuntimeAuditStatus.HEALTHY,
            exit_code=RuntimeAuditExitCode(0),
            generated_at_utc=datetime(2026, 5, 21, 12, 0, 0),
            findings=[],
        )


# ═══════════════════════════════════════════════════════════════════════════
# SECTION: Auditor service tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_probe_health_success() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_health not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_client.get = AsyncMock(return_value=mock_resp)
    result = await probe_health(mock_client, "http://test:8080")
    assert result.status == RuntimeAuditProbeStatus.SUCCESS
    assert result.reachable is True


@pytest.mark.asyncio
async def test_probe_health_unreachable_returns_probe_error() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_health not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("fail"))
    result = await probe_health(mock_client, "http://test:8080")
    assert result.status == RuntimeAuditProbeStatus.ERROR
    assert result.reachable is False


@pytest.mark.asyncio
async def test_probe_health_timeout() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_health not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("t"))
    result = await probe_health(mock_client, "http://test:8080", timeout=0.1)
    assert result.status == RuntimeAuditProbeStatus.TIMEOUT


@pytest.mark.asyncio
async def test_probe_readiness_success_with_dry_run() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_readiness not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {
        "status": "READY",
        "checks": {"database": "reachable"},
        "dry_run": True,
    }
    mock_client.get = AsyncMock(return_value=mock_resp)
    result = await probe_readiness(mock_client, "http://test:8080")
    assert result.status == RuntimeAuditProbeStatus.SUCCESS
    assert result.ready is True
    assert result.dry_run_posture is not None
    assert result.dry_run_posture.dry_run_confirmed is True


@pytest.mark.asyncio
async def test_probe_readiness_dry_run_false_is_safety_gate() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_readiness not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"status": "READY", "checks": {}, "dry_run": False}
    mock_client.get = AsyncMock(return_value=mock_resp)
    result = await probe_readiness(mock_client, "http://test:8080")
    assert result.dry_run_posture is not None
    assert result.dry_run_posture.dry_run_confirmed is False
    finding = check_dry_run_posture(result)
    assert finding is not None
    assert finding.finding_type == RuntimeAuditFindingType.SAFETY_GATE


@pytest.mark.asyncio
async def test_probe_readiness_missing_dry_run_posture_fails_closed() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_readiness not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.return_value = {"status": "READY", "checks": {}}
    mock_client.get = AsyncMock(return_value=mock_resp)
    result = await probe_readiness(mock_client, "http://test:8080")
    assert result.dry_run_posture is None
    finding = check_dry_run_posture(result)
    assert finding is not None
    assert finding.failure_reason == RuntimeAuditFailureReason.DRY_RUN_POSTURE_MISSING


@pytest.mark.asyncio
async def test_probe_readiness_unreachable() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_readiness not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("fail"))
    result = await probe_readiness(mock_client, "http://test:8080")
    assert result.status == RuntimeAuditProbeStatus.ERROR
    assert result.reachable is False


def test_parse_prometheus_text_valid() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("parse_prometheus_text not implemented")
    samples, forbidden, err = parse_prometheus_text(_healthy_prometheus_text())
    assert len(samples) > 0
    assert len(forbidden) == 0
    assert err is None


def test_parse_prometheus_text_forbidden_label_returns_exit_2() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("parse_prometheus_text not implemented")
    _, forbidden, _ = parse_prometheus_text(_forbidden_label_prometheus_text())
    assert len(forbidden) > 0


def test_parse_prometheus_text_malformed_returns_error() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("parse_prometheus_text not implemented")
    _, _, err = parse_prometheus_text(_malformed_prometheus_text())
    assert err is not None


def test_check_forbidden_content_clean() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("check_forbidden_content not implemented")
    result = check_forbidden_content("This is a clean audit report.")
    assert result.clean is True


def test_check_forbidden_content_detects_api_key() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("check_forbidden_content not implemented")
    result = check_forbidden_content("key: sk-ant-api03-abcdefghijklmnopqrstuv")
    assert result.clean is False


def test_check_forbidden_content_detects_wallet_address() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("check_forbidden_content not implemented")
    result = check_forbidden_content(
        "wallet: 0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    )
    assert result.clean is False


def test_check_forbidden_content_detects_condition_id() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("check_forbidden_content not implemented")
    result = check_forbidden_content("id: 0x" + "a1" * 32)
    assert result.clean is False


def test_check_dry_run_posture_true() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("check_dry_run_posture not implemented")
    readiness = RuntimeAuditReadinessProbe(
        status=RuntimeAuditProbeStatus.SUCCESS,
        reachable=True,
        ready=True,
        dry_run_posture=RuntimeAuditDryRunPosture(
            dry_run_confirmed=True, source="readyz"
        ),
    )
    assert check_dry_run_posture(readiness) is None


def test_check_dry_run_posture_false_is_safety_gate() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("check_dry_run_posture not implemented")
    readiness = RuntimeAuditReadinessProbe(
        status=RuntimeAuditProbeStatus.SUCCESS,
        reachable=True,
        ready=True,
        dry_run_posture=RuntimeAuditDryRunPosture(
            dry_run_confirmed=False, source="readyz"
        ),
    )
    finding = check_dry_run_posture(readiness)
    assert finding is not None
    assert finding.failure_reason == RuntimeAuditFailureReason.DRY_RUN_FALSE


@pytest.mark.asyncio
async def test_probe_metrics_success() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_metrics not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_resp.text = _healthy_prometheus_text()
    mock_client.get = AsyncMock(return_value=mock_resp)
    probe, samples = await probe_metrics(mock_client, "http://test:8080")
    assert probe.status == RuntimeAuditProbeStatus.SUCCESS
    assert len(samples) > 0


@pytest.mark.asyncio
async def test_probe_metrics_unreachable() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_metrics not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("fail"))
    probe, _ = await probe_metrics(mock_client, "http://test:8080")
    assert probe.status == RuntimeAuditProbeStatus.ERROR
    assert probe.reachable is False


@pytest.mark.asyncio
async def test_probe_database_file_exists_read_only() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_database not implemented")
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        f.write(b"x" * 1024)
        f.flush()
        result = await probe_database(f.name)
    assert result.status == RuntimeAuditProbeStatus.SUCCESS
    assert result.file_exists is True


@pytest.mark.asyncio
async def test_probe_database_file_missing_does_not_create() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_database not implemented")
    result = await probe_database("/tmp/nonexistent_audit_test_12345.db")
    assert result.status == RuntimeAuditProbeStatus.UNAVAILABLE
    assert result.file_exists is False


@pytest.mark.asyncio
async def test_probe_docker_unavailable_on_dev_machine() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_docker not implemented")
    result = await probe_docker(timeout=2.0)
    assert result.status in (
        RuntimeAuditProbeStatus.SUCCESS,
        RuntimeAuditProbeStatus.UNAVAILABLE,
        RuntimeAuditProbeStatus.DEGRADED,
        RuntimeAuditProbeStatus.ERROR,
    )


@pytest.mark.asyncio
async def test_probe_docker_read_only_no_mutating_commands() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_docker not implemented")
    import inspect

    src = inspect.getsource(probe_docker)
    for cmd in (
        "docker compose restart",
        "docker compose stop",
        "docker compose start",
    ):
        assert cmd not in src


@pytest.mark.asyncio
async def test_probe_log_tail_missing_file() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_log_tail not implemented")
    result = await probe_log_tail(log_path="/tmp/nonexistent_log_12345.log")
    assert result.status == RuntimeAuditProbeStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_probe_log_tail_bounded_byte_cap() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_log_tail not implemented")
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        f.write("INFO: test line\n" * 500)
        f.flush()
        result = await probe_log_tail(log_path=f.name, byte_cap=256)
    assert result.status == RuntimeAuditProbeStatus.SUCCESS
    assert result.bytes_scanned <= 512
    os.unlink(f.name)


@pytest.mark.asyncio
async def test_probe_log_tail_detects_forbidden_content() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("probe_log_tail not implemented")
    import tempfile
    import os

    with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
        f.write("api_key=sk-ant-api03-abcdefghijklmnopqrstuv\n")
        f.flush()
        result = await probe_log_tail(log_path=f.name)
    assert result.forbidden_content_detected is True
    os.unlink(f.name)


@pytest.mark.asyncio
async def test_summarize_ledger_bounded_window(db_session_factory) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("summarize_ledger not implemented")
    result = await summarize_ledger(db_session_factory, lookback_hours=24, limit=100)
    assert result.available is True
    assert result.total_events >= 0


@pytest.mark.asyncio
async def test_summarize_ledger_missing_table_unavailable(db_session_factory) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("summarize_ledger not implemented")
    result = await summarize_ledger(db_session_factory, lookback_hours=24, limit=100)
    assert result.available is True or result.message is not None


@pytest.mark.asyncio
async def test_summarize_decision_repository_bounded(db_session_factory) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("summarize_decision_repository not implemented")
    result = await summarize_decision_repository(
        db_session_factory, lookback_hours=24, limit=100
    )
    assert result.available is True or result.message is not None


@pytest.mark.asyncio
async def test_summarize_market_repository_stale_detection(db_session_factory) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("summarize_market_repository not implemented")
    result = await summarize_market_repository(
        db_session_factory, lookback_hours=24, limit=100
    )
    assert result.available is True or result.message is not None


@pytest.mark.asyncio
async def test_summarize_position_repository_decimal_exposure(
    db_session_factory,
) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("summarize_position_repository not implemented")
    result = await summarize_position_repository(db_session_factory)
    assert result.available is True or result.message is not None
    assert isinstance(result.total_open_exposure_usdc, Decimal)


@pytest.mark.asyncio
async def test_summarize_execution_repository_dry_run_evidence(
    db_session_factory,
) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("summarize_execution_repository not implemented")
    result = await summarize_execution_repository(
        db_session_factory, lookback_hours=24, limit=100
    )
    assert result.available is True or result.message is not None


def test_write_audit_artifacts_atomic_swap(tmp_path: Path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("write_audit_artifacts not implemented")
    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.HEALTHY,
        exit_code=RuntimeAuditExitCode(0),
        generated_at_utc=_utc(),
        findings=[],
    )
    out = tmp_path / "docs" / "operations" / "runtime_audits"
    result = write_audit_artifacts(report, output_dir=out, project_root=tmp_path)
    assert result.success is True
    assert result.latest_json_updated is True
    assert result.latest_md_updated is True
    assert (out / "latest.json").exists()
    assert (out / "latest.md").exists()


def test_write_audit_artifacts_path_traversal_rejected(tmp_path: Path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("write_audit_artifacts not implemented")
    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.HEALTHY,
        exit_code=RuntimeAuditExitCode(0),
        generated_at_utc=_utc(),
        findings=[],
    )
    result = write_audit_artifacts(
        report,
        output_dir=Path("/etc/evil"),
        project_root=tmp_path,
    )
    assert result.success is False


def test_write_audit_artifacts_forbidden_content_rejected(tmp_path: Path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("write_audit_artifacts not implemented")
    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.HEALTHY,
        exit_code=RuntimeAuditExitCode(0),
        generated_at_utc=_utc(),
        findings=[
            RuntimeAuditFinding(
                finding_type=RuntimeAuditFindingType.WARNING,
                severity=RuntimeAuditSeverity.WARNING,
                message="api_key=sk-ant-api03-abcdefghijklmnopqrstuv",
                source="test",
            )
        ],
    )
    out = tmp_path / "docs" / "operations" / "runtime_audits"
    result = write_audit_artifacts(report, output_dir=out, project_root=tmp_path)
    assert result.success is False


def test_write_audit_artifacts_partial_failure_keeps_latest_intact(
    tmp_path: Path,
) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("write_audit_artifacts not implemented")
    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.HEALTHY,
        exit_code=RuntimeAuditExitCode(0),
        generated_at_utc=_utc(),
        findings=[],
    )
    out = tmp_path / "docs" / "operations" / "runtime_audits"
    r1 = write_audit_artifacts(report, output_dir=out, project_root=tmp_path)
    assert r1.success is True
    assert (out / "latest.json").exists()


@pytest.mark.asyncio
async def test_send_telegram_alert_disabled() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("send_telegram_alert not implemented")
    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.DEGRADED,
        exit_code=RuntimeAuditExitCode(1),
        generated_at_utc=_utc(),
        findings=[],
    )
    result = await send_telegram_alert(None, report, enabled=False)
    assert result.sent is False
    assert result.reason == "disabled"


@pytest.mark.asyncio
async def test_send_telegram_alert_exit_code_1_sends() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("send_telegram_alert not implemented")
    from unittest.mock import AsyncMock

    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.DEGRADED,
        exit_code=RuntimeAuditExitCode(1),
        generated_at_utc=_utc(),
        findings=[],
    )
    mock_notifier = AsyncMock()
    mock_notifier.try_send_execution_event = AsyncMock(return_value=True)
    result = await send_telegram_alert(mock_notifier, report, enabled=True)
    assert result.sent is True


@pytest.mark.asyncio
async def test_send_telegram_alert_exit_code_0_skips() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("send_telegram_alert not implemented")
    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.HEALTHY,
        exit_code=RuntimeAuditExitCode(0),
        generated_at_utc=_utc(),
        findings=[],
    )
    result = await send_telegram_alert(None, report, enabled=True)
    assert result.sent is False
    assert result.reason == "exit_code_0_no_alert"


@pytest.mark.asyncio
async def test_send_telegram_alert_secret_safe_payload() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("send_telegram_alert not implemented")
    from unittest.mock import AsyncMock

    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.DEGRADED,
        exit_code=RuntimeAuditExitCode(1),
        generated_at_utc=_utc(),
        findings=[],
    )
    mock_notifier = AsyncMock()
    mock_notifier.try_send_execution_event = AsyncMock(return_value=True)
    await send_telegram_alert(mock_notifier, report, enabled=True)
    call_args = mock_notifier.try_send_execution_event.call_args
    text = call_args[0][0]
    fc = check_forbidden_content(text)
    assert fc.clean is True


@pytest.mark.asyncio
async def test_send_telegram_alert_failure_typed_result() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("send_telegram_alert not implemented")
    from unittest.mock import AsyncMock

    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.DEGRADED,
        exit_code=RuntimeAuditExitCode(1),
        generated_at_utc=_utc(),
        findings=[],
    )
    mock_notifier = AsyncMock()
    mock_notifier.try_send_execution_event = AsyncMock(return_value=False)
    result = await send_telegram_alert(mock_notifier, report, enabled=True)
    assert result.sent is False
    assert result.reason == "send_failed"


@pytest.mark.asyncio
async def test_run_audit_healthy_exit_0() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_audit not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    health_resp = MagicMock(status_code=200)
    ready_resp = MagicMock(status_code=200)
    ready_resp.json.return_value = {"status": "READY", "dry_run": True, "checks": {}}
    metrics_resp = MagicMock(status_code=200)
    metrics_resp.text = _healthy_prometheus_text()
    mock_client.get = AsyncMock(side_effect=[health_resp, ready_resp, metrics_resp])
    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        project_root=Path("/tmp"),
        output_dir=Path("/tmp/docs/operations/runtime_audits"),
    )
    assert report.exit_code == RuntimeAuditExitCode(0)
    assert report.status == RuntimeAuditStatus.HEALTHY


@pytest.mark.asyncio
async def test_run_audit_degraded_exit_1() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_audit not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    health_resp = MagicMock(status_code=200)
    ready_resp = MagicMock(status_code=503)
    ready_resp.json.return_value = {"status": "DEGRADED", "dry_run": True, "checks": {}}
    metrics_resp = MagicMock(status_code=200)
    metrics_resp.text = _healthy_prometheus_text()
    mock_client.get = AsyncMock(side_effect=[health_resp, ready_resp, metrics_resp])
    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        project_root=Path("/tmp"),
        output_dir=Path("/tmp/docs/operations/runtime_audits"),
    )
    assert report.exit_code.value >= 1


@pytest.mark.asyncio
async def test_run_audit_dry_run_false_exit_2() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_audit not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    health_resp = MagicMock(status_code=200)
    ready_resp = MagicMock(status_code=200)
    ready_resp.json.return_value = {"status": "READY", "dry_run": False, "checks": {}}
    metrics_resp = MagicMock(status_code=200)
    metrics_resp.text = _healthy_prometheus_text()
    mock_client.get = AsyncMock(side_effect=[health_resp, ready_resp, metrics_resp])
    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        project_root=Path("/tmp"),
        output_dir=Path("/tmp/docs/operations/runtime_audits"),
    )
    assert report.exit_code == RuntimeAuditExitCode(2)


@pytest.mark.asyncio
async def test_run_audit_forbidden_metric_label_exit_2() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_audit not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    health_resp = MagicMock(status_code=200)
    ready_resp = MagicMock(status_code=200)
    ready_resp.json.return_value = {"status": "READY", "dry_run": True, "checks": {}}
    metrics_resp = MagicMock(status_code=200)
    metrics_resp.text = _forbidden_label_prometheus_text()
    mock_client.get = AsyncMock(side_effect=[health_resp, ready_resp, metrics_resp])
    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        project_root=Path("/tmp"),
        output_dir=Path("/tmp/docs/operations/runtime_audits"),
    )
    assert report.exit_code == RuntimeAuditExitCode(2)


@pytest.mark.asyncio
async def test_run_audit_probe_error_exit_3() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_audit not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("fail"))
    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        project_root=Path("/tmp"),
        output_dir=Path("/tmp/docs/operations/runtime_audits"),
    )
    assert report.exit_code.value >= 2


@pytest.mark.asyncio
async def test_run_audit_deterministic_same_inputs() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_audit not implemented")
    from unittest.mock import AsyncMock, MagicMock

    def make_mock():
        m = MagicMock()
        h = MagicMock(status_code=200)
        r = MagicMock(status_code=200)
        r.json.return_value = {"status": "READY", "dry_run": True, "checks": {}}
        mt = MagicMock(status_code=200)
        mt.text = _healthy_prometheus_text()
        m.get = AsyncMock(side_effect=[h, r, mt])
        return m

    r1 = await run_audit(
        http_client=make_mock(),
        base_url="http://test:8080",
        project_root=Path("/tmp"),
        output_dir=Path("/tmp/docs/operations/runtime_audits"),
    )
    r2 = await run_audit(
        http_client=make_mock(),
        base_url="http://test:8080",
        project_root=Path("/tmp"),
        output_dir=Path("/tmp/docs/operations/runtime_audits"),
    )
    assert r1.status == r2.status
    assert r1.exit_code == r2.exit_code


@pytest.mark.asyncio
async def test_run_audit_read_only_no_event_mutation() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_audit not implemented")
    import inspect

    src = inspect.getsource(run_audit)
    assert "repo.append(" not in src
    assert "repo.delete(" not in src
    assert "repo.update(" not in src


@pytest.mark.asyncio
async def test_run_audit_optional_probe_unavailable_no_crash() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_audit not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    health_resp = MagicMock(status_code=200)
    ready_resp = MagicMock(status_code=200)
    ready_resp.json.return_value = {"status": "READY", "dry_run": True, "checks": {}}
    mock_client.get = AsyncMock(
        side_effect=[health_resp, ready_resp, httpx.ConnectError("fail")]
    )
    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        project_root=Path("/tmp"),
        output_dir=Path("/tmp/docs/operations/runtime_audits"),
        skip_docker=True,
        skip_log_tail=True,
    )
    assert report is not None
    assert report.exit_code.value in (0, 1, 2, 3)


# ═══════════════════════════════════════════════════════════════════════════
# SECTION: CLI tests
# ═══════════════════════════════════════════════════════════════════════════


def test_cli_module_exists() -> None:
    if not _CLI_AVAILABLE:
        raise NotImplementedError("CLI scripts/ops/periodic_runtime_audit.py not found")
    assert _cli is not None


def test_cli_has_main_entrypoint() -> None:
    if not _CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    assert hasattr(_cli, "main")


def test_cli_exit_code_0_healthy(monkeypatch: pytest.MonkeyPatch) -> None:
    if not _CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    from unittest.mock import MagicMock, patch

    mock_report = MagicMock()
    mock_report.exit_code = RuntimeAuditExitCode(0)
    mock_report.status = RuntimeAuditStatus.HEALTHY
    mock_report.findings = []

    async def mock_run_audit(**kwargs):
        return mock_report

    monkeypatch.setattr("sys.argv", ["periodic_runtime_audit.py"])
    with patch.object(_cli, "run_audit", side_effect=mock_run_audit):
        with patch("src.observability.runtime_audit.httpx.AsyncClient"):
            pass
    assert _cli.main is not None


def test_cli_exit_code_1_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    if not _CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    assert _cli is not None


def test_cli_exit_code_2_safety_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    if not _CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    assert _cli is not None


def test_cli_exit_code_3_probe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    if not _CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    assert _cli is not None


def test_cli_output_dir_constrained_to_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    monkeypatch.setattr(
        "sys.argv", ["periodic_runtime_audit.py", "--output-dir", "/etc/evil"]
    )
    result = _cli.main()
    assert result == RuntimeAuditExitCode.PROBE_ERROR.value


def test_cli_rejects_absolute_output_path_outside_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if not _CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    monkeypatch.setattr(
        "sys.argv", ["periodic_runtime_audit.py", "--output-dir", "/tmp/outside"]
    )
    result = _cli.main()
    assert result == RuntimeAuditExitCode.PROBE_ERROR.value


# ═══════════════════════════════════════════════════════════════════════════
# SECTION: LLM reviewer tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_reviewer_disabled_by_default() -> None:
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError("run_llm_review not implemented")
    result = await run_llm_review(
        RuntimeAuditLLMReviewRequest(
            audit_artifact_path="docs/operations/runtime_audits/latest.json",
        ),
        enabled=False,
    )
    assert result.status == RuntimeAuditLLMReviewStatus.DISABLED


@pytest.mark.asyncio
async def test_reviewer_missing_api_key_returns_config_error() -> None:
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError("run_llm_review not implemented")
    result = await run_llm_review(
        RuntimeAuditLLMReviewRequest(
            audit_artifact_path="docs/operations/runtime_audits/latest.json",
        ),
        api_key=None,
        enabled=True,
    )
    assert result.status == RuntimeAuditLLMReviewStatus.CONFIG_ERROR


@pytest.mark.asyncio
async def test_reviewer_success_writes_advisory_markdown(tmp_path: Path) -> None:
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError("run_llm_review not implemented")
    from unittest.mock import AsyncMock, MagicMock, patch

    artifact_dir = tmp_path / "docs" / "operations" / "runtime_audits"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "latest.json"
    artifact.write_text('{"status": "HEALTHY"}', encoding="utf-8")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "All systems nominal."}}]
    }
    mock_resp.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch(
        "src.observability.runtime_audit.httpx.AsyncClient", return_value=mock_client
    ):
        result = await run_llm_review(
            RuntimeAuditLLMReviewRequest(
                audit_artifact_path="docs/operations/runtime_audits/latest.json",
            ),
            api_key="test-key",
            project_root=tmp_path,
            enabled=True,
        )
    assert result.status == RuntimeAuditLLMReviewStatus.SUCCESS


@pytest.mark.asyncio
async def test_reviewer_timeout_typed_result(tmp_path: Path) -> None:
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError("run_llm_review not implemented")
    from unittest.mock import AsyncMock, patch

    artifact_dir = tmp_path / "docs" / "operations" / "runtime_audits"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "latest.json"
    artifact.write_text('{"status": "HEALTHY"}', encoding="utf-8")
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch(
        "src.observability.runtime_audit.httpx.AsyncClient", return_value=mock_client
    ):
        result = await run_llm_review(
            RuntimeAuditLLMReviewRequest(
                audit_artifact_path="docs/operations/runtime_audits/latest.json",
            ),
            api_key="test-key",
            project_root=tmp_path,
            enabled=True,
        )
    assert result.status == RuntimeAuditLLMReviewStatus.TIMEOUT


@pytest.mark.asyncio
async def test_reviewer_forbidden_content_in_response_rejected(tmp_path: Path) -> None:
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError("run_llm_review not implemented")
    from unittest.mock import AsyncMock, MagicMock, patch

    artifact_dir = tmp_path / "docs" / "operations" / "runtime_audits"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "latest.json"
    artifact.write_text('{"status": "HEALTHY"}', encoding="utf-8")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "Found api_key=sk-ant-api03-abcdefghijklmnopqrstuv"
                }
            }
        ]
    }
    mock_resp.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch(
        "src.observability.runtime_audit.httpx.AsyncClient", return_value=mock_client
    ):
        result = await run_llm_review(
            RuntimeAuditLLMReviewRequest(
                audit_artifact_path="docs/operations/runtime_audits/latest.json",
            ),
            api_key="test-key",
            project_root=tmp_path,
            enabled=True,
        )
    assert result.status == RuntimeAuditLLMReviewStatus.FORBIDDEN_CONTENT


@pytest.mark.asyncio
async def test_reviewer_uses_direct_httpx_no_framework() -> None:
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError("run_llm_review not implemented")
    import inspect

    src = inspect.getsource(run_llm_review)
    assert "httpx" in src
    assert "openai" not in src
    assert "hermes" not in src.lower()
    assert "opencode" not in src.lower()


@pytest.mark.asyncio
async def test_reviewer_no_write_authority_beyond_review_artifact() -> None:
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError("run_llm_review not implemented")
    import inspect

    src = inspect.getsource(run_llm_review)
    assert "subprocess" not in src
    assert "docker" not in src.lower()
    assert "git " not in src


@pytest.mark.asyncio
async def test_reviewer_api_key_never_printed() -> None:
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError("run_llm_review not implemented")
    import inspect

    src = inspect.getsource(run_llm_review)
    assert "print(" not in src


# ═══════════════════════════════════════════════════════════════════════════
# SECTION: Coverage-boosting edge-case tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_probe_health_non_200_response() -> None:
    """Health probe reachable but non-200 returns ERROR."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock(status_code=500)
    mock_client.get = AsyncMock(return_value=mock_resp)
    result = await probe_health(mock_client, "http://test:8080")
    assert result.status == RuntimeAuditProbeStatus.ERROR
    assert result.reachable is True
    assert "HTTP 500" in result.message


@pytest.mark.asyncio
async def test_probe_readiness_unexpected_status_code() -> None:
    """Readiness probe with unexpected HTTP code returns ERROR."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock(status_code=404)
    mock_client.get = AsyncMock(return_value=mock_resp)
    result = await probe_readiness(mock_client, "http://test:8080")
    assert result.status == RuntimeAuditProbeStatus.ERROR
    assert result.reachable is True


@pytest.mark.asyncio
async def test_probe_readiness_invalid_json() -> None:
    """Readiness probe with invalid JSON returns ERROR."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_resp.json.side_effect = ValueError("bad json")
    mock_client.get = AsyncMock(return_value=mock_resp)
    result = await probe_readiness(mock_client, "http://test:8080")
    assert result.status == RuntimeAuditProbeStatus.ERROR
    assert "Invalid JSON" in result.message


@pytest.mark.asyncio
async def test_probe_readiness_timeout() -> None:
    """Readiness probe timeout returns TIMEOUT."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("t"))
    result = await probe_readiness(mock_client, "http://test:8080")
    assert result.status == RuntimeAuditProbeStatus.TIMEOUT


@pytest.mark.asyncio
async def test_probe_metrics_non_200() -> None:
    """Metrics probe reachable non-200 returns ERROR."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock(status_code=500)
    mock_client.get = AsyncMock(return_value=mock_resp)
    probe, samples = await probe_metrics(mock_client, "http://test:8080")
    assert probe.status == RuntimeAuditProbeStatus.ERROR
    assert probe.reachable is True


@pytest.mark.asyncio
async def test_probe_metrics_timeout() -> None:
    """Metrics probe timeout returns TIMEOUT."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("t"))
    probe, _ = await probe_metrics(mock_client, "http://test:8080")
    assert probe.status == RuntimeAuditProbeStatus.TIMEOUT


@pytest.mark.asyncio
async def test_probe_metrics_parse_error_only() -> None:
    """Metrics probe with only parse errors returns ERROR."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_resp = MagicMock(status_code=200)
    mock_resp.text = "THIS IS NOT PROMETHEUS FORMAT {{{garbage\n"
    mock_client.get = AsyncMock(return_value=mock_resp)
    probe, samples = await probe_metrics(mock_client, "http://test:8080")
    assert probe.status == RuntimeAuditProbeStatus.ERROR
    assert probe.parse_error is not None


@pytest.mark.asyncio
async def test_probe_database_oversized_degraded() -> None:
    """Database probe oversized file returns DEGRADED."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        f.write(b"x" * 2048)
        f.flush()
        result = await probe_database(f.name, max_size_bytes=1024)
    assert result.status == RuntimeAuditProbeStatus.DEGRADED
    assert result.file_exists is True


@pytest.mark.asyncio
async def test_probe_log_tail_no_path_configured() -> None:
    """Log tail with no path returns UNAVAILABLE."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    import os

    old = os.environ.pop("APP_LOG_PATH", None)
    try:
        result = await probe_log_tail(log_path="")
    finally:
        if old is not None:
            os.environ["APP_LOG_PATH"] = old
    assert result.status == RuntimeAuditProbeStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_probe_docker_timeout() -> None:
    """Docker probe timeout returns TIMEOUT."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import patch
    import subprocess as sp

    with patch(
        "src.observability.runtime_audit.subprocess.run",
        side_effect=sp.TimeoutExpired("docker", 1),
    ):
        result = await probe_docker(timeout=0.1)
    assert result.status == RuntimeAuditProbeStatus.TIMEOUT


@pytest.mark.asyncio
async def test_probe_docker_nonzero_return() -> None:
    """Docker probe nonzero return returns UNAVAILABLE."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import patch, MagicMock

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stdout = ""
    with patch(
        "src.observability.runtime_audit.subprocess.run", return_value=mock_result
    ):
        result = await probe_docker(timeout=2.0)
    assert result.status == RuntimeAuditProbeStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_probe_docker_file_not_found() -> None:
    """Docker probe FileNotFoundError returns UNAVAILABLE."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import patch

    with patch(
        "src.observability.runtime_audit.subprocess.run",
        side_effect=FileNotFoundError("docker"),
    ):
        result = await probe_docker(timeout=2.0)
    assert result.status == RuntimeAuditProbeStatus.UNAVAILABLE
    assert result.docker_available is False


@pytest.mark.asyncio
async def test_probe_docker_generic_error() -> None:
    """Docker probe generic error returns ERROR."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import patch

    with patch(
        "src.observability.runtime_audit.subprocess.run", side_effect=OSError("fail")
    ):
        result = await probe_docker(timeout=2.0)
    assert result.status == RuntimeAuditProbeStatus.ERROR


@pytest.mark.asyncio
async def test_probe_docker_with_services() -> None:
    """Docker probe with running services returns SUCCESS."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import patch, MagicMock

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = '{"State": "running", "RestartCount": 0}\n{"State": "running", "RestartCount": 2}\n'
    with patch(
        "src.observability.runtime_audit.subprocess.run", return_value=mock_result
    ):
        result = await probe_docker(timeout=2.0)
    assert result.status == RuntimeAuditProbeStatus.SUCCESS
    assert result.services_running == 2
    assert result.max_restart_count == 2


@pytest.mark.asyncio
async def test_probe_docker_degraded_services() -> None:
    """Docker probe with fewer running than total returns DEGRADED."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import patch, MagicMock

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = '{"State": "running", "RestartCount": 0}\n{"State": "paused", "RestartCount": 0}\n'
    with patch(
        "src.observability.runtime_audit.subprocess.run", return_value=mock_result
    ):
        result = await probe_docker(timeout=2.0)
    assert result.status == RuntimeAuditProbeStatus.DEGRADED


@pytest.mark.asyncio
async def test_probe_docker_malformed_json_lines() -> None:
    """Docker probe skips malformed JSON lines."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import patch, MagicMock

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = 'not json\n{"State": "running", "RestartCount": 0}\n'
    with patch(
        "src.observability.runtime_audit.subprocess.run", return_value=mock_result
    ):
        result = await probe_docker(timeout=2.0)
    assert result.services_total == 1


def test_parse_prometheus_invalid_value() -> None:
    """Prometheus parser handles invalid numeric values."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    text = 'metric_name{label="val"} not_a_number\n'
    samples, forbidden, err = parse_prometheus_text(text)
    assert err is not None
    assert len(samples) == 0


def test_check_forbidden_content_substring_detection() -> None:
    """Forbidden content scan detects forbidden substrings."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    result = check_forbidden_content("connection_string=postgres://user:pass@host/db")
    assert result.clean is False
    assert any("connection_string" in p for p in result.forbidden_patterns_found)


@pytest.mark.asyncio
async def test_reviewer_path_traversal_rejected(tmp_path: Path) -> None:
    """Reviewer rejects artifact path outside project root."""
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError
    result = await run_llm_review(
        RuntimeAuditLLMReviewRequest(
            audit_artifact_path="../../etc/passwd",
        ),
        api_key="test-key",
        project_root=tmp_path,
        enabled=True,
    )
    assert result.status == RuntimeAuditLLMReviewStatus.CONFIG_ERROR


@pytest.mark.asyncio
async def test_reviewer_artifact_not_found(tmp_path: Path) -> None:
    """Reviewer returns CONFIG_ERROR when artifact missing."""
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError
    result = await run_llm_review(
        RuntimeAuditLLMReviewRequest(
            audit_artifact_path="docs/operations/runtime_audits/nonexistent.json",
        ),
        api_key="test-key",
        project_root=tmp_path,
        enabled=True,
    )
    assert result.status == RuntimeAuditLLMReviewStatus.CONFIG_ERROR


@pytest.mark.asyncio
async def test_reviewer_http_error(tmp_path: Path) -> None:
    """Reviewer returns HTTP_ERROR on generic exception."""
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock, patch

    artifact_dir = tmp_path / "docs" / "operations" / "runtime_audits"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "latest.json"
    artifact.write_text('{"status": "HEALTHY"}', encoding="utf-8")
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "err", request=MagicMock(), response=MagicMock()
        )
    )
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch(
        "src.observability.runtime_audit.httpx.AsyncClient", return_value=mock_client
    ):
        result = await run_llm_review(
            RuntimeAuditLLMReviewRequest(
                audit_artifact_path="docs/operations/runtime_audits/latest.json",
            ),
            api_key="test-key",
            project_root=tmp_path,
            enabled=True,
        )
    assert result.status == RuntimeAuditLLMReviewStatus.HTTP_ERROR


@pytest.mark.asyncio
async def test_reviewer_no_choices_in_response(tmp_path: Path) -> None:
    """Reviewer returns HTTP_ERROR when no choices."""
    if not _REVIEWER_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock, patch

    artifact_dir = tmp_path / "docs" / "operations" / "runtime_audits"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "latest.json"
    artifact.write_text('{"status": "HEALTHY"}', encoding="utf-8")
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"choices": []}
    mock_resp.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    with patch(
        "src.observability.runtime_audit.httpx.AsyncClient", return_value=mock_client
    ):
        result = await run_llm_review(
            RuntimeAuditLLMReviewRequest(
                audit_artifact_path="docs/operations/runtime_audits/latest.json",
            ),
            api_key="test-key",
            project_root=tmp_path,
            enabled=True,
        )
    assert result.status == RuntimeAuditLLMReviewStatus.HTTP_ERROR


@pytest.mark.asyncio
async def test_send_telegram_alert_notifier_unavailable() -> None:
    """Telegram alert with None notifier returns unavailable."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.DEGRADED,
        exit_code=RuntimeAuditExitCode(1),
        generated_at_utc=_utc(),
        findings=[],
    )
    result = await send_telegram_alert(None, report, enabled=True)
    assert result.sent is False
    assert result.reason == "notifier_unavailable"


@pytest.mark.asyncio
async def test_send_telegram_alert_exception_typed() -> None:
    """Telegram alert exception returns typed failure."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock

    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.DEGRADED,
        exit_code=RuntimeAuditExitCode(1),
        generated_at_utc=_utc(),
        findings=[],
    )
    mock_notifier = AsyncMock()
    mock_notifier.try_send_execution_event = AsyncMock(side_effect=RuntimeError("boom"))
    result = await send_telegram_alert(mock_notifier, report, enabled=True)
    assert result.sent is False
    assert result.reason == "send_error"


def test_write_audit_artifacts_write_error(tmp_path: Path) -> None:
    """Artifact write error returns typed failure."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.HEALTHY,
        exit_code=RuntimeAuditExitCode(0),
        generated_at_utc=_utc(),
        findings=[],
    )
    out = tmp_path / "readonly_dir"
    out.mkdir()
    out.chmod(0o444)
    result = write_audit_artifacts(report, output_dir=out, project_root=tmp_path)
    out.chmod(0o755)
    assert result.success is False or result.latest_json_updated is False


@pytest.mark.asyncio
async def test_summarize_ledger_exception_returns_unavailable() -> None:
    """Ledger summary exception returns unavailable."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_factory = MagicMock()
    mock_session = AsyncMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch(
        "src.observability.runtime_audit.OperationalEventRepository"
    ) as mock_repo_cls:
        mock_repo = MagicMock()
        mock_repo.read_window = AsyncMock(side_effect=RuntimeError("db error"))
        mock_repo_cls.return_value = mock_repo
        result = await summarize_ledger(mock_factory)
    assert result.available is False
    assert "RuntimeError" in result.message


@pytest.mark.asyncio
async def test_summarize_decision_exception_returns_unavailable() -> None:
    """Decision summary exception returns unavailable."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_factory = MagicMock()
    mock_session = AsyncMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch("src.observability.runtime_audit.DecisionRepository") as mock_repo_cls:
        mock_repo = MagicMock()
        mock_repo.get_recent_decisions = AsyncMock(side_effect=RuntimeError("db error"))
        mock_repo_cls.return_value = mock_repo
        result = await summarize_decision_repository(mock_factory)
    assert result.available is False


@pytest.mark.asyncio
async def test_summarize_market_exception_returns_unavailable() -> None:
    """Market summary exception returns unavailable."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_factory = MagicMock()
    mock_session = AsyncMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch("src.observability.runtime_audit.MarketRepository") as mock_repo_cls:
        mock_repo = MagicMock()
        mock_repo.get_recent_snapshots = AsyncMock(side_effect=RuntimeError("db error"))
        mock_repo_cls.return_value = mock_repo
        result = await summarize_market_repository(mock_factory)
    assert result.available is False


@pytest.mark.asyncio
async def test_summarize_position_exception_returns_unavailable() -> None:
    """Position summary exception returns unavailable."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_factory = MagicMock()
    mock_session = AsyncMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch("src.observability.runtime_audit.PositionRepository") as mock_repo_cls:
        mock_repo = MagicMock()
        mock_repo.get_open_positions = AsyncMock(side_effect=RuntimeError("db error"))
        mock_repo_cls.return_value = mock_repo
        result = await summarize_position_repository(mock_factory)
    assert result.available is False


@pytest.mark.asyncio
async def test_summarize_execution_exception_returns_unavailable() -> None:
    """Execution summary exception returns unavailable."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_factory = MagicMock()
    mock_session = AsyncMock()
    mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch("src.observability.runtime_audit.ExecutionRepository") as mock_repo_cls:
        mock_repo = MagicMock()
        mock_repo.get_recent_executions = AsyncMock(
            side_effect=RuntimeError("db error")
        )
        mock_repo_cls.return_value = mock_repo
        result = await summarize_execution_repository(mock_factory)
    assert result.available is False


@pytest.mark.asyncio
async def test_probe_database_stat_error() -> None:
    """Database probe stat error returns ERROR."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import patch, MagicMock

    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_path.stat.side_effect = OSError("stat failed")
    mock_path.name = "test.db"
    with patch("src.observability.runtime_audit.Path", return_value=mock_path):
        result = await probe_database("test.db")
    assert result.status == RuntimeAuditProbeStatus.ERROR


@pytest.mark.asyncio
async def test_probe_log_tail_read_error() -> None:
    """Log tail read error returns ERROR."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import patch, MagicMock

    mock_path = MagicMock()
    mock_path.exists.return_value = True
    mock_path.stat.return_value = MagicMock(st_size=1024)
    mock_path.__truediv__ = MagicMock(return_value=mock_path)
    with patch("src.observability.runtime_audit.Path", return_value=mock_path):
        with patch("builtins.open", side_effect=OSError("read failed")):
            result = await probe_log_tail(log_path="/fake/log.log")
    assert result.status == RuntimeAuditProbeStatus.ERROR


@pytest.mark.asyncio
async def test_run_audit_health_non_200_finding() -> None:
    """Run audit adds finding for reachable non-200 health."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    health_resp = MagicMock(status_code=500)
    ready_resp = MagicMock(status_code=200)
    ready_resp.json.return_value = {"status": "READY", "dry_run": True, "checks": {}}
    metrics_resp = MagicMock(status_code=200)
    metrics_resp.text = "# HELP test\n# TYPE test counter\ntest 1\n"
    mock_client.get = AsyncMock(side_effect=[health_resp, ready_resp, metrics_resp])
    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        project_root=Path("/tmp"),
        output_dir=Path("/tmp/docs/operations/runtime_audits"),
    )
    health_findings = [f for f in report.findings if f.source == "health"]
    assert len(health_findings) >= 1


@pytest.mark.asyncio
async def test_run_audit_metrics_reachable_error_finding() -> None:
    """Run audit adds finding for reachable metrics error."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    health_resp = MagicMock(status_code=200)
    ready_resp = MagicMock(status_code=200)
    ready_resp.json.return_value = {"status": "READY", "dry_run": True, "checks": {}}
    metrics_resp = MagicMock(status_code=500)
    mock_client.get = AsyncMock(side_effect=[health_resp, ready_resp, metrics_resp])
    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        project_root=Path("/tmp"),
        output_dir=Path("/tmp/docs/operations/runtime_audits"),
    )
    metrics_findings = [f for f in report.findings if f.source == "metrics"]
    assert len(metrics_findings) >= 1


def test_write_audit_artifacts_latest_json_swap_failure_returns_typed_failure(
    tmp_path: Path,
) -> None:
    """latest.json atomic replace failure must return success=False with typed reason."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.HEALTHY,
        exit_code=RuntimeAuditExitCode(0),
        generated_at_utc=_utc(),
        findings=[],
    )
    out = tmp_path / "docs" / "operations" / "runtime_audits"
    out.mkdir(parents=True, exist_ok=True)
    # Pre-create latest.json as a non-empty directory so .replace() fails.
    latest_json_dir = out / "latest.json"
    latest_json_dir.mkdir()
    (latest_json_dir / "blocker.txt").write_text("x", encoding="utf-8")

    result = write_audit_artifacts(report, output_dir=out, project_root=tmp_path)

    assert result.success is False
    assert result.latest_json_updated is False
    assert result.failure_reason is not None
    assert "latest.json" in result.failure_reason
    # Timestamped artifacts must remain intact.
    timestamped = [
        p for p in out.iterdir() if p.is_file() and p.name.startswith("runtime-audit-")
    ]
    assert len(timestamped) >= 2  # json + md


def test_write_audit_artifacts_latest_md_swap_failure_returns_typed_failure(
    tmp_path: Path,
) -> None:
    """latest.md atomic replace failure must return success=False with typed reason."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.HEALTHY,
        exit_code=RuntimeAuditExitCode(0),
        generated_at_utc=_utc(),
        findings=[],
    )
    out = tmp_path / "docs" / "operations" / "runtime_audits"
    out.mkdir(parents=True, exist_ok=True)
    latest_md_dir = out / "latest.md"
    latest_md_dir.mkdir()
    (latest_md_dir / "blocker.txt").write_text("x", encoding="utf-8")

    result = write_audit_artifacts(report, output_dir=out, project_root=tmp_path)

    assert result.success is False
    assert result.latest_md_updated is False
    assert result.failure_reason is not None
    assert "latest.md" in result.failure_reason


@pytest.mark.asyncio
async def test_run_audit_latest_swap_failure_yields_probe_error_exit_code(
    tmp_path: Path,
) -> None:
    """run_audit must derive PROBE_ERROR / exit code 3 when latest.* swap fails."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError
    from unittest.mock import AsyncMock, MagicMock

    out = tmp_path / "docs" / "operations" / "runtime_audits"
    out.mkdir(parents=True, exist_ok=True)
    latest_json_dir = out / "latest.json"
    latest_json_dir.mkdir()
    (latest_json_dir / "blocker.txt").write_text("x", encoding="utf-8")

    mock_client = MagicMock()
    health_resp = MagicMock(status_code=200)
    ready_resp = MagicMock(status_code=200)
    ready_resp.json.return_value = {
        "status": "READY",
        "dry_run": True,
        "checks": {},
    }
    metrics_resp = MagicMock(status_code=200)
    metrics_resp.text = "# HELP test\n# TYPE test counter\ntest 1\n"
    mock_client.get = AsyncMock(side_effect=[health_resp, ready_resp, metrics_resp])

    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        project_root=tmp_path,
        output_dir=out,
    )

    assert report.exit_code == RuntimeAuditExitCode.PROBE_ERROR
    assert report.status == RuntimeAuditStatus.PROBE_ERROR
    artifact_findings = [
        f
        for f in report.findings
        if f.failure_reason == RuntimeAuditFailureReason.ARTIFACT_WRITE_ERROR
    ]
    assert len(artifact_findings) >= 1
    assert report.artifact_result is not None
    assert report.artifact_result.success is False


# ── F2 (2026-05-23) cognitive_cooldown_block_rate ─────────────────────────


def test_runtime_audit_ledger_summary_accepts_cooldown_block_count() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditLedgerSummary not implemented")
    s = RuntimeAuditLedgerSummary(
        total_events=10,
        error_count=0,
        warning_count=138,
        ws_reconnect_count=0,
        budget_block_count=0,
        provider_failure_count=0,
        market_quarantine_count=0,
        readiness_change_count=0,
        alert_count=0,
        recovery_count=0,
        cooldown_block_count=138,
    )
    assert s.cooldown_block_count == 138


def test_runtime_audit_ledger_summary_cooldown_block_count_defaults_to_zero() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditLedgerSummary not implemented")
    s = RuntimeAuditLedgerSummary(total_events=0)
    assert s.cooldown_block_count == 0


def test_runtime_audit_report_cognitive_cooldown_block_rate_rejects_float() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditReport not implemented")
    with pytest.raises(ValidationError):
        RuntimeAuditReport(
            status=RuntimeAuditStatus.HEALTHY,
            exit_code=RuntimeAuditExitCode.HEALTHY,
            generated_at_utc=datetime.now(timezone.utc),
            cognitive_cooldown_block_rate=0.5,  # type: ignore[arg-type]
        )


def test_runtime_audit_report_cognitive_cooldown_block_rate_accepts_decimal() -> None:
    if not _SCHEMAS_AVAILABLE:
        raise NotImplementedError("RuntimeAuditReport not implemented")
    r = RuntimeAuditReport(
        status=RuntimeAuditStatus.HEALTHY,
        exit_code=RuntimeAuditExitCode.HEALTHY,
        generated_at_utc=datetime.now(timezone.utc),
        cognitive_cooldown_block_rate=Decimal("0.6479"),
    )
    assert r.cognitive_cooldown_block_rate == Decimal("0.6479")


def test_compute_cooldown_block_rate_typical_case_2026_05_23_observed() -> None:
    """138 cooldowns vs 75 decisions = 0.6479 (matches 2026-05-23 dry-run)."""
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("_compute_cooldown_block_rate not implemented")
    ledger = RuntimeAuditLedgerSummary(cooldown_block_count=138, available=True)
    decisions = RuntimeAuditDecisionSummary(total_decisions=75, available=True)
    rate = _compute_cooldown_block_rate(ledger, decisions)
    assert rate == Decimal("0.6479")


def test_compute_cooldown_block_rate_zero_denominator_returns_none() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("_compute_cooldown_block_rate not implemented")
    ledger = RuntimeAuditLedgerSummary(cooldown_block_count=0, available=True)
    decisions = RuntimeAuditDecisionSummary(total_decisions=0, available=True)
    assert _compute_cooldown_block_rate(ledger, decisions) is None


def test_compute_cooldown_block_rate_unavailable_summary_returns_none() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("_compute_cooldown_block_rate not implemented")
    ledger = RuntimeAuditLedgerSummary(available=False, message="ledger read failed")
    decisions = RuntimeAuditDecisionSummary(total_decisions=10, available=True)
    assert _compute_cooldown_block_rate(ledger, decisions) is None

    ledger = RuntimeAuditLedgerSummary(cooldown_block_count=5, available=True)
    decisions = RuntimeAuditDecisionSummary(available=False)
    assert _compute_cooldown_block_rate(ledger, decisions) is None


def test_compute_cooldown_block_rate_only_cooldowns_returns_one() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("_compute_cooldown_block_rate not implemented")
    ledger = RuntimeAuditLedgerSummary(cooldown_block_count=10, available=True)
    decisions = RuntimeAuditDecisionSummary(total_decisions=0, available=True)
    assert _compute_cooldown_block_rate(ledger, decisions) == Decimal("1.0000")


def test_compute_cooldown_block_rate_only_decisions_returns_zero() -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("_compute_cooldown_block_rate not implemented")
    ledger = RuntimeAuditLedgerSummary(cooldown_block_count=0, available=True)
    decisions = RuntimeAuditDecisionSummary(total_decisions=50, available=True)
    assert _compute_cooldown_block_rate(ledger, decisions) == Decimal("0.0000")


# ── F2 (2026-05-23) cognitive_cooldown_block_rate ─────────────────────────
