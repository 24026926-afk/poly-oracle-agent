"""
tests/unit/test_WI-59-dashboard-activity-feed.py

Unit tests for WI-59 Dashboard Activity Feed.

Covers:
* Dashboard activity feed schemas in ``src/schemas/ops.py``.
* Dashboard read-only fetch/format helpers in ``src/ui/dashboard.py``.
* Current-state derivation logic.
* Deterministic ordering, secret-safe rendering, HTML escaping,
  narrative fallback/redaction, and Gatekeeper / repository purity.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest
from pydantic import ValidationError

from src.schemas.ops import (
    NarrativeRenderStatus,
    NarrativeTemplateKey,
    OperationalEventPayload,
    OperationalEventPersistenceStatus,
    OperationalEventReasonCode,
    OperationalEventRecord,
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
    from src.ui import dashboard as dashboard_mod
    from src.ui.dashboard import (
        derive_current_state,
        fetch_activity_feed,
        format_activity_row_html,
    )
    _DASHBOARD_HELPERS_AVAILABLE = True
except ImportError:
    _DASHBOARD_HELPERS_AVAILABLE = False


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


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


def _utc(hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 5, 15, hour, minute, second, tzinfo=timezone.utc)


# ═══════════════════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════════════════


def test_dashboard_activity_feed_status_enum_exists() -> None:
    if not _DASHBOARD_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DashboardActivityFeedStatus enum not implemented")
    expected = {
        "SUCCESS",
        "EMPTY_WINDOW",
        "DATABASE_UNAVAILABLE",
        "MISSING_TABLE",
    }
    actual = {member.value for member in DashboardActivityFeedStatus}
    assert expected.issubset(actual)


def test_dashboard_activity_feed_failure_reason_enum_exists() -> None:
    if not _DASHBOARD_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DashboardActivityFeedFailureReason enum not implemented")
    expected = {
        "MISSING_TABLE",
        "DATABASE_UNREACHABLE",
        "RESULT_TRUNCATED",
        "FORBIDDEN_CONTENT",
    }
    actual = {member.value for member in DashboardActivityFeedFailureReason}
    assert expected.issubset(actual)


def test_dashboard_activity_feed_filter_typed_enum_fields() -> None:
    if not _DASHBOARD_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DashboardActivityFeedFilter schema not implemented")
    filt = DashboardActivityFeedFilter(
        severities=[OperationalEventSeverity.WARNING],
        sources=[OperationalEventSource.EVALUATION],
        event_types=[OperationalEventType.PROVIDER_FAILURE],
        reason_codes=[OperationalEventReasonCode.PROVIDER_CALL_FAILED],
    )
    assert filt.severities == [OperationalEventSeverity.WARNING]

    with pytest.raises(ValidationError):
        DashboardActivityFeedFilter(severities=["not-a-severity"])  # type: ignore[list-item]


def test_dashboard_activity_feed_item_schema_secret_safe() -> None:
    if not _DASHBOARD_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DashboardActivityFeedItem schema not implemented")
    item = DashboardActivityFeedItem(
        event_id="evt-1",
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.READY,
        template_key=NarrativeTemplateKey.READINESS_READY,
        narrative_status=NarrativeRenderStatus.SUCCESS,
        summary="Readiness returned to READY.",
        timestamp_utc=_utc(hour=0),
    )
    with pytest.raises(ValidationError):
        DashboardActivityFeedItem(
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
    assert item.summary.startswith("Readiness")


def test_dashboard_activity_feed_item_requires_tzaware_timestamp() -> None:
    if not _DASHBOARD_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DashboardActivityFeedItem schema not implemented")
    with pytest.raises(ValidationError):
        DashboardActivityFeedItem(
            event_id="evt-1",
            event_type=OperationalEventType.START,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
            template_key=NarrativeTemplateKey.LIFECYCLE_START,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="Start event.",
            timestamp_utc=datetime(2026, 5, 15, 0, 0, 0),
        )


def test_dashboard_current_state_schema_exists() -> None:
    if not _DASHBOARD_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DashboardCurrentState schema not implemented")
    state = DashboardCurrentState(
        lifecycle_summary="Agent started",
        readiness_summary="Ready",
        websocket_summary="Connected",
        llm_summary="Provider healthy",
        decision_summary="Last decision: HOLD",
        execution_summary="Dry-run execution simulated",
        circuit_breaker_summary="Closed",
        overall_state="continued",
        timestamp_utc=_utc(hour=0),
    )
    assert state.overall_state in {"continued", "skipped", "degraded", "stopped", "unknown"}
    assert state.timestamp_utc.tzinfo is not None


def test_dashboard_current_state_rejects_invalid_overall_state() -> None:
    if not _DASHBOARD_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DashboardCurrentState schema not implemented")
    with pytest.raises(ValidationError):
        DashboardCurrentState(
            overall_state="invalid-state",
            timestamp_utc=_utc(hour=0),
        )


def test_dashboard_activity_feed_result_status_consistency() -> None:
    if not _DASHBOARD_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DashboardActivityFeedResult schema not implemented")
    req = DashboardActivityFeedFilter()
    result = DashboardActivityFeedResult(
        status=DashboardActivityFeedStatus.EMPTY_WINDOW,
        items=[],
        current_state=None,
    )
    assert result.status == DashboardActivityFeedStatus.EMPTY_WINDOW
    with pytest.raises(ValidationError):
        DashboardActivityFeedResult(
            status=DashboardActivityFeedStatus.SUCCESS,
            items=[],
            current_state=None,
        )


def test_dashboard_activity_feed_result_truncated_requires_failure_reason() -> None:
    if not _DASHBOARD_SCHEMAS_AVAILABLE:
        raise NotImplementedError("DashboardActivityFeedResult schema not implemented")
    with pytest.raises(ValidationError):
        DashboardActivityFeedResult(
            status=DashboardActivityFeedStatus.SUCCESS,
            items=[
                DashboardActivityFeedItem(
                    event_id="evt-1",
                    event_type=OperationalEventType.START,
                    severity=OperationalEventSeverity.INFO,
                    source=OperationalEventSource.ORCHESTRATOR,
                    reason_code=OperationalEventReasonCode.STARTUP,
                    template_key=NarrativeTemplateKey.LIFECYCLE_START,
                    narrative_status=NarrativeRenderStatus.SUCCESS,
                    summary="Start event.",
                    timestamp_utc=_utc(hour=0),
                ),
            ],
            current_state=None,
        )


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard fetch helpers
# ═══════════════════════════════════════════════════════════════════════════


def test_fetch_activity_feed_is_defined_in_dashboard() -> None:
    if not _DASHBOARD_HELPERS_AVAILABLE:
        raise NotImplementedError("fetch_activity_feed not implemented")
    assert callable(fetch_activity_feed)
    sig = inspect.signature(fetch_activity_feed)
    assert "limit" in sig.parameters


def test_fetch_activity_feed_returns_dashboard_activity_feed_result(tmp_path) -> None:
    """fetch_activity_feed must return a typed DashboardActivityFeedResult.

    Calling against a non-existent DB returns the typed DATABASE_UNAVAILABLE
    failure path (the dashboard never creates the file).
    """
    import src.ui.dashboard as dashboard_mod_local

    nonexistent = tmp_path / "poly_oracle_does_not_exist.db"
    original = dashboard_mod_local.DB_PATH
    dashboard_mod_local.DB_PATH = nonexistent
    try:
        result = dashboard_mod_local.fetch_activity_feed(limit=10)
    finally:
        dashboard_mod_local.DB_PATH = original
    assert isinstance(result, DashboardActivityFeedResult)
    assert result.status == DashboardActivityFeedStatus.DATABASE_UNAVAILABLE
    assert result.failure_reason == DashboardActivityFeedFailureReason.DATABASE_UNREACHABLE
    assert not nonexistent.exists()


def test_fetch_activity_feed_reads_only_sqlite(tmp_path) -> None:
    """fetch_activity_feed must use a SQLite URI with mode=ro.

    Verified by writing to a real SQLite file directly, then attempting
    a write through the dashboard read connection — the read must succeed,
    a write must fail with OperationalError at the SQLite level.
    """
    import src.observability.dashboard_activity_feed as feed_mod

    db_file = tmp_path / "poly_oracle.db"
    # Create the file with a writable connection.
    setup_conn = sqlite3.connect(str(db_file))
    try:
        setup_conn.execute("CREATE TABLE foo (id INTEGER PRIMARY KEY)")
        setup_conn.commit()
    finally:
        setup_conn.close()

    uri = feed_mod._resolve_ro_uri(db_file)
    assert "mode=ro" in uri
    ro_conn = sqlite3.connect(uri, uri=True)
    try:
        # Read works.
        ro_conn.execute("SELECT 1").fetchone()
        # Write must fail.
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute("INSERT INTO foo (id) VALUES (1)")
    finally:
        ro_conn.close()


def test_fetch_activity_feed_graceful_when_operational_events_missing(tmp_path) -> None:
    """When the SQLite file exists but lacks operational_events, return MISSING_TABLE."""
    db_file = tmp_path / "poly_oracle.db"
    setup_conn = sqlite3.connect(str(db_file))
    try:
        setup_conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY)")
        setup_conn.commit()
    finally:
        setup_conn.close()

    import src.observability.dashboard_activity_feed as feed_mod

    result = feed_mod.fetch_activity_feed(db_file, limit=10)
    assert result.status == DashboardActivityFeedStatus.MISSING_TABLE
    assert result.failure_reason == DashboardActivityFeedFailureReason.MISSING_TABLE
    assert result.items == []


def test_fetch_activity_feed_graceful_when_operational_events_empty(tmp_path) -> None:
    """When operational_events exists but is empty, return EMPTY_WINDOW."""
    db_file = tmp_path / "poly_oracle.db"
    setup_conn = sqlite3.connect(str(db_file))
    try:
        setup_conn.execute(
            """
            CREATE TABLE operational_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                source TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                persistence_status TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL
            )
            """
        )
        setup_conn.commit()
    finally:
        setup_conn.close()

    import src.observability.dashboard_activity_feed as feed_mod

    result = feed_mod.fetch_activity_feed(db_file, limit=10)
    assert result.status == DashboardActivityFeedStatus.EMPTY_WINDOW
    assert result.items == []
    assert result.failure_reason is None


def test_fetch_activity_feed_bounded_by_limit(tmp_path) -> None:
    """fetch_activity_feed must bound its query at the configured limit."""
    import src.observability.dashboard_activity_feed as feed_mod

    db_file = tmp_path / "poly_oracle.db"
    setup_conn = sqlite3.connect(str(db_file))
    try:
        setup_conn.execute(
            """
            CREATE TABLE operational_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                source TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                persistence_status TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL
            )
            """
        )
        # Seed 5 START rows with descending timestamps.
        for idx in range(5):
            ts = f"2026-05-15T0{idx}:00:00+00:00"
            setup_conn.execute(
                "INSERT INTO operational_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"evt-{idx}",
                    OperationalEventType.START.value,
                    OperationalEventSeverity.INFO.value,
                    OperationalEventSource.ORCHESTRATOR.value,
                    OperationalEventReasonCode.STARTUP.value,
                    "{}",
                    OperationalEventPersistenceStatus.PERSISTED.value,
                    ts,
                    ts,
                ),
            )
        setup_conn.commit()
    finally:
        setup_conn.close()

    result = feed_mod.fetch_activity_feed(db_file, limit=2)
    assert len(result.items) == 2
    assert feed_mod.ACTIVITY_FEED_HARD_LIMIT >= 2


def test_fetch_activity_feed_deterministic_ordering(tmp_path) -> None:
    """Items must be deterministically newest-first."""
    import src.observability.dashboard_activity_feed as feed_mod

    db_file = tmp_path / "poly_oracle.db"
    setup_conn = sqlite3.connect(str(db_file))
    try:
        setup_conn.execute(
            """
            CREATE TABLE operational_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                source TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                persistence_status TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL
            )
            """
        )
        # Seed events out of order.
        for ts_hour in (3, 1, 2):
            ts = f"2026-05-15T0{ts_hour}:00:00+00:00"
            setup_conn.execute(
                "INSERT INTO operational_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"evt-h{ts_hour}",
                    OperationalEventType.START.value,
                    OperationalEventSeverity.INFO.value,
                    OperationalEventSource.ORCHESTRATOR.value,
                    OperationalEventReasonCode.STARTUP.value,
                    "{}",
                    OperationalEventPersistenceStatus.PERSISTED.value,
                    ts,
                    ts,
                ),
            )
        setup_conn.commit()
    finally:
        setup_conn.close()

    result = feed_mod.fetch_activity_feed(db_file, limit=10)
    assert len(result.items) == 3
    timestamps = [item.timestamp_utc for item in result.items]
    # Newest first.
    assert timestamps == sorted(timestamps, reverse=True)


def test_fetch_activity_feed_stable_order_for_duplicate_timestamps(tmp_path) -> None:
    """Items with identical timestamps must order by id ascending."""
    import src.observability.dashboard_activity_feed as feed_mod

    db_file = tmp_path / "poly_oracle.db"
    setup_conn = sqlite3.connect(str(db_file))
    try:
        setup_conn.execute(
            """
            CREATE TABLE operational_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                source TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                persistence_status TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL
            )
            """
        )
        same_ts = "2026-05-15T01:00:00+00:00"
        for evt_id in ("bbbbbbbb", "aaaaaaaa"):
            setup_conn.execute(
                "INSERT INTO operational_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evt_id,
                    OperationalEventType.START.value,
                    OperationalEventSeverity.INFO.value,
                    OperationalEventSource.ORCHESTRATOR.value,
                    OperationalEventReasonCode.STARTUP.value,
                    "{}",
                    OperationalEventPersistenceStatus.PERSISTED.value,
                    same_ts,
                    same_ts,
                ),
            )
        setup_conn.commit()
    finally:
        setup_conn.close()

    result1 = feed_mod.fetch_activity_feed(db_file, limit=10)
    result2 = feed_mod.fetch_activity_feed(db_file, limit=10)
    ids1 = [item.event_id for item in result1.items]
    ids2 = [item.event_id for item in result2.items]
    assert ids1 == ids2  # deterministic
    assert ids1 == ["aaaaaaaa", "bbbbbbbb"]  # tie-broken by id ascending


# ═══════════════════════════════════════════════════════════════════════════
# Current-state derivation
# ═══════════════════════════════════════════════════════════════════════════


def test_derive_current_state_is_defined_in_dashboard() -> None:
    if not _DASHBOARD_HELPERS_AVAILABLE:
        raise NotImplementedError("derive_current_state not implemented")
    assert callable(derive_current_state)


def test_derive_current_state_returns_dashboard_current_state() -> None:
    """derive_current_state returns a typed DashboardCurrentState."""
    state = derive_current_state([])
    assert isinstance(state, DashboardCurrentState)
    assert state.overall_state == "unknown"
    assert state.timestamp_utc.tzinfo is not None


def test_derive_current_state_handles_no_events() -> None:
    """With no events, all category summaries stay None and overall is unknown."""
    state = derive_current_state([])
    assert state.lifecycle_summary is None
    assert state.readiness_summary is None
    assert state.websocket_summary is None
    assert state.llm_summary is None
    assert state.decision_summary is None
    assert state.execution_summary is None
    assert state.circuit_breaker_summary is None
    assert state.overall_state == "unknown"


def test_derive_current_state_derives_lifecycle_from_latest() -> None:
    """derive_current_state surfaces the most recent lifecycle event."""
    items = [
        DashboardActivityFeedItem(
            event_id="evt-1",
            event_type=OperationalEventType.START,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
            template_key=NarrativeTemplateKey.LIFECYCLE_START,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="The agent started up.",
            continuation_state="continued",
            timestamp_utc=_utc(hour=1),
        ),
    ]
    state = derive_current_state(items)
    assert state.lifecycle_summary == "The agent started up."
    assert state.overall_state == "continued"


def test_derive_current_state_derives_readiness_from_latest() -> None:
    """derive_current_state surfaces the most recent readiness event."""
    items = [
        DashboardActivityFeedItem(
            event_id="evt-1",
            event_type=OperationalEventType.READY_STATE_CHANGED,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.READY,
            template_key=NarrativeTemplateKey.READINESS_READY,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="Readiness returned to READY.",
            continuation_state="continued",
            timestamp_utc=_utc(hour=1),
        ),
    ]
    state = derive_current_state(items)
    assert state.readiness_summary == "Readiness returned to READY."


def test_derive_current_state_derives_websocket_from_latest() -> None:
    """derive_current_state surfaces the most recent WebSocket health event."""
    items = [
        DashboardActivityFeedItem(
            event_id="evt-1",
            event_type=OperationalEventType.WS_PONG_STALE,
            severity=OperationalEventSeverity.WARNING,
            source=OperationalEventSource.INGESTION,
            reason_code=OperationalEventReasonCode.WS_PONG_TIMEOUT,
            template_key=NarrativeTemplateKey.WS_PONG_STALE,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="WebSocket missed liveness pings.",
            continuation_state="degraded",
            timestamp_utc=_utc(hour=1),
        ),
    ]
    state = derive_current_state(items)
    assert state.websocket_summary == "WebSocket missed liveness pings."


def test_derive_current_state_derives_llm_provider_from_latest() -> None:
    """derive_current_state surfaces the most recent LLM/provider/budget event."""
    items = [
        DashboardActivityFeedItem(
            event_id="evt-1",
            event_type=OperationalEventType.PROVIDER_FAILURE,
            severity=OperationalEventSeverity.WARNING,
            source=OperationalEventSource.EVALUATION,
            reason_code=OperationalEventReasonCode.PROVIDER_CALL_FAILED,
            template_key=NarrativeTemplateKey.PROVIDER_CALL_FAILED,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="Provider call failed.",
            continuation_state="skipped",
            timestamp_utc=_utc(hour=1),
        ),
    ]
    state = derive_current_state(items)
    assert state.llm_summary == "Provider call failed."


def test_derive_current_state_derives_decision_from_latest() -> None:
    """derive_current_state surfaces the most recent decision event."""
    items = [
        DashboardActivityFeedItem(
            event_id="evt-1",
            event_type=OperationalEventType.DECISION_ACCEPTED,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.EVALUATION,
            reason_code=OperationalEventReasonCode.DECISION_HOLD,
            template_key=NarrativeTemplateKey.DECISION_ACCEPTED_HOLD,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="Evaluation accepted with HOLD action.",
            continuation_state="continued",
            timestamp_utc=_utc(hour=1),
        ),
    ]
    state = derive_current_state(items)
    assert state.decision_summary == "Evaluation accepted with HOLD action."


def test_derive_current_state_preserves_dry_run_execution() -> None:
    """derive_current_state preserves dry-run execution context."""
    items = [
        DashboardActivityFeedItem(
            event_id="evt-1",
            event_type=OperationalEventType.EXECUTION_DRY_RUN,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.EXECUTION,
            reason_code=OperationalEventReasonCode.EXEC_DRY_RUN_SKIP,
            template_key=NarrativeTemplateKey.EXECUTION_DRY_RUN,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="Execution simulated; no live signing or broadcasting occurred.",
            continuation_state="continued",
            dry_run=True,
            timestamp_utc=_utc(hour=1),
        ),
    ]
    state = derive_current_state(items)
    assert state.execution_summary is not None
    assert "simulated" in state.execution_summary.lower() or "dry" in state.execution_summary.lower()


def test_derive_current_state_derives_circuit_breaker_from_latest() -> None:
    """derive_current_state surfaces the most recent circuit breaker event."""
    items = [
        DashboardActivityFeedItem(
            event_id="evt-1",
            event_type=OperationalEventType.CIRCUIT_BREAKER_OPEN,
            severity=OperationalEventSeverity.CRITICAL,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.CB_OPEN,
            template_key=NarrativeTemplateKey.CIRCUIT_BREAKER_OPEN,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="Circuit breaker opened; new BUY routing blocked.",
            continuation_state="degraded",
            timestamp_utc=_utc(hour=1),
        ),
    ]
    state = derive_current_state(items)
    assert state.circuit_breaker_summary == "Circuit breaker opened; new BUY routing blocked."
    assert state.overall_state == "degraded"


# ═══════════════════════════════════════════════════════════════════════════
# Regression: MAAP findings (WI-59)
# ═══════════════════════════════════════════════════════════════════════════


def test_derive_current_state_newer_recovery_supersedes_older_degraded() -> None:
    """MAAP HIGH regression: latest recovery event must override older degradation.

    The original implementation scanned the newest-10 prefix for any
    ``stopped``/``degraded`` continuation_state and returned that, which
    let a stale degraded event hold the panel in ``degraded`` even after
    the agent recovered. The fix is strict latest-wins semantics.
    """
    items = [
        # Newest event: READY recovery (continued).
        DashboardActivityFeedItem(
            event_id="evt-newer",
            event_type=OperationalEventType.READY_STATE_CHANGED,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.READY,
            template_key=NarrativeTemplateKey.READINESS_READY,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="Readiness returned to READY.",
            continuation_state="continued",
            timestamp_utc=_utc(hour=3),
        ),
        # Older event: DEGRADED.
        DashboardActivityFeedItem(
            event_id="evt-older",
            event_type=OperationalEventType.READY_STATE_CHANGED,
            severity=OperationalEventSeverity.WARNING,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.DEGRADED,
            template_key=NarrativeTemplateKey.READINESS_DEGRADED,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="Readiness degraded; trading paths restricted.",
            continuation_state="degraded",
            timestamp_utc=_utc(hour=1),
        ),
    ]
    state = derive_current_state(items)
    assert state.overall_state == "continued", (
        "Newer recovery event must override older degraded event in overall_state"
    )


def test_derive_current_state_newer_recovery_supersedes_older_stopped() -> None:
    """MAAP HIGH regression: latest CB_CLOSED must override older CB_OPEN."""
    items = [
        # Newest event: CB_CLOSED (continued).
        DashboardActivityFeedItem(
            event_id="evt-cb-closed",
            event_type=OperationalEventType.CIRCUIT_BREAKER_CLOSED,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.CB_CLOSED,
            template_key=NarrativeTemplateKey.CIRCUIT_BREAKER_CLOSED,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="Circuit breaker closed.",
            continuation_state="continued",
            timestamp_utc=_utc(hour=3),
        ),
        # Older event: CB_OPEN (degraded).
        DashboardActivityFeedItem(
            event_id="evt-cb-open",
            event_type=OperationalEventType.CIRCUIT_BREAKER_OPEN,
            severity=OperationalEventSeverity.CRITICAL,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.CB_OPEN,
            template_key=NarrativeTemplateKey.CIRCUIT_BREAKER_OPEN,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="Circuit breaker opened.",
            continuation_state="degraded",
            timestamp_utc=_utc(hour=1),
        ),
    ]
    state = derive_current_state(items)
    assert state.overall_state == "continued"


def test_malformed_readiness_payload_uses_typed_state_fallback() -> None:
    """MAAP regression: malformed latest NOT_READY still reports stopped.

    Narrative rendering falls back for malformed JSON and does not trust
    payload fields. The dashboard may still derive current state from the
    bounded typed event/reason enums.
    """
    import src.observability.dashboard_activity_feed as feed_mod
    from src.observability.operational_narratives import render_event

    record = _make_record(
        event_type=OperationalEventType.READY_STATE_CHANGED,
        reason_code=OperationalEventReasonCode.NOT_READY,
        severity=OperationalEventSeverity.CRITICAL,
        payload_json="{not valid json",
        timestamp=_utc(hour=3),
    )

    narrative = render_event(record)
    item = feed_mod._record_to_item(record, narrative)

    assert narrative.status == NarrativeRenderStatus.FALLBACK
    assert item is not None
    assert item.continuation_state == "stopped"
    state = derive_current_state([item])
    assert state.overall_state == "stopped"
    assert state.readiness_summary == item.summary


def test_redacted_readiness_payload_uses_typed_state_fallback() -> None:
    """MAAP regression: redacted latest DEGRADED still reports degraded."""
    import src.observability.dashboard_activity_feed as feed_mod
    from src.observability.operational_narratives import render_event

    forbidden = "0x" + "b" * 40
    record = _make_record(
        event_type=OperationalEventType.READY_STATE_CHANGED,
        reason_code=OperationalEventReasonCode.DEGRADED,
        severity=OperationalEventSeverity.WARNING,
        payload_json=json.dumps({"message": f"unsafe {forbidden}"}),
        timestamp=_utc(hour=3),
    )

    narrative = render_event(record)
    item = feed_mod._record_to_item(record, narrative)

    assert narrative.status == NarrativeRenderStatus.REDACTED
    assert item is not None
    assert item.continuation_state == "degraded"
    assert forbidden not in item.summary
    state = derive_current_state([item])
    assert state.overall_state == "degraded"


def test_fetch_activity_feed_drops_rows_with_null_timestamps(tmp_path) -> None:
    """MAAP MEDIUM regression: NULL persisted timestamps must not become ``now()``.

    A corrupt row whose ``created_at_utc`` is missing was previously
    promoted into the feed with a fabricated ``datetime.now(timezone.utc)``
    timestamp, breaking ordering determinism and potentially placing
    corrupt data at the top of the timeline. The fix is to drop such
    rows at the data-ingress layer.
    """
    import src.observability.dashboard_activity_feed as feed_mod

    db_file = tmp_path / "poly_oracle.db"
    setup_conn = sqlite3.connect(str(db_file))
    try:
        # Schema deliberately allows NULL on created_at_utc / recorded_at_utc
        # so we can simulate the corrupt-row condition.
        setup_conn.execute(
            """
            CREATE TABLE operational_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                source TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                persistence_status TEXT NOT NULL,
                created_at_utc TEXT,
                recorded_at_utc TEXT
            )
            """
        )
        # Insert one valid row.
        setup_conn.execute(
            "INSERT INTO operational_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "evt-valid",
                OperationalEventType.START.value,
                OperationalEventSeverity.INFO.value,
                OperationalEventSource.ORCHESTRATOR.value,
                OperationalEventReasonCode.STARTUP.value,
                "{}",
                OperationalEventPersistenceStatus.PERSISTED.value,
                "2026-05-15T01:00:00+00:00",
                "2026-05-15T01:00:00+00:00",
            ),
        )
        # Insert one corrupt row with NULL timestamps.
        setup_conn.execute(
            "INSERT INTO operational_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "evt-null-ts",
                OperationalEventType.START.value,
                OperationalEventSeverity.INFO.value,
                OperationalEventSource.ORCHESTRATOR.value,
                OperationalEventReasonCode.STARTUP.value,
                "{}",
                OperationalEventPersistenceStatus.PERSISTED.value,
                None,
                None,
            ),
        )
        setup_conn.commit()
    finally:
        setup_conn.close()

    result = feed_mod.fetch_activity_feed(db_file, limit=10)
    event_ids = [item.event_id for item in result.items]
    assert "evt-valid" in event_ids
    assert "evt-null-ts" not in event_ids, (
        "Corrupt row with NULL timestamps must be dropped, not promoted "
        "with a fabricated now() timestamp"
    )


def test_fetch_activity_feed_drops_rows_with_unparseable_timestamps(tmp_path) -> None:
    """MAAP MEDIUM regression: unparseable timestamps must not become ``now()``."""
    import src.observability.dashboard_activity_feed as feed_mod

    db_file = tmp_path / "poly_oracle.db"
    setup_conn = sqlite3.connect(str(db_file))
    try:
        setup_conn.execute(
            """
            CREATE TABLE operational_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                source TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                persistence_status TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                recorded_at_utc TEXT NOT NULL
            )
            """
        )
        # Insert one valid row.
        setup_conn.execute(
            "INSERT INTO operational_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "evt-valid",
                OperationalEventType.START.value,
                OperationalEventSeverity.INFO.value,
                OperationalEventSource.ORCHESTRATOR.value,
                OperationalEventReasonCode.STARTUP.value,
                "{}",
                OperationalEventPersistenceStatus.PERSISTED.value,
                "2026-05-15T01:00:00+00:00",
                "2026-05-15T01:00:00+00:00",
            ),
        )
        # Insert one corrupt row with unparseable timestamp text.
        setup_conn.execute(
            "INSERT INTO operational_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "evt-bad-ts",
                OperationalEventType.START.value,
                OperationalEventSeverity.INFO.value,
                OperationalEventSource.ORCHESTRATOR.value,
                OperationalEventReasonCode.STARTUP.value,
                "{}",
                OperationalEventPersistenceStatus.PERSISTED.value,
                "not-a-real-timestamp",
                "also-not-a-timestamp",
            ),
        )
        setup_conn.commit()
    finally:
        setup_conn.close()

    result = feed_mod.fetch_activity_feed(db_file, limit=10)
    event_ids = [item.event_id for item in result.items]
    assert "evt-valid" in event_ids
    assert "evt-bad-ts" not in event_ids


def test_parse_sqlite_timestamp_returns_none_for_missing_or_invalid() -> None:
    """MAAP MEDIUM regression: the parser must not fabricate timestamps."""
    import src.observability.dashboard_activity_feed as feed_mod

    assert feed_mod._parse_sqlite_timestamp(None) is None
    assert feed_mod._parse_sqlite_timestamp("not-a-timestamp") is None
    assert feed_mod._parse_sqlite_timestamp("") is None
    # Still returns a real tz-aware datetime for valid input.
    parsed = feed_mod._parse_sqlite_timestamp("2026-05-15T01:00:00+00:00")
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_invalid_timestamp_log_does_not_echo_raw_persisted_values(monkeypatch) -> None:
    """MAAP regression: corrupt-row logs must remain low-cardinality.

    Rows with invalid timestamps are dropped before enum validation. The
    warning emitted on that path must not echo raw persisted event_type or
    reason_code values because those columns may be corrupt, secret-like,
    or high-cardinality.
    """
    import src.observability.dashboard_activity_feed as feed_mod

    captured: list[tuple[str, dict]] = []

    class FakeLogger:
        def warning(self, event: str, **kwargs) -> None:
            captured.append((event, kwargs))

    monkeypatch.setattr(feed_mod, "logger", FakeLogger())
    leaked_value = "0x" + "a" * 40

    record = feed_mod._row_to_record(
        (
            "evt-bad",
            leaked_value,
            OperationalEventSeverity.INFO.value,
            OperationalEventSource.ORCHESTRATOR.value,
            "raw-market-id-" + leaked_value,
            "{}",
            OperationalEventPersistenceStatus.PERSISTED.value,
            None,
            None,
        )
    )

    assert record is None
    assert captured == [
        (
            "dashboard_activity_feed.row.invalid_timestamp_dropped",
            {"failure_reason": "invalid_timestamp"},
        )
    ]
    assert leaked_value not in repr(captured)


# ═══════════════════════════════════════════════════════════════════════════
# Rendering helpers
# ═══════════════════════════════════════════════════════════════════════════


def test_format_activity_row_html_escapes_values() -> None:
    """format_activity_row_html escapes potentially HTML-injecting payloads.

    Schema-level forbidden-content scan blocks raw secrets, but the
    summary string may legitimately contain ``<`` or ``&`` characters
    via narrative templates; the renderer must escape these.
    """
    item = DashboardActivityFeedItem(
        event_id="evt-1",
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.READY,
        template_key=NarrativeTemplateKey.READINESS_READY,
        narrative_status=NarrativeRenderStatus.SUCCESS,
        summary="Readiness now < READY > & continued.",
        timestamp_utc=_utc(hour=0),
    )
    html = format_activity_row_html(item)
    assert "<" not in html.split("summary")[0] or True
    # Summary characters must be escaped in the rendered output.
    assert "&lt; READY &gt;" in html
    assert "&amp;" in html
    # The raw < character should NOT appear in the rendered text content
    # (it should be escaped). Cell tags themselves are still <td>.
    assert "< READY >" not in html


def test_format_activity_row_html_secret_safe() -> None:
    """DashboardActivityFeedItem schema rejects secret-bearing summaries.

    The renderer never sees a secret-bearing item because schema
    validation blocks construction.
    """
    with pytest.raises(ValidationError):
        DashboardActivityFeedItem(
            event_id="evt-1",
            event_type=OperationalEventType.READY_STATE_CHANGED,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.READY,
            template_key=NarrativeTemplateKey.READINESS_READY,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="private_key 0x" + "a" * 64,
            timestamp_utc=_utc(hour=0),
        )


def test_format_activity_row_html_rejects_high_cardinality_identifiers() -> None:
    """Schema rejects token IDs / wallet addresses inside summary."""
    with pytest.raises(ValidationError):
        DashboardActivityFeedItem(
            event_id="evt-1",
            event_type=OperationalEventType.MARKET_DISCOVERED,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.INGESTION,
            reason_code=OperationalEventReasonCode.MARKET_FOUND,
            template_key=NarrativeTemplateKey.MARKET_DISCOVERED,
            narrative_status=NarrativeRenderStatus.SUCCESS,
            summary="Market 0x" + "a" * 40 + " leaked",  # wallet-address-like
            timestamp_utc=_utc(hour=0),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Purity invariants
# ═══════════════════════════════════════════════════════════════════════════


def test_dashboard_module_does_not_import_execution_routing() -> None:
    if not _DASHBOARD_HELPERS_AVAILABLE:
        raise NotImplementedError("dashboard module not available")
    text = Path(dashboard_mod.__file__).read_text()
    forbidden_imports = [
        "from src.agents.execution",
        "import ExecutionRouter",
        "from src.orchestrator",
        "from src.agents.evaluation.claude_client",
    ]
    for forbidden in forbidden_imports:
        assert forbidden not in text, (
            f"Dashboard must not import execution routing or LLM paths; found {forbidden!r}"
        )


def test_dashboard_module_does_not_call_create_all() -> None:
    if not _DASHBOARD_HELPERS_AVAILABLE:
        raise NotImplementedError("dashboard module not available")
    text = Path(dashboard_mod.__file__).read_text()
    assert "create_all" not in text


def test_dashboard_module_uses_read_only_sqlite_uri() -> None:
    if not _DASHBOARD_HELPERS_AVAILABLE:
        raise NotImplementedError("dashboard module not available")
    text = Path(dashboard_mod.__file__).read_text()
    assert "mode=ro" in text


def test_dashboard_module_does_not_import_base_metadata() -> None:
    if not _DASHBOARD_HELPERS_AVAILABLE:
        raise NotImplementedError("dashboard module not available")
    text = Path(dashboard_mod.__file__).read_text()
    assert "Base.metadata" not in text


def test_operational_event_repository_has_no_update_or_delete_methods() -> None:
    """OperationalEventRepository must remain append/read-only."""
    from src.db.repositories.operational_event_repository import (
        OperationalEventRepository,
    )

    forbidden = {"update", "delete", "backfill", "remove", "purge"}
    for name in dir(OperationalEventRepository):
        if name.startswith("_"):
            continue
        assert name.lower() not in forbidden, (
            f"OperationalEventRepository must remain append/read-only; "
            f"found public method {name!r}"
        )
