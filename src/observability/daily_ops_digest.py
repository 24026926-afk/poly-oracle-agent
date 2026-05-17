"""
src/observability/daily_ops_digest.py

WI-60 — Daily Operations Digest service.

Read-only, repository-backed helper that composes the WI-56 operational
event ledger, the WI-57 deterministic narrative layer, the WI-58 replay
summary counting patterns, and the WI-59 dashboard current-state
semantics into a typed, secret-safe daily digest report. The digest is
written to ``03_Daily/YYYY-MM-DD-bot.md`` and never overwrites manual
operator notes at ``03_Daily/YYYY-MM-DD.md``.

Constraints:

* Reads operational events exclusively through
  ``OperationalEventRepository.read_window``.
* Reads position-derived paper PnL exclusively through
  ``PositionRepository``. Raw SQLAlchemy sessions never escape this
  module.
* Never appends, updates, deletes, or backfills operational events.
* Never imports or invokes LLM clients, execution routing, signing,
  broadcasting, or live wallet mutation paths.
* Never performs trading, sizing, EV, Kelly, or exposure calculations.
* Output sections are validated against the WI-56 secret/high-cardinality
  scan at the schema boundary AND on the final rendered text before
  writing to disk or sending to Telegram.
* Manual coding daily notes at ``03_Daily/YYYY-MM-DD.md`` are never
  created, overwritten, truncated, appended, renamed, or deleted.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

import structlog
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.repositories.operational_event_repository import (
    OperationalEventRepository,
)
from src.db.repositories.position_repository import PositionRepository
from src.observability.operational_narratives import render_event
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
    NarrativeRenderResult,
    NarrativeRenderStatus,
    OperationalEventQuery,
    OperationalEventReadWindow,
    OperationalEventReasonCode,
    OperationalEventRecord,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
    _scan_event_payload,
)

logger = structlog.get_logger(__name__)


# ── Constants ──────────────────────────────────────────────────────────────

DEFAULT_DAILY_NOTES_DIR_NAME: str = "03_Daily"
_BOT_FILE_SUFFIX: str = "-bot.md"
_BOT_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-bot\.md$")
_MANUAL_FILENAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_DEFAULT_EVENT_LIMIT: int = 1000
# Hard cap on pagination loops so a misconfigured window cannot exhaust
# memory. 200 pages × 1000 events = 200_000 events, far above any
# realistic single-UTC-day digest volume for this bot.
_MAX_EVENT_READ_PAGES: int = 200
_TOP_EVENT_LIMIT: int = 10
_UNRESOLVED_LIMIT: int = 10
_TELEGRAM_TEXT_LIMIT: int = 1024

_FALLBACK_GENERIC_SUMMARY: str = (
    "An operational event was recorded but did not match a known narrative "
    "template; review the ledger for typed context."
)

_DECISION_REASON_TO_FIELD: dict[OperationalEventReasonCode, str] = {
    OperationalEventReasonCode.DECISION_BUY: "accepted_buy",
    OperationalEventReasonCode.DECISION_HOLD: "accepted_hold",
    OperationalEventReasonCode.DECISION_SKIP_LOW_CONF: "skipped_low_conf",
    OperationalEventReasonCode.DECISION_SKIP_LOW_EV: "skipped_low_ev",
    OperationalEventReasonCode.DECISION_SKIP_HIGH_SPREAD: "skipped_high_spread",
    OperationalEventReasonCode.DECISION_SKIP_EXPOSURE: "skipped_exposure",
    OperationalEventReasonCode.DECISION_SKIP_TTR: "skipped_ttr",
}

_MARKET_REJECTION_TYPES: set[OperationalEventType] = {
    OperationalEventType.MARKET_REJECTED,
    OperationalEventType.MARKET_QUARANTINE,
}

_BUDGET_BLOCK_TYPES: set[OperationalEventType] = {
    OperationalEventType.BUDGET_BLOCK,
    OperationalEventType.LLM_CALL_BLOCKED,
}

_SEVERITY_RANK: dict[OperationalEventSeverity, int] = {
    OperationalEventSeverity.CRITICAL: 4,
    OperationalEventSeverity.ERROR: 3,
    OperationalEventSeverity.WARNING: 2,
    OperationalEventSeverity.INFO: 1,
}

# Severity that should appear in unresolved warning/error sections.
_WARNING_SEVERITIES = {OperationalEventSeverity.WARNING}
_ERROR_SEVERITIES = {
    OperationalEventSeverity.ERROR,
    OperationalEventSeverity.CRITICAL,
}


# ── Path validation ────────────────────────────────────────────────────────


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _derive_window(request: DailyOpsDigestRequest) -> DailyOpsDigestWindow:
    """Derive the UTC daily window from the request's digest_date_utc.

    If the caller supplied an explicit window we honor it. Otherwise
    we compute ``[00:00, 23:59:59.999999]`` UTC for the requested date.
    """
    if request.window is not None:
        return request.window
    date_only = request.digest_date_utc.date()
    from_utc = datetime.combine(date_only, time.min, tzinfo=timezone.utc)
    # Inclusive upper bound (matches repository BETWEEN semantics).
    to_utc = datetime.combine(date_only, time.max, tzinfo=timezone.utc)
    return DailyOpsDigestWindow(from_utc=from_utc, to_utc=to_utc)


def _resolve_daily_notes_root(
    request: DailyOpsDigestRequest,
    explicit_root: Optional[Path],
) -> Path:
    """Resolve the absolute path of the daily notes directory.

    Resolution order:

    1. Caller-supplied ``explicit_root`` (used by tests).
    2. ``request.daily_notes_dir`` if absolute.
    3. Vault-relative resolution: project root → parent → ``03_Daily``.
    """
    if explicit_root is not None:
        return explicit_root.resolve()
    candidate = Path(request.daily_notes_dir)
    if candidate.is_absolute():
        return candidate.resolve()
    # Project root is the parent of src/.
    project_root = Path(__file__).resolve().parents[2]
    vault_root = project_root.parent
    return (vault_root / candidate).resolve()


def _validate_output_path(
    request: DailyOpsDigestRequest,
    daily_notes_root: Path,
) -> tuple[Optional[Path], Optional[DailyOpsDigestFailureReason], Optional[str]]:
    """Validate the resolved output path.

    Returns ``(path, None, None)`` on success, or
    ``(None, failure_reason, message)`` on failure. The path must:

    * sit directly inside ``daily_notes_root``;
    * match ``YYYY-MM-DD-bot.md``;
    * not coincide with a manual note path.
    """
    date_str = request.digest_date_utc.date().isoformat()
    default_name = f"{date_str}{_BOT_FILE_SUFFIX}"

    if request.output_path is None:
        candidate = (daily_notes_root / default_name).resolve()
    else:
        raw = request.output_path.strip()
        if not raw:
            return (
                None,
                DailyOpsDigestFailureReason.INVALID_FILENAME,
                "output_path must not be empty",
            )
        candidate = Path(raw)
        if candidate.is_absolute():
            candidate = candidate.resolve()
        else:
            # Relative paths may either be:
            #   * a bare filename (resolved under daily_notes_root), or
            #   * prefixed with the daily-notes basename (e.g.
            #     "03_Daily/2026-05-15-bot.md") which the runbook
            #     documents and operators commonly type. Strip that
            #     leading prefix so the candidate resolves to a direct
            #     child of daily_notes_root rather than a nested copy.
            parts = candidate.parts
            if (
                len(parts) >= 2
                and parts[0] == daily_notes_root.name
            ):
                candidate = Path(*parts[1:])
            candidate = (daily_notes_root / candidate).resolve()

    name = candidate.name

    # Block manual notes BEFORE checking the bot-file regex so the typed
    # reason matches operator expectations.
    if _MANUAL_FILENAME_RE.match(name):
        return (
            None,
            DailyOpsDigestFailureReason.MANUAL_NOTE_WOULD_OVERWRITE,
            "refusing to overwrite manual coding daily note",
        )

    if not _BOT_FILENAME_RE.match(name):
        return (
            None,
            DailyOpsDigestFailureReason.INVALID_FILENAME,
            "digest filename must match YYYY-MM-DD-bot.md",
        )

    expected_name = default_name
    if name != expected_name:
        return (
            None,
            DailyOpsDigestFailureReason.INVALID_FILENAME,
            "digest filename must match the requested digest date",
        )

    # Path traversal guard — the resolved candidate must be a direct child
    # of the configured daily notes directory.
    try:
        candidate.relative_to(daily_notes_root)
    except ValueError:
        return (
            None,
            DailyOpsDigestFailureReason.PATH_OUTSIDE_DAILY,
            "digest output path must reside under the daily notes directory",
        )

    if candidate.parent != daily_notes_root:
        return (
            None,
            DailyOpsDigestFailureReason.PATH_OUTSIDE_DAILY,
            "digest output path must be a direct child of the daily notes directory",
        )

    return candidate, None, None


# ── Reading helpers ────────────────────────────────────────────────────────


async def _read_events(
    session_factory: async_sessionmaker[AsyncSession],
    window: DailyOpsDigestWindow,
) -> tuple[
    Optional[list[OperationalEventRecord]],
    DailyOpsDigestStatus,
    Optional[DailyOpsDigestFailureReason],
    Optional[str],
]:
    """Read every operational event in the requested window.

    Pages through ``OperationalEventRepository.read_window`` using the
    typed offset cursor so aggregate counters cover the complete window
    even when daily event volume exceeds the per-page schema cap. The
    bounded top-event and unresolved lists are applied later by the
    aggregation layer — the read itself is unbounded within the configured
    safety cap.

    Returns ``(events, status, failure_reason, message)``. ``events`` is
    None when a typed failure occurred; otherwise it is a (possibly
    empty) list of records.
    """
    all_events: list[OperationalEventRecord] = []
    cap_reached = False
    try:
        async with session_factory() as session:
            repo = OperationalEventRepository(session)
            offset = 0
            for _ in range(_MAX_EVENT_READ_PAGES):
                query = OperationalEventQuery(
                    start_time_utc=window.from_utc,
                    end_time_utc=window.to_utc,
                    limit=_DEFAULT_EVENT_LIMIT,
                    offset=offset,
                )
                window_result: OperationalEventReadWindow = await repo.read_window(query)
                all_events.extend(window_result.events)
                if not window_result.has_more or not window_result.events:
                    break
                offset += len(window_result.events)
            else:
                # Safety cap reached AND the last page still reported
                # has_more=True. Failing closed is mandatory: partial
                # aggregates would silently undercount LLM calls, budget
                # blocks, decisions, and markets — exactly the failure
                # mode the typed digest is supposed to prevent.
                cap_reached = True
    except OperationalError as exc:
        text = str(exc).lower()
        if "no such table" in text or "operational_events" in text:
            return (
                None,
                DailyOpsDigestStatus.MISSING_TABLE,
                DailyOpsDigestFailureReason.MISSING_TABLE,
                "operational_events table is unavailable in this deployment",
            )
        return (
            None,
            DailyOpsDigestStatus.DATABASE_UNAVAILABLE,
            DailyOpsDigestFailureReason.DATABASE_UNREACHABLE,
            "operational event database is unreachable",
        )
    except SQLAlchemyError:
        return (
            None,
            DailyOpsDigestStatus.REPOSITORY_FAILURE,
            DailyOpsDigestFailureReason.REPOSITORY_ERROR,
            "operational event repository read failed",
        )
    except Exception:  # noqa: BLE001 — defensive boundary
        logger.warning("daily_ops_digest.unexpected_read_exception", exc_info=False)
        return (
            None,
            DailyOpsDigestStatus.REPOSITORY_FAILURE,
            DailyOpsDigestFailureReason.REPOSITORY_ERROR,
            "operational event repository read failed",
        )

    if cap_reached:
        logger.warning(
            "daily_ops_digest.event_read_cap_reached",
            pages=_MAX_EVENT_READ_PAGES,
            page_size=_DEFAULT_EVENT_LIMIT,
        )
        return (
            None,
            DailyOpsDigestStatus.READ_CAP_REACHED,
            DailyOpsDigestFailureReason.READ_CAP_REACHED,
            "operational event window exceeded the bounded read budget",
        )

    return all_events, DailyOpsDigestStatus.SUCCESS, None, None


async def _read_paper_pnl(
    session_factory: async_sessionmaker[AsyncSession],
    window: DailyOpsDigestWindow,
) -> Optional[DailyOpsDigestPnLSummary]:
    """Read repository-backed paper PnL data for the window.

    Returns ``None`` if positions cannot be read; the digest renders
    "unavailable" in that case rather than fabricating zero values.
    Repository errors are absorbed because PnL is informational and
    must not block file digest generation.
    """
    try:
        async with session_factory() as session:
            repo = PositionRepository(session)
            settled = await repo.get_settled_positions()
            open_positions = await repo.get_open_positions()
    except SQLAlchemyError:
        return None
    except Exception:  # noqa: BLE001 — defensive
        logger.warning("daily_ops_digest.position_read_failed", exc_info=False)
        return None

    closed_in_window = [
        p
        for p in settled
        if p.closed_at_utc is not None
        and window.from_utc <= _ensure_utc(p.closed_at_utc) <= window.to_utc
    ]

    realized_total: Optional[Decimal] = None
    gas_fees_total: Optional[Decimal] = None

    if closed_in_window:
        realized_total = Decimal("0")
        gas_fees_total = Decimal("0")
        for pos in closed_in_window:
            if pos.realized_pnl is not None:
                realized_total += Decimal(str(pos.realized_pnl))
            gas = pos.gas_cost_usdc if pos.gas_cost_usdc is not None else Decimal("0")
            fees = pos.fees_usdc if pos.fees_usdc is not None else Decimal("0")
            gas_fees_total += Decimal(str(gas)) + Decimal(str(fees))

    if not settled and not open_positions:
        return None

    return DailyOpsDigestPnLSummary(
        realized_pnl=realized_total,
        unrealized_pnl=None,
        gas_and_fees=gas_fees_total,
        closed_position_count=len(closed_in_window),
        open_position_count=len(open_positions),
    )


# ── Event → Highlight conversion ───────────────────────────────────────────


def _record_to_highlight(
    record: OperationalEventRecord,
    result: NarrativeRenderResult,
) -> Optional[DailyOpsDigestEventHighlight]:
    """Convert a record + narrative render result into a typed highlight.

    Returns ``None`` when the resulting summary would expose forbidden
    content; the caller drops such records.
    """
    narrative = result.narrative
    if narrative is None:
        summary = _FALLBACK_GENERIC_SUMMARY
    else:
        if narrative.kind == "operational":
            assert narrative.operational is not None
            summary = narrative.operational.summary
        else:
            assert narrative.decision is not None
            summary = narrative.decision.summary

    if _scan_event_payload(summary):
        return None

    try:
        return DailyOpsDigestEventHighlight(
            event_id=record.id,
            event_type=record.event_type,
            severity=record.severity,
            reason_code=record.reason_code,
            summary=summary,
            timestamp_utc=_ensure_utc(record.created_at_utc),
        )
    except Exception:  # noqa: BLE001 — defensive
        return None


# ── Aggregation ────────────────────────────────────────────────────────────


def _sort_chronological(
    records: list[OperationalEventRecord],
) -> list[OperationalEventRecord]:
    """Sort ascending by created_at_utc, tie-break on stable id ascending."""
    return sorted(records, key=lambda r: (r.created_at_utc, r.id))


def _derive_run_summary(
    records: list[OperationalEventRecord],
    window: DailyOpsDigestWindow,
) -> DailyOpsDigestRunSummary:
    """Derive run lifecycle, provider, readiness, dry-run and market counts.

    All values are typed-only; unknowns remain ``None``.
    """
    start_utc: Optional[datetime] = None
    stop_utc: Optional[datetime] = None
    latest_readiness: Optional[str] = None
    latest_readiness_ts: Optional[datetime] = None
    latest_provider: Optional[str] = None
    latest_provider_ts: Optional[datetime] = None
    latest_dry_run: Optional[bool] = None
    latest_dry_run_ts: Optional[datetime] = None
    markets_seen = 0
    markets_rejected = 0

    saw_start = False
    saw_shutdown = False
    has_any_lifecycle_evidence = False

    # Edge Case 9: when multiple START/SHUTDOWN cycles exist in one
    # window, uptime must be summed from typed pairs. Tracking the open
    # START cursor through the chronological walk lets a partial trailing
    # cycle (Edge Case 10) be flagged without inflating uptime.
    total_uptime: timedelta = timedelta(0)
    open_start: Optional[datetime] = None

    for record in records:
        ts = _ensure_utc(record.created_at_utc)

        # Lifecycle
        if record.event_type == OperationalEventType.START:
            has_any_lifecycle_evidence = True
            if start_utc is None or ts < start_utc:
                start_utc = ts
            saw_start = True
            # Open a new span only if no prior START is currently open.
            # Anomalous consecutive STARTs keep the earliest open cursor.
            if open_start is None:
                open_start = ts
        elif record.event_type == OperationalEventType.SHUTDOWN:
            has_any_lifecycle_evidence = True
            if stop_utc is None or ts > stop_utc:
                stop_utc = ts
            saw_shutdown = True
            # Pair this SHUTDOWN with the open START if one exists and
            # the SHUTDOWN is not earlier than the open START (defensive
            # against clock skew). Dangling SHUTDOWNs without a prior
            # START contribute no uptime.
            if open_start is not None and ts >= open_start:
                total_uptime += ts - open_start
                open_start = None
        elif record.event_type == OperationalEventType.CONFIG_LOADED:
            has_any_lifecycle_evidence = True

        # Readiness
        if record.event_type == OperationalEventType.READY_STATE_CHANGED:
            if latest_readiness_ts is None or ts >= latest_readiness_ts:
                latest_readiness_ts = ts
                if record.reason_code == OperationalEventReasonCode.READY:
                    latest_readiness = "READY"
                elif record.reason_code == OperationalEventReasonCode.DEGRADED:
                    latest_readiness = "DEGRADED"
                elif record.reason_code == OperationalEventReasonCode.NOT_READY:
                    latest_readiness = "NOT_READY"

        # Provider — derive from typed payload field only.
        provider = _extract_typed_string_field(record, "provider_name")
        if provider is not None:
            if latest_provider_ts is None or ts >= latest_provider_ts:
                latest_provider_ts = ts
                latest_provider = provider

        # Dry-run state — accept typed boolean payload field.
        dry_run = _extract_typed_bool_field(record, "dry_run")
        if dry_run is not None:
            if latest_dry_run_ts is None or ts >= latest_dry_run_ts:
                latest_dry_run_ts = ts
                latest_dry_run = dry_run

        # Markets
        if record.event_type == OperationalEventType.MARKET_DISCOVERED:
            markets_seen += 1
        if record.event_type in _MARKET_REJECTION_TYPES:
            markets_rejected += 1

    # Status derivation.
    # * partial   → the latest START in the window has no paired SHUTDOWN
    #               (Edge Case 10), even if earlier cycles completed.
    # * completed → at least one START and one SHUTDOWN observed, and the
    #               latest START is paired (open_start is None).
    # * unknown   → events exist (lifecycle or otherwise) but no START to
    #               anchor a run. We never invent a "no_run" verdict when
    #               the ledger contains operator-visible activity.
    # * no_run    → zero records in the digest window.
    if open_start is not None:
        run_status = "partial"
    elif saw_start and saw_shutdown:
        run_status = "completed"
    elif saw_start:
        # Defensive: saw_start with no SHUTDOWN means open_start was set
        # above, so this branch is unreachable in practice. Kept for
        # readability of the status taxonomy.
        run_status = "partial"
    elif records:
        run_status = "unknown"
    else:
        run_status = "no_run"

    # Uptime is the sum of typed lifecycle pair spans (Edge Case 9).
    # Partial runs leave uptime unknown rather than inventing a stop
    # time or counting incomplete cycles (Edge Case 10).
    uptime_seconds: Optional[int] = None
    if saw_start and saw_shutdown and open_start is None:
        uptime_seconds = int(total_uptime.total_seconds())

    return DailyOpsDigestRunSummary(
        start_utc=start_utc,
        stop_utc=stop_utc,
        uptime_seconds=uptime_seconds,
        run_status=run_status,
        active_provider=latest_provider,
        dry_run=latest_dry_run,
        latest_readiness=latest_readiness,
        markets_seen=markets_seen,
        markets_rejected=markets_rejected,
    )


def _extract_typed_string_field(
    record: OperationalEventRecord, field: str
) -> Optional[str]:
    """Pull a typed, secret-safe string from the persisted payload JSON.

    Returns ``None`` when the payload is malformed, missing the field,
    or contains forbidden content. The digest never reads payload text
    that has not passed the secret scan.
    """
    import json

    try:
        payload = json.loads(record.payload_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    if _scan_event_payload(value):
        return None
    if len(value) > 64:
        return None
    return value


def _extract_typed_bool_field(
    record: OperationalEventRecord, field: str
) -> Optional[bool]:
    """Pull a typed bool from the persisted payload JSON, or ``None``."""
    import json

    try:
        payload = json.loads(record.payload_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get(field)
    if isinstance(value, bool):
        return value
    return None


def _derive_decision_summary(
    records: list[OperationalEventRecord],
) -> DailyOpsDigestDecisionSummary:
    """Count decisions and skips from typed reason codes only."""
    counts: dict[str, int] = {
        "accepted_buy": 0,
        "accepted_hold": 0,
        "skipped_low_conf": 0,
        "skipped_low_ev": 0,
        "skipped_high_spread": 0,
        "skipped_exposure": 0,
        "skipped_ttr": 0,
    }
    for record in records:
        if record.event_type not in {
            OperationalEventType.DECISION_ACCEPTED,
            OperationalEventType.DECISION_SKIPPED,
        }:
            continue
        field = _DECISION_REASON_TO_FIELD.get(record.reason_code)
        if field is None:
            continue
        counts[field] += 1
    return DailyOpsDigestDecisionSummary(**counts)


def _derive_llm_summary(
    records: list[OperationalEventRecord],
) -> DailyOpsDigestLLMSummary:
    """Aggregate LLM activity counts and Decimal-only spend."""
    llm_calls = 0
    budget_blocks = 0
    cooldown_blocks = 0
    provider_failures = 0
    spend_total = Decimal("0")
    saw_any_spend = False

    for record in records:
        if record.event_type == OperationalEventType.LLM_CALL_STARTED:
            llm_calls += 1
        if record.event_type in _BUDGET_BLOCK_TYPES:
            budget_blocks += 1
        if record.event_type == OperationalEventType.COOLDOWN_BLOCK:
            cooldown_blocks += 1
        if record.event_type == OperationalEventType.PROVIDER_FAILURE:
            provider_failures += 1

        # Estimated spend: parse a Decimal-compatible cost payload field
        # when present. Float values are explicitly rejected.
        cost = _extract_decimal_field(record, "estimated_cost_usd")
        if cost is not None:
            spend_total += cost
            saw_any_spend = True

    return DailyOpsDigestLLMSummary(
        llm_calls=llm_calls,
        budget_blocks=budget_blocks,
        cooldown_blocks=cooldown_blocks,
        provider_failures=provider_failures,
        estimated_spend_usd=spend_total if saw_any_spend else None,
    )


def _extract_decimal_field(
    record: OperationalEventRecord, field: str
) -> Optional[Decimal]:
    """Pull a Decimal value from payload JSON. Floats are rejected.

    Returns ``None`` when the value is missing, malformed, or float.
    """
    import json

    try:
        payload = json.loads(record.payload_json or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get(field)
    if value is None:
        return None
    if isinstance(value, float):
        return None
    try:
        return Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None


def _select_top_events(
    highlights: list[DailyOpsDigestEventHighlight],
) -> list[DailyOpsDigestEventHighlight]:
    """Deterministically pick top events by severity then time.

    Stable sort keys: severity rank descending, then timestamp ascending,
    then event_id ascending. The top window is bounded.
    """
    sorted_items = sorted(
        highlights,
        key=lambda h: (
            -_SEVERITY_RANK.get(h.severity, 0),
            h.timestamp_utc,
            h.event_id,
        ),
    )
    return sorted_items[:_TOP_EVENT_LIMIT]


def _select_unresolved(
    records: list[OperationalEventRecord],
    highlights_by_id: dict[str, DailyOpsDigestEventHighlight],
    severities: set[OperationalEventSeverity],
) -> list[DailyOpsDigestEventHighlight]:
    """Pick unresolved warning/error events using typed recovery semantics.

    A warning/error event is considered resolved when a later typed
    recovery event of compatible category is observed. Without explicit
    pairing, we use a category-based latest-status check: if the latest
    event in the relevant category is an unresolved warning/error, we
    surface those events.
    """
    # Sort all records chronologically to support latest-wins semantics.
    chronological = _sort_chronological(records)

    # Detect categorical recovery. For now we rely on:
    # * READY_STATE_CHANGED → READY supersedes earlier DEGRADED/NOT_READY.
    # * CIRCUIT_BREAKER_CLOSED supersedes earlier CIRCUIT_BREAKER_OPEN.
    # * ERROR_RECOVERED supersedes prior ERROR severity events.
    # * WS_CONNECTED / WS_RECONNECT supersedes WS_PONG_STALE.
    readiness_recovered = False
    breaker_recovered = False
    error_recovered = False
    ws_recovered = False
    for record in chronological:
        if (
            record.event_type == OperationalEventType.READY_STATE_CHANGED
            and record.reason_code == OperationalEventReasonCode.READY
        ):
            readiness_recovered = True
        elif (
            record.event_type == OperationalEventType.READY_STATE_CHANGED
            and record.reason_code
            in {
                OperationalEventReasonCode.DEGRADED,
                OperationalEventReasonCode.NOT_READY,
            }
        ):
            readiness_recovered = False

        if record.event_type == OperationalEventType.CIRCUIT_BREAKER_CLOSED:
            breaker_recovered = True
        elif record.event_type == OperationalEventType.CIRCUIT_BREAKER_OPEN:
            breaker_recovered = False

        if (
            record.event_type == OperationalEventType.ERROR_RECOVERED
            and record.reason_code == OperationalEventReasonCode.ERROR_HANDLED
        ):
            error_recovered = True

        if record.event_type in {
            OperationalEventType.WS_CONNECTED,
            OperationalEventType.WS_RECONNECT,
        }:
            ws_recovered = True
        elif record.event_type == OperationalEventType.WS_PONG_STALE:
            ws_recovered = False

    unresolved: list[DailyOpsDigestEventHighlight] = []
    for record in chronological:
        if record.severity not in severities:
            continue
        highlight = highlights_by_id.get(record.id)
        if highlight is None:
            continue

        # Drop events whose category has since recovered.
        if record.event_type == OperationalEventType.READY_STATE_CHANGED:
            if readiness_recovered:
                continue
        if record.event_type == OperationalEventType.CIRCUIT_BREAKER_OPEN:
            if breaker_recovered:
                continue
        if record.event_type == OperationalEventType.WS_PONG_STALE:
            if ws_recovered:
                continue
        if (
            record.severity
            in {
                OperationalEventSeverity.ERROR,
                OperationalEventSeverity.CRITICAL,
            }
            and record.event_type == OperationalEventType.ERROR_RECOVERED
        ):
            continue
        if error_recovered and record.event_type not in {
            OperationalEventType.ERROR_RECOVERED,
            OperationalEventType.CIRCUIT_BREAKER_OPEN,
            OperationalEventType.READY_STATE_CHANGED,
        }:
            # Generic ERROR_RECOVERED only clears generic error events.
            if record.severity in {
                OperationalEventSeverity.ERROR,
                OperationalEventSeverity.CRITICAL,
            }:
                continue

        unresolved.append(highlight)

    return unresolved[:_UNRESOLVED_LIMIT]


def _derive_operator_checks(
    run_summary: Optional[DailyOpsDigestRunSummary],
    decision_summary: Optional[DailyOpsDigestDecisionSummary],
    llm_summary: Optional[DailyOpsDigestLLMSummary],
    unresolved_warnings: list[DailyOpsDigestEventHighlight],
    unresolved_errors: list[DailyOpsDigestEventHighlight],
) -> list[DailyOpsDigestOperatorCheck]:
    """Generate deterministic next-action recommendations.

    All messages are bounded, secret-free, and derived from typed state.
    """
    checks: list[DailyOpsDigestOperatorCheck] = []

    if run_summary is not None:
        if run_summary.run_status == "partial":
            checks.append(
                DailyOpsDigestOperatorCheck(
                    category="lifecycle",
                    message=(
                        "The agent started but did not record a shutdown event "
                        "within the digest window. Verify the process is still "
                        "running or capture a shutdown log."
                    ),
                    severity=OperationalEventSeverity.WARNING,
                )
            )
        if run_summary.latest_readiness == "DEGRADED":
            checks.append(
                DailyOpsDigestOperatorCheck(
                    category="readiness",
                    message=(
                        "Readiness ended the window in DEGRADED state. Inspect "
                        "the most recent readiness narrative for context."
                    ),
                    severity=OperationalEventSeverity.WARNING,
                )
            )
        elif run_summary.latest_readiness == "NOT_READY":
            checks.append(
                DailyOpsDigestOperatorCheck(
                    category="readiness",
                    message=(
                        "Readiness ended the window in NOT_READY state; trading "
                        "paths were paused. Confirm WebSocket and database health."
                    ),
                    severity=OperationalEventSeverity.ERROR,
                )
            )

    if llm_summary is not None:
        if llm_summary.budget_blocks > 0:
            checks.append(
                DailyOpsDigestOperatorCheck(
                    category="budget",
                    message=(
                        "LLM budget limits blocked one or more evaluations. "
                        "Review the typed budget block events and adjust limits "
                        "if expected throughput requires it."
                    ),
                    severity=OperationalEventSeverity.WARNING,
                )
            )
        if llm_summary.provider_failures > 0:
            checks.append(
                DailyOpsDigestOperatorCheck(
                    category="provider",
                    message=(
                        "LLM provider failures were recorded. Check provider "
                        "health and the typed provider failure narratives."
                    ),
                    severity=OperationalEventSeverity.WARNING,
                )
            )
        if llm_summary.cooldown_blocks > 0:
            checks.append(
                DailyOpsDigestOperatorCheck(
                    category="cooldown",
                    message=(
                        "Per-market cooldowns activated. Review the typed "
                        "cooldown block narratives for affected categories."
                    ),
                    severity=OperationalEventSeverity.INFO,
                )
            )

    if unresolved_errors:
        checks.append(
            DailyOpsDigestOperatorCheck(
                category="general",
                message=(
                    "Unresolved error or critical events were recorded without "
                    "a typed recovery event. Inspect the unresolved errors "
                    "section for typed context."
                ),
                severity=OperationalEventSeverity.ERROR,
            )
        )
    elif unresolved_warnings:
        checks.append(
            DailyOpsDigestOperatorCheck(
                category="general",
                message=(
                    "Unresolved warnings were recorded. Review the typed "
                    "warning narratives to confirm intended behavior."
                ),
                severity=OperationalEventSeverity.WARNING,
            )
        )

    if not checks:
        checks.append(
            DailyOpsDigestOperatorCheck(
                category="general",
                message=(
                    "No deterministic operator action required from the typed "
                    "event window. Continue routine monitoring."
                ),
                severity=OperationalEventSeverity.INFO,
            )
        )

    return checks


# ── Markdown rendering ─────────────────────────────────────────────────────


def _format_decimal(value: Optional[Decimal], *, places: int = 4) -> str:
    """Format a Decimal for digest display, or '(unavailable)'.

    Never falls back to float arithmetic.
    """
    if value is None:
        return "(unavailable)"
    quantized = value.quantize(Decimal(10) ** -places)
    return f"{quantized}"


def _format_optional_int(value: Optional[int]) -> str:
    if value is None:
        return "(unavailable)"
    return str(value)


def _format_optional_str(value: Optional[str]) -> str:
    if value is None or value == "":
        return "(unavailable)"
    return value


def _format_optional_bool(value: Optional[bool]) -> str:
    if value is None:
        return "(unavailable)"
    return "true" if value else "false"


def _format_optional_dt(value: Optional[datetime]) -> str:
    if value is None:
        return "(unavailable)"
    return _ensure_utc(value).isoformat()


def render_digest_markdown(report: DailyOpsDigestReport) -> str:
    """Render a deterministic markdown digest from a typed report.

    The output is the literal file content written to disk. It is
    intentionally minimal and does not include random IDs or timestamps
    other than typed event evidence so that re-running with the same
    inputs produces identical text.
    """
    date_str = report.request.digest_date_utc.date().isoformat()
    window = report.request.window or _derive_window(report.request)

    lines: list[str] = []
    lines.append(f"# Daily Bot Operations Digest — {date_str}")
    lines.append("")
    lines.append(
        f"Digest window (UTC): `{window.from_utc.isoformat()}` → "
        f"`{window.to_utc.isoformat()}`"
    )
    lines.append("")

    lines.append("## Status")
    lines.append("")
    lines.append(f"- **Status:** `{report.status.value}`")
    if report.failure_reason is not None:
        lines.append(f"- **Failure reason:** `{report.failure_reason.value}`")
    if report.message:
        lines.append(f"- **Note:** {report.message}")
    lines.append("")

    # Run summary
    lines.append("## Run summary")
    lines.append("")
    if report.run_summary is None:
        lines.append("- (no typed lifecycle events in window)")
    else:
        rs = report.run_summary
        lines.append(f"- **Run status:** `{rs.run_status}`")
        lines.append(f"- **Start (UTC):** {_format_optional_dt(rs.start_utc)}")
        lines.append(f"- **Stop (UTC):** {_format_optional_dt(rs.stop_utc)}")
        lines.append(f"- **Uptime (seconds):** {_format_optional_int(rs.uptime_seconds)}")
        lines.append(f"- **Active provider:** {_format_optional_str(rs.active_provider)}")
        lines.append(f"- **Dry-run:** {_format_optional_bool(rs.dry_run)}")
        lines.append(
            f"- **Latest readiness:** {_format_optional_str(rs.latest_readiness)}"
        )
        lines.append(f"- **Markets seen:** {rs.markets_seen}")
        lines.append(f"- **Markets rejected/quarantined:** {rs.markets_rejected}")
    lines.append("")

    # Decisions
    lines.append("## Decisions")
    lines.append("")
    if report.decision_summary is None:
        lines.append("- (no typed decision events in window)")
    else:
        ds = report.decision_summary
        lines.append(f"- **Accepted BUY:** {ds.accepted_buy}")
        lines.append(f"- **Accepted HOLD:** {ds.accepted_hold}")
        lines.append(f"- **Skipped (low confidence):** {ds.skipped_low_conf}")
        lines.append(f"- **Skipped (low EV):** {ds.skipped_low_ev}")
        lines.append(f"- **Skipped (high spread):** {ds.skipped_high_spread}")
        lines.append(f"- **Skipped (exposure):** {ds.skipped_exposure}")
        lines.append(f"- **Skipped (TTR):** {ds.skipped_ttr}")
    lines.append("")

    # LLM
    lines.append("## LLM guard")
    lines.append("")
    if report.llm_summary is None:
        lines.append("- (no typed LLM events in window)")
    else:
        ls = report.llm_summary
        lines.append(f"- **LLM calls:** {ls.llm_calls}")
        lines.append(f"- **Budget blocks:** {ls.budget_blocks}")
        lines.append(f"- **Cooldown blocks:** {ls.cooldown_blocks}")
        lines.append(f"- **Provider failures:** {ls.provider_failures}")
        lines.append(
            f"- **Estimated spend (USD):** {_format_decimal(ls.estimated_spend_usd)}"
        )
    lines.append("")

    # PnL
    lines.append("## Paper PnL")
    lines.append("")
    if report.pnl_summary is None:
        lines.append("- (no repository-backed position data for this window)")
    else:
        ps = report.pnl_summary
        lines.append(f"- **Realized PnL (USDC):** {_format_decimal(ps.realized_pnl, places=6)}")
        lines.append(f"- **Unrealized PnL (USDC):** {_format_decimal(ps.unrealized_pnl, places=6)}")
        lines.append(f"- **Gas + fees (USDC):** {_format_decimal(ps.gas_and_fees, places=6)}")
        lines.append(f"- **Closed positions in window:** {ps.closed_position_count}")
        lines.append(f"- **Open positions:** {ps.open_position_count}")
    lines.append("")

    # Top events
    lines.append("## Top operational events")
    lines.append("")
    if not report.top_events:
        lines.append("- (no operator-visible events in window)")
    else:
        for ev in report.top_events:
            lines.append(
                f"- `{ev.timestamp_utc.isoformat()}` "
                f"**{ev.severity.value}** `{ev.event_type.value}`/"
                f"`{ev.reason_code.value}` — {ev.summary}"
            )
    lines.append("")

    # Unresolved warnings
    lines.append("## Unresolved warnings")
    lines.append("")
    if not report.unresolved_warnings:
        lines.append("- (none)")
    else:
        for ev in report.unresolved_warnings:
            lines.append(
                f"- `{ev.timestamp_utc.isoformat()}` `{ev.event_type.value}`/"
                f"`{ev.reason_code.value}` — {ev.summary}"
            )
    lines.append("")

    # Unresolved errors
    lines.append("## Unresolved errors")
    lines.append("")
    if not report.unresolved_errors:
        lines.append("- (none)")
    else:
        for ev in report.unresolved_errors:
            lines.append(
                f"- `{ev.timestamp_utc.isoformat()}` `{ev.event_type.value}`/"
                f"`{ev.reason_code.value}` — {ev.summary}"
            )
    lines.append("")

    # Operator checks
    lines.append("## Recommended operator checks")
    lines.append("")
    if not report.operator_checks:
        lines.append("- (none)")
    else:
        for check in report.operator_checks:
            lines.append(
                f"- **[{check.category}]** ({check.severity.value}) {check.message}"
            )
    lines.append("")

    # Telegram delivery footer. ``sent_at_utc`` is intentionally omitted
    # from the durable file: WI-60 requires the digest text to be
    # byte-identical across re-runs with the same persisted data, and
    # wall-clock send timestamps would break that invariant. The full
    # typed timestamp remains available on the returned report for
    # callers that want runtime metadata.
    lines.append("## Telegram delivery")
    lines.append("")
    lines.append(f"- **Status:** `{report.telegram_result.status}`")
    if report.telegram_result.failure_reason is not None:
        lines.append(f"- **Note:** {report.telegram_result.failure_reason}")
    lines.append("")

    return "\n".join(lines)


def render_telegram_text(report: DailyOpsDigestReport) -> Optional[str]:
    """Build the short, bounded, secret-safe Telegram summary text.

    Returns ``None`` if the typed report has no operator-facing content.
    """
    date_str = report.request.digest_date_utc.date().isoformat()
    parts: list[str] = [f"Daily digest {date_str}:"]

    if report.run_summary is not None:
        parts.append(f"run={report.run_summary.run_status}")
        if report.run_summary.latest_readiness:
            parts.append(f"readiness={report.run_summary.latest_readiness}")
    if report.decision_summary is not None:
        ds = report.decision_summary
        parts.append(
            f"decisions buy={ds.accepted_buy} hold={ds.accepted_hold}"
        )
    if report.llm_summary is not None:
        ls = report.llm_summary
        parts.append(
            f"llm calls={ls.llm_calls} budget_blocks={ls.budget_blocks} "
            f"provider_failures={ls.provider_failures}"
        )
    if report.unresolved_errors:
        parts.append(f"unresolved_errors={len(report.unresolved_errors)}")
    if report.unresolved_warnings:
        parts.append(f"unresolved_warnings={len(report.unresolved_warnings)}")

    text = "; ".join(parts)
    if len(text) > _TELEGRAM_TEXT_LIMIT:
        text = text[: _TELEGRAM_TEXT_LIMIT - 1] + "…"
    return text


# ── Telegram delivery ──────────────────────────────────────────────────────


async def _maybe_send_telegram(
    summary: DailyOpsDigestTelegramSummary,
    telegram_notifier,
) -> DailyOpsDigestTelegramResult:
    """Attempt Telegram delivery; never raise. Returns a typed result.

    Prefers ``try_send_execution_event`` (typed bool return) when the
    notifier exposes it so a swallowed transport failure is reported as
    ``failed`` rather than silently as ``sent``. Falls back to the older
    ``send_execution_event`` interface used by integration test doubles.
    """
    if not summary.enabled or summary.text is None:
        return DailyOpsDigestTelegramResult(status="disabled")

    if telegram_notifier is None:
        return DailyOpsDigestTelegramResult(
            status="skipped",
            failure_reason="telegram notifier not configured",
        )

    typed_send = getattr(telegram_notifier, "try_send_execution_event", None)
    if callable(typed_send):
        try:
            sent = await typed_send(summary.text, dry_run=False)
        except Exception:  # noqa: BLE001 — defensive; never crash digest
            logger.warning(
                "daily_ops_digest.telegram_send_failed", exc_info=False
            )
            return DailyOpsDigestTelegramResult(
                status="failed",
                failure_reason="telegram send failed",
            )
        if not bool(sent):
            return DailyOpsDigestTelegramResult(
                status="failed",
                failure_reason="telegram send reported failure",
            )
        return DailyOpsDigestTelegramResult(
            status="sent",
            sent_at_utc=datetime.now(timezone.utc),
        )

    # Fallback: legacy notifier without a typed return. Exception → failed,
    # otherwise treat as best-effort sent. Real production code paths use
    # the typed interface above.
    try:
        await telegram_notifier.send_execution_event(summary.text, dry_run=False)
    except Exception:  # noqa: BLE001 — defensive; never crash digest
        logger.warning("daily_ops_digest.telegram_send_failed", exc_info=False)
        return DailyOpsDigestTelegramResult(
            status="failed",
            failure_reason="telegram send failed",
        )

    return DailyOpsDigestTelegramResult(
        status="sent",
        sent_at_utc=datetime.now(timezone.utc),
    )


# ── Public API ─────────────────────────────────────────────────────────────


def _failure_report(
    request: DailyOpsDigestRequest,
    *,
    status: DailyOpsDigestStatus,
    failure_reason: DailyOpsDigestFailureReason,
    message: str,
    path: str = "",
    telegram_result: Optional[DailyOpsDigestTelegramResult] = None,
) -> DailyOpsDigestReport:
    """Build a typed failure report with a bounded, secret-free message.

    ``telegram_result`` carries the actual Telegram outcome when the
    failure occurs AFTER delivery (render, scan, path-guard, atomic
    write). Pre-Telegram failures (path validation, repository read)
    leave it ``None`` and the report records ``disabled``.
    """
    safe_path = path if path else "<unresolved>"
    return DailyOpsDigestReport(
        status=status,
        request=request,
        run_summary=None,
        decision_summary=None,
        llm_summary=None,
        pnl_summary=None,
        top_events=[],
        unresolved_warnings=[],
        unresolved_errors=[],
        operator_checks=[],
        telegram_result=telegram_result
        or DailyOpsDigestTelegramResult(status="disabled"),
        write_result=DailyOpsDigestWriteResult(
            path=safe_path,
            written=False,
            bytes_written=0,
        ),
        failure_reason=failure_reason,
        message=message,
    )


async def generate_digest(
    request: DailyOpsDigestRequest,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    telegram_notifier=None,
    daily_notes_root: Optional[Path] = None,
) -> DailyOpsDigestReport:
    """Generate the typed daily ops digest for ``request``.

    On success the digest markdown file is written at the validated bot
    digest path and a typed report is returned. Database or repository
    failures, invalid output paths, manual-note collisions, and forbidden
    content are returned as typed non-success reports — the manual note
    file is never created, overwritten, truncated, or deleted in any
    path.
    """
    daily_root = _resolve_daily_notes_root(request, daily_notes_root)

    path_result, path_failure, path_message = _validate_output_path(
        request, daily_root
    )
    if path_failure is not None:
        # Translate path-failure typed reasons into the correct status.
        return _failure_report(
            request,
            status=DailyOpsDigestStatus.PATH_FAILURE,
            failure_reason=path_failure,
            message=path_message or "invalid digest output path",
        )
    assert path_result is not None
    output_path = path_result

    window = _derive_window(request)

    events, fetch_status, fetch_failure, fetch_message = await _read_events(
        session_factory, window
    )
    if events is None:
        # Repository / database failure: do not write any file.
        assert fetch_failure is not None
        return _failure_report(
            request,
            status=fetch_status,
            failure_reason=fetch_failure,
            message=fetch_message or "operational event read failed",
            path=str(output_path),
        )

    pnl_summary = await _read_paper_pnl(session_factory, window)

    # Sort chronologically — drives all derived state.
    sorted_records = _sort_chronological(events)

    # Build highlights for each record once; reuse across sections.
    highlights_by_id: dict[str, DailyOpsDigestEventHighlight] = {}
    all_highlights: list[DailyOpsDigestEventHighlight] = []
    for record in sorted_records:
        narrative = render_event(record)
        highlight = _record_to_highlight(record, narrative)
        if highlight is None:
            continue
        highlights_by_id[record.id] = highlight
        all_highlights.append(highlight)

    if not events and pnl_summary is None:
        # Truly empty day — write a valid no-run digest file.
        empty_report = DailyOpsDigestReport(
            status=DailyOpsDigestStatus.EMPTY_WINDOW,
            request=request,
            run_summary=None,
            decision_summary=None,
            llm_summary=None,
            pnl_summary=None,
            top_events=[],
            unresolved_warnings=[],
            unresolved_errors=[],
            operator_checks=[
                DailyOpsDigestOperatorCheck(
                    category="general",
                    message=(
                        "No operational events were recorded for the digest "
                        "window. Confirm the bot was scheduled to run."
                    ),
                    severity=OperationalEventSeverity.INFO,
                )
            ],
            telegram_result=DailyOpsDigestTelegramResult(status="disabled"),
            write_result=DailyOpsDigestWriteResult(
                path=str(output_path),
                written=False,
                bytes_written=0,
            ),
        )
        return _write_and_finalize(
            empty_report,
            output_path=output_path,
            telegram_notifier=telegram_notifier,
        )

    run_summary = _derive_run_summary(sorted_records, window)
    decision_summary = _derive_decision_summary(sorted_records)
    llm_summary = _derive_llm_summary(sorted_records)

    top_events = _select_top_events(all_highlights)
    unresolved_warnings = _select_unresolved(
        sorted_records, highlights_by_id, _WARNING_SEVERITIES
    )
    unresolved_errors = _select_unresolved(
        sorted_records, highlights_by_id, _ERROR_SEVERITIES
    )
    operator_checks = _derive_operator_checks(
        run_summary,
        decision_summary,
        llm_summary,
        unresolved_warnings,
        unresolved_errors,
    )

    # Build the success report with a placeholder write result; replace
    # after the disk write succeeds and Telegram path runs.
    pending_report = DailyOpsDigestReport(
        status=DailyOpsDigestStatus.SUCCESS,
        request=request,
        run_summary=run_summary,
        decision_summary=decision_summary,
        llm_summary=llm_summary,
        pnl_summary=pnl_summary,
        top_events=top_events,
        unresolved_warnings=unresolved_warnings,
        unresolved_errors=unresolved_errors,
        operator_checks=operator_checks,
        telegram_result=DailyOpsDigestTelegramResult(status="disabled"),
        write_result=DailyOpsDigestWriteResult(
            path=str(output_path),
            written=False,
            bytes_written=0,
        ),
    )

    return _write_and_finalize(
        pending_report,
        output_path=output_path,
        telegram_notifier=telegram_notifier,
    )


def _write_and_finalize(
    report: DailyOpsDigestReport,
    *,
    output_path: Path,
    telegram_notifier,
) -> DailyOpsDigestReport:
    """Synchronously render, scan, write, and optionally Telegram-deliver.

    Helper allows the empty-window and success paths to share write +
    Telegram logic without duplicating safety checks.
    """
    return _sync_finalize(
        report=report,
        output_path=output_path,
        telegram_notifier=telegram_notifier,
    )


def _sync_finalize(
    *,
    report: DailyOpsDigestReport,
    output_path: Path,
    telegram_notifier,
) -> DailyOpsDigestReport:
    """Deliver Telegram, render markdown with the durable footer, write file.

    Telegram delivery happens BEFORE markdown rendering so the on-disk
    digest footer reflects the actual delivery outcome (``sent``,
    ``failed``, ``disabled``) rather than the construction-time placeholder.
    The Telegram summary text uses only typed counter fields and never
    carries payload-derived strings, so sending before the markdown
    secret-scan does not weaken WI-60's safety guarantees.
    """
    # 1. Optional Telegram delivery FIRST so the file footer is durable.
    telegram_summary = DailyOpsDigestTelegramSummary(
        enabled=report.request.enable_telegram,
        text=render_telegram_text(report) if report.request.enable_telegram else None,
    )
    telegram_result = _run_sync_telegram(telegram_summary, telegram_notifier)

    # 2. Rebuild the report with the actual telegram_result so the
    # rendered markdown footer matches what really happened.
    report = report.model_copy(update={"telegram_result": telegram_result})

    # 3. Render markdown text (now reflects real telegram_result).
    try:
        markdown = render_digest_markdown(report)
    except Exception:  # noqa: BLE001 — defensive
        logger.warning("daily_ops_digest.render_failed", exc_info=False)
        return _failure_report(
            report.request,
            status=DailyOpsDigestStatus.FORBIDDEN_CONTENT,
            failure_reason=DailyOpsDigestFailureReason.FORBIDDEN_CONTENT,
            message="digest rendering failed",
            path=str(output_path),
            telegram_result=telegram_result,
        )

    # 4. Defense-in-depth secret scan on the fully rendered text.
    if _scan_event_payload(markdown):
        logger.warning("daily_ops_digest.forbidden_content_in_rendered_text")
        return _failure_report(
            report.request,
            status=DailyOpsDigestStatus.FORBIDDEN_CONTENT,
            failure_reason=DailyOpsDigestFailureReason.FORBIDDEN_CONTENT,
            message="rendered digest contained forbidden content",
            path=str(output_path),
            telegram_result=telegram_result,
        )

    # 5. Safety check — never write to a manual note path even if path
    # validation was bypassed (extra guard against future code changes).
    if _MANUAL_FILENAME_RE.match(output_path.name):
        return _failure_report(
            report.request,
            status=DailyOpsDigestStatus.PATH_FAILURE,
            failure_reason=DailyOpsDigestFailureReason.MANUAL_NOTE_WOULD_OVERWRITE,
            message="refusing to overwrite manual coding daily note",
            path=str(output_path),
            telegram_result=telegram_result,
        )

    # 6. Write atomically: write to a temp file and rename.
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        tmp_path.write_text(markdown, encoding="utf-8")
        tmp_path.replace(output_path)
        bytes_written = len(markdown.encode("utf-8"))
    except OSError:
        logger.warning("daily_ops_digest.write_failed", exc_info=False)
        return _failure_report(
            report.request,
            status=DailyOpsDigestStatus.PATH_FAILURE,
            failure_reason=DailyOpsDigestFailureReason.PATH_OUTSIDE_DAILY,
            message="digest file write failed",
            path=str(output_path),
            telegram_result=telegram_result,
        )

    write_result = DailyOpsDigestWriteResult(
        path=str(output_path),
        written=True,
        bytes_written=bytes_written,
    )

    # Rebuild the report with the final write_result. telegram_result is
    # already present on ``report`` from the model_copy above, so the
    # returned report and the on-disk markdown agree.
    return report.model_copy(update={"write_result": write_result})


def _run_sync_telegram(
    summary: DailyOpsDigestTelegramSummary,
    telegram_notifier,
) -> DailyOpsDigestTelegramResult:
    """Run the async telegram delivery to completion regardless of loop state.

    The digest generation may be called from an async or sync context.
    A failed Telegram send must never propagate or corrupt the written
    file; the typed result captures the outcome.
    """
    import asyncio
    import threading

    if not summary.enabled or summary.text is None:
        return DailyOpsDigestTelegramResult(status="disabled")

    coro = _maybe_send_telegram(summary, telegram_notifier)

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(coro)
        except Exception:  # noqa: BLE001 — defensive
            logger.warning("daily_ops_digest.telegram_loop_failed", exc_info=False)
            return DailyOpsDigestTelegramResult(
                status="failed", failure_reason="telegram delivery failed"
            )

    result_box: dict[str, DailyOpsDigestTelegramResult] = {}

    def _worker() -> None:
        try:
            result_box["r"] = asyncio.run(coro)
        except Exception:  # noqa: BLE001 — defensive
            logger.warning("daily_ops_digest.telegram_worker_failed", exc_info=False)
            result_box["r"] = DailyOpsDigestTelegramResult(
                status="failed", failure_reason="telegram delivery failed"
            )

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=30.0)
    if thread.is_alive():
        return DailyOpsDigestTelegramResult(
            status="failed", failure_reason="telegram delivery timed out"
        )
    return result_box.get(
        "r",
        DailyOpsDigestTelegramResult(
            status="failed", failure_reason="telegram delivery failed"
        ),
    )
