"""
src/observability/dashboard_activity_feed.py

WI-59 — Dashboard Activity Feed service.

Read-only helpers that turn the durable WI-56 operational event ledger
into typed, secret-safe rows for the Streamlit dashboard activity
timeline and the "what is the bot doing right now?" current-state
panel. The dashboard module imports these helpers; nothing in this
module imports Streamlit.

Constraints:

* Reads exclusively through bounded SQLite ``mode=ro`` URI connections
  or, in tests, through ``OperationalEventRepository`` via the existing
  async test session factory.
* Never appends, updates, deletes, or backfills operational events.
* Never imports or invokes LLM clients, execution routing, signing,
  broadcasting, or live wallet mutation paths.
* Never performs trading, sizing, EV, Kelly, PnL, exposure, or
  provider-cost calculations.
* Output rows are validated against the WI-56 secret/high-cardinality
  scan at the ``DashboardActivityFeedItem`` schema boundary; the
  current-state panel never invents readiness, provider, market,
  decision, or dry-run data.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Optional

import structlog

from src.observability.operational_narratives import render_event
from src.schemas.ops import (
    DashboardActivityFeedFailureReason,
    DashboardActivityFeedFilter,
    DashboardActivityFeedItem,
    DashboardActivityFeedResult,
    DashboardActivityFeedStatus,
    DashboardCurrentState,
    NarrativeRenderResult,
    NarrativeRenderStatus,
    NarrativeTemplateKey,
    OperationalEventPersistenceStatus,
    OperationalEventReasonCode,
    OperationalEventRecord,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
    _scan_event_payload,
)

logger = structlog.get_logger(__name__)


# ── Bounded limits and lookback ────────────────────────────────────────────

# Hard upper bound on rows fetched per call. Matches the
# ``OperationalEventQuery.limit`` repository cap.
ACTIVITY_FEED_HARD_LIMIT: int = 1000

# Default lookback used when callers do not supply explicit window bounds.
# The dashboard does not show historical archives; recent operator-relevant
# events are sufficient and bounded.
DEFAULT_ACTIVITY_LIMIT: int = 200

# Fallback message used only when narrative render returned no narrative.
_FALLBACK_GENERIC_SUMMARY: str = (
    "An operational event was recorded but did not match a known narrative "
    "template; review the ledger for typed context."
)


# ── Shared helpers ─────────────────────────────────────────────────────────


def _as_utc(dt: datetime) -> datetime:
    """Ensure a datetime is tz-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _sort_records_recent_first(
    records: list[OperationalEventRecord],
) -> list[OperationalEventRecord]:
    """Sort records newest-first; tie-break on stable persisted id ascending.

    The dashboard timeline is operator-scanning, so newer events appear
    at the top. Identical timestamps are tie-broken by ``id`` ascending
    so rendering is stable across refreshes.
    """
    return sorted(records, key=lambda r: (-r.created_at_utc.timestamp(), r.id))


def _record_to_item(
    record: OperationalEventRecord,
    result: NarrativeRenderResult,
) -> Optional[DashboardActivityFeedItem]:
    """Build a typed feed item from a narrative render result.

    Returns ``None`` if the resulting item would expose forbidden content
    even after the WI-57 redaction path; the caller drops such records
    rather than displaying unsafe text.
    """
    narrative = result.narrative
    timestamp = _as_utc(record.created_at_utc)

    if narrative is None:
        summary = _FALLBACK_GENERIC_SUMMARY
        template_key = NarrativeTemplateKey.GENERIC
        continuation_state: Optional[str] = None
        dry_run: Optional[bool] = None
    else:
        if narrative.kind == "operational":
            assert narrative.operational is not None
            inner = narrative.operational
        else:
            assert narrative.decision is not None
            inner = narrative.decision
        summary = inner.summary
        template_key = inner.template_key
        continuation_state = inner.continuation_state
        dry_run = inner.dry_run

    if continuation_state is None:
        continuation_state = _continuation_state_from_typed_event(
            record.event_type,
            record.reason_code,
        )

    # Defense-in-depth scan. The narrative layer already scans; this is
    # the last hop before operator output.
    if _scan_event_payload(summary):
        logger.warning(
            "dashboard_activity_feed.item.forbidden_content_blocked",
            event_type=record.event_type.value,
            reason_code=record.reason_code.value,
        )
        return None

    try:
        return DashboardActivityFeedItem(
            event_id=record.id,
            event_type=record.event_type,
            severity=record.severity,
            source=record.source,
            reason_code=record.reason_code,
            template_key=template_key,
            narrative_status=result.status,
            summary=summary,
            continuation_state=continuation_state,
            dry_run=dry_run,
            timestamp_utc=timestamp,
        )
    except Exception:  # noqa: BLE001 — defensive boundary; never crash dashboard
        logger.warning(
            "dashboard_activity_feed.item.construction_failed",
            event_type=record.event_type.value,
            reason_code=record.reason_code.value,
        )
        return None


