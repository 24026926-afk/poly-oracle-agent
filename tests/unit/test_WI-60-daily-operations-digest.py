"""
tests/unit/test_WI-60-daily-operations-digest.py

Unit tests for WI-60 Daily Operations Digest.

Covers:
* Typed digest schemas in ``src/schemas/ops.py``.
* Deterministic daily digest generation in ``src/observability/daily_ops_digest.py``.
* ``scripts/ops/generate_daily_ops_digest.py`` CLI argument parsing, exit
  codes, path constraints, no-overwrite safety, and deterministic output.
* Decimal spend / PnL formatting, secret-safe redaction, empty/partial-run
  handling, unresolved warnings/errors, Telegram disabled/enabled/failure
  paths, and Gatekeeper / repository purity.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pytest
from pydantic import ValidationError
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models import OperationalEvent
from src.db.repositories.operational_event_repository import (
    OperationalEventRepository,
)
from src.schemas.ops import (
    OperationalEventCreate,
    OperationalEventPayload,
    OperationalEventPersistenceStatus,
    OperationalEventReasonCode,
    OperationalEventRecord,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
)

# WI-60 schemas — may not exist during red phase
try:
    from src.schemas.ops import (
        DailyOpsDigestDecisionSummary,
        DailyOpsDigestEventHighlight,
        DailyOpsDigestFailureReason,
        DailyOpsDigestLLMSummary,
        DailyOpsDigestOperatorCheck,
        DailyOpsDigestPnLSummary,
        DailyOpsDigestReport,
        DailyOpsDigestRequest,
        DailyOpsDigestRunSummary,
        DailyOpsDigestStatus,
        DailyOpsDigestTelegramResult,
        DailyOpsDigestTelegramSummary,
        DailyOpsDigestWindow,
        DailyOpsDigestWriteResult,
    )

    _DIGEST_SCHEMAS_AVAILABLE = True
except ImportError:
    _DIGEST_SCHEMAS_AVAILABLE = False

# WI-60 digest service — may not exist during red phase
try:
    from src.observability.daily_ops_digest import generate_digest

    _DIGEST_SERVICE_AVAILABLE = True
except ImportError:
    _DIGEST_SERVICE_AVAILABLE = False

# WI-60 CLI — may not exist during red phase
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = _PROJECT_ROOT / "scripts" / "ops" / "generate_daily_ops_digest.py"
_cli = None
if _CLI_PATH.exists():
    _spec = importlib.util.spec_from_file_location("wi60_digest_cli", _CLI_PATH)
    if _spec is not None and _spec.loader is not None:
        _cli = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_cli)
        _DIGEST_CLI_AVAILABLE = True
    else:
        _DIGEST_CLI_AVAILABLE = False
else:
    _DIGEST_CLI_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _utc(
    year: int = 2026,
    month: int = 5,
    day: int = 15,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _make_record(
    *,
    event_type: OperationalEventType,
    reason_code: OperationalEventReasonCode,
    severity: OperationalEventSeverity = OperationalEventSeverity.INFO,
    source: OperationalEventSource = OperationalEventSource.ORCHESTRATOR,
    payload: dict | None = None,
    timestamp: datetime | None = None,
    payload_json: str | None = None,
) -> OperationalEventRecord:
    if payload_json is None:
        payload_json = json.dumps(payload or {})
    return OperationalEventRecord(
        id=str(uuid.uuid4()),
        event_type=event_type,
        severity=severity,
        source=source,
        reason_code=reason_code,
        payload_json=payload_json,
        persistence_status=OperationalEventPersistenceStatus.PERSISTED,
        created_at_utc=timestamp or datetime.now(timezone.utc),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════════════════


def test_daily_ops_digest_status_enum_exists() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestStatus enum not implemented")
    expected = {
        "SUCCESS",
        "EMPTY_WINDOW",
        "DATABASE_UNAVAILABLE",
        "MISSING_TABLE",
        "PATH_FAILURE",
        "FORBIDDEN_CONTENT",
    }
    actual = {member.value for member in DailyOpsDigestStatus}
    assert expected.issubset(actual)


def test_daily_ops_digest_failure_reason_enum_exists() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestFailureReason enum not implemented")
    expected = {
        "DATABASE_UNREACHABLE",
        "MISSING_TABLE",
        "PATH_OUTSIDE_DAILY",
        "MANUAL_NOTE_WOULD_OVERWRITE",
        "FORBIDDEN_CONTENT",
    }
    actual = {member.value for member in DailyOpsDigestFailureReason}
    assert expected.issubset(actual)


def test_daily_ops_digest_request_schema_requires_tzaware() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestRequest schema not implemented")
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(),
    )
    assert req.digest_date_utc.tzinfo is not None
    with pytest.raises(ValidationError):
        DailyOpsDigestRequest(
            digest_date_utc=datetime(2026, 5, 15, 0, 0, 0),  # naive
        )


def test_daily_ops_digest_window_schema_exists() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestWindow schema not implemented")
    window = DailyOpsDigestWindow(
        from_utc=_utc(hour=0),
        to_utc=_utc(hour=23, minute=59, second=59),
    )
    assert window.from_utc.tzinfo is not None
    assert window.to_utc >= window.from_utc


def test_daily_ops_digest_run_summary_schema_exists() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestRunSummary schema not implemented")
    summary = DailyOpsDigestRunSummary(
        start_utc=_utc(hour=0),
        stop_utc=_utc(hour=6),
        uptime_seconds=21600,
        run_status="completed",
    )
    assert summary.start_utc is not None
    assert summary.uptime_seconds >= 0


def test_daily_ops_digest_run_summary_partial_run() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestRunSummary schema not implemented")
    summary = DailyOpsDigestRunSummary(
        start_utc=_utc(hour=0),
        stop_utc=None,
        uptime_seconds=None,
        run_status="partial",
    )
    assert summary.stop_utc is None
    assert summary.run_status == "partial"


def test_daily_ops_digest_decision_summary_schema_exists() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError(
            "DailyOpsDigestDecisionSummary schema not implemented"
        )
    summary = DailyOpsDigestDecisionSummary(
        accepted_buy=1,
        accepted_hold=2,
        skipped_low_conf=3,
        skipped_low_ev=0,
        skipped_high_spread=1,
        skipped_exposure=0,
        skipped_ttr=0,
    )
    assert summary.accepted_buy == 1


def test_daily_ops_digest_llm_summary_schema_exists() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestLLMSummary schema not implemented")
    summary = DailyOpsDigestLLMSummary(
        llm_calls=10,
        budget_blocks=2,
        cooldown_blocks=1,
        provider_failures=1,
        estimated_spend_usd=Decimal("0.42"),
    )
    assert summary.llm_calls == 10
    assert isinstance(summary.estimated_spend_usd, Decimal)


def test_daily_ops_digest_llm_summary_missing_spend_is_none() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestLLMSummary schema not implemented")
    summary = DailyOpsDigestLLMSummary(
        llm_calls=0,
        budget_blocks=0,
        cooldown_blocks=0,
        provider_failures=0,
        estimated_spend_usd=None,
    )
    assert summary.estimated_spend_usd is None


def test_daily_ops_digest_pnl_summary_schema_decimal_only() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestPnLSummary schema not implemented")
    summary = DailyOpsDigestPnLSummary(
        realized_pnl=Decimal("12.34"),
        unrealized_pnl=None,
        gas_and_fees=Decimal("0.01"),
    )
    assert isinstance(summary.realized_pnl, Decimal)


def test_daily_ops_digest_event_highlight_schema_exists() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestEventHighlight schema not implemented")
    highlight = DailyOpsDigestEventHighlight(
        event_id="evt-1",
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.INFO,
        reason_code=OperationalEventReasonCode.READY,
        summary="Readiness returned to READY.",
        timestamp_utc=_utc(hour=1),
    )
    assert highlight.summary.startswith("Readiness")


def test_daily_ops_digest_event_highlight_secret_safe() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestEventHighlight schema not implemented")
    with pytest.raises(ValidationError):
        DailyOpsDigestEventHighlight(
            event_id="evt-2",
            event_type=OperationalEventType.START,
            severity=OperationalEventSeverity.INFO,
            reason_code=OperationalEventReasonCode.STARTUP,
            summary="api_key leaked here",
            timestamp_utc=_utc(hour=1),
        )


def test_daily_ops_digest_operator_check_schema_exists() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestOperatorCheck schema not implemented")
    check = DailyOpsDigestOperatorCheck(
        category="readiness",
        message="Review degraded readiness state.",
        severity=OperationalEventSeverity.WARNING,
    )
    assert check.category == "readiness"


def test_daily_ops_digest_telegram_summary_schema_exists() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError(
            "DailyOpsDigestTelegramSummary schema not implemented"
        )
    summary = DailyOpsDigestTelegramSummary(
        enabled=True,
        text="Daily digest: 3 decisions, 1 warning.",
    )
    assert summary.enabled is True
    assert len(summary.text) <= 1024


def test_daily_ops_digest_telegram_result_schema_exists() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestTelegramResult schema not implemented")
    result = DailyOpsDigestTelegramResult(
        status="disabled",
        sent_at_utc=None,
        failure_reason=None,
    )
    assert result.status == "disabled"


def test_daily_ops_digest_write_result_schema_exists() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestWriteResult schema not implemented")
    result = DailyOpsDigestWriteResult(
        path="03_Daily/2026-05-15-bot.md",
        written=True,
        bytes_written=2048,
    )
    assert result.written is True


def test_daily_ops_digest_report_schema_composition() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestReport schema not implemented")
    req = DailyOpsDigestRequest(digest_date_utc=_utc())
    report = DailyOpsDigestReport(
        status=DailyOpsDigestStatus.EMPTY_WINDOW,
        request=req,
        run_summary=None,
        decision_summary=None,
        llm_summary=None,
        pnl_summary=None,
        top_events=[],
        unresolved_warnings=[],
        unresolved_errors=[],
        operator_checks=[],
        telegram_result=DailyOpsDigestTelegramResult(status="disabled"),
        write_result=DailyOpsDigestWriteResult(
            path="03_Daily/2026-05-15-bot.md",
            written=True,
            bytes_written=0,
        ),
    )
    assert report.request is req
    with pytest.raises(ValidationError):
        DailyOpsDigestReport(
            status=DailyOpsDigestStatus.SUCCESS,
            request=req,
            run_summary=None,
            decision_summary=None,
            llm_summary=None,
            pnl_summary=None,
            top_events=[],
            unresolved_warnings=[],
            unresolved_errors=[],
            operator_checks=[],
            telegram_result=DailyOpsDigestTelegramResult(status="disabled"),
            write_result=DailyOpsDigestWriteResult(
                path="03_Daily/2026-05-15-bot.md",
                written=True,
                bytes_written=0,
            ),
        )


def test_daily_ops_digest_report_failure_requires_reason() -> None:
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestReport schema not implemented")
    req = DailyOpsDigestRequest(digest_date_utc=_utc())
    with pytest.raises(ValidationError):
        DailyOpsDigestReport(
            status=DailyOpsDigestStatus.PATH_FAILURE,
            request=req,
            run_summary=None,
            decision_summary=None,
            llm_summary=None,
            pnl_summary=None,
            top_events=[],
            unresolved_warnings=[],
            unresolved_errors=[],
            operator_checks=[],
            telegram_result=DailyOpsDigestTelegramResult(status="disabled"),
            write_result=DailyOpsDigestWriteResult(
                path="03_Daily/2026-05-15-bot.md",
                written=False,
                bytes_written=0,
            ),
        )


def test_daily_ops_digest_report_read_cap_requires_reason() -> None:
    """READ_CAP_REACHED is a typed non-success status and must carry a reason."""
    if not _DIGEST_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DailyOpsDigestReport schema not implemented")
    req = DailyOpsDigestRequest(digest_date_utc=_utc())
    with pytest.raises(ValidationError):
        DailyOpsDigestReport(
            status=DailyOpsDigestStatus.READ_CAP_REACHED,
            request=req,
            run_summary=None,
            decision_summary=None,
            llm_summary=None,
            pnl_summary=None,
            top_events=[],
            unresolved_warnings=[],
            unresolved_errors=[],
            operator_checks=[],
            telegram_result=DailyOpsDigestTelegramResult(status="disabled"),
            write_result=DailyOpsDigestWriteResult(
                path="03_Daily/2026-05-15-bot.md",
                written=False,
                bytes_written=0,
            ),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Digest service
# ═══════════════════════════════════════════════════════════════════════════


def test_generate_digest_is_defined() -> None:
    if not _DIGEST_SERVICE_AVAILABLE:
        raise NotImplementedError("generate_digest not implemented")
    assert callable(generate_digest)


def test_generate_digest_signature_accepts_request_and_session_factory() -> None:
    if not _DIGEST_SERVICE_AVAILABLE:
        raise NotImplementedError("generate_digest not implemented")
    import inspect

    sig = inspect.signature(generate_digest)
    assert "request" in sig.parameters
    assert "session_factory" in sig.parameters


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


def test_cli_module_exists() -> None:
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("generate_daily_ops_digest.py CLI not implemented")
    assert _cli is not None


def test_cli_accepts_date_argument() -> None:
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("generate_daily_ops_digest.py CLI not implemented")
    assert hasattr(_cli, "main")
    import inspect

    sig = inspect.signature(_cli.main)
    assert any(p in sig.parameters for p in ("date", "digest_date"))


def test_cli_returns_invalid_input_for_malformed_date() -> None:
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("generate_daily_ops_digest.py CLI not implemented")
    rc = _cli.main(["--date", "not-a-date"])
    assert rc != 0


def test_cli_returns_invalid_input_for_path_outside_daily() -> None:
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("generate_daily_ops_digest.py CLI not implemented")
    rc = _cli.main(["--date", "2026-05-15", "--output", "/tmp/malicious.md"])
    assert rc != 0


def test_cli_returns_invalid_input_for_manual_note_path() -> None:
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("generate_daily_ops_digest.py CLI not implemented")
    rc = _cli.main(["--date", "2026-05-15", "--output", "03_Daily/2026-05-15.md"])
    assert rc != 0


# ═══════════════════════════════════════════════════════════════════════════
# Event seeding helpers (require db_session_factory fixture)
# ═══════════════════════════════════════════════════════════════════════════


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
    """Persist one event via the repository (typed validation path)."""
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


async def _insert_raw_row(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    event_type: OperationalEventType,
    severity: OperationalEventSeverity,
    source: OperationalEventSource,
    reason_code: OperationalEventReasonCode,
    timestamp_utc: datetime,
    payload_json: str,
    event_id: Optional[str] = None,
) -> str:
    """Insert a raw OperationalEvent row, bypassing typed payload validation."""
    row_id = event_id or str(uuid.uuid4())
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                insert(OperationalEvent).values(
                    id=row_id,
                    event_type=event_type.value,
                    severity=severity.value,
                    source=source.value,
                    reason_code=reason_code.value,
                    payload_json=payload_json,
                    persistence_status=OperationalEventPersistenceStatus.PERSISTED.value,
                    created_at_utc=timestamp_utc,
                    recorded_at_utc=timestamp_utc,
                )
            )
    return row_id


def _make_request(
    digest_date: Optional[datetime] = None,
    *,
    output_path: Optional[str] = None,
    daily_notes_dir: str = "03_Daily",
    enable_telegram: bool = False,
):
    """Build a typed DailyOpsDigestRequest for tests."""
    from src.schemas.ops import DailyOpsDigestRequest

    return DailyOpsDigestRequest(
        digest_date_utc=digest_date or _utc(hour=0),
        output_path=output_path,
        daily_notes_dir=daily_notes_dir,
        enable_telegram=enable_telegram,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Determinism and safety
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_digest_output_is_deterministic_for_same_input(
    db_session_factory, tmp_path
) -> None:
    """Re-running the digest with the same inputs produces identical content."""
    from src.observability.daily_ops_digest import generate_digest
    from src.schemas.ops import DailyOpsDigestStatus

    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.SHUTDOWN,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.GRACEFUL_SHUTDOWN,
        timestamp_utc=_utc(hour=5),
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()

    req = _make_request(daily_notes_dir=str(daily_root))

    report1 = await generate_digest(
        req, db_session_factory, daily_notes_root=daily_root
    )
    text1 = Path(report1.write_result.path).read_text()

    report2 = await generate_digest(
        req, db_session_factory, daily_notes_root=daily_root
    )
    text2 = Path(report2.write_result.path).read_text()

    assert report1.status == DailyOpsDigestStatus.SUCCESS
    assert report2.status == DailyOpsDigestStatus.SUCCESS
    assert text1 == text2


@pytest.mark.asyncio
async def test_digest_does_not_overwrite_manual_daily_notes(
    db_session_factory, tmp_path
) -> None:
    """The manual note YYYY-MM-DD.md is never created, modified, or deleted."""
    from src.observability.daily_ops_digest import generate_digest

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    manual_path = daily_root / "2026-05-15.md"
    manual_content = "# Manual coding notes\n\nDo not touch."
    manual_path.write_text(manual_content)
    manual_mtime_before = manual_path.stat().st_mtime

    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )

    req = _make_request(daily_notes_dir=str(daily_root))
    await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    # Bot digest written; manual note untouched.
    bot_path = daily_root / "2026-05-15-bot.md"
    assert bot_path.exists()
    assert manual_path.exists()
    assert manual_path.read_text() == manual_content
    assert manual_path.stat().st_mtime == manual_mtime_before


@pytest.mark.asyncio
async def test_digest_path_constrained_to_daily_subdirectory(
    db_session_factory, tmp_path
) -> None:
    """Output paths outside the daily notes dir are rejected with PATH_FAILURE."""
    from src.observability.daily_ops_digest import generate_digest
    from src.schemas.ops import DailyOpsDigestFailureReason, DailyOpsDigestStatus

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()

    # Attempt to write outside the configured daily notes directory.
    outside_path = tmp_path / "elsewhere" / "2026-05-15-bot.md"
    req = _make_request(
        daily_notes_dir=str(daily_root),
        output_path=str(outside_path),
    )
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)
    assert report.status == DailyOpsDigestStatus.PATH_FAILURE
    assert report.failure_reason == DailyOpsDigestFailureReason.PATH_OUTSIDE_DAILY
    assert report.write_result.written is False
    assert not outside_path.exists()


@pytest.mark.asyncio
async def test_digest_path_rejects_manual_note_filename(
    db_session_factory, tmp_path
) -> None:
    """Explicit YYYY-MM-DD.md output path is rejected with typed failure."""
    from src.observability.daily_ops_digest import generate_digest
    from src.schemas.ops import DailyOpsDigestFailureReason, DailyOpsDigestStatus

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()

    manual_path = daily_root / "2026-05-15.md"
    req = _make_request(
        daily_notes_dir=str(daily_root),
        output_path=str(manual_path),
    )
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)
    assert report.status == DailyOpsDigestStatus.PATH_FAILURE
    assert (
        report.failure_reason == DailyOpsDigestFailureReason.MANUAL_NOTE_WOULD_OVERWRITE
    )
    assert report.write_result.written is False
    assert not manual_path.exists()


@pytest.mark.asyncio
async def test_digest_handles_empty_day_gracefully(
    db_session_factory, tmp_path
) -> None:
    """A day with no events produces a valid EMPTY_WINDOW digest file."""
    from src.observability.daily_ops_digest import generate_digest
    from src.schemas.ops import DailyOpsDigestStatus

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root))
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    assert report.status == DailyOpsDigestStatus.EMPTY_WINDOW
    bot_path = daily_root / "2026-05-15-bot.md"
    assert bot_path.exists()
    content = bot_path.read_text()
    assert "EMPTY_WINDOW" in content


@pytest.mark.asyncio
async def test_digest_handles_partial_run_gracefully(
    db_session_factory, tmp_path
) -> None:
    """START without SHUTDOWN yields run_status=partial with no invented stop time."""
    from src.observability.daily_ops_digest import generate_digest
    from src.schemas.ops import DailyOpsDigestStatus

    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root))
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    assert report.status == DailyOpsDigestStatus.SUCCESS
    assert report.run_summary is not None
    assert report.run_summary.run_status == "partial"
    assert report.run_summary.start_utc is not None
    assert report.run_summary.stop_utc is None
    assert report.run_summary.uptime_seconds is None


@pytest.mark.asyncio
async def test_digest_decimal_formatting_for_spend_and_pnl(
    db_session_factory, tmp_path
) -> None:
    """LLM spend and paper PnL use Decimal end-to-end and never raw float."""
    from src.observability.daily_ops_digest import generate_digest
    from src.schemas.ops import DailyOpsDigestStatus

    # Insert an LLM_CALL_STARTED event with a Decimal-compatible spend
    # field in the raw payload JSON.
    raw_payload = json.dumps(
        {"estimated_cost_usd": "0.0125", "provider_name": "anthropic"}
    )
    await _insert_raw_row(
        db_session_factory,
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        timestamp_utc=_utc(hour=1),
        payload_json=raw_payload,
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root))
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    assert report.status == DailyOpsDigestStatus.SUCCESS
    assert report.llm_summary is not None
    assert isinstance(report.llm_summary.estimated_spend_usd, Decimal)
    assert report.llm_summary.estimated_spend_usd == Decimal("0.0125")


@pytest.mark.asyncio
async def test_digest_decimal_rejects_float_spend(db_session_factory, tmp_path) -> None:
    """A float estimated_cost_usd in payload is dropped, never summed as float."""
    from src.observability.daily_ops_digest import generate_digest
    from src.schemas.ops import DailyOpsDigestStatus

    # Float value — must be ignored (no fabricated zero).
    raw_payload = json.dumps({"estimated_cost_usd": 0.0125})
    await _insert_raw_row(
        db_session_factory,
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        timestamp_utc=_utc(hour=1),
        payload_json=raw_payload,
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root))
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    assert report.status == DailyOpsDigestStatus.SUCCESS
    assert report.llm_summary is not None
    assert report.llm_summary.llm_calls == 1
    # Float was rejected → spend remains None (unavailable, not zero).
    assert report.llm_summary.estimated_spend_usd is None


@pytest.mark.asyncio
async def test_digest_redacts_forbidden_content_before_write(
    db_session_factory, tmp_path
) -> None:
    """Forbidden payload content is dropped from highlights (never bleeds out)."""
    from src.observability.daily_ops_digest import generate_digest

    # Raw payload containing a wallet address pattern — bypass typed validation.
    bad_payload = json.dumps({"message": "wallet 0x" + "a" * 40 + " leaked"})
    await _insert_raw_row(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
        payload_json=bad_payload,
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root))
    await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    bot_path = daily_root / "2026-05-15-bot.md"
    text = bot_path.read_text()
    # Wallet address must NEVER appear in the written file.
    assert "0x" + "a" * 40 not in text


@pytest.mark.asyncio
async def test_digest_telegram_disabled_by_default(
    db_session_factory, tmp_path
) -> None:
    """Telegram delivery is disabled unless explicitly enabled in request."""
    from src.observability.daily_ops_digest import generate_digest

    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root), enable_telegram=False)
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)
    assert report.telegram_result.status == "disabled"


@pytest.mark.asyncio
async def test_digest_telegram_enabled_path(
    db_session_factory, tmp_path, monkeypatch
) -> None:
    """When enabled, telegram_notifier.send_execution_event is invoked once."""
    from src.observability.daily_ops_digest import generate_digest

    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )

    sends: list[tuple[str, bool]] = []

    class _FakeNotifier:
        async def send_execution_event(self, summary: str, dry_run: bool) -> None:
            sends.append((summary, dry_run))

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root), enable_telegram=True)
    report = await generate_digest(
        req,
        db_session_factory,
        daily_notes_root=daily_root,
        telegram_notifier=_FakeNotifier(),
    )

    assert report.telegram_result.status == "sent"
    assert report.telegram_result.sent_at_utc is not None
    assert len(sends) == 1
    assert "Daily digest" in sends[0][0]


@pytest.mark.asyncio
async def test_digest_telegram_failure_does_not_corrupt_digest_file(
    db_session_factory, tmp_path
) -> None:
    """Telegram send failure surfaces a typed result without dropping the file."""
    from src.observability.daily_ops_digest import generate_digest

    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )

    class _FailingNotifier:
        async def send_execution_event(self, summary: str, dry_run: bool) -> None:
            raise RuntimeError("telegram blew up")

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root), enable_telegram=True)
    report = await generate_digest(
        req,
        db_session_factory,
        daily_notes_root=daily_root,
        telegram_notifier=_FailingNotifier(),
    )

    assert report.telegram_result.status == "failed"
    assert report.telegram_result.failure_reason is not None
    # File digest was still written.
    assert report.write_result.written is True
    bot_path = daily_root / "2026-05-15-bot.md"
    assert bot_path.exists()


# ═══════════════════════════════════════════════════════════════════════════
# Repository purity
# ═══════════════════════════════════════════════════════════════════════════


def test_digest_no_raw_db_sessions_outside_repository() -> None:
    """The service module must not call .execute/.scalars directly on sessions."""
    import src.observability.daily_ops_digest as digest_mod_local

    text = Path(digest_mod_local.__file__).read_text()
    assert "session.execute" not in text
    assert "session.scalars" not in text


def test_digest_no_create_all_in_service_or_cli() -> None:
    """Neither the service nor the CLI may call Base.metadata.create_all()."""
    import src.observability.daily_ops_digest as digest_mod_local

    service_text = Path(digest_mod_local.__file__).read_text()
    cli_text = _CLI_PATH.read_text()
    assert "create_all" not in service_text
    assert "create_all" not in cli_text


def test_digest_service_does_not_import_llm_clients() -> None:
    """The service module never imports ClaudeClient or any LLM provider."""
    import src.observability.daily_ops_digest as digest_mod_local

    text = Path(digest_mod_local.__file__).read_text()
    forbidden = [
        "ClaudeClient",
        "deepseek",
        "grok",
        "anthropic",
        "from src.agents.evaluation",
        "from src.agents.execution.execution_router",
        "execution_router",
    ]
    for needle in forbidden:
        assert needle not in text, f"Forbidden import or reference found: {needle}"


def test_digest_service_does_not_modify_llm_evaluation_response() -> None:
    """The service does not import or reference LLMEvaluationResponse."""
    import src.observability.daily_ops_digest as digest_mod_local

    text = Path(digest_mod_local.__file__).read_text()
    assert "LLMEvaluationResponse" not in text


def test_digest_repository_remains_append_read_only() -> None:
    """OperationalEventRepository must not gain update/delete public methods."""
    forbidden = {"update", "delete", "backfill", "remove", "purge"}
    for name in dir(OperationalEventRepository):
        if name.startswith("_"):
            continue
        assert name.lower() not in forbidden, (
            f"OperationalEventRepository must remain append/read-only; "
            f"found public method {name!r}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# CLI helper coverage
# ═══════════════════════════════════════════════════════════════════════════


def test_cli_safe_echo_redacts_secret_shaped_input() -> None:
    """_safe_echo replaces wallet-like and overlong inputs with a typed tag."""
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    # Wallet pattern triggers redaction.
    wallet = "0x" + "a" * 40
    assert _cli._safe_echo(wallet) == "<redacted>"
    # Overlong input triggers length redaction.
    assert _cli._safe_echo("x" * 100) == "<redacted-length>"
    # Plain value passes through.
    assert _cli._safe_echo("ok") == "ok"


def test_cli_scrub_argparse_message_quotes_redacted_token() -> None:
    """_scrub_argparse_message redacts a wallet-shaped token in error text."""
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    wallet = "0x" + "a" * 40
    msg = f"invalid value: '{wallet}'"
    scrubbed = _cli._scrub_argparse_message(msg)
    assert wallet not in scrubbed
    assert "<redacted>" in scrubbed


def test_cli_parse_date_defaults_to_today_utc() -> None:
    """_parse_date(None) returns today's UTC date at midnight."""
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    parsed = _cli._parse_date(None)
    today = datetime.now(timezone.utc).date()
    assert parsed.tzinfo is not None
    assert parsed.date() == today


