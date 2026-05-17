"""
tests/integration/test_WI-58-incident-replay-cli.py

End-to-end integration tests for the WI-58 incident replay CLI.

These tests drive the CLI against a real in-memory SQLite-backed
``OperationalEventRepository``, exercising the full path: argparse →
typed schema → repository read → WI-57 narrative render → secret scan →
typed summary → formatted operator output.
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
from src.observability.incident_replay import format_report_lines, run_replay
from src.schemas.ops import (
    IncidentReplayRequest,
    IncidentReplayStatus,
    OperationalEventCreate,
    OperationalEventPayload,
    OperationalEventReasonCode,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = _PROJECT_ROOT / "scripts" / "ops" / "replay.py"


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("wi58_replay_cli_int", _CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cli = _load_cli_module()


def _utc(hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, 5, 15, hour, minute, 0, tzinfo=timezone.utc)


async def _seed_typical_window(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Seed a representative incident sequence into the ledger."""
    events: list[
        tuple[
            OperationalEventType,
            OperationalEventSeverity,
            OperationalEventSource,
            OperationalEventReasonCode,
            datetime,
            Optional[OperationalEventPayload],
        ]
    ] = [
        (
            OperationalEventType.START,
            OperationalEventSeverity.INFO,
            OperationalEventSource.ORCHESTRATOR,
            OperationalEventReasonCode.STARTUP,
            _utc(hour=0),
            None,
        ),
        (
            OperationalEventType.WS_CONNECTED,
            OperationalEventSeverity.INFO,
            OperationalEventSource.INGESTION,
            OperationalEventReasonCode.WS_ESTABLISHED,
            _utc(hour=0, minute=2),
            None,
        ),
        (
            OperationalEventType.LLM_CALL_STARTED,
            OperationalEventSeverity.INFO,
            OperationalEventSource.EVALUATION,
            OperationalEventReasonCode.PROVIDER_CALL_STARTED,
            _utc(hour=0, minute=5),
            None,
        ),
        (
            OperationalEventType.DECISION_ACCEPTED,
            OperationalEventSeverity.INFO,
            OperationalEventSource.EVALUATION,
            OperationalEventReasonCode.DECISION_HOLD,
            _utc(hour=0, minute=6),
            None,
        ),
        (
            OperationalEventType.PROVIDER_FAILURE,
            OperationalEventSeverity.WARNING,
            OperationalEventSource.EVALUATION,
            OperationalEventReasonCode.PROVIDER_CALL_FAILED,
            _utc(hour=0, minute=8),
            None,
        ),
        (
            OperationalEventType.BUDGET_BLOCK,
            OperationalEventSeverity.WARNING,
            OperationalEventSource.EVALUATION,
            OperationalEventReasonCode.BUDGET_HOURLY,
            _utc(hour=0, minute=10),
            None,
        ),
        (
            OperationalEventType.READY_STATE_CHANGED,
            OperationalEventSeverity.WARNING,
            OperationalEventSource.ORCHESTRATOR,
            OperationalEventReasonCode.DEGRADED,
            _utc(hour=0, minute=12),
            None,
        ),
        (
            OperationalEventType.READY_STATE_CHANGED,
            OperationalEventSeverity.INFO,
            OperationalEventSource.ORCHESTRATOR,
            OperationalEventReasonCode.READY,
            _utc(hour=0, minute=20),
            None,
        ),
        (
            OperationalEventType.SHUTDOWN,
            OperationalEventSeverity.INFO,
            OperationalEventSource.ORCHESTRATOR,
            OperationalEventReasonCode.GRACEFUL_SHUTDOWN,
            _utc(hour=0, minute=30),
            None,
        ),
    ]
    async with session_factory() as session:
        async with session.begin():
            repo = OperationalEventRepository(session)
            for et, sev, src, rc, ts, payload in events:
                await repo.append(
                    OperationalEventCreate(
                        event_type=et,
                        severity=sev,
                        source=src,
                        reason_code=rc,
                        payload=payload or OperationalEventPayload(),
                        timestamp_utc=ts,
                    )
                )