# ── SQLite read path (Streamlit synchronous dashboard) ─────────────────────


def _resolve_ro_uri(db_path: Path) -> str:
    """Build a read-only SQLite URI for the dashboard database."""
    return f"file:{db_path.resolve()}?mode=ro"


def _operational_events_table_exists(conn: sqlite3.Connection) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='operational_events'"
    )
    return cur.fetchone() is not None


def _row_to_record(row: tuple) -> Optional[OperationalEventRecord]:
    """Convert a raw sqlite row into a typed OperationalEventRecord.

    Returns ``None`` when the row contains an unknown enum value, has a
    missing or unparseable ``created_at_utc`` / ``recorded_at_utc``, or
    is otherwise unparseable. The caller drops such rows from rendering
    so that corrupt persisted rows never get promoted into the activity
    timeline or the current-state panel with fabricated timestamps.
    """
    try:
        (
            event_id,
            event_type,
            severity,
            source,
            reason_code,
            payload_json,
            persistence_status,
            created_at_str,
            recorded_at_str,
        ) = row
        created_at = _parse_sqlite_timestamp(created_at_str)
        recorded_at = _parse_sqlite_timestamp(recorded_at_str)
        if created_at is None or recorded_at is None:
            # Refuse to invent timestamps for corrupt or missing values.
            # Such rows are treated as invalid persisted data and dropped.
            logger.warning(
                "dashboard_activity_feed.row.invalid_timestamp_dropped",
                failure_reason="invalid_timestamp",
            )
            return None
        return OperationalEventRecord(
            id=event_id,
            event_type=OperationalEventType(event_type),
            severity=OperationalEventSeverity(severity),
            source=OperationalEventSource(source),
            reason_code=OperationalEventReasonCode(reason_code),
            payload_json=payload_json or "{}",
            persistence_status=OperationalEventPersistenceStatus(persistence_status),
            created_at_utc=created_at,
            recorded_at_utc=recorded_at,
        )
    except (ValueError, TypeError, KeyError):
        return None


def _parse_sqlite_timestamp(value: object) -> Optional[datetime]:
    """Parse a SQLite timestamp value into a tz-aware UTC datetime.

    Returns ``None`` when the value is missing or unparseable. The
    dashboard never invents timestamps for corrupt persisted rows;
    fabricating a ``now()`` value would make ordering non-deterministic
    and could promote a corrupt row into the current-state panel. Such
    rows are filtered out at the data-ingress layer instead.
    """
    if isinstance(value, datetime):
        return _as_utc(value)
    if value is None:
        return None
    text = str(value)
    # SQLite stores datetimes as ISO strings without offset by default.
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        if "T" not in text and " " in text:
            text = text.replace(" ", "T", 1)
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(dt)


def _filter_matches(
    record: OperationalEventRecord,
    filt: DashboardActivityFeedFilter,
) -> bool:
    """Apply typed filter to a record (intersect across populated fields)."""
    if filt.severities and record.severity not in filt.severities:
        return False
    if filt.sources and record.source not in filt.sources:
        return False
    if filt.event_types and record.event_type not in filt.event_types:
        return False
    if filt.reason_codes and record.reason_code not in filt.reason_codes:
        return False
    return True


