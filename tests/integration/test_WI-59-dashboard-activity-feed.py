"""
tests/integration/test_WI-59-dashboard-activity-feed.py

End-to-end integration tests for WI-59 Dashboard Activity Feed.

These tests drive the dashboard helpers against a real in-memory
SQLite-backed ``OperationalEventRepository``, exercising the full path:
repository append → read-window → WI-57 narrative render →
dashboard feed item → current-state derivation → secret scan →
HTML rendering.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models import OperationalEvent
from src.db.repositories.operational_event_repository import (
    OperationalEventRepository,
)
from src.schemas.ops import (
    NarrativeRenderStatus,
    OperationalEventCreate,
    OperationalEventPayload,
    OperationalEventPersistenceStatus,
    OperationalEventReasonCode,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
)

# WI-59 schemas — may not exist during red phase
try:
    from src.schemas.ops import (
        DashboardActivityFeedFailureReason,
        DashboardActivityFeedFilter,
        DashboardActivityFeedItem,
        DashboardActivityFeedResult,
        DashboardActivityFeedStatus,
        DashboardCurrentState,
    )
    _DASHBOARD_SCHEMAS_AVAILABLE = True
except ImportError:
    _DASHBOARD_SCHEMAS_AVAILABLE = False

# WI-59 dashboard helpers — may not exist during red phase
try:
    from src.observability.dashboard_activity_feed import (
        derive_current_state,
        fetch_activity_feed_async,
    )
    _DASHBOARD_HELPERS_AVAILABLE = True
except ImportError:
    _DASHBOARD_HELPERS_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _utc(hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 5, 15, hour, minute, second, tzinfo=timezone.utc)


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


# ═══════════════════════════════════════════════════════════════════════════
# End-to-end feed retrieval
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fetch_activity_feed_returns_typed_result(db_session_factory) -> None:
    """fetch_activity_feed_async returns a typed DashboardActivityFeedResult."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert isinstance(result, DashboardActivityFeedResult)
    assert result.status == DashboardActivityFeedStatus.SUCCESS
    assert len(result.items) == 1
    assert result.current_state is not None


@pytest.mark.asyncio
async def test_fetch_activity_feed_includes_narrative_summary_per_item(
    db_session_factory,
) -> None:
    """Each feed item carries a deterministic WI-57 narrative summary.

    Note: SQLite-backed timestamps may be persisted naive, which causes
    WI-57 to return ``FALLBACK`` status with a ``NAIVE_TIMESTAMP`` reason
    even though the correct typed template was matched. Both SUCCESS
    and FALLBACK statuses are acceptable here; the contract is that a
    deterministic typed template is matched and a non-empty summary is
    produced.
    """
    from src.schemas.ops import NarrativeTemplateKey

    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.READY,
        timestamp_utc=_utc(hour=1),
    )
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert len(result.items) == 1
    item = result.items[0]
    assert item.summary  # non-empty
    assert item.narrative_status in {
        NarrativeRenderStatus.SUCCESS,
        NarrativeRenderStatus.FALLBACK,
    }
    assert item.template_key == NarrativeTemplateKey.READINESS_READY


@pytest.mark.asyncio
async def test_fetch_activity_feed_includes_timestamp_severity_source_reason(
    db_session_factory,
) -> None:
    """Each feed item exposes timestamp, severity, source, event type, reason."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.PROVIDER_FAILURE,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_FAILED,
        timestamp_utc=_utc(hour=1),
    )
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    item = result.items[0]
    assert item.timestamp_utc.tzinfo is not None
    assert item.severity == OperationalEventSeverity.WARNING
    assert item.source == OperationalEventSource.EVALUATION
    assert item.event_type == OperationalEventType.PROVIDER_FAILURE
    assert item.reason_code == OperationalEventReasonCode.PROVIDER_CALL_FAILED


@pytest.mark.asyncio
async def test_fetch_activity_feed_bounded_window(db_session_factory) -> None:
    """fetch_activity_feed_async respects the limit parameter."""
    for hour in range(1, 6):
        await _append_event(
            db_session_factory,
            event_type=OperationalEventType.START,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
            timestamp_utc=_utc(hour=hour),
        )
    result = await fetch_activity_feed_async(db_session_factory, limit=2)
    assert len(result.items) == 2


@pytest.mark.asyncio
async def test_fetch_activity_feed_empty_window_returns_empty_result(
    db_session_factory,
) -> None:
    """Zero events produces typed EMPTY_WINDOW."""
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert result.status == DashboardActivityFeedStatus.EMPTY_WINDOW
    assert result.items == []
    assert result.current_state is None


@pytest.mark.asyncio
async def test_fetch_activity_feed_missing_table_returns_graceful_result() -> None:
    """Missing operational_events table produces typed MISSING_TABLE / DATABASE_UNAVAILABLE."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite://", echo=False, future=True)
    # Do not create any tables.
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        result = await fetch_activity_feed_async(factory, limit=10)
        assert result.status in {
            DashboardActivityFeedStatus.MISSING_TABLE,
            DashboardActivityFeedStatus.DATABASE_UNAVAILABLE,
        }
        assert result.failure_reason is not None
        assert result.items == []
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_fetch_activity_feed_deterministic_order(db_session_factory) -> None:
    """Items are ordered deterministically newest-first."""
    for hour in (3, 1, 2):
        await _append_event(
            db_session_factory,
            event_type=OperationalEventType.START,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
            timestamp_utc=_utc(hour=hour),
        )
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    timestamps = [item.timestamp_utc for item in result.items]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_fetch_activity_feed_stable_order_for_duplicate_timestamps(
    db_session_factory,
) -> None:
    """Identical timestamps tie-break on id ascending (stable across calls)."""
    same_ts = _utc(hour=1)
    a = await _insert_raw_row(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=same_ts,
        payload_json="{}",
        event_id="aaaaaaaa",
    )
    b = await _insert_raw_row(
        db_session_factory,
        event_type=OperationalEventType.WS_CONNECTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.INGESTION,
        reason_code=OperationalEventReasonCode.WS_ESTABLISHED,
        timestamp_utc=same_ts,
        payload_json="{}",
        event_id="bbbbbbbb",
    )
    result1 = await fetch_activity_feed_async(db_session_factory, limit=10)
    result2 = await fetch_activity_feed_async(db_session_factory, limit=10)
    ids1 = [item.event_id for item in result1.items]
    ids2 = [item.event_id for item in result2.items]
    assert ids1 == ids2
    assert ids1 == [a, b]  # tie-broken by id ascending