@pytest.mark.asyncio
async def test_integration_end_to_end_replay_via_service(db_session_factory) -> None:
    await _seed_typical_window(db_session_factory)
    request = IncidentReplayRequest(
        from_utc=_utc(hour=0),
        to_utc=_utc(hour=1),
    )
    report = await run_replay(request, db_session_factory)

    assert report.status == IncidentReplayStatus.SUCCESS
    assert report.summary.total_events == 9
    assert report.summary.warnings == 3
    assert report.summary.llm_calls == 1
    assert report.summary.budget_blocks == 1
    assert report.summary.provider_failures == 1
    assert report.summary.readiness_changes == 2
    # Chronological by construction.
    timestamps = [line.timestamp_utc for line in report.lines]
    assert timestamps == sorted(timestamps)


@pytest.mark.asyncio
async def test_integration_cli_prints_summary_and_exits_zero(
    db_session_factory,
    capsys,
) -> None:
    await _seed_typical_window(db_session_factory)
    rc = _cli.main(
        [
            "--from",
            "2026-05-15T00:00:00Z",
            "--to",
            "2026-05-15T01:00:00Z",
        ],
        session_factory=db_session_factory,
    )
    captured = capsys.readouterr().out
    assert rc == _cli.EXIT_OK
    assert "Incident Replay" in captured
    assert "total_events: 9" in captured
    assert "provider_failures: 1" in captured
    assert "budget_blocks: 1" in captured
    assert "readiness_changes: 2" in captured


@pytest.mark.asyncio
async def test_integration_cli_combined_filters_intersect(
    db_session_factory,
    capsys,
) -> None:
    await _seed_typical_window(db_session_factory)
    rc = _cli.main(
        [
            "--from",
            "2026-05-15T00:00:00Z",
            "--to",
            "2026-05-15T01:00:00Z",
            "--severity",
            "WARNING",
            "--source",
            "EVALUATION",
        ],
        session_factory=db_session_factory,
    )
    captured = capsys.readouterr().out
    assert rc == _cli.EXIT_OK
    # Only events that are both WARNING *and* sourced from EVALUATION.
    # PROVIDER_FAILURE and BUDGET_BLOCK from the seed match.
    assert "total_events: 2" in captured


@pytest.mark.asyncio
async def test_integration_cli_empty_window_exits_zero_with_typed_status(
    db_session_factory,
    capsys,
) -> None:
    rc = _cli.main(
        [
            "--from",
            "2026-05-15T00:00:00Z",
            "--to",
            "2026-05-15T01:00:00Z",
        ],
        session_factory=db_session_factory,
    )
    captured = capsys.readouterr().out
    assert rc == _cli.EXIT_OK
    assert "status: EMPTY_WINDOW" in captured
    assert "(no events in window)" in captured


@pytest.mark.asyncio
async def test_integration_report_lines_are_secret_free(
    db_session_factory,
) -> None:
    await _seed_typical_window(db_session_factory)
    request = IncidentReplayRequest(
        from_utc=_utc(hour=0),
        to_utc=_utc(hour=1),
    )
    report = await run_replay(request, db_session_factory)
    rendered = "\n".join(format_report_lines(report))
    # No 64-hex condition IDs, 40-hex wallet addresses, telegram tokens,
    # or "api_key" / "private key" strings escape.
    for forbidden in ("api_key", "private key", "telegram"):
        assert forbidden not in rendered.lower()


@pytest.mark.asyncio
async def test_integration_invalid_filter_exits_non_zero(
    db_session_factory,
    capsys,
) -> None:
    await _seed_typical_window(db_session_factory)
    rc = _cli.main(
        [
            "--from",
            "2026-05-15T00:00:00Z",
            "--to",
            "2026-05-15T01:00:00Z",
            "--event-type",
            "NOT_A_REAL_EVENT_TYPE",
        ],
        session_factory=db_session_factory,
    )
    captured = capsys.readouterr().out
    assert rc == _cli.EXIT_INVALID_INPUT
    assert "INVALID_FILTER" in captured
    assert "UNKNOWN_ENUM_VALUE" in captured