def _read_records_from_sqlite(
    db_path: Path,
    *,
    limit: int,
) -> tuple[
    list[OperationalEventRecord],
    DashboardActivityFeedStatus,
    Optional[DashboardActivityFeedFailureReason],
    Optional[str],
]:
    """Read recent operational event records from the read-only SQLite URI.

    Returns the raw record list plus a status/failure_reason/message
    triplet so callers can construct the typed feed result.
    """
    if not db_path.exists():
        return (
            [],
            DashboardActivityFeedStatus.DATABASE_UNAVAILABLE,
            DashboardActivityFeedFailureReason.DATABASE_UNREACHABLE,
            "operational event database is not available",
        )

    uri = _resolve_ro_uri(db_path)
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.OperationalError:
        return (
            [],
            DashboardActivityFeedStatus.DATABASE_UNAVAILABLE,
            DashboardActivityFeedFailureReason.DATABASE_UNREACHABLE,
            "operational event database is unreachable",
        )

    try:
        with conn:
            if not _operational_events_table_exists(conn):
                return (
                    [],
                    DashboardActivityFeedStatus.MISSING_TABLE,
                    DashboardActivityFeedFailureReason.MISSING_TABLE,
                    "operational_events table is unavailable in this deployment",
                )

            bounded = max(1, min(limit, ACTIVITY_FEED_HARD_LIMIT))
            rows = conn.execute(
                """
                SELECT
                    id,
                    event_type,
                    severity,
                    source,
                    reason_code,
                    payload_json,
                    persistence_status,
                    created_at_utc,
                    recorded_at_utc
                FROM operational_events
                ORDER BY created_at_utc DESC, id ASC
                LIMIT ?
                """,
                (bounded,),
            ).fetchall()
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "no such table" in text:
            return (
                [],
                DashboardActivityFeedStatus.MISSING_TABLE,
                DashboardActivityFeedFailureReason.MISSING_TABLE,
                "operational_events table is unavailable in this deployment",
            )
        return (
            [],
            DashboardActivityFeedStatus.DATABASE_UNAVAILABLE,
            DashboardActivityFeedFailureReason.DATABASE_UNREACHABLE,
            "operational event database is unreachable",
        )
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001 — defensive
            pass

    records: list[OperationalEventRecord] = []
    for row in rows:
        record = _row_to_record(row)
        if record is not None:
            records.append(record)

    return records, DashboardActivityFeedStatus.SUCCESS, None, None


# ── Repository read path (async, used by integration tests) ────────────────


async def _read_records_via_repository(
    session_factory,
    *,
    limit: int,
) -> tuple[
    list[OperationalEventRecord],
    DashboardActivityFeedStatus,
    Optional[DashboardActivityFeedFailureReason],
    Optional[str],
]:
    """Read records via OperationalEventRepository for async integration paths."""
    from sqlalchemy.exc import OperationalError, SQLAlchemyError

    from src.db.repositories.operational_event_repository import (
        OperationalEventRepository,
    )
    from src.schemas.ops import OperationalEventQuery

    bounded = max(1, min(limit, ACTIVITY_FEED_HARD_LIMIT))
    query = OperationalEventQuery(limit=bounded)

    try:
        async with session_factory() as session:
            repo = OperationalEventRepository(session)
            window = await repo.read_window(query)
    except OperationalError as exc:
        text = str(exc).lower()
        if "no such table" in text or "operational_events" in text:
            return (
                [],
                DashboardActivityFeedStatus.MISSING_TABLE,
                DashboardActivityFeedFailureReason.MISSING_TABLE,
                "operational_events table is unavailable in this deployment",
            )
        return (
            [],
            DashboardActivityFeedStatus.DATABASE_UNAVAILABLE,
            DashboardActivityFeedFailureReason.DATABASE_UNREACHABLE,
            "operational event database is unreachable",
        )
    except SQLAlchemyError:
        return (
            [],
            DashboardActivityFeedStatus.DATABASE_UNAVAILABLE,
            DashboardActivityFeedFailureReason.DATABASE_UNREACHABLE,
            "operational event repository read failed",
        )

    return list(window.events), DashboardActivityFeedStatus.SUCCESS, None, None


# ── Current-state derivation ──────────────────────────────────────────────


_LIFECYCLE_EVENT_TYPES = {
    OperationalEventType.START,
    OperationalEventType.SHUTDOWN,
    OperationalEventType.CONFIG_LOADED,
}