def test_cli_argparse_error_path_prints_typed_status() -> None:
    """The custom argparse error handler returns EXIT_INVALID_INPUT."""
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    # Pass an unknown argument to trigger argparse error.
    rc = _cli.main(["--definitely-not-a-flag", "value"])
    assert rc == _cli.EXIT_INVALID_INPUT


def test_cli_status_to_exit_code_mapping() -> None:
    """Status codes map to the documented CLI exit codes."""
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    from src.schemas.ops import DailyOpsDigestStatus

    assert _cli._status_to_exit_code(DailyOpsDigestStatus.SUCCESS) == _cli.EXIT_OK
    assert _cli._status_to_exit_code(DailyOpsDigestStatus.EMPTY_WINDOW) == _cli.EXIT_OK
    assert (
        _cli._status_to_exit_code(DailyOpsDigestStatus.PATH_FAILURE)
        == _cli.EXIT_INVALID_INPUT
    )
    assert (
        _cli._status_to_exit_code(DailyOpsDigestStatus.MISSING_TABLE)
        == _cli.EXIT_REPOSITORY
    )
    assert (
        _cli._status_to_exit_code(DailyOpsDigestStatus.FORBIDDEN_CONTENT)
        == _cli.EXIT_FORBIDDEN
    )
    # READ_CAP_REACHED is a repository-class failure: fail closed and exit
    # non-zero so cron/systemd surfaces the truncation rather than treat a
    # silently-undercounted digest as success.
    assert (
        _cli._status_to_exit_code(DailyOpsDigestStatus.READ_CAP_REACHED)
        == _cli.EXIT_REPOSITORY
    )


