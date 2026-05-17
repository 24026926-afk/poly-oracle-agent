"""
src/observability/incident_replay.py

WI-58 — Incident Replay service.

Read-only, repository-backed helper that powers the ``scripts/ops/replay.py``
CLI. It composes the WI-56 operational event ledger and the WI-57
deterministic narrative layer into a typed, secret-safe replay report.

Constraints:

* Reads exclusively through ``OperationalEventRepository.read_window``.
  Raw SQLAlchemy sessions never escape this module.
* Never appends, updates, deletes, or backfills operational events.
* Never imports or invokes LLM clients, execution routing, signing,
  broadcasting, or live wallet mutation paths.
* Never performs trading, sizing, EV, Kelly, PnL, exposure, or
  provider-cost calculations.
* Output lines are validated against the WI-56 secret/high-cardinality
  scan at the ``IncidentReplayLine`` schema boundary; the typed summary
  builder rejects any line that would expose forbidden content.
"""

from __future__ import annotations

from datetime import timezone
from typing import Optional

import structlog
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.repositories.operational_event_repository import (
    OperationalEventRepository,
)
from src.observability.operational_narratives import render_event
from src.schemas.ops import (
    IncidentReplayFailureReason,
    IncidentReplayFilter,
    IncidentReplayLine,
    IncidentReplayReport,
    IncidentReplayRequest,
    IncidentReplayStatus,
    IncidentReplaySummary,
    NarrativeRenderResult,
    NarrativeTemplateKey,
    OperationalEventQuery,
    OperationalEventReadWindow,
    OperationalEventReasonCode,
    OperationalEventRecord,
    OperationalEventSeverity,
    OperationalEventType,
    _scan_event_payload,
)

logger = structlog.get_logger(__name__)


# ── Stable typed aggregation tables ────────────────────────────────────────


_DECISION_ACTION_BY_REASON: dict[OperationalEventReasonCode, str] = {
    OperationalEventReasonCode.DECISION_BUY: "BUY",
    OperationalEventReasonCode.DECISION_HOLD: "HOLD",
    OperationalEventReasonCode.DECISION_SKIP_LOW_CONF: "SKIP",
    OperationalEventReasonCode.DECISION_SKIP_LOW_EV: "SKIP",
    OperationalEventReasonCode.DECISION_SKIP_HIGH_SPREAD: "SKIP",
    OperationalEventReasonCode.DECISION_SKIP_EXPOSURE: "SKIP",
    OperationalEventReasonCode.DECISION_SKIP_TTR: "SKIP",
}

_SKIP_REASON_CODES: set[OperationalEventReasonCode] = {
    OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
    OperationalEventReasonCode.DECISION_SKIP_LOW_EV,
    OperationalEventReasonCode.DECISION_SKIP_HIGH_SPREAD,
    OperationalEventReasonCode.DECISION_SKIP_EXPOSURE,
    OperationalEventReasonCode.DECISION_SKIP_TTR,
}

_MARKET_EVENT_TYPES: set[OperationalEventType] = {
    OperationalEventType.MARKET_DISCOVERED,
    OperationalEventType.MARKET_REJECTED,
    OperationalEventType.MARKET_QUARANTINE,
}

_BUDGET_BLOCK_EVENT_TYPES: set[OperationalEventType] = {
    OperationalEventType.BUDGET_BLOCK,
    OperationalEventType.LLM_CALL_BLOCKED,
}

_FALLBACK_GENERIC_SUMMARY: str = (
    "An operational event was recorded but did not match a known narrative "
    "template; review the ledger for typed context."
)


# ── Conversion helpers ─────────────────────────────────────────────────────


def _build_query(request: IncidentReplayRequest) -> OperationalEventQuery:
    """Translate a typed replay request into a repository query."""
    filt: IncidentReplayFilter = request.filter
    return OperationalEventQuery(
        event_types=filt.event_types,
        severities=filt.severities,
        sources=filt.sources,
        reason_codes=filt.reason_codes,
        start_time_utc=request.from_utc,
        end_time_utc=request.to_utc,
        limit=request.limit,
    )


def _sort_records_chronological(
    records: list[OperationalEventRecord],
) -> list[OperationalEventRecord]:
    """Sort events chronologically; break ties on stable persisted id."""
    return sorted(records, key=lambda r: (r.created_at_utc, r.id))


