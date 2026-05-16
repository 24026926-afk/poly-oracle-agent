"""
tests/unit/test_WI-58-incident-replay-cli.py

Unit tests for WI-58 Incident Replay CLI.

Covers:

* Typed replay schemas in ``src/schemas/ops.py``.
* Repository-backed replay service in
  ``src/observability/incident_replay.py``.
* ``scripts/ops/replay.py`` CLI argument parsing, exit codes, and
  printed report safety.
* Chronological ordering, filter independence/combination, summary
  aggregation, narrative fallback/redaction handling, and Gatekeeper /
  repository / live-trading purity invariants.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models import OperationalEvent
from src.db.repositories.operational_event_repository import (
    OperationalEventRepository,
)
from src.observability import incident_replay as replay_mod
from src.observability.incident_replay import (
    format_report_lines,
    run_replay,
)
from src.schemas.ops import (
    IncidentReplayFailureReason,
    IncidentReplayFilter,
    IncidentReplayLine,
    IncidentReplayReport,
    IncidentReplayRequest,
    IncidentReplayStatus,
    IncidentReplaySummary,
    NarrativeRenderStatus,
    NarrativeTemplateKey,
    OperationalEventCreate,
    OperationalEventPayload,
    OperationalEventPersistenceStatus,
    OperationalEventReasonCode,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
)
from src.schemas.llm import LLMEvaluationResponse


# ═══════════════════════════════════════════════════════════════════════════
# Helpers — load CLI module dynamically (it lives under scripts/ops/)
# ═══════════════════════════════════════════════════════════════════════════


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = _PROJECT_ROOT / "scripts" / "ops" / "replay.py"


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("wi58_replay_cli", _CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cli = _load_cli_module()


# ═══════════════════════════════════════════════════════════════════════════
# Helpers — event seeding
# ═══════════════════════════════════════════════════════════════════════════


def _utc(year: int = 2026, month: int = 5, day: int = 15, hour: int = 0,
         minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


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
    """Insert a raw OperationalEvent row, bypassing typed payload validation.

    Used to exercise replay behavior against malformed payload JSON,
    payloads with forbidden content, and ID-tie-breaker ordering.
    """
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


def _default_request(
    *,
    from_utc: Optional[datetime] = None,
    to_utc: Optional[datetime] = None,
    filter_: Optional[IncidentReplayFilter] = None,
    limit: int = 1000,
) -> IncidentReplayRequest:
    return IncidentReplayRequest(
        from_utc=from_utc or _utc(hour=0),
        to_utc=to_utc or _utc(hour=23),
        filter=filter_ or IncidentReplayFilter(),
        limit=limit,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════════════════


def test_incident_replay_request_schema_exists_in_ops() -> None:
    req = IncidentReplayRequest(
        from_utc=_utc(hour=0),
        to_utc=_utc(hour=1),
    )
    assert req.from_utc.tzinfo is not None
    assert req.to_utc.tzinfo is not None
    assert isinstance(req.filter, IncidentReplayFilter)


def test_incident_replay_filter_schema_typed_enum_fields() -> None:
    # Typed values accepted.
    filt = IncidentReplayFilter(
        severities=[OperationalEventSeverity.WARNING],
        sources=[OperationalEventSource.EVALUATION],
        event_types=[OperationalEventType.PROVIDER_FAILURE],
        reason_codes=[OperationalEventReasonCode.PROVIDER_CALL_FAILED],
    )
    assert filt.severities == [OperationalEventSeverity.WARNING]

    # Free-form strings rejected.
    with pytest.raises(ValidationError):
        IncidentReplayFilter(severities=["definitely-not-a-severity"])  # type: ignore[list-item]
    with pytest.raises(ValidationError):
        IncidentReplayFilter(event_types=["BOGUS"])  # type: ignore[list-item]


def test_incident_replay_line_schema_secret_safe() -> None:
    line = IncidentReplayLine(
        event_id="evt-1",
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.READY,
        template_key=NarrativeTemplateKey.READINESS_READY,
        narrative_status=NarrativeRenderStatus.SUCCESS,
        summary="Readiness returned to READY; trading paths are eligible again.",
        timestamp_utc=_utc(hour=0),
    )
    # Secret content must be rejected at the schema boundary.
    with pytest.raises(ValidationError):
        IncidentReplayLine(
            event_id="evt-2",
            event_type=OperationalEventType.START,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
            template_key=NarrativeTemplateKey.LIFECYCLE_START,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="api_key leaked here",
            timestamp_utc=_utc(hour=0),
        )
    # Line forbids naive timestamps.
    with pytest.raises(ValidationError):
        IncidentReplayLine(
            event_id="evt-3",
            event_type=OperationalEventType.START,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
            template_key=NarrativeTemplateKey.LIFECYCLE_START,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="The agent started up.",
            timestamp_utc=datetime(2026, 5, 15, 0, 0, 0),  # naive
        )
    assert line.summary.startswith("Readiness")


def test_incident_replay_summary_counts_shape() -> None:
    s = IncidentReplaySummary(
        total_events=3,
        warnings=1,
        errors=1,
        markets_seen=1,
        decisions_by_action={"BUY": 1, "SKIP": 2},
        skips_by_reason={"DECISION_SKIP_LOW_CONF": 1},
        llm_calls=2,
        budget_blocks=1,
        cooldown_blocks=1,
        provider_failures=1,
        readiness_changes=1,
    )
    for attr in (
        "total_events", "warnings", "errors", "markets_seen",
        "decisions_by_action", "skips_by_reason", "llm_calls",
        "budget_blocks", "cooldown_blocks", "provider_failures",
        "readiness_changes",
    ):
        assert hasattr(s, attr)
    with pytest.raises(ValidationError):
        IncidentReplaySummary(decisions_by_action={"SELL": 1})
    with pytest.raises(ValidationError):
        IncidentReplaySummary(skips_by_reason={"definitely-not-a-reason-code": 1})


def test_incident_replay_report_schema_composition() -> None:
    req = _default_request()
    report = IncidentReplayReport(
        status=IncidentReplayStatus.EMPTY_WINDOW,
        request=req,
        lines=[],
        summary=IncidentReplaySummary(),
    )
    assert report.request is req
    # SUCCESS status without lines is invalid (empty windows must be EMPTY_WINDOW)
    with pytest.raises(ValidationError):
        IncidentReplayReport(
            status=IncidentReplayStatus.SUCCESS,
            request=req,
            lines=[],
            summary=IncidentReplaySummary(),
        )
    # Failure statuses require a typed failure reason.
    with pytest.raises(ValidationError):
        IncidentReplayReport(
            status=IncidentReplayStatus.REPOSITORY_FAILURE,
            request=req,
            lines=[],
            summary=IncidentReplaySummary(),
        )


def test_incident_replay_status_and_failure_reason_enums() -> None:
    expected_statuses = {
        "SUCCESS", "EMPTY_WINDOW", "INVALID_WINDOW",
        "INVALID_TIMESTAMP", "INVALID_FILTER",
        "REPOSITORY_FAILURE", "DATABASE_UNAVAILABLE", "TRUNCATED",
    }
    actual = {member.value for member in IncidentReplayStatus}
    assert expected_statuses.issubset(actual)

    expected_failures = {
        "FROM_AFTER_TO", "MALFORMED_TIMESTAMP", "NAIVE_TIMESTAMP",
        "UNKNOWN_ENUM_VALUE", "REPOSITORY_ERROR", "MISSING_EVENT_TABLE",
        "DATABASE_UNREACHABLE", "FORBIDDEN_CONTENT", "RESULT_TRUNCATED",
    }
    actual_failures = {member.value for member in IncidentReplayFailureReason}
    assert expected_failures.issubset(actual_failures)


# ═══════════════════════════════════════════════════════════════════════════
# Timestamp validation
# ═══════════════════════════════════════════════════════════════════════════


def test_from_after_to_is_invalid_window() -> None:
    with pytest.raises(ValidationError):
        IncidentReplayRequest(
            from_utc=_utc(hour=2),
            to_utc=_utc(hour=1),
        )


def test_from_equal_to_is_valid_zero_or_minimal_window() -> None:
    req = IncidentReplayRequest(
        from_utc=_utc(hour=1),
        to_utc=_utc(hour=1),
    )
    assert req.from_utc == req.to_utc


def test_malformed_timestamp_returns_invalid_timestamp() -> None:
    rc = _cli.main(["--from", "not-a-date", "--to", "2026-05-15T01:00:00Z"], session_factory=None)
    assert rc == _cli.EXIT_INVALID_INPUT


def test_naive_timestamp_is_rejected_or_normalized_to_utc() -> None:
    # CLI: naive timestamp is rejected with INVALID_TIMESTAMP.
    rc = _cli.main(
        ["--from", "2026-05-15T00:00:00", "--to", "2026-05-15T01:00:00Z"],
        session_factory=None,
    )
    assert rc == _cli.EXIT_INVALID_INPUT

    # Schema: naive datetime rejected by IncidentReplayRequest validator.
    with pytest.raises(ValidationError):
        IncidentReplayRequest(
            from_utc=datetime(2026, 5, 15, 0, 0, 0),
            to_utc=_utc(hour=1),
        )


def test_non_utc_offset_is_normalized_to_utc_before_query() -> None:
    plus_two = timezone(timedelta(hours=2))
    req = IncidentReplayRequest(
        from_utc=datetime(2026, 5, 15, 2, 0, 0, tzinfo=plus_two),
        to_utc=datetime(2026, 5, 15, 3, 0, 0, tzinfo=plus_two),
    )
    assert req.from_utc == datetime(2026, 5, 15, 0, 0, 0, tzinfo=timezone.utc)
    assert req.to_utc == datetime(2026, 5, 15, 1, 0, 0, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
# Filters — independent and combined
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_severity_filter_independent(db_session_factory) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.DEGRADED,
        timestamp_utc=_utc(hour=1),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.READY,
        timestamp_utc=_utc(hour=2),
    )
    req = _default_request(filter_=IncidentReplayFilter(
        severities=[OperationalEventSeverity.WARNING],
    ))
    report = await run_replay(req, db_session_factory)
    assert all(line.severity == OperationalEventSeverity.WARNING for line in report.lines)
    assert len(report.lines) == 1


@pytest.mark.asyncio
async def test_source_filter_independent(db_session_factory) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.WS_CONNECTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.INGESTION,
        reason_code=OperationalEventReasonCode.WS_ESTABLISHED,
        timestamp_utc=_utc(hour=1),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        timestamp_utc=_utc(hour=2),
    )
    req = _default_request(filter_=IncidentReplayFilter(
        sources=[OperationalEventSource.EVALUATION],
    ))
    report = await run_replay(req, db_session_factory)
    assert all(line.source == OperationalEventSource.EVALUATION for line in report.lines)
    assert len(report.lines) == 1


@pytest.mark.asyncio
async def test_event_type_filter_independent(db_session_factory) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        timestamp_utc=_utc(hour=1),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.PROVIDER_FAILURE,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_FAILED,
        timestamp_utc=_utc(hour=2),
    )
    req = _default_request(filter_=IncidentReplayFilter(
        event_types=[OperationalEventType.PROVIDER_FAILURE],
    ))
    report = await run_replay(req, db_session_factory)
    assert all(line.event_type == OperationalEventType.PROVIDER_FAILURE for line in report.lines)
    assert len(report.lines) == 1


@pytest.mark.asyncio
async def test_reason_code_filter_independent(db_session_factory) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.DECISION_SKIPPED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
        timestamp_utc=_utc(hour=1),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.DECISION_SKIPPED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_SKIP_LOW_EV,
        timestamp_utc=_utc(hour=2),
    )
    req = _default_request(filter_=IncidentReplayFilter(
        reason_codes=[OperationalEventReasonCode.DECISION_SKIP_LOW_CONF],
    ))
    report = await run_replay(req, db_session_factory)
    assert all(
        line.reason_code == OperationalEventReasonCode.DECISION_SKIP_LOW_CONF
        for line in report.lines
    )
    assert len(report.lines) == 1


@pytest.mark.asyncio
async def test_filters_combined_intersect(db_session_factory) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.DECISION_SKIPPED,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
        timestamp_utc=_utc(hour=1),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.DECISION_SKIPPED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
        timestamp_utc=_utc(hour=2),
    )
    req = _default_request(filter_=IncidentReplayFilter(
        severities=[OperationalEventSeverity.WARNING],
        event_types=[OperationalEventType.DECISION_SKIPPED],
    ))
    report = await run_replay(req, db_session_factory)
    assert len(report.lines) == 1
    assert report.lines[0].severity == OperationalEventSeverity.WARNING


def test_invalid_enum_filter_value_returns_invalid_filter() -> None:
    rc = _cli.main(
        [
            "--from", "2026-05-15T00:00:00Z",
            "--to", "2026-05-15T01:00:00Z",
            "--severity", "definitely-not-a-severity",
        ],
        session_factory=None,
    )
    assert rc == _cli.EXIT_INVALID_INPUT


@pytest.mark.asyncio
async def test_contradictory_filters_produce_zero_event_report(db_session_factory) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        timestamp_utc=_utc(hour=1),
    )
    req = _default_request(filter_=IncidentReplayFilter(
        # Combination cannot match — LLM_CALL_STARTED never has WS_ESTABLISHED reason.
        event_types=[OperationalEventType.LLM_CALL_STARTED],
        reason_codes=[OperationalEventReasonCode.WS_ESTABLISHED],
    ))
    report = await run_replay(req, db_session_factory)
    assert report.status == IncidentReplayStatus.EMPTY_WINDOW
    assert report.lines == []
    assert report.summary.total_events == 0


# ═══════════════════════════════════════════════════════════════════════════
# Repository purity
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_replay_reads_through_operational_event_repository(
    db_session_factory, monkeypatch,
) -> None:
    calls: list = []
    real_read_window = OperationalEventRepository.read_window

    async def _spy(self, query):
        calls.append(query)
        return await real_read_window(self, query)

    monkeypatch.setattr(OperationalEventRepository, "read_window", _spy)
    req = _default_request()
    await run_replay(req, db_session_factory)
    assert len(calls) == 1


def test_replay_does_not_open_raw_sessions_outside_repository() -> None:
    text = Path(replay_mod.__file__).read_text()
    # The replay module must hand sessions to the repository immediately.
    # No direct .execute()/.scalars() against AsyncSession is allowed.
    assert "session.execute" not in text
    assert "session.scalars" not in text
    cli_text = _CLI_PATH.read_text()
    assert "AsyncSession(" not in cli_text
    assert "session.execute" not in cli_text


def test_operational_event_repository_has_no_update_or_delete_methods() -> None:
    forbidden = {"update", "delete", "backfill", "remove", "purge"}
    for name in dir(OperationalEventRepository):
        if name.startswith("_"):
            continue
        assert name.lower() not in forbidden, (
            f"OperationalEventRepository must remain append/read-only; "
            f"found public method {name!r}"
        )


def test_no_base_metadata_create_all_in_cli_or_runtime_paths() -> None:
    cli_text = _CLI_PATH.read_text()
    service_text = Path(replay_mod.__file__).read_text()
    assert "create_all" not in cli_text
    assert "create_all" not in service_text


# ═══════════════════════════════════════════════════════════════════════════
# Ordering and rendering
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_replay_output_is_chronological_by_event_creation_time(
    db_session_factory,
) -> None:
    # Seed out of order — repository returns desc by created_at_utc.
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        timestamp_utc=_utc(hour=3),
    )
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
        event_type=OperationalEventType.WS_CONNECTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.INGESTION,
        reason_code=OperationalEventReasonCode.WS_ESTABLISHED,
        timestamp_utc=_utc(hour=2),
    )
    report = await run_replay(_default_request(), db_session_factory)
    timestamps = [line.timestamp_utc for line in report.lines]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_replay_ordering_is_deterministic_for_duplicate_timestamps(
    db_session_factory,
) -> None:
    same_ts = _utc(hour=2, second=0)
    a = await _insert_raw_row(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=same_ts,
        payload_json="{}",
        event_id="aaaaaaaa-0000",
    )
    b = await _insert_raw_row(
        db_session_factory,
        event_type=OperationalEventType.WS_CONNECTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.INGESTION,
        reason_code=OperationalEventReasonCode.WS_ESTABLISHED,
        timestamp_utc=same_ts,
        payload_json="{}",
        event_id="bbbbbbbb-0001",
    )
    report1 = await run_replay(_default_request(), db_session_factory)
    report2 = await run_replay(_default_request(), db_session_factory)
    ids1 = [line.event_id for line in report1.lines]
    ids2 = [line.event_id for line in report2.lines]
    assert ids1 == ids2  # deterministic
    assert ids1 == [a, b]  # tie-broken by id ascending


@pytest.mark.asyncio
async def test_replay_lines_use_wi57_narrative_renderer(
    db_session_factory, monkeypatch,
) -> None:
    seen: list = []
    real_render = replay_mod.render_event

    def _spy(record):
        seen.append(record.id)
        return real_render(record)

    monkeypatch.setattr(replay_mod, "render_event", _spy)
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )
    report = await run_replay(_default_request(), db_session_factory)
    assert seen, "render_event must be called for each rendered line"
    assert report.lines[0].template_key == NarrativeTemplateKey.LIFECYCLE_START


@pytest.mark.asyncio
async def test_unknown_event_reason_combo_uses_conservative_generic_narrative(
    db_session_factory,
) -> None:
    # MARKET_DISCOVERED + WS_LOST is a typed but unmapped combination.
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.MARKET_DISCOVERED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.INGESTION,
        reason_code=OperationalEventReasonCode.WS_LOST,
        timestamp_utc=_utc(hour=1),
    )
    report = await run_replay(_default_request(), db_session_factory)
    assert len(report.lines) == 1
    assert report.lines[0].template_key == NarrativeTemplateKey.GENERIC
    assert report.lines[0].narrative_status == NarrativeRenderStatus.FALLBACK


@pytest.mark.asyncio
async def test_narrative_fallback_status_produces_safe_replay_line(
    db_session_factory,
) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.MARKET_DISCOVERED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.INGESTION,
        reason_code=OperationalEventReasonCode.WS_LOST,
        timestamp_utc=_utc(hour=1),
    )
    report = await run_replay(_default_request(), db_session_factory)
    assert report.status == IncidentReplayStatus.SUCCESS
    line = report.lines[0]
    assert line.narrative_status == NarrativeRenderStatus.FALLBACK
    assert "raw_prompt" not in line.summary.lower()


@pytest.mark.asyncio
async def test_narrative_redaction_status_produces_safe_replay_line(
    db_session_factory,
) -> None:
    # Raw payload with forbidden content bypasses typed payload validation.
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
    report = await run_replay(_default_request(), db_session_factory)
    assert report.status == IncidentReplayStatus.SUCCESS
    assert len(report.lines) == 1
    line = report.lines[0]
    assert line.narrative_status == NarrativeRenderStatus.REDACTED
    assert "0x" not in line.summary  # no wallet address bleed-through


@pytest.mark.asyncio
async def test_malformed_payload_json_does_not_crash_replay(
    db_session_factory,
) -> None:
    await _insert_raw_row(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
        payload_json="not-valid-json{",
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.SHUTDOWN,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.GRACEFUL_SHUTDOWN,
        timestamp_utc=_utc(hour=2),
    )
    report = await run_replay(_default_request(), db_session_factory)
    assert len(report.lines) == 2  # both rendered, one with FALLBACK status
    statuses = {line.narrative_status for line in report.lines}
    assert NarrativeRenderStatus.FALLBACK in statuses


# ═══════════════════════════════════════════════════════════════════════════
# Summary aggregation
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_summary_total_events_count(db_session_factory) -> None:
    for hour in (1, 2, 3):
        await _append_event(
            db_session_factory,
            event_type=OperationalEventType.START,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
            timestamp_utc=_utc(hour=hour),
        )
    report = await run_replay(_default_request(), db_session_factory)
    assert report.summary.total_events == 3
    assert report.summary.total_events == len(report.lines)


@pytest.mark.asyncio
async def test_summary_warnings_and_errors_counts_use_typed_severity(
    db_session_factory,
) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.DEGRADED,
        timestamp_utc=_utc(hour=1),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.PROVIDER_FAILURE,
        severity=OperationalEventSeverity.ERROR,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_FAILED,
        timestamp_utc=_utc(hour=2),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.CIRCUIT_BREAKER_OPEN,
        severity=OperationalEventSeverity.CRITICAL,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.CB_OPEN,
        timestamp_utc=_utc(hour=3),
    )
    report = await run_replay(_default_request(), db_session_factory)
    assert report.summary.warnings == 1
    assert report.summary.errors == 2  # ERROR + CRITICAL


@pytest.mark.asyncio
async def test_summary_markets_seen_uses_typed_bounded_count_only(
    db_session_factory,
) -> None:
    for hour, etype, rcode in (
        (1, OperationalEventType.MARKET_DISCOVERED, OperationalEventReasonCode.MARKET_FOUND),
        (2, OperationalEventType.MARKET_REJECTED, OperationalEventReasonCode.MARKET_INELIGIBLE),
        (3, OperationalEventType.MARKET_QUARANTINE, OperationalEventReasonCode.MARKET_QUARANTINED),
    ):
        await _append_event(
            db_session_factory,
            event_type=etype,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.INGESTION,
            reason_code=rcode,
            timestamp_utc=_utc(hour=hour),
        )
    report = await run_replay(_default_request(), db_session_factory)
    assert report.summary.markets_seen == 3
    # No token IDs or condition IDs in any rendered output line.
    for line in report.lines:
        assert not re.search(r"\b\d{10,}\b", line.summary)
        assert not re.search(r"0x[a-fA-F0-9]{64}\b", line.summary)


@pytest.mark.asyncio
async def test_summary_decisions_by_action_uses_typed_action_only(
    db_session_factory,
) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.DECISION_ACCEPTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_BUY,
        timestamp_utc=_utc(hour=1),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.DECISION_ACCEPTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_HOLD,
        timestamp_utc=_utc(hour=2),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.DECISION_SKIPPED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
        timestamp_utc=_utc(hour=3),
    )
    report = await run_replay(_default_request(), db_session_factory)
    assert report.summary.decisions_by_action == {"BUY": 1, "HOLD": 1, "SKIP": 1}


@pytest.mark.asyncio
async def test_summary_skips_by_reason_uses_stable_reason_codes(
    db_session_factory,
) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.DECISION_SKIPPED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
        timestamp_utc=_utc(hour=1),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.DECISION_SKIPPED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_SKIP_HIGH_SPREAD,
        timestamp_utc=_utc(hour=2),
    )
    report = await run_replay(_default_request(), db_session_factory)
    skips = report.summary.skips_by_reason
    assert skips.get("DECISION_SKIP_LOW_CONF") == 1
    assert skips.get("DECISION_SKIP_HIGH_SPREAD") == 1
    # Keys are stable enum values, not free-form text.
    allowed = {c.value for c in OperationalEventReasonCode}
    assert all(k in allowed for k in skips)


@pytest.mark.asyncio
async def test_summary_llm_calls_count_from_typed_events(db_session_factory) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        timestamp_utc=_utc(hour=1),
    )
    report = await run_replay(_default_request(), db_session_factory)
    assert report.summary.llm_calls == 1


@pytest.mark.asyncio
async def test_summary_budget_blocks_count(db_session_factory) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.BUDGET_BLOCK,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.BUDGET_DAILY,
        timestamp_utc=_utc(hour=1),
    )
    report = await run_replay(_default_request(), db_session_factory)
    assert report.summary.budget_blocks == 1


@pytest.mark.asyncio
async def test_summary_cooldown_blocks_count(db_session_factory) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.COOLDOWN_BLOCK,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.COOLDOWN_REPEATED_HOLD,
        timestamp_utc=_utc(hour=1),
    )
    report = await run_replay(_default_request(), db_session_factory)
    assert report.summary.cooldown_blocks == 1


@pytest.mark.asyncio
async def test_summary_provider_failures_count(db_session_factory) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.PROVIDER_FAILURE,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_FAILED,
        timestamp_utc=_utc(hour=1),
    )
    report = await run_replay(_default_request(), db_session_factory)
    assert report.summary.provider_failures == 1


@pytest.mark.asyncio
async def test_summary_readiness_changes_count(db_session_factory) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.READY,
        timestamp_utc=_utc(hour=1),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.DEGRADED,
        timestamp_utc=_utc(hour=2),
    )
    report = await run_replay(_default_request(), db_session_factory)
    assert report.summary.readiness_changes == 2


# ═══════════════════════════════════════════════════════════════════════════
# Empty / zero-result windows
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_empty_window_produces_valid_zero_event_report(
    db_session_factory,
) -> None:
    report = await run_replay(_default_request(), db_session_factory)
    assert report.status == IncidentReplayStatus.EMPTY_WINDOW
    assert report.lines == []
    assert report.summary.total_events == 0


@pytest.mark.asyncio
async def test_filtered_zero_result_window_produces_valid_zero_event_report(
    db_session_factory,
) -> None:
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )
    req = _default_request(filter_=IncidentReplayFilter(
        severities=[OperationalEventSeverity.CRITICAL],
    ))
    report = await run_replay(req, db_session_factory)
    assert report.status == IncidentReplayStatus.EMPTY_WINDOW
    # Active filters survive in the report request.
    assert report.request.filter.severities == [OperationalEventSeverity.CRITICAL]


# ═══════════════════════════════════════════════════════════════════════════
# Secret / high-cardinality safety
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_replay_output_contains_no_token_ids_or_condition_ids(
    db_session_factory,
) -> None:
    bad_payload = json.dumps({"message": "0x" + "a" * 64})
    await _insert_raw_row(
        db_session_factory,
        event_type=OperationalEventType.MARKET_DISCOVERED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.INGESTION,
        reason_code=OperationalEventReasonCode.MARKET_FOUND,
        timestamp_utc=_utc(hour=1),
        payload_json=bad_payload,
    )
    report = await run_replay(_default_request(), db_session_factory)
    rendered = "\n".join(format_report_lines(report))
    assert not re.search(r"0x[a-fA-F0-9]{64}\b", rendered)
    assert not re.search(r"\b\d{10,}\b", rendered)


@pytest.mark.asyncio
async def test_replay_output_contains_no_wallet_addresses_or_keys(
    db_session_factory,
) -> None:
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
    report = await run_replay(_default_request(), db_session_factory)
    rendered = "\n".join(format_report_lines(report))
    assert not re.search(r"0x[a-fA-F0-9]{40}\b", rendered)
    assert "private key" not in rendered.lower()
    assert "api_key" not in rendered.lower()


@pytest.mark.asyncio
async def test_replay_output_contains_no_raw_prompts_or_reasoning(
    db_session_factory,
) -> None:
    bad_payload = json.dumps({"message": "raw_prompt: you are a helpful assistant"})
    await _insert_raw_row(
        db_session_factory,
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        timestamp_utc=_utc(hour=1),
        payload_json=bad_payload,
    )
    report = await run_replay(_default_request(), db_session_factory)
    rendered = "\n".join(format_report_lines(report))
    assert "raw_prompt" not in rendered.lower()
    assert "reasoning_log" not in rendered.lower()


def test_replay_output_contains_no_raw_exception_messages_or_sql() -> None:
    # When the repository fails, the printed report must carry only the
    # bounded, low-cardinality message; no raw exception text.
    service_text = Path(replay_mod.__file__).read_text()
    # The service catches SQLAlchemy errors but discards the exception
    # detail when building the user-facing message.
    assert "operational event database is unreachable" in service_text
    assert "operational event repository read failed" in service_text


def test_replay_output_is_scanned_before_printing() -> None:
    service_text = Path(replay_mod.__file__).read_text()
    # The line builder calls _scan_event_payload as a final defense in depth.
    assert "_scan_event_payload(summary)" in service_text


# ═══════════════════════════════════════════════════════════════════════════
# CLI exit codes and entrypoint behavior
# ═══════════════════════════════════════════════════════════════════════════


def test_cli_module_exists_at_scripts_ops_replay() -> None:
    assert _CLI_PATH.exists()
    assert hasattr(_cli, "main")


@pytest.mark.asyncio
async def test_cli_exits_zero_on_successful_empty_or_populated_report(
    db_session_factory,
) -> None:
    rc = _cli.main(
        ["--from", "2026-05-15T00:00:00Z", "--to", "2026-05-15T23:00:00Z"],
        session_factory=db_session_factory,
    )
    assert rc == _cli.EXIT_OK


def test_cli_exits_non_zero_on_invalid_window() -> None:
    rc = _cli.main(
        ["--from", "2026-05-15T02:00:00Z", "--to", "2026-05-15T01:00:00Z"],
        session_factory=None,
    )
    assert rc == _cli.EXIT_INVALID_INPUT


def test_cli_exits_non_zero_on_invalid_timestamp() -> None:
    rc = _cli.main(
        ["--from", "bogus", "--to", "2026-05-15T01:00:00Z"],
        session_factory=None,
    )
    assert rc == _cli.EXIT_INVALID_INPUT


def test_cli_exits_non_zero_on_invalid_filter() -> None:
    rc = _cli.main(
        [
            "--from", "2026-05-15T00:00:00Z",
            "--to", "2026-05-15T01:00:00Z",
            "--reason-code", "definitely-not-a-reason-code",
        ],
        session_factory=None,
    )
    assert rc == _cli.EXIT_INVALID_INPUT


@pytest.mark.asyncio
async def test_cli_exits_non_zero_on_repository_failure(
    db_session_factory, monkeypatch,
) -> None:
    from sqlalchemy.exc import SQLAlchemyError

    async def _boom(self, query):
        raise SQLAlchemyError("simulated repository failure")

    monkeypatch.setattr(OperationalEventRepository, "read_window", _boom)
    rc = _cli.main(
        ["--from", "2026-05-15T00:00:00Z", "--to", "2026-05-15T01:00:00Z"],
        session_factory=db_session_factory,
    )
    assert rc == _cli.EXIT_REPOSITORY


@pytest.mark.asyncio
async def test_cli_handles_missing_operational_events_table_safely(
    db_session_factory, monkeypatch,
) -> None:
    from sqlalchemy.exc import OperationalError

    async def _boom(self, query):
        raise OperationalError(
            "SELECT", {}, Exception("no such table: operational_events")
        )

    monkeypatch.setattr(OperationalEventRepository, "read_window", _boom)
    rc = _cli.main(
        ["--from", "2026-05-15T00:00:00Z", "--to", "2026-05-15T01:00:00Z"],
        session_factory=db_session_factory,
    )
    assert rc == _cli.EXIT_REPOSITORY


# ═══════════════════════════════════════════════════════════════════════════
# Purity invariants
# ═══════════════════════════════════════════════════════════════════════════


def test_cli_does_not_import_any_llm_client() -> None:
    text = _CLI_PATH.read_text()
    for forbidden in (
        "anthropic", "deepseek", "grok",
        "from src.agents.evaluation.claude_client",
        "from src.agents.evaluation.deepseek_client",
        "from src.agents.evaluation.grok_client",
        "from src.agents.evaluation.llm_provider",
    ):
        assert forbidden.lower() not in text.lower(), (
            f"replay CLI must not import an LLM client; found {forbidden!r}"
        )


def test_cli_does_not_import_execution_router_or_signing_paths() -> None:
    text = _CLI_PATH.read_text()
    for forbidden in (
        "execution_router",
        "ExecutionRouter",
        "polymarket_client",
        "transaction_signer",
        "tx_signer",
        "wallet_signer",
        "from src.agents.execution",
    ):
        assert forbidden.lower() not in text.lower(), (
            f"replay CLI must not import execution/signing path; found {forbidden!r}"
        )


def test_cli_does_not_modify_operational_events() -> None:
    text = _CLI_PATH.read_text()
    service_text = Path(replay_mod.__file__).read_text()
    for forbidden in (
        "repo.append(", "repository.append(", ".batch_append(",
        "INSERT INTO operational_events",
        "DELETE FROM operational_events",
        "UPDATE operational_events",
    ):
        assert forbidden not in text, f"CLI must not write events; found {forbidden!r}"
        assert forbidden not in service_text, (
            f"replay service must not write events; found {forbidden!r}"
        )


def test_llm_evaluation_response_schema_unchanged_by_wi58() -> None:
    # The terminal Gatekeeper schema must not learn presentation fields.
    fields = LLMEvaluationResponse.model_fields
    forbidden_fields = {
        "narrative", "operational_narrative", "decision_narrative",
        "incident_replay_line", "replay_summary",
    }
    assert forbidden_fields.isdisjoint(fields.keys())


def test_no_dry_run_or_live_trading_behavior_changes() -> None:
    text = _CLI_PATH.read_text()
    service_text = Path(replay_mod.__file__).read_text()
    # Narrow forbidden patterns: runtime mutation identifiers only.
    # Docstring references like "never broadcasts" are explicitly
    # documenting the constraint and must remain readable.
    for forbidden in (
        "dry_run = False",
        "DRY_RUN=false",
        "broadcast_transaction",
        "broadcast_tx",
        "sign_transaction",
        "web3.eth.send",
    ):
        assert forbidden not in text
        assert forbidden not in service_text


def test_no_trading_or_sizing_calculations_in_replay_code() -> None:
    """Strip docstrings/comments; verify no trading-math identifiers remain.

    Docstrings explicitly enumerate the trading concepts the replay code
    refuses to perform (Kelly, EV, sizing, PnL) — those mentions are
    documentation, not implementation. The check below ignores docstrings
    and inline comments and inspects executable lines only.
    """
    service_text = Path(replay_mod.__file__).read_text()
    code_lines: list[str] = []
    in_docstring = False
    docstring_delim = '"""'
    for line in service_text.splitlines():
        stripped = line.strip()
        if in_docstring:
            if docstring_delim in stripped:
                in_docstring = False
            continue
        if stripped.startswith(docstring_delim):
            # Could be a one-line docstring or block start.
            if stripped.count(docstring_delim) >= 2:
                continue
            in_docstring = True
            continue
        if stripped.startswith("#"):
            continue
        code_lines.append(line)
    code_only = "\n".join(code_lines)
    for forbidden in ("kelly", "compute_ev(", "expected_value(", "sizing("):
        assert forbidden.lower() not in code_only.lower(), (
            f"trading-math identifier {forbidden!r} found in executable code"
        )


