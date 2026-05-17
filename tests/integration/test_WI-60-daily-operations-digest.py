"""
tests/integration/test_WI-60-daily-operations-digest.py

Integration tests for WI-60 Daily Operations Digest.

Covers end-to-end paths that exercise the repository + service + CLI
boundary against the shared in-memory async SQLite engine:

* Full lifecycle digest generation with START + SHUTDOWN + decisions.
* Repository-backed paper PnL aggregation via PositionRepository.
* Missing operational_events table fails closed with typed reasons.
* CLI end-to-end run produces the expected file and exit code.
* MAAP regression: latest typed recovery does not crash digest derivation.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models import OperationalEvent, Position
from src.db.repositories.operational_event_repository import (
    OperationalEventRepository,
)
from src.observability.daily_ops_digest import (
    generate_digest,
    render_digest_markdown,
    render_telegram_text,
)
from src.schemas.ops import (
    DailyOpsDigestFailureReason,
    DailyOpsDigestRequest,
    DailyOpsDigestStatus,
    OperationalEventCreate,
    OperationalEventPayload,
    OperationalEventPersistenceStatus,
    OperationalEventReasonCode,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CLI_PATH = _PROJECT_ROOT / "scripts" / "ops" / "generate_daily_ops_digest.py"


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("wi60_digest_cli_int", _CLI_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_cli = _load_cli_module()


def _utc(
    year: int = 2026,
    month: int = 5,
    day: int = 15,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
) -> datetime:
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


async def _insert_settled_position(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    condition_id: str,
    realized_pnl: Decimal,
    closed_at_utc: datetime,
    gas: Decimal = Decimal("0.05"),
    fees: Decimal = Decimal("0.10"),
) -> None:
    async with session_factory() as session:
        async with session.begin():
            position = Position(
                id=str(uuid.uuid4()),
                condition_id=condition_id,
                token_id=f"tok-{uuid.uuid4().hex[:8]}",
                status="CLOSED",
                side="BUY",
                entry_price=Decimal("0.45"),
                order_size_usdc=Decimal("25"),
                kelly_fraction=Decimal("0.25"),
                best_ask_at_entry=Decimal("0.455"),
                bankroll_usdc_at_entry=Decimal("1000"),
                execution_action="BUY",
                reason="integration_test",
                routed_at_utc=closed_at_utc - timedelta(hours=2),
                realized_pnl=realized_pnl,
                exit_price=Decimal("0.55"),
                closed_at_utc=closed_at_utc,
                gas_cost_usdc=gas,
                fees_usdc=fees,
            )
            session.add(position)


# ───────────────────────────────────────────────────────────────────────────
# End-to-end digest happy path
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_lifecycle_digest_generation(
    db_session_factory, tmp_path
) -> None:
    """A typical day produces a SUCCESS digest with completed run and counts."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=0, minute=5),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.READY,
        timestamp_utc=_utc(hour=0, minute=6),
    )
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
        event_type=OperationalEventType.DECISION_ACCEPTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_HOLD,
        timestamp_utc=_utc(hour=2, minute=1),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.DECISION_SKIPPED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
        timestamp_utc=_utc(hour=3),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.SHUTDOWN,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.GRACEFUL_SHUTDOWN,
        timestamp_utc=_utc(hour=23, minute=50),
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
    )
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    assert report.status == DailyOpsDigestStatus.SUCCESS
    assert report.run_summary is not None
    assert report.run_summary.run_status == "completed"
    assert report.run_summary.start_utc == _utc(hour=0, minute=5)
    assert report.run_summary.stop_utc == _utc(hour=23, minute=50)
    assert report.run_summary.uptime_seconds is not None
    assert report.run_summary.uptime_seconds > 0
    assert report.run_summary.latest_readiness == "READY"

    assert report.decision_summary is not None
    assert report.decision_summary.accepted_hold == 1
    assert report.decision_summary.skipped_low_conf == 1

    assert report.llm_summary is not None
    assert report.llm_summary.llm_calls == 1

    # Digest file was written deterministically.
    bot_path = daily_root / "2026-05-15-bot.md"
    assert bot_path.exists()
    assert report.write_result.path == str(bot_path)
    assert report.write_result.bytes_written > 0


