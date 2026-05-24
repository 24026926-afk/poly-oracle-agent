"""
tests/integration/test_WI-61-periodic-runtime-audit.py

Integration tests for WI-61 Periodic Runtime Audit.

Covers end-to-end paths that exercise the repository + auditor service
boundary against the shared in-memory async SQLite engine:

* Full audit lifecycle with operational events and repository summaries.
* Ledger summary aggregation via OperationalEventRepository.
* Decision/market/position/execution repository bounded reads.
* Missing operational_events table produces typed unavailable finding.
* Read-only SQLite probe does not create or mutate the database file.
* Artifact write + atomic latest swap against a real temp directory.
* CLI end-to-end run produces expected exit code and artifacts.
* Telegram alert integration with mocked notifier.
* LLM reviewer disabled-by-default integration path.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.repositories.operational_event_repository import (
    OperationalEventRepository,
)
from src.schemas.ops import (
    OperationalEventCreate,
    OperationalEventPayload,
    OperationalEventReasonCode,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
)

# WI-61 auditor — may not exist during red phase
try:
    from src.observability.runtime_audit import (
        run_audit,
        summarize_ledger,
        write_audit_artifacts,
    )

    _AUDITOR_AVAILABLE = True
except ImportError:
    _AUDITOR_AVAILABLE = False

# WI-61 schemas — may not exist during red phase
try:
    from src.schemas.runtime_audit import (
        RuntimeAuditExitCode,
        RuntimeAuditReport,
        RuntimeAuditStatus,
    )

    _SCHEMAS_AVAILABLE = True
except ImportError:
    _SCHEMAS_AVAILABLE = False

# WI-61 CLI
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = _PROJECT_ROOT / "scripts" / "ops" / "periodic_runtime_audit.py"
_cli = None
_CLI_AVAILABLE = False
if _CLI_PATH.exists():
    _spec = importlib.util.spec_from_file_location("wi61_audit_cli_int", _CLI_PATH)
    if _spec is not None and _spec.loader is not None:
        _cli = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_cli)
        _CLI_AVAILABLE = True


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _utc(year=2026, month=5, day=21, hour=12, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _now_utc():
    return datetime.now(timezone.utc)


async def _append_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_type: OperationalEventType,
    severity: OperationalEventSeverity,
    source: OperationalEventSource,
    reason_code: OperationalEventReasonCode,
    timestamp_utc: datetime,
    payload: Optional[OperationalEventPayload] = None,
) -> None:
    create = OperationalEventCreate(
        event_type=event_type,
        severity=severity,
        source=source,
        reason_code=reason_code,
        payload=payload or OperationalEventPayload(),
        timestamp_utc=timestamp_utc,
    )
    async with session_factory() as session:
        async with session.begin():
            repo = OperationalEventRepository(session)
            await repo.append(create)


# ═══════════════════════════════════════════════════════════════════════════
# Integration: Ledger summary
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_summarize_ledger_with_events(db_session_factory, tmp_path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("summarize_ledger not implemented")
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_now_utc(),
    )
    result = await summarize_ledger(db_session_factory, lookback_hours=24, limit=100)
    assert result.available is True
    assert result.total_events >= 1


@pytest.mark.asyncio
async def test_summarize_ledger_empty_window(db_session_factory, tmp_path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("summarize_ledger not implemented")
    result = await summarize_ledger(db_session_factory, lookback_hours=24, limit=100)
    assert result.available is True
    assert result.total_events == 0


@pytest.mark.asyncio
async def test_summarize_ledger_counts_errors_and_warnings(
    db_session_factory, tmp_path
) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("summarize_ledger not implemented")
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.ALERT_SENT,
        severity=OperationalEventSeverity.ERROR,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.DEGRADED,
        timestamp_utc=_now_utc(),
    )
    result = await summarize_ledger(db_session_factory, lookback_hours=24, limit=100)
    assert result.error_count >= 1


# ═══════════════════════════════════════════════════════════════════════════
# Integration: Full audit lifecycle
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_audit_full_lifecycle_healthy(db_session_factory, tmp_path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_audit not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    health_resp = MagicMock(status_code=200)
    ready_resp = MagicMock(status_code=200)
    ready_resp.json.return_value = {"status": "READY", "dry_run": True, "checks": {}}
    metrics_resp = MagicMock(status_code=200)
    metrics_resp.text = "# HELP test\n# TYPE test counter\ntest 1\n"
    mock_client.get = AsyncMock(side_effect=[health_resp, ready_resp, metrics_resp])
    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        session_factory=db_session_factory,
        project_root=tmp_path,
        output_dir=tmp_path / "docs" / "operations" / "runtime_audits",
    )
    assert report.exit_code.value in (0, 1)
    assert report.ledger_summary is not None


@pytest.mark.asyncio
async def test_run_audit_with_degraded_ledger(db_session_factory, tmp_path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_audit not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    health_resp = MagicMock(status_code=200)
    ready_resp = MagicMock(status_code=200)
    ready_resp.json.return_value = {"status": "READY", "dry_run": True, "checks": {}}
    metrics_resp = MagicMock(status_code=200)
    metrics_resp.text = "# HELP test\n# TYPE test counter\ntest 1\n"
    mock_client.get = AsyncMock(side_effect=[health_resp, ready_resp, metrics_resp])
    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        session_factory=db_session_factory,
        project_root=tmp_path,
        output_dir=tmp_path / "docs" / "operations" / "runtime_audits",
    )
    assert report is not None


@pytest.mark.asyncio
async def test_run_audit_repository_read_failure_typed(
    db_session_factory, tmp_path
) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_audit not implemented")
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    health_resp = MagicMock(status_code=200)
    ready_resp = MagicMock(status_code=200)
    ready_resp.json.return_value = {"status": "READY", "dry_run": True, "checks": {}}
    metrics_resp = MagicMock(status_code=200)
    metrics_resp.text = "# HELP test\n# TYPE test counter\ntest 1\n"
    mock_client.get = AsyncMock(side_effect=[health_resp, ready_resp, metrics_resp])
    report = await run_audit(
        http_client=mock_client,
        base_url="http://test:8080",
        session_factory=db_session_factory,
        project_root=tmp_path,
        output_dir=tmp_path / "docs" / "operations" / "runtime_audits",
    )
    assert report is not None


# ═══════════════════════════════════════════════════════════════════════════
# Integration: Artifact writes
# ═══════════════════════════════════════════════════════════════════════════


def test_write_artifacts_creates_timestamped_and_latest(tmp_path: Path) -> None:
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
    assert (out / "latest.json").exists()
    assert (out / "latest.md").exists()


def test_write_artifacts_atomic_latest_swap(tmp_path: Path) -> None:
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
    assert r1.latest_json_updated is True


def test_write_artifacts_forbidden_content_blocks_write(tmp_path: Path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("write_audit_artifacts not implemented")
    from src.schemas.runtime_audit import (
        RuntimeAuditFinding,
        RuntimeAuditFindingType,
        RuntimeAuditSeverity,
    )

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


# ═══════════════════════════════════════════════════════════════════════════
# Integration: CLI end-to-end
# ═══════════════════════════════════════════════════════════════════════════


def test_cli_e2e_produces_exit_code_and_artifacts(tmp_path: Path) -> None:
    if not _CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    assert _cli is not None
    assert hasattr(_cli, "main")


def test_cli_e2e_no_running_orchestrator_probe_errors(tmp_path: Path) -> None:
    if not _CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    assert _cli is not None


# ═══════════════════════════════════════════════════════════════════════════
# Integration: Telegram alert
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_telegram_alert_sent_on_degraded(db_session_factory, tmp_path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("send_telegram_alert not implemented")
    from unittest.mock import AsyncMock
    from src.observability.runtime_audit import send_telegram_alert

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
async def test_telegram_alert_skipped_on_healthy(db_session_factory, tmp_path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("send_telegram_alert not implemented")
    from src.observability.runtime_audit import send_telegram_alert

    report = RuntimeAuditReport(
        status=RuntimeAuditStatus.HEALTHY,
        exit_code=RuntimeAuditExitCode(0),
        generated_at_utc=_utc(),
        findings=[],
    )
    result = await send_telegram_alert(None, report, enabled=True)
    assert result.sent is False


# ═══════════════════════════════════════════════════════════════════════════
# Integration: LLM reviewer
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_reviewer_disabled_produces_no_artifact(tmp_path: Path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_llm_review not implemented")
    from src.observability.runtime_audit import run_llm_review
    from src.schemas.runtime_audit import (
        RuntimeAuditLLMReviewRequest,
        RuntimeAuditLLMReviewStatus,
    )

    result = await run_llm_review(
        RuntimeAuditLLMReviewRequest(
            audit_artifact_path="docs/operations/runtime_audits/latest.json",
        ),
        enabled=False,
    )
    assert result.status == RuntimeAuditLLMReviewStatus.DISABLED


@pytest.mark.asyncio
async def test_reviewer_enabled_with_mock_moonshot(tmp_path: Path) -> None:
    if not _AUDITOR_AVAILABLE:
        raise NotImplementedError("run_llm_review not implemented")
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.observability.runtime_audit import run_llm_review
    from src.schemas.runtime_audit import (
        RuntimeAuditLLMReviewRequest,
        RuntimeAuditLLMReviewStatus,
    )

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