_WS_EVENT_TYPES = {
    OperationalEventType.WS_CONNECTED,
    OperationalEventType.WS_RECONNECT,
    OperationalEventType.WS_PONG_STALE,
}

_LLM_EVENT_TYPES = {
    OperationalEventType.LLM_CALL_STARTED,
    OperationalEventType.LLM_CALL_BLOCKED,
    OperationalEventType.PROVIDER_FAILURE,
    OperationalEventType.BUDGET_BLOCK,
    OperationalEventType.COOLDOWN_BLOCK,
}

_DECISION_EVENT_TYPES = {
    OperationalEventType.DECISION_ACCEPTED,
    OperationalEventType.DECISION_SKIPPED,
}

_CIRCUIT_BREAKER_EVENT_TYPES = {
    OperationalEventType.CIRCUIT_BREAKER_OPEN,
    OperationalEventType.CIRCUIT_BREAKER_CLOSED,
    OperationalEventType.ALERT_SENT,
}


_ALLOWED_CONTINUATION_STATES = frozenset(
    {"continued", "skipped", "degraded", "stopped"}
)

_CONTINUATION_STATE_BY_TYPED_EVENT: dict[
    tuple[OperationalEventType, OperationalEventReasonCode],
    str,
] = {
    (OperationalEventType.START, OperationalEventReasonCode.STARTUP): "continued",
    (
        OperationalEventType.SHUTDOWN,
        OperationalEventReasonCode.GRACEFUL_SHUTDOWN,
    ): "stopped",
    (
        OperationalEventType.SHUTDOWN,
        OperationalEventReasonCode.FORCED_SHUTDOWN,
    ): "stopped",
    (OperationalEventType.CONFIG_LOADED, OperationalEventReasonCode.CONFIG_VALID): (
        "continued"
    ),
    (OperationalEventType.CONFIG_LOADED, OperationalEventReasonCode.CONFIG_INVALID): (
        "stopped"
    ),
    (OperationalEventType.MARKET_DISCOVERED, OperationalEventReasonCode.MARKET_FOUND): (
        "continued"
    ),
    (
        OperationalEventType.MARKET_DISCOVERED,
        OperationalEventReasonCode.MARKET_ELIGIBLE,
    ): "continued",
    (
        OperationalEventType.MARKET_REJECTED,
        OperationalEventReasonCode.MARKET_INELIGIBLE,
    ): "skipped",
    (
        OperationalEventType.MARKET_REJECTED,
        OperationalEventReasonCode.MARKET_NOT_FOUND,
    ): "skipped",
    (
        OperationalEventType.MARKET_REJECTED,
        OperationalEventReasonCode.MARKET_COOLDOWN,
    ): ("skipped"),
    (
        OperationalEventType.MARKET_QUARANTINE,
        OperationalEventReasonCode.MARKET_QUARANTINED,
    ): "skipped",
    (OperationalEventType.WS_CONNECTED, OperationalEventReasonCode.WS_ESTABLISHED): (
        "continued"
    ),
    (OperationalEventType.WS_RECONNECT, OperationalEventReasonCode.WS_RECONNECTED): (
        "continued"
    ),
    (OperationalEventType.WS_PONG_STALE, OperationalEventReasonCode.WS_PONG_TIMEOUT): (
        "degraded"
    ),
    (OperationalEventType.READY_STATE_CHANGED, OperationalEventReasonCode.READY): (
        "continued"
    ),
    (OperationalEventType.READY_STATE_CHANGED, OperationalEventReasonCode.DEGRADED): (
        "degraded"
    ),
    (OperationalEventType.READY_STATE_CHANGED, OperationalEventReasonCode.NOT_READY): (
        "stopped"
    ),
    (OperationalEventType.BUDGET_BLOCK, OperationalEventReasonCode.BUDGET_DAILY): (
        "skipped"
    ),
    (OperationalEventType.BUDGET_BLOCK, OperationalEventReasonCode.BUDGET_HOURLY): (
        "skipped"
    ),
    (OperationalEventType.BUDGET_BLOCK, OperationalEventReasonCode.BUDGET_TOKEN): (
        "skipped"
    ),
    (OperationalEventType.BUDGET_BLOCK, OperationalEventReasonCode.BUDGET_COST): (
        "skipped"
    ),
    (
        OperationalEventType.BUDGET_BLOCK,
        OperationalEventReasonCode.BUDGET_REFLECTION,
    ): "skipped",
    (
        OperationalEventType.COOLDOWN_BLOCK,
        OperationalEventReasonCode.COOLDOWN_REPEATED_HOLD,
    ): "skipped",
    (
        OperationalEventType.COOLDOWN_BLOCK,
        OperationalEventReasonCode.COOLDOWN_REPEATED_INVALID,
    ): "skipped",
    (
        OperationalEventType.PROVIDER_FAILURE,
        OperationalEventReasonCode.PROVIDER_CALL_FAILED,
    ): "skipped",
    (
        OperationalEventType.PROVIDER_FAILURE,
        OperationalEventReasonCode.PROVIDER_RESPONSE_MALFORMED,
    ): "skipped",
    (OperationalEventType.DECISION_ACCEPTED, OperationalEventReasonCode.DECISION_BUY): (
        "continued"
    ),
    (
        OperationalEventType.DECISION_ACCEPTED,
        OperationalEventReasonCode.DECISION_HOLD,
    ): ("continued"),
    (
        OperationalEventType.DECISION_SKIPPED,
        OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
    ): "skipped",
    (
        OperationalEventType.DECISION_SKIPPED,
        OperationalEventReasonCode.DECISION_SKIP_LOW_EV,
    ): "skipped",
    (
        OperationalEventType.DECISION_SKIPPED,
        OperationalEventReasonCode.DECISION_SKIP_HIGH_SPREAD,
    ): "skipped",
    (
        OperationalEventType.DECISION_SKIPPED,
        OperationalEventReasonCode.DECISION_SKIP_EXPOSURE,
    ): "skipped",
    (
        OperationalEventType.DECISION_SKIPPED,
        OperationalEventReasonCode.DECISION_SKIP_TTR,
    ): "skipped",
    (
        OperationalEventType.EXECUTION_DRY_RUN,
        OperationalEventReasonCode.EXEC_DRY_RUN_SKIP,
    ): "continued",
    (OperationalEventType.CIRCUIT_BREAKER_OPEN, OperationalEventReasonCode.CB_OPEN): (
        "degraded"
    ),
    (
        OperationalEventType.CIRCUIT_BREAKER_CLOSED,
        OperationalEventReasonCode.CB_CLOSED,
    ): "continued",
    (
        OperationalEventType.CIRCUIT_BREAKER_CLOSED,
        OperationalEventReasonCode.CB_OVERRIDE,
    ): "continued",
    (OperationalEventType.ALERT_SENT, OperationalEventReasonCode.ALERT_DISPATCHED): (
        "continued"
    ),
    (
        OperationalEventType.ALERT_SENT,
        OperationalEventReasonCode.ALERT_DISPATCH_FAILED,
    ): "degraded",
    (OperationalEventType.ERROR_RECOVERED, OperationalEventReasonCode.ERROR_HANDLED): (
        "continued"
    ),
    (
        OperationalEventType.ERROR_RECOVERED,
        OperationalEventReasonCode.ERROR_UNHANDLED,
    ): ("degraded"),
}