def _summary_from_line(
    line: IncidentReplayLine,
    summary_state: dict,
) -> None:
    """Accumulate one rendered replay line into the running summary state.

    Counts are derived only from typed fields on the line (event type,
    severity, reason code). No payload-derived text is inspected here.
    """
    summary_state["total_events"] += 1

    if line.severity == OperationalEventSeverity.WARNING:
        summary_state["warnings"] += 1
    if line.severity in (
        OperationalEventSeverity.ERROR,
        OperationalEventSeverity.CRITICAL,
    ):
        summary_state["errors"] += 1

    if line.event_type in _MARKET_EVENT_TYPES:
        summary_state["markets_seen"] += 1

    if line.event_type == OperationalEventType.LLM_CALL_STARTED:
        summary_state["llm_calls"] += 1

    if line.event_type in _BUDGET_BLOCK_EVENT_TYPES:
        summary_state["budget_blocks"] += 1

    if line.event_type == OperationalEventType.COOLDOWN_BLOCK:
        summary_state["cooldown_blocks"] += 1

    if line.event_type == OperationalEventType.PROVIDER_FAILURE:
        summary_state["provider_failures"] += 1

    if line.event_type == OperationalEventType.READY_STATE_CHANGED:
        summary_state["readiness_changes"] += 1

    action = _DECISION_ACTION_BY_REASON.get(line.reason_code)
    if action is not None and line.event_type in (
        OperationalEventType.DECISION_ACCEPTED,
        OperationalEventType.DECISION_SKIPPED,
    ):
        summary_state["decisions_by_action"][action] = (
            summary_state["decisions_by_action"].get(action, 0) + 1
        )

    if (
        line.event_type == OperationalEventType.DECISION_SKIPPED
        and line.reason_code in _SKIP_REASON_CODES
    ):
        key = line.reason_code.value
        summary_state["skips_by_reason"][key] = (
            summary_state["skips_by_reason"].get(key, 0) + 1
        )