@pytest.mark.asyncio
async def test_digest_includes_repository_backed_pnl(
    db_session_factory, tmp_path
) -> None:
    """When settled positions exist in window, paper PnL aggregates them."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )
    await _insert_settled_position(
        db_session_factory,
        condition_id="cond-int-1",
        realized_pnl=Decimal("12.50"),
        closed_at_utc=_utc(hour=10),
        gas=Decimal("0.05"),
        fees=Decimal("0.10"),
    )
    await _insert_settled_position(
        db_session_factory,
        condition_id="cond-int-2",
        realized_pnl=Decimal("-3.25"),
        closed_at_utc=_utc(hour=12),
        gas=Decimal("0.04"),
        fees=Decimal("0.08"),
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
    )
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    assert report.pnl_summary is not None
    # SQLite Numeric round-trip loses sub-cent precision; quantize for comparison.
    quantized = Decimal("0.0001")
    assert (
        report.pnl_summary.realized_pnl is not None
        and abs(report.pnl_summary.realized_pnl - (Decimal("12.50") + Decimal("-3.25")))
        < quantized
    )
    assert (
        report.pnl_summary.gas_and_fees is not None
        and abs(
            report.pnl_summary.gas_and_fees
            - (Decimal("0.05") + Decimal("0.10") + Decimal("0.04") + Decimal("0.08"))
        )
        < quantized
    )
    assert report.pnl_summary.closed_position_count == 2


@pytest.mark.asyncio
async def test_digest_settled_positions_outside_window_excluded(
    db_session_factory, tmp_path
) -> None:
    """Positions closed outside the UTC day are not counted in paper PnL."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=1),
    )
    # Closed the day before the digest window.
    await _insert_settled_position(
        db_session_factory,
        condition_id="cond-out-1",
        realized_pnl=Decimal("99"),
        closed_at_utc=_utc(year=2026, month=5, day=14, hour=23),
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
    )
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    assert report.pnl_summary is not None
    # No positions closed within window; realized_pnl stays None (unavailable).
    assert report.pnl_summary.closed_position_count == 0
    assert report.pnl_summary.realized_pnl is None


# ───────────────────────────────────────────────────────────────────────────
# Missing-table / repository failure paths
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_operational_events_table_fails_closed(
    db_session_factory, tmp_path
) -> None:
    """When operational_events is missing, no digest file is created."""
    # Drop the operational_events table after the engine was set up so
    # the digest read path observes "no such table".
    factory = db_session_factory
    async with factory() as session:
        async with session.begin():
            await session.execute(text("DROP TABLE operational_events"))

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
    )
    report = await generate_digest(req, factory, daily_notes_root=daily_root)

    assert report.status == DailyOpsDigestStatus.MISSING_TABLE
    assert report.failure_reason == DailyOpsDigestFailureReason.MISSING_TABLE
    bot_path = daily_root / "2026-05-15-bot.md"
    assert not bot_path.exists()


# ───────────────────────────────────────────────────────────────────────────
# CLI end-to-end
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cli_end_to_end_writes_digest(
    db_session_factory, tmp_path
) -> None:
    """The CLI run with a real session factory produces a digest and returns 0."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=0, minute=5),
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    rc = _cli.main(
        [
            "--date",
            "2026-05-15",
            "--daily-notes-dir",
            str(daily_root),
        ],
        session_factory=db_session_factory,
        daily_notes_root=daily_root,
    )
    assert rc == 0
    bot_path = daily_root / "2026-05-15-bot.md"
    assert bot_path.exists()


@pytest.mark.asyncio
async def test_cli_telegram_failure_returns_zero(
    db_session_factory, tmp_path
) -> None:
    """CLI exit code remains 0 even when telegram delivery fails."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=0, minute=5),
    )

    class _FailingNotifier:
        async def send_execution_event(self, summary: str, dry_run: bool) -> None:
            raise RuntimeError("nope")

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    rc = _cli.main(
        [
            "--date",
            "2026-05-15",
            "--daily-notes-dir",
            str(daily_root),
            "--enable-telegram",
        ],
        session_factory=db_session_factory,
        daily_notes_root=daily_root,
        telegram_notifier=_FailingNotifier(),
    )
    assert rc == 0
    bot_path = daily_root / "2026-05-15-bot.md"
    assert bot_path.exists()