def _continuation_state_from_typed_event(
    event_type: OperationalEventType,
    reason_code: OperationalEventReasonCode,
) -> Optional[str]:
    """Return a safe state fallback from bounded typed event metadata."""
    return _CONTINUATION_STATE_BY_TYPED_EVENT.get((event_type, reason_code))


def _resolve_item_continuation_state(item: DashboardActivityFeedItem) -> Optional[str]:
    """Resolve item state, falling back only to low-cardinality typed enums."""
    if item.continuation_state in _ALLOWED_CONTINUATION_STATES:
        return item.continuation_state
    return _continuation_state_from_typed_event(item.event_type, item.reason_code)


def _latest_summary(
    items: list[DashboardActivityFeedItem],
    accepted_event_types: set[OperationalEventType],
) -> tuple[Optional[str], Optional[str]]:
    """Return the (summary, continuation_state) of the most recent item
    whose event_type is in ``accepted_event_types``.

    Items are assumed already newest-first.
    """
    for item in items:
        if item.event_type in accepted_event_types:
            return item.summary, _resolve_item_continuation_state(item)
    return None, None


def _resolve_overall_state(
    items: list[DashboardActivityFeedItem],
) -> str:
    """Pick the operator-level overall state from the most recent typed item.

    Strict latest-wins semantics: the newest event's continuation_state is
    authoritative. The dashboard's purpose is to reflect what the bot is
    doing *right now*, so a newer recovery event (READY, CB_CLOSED,
    WS_RECONNECT, ERROR_HANDLED, ...) MUST visibly supersede an older
    ``stopped`` or ``degraded`` event. A prefix scan that promoted an
    older stopped/degraded event over a newer recovery would be wrong:
    it would persistently report stale degradation even after the
    operative state has recovered. ``unknown`` is returned when the
    feed is empty or the latest item has no typed continuation_state.
    """
    if not items:
        return "unknown"

    latest = _resolve_item_continuation_state(items[0])
    if latest in _ALLOWED_CONTINUATION_STATES:
        return latest
    return "unknown"