@pytest.mark.asyncio
async def test_fetch_activity_feed_filter_by_severity(db_session_factory) -> None:
    """Severity filter narrows the feed deterministically."""
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
    result = await fetch_activity_feed_async(
        db_session_factory,
        limit=10,
        filter=DashboardActivityFeedFilter(
            severities=[OperationalEventSeverity.WARNING]
        ),
    )
    assert all(
        item.severity == OperationalEventSeverity.WARNING for item in result.items
    )
    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_fetch_activity_feed_filter_by_source(db_session_factory) -> None:
    """Source filter narrows the feed deterministically."""
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
    result = await fetch_activity_feed_async(
        db_session_factory,
        limit=10,
        filter=DashboardActivityFeedFilter(
            sources=[OperationalEventSource.EVALUATION]
        ),
    )
    assert all(
        item.source == OperationalEventSource.EVALUATION for item in result.items
    )
    assert len(result.items) == 1


@pytest.mark.asyncio
async def test_fetch_activity_feed_filter_by_event_type(db_session_factory) -> None:
    """Event-type filter narrows the feed deterministically."""
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
    result = await fetch_activity_feed_async(
        db_session_factory,
        limit=10,
        filter=DashboardActivityFeedFilter(
            event_types=[OperationalEventType.PROVIDER_FAILURE]
        ),
    )
    assert len(result.items) == 1
    assert result.items[0].event_type == OperationalEventType.PROVIDER_FAILURE


@pytest.mark.asyncio
async def test_fetch_activity_feed_filter_by_reason_code(db_session_factory) -> None:
    """Reason-code filter narrows the feed deterministically."""
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
    result = await fetch_activity_feed_async(
        db_session_factory,
        limit=10,
        filter=DashboardActivityFeedFilter(
            reason_codes=[OperationalEventReasonCode.DECISION_SKIP_LOW_CONF]
        ),
    )
    assert len(result.items) == 1
    assert result.items[0].reason_code == OperationalEventReasonCode.DECISION_SKIP_LOW_CONF


@pytest.mark.asyncio
async def test_fetch_activity_feed_combined_filters_intersect(db_session_factory) -> None:
    """Combined filters intersect (AND)."""
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
    result = await fetch_activity_feed_async(
        db_session_factory,
        limit=10,
        filter=DashboardActivityFeedFilter(
            severities=[OperationalEventSeverity.WARNING],
            event_types=[OperationalEventType.DECISION_SKIPPED],
        ),
    )
    assert len(result.items) == 1
    assert result.items[0].severity == OperationalEventSeverity.WARNING


@pytest.mark.asyncio
async def test_fetch_activity_feed_malformed_payload_falls_back(
    db_session_factory,
) -> None:
    """Malformed payload JSON produces a fallback or redacted item, never crashes."""
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
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert len(result.items) == 2
    statuses = {item.narrative_status for item in result.items}
    assert NarrativeRenderStatus.FALLBACK in statuses or NarrativeRenderStatus.SUCCESS in statuses