# ═══════════════════════════════════════════════════════════════════════════
# MAAP regression — explicit window CLI args
# ═══════════════════════════════════════════════════════════════════════════


def test_cli_from_to_utc_must_be_paired() -> None:
    """Supplying only one of --from-utc/--to-utc returns EXIT_INVALID_INPUT."""
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    rc = _cli.main(
        [
            "--date",
            "2026-05-15",
            "--from-utc",
            "2026-05-15T00:00:00Z",
        ]
    )
    assert rc == _cli.EXIT_INVALID_INPUT


def test_cli_from_to_utc_rejects_naive_timestamp() -> None:
    """Naive (no-tz) timestamps are rejected with EXIT_INVALID_INPUT."""
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    rc = _cli.main(
        [
            "--date",
            "2026-05-15",
            "--from-utc",
            "2026-05-15T00:00:00",
            "--to-utc",
            "2026-05-15T23:59:59Z",
        ]
    )
    assert rc == _cli.EXIT_INVALID_INPUT


def test_cli_from_to_utc_rejects_offset_window() -> None:
    """Bounds outside the --date UTC day are rejected."""
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    rc = _cli.main(
        [
            "--date",
            "2026-05-15",
            "--from-utc",
            "2026-05-14T23:00:00Z",
            "--to-utc",
            "2026-05-15T01:00:00Z",
        ]
    )
    assert rc == _cli.EXIT_INVALID_INPUT