def derive_current_state(
    items: list[DashboardActivityFeedItem],
    *,
    now_utc: Optional[datetime] = None,
) -> DashboardCurrentState:
    """Build a typed ``what is the bot doing right now?`` panel state.

    Each summary field is filled only when a recent typed event supports
    that category. Fields stay ``None`` otherwise so the dashboard never
    invents readiness, provider, market, decision, or dry-run data.
    """
    timestamp = _as_utc(now_utc or datetime.now(timezone.utc))

    if not items:
        return DashboardCurrentState(
            lifecycle_summary=None,
            readiness_summary=None,
            websocket_summary=None,
            llm_summary=None,
            decision_summary=None,
            execution_summary=None,
            circuit_breaker_summary=None,
            overall_state="unknown",
            timestamp_utc=timestamp,
        )

    lifecycle_summary, _ = _latest_summary(items, _LIFECYCLE_EVENT_TYPES)
    readiness_summary, _ = _latest_summary(
        items, {OperationalEventType.READY_STATE_CHANGED}
    )
    websocket_summary, _ = _latest_summary(items, _WS_EVENT_TYPES)
    llm_summary, _ = _latest_summary(items, _LLM_EVENT_TYPES)
    decision_summary, _ = _latest_summary(items, _DECISION_EVENT_TYPES)
    execution_summary, _ = _latest_summary(
        items, {OperationalEventType.EXECUTION_DRY_RUN}
    )
    circuit_breaker_summary, _ = _latest_summary(items, _CIRCUIT_BREAKER_EVENT_TYPES)

    overall_state = _resolve_overall_state(items)

    return DashboardCurrentState(
        lifecycle_summary=lifecycle_summary,
        readiness_summary=readiness_summary,
        websocket_summary=websocket_summary,
        llm_summary=llm_summary,
        decision_summary=decision_summary,
        execution_summary=execution_summary,
        circuit_breaker_summary=circuit_breaker_summary,
        overall_state=overall_state,
        timestamp_utc=timestamp,
    )


# ── Public API: synchronous dashboard fetch ────────────────────────────────


def fetch_activity_feed(
    db_path: Path,
    *,
    limit: int = DEFAULT_ACTIVITY_LIMIT,
    filter: Optional[DashboardActivityFeedFilter] = None,
) -> DashboardActivityFeedResult:
    """Read the recent activity feed from the read-only SQLite ledger.

    The dashboard is synchronous Streamlit, so this function is sync.
    All database access is performed through a read-only SQLite URI;
    no writes are issued. Database / repository failures are caught
    and surfaced as typed non-success statuses with a bounded,
    low-cardinality message.
    """
    filt = filter or DashboardActivityFeedFilter()

    records, fetch_status, failure_reason, message = _read_records_from_sqlite(
        db_path, limit=limit
    )

    return _build_result_from_records(
        records=records,
        fetch_status=fetch_status,
        failure_reason=failure_reason,
        message=message,
        filt=filt,
    )