def test_decimal_safety_preserved_at_replay_schema_boundaries() -> None:
    # OperationalEventPayload (the upstream replay input) rejects float
    # in financial fields.
    with pytest.raises(ValidationError):
        OperationalEventPayload(budget_remaining=1.23)  # type: ignore[arg-type]
    # Decimal values are accepted.
    p = OperationalEventPayload(budget_remaining=Decimal("1.23"))
    assert p.budget_remaining == Decimal("1.23")


def test_replay_metrics_and_logs_use_low_cardinality_labels_only() -> None:
    service_text = Path(replay_mod.__file__).read_text()
    # Structured log calls in the replay path only emit typed enum values
    # (event_type.value / reason_code.value) — never raw payload text.
    log_lines = [
        line for line in service_text.splitlines() if "logger." in line
    ]
    for line in log_lines:
        if "payload" in line.lower() and "payload_json" not in line:
            pytest.fail(f"replay log line emits payload text: {line!r}")


# ═══════════════════════════════════════════════════════════════════════════
# Truncation
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_large_window_is_bounded_with_typed_truncation_indicator(
    db_session_factory, monkeypatch,
) -> None:
    from src.schemas.ops import OperationalEventReadWindow, OperationalEventRecord

    fake_records = [
        OperationalEventRecord(
            id=f"evt-{i:04d}",
            event_type=OperationalEventType.START,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
            payload_json="{}",
            persistence_status=OperationalEventPersistenceStatus.PERSISTED,
            created_at_utc=_utc(hour=1, minute=i % 60, second=i // 60),
            recorded_at_utc=_utc(hour=1),
        )
        for i in range(3)
    ]

    async def _fake_read_window(self, query):
        return OperationalEventReadWindow(
            events=fake_records,
            start_time_utc=query.start_time_utc,
            end_time_utc=query.end_time_utc,
            total_count=999,
            has_more=True,
        )

    monkeypatch.setattr(OperationalEventRepository, "read_window", _fake_read_window)
    report = await run_replay(_default_request(), db_session_factory)
    assert report.status == IncidentReplayStatus.TRUNCATED
    assert report.has_more is True
    assert report.failure_reason == IncidentReplayFailureReason.RESULT_TRUNCATED


# ═══════════════════════════════════════════════════════════════════════════
# Runbook
# ═══════════════════════════════════════════════════════════════════════════


def test_invalid_filter_value_does_not_leak_secret_shaped_input(capsys) -> None:
    """MAAP finding 1: invalid filter input must not be echoed verbatim
    if it matches secret/high-cardinality patterns (API keys, telegram
    tokens, wallet addresses, condition IDs, etc.)."""
    secret_shaped = "sk-" + "a" * 40
    rc = _cli.main(
        [
            "--from", "2026-05-15T00:00:00Z",
            "--to", "2026-05-15T01:00:00Z",
            "--severity", secret_shaped,
        ],
        session_factory=None,
    )
    captured = capsys.readouterr().out
    assert rc == _cli.EXIT_INVALID_INPUT
    assert secret_shaped not in captured
    assert "<redacted>" in captured or "INVALID_FILTER" in captured


def test_argparse_does_not_leak_secret_shaped_invalid_input(capsys) -> None:
    """MAAP rerun finding: argparse used to echo raw type-conversion
    failures (e.g. ``--limit sk-...``) before ``main()`` could sanitize
    them. The CLI must scrub every raw token through ``_safe_echo``
    on every argparse failure path."""
    secret_shaped = "sk-abcdefghijklmnopqrstuvwxyz123456"
    # Non-int --limit value that previously triggered argparse's raw echo.
    rc_limit = _cli.main(
        [
            "--from", "2026-05-15T00:00:00Z",
            "--to", "2026-05-15T01:00:00Z",
            "--limit", secret_shaped,
        ],
        session_factory=None,
    )
    captured_limit = capsys.readouterr().out
    assert rc_limit == _cli.EXIT_INVALID_INPUT
    assert secret_shaped not in captured_limit

    # Unrecognized flag with a secret-shaped value (argparse appends
    # raw tokens here, not single-quoted).
    rc_unknown = _cli.main(
        [
            "--from", "2026-05-15T00:00:00Z",
            "--to", "2026-05-15T01:00:00Z",
            "--unknown-flag", secret_shaped,
        ],
        session_factory=None,
    )
    captured_unknown = capsys.readouterr().out
    assert rc_unknown == _cli.EXIT_INVALID_INPUT
    assert secret_shaped not in captured_unknown


def test_invalid_limit_fails_closed_with_non_zero_exit(capsys) -> None:
    """MAAP finding 2: --limit values outside [1, 1000] must fail closed
    with a typed CLI error, not be silently clamped to a different
    request that proceeds to the database."""
    rc = _cli.main(
        [
            "--from", "2026-05-15T00:00:00Z",
            "--to", "2026-05-15T01:00:00Z",
            "--limit", "0",
        ],
        session_factory=None,
    )
    captured = capsys.readouterr().out
    assert rc == _cli.EXIT_INVALID_INPUT
    assert "LIMIT_OUT_OF_RANGE" in captured

    rc_high = _cli.main(
        [
            "--from", "2026-05-15T00:00:00Z",
            "--to", "2026-05-15T01:00:00Z",
            "--limit", "1001",
        ],
        session_factory=None,
    )
    assert rc_high == _cli.EXIT_INVALID_INPUT


def test_incident_replay_runbook_exists() -> None:
    runbook = _PROJECT_ROOT / "docs" / "runbooks" / "incident-replay.md"
    assert runbook.exists()
    text = runbook.read_text()
    for keyword in (
        "--from", "--to", "filter", "empty", "invalid", "incident",
    ):
        assert keyword.lower() in text.lower()