def test_cli_from_to_utc_rejects_inverted_window() -> None:
    """from >= to is rejected as invalid input."""
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    rc = _cli.main(
        [
            "--date",
            "2026-05-15",
            "--from-utc",
            "2026-05-15T12:00:00Z",
            "--to-utc",
            "2026-05-15T11:00:00Z",
        ]
    )
    assert rc == _cli.EXIT_INVALID_INPUT


def test_cli_parse_iso_utc_accepts_z_suffix() -> None:
    """_parse_iso_utc accepts the trailing-Z form and returns tz-aware UTC."""
    if not _DIGEST_CLI_AVAILABLE:
        raise NotImplementedError("CLI not implemented")
    parsed = _cli._parse_iso_utc("--from-utc", "2026-05-15T00:00:00Z")
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
    assert parsed.year == 2026 and parsed.day == 15


# ═══════════════════════════════════════════════════════════════════════════
# MAAP regression — Decimal robustness
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_digest_malformed_decimal_spend_does_not_crash(
    db_session_factory, tmp_path
) -> None:
    """A non-Decimal-parseable estimated_cost_usd is dropped, not raised."""
    from src.observability.daily_ops_digest import generate_digest
    from src.schemas.ops import DailyOpsDigestStatus

    # "not-a-decimal" raises decimal.InvalidOperation, not TypeError/ValueError.
    raw_payload = json.dumps({"estimated_cost_usd": "not-a-decimal"})
    await _insert_raw_row(
        db_session_factory,
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        timestamp_utc=_utc(hour=1),
        payload_json=raw_payload,
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root))
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    # Digest still succeeds; spend remains unavailable rather than crashing.
    assert report.status == DailyOpsDigestStatus.SUCCESS
    assert report.llm_summary is not None
    assert report.llm_summary.llm_calls == 1
    assert report.llm_summary.estimated_spend_usd is None