async def fetch_activity_feed_async(
    session_factory,
    *,
    limit: int = DEFAULT_ACTIVITY_LIMIT,
    filter: Optional[DashboardActivityFeedFilter] = None,
) -> DashboardActivityFeedResult:
    """Async version that reads through OperationalEventRepository.

    Used by integration tests that drive the helper against a real
    in-memory SQLite-backed repository.
    """
    filt = filter or DashboardActivityFeedFilter()

    records, fetch_status, failure_reason, message = await _read_records_via_repository(
        session_factory, limit=limit
    )

    return _build_result_from_records(
        records=records,
        fetch_status=fetch_status,
        failure_reason=failure_reason,
        message=message,
        filt=filt,
    )


def _build_result_from_records(
    *,
    records: list[OperationalEventRecord],
    fetch_status: DashboardActivityFeedStatus,
    failure_reason: Optional[DashboardActivityFeedFailureReason],
    message: Optional[str],
    filt: DashboardActivityFeedFilter,
) -> DashboardActivityFeedResult:
    """Compose a typed result from raw records and a fetch outcome."""
    if fetch_status in (
        DashboardActivityFeedStatus.DATABASE_UNAVAILABLE,
        DashboardActivityFeedStatus.MISSING_TABLE,
    ):
        return DashboardActivityFeedResult(
            status=fetch_status,
            items=[],
            current_state=None,
            failure_reason=failure_reason,
            message=message,
            has_more=False,
        )

    sorted_records = _sort_records_recent_first(records)

    items: list[DashboardActivityFeedItem] = []
    for record in sorted_records:
        if not _filter_matches(record, filt):
            continue
        result = render_event(record)
        item = _record_to_item(record, result)
        if item is not None:
            items.append(item)

    if not items:
        return DashboardActivityFeedResult(
            status=DashboardActivityFeedStatus.EMPTY_WINDOW,
            items=[],
            current_state=None,
            failure_reason=None,
            message=None,
            has_more=False,
        )

    current_state = derive_current_state(items)
    return DashboardActivityFeedResult(
        status=DashboardActivityFeedStatus.SUCCESS,
        items=items,
        current_state=current_state,
        failure_reason=None,
        message=None,
        has_more=False,
    )


# ── Rendering helpers ──────────────────────────────────────────────────────


_SEVERITY_TONE_CLASS = {
    OperationalEventSeverity.INFO: "tag-neutral",
    OperationalEventSeverity.WARNING: "tag-positive",
    OperationalEventSeverity.ERROR: "tag-negative",
    OperationalEventSeverity.CRITICAL: "tag-negative",
}


def format_activity_row_html(item: DashboardActivityFeedItem) -> str:
    """Render one feed item as a safe HTML table row.

    Every cell value is escaped before injection. Token IDs, condition
    IDs, raw payloads, and exception text never appear here because the
    item already passed the secret/high-cardinality scan at the schema
    boundary; this rendering does not introduce any new payload-derived
    text.
    """
    tone = _SEVERITY_TONE_CLASS.get(item.severity, "tag-neutral")
    timestamp_text = item.timestamp_utc.strftime("%Y-%m-%d %H:%M:%S")
    dry_label = ""
    if item.dry_run is not None:
        dry_label = f' <span class="ribbon-chip">dry_run={"true" if item.dry_run else "false"}</span>'
    narrative_label = ""
    if item.narrative_status != NarrativeRenderStatus.SUCCESS:
        narrative_label = f' <span class="ribbon-chip">{escape(item.narrative_status.value.lower())}</span>'
    return (
        f"<tr>"
        f'<td style="padding:10px 12px;color:#888;font-size:11px">'
        f"{escape(timestamp_text)}</td>"
        f'<td style="padding:10px 12px"><span class="delta-tag {tone}">'
        f"{escape(item.severity.value)}</span></td>"
        f'<td style="padding:10px 12px;color:#aaa;font-size:11px">'
        f"{escape(item.source.value)}</td>"
        f'<td style="padding:10px 12px;color:#aaa;font-size:11px">'
        f"{escape(item.event_type.value)}/{escape(item.reason_code.value)}</td>"
        f'<td style="padding:10px 12px;color:#ccc">'
        f"{escape(item.summary)}{dry_label}{narrative_label}</td>"
        f"</tr>"
    )