# ───────────────────────────────────────────────────────────────────────────
# MAAP regression: latest-wins recovery semantics
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unresolved_warnings_clear_after_typed_recovery(
    db_session_factory, tmp_path
) -> None:
    """Newer typed READY clears older DEGRADED from unresolved_warnings."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.DEGRADED,
        timestamp_utc=_utc(hour=2),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.READY_STATE_CHANGED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.READY,
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

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
    )
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    assert report.status == DailyOpsDigestStatus.SUCCESS
    # DEGRADED was followed by READY → should not be in unresolved_warnings.
    degraded_in_unresolved = any(
        ev.reason_code == OperationalEventReasonCode.DEGRADED
        for ev in report.unresolved_warnings
    )
    assert not degraded_in_unresolved


@pytest.mark.asyncio
async def test_circuit_breaker_recovery_clears_open_event(
    db_session_factory, tmp_path
) -> None:
    """CIRCUIT_BREAKER_CLOSED clears earlier CIRCUIT_BREAKER_OPEN from unresolved errors."""
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.CIRCUIT_BREAKER_OPEN,
        severity=OperationalEventSeverity.CRITICAL,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.CB_OPEN,
        timestamp_utc=_utc(hour=2),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.CIRCUIT_BREAKER_CLOSED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.CB_CLOSED,
        timestamp_utc=_utc(hour=5),
    )
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
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
    )
    report = await generate_digest(req, db_session_factory, daily_notes_root=daily_root)

    cb_open_in_unresolved_errors = any(
        ev.event_type == OperationalEventType.CIRCUIT_BREAKER_OPEN
        for ev in report.unresolved_errors
    )
    assert not cb_open_in_unresolved_errors


# ───────────────────────────────────────────────────────────────────────────
# Rendering helpers
# ───────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_render_telegram_text_is_bounded_and_secret_safe(
    db_session_factory, tmp_path
) -> None:
    """The telegram summary stays under the configured length cap."""
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
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
        enable_telegram=True,
    )

    class _CaptureNotifier:
        def __init__(self) -> None:
            self.last_summary: Optional[str] = None

        async def send_execution_event(self, summary: str, dry_run: bool) -> None:
            self.last_summary = summary

    notifier = _CaptureNotifier()
    report = await generate_digest(
        req,
        db_session_factory,
        daily_notes_root=daily_root,
        telegram_notifier=notifier,
    )

    assert notifier.last_summary is not None
    assert len(notifier.last_summary) <= 1024
    text_payload = render_telegram_text(report) or ""
    assert "Daily digest" in text_payload


# ───────────────────────────────────────────────────────────────────────────
# Aggregate-count integrity — pagination beyond the per-page schema cap
# ───────────────────────────────────────────────────────────────────────────


async def _bulk_insert_events(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    base_ts: datetime,
    event_type: OperationalEventType,
    severity: OperationalEventSeverity,
    source: OperationalEventSource,
    reason_code: OperationalEventReasonCode,
    count: int,
    step: timedelta = timedelta(milliseconds=1),
) -> None:
    """Fast bulk-insert helper that stamps monotonically increasing timestamps.

    Bypasses the per-event session round-trip of ``_append_event`` so a
    multi-thousand event window can be assembled in a single transaction.
    Payload JSON is the secret-safe default ``{}``.
    """
    now = datetime.now(timezone.utc)
    rows = [
        {
            "id": str(uuid.uuid4()),
            "event_type": event_type.value,
            "severity": severity.value,
            "source": source.value,
            "reason_code": reason_code.value,
            "payload_json": "{}",
            "persistence_status": OperationalEventPersistenceStatus.PERSISTED.value,
            "created_at_utc": base_ts + step * i,
            "recorded_at_utc": now,
        }
        for i in range(count)
    ]
    async with session_factory() as session:
        async with session.begin():
            await session.execute(insert(OperationalEvent), rows)


@pytest.mark.asyncio
async def test_digest_aggregates_full_window_when_events_exceed_page_limit(
    db_session_factory, tmp_path
) -> None:
    """WI-60 rule: aggregate counts must include every typed event in the
    digest window even when daily volume exceeds the per-page schema cap of
    1000 records. The previous implementation read only the most-recent 1000
    rows, silently dropping the START lifecycle event and undercounting LLM
    calls, budget blocks, decisions, and market discoveries.
    """
    # Lifecycle anchors at the edges of the day. START must survive even
    # though it is the oldest record once the busy mid-day window lands.
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=0, minute=0, second=1),
    )
    await _bulk_insert_events(
        db_session_factory,
        base_ts=_utc(hour=1),
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        count=1100,
    )
    await _bulk_insert_events(
        db_session_factory,
        base_ts=_utc(hour=10),
        event_type=OperationalEventType.DECISION_ACCEPTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.DECISION_BUY,
        count=40,
    )
    await _bulk_insert_events(
        db_session_factory,
        base_ts=_utc(hour=11),
        event_type=OperationalEventType.BUDGET_BLOCK,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.BUDGET_HOURLY,
        count=25,
    )
    await _bulk_insert_events(
        db_session_factory,
        base_ts=_utc(hour=12),
        event_type=OperationalEventType.MARKET_DISCOVERED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.INGESTION,
        reason_code=OperationalEventReasonCode.MARKET_FOUND,
        count=150,
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.SHUTDOWN,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.GRACEFUL_SHUTDOWN,
        timestamp_utc=_utc(hour=23, minute=59),
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
    )
    report = await generate_digest(
        req, db_session_factory, daily_notes_root=daily_root
    )

    assert report.status == DailyOpsDigestStatus.SUCCESS
    assert report.run_summary is not None
    # START survives pagination; lifecycle is fully observed.
    assert report.run_summary.run_status == "completed"
    assert report.run_summary.start_utc == _utc(hour=0, minute=0, second=1)
    assert report.run_summary.stop_utc == _utc(hour=23, minute=59)
    assert report.run_summary.markets_seen == 150

    assert report.llm_summary is not None
    # 1100 LLM call starts + 25 BUDGET_BLOCKs are both in
    # _BUDGET_BLOCK_TYPES paths; budget blocks remain distinct.
    assert report.llm_summary.llm_calls == 1100
    assert report.llm_summary.budget_blocks == 25

    assert report.decision_summary is not None
    assert report.decision_summary.accepted_buy == 40

    # Top-event list remains bounded — only the *aggregates* are unbounded.
    assert len(report.top_events) <= 10


@pytest.mark.asyncio
async def test_digest_file_is_byte_identical_across_runs_with_telegram(
    db_session_factory, tmp_path
) -> None:
    """Re-running the digest with the same persisted data and Telegram
    enabled must produce a byte-identical file. The Telegram delivery
    timestamp would otherwise drift between runs and break WI-60's
    determinism invariant (rule 6).
    """
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

    class _TypedOkNotifier:
        async def try_send_execution_event(self, summary: str, dry_run: bool) -> bool:
            return True

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
        enable_telegram=True,
    )

    report1 = await generate_digest(
        req,
        db_session_factory,
        daily_notes_root=daily_root,
        telegram_notifier=_TypedOkNotifier(),
    )
    bot_path = Path(report1.write_result.path)
    text1 = bot_path.read_text(encoding="utf-8")

    report2 = await generate_digest(
        req,
        db_session_factory,
        daily_notes_root=daily_root,
        telegram_notifier=_TypedOkNotifier(),
    )
    text2 = bot_path.read_text(encoding="utf-8")

    assert report1.status == DailyOpsDigestStatus.SUCCESS
    assert report2.status == DailyOpsDigestStatus.SUCCESS
    assert report1.telegram_result.status == "sent"
    assert report2.telegram_result.status == "sent"
    # Even with two real (different) sent_at_utc timestamps on the typed
    # reports, the on-disk file is byte-identical.
    assert text1 == text2


@pytest.mark.asyncio
async def test_run_summary_uptime_sums_multiple_typed_lifecycle_pairs(
    db_session_factory, tmp_path
) -> None:
    """Edge Case 9: a day with two completed START/SHUTDOWN cycles must
    report uptime as the sum of the typed pair spans, not as the span
    between the earliest START and the latest SHUTDOWN.
    """
    # First cycle: 01:00 – 02:00 (1 hour)
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
        timestamp_utc=_utc(hour=2),
    )
    # Second cycle: 10:00 – 11:00 (1 hour)
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=10),
    )
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.SHUTDOWN,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.GRACEFUL_SHUTDOWN,
        timestamp_utc=_utc(hour=11),
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
    )
    report = await generate_digest(
        req, db_session_factory, daily_notes_root=daily_root
    )

    assert report.status == DailyOpsDigestStatus.SUCCESS
    assert report.run_summary is not None
    assert report.run_summary.run_status == "completed"
    # Display anchors remain earliest START / latest SHUTDOWN.
    assert report.run_summary.start_utc == _utc(hour=1)
    assert report.run_summary.stop_utc == _utc(hour=11)
    # Uptime is summed from typed pairs: 3600 + 3600 = 7200, NOT 36000.
    assert report.run_summary.uptime_seconds == 7200


@pytest.mark.asyncio
async def test_run_summary_partial_after_completed_cycle_returns_unavailable(
    db_session_factory, tmp_path
) -> None:
    """Edge Case 10: a completed cycle followed by an open START at the
    end of the window marks the run as partial and leaves uptime
    unavailable rather than guessing a stop time or summing only the
    completed cycle.
    """
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
        timestamp_utc=_utc(hour=2),
    )
    # Second cycle starts but does not stop within the window.
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=10),
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
    )
    report = await generate_digest(
        req, db_session_factory, daily_notes_root=daily_root
    )

    assert report.status == DailyOpsDigestStatus.SUCCESS
    assert report.run_summary is not None
    assert report.run_summary.run_status == "partial"
    assert report.run_summary.start_utc == _utc(hour=1)
    # Last typed SHUTDOWN remains visible as the latest stop anchor.
    assert report.run_summary.stop_utc == _utc(hour=2)
    # No invented uptime: the trailing open cycle leaves it unavailable.
    assert report.run_summary.uptime_seconds is None


@pytest.mark.asyncio
async def test_digest_file_footer_reflects_telegram_failure(
    db_session_factory, tmp_path
) -> None:
    """The on-disk digest footer must record the real Telegram outcome,
    not the construction-time placeholder. Previously the markdown was
    rendered before delivery so the durable file always read ``disabled``
    even when the runtime report said ``sent`` or ``failed``.
    """
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=0, minute=5),
    )

    class _TypedFailingNotifier:
        async def try_send_execution_event(self, summary: str, dry_run: bool) -> bool:
            return False

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
        enable_telegram=True,
    )
    report = await generate_digest(
        req,
        db_session_factory,
        daily_notes_root=daily_root,
        telegram_notifier=_TypedFailingNotifier(),
    )

    assert report.telegram_result.status == "failed"
    assert report.write_result.written is True

    bot_path = daily_root / "2026-05-15-bot.md"
    assert bot_path.exists()
    contents = bot_path.read_text(encoding="utf-8")
    # The Telegram delivery section is the documented runbook footer.
    assert "## Telegram delivery" in contents
    assert "`failed`" in contents
    assert "`disabled`" not in contents


@pytest.mark.asyncio
async def test_digest_write_failure_preserves_telegram_outcome(
    db_session_factory, tmp_path, monkeypatch
) -> None:
    """When the atomic write fails AFTER Telegram has delivered, the typed
    failure report must carry the real telegram_result instead of clobbering
    it with the ``disabled`` placeholder. Otherwise the operator sees the
    Telegram message land but the CLI report claims delivery never happened.
    """
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=0, minute=5),
    )

    class _TypedOkNotifier:
        def __init__(self) -> None:
            self.calls: int = 0

        async def try_send_execution_event(self, summary: str, dry_run: bool) -> bool:
            self.calls += 1
            return True

    original_write_text = Path.write_text

    def _failing_tmp_write(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name.endswith(".tmp"):
            raise OSError("simulated disk failure")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _failing_tmp_write)

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    notifier = _TypedOkNotifier()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
        enable_telegram=True,
    )
    report = await generate_digest(
        req,
        db_session_factory,
        daily_notes_root=daily_root,
        telegram_notifier=notifier,
    )

    # Telegram was actually delivered exactly once.
    assert notifier.calls == 1
    # Write failed → typed PATH failure surfaces.
    assert report.status == DailyOpsDigestStatus.PATH_FAILURE
    assert report.failure_reason is not None
    assert report.write_result.written is False
    assert not (daily_root / "2026-05-15-bot.md").exists()
    # Critical auditability invariant: the real delivery outcome must
    # NOT be erased by the post-Telegram failure path.
    assert report.telegram_result.status == "sent"
    assert report.telegram_result.sent_at_utc is not None


@pytest.mark.asyncio
async def test_digest_file_footer_reflects_telegram_success(
    db_session_factory, tmp_path
) -> None:
    """When Telegram delivery succeeds, the durable footer says ``sent`` and
    records a UTC timestamp — proving the markdown sees the real outcome.
    """
    await _append_event(
        db_session_factory,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp_utc=_utc(hour=0, minute=5),
    )

    class _TypedOkNotifier:
        async def try_send_execution_event(self, summary: str, dry_run: bool) -> bool:
            return True

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
        enable_telegram=True,
    )
    report = await generate_digest(
        req,
        db_session_factory,
        daily_notes_root=daily_root,
        telegram_notifier=_TypedOkNotifier(),
    )

    assert report.telegram_result.status == "sent"
    assert report.write_result.written is True

    bot_path = daily_root / "2026-05-15-bot.md"
    contents = bot_path.read_text(encoding="utf-8")
    assert "## Telegram delivery" in contents
    assert "`sent`" in contents
    # ``sent_at_utc`` is a wall-clock value and is intentionally NOT
    # rendered into the durable file so re-runs stay byte-identical.
    assert "Sent at (UTC):" not in contents
    assert "`disabled`" not in contents
    # The full typed timestamp remains on the in-memory report.
    assert report.telegram_result.sent_at_utc is not None


@pytest.mark.asyncio
async def test_digest_fails_closed_when_event_read_cap_is_exceeded(
    db_session_factory, tmp_path, monkeypatch
) -> None:
    """When pagination would exceed the bounded read budget the digest must
    refuse to write a partial file. Returning a successful-but-undercounted
    digest is the exact failure mode WI-60's aggregate-count rule forbids.
    """
    from src.observability import daily_ops_digest as digest_mod

    # Shrink the safety cap so the test can trigger it without inserting
    # 200_000 events. With page_size=5 and max_pages=2, any window with
    # more than 10 events plus a positive has_more flag should trip it.
    monkeypatch.setattr(digest_mod, "_DEFAULT_EVENT_LIMIT", 5)
    monkeypatch.setattr(digest_mod, "_MAX_EVENT_READ_PAGES", 2)

    await _bulk_insert_events(
        db_session_factory,
        base_ts=_utc(hour=1),
        event_type=OperationalEventType.LLM_CALL_STARTED,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_STARTED,
        count=25,
    )

    daily_root = tmp_path / "03_Daily"
    daily_root.mkdir()
    req = DailyOpsDigestRequest(
        digest_date_utc=_utc(hour=0),
        daily_notes_dir=str(daily_root),
    )
    report = await generate_digest(
        req, db_session_factory, daily_notes_root=daily_root
    )

    assert report.status == DailyOpsDigestStatus.READ_CAP_REACHED
    assert report.failure_reason == DailyOpsDigestFailureReason.READ_CAP_REACHED
    # No partial digest file written: aggregates would silently undercount.
    assert report.write_result.written is False
    assert not (daily_root / "2026-05-15-bot.md").exists()
    # Aggregates are not synthesized from partial reads.
    assert report.run_summary is None
    assert report.llm_summary is None
    assert report.decision_summary is None