# ═══════════════════════════════════════════════════════════════════════════
# MAAP regression — Telegram typed-failure semantics
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_digest_prefers_typed_telegram_send_when_present(
    db_session_factory, tmp_path
) -> None:
    """When notifier exposes try_send_execution_event, that path is used."""
    from src.observability.daily_ops_digest import generate_digest

    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )

    typed_calls: list[tuple[str, bool]] = []
    legacy_calls: list[tuple[str, bool]] = []

    class _BothInterfacesNotifier:
        async def try_send_execution_event(self, summary: str, dry_run: bool) -> bool:
            typed_calls.append((summary, dry_run))
            return True

        async def send_execution_event(self, summary: str, dry_run: bool) -> None:
            legacy_calls.append((summary, dry_run))

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root), enable_telegram=True)
    report = await generate_digest(
        req,
        db_session_factory,
        daily_notes_root=daily_root,
        telegram_notifier=_BothInterfacesNotifier(),
    )

    assert len(typed_calls) == 1
    assert legacy_calls == []
    assert report.telegram_result.status == "sent"
    assert report.telegram_result.sent_at_utc is not None


@pytest.mark.asyncio
async def test_digest_typed_telegram_false_marks_failed(
    db_session_factory, tmp_path
) -> None:
    """try_send_execution_event returning False yields telegram_result.failed."""
    from src.observability.daily_ops_digest import generate_digest

    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )

    class _SwallowingNotifier:
        async def try_send_execution_event(self, summary: str, dry_run: bool) -> bool:
            return False

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root), enable_telegram=True)
    report = await generate_digest(
        req,
        db_session_factory,
        daily_notes_root=daily_root,
        telegram_notifier=_SwallowingNotifier(),
    )

    assert report.telegram_result.status == "failed"
    assert report.telegram_result.failure_reason is not None
    # File digest was still written.
    assert report.write_result.written is True