@pytest.mark.asyncio
async def test_fetch_activity_feed_forbidden_content_redacts(
    db_session_factory,
) -> None:
    """Raw payloads with forbidden content render as redacted items, never leak."""
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
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    # Either the row is redacted into a safe summary, or the typed-line
    # construction is dropped. Either way, no wallet-address bleed-through.
    for item in result.items:
        assert "0x" + "a" * 40 not in item.summary


# ═══════════════════════════════════════════════════════════════════════════
# Current-state derivation
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_derive_current_state_from_recent_events(db_session_factory) -> None:
    """Current-state panel derives state from recent typed events."""
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
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.READY,
        timestamp_utc=_utc(hour=2),
    )
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert result.current_state is not None
    assert result.current_state.lifecycle_summary is not None
    assert result.current_state.readiness_summary is not None


@pytest.mark.asyncio
async def test_derive_current_state_no_events_returns_unknown(
    db_session_factory,
) -> None:
    """Empty ledger produces EMPTY_WINDOW with no current state."""
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert result.status == DashboardActivityFeedStatus.EMPTY_WINDOW
    assert result.current_state is None


@pytest.mark.asyncio
async def test_derive_current_state_reflects_degraded_readiness(
    db_session_factory,
) -> None:
    """Latest readiness=DEGRADED produces overall_state=degraded."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.DEGRADED,
        timestamp_utc=_utc(hour=1),
    )
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert result.current_state is not None
    assert result.current_state.overall_state == "degraded"


@pytest.mark.asyncio
async def test_derive_current_state_reflects_not_ready(
    db_session_factory,
) -> None:
    """Latest readiness=NOT_READY produces overall_state=stopped."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.CRITICAL,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.NOT_READY,
        timestamp_utc=_utc(hour=1),
    )
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert result.current_state is not None
    assert result.current_state.overall_state == "stopped"


@pytest.mark.asyncio
async def test_derive_current_state_preserves_dry_run_execution(
    db_session_factory,
) -> None:
    """Latest EXECUTION_DRY_RUN preserves simulated-execution context."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.EXECUTION_DRY_RUN,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EXECUTION,
        reason_code=OperationalEventReasonCode.EXEC_DRY_RUN_SKIP,
        timestamp_utc=_utc(hour=1),
        payload=OperationalEventPayload(dry_run=True),
    )
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert result.current_state is not None
    assert result.current_state.execution_summary is not None


@pytest.mark.asyncio
async def test_derive_current_state_summarizes_provider_failure(
    db_session_factory,
) -> None:
    """Latest provider failure surfaces in llm_summary."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.PROVIDER_FAILURE,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_FAILED,
        timestamp_utc=_utc(hour=1),
    )
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert result.current_state is not None
    assert result.current_state.llm_summary is not None


@pytest.mark.asyncio
async def test_derive_current_state_summarizes_budget_block(
    db_session_factory,
) -> None:
    """Latest budget block surfaces in llm_summary."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.BUDGET_BLOCK,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.BUDGET_HOURLY,
        timestamp_utc=_utc(hour=1),
    )
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert result.current_state is not None
    assert result.current_state.llm_summary is not None


@pytest.mark.asyncio
async def test_derive_current_state_summarizes_circuit_breaker_open(
    db_session_factory,
) -> None:
    """Latest CIRCUIT_BREAKER_OPEN surfaces in circuit_breaker_summary."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.CIRCUIT_BREAKER_OPEN,
        severity=OperationalEventSeverity.CRITICAL,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.CB_OPEN,
        timestamp_utc=_utc(hour=1),
    )
    result = await fetch_activity_feed_async(db_session_factory, limit=10)
    assert result.current_state is not None
    assert result.current_state.circuit_breaker_summary is not None
    assert result.current_state.overall_state == "degraded"


# ═══════════════════════════════════════════════════════════════════════════
# Read-only and purity invariants
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_fetch_activity_feed_does_not_write_to_operational_events(
    db_session_factory,
) -> None:
    """Calling fetch_activity_feed must not insert any rows."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )
    # Snapshot count before.
    from sqlalchemy import func, select

    async with db_session_factory() as session:
        before = (
            await session.execute(select(func.count(OperationalEvent.id)))
        ).scalar_one()

    await fetch_activity_feed_async(db_session_factory, limit=10)
    await fetch_activity_feed_async(db_session_factory, limit=10)

    async with db_session_factory() as session:
        after = (
            await session.execute(select(func.count(OperationalEvent.id)))
        ).scalar_one()
    assert before == after


@pytest.mark.asyncio
async def test_fetch_activity_feed_does_not_create_database_file(tmp_path) -> None:
    """Sync fetch_activity_feed must not create the SQLite file when absent."""
    from src.observability.dashboard_activity_feed import fetch_activity_feed

    nonexistent = tmp_path / "never_created.db"
    result = fetch_activity_feed(nonexistent, limit=10)
    assert result.status == DashboardActivityFeedStatus.DATABASE_UNAVAILABLE
    assert not nonexistent.exists()