def _line_from_render(
    record: OperationalEventRecord,
    result: NarrativeRenderResult,
) -> Optional[IncidentReplayLine]:
    """Build a typed replay line from a narrative render result.

    Returns ``None`` if the resulting line would expose forbidden content
    even after the WI-57 redaction path; the caller drops such records
    rather than printing unsafe text.
    """
    narrative = result.narrative
    timestamp = record.created_at_utc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    if narrative is None:
        # FAILED render — no narrative payload available. Emit a typed
        # generic line so the operator still sees that the event existed.
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

    # Final defense-in-depth scan. The narrative layer already scans,
    # but the replay surface is the last hop before operator output.
    if _scan_event_payload(summary):
        logger.warning(
            "incident_replay.line.forbidden_content_blocked",
            event_type=record.event_type.value,
            reason_code=record.reason_code.value,
        )
        return None

    try:
        return IncidentReplayLine(
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
    except Exception:  # noqa: BLE001 — defensive, must never crash replay
        logger.warning(
            "incident_replay.line.construction_failed",
            event_type=record.event_type.value,
            reason_code=record.reason_code.value,
        )
        return None


# ── Public API ─────────────────────────────────────────────────────────────


async def run_replay(
    request: IncidentReplayRequest,
    session_factory: async_sessionmaker[AsyncSession],
) -> IncidentReplayReport:
    """Read the configured event window and return a typed replay report.

    All database access is performed through ``OperationalEventRepository``;
    no raw SQL or session leaks outside this function. Database / repository
    failures are caught and surfaced as typed non-success statuses with a
    bounded, low-cardinality message.
    """
    query = _build_query(request)

    try:
        async with session_factory() as session:
            repo = OperationalEventRepository(session)
            window: OperationalEventReadWindow = await repo.read_window(query)
    except OperationalError as exc:
        # SQLite/SA reports both "no such table" and "unable to open" via
        # OperationalError. Split the two so operators know whether to
        # provision a fresh ledger or check disk/permissions.
        text = str(exc).lower()
        if "no such table" in text or "operational_events" in text:
            return _failure_report(
                request,
                IncidentReplayStatus.DATABASE_UNAVAILABLE,
                IncidentReplayFailureReason.MISSING_EVENT_TABLE,
                "operational_events table is unavailable in this deployment",
            )
        return _failure_report(
            request,
            IncidentReplayStatus.DATABASE_UNAVAILABLE,
            IncidentReplayFailureReason.DATABASE_UNREACHABLE,
            "operational event database is unreachable",
        )
    except SQLAlchemyError:
        return _failure_report(
            request,
            IncidentReplayStatus.REPOSITORY_FAILURE,
            IncidentReplayFailureReason.REPOSITORY_ERROR,
            "operational event repository read failed",
        )
    except Exception:  # noqa: BLE001 — defensive boundary; never re-raise
        logger.warning("incident_replay.unexpected_exception", exc_info=False)
        return _failure_report(
            request,
            IncidentReplayStatus.REPOSITORY_FAILURE,
            IncidentReplayFailureReason.REPOSITORY_ERROR,
            "operational event repository read failed",
        )

    sorted_records = _sort_records_chronological(window.events)

    lines: list[IncidentReplayLine] = []
    for record in sorted_records:
        result = render_event(record)
        line = _line_from_render(record, result)
        if line is not None:
            lines.append(line)

    summary_state: dict = {
        "total_events": 0,
        "warnings": 0,
        "errors": 0,
        "markets_seen": 0,
        "llm_calls": 0,
        "budget_blocks": 0,
        "cooldown_blocks": 0,
        "provider_failures": 0,
        "readiness_changes": 0,
        "decisions_by_action": {},
        "skips_by_reason": {},
    }
    for line in lines:
        _summary_from_line(line, summary_state)

    summary = IncidentReplaySummary(**summary_state)

    if not lines:
        return IncidentReplayReport(
            status=IncidentReplayStatus.EMPTY_WINDOW,
            request=request,
            lines=[],
            summary=summary,
            failure_reason=None,
            message=None,
            has_more=window.has_more,
        )

    status = (
        IncidentReplayStatus.TRUNCATED
        if window.has_more
        else IncidentReplayStatus.SUCCESS
    )
    failure_reason = (
        IncidentReplayFailureReason.RESULT_TRUNCATED if window.has_more else None
    )
    message = (
        "result truncated at configured limit; narrow the window or filters"
        if window.has_more
        else None
    )
    return IncidentReplayReport(
        status=status,
        request=request,
        lines=lines,
        summary=summary,
        failure_reason=failure_reason,
        message=message,
        has_more=window.has_more,
    )


def _failure_report(
    request: IncidentReplayRequest,
    status: IncidentReplayStatus,
    failure_reason: IncidentReplayFailureReason,
    message: str,
) -> IncidentReplayReport:
    """Build a typed failure report with a bounded, secret-free message."""
    return IncidentReplayReport(
        status=status,
        request=request,
        lines=[],
        summary=IncidentReplaySummary(),
        failure_reason=failure_reason,
        message=message,
        has_more=False,
    )


# ── Printable rendering ────────────────────────────────────────────────────


def format_report_lines(report: IncidentReplayReport) -> list[str]:
    """Render a typed replay report into operator-facing text lines.

    Output is intentionally minimal: a header, one line per event in
    chronological order, and the typed summary footer. Token IDs,
    condition IDs, raw payloads, and exception text never appear.
    """
    out: list[str] = []
    out.append("=== Incident Replay ===")
    out.append(
        f"window: {report.request.from_utc.isoformat()} → "
        f"{report.request.to_utc.isoformat()}"
    )
    filt = report.request.filter
    if not filt.is_empty():
        out.append("filters:")
        if filt.severities:
            out.append("  severity: " + ",".join(s.value for s in filt.severities))
        if filt.sources:
            out.append("  source: " + ",".join(s.value for s in filt.sources))
        if filt.event_types:
            out.append("  event_type: " + ",".join(t.value for t in filt.event_types))
        if filt.reason_codes:
            out.append("  reason_code: " + ",".join(r.value for r in filt.reason_codes))
    out.append(f"status: {report.status.value}")
    if report.failure_reason is not None:
        out.append(f"failure_reason: {report.failure_reason.value}")
    if report.message:
        out.append(f"note: {report.message}")
    out.append("")
    out.append("--- events ---")
    if not report.lines:
        out.append("(no events in window)")
    else:
        for line in report.lines:
            dry = (
                ""
                if line.dry_run is None
                else f" [dry_run={str(line.dry_run).lower()}]"
            )
            out.append(
                f"{line.timestamp_utc.isoformat()} "
                f"{line.severity.value} {line.source.value} "
                f"{line.event_type.value}/{line.reason_code.value}{dry}: "
                f"{line.summary}"
            )
    out.append("")
    s = report.summary
    out.append("--- summary ---")
    out.append(f"total_events: {s.total_events}")
    out.append(f"warnings: {s.warnings}")
    out.append(f"errors: {s.errors}")
    out.append(f"markets_seen: {s.markets_seen}")
    out.append(f"llm_calls: {s.llm_calls}")
    out.append(f"budget_blocks: {s.budget_blocks}")
    out.append(f"cooldown_blocks: {s.cooldown_blocks}")
    out.append(f"provider_failures: {s.provider_failures}")
    out.append(f"readiness_changes: {s.readiness_changes}")
    if s.decisions_by_action:
        parts = ",".join(f"{k}={v}" for k, v in sorted(s.decisions_by_action.items()))
        out.append(f"decisions_by_action: {parts}")
    else:
        out.append("decisions_by_action: (none)")
    if s.skips_by_reason:
        parts = ",".join(f"{k}={v}" for k, v in sorted(s.skips_by_reason.items()))
        out.append(f"skips_by_reason: {parts}")
    else:
        out.append("skips_by_reason: (none)")
    if report.has_more:
        out.append("has_more: true")
    return out