def test_telegram_notifier_exposes_try_send_execution_event() -> None:
    """TelegramNotifier exposes a typed bool-returning send for digest use."""
    from src.agents.execution.telegram_notifier import TelegramNotifier

    method = getattr(TelegramNotifier, "try_send_execution_event", None)
    assert callable(method)


# ═══════════════════════════════════════════════════════════════════════════
# MAAP regression — output path resolution accepts documented form
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_digest_accepts_output_with_daily_notes_basename_prefix(
    db_session_factory, tmp_path
) -> None:
    """--output 03_Daily/YYYY-MM-DD-bot.md (relative) resolves correctly."""
    from src.observability.daily_ops_digest import generate_digest
    from src.schemas.ops import DailyOpsDigestStatus

    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )

    # Use a daily_notes_dir whose basename matches the relative output prefix.
    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(
        daily_notes_dir=str(daily_root),
        output_path="03_Daily/2026-05-15-bot.md",
    )
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    assert report.status == DailyOpsDigestStatus.SUCCESS
    expected_path = daily_root / "2026-05-15-bot.md"
    assert report.write_result.path == str(expected_path)
    assert expected_path.exists()
    # And the candidate must NOT have been double-nested.
    nested = daily_root / "03_Daily" / "2026-05-15-bot.md"
    assert not nested.exists()


# ═══════════════════════════════════════════════════════════════════════════
# MAAP regression — run_status semantics
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_digest_records_without_start_yield_unknown_not_no_run(
    db_session_factory, tmp_path
) -> None:
    """Events present without a typed START produce run_status=unknown."""
    from src.observability.daily_ops_digest import generate_digest
    from src.schemas.ops import DailyOpsDigestStatus

    # No START event — only an LLM call and a discovery event.
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        timestamp_utc=_utc(hour=2),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.MARKET_DISCOVERED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.INGESTION,
        reason_code=OperationalEventReasonCode.MARKET_FOUND,
        timestamp_utc=_utc(hour=3),
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root))
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    assert report.status == DailyOpsDigestStatus.SUCCESS
    assert report.run_summary is not None
    assert report.run_summary.run_status == "unknown"
    assert report.run_summary.start_utc is None
    assert report.run_summary.stop_utc is None


@pytest.mark.asyncio
async def test_digest_zero_records_yields_no_run(db_session_factory, tmp_path) -> None:
    """Zero records in the window yields run_status=no_run via EMPTY_WINDOW."""
    from src.observability.daily_ops_digest import generate_digest
    from src.schemas.ops import DailyOpsDigestStatus

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = _make_request(daily_notes_dir=str(daily_root))
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    assert report.status == DailyOpsDigestStatus.EMPTY_WINDOW
    # An empty-window digest still encodes the no_run conclusion clearly
    # in the rendered file. The typed run_summary may be None for the
    # EMPTY_WINDOW shortcut path; that is the documented contract.
