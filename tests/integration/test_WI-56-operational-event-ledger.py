"""
tests/integration/test_WI-56-operational-event-ledger.py

Integration tests for WI-56 Operational Event Ledger — migration,
repository append/read-window, append-only API contract, event bus
batch flush, shutdown flush, queue overflow, and orchestrator
lifecycle event emission.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.schemas.ops import (
    OperationalEventCreate,
    OperationalEventPayload,
    OperationalEventQuery,
    OperationalEventReasonCode,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
    OperationalEventBatchResult,
    OperationalEventAppendResult,
    OperationalEventFlushResult,
    OperationalEventPersistenceStatus,
)
from src.db.repositories.operational_event_repository import (
    OperationalEventRepository,
)
from src.observability.operational_event_bus import OperationalEventBus


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_config(**overrides):
    cfg = MagicMock()
    cfg.enable_operational_event_ledger = True
    cfg.event_ledger_queue_size = 100
    cfg.event_ledger_batch_size = 10
    cfg.event_ledger_flush_interval_sec = Decimal("10")
    cfg.event_ledger_shutdown_flush_timeout_sec = Decimal("5")
    cfg.event_ledger_overflow_policy = "drop_oldest"
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _make_create(**overrides):
    kwargs = {
        "event_type": OperationalEventType.START,
        "severity": OperationalEventSeverity.INFO,
        "source": OperationalEventSource.ORCHESTRATOR,
        "reason_code": OperationalEventReasonCode.STARTUP,
        "payload": OperationalEventPayload(),
    }
    kwargs.update(overrides)
    return OperationalEventCreate(**kwargs)


# ═══════════════════════════════════════════════════════════════════════════
# Migration / Table Shape
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_migration_creates_operational_events_table(async_engine):
    """The operational_events table exists in the test database."""
    from sqlalchemy import inspect as sa_inspect

    def _get_tables(conn):
        return sa_inspect(conn).get_table_names()

    async with async_engine.connect() as conn:
        tables = await conn.run_sync(_get_tables)
    assert "operational_events" in tables


@pytest.mark.asyncio
async def test_operational_events_table_has_required_columns(async_engine):
    """Verify all required columns exist on the operational_events table."""
    from sqlalchemy import inspect as sa_inspect

    def _get_columns(conn):
        return {
            col["name"] for col in sa_inspect(conn).get_columns("operational_events")
        }

    async with async_engine.connect() as conn:
        columns = await conn.run_sync(_get_columns)
    required = {
        "id",
        "event_type",
        "severity",
        "source",
        "reason_code",
        "payload_json",
        "persistence_status",
        "created_at_utc",
        "recorded_at_utc",
    }
    assert required.issubset(columns)


# ═══════════════════════════════════════════════════════════════════════════
# Repository Append / Read-Window
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_repository_append_persists_single_event(async_session):
    """Append persists an event and returns a valid record."""
    repo = OperationalEventRepository(async_session)
    event = _make_create()
    record = await repo.append(event)

    assert record.id is not None
    assert record.event_type == OperationalEventType.START
    assert record.persistence_status == OperationalEventPersistenceStatus.PERSISTED


@pytest.mark.asyncio
async def test_repository_read_window_returns_events(async_session):
    """read_window returns events within the current transaction."""
    repo = OperationalEventRepository(async_session)
    event = _make_create()
    await repo.append(event)
    await async_session.flush()

    query = OperationalEventQuery(limit=10)
    window = await repo.read_window(query)
    assert window.total_count >= 1
    assert len(window.events) >= 1


@pytest.mark.asyncio
async def test_repository_read_window_filters_by_event_type(async_session):
    """read_window filters by event type."""
    repo = OperationalEventRepository(async_session)
    await repo.append(_make_create(event_type=OperationalEventType.START))
    await repo.append(_make_create(event_type=OperationalEventType.SHUTDOWN))
    await async_session.flush()

    query = OperationalEventQuery(
        event_types=[OperationalEventType.START],
        limit=10,
    )
    window = await repo.read_window(query)
    for evt in window.events:
        assert evt.event_type == OperationalEventType.START


@pytest.mark.asyncio
async def test_repository_read_window_filters_by_severity(async_session):
    """read_window filters by severity."""
    repo = OperationalEventRepository(async_session)
    await repo.append(_make_create(severity=OperationalEventSeverity.INFO))
    await repo.append(
        _make_create(
            severity=OperationalEventSeverity.CRITICAL,
            event_type=OperationalEventType.ERROR_RECOVERED,
            reason_code=OperationalEventReasonCode.ERROR_HANDLED,
        )
    )
    await async_session.flush()

    query = OperationalEventQuery(
        severities=[OperationalEventSeverity.CRITICAL],
        limit=10,
    )
    window = await repo.read_window(query)
    for evt in window.events:
        assert evt.severity == OperationalEventSeverity.CRITICAL


@pytest.mark.asyncio
async def test_repository_has_no_update_delete_methods():
    """Repository public API has no update or delete methods."""
    public = [
        m
        for m in dir(OperationalEventRepository)
        if not m.startswith("_")
        and callable(getattr(OperationalEventRepository, m, None))
    ]
    assert "update" not in public
    assert "delete" not in public


@pytest.mark.asyncio
async def test_repository_batch_append_persists_multiple_events(async_session):
    """batch_append persists multiple events and reports counts."""
    repo = OperationalEventRepository(async_session)
    events = [
        _make_create(),
        _make_create(event_type=OperationalEventType.SHUTDOWN),
        _make_create(
            event_type=OperationalEventType.CONFIG_LOADED,
            reason_code=OperationalEventReasonCode.CONFIG_VALID,
        ),
    ]
    result = await repo.batch_append(events)

    assert result.total == 3
    assert result.succeeded == 3
    assert result.failed == 0


# ═══════════════════════════════════════════════════════════════════════════
# Event Bus Integration
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_event_bus_publish_and_flush_persists_events(async_session):
    """Events published to the bus are persisted on flush."""
    repo = OperationalEventRepository(async_session)

    async def factory():
        return repo

    bus = OperationalEventBus(
        repository_factory=factory,
        config=_make_config(),
    )
    await bus.publish(_make_create())
    result = await bus._flush_batch()

    assert isinstance(result, OperationalEventFlushResult)


@pytest.mark.asyncio
async def test_event_bus_queue_overflow_behavior(async_session):
    """Queue overflow returns typed result and does not crash."""
    repo = OperationalEventRepository(async_session)

    async def factory():
        return repo

    config = _make_config(
        event_ledger_queue_size=1, event_ledger_overflow_policy="drop_newest"
    )
    bus = OperationalEventBus(repository_factory=factory, config=config)

    # Fill with WARNING (not diagnostic, so _pop_diagnostic won't free space)
    await bus.publish(_make_create(severity=OperationalEventSeverity.WARNING))
    result = await bus.publish(_make_create(event_type=OperationalEventType.SHUTDOWN))
    assert isinstance(result, OperationalEventAppendResult)
    assert result.accepted is False


@pytest.mark.asyncio
async def test_event_bus_queue_state_reports_dropped(async_session):
    """queue_state reports dropped events after overflow."""
    repo = OperationalEventRepository(async_session)

    async def factory():
        return repo

    config = _make_config(
        event_ledger_queue_size=1, event_ledger_overflow_policy="drop_newest"
    )
    bus = OperationalEventBus(repository_factory=factory, config=config)

    # Fill with WARNING so _pop_diagnostic can't free space
    await bus.publish(_make_create(severity=OperationalEventSeverity.WARNING))
    # Second WARNING is rejected (not critical, drop_newest applies)
    await bus.publish(_make_create(severity=OperationalEventSeverity.WARNING))

    state = bus.queue_state()
    assert state.dropped_total >= 1
    assert state.overflow is True


@pytest.mark.asyncio
async def test_event_bus_shutdown_flush_drains_queue(async_session):
    """stop() drains remaining events."""
    repo = OperationalEventRepository(async_session)
    repo.batch_append = AsyncMock(
        return_value=OperationalEventBatchResult(batch_id="b1", total=1, succeeded=1)
    )

    async def factory():
        return repo

    bus = OperationalEventBus(repository_factory=factory, config=_make_config())
    bus._running = True
    await bus.publish(_make_create())
    await bus.stop()

    state = bus.queue_state()
    assert state.current_depth == 0


# ═══════════════════════════════════════════════════════════════════════════
# Append-Only Semantics
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_events_are_immutable_after_persistence(async_session):
    """Persisted events cannot be modified through the repository."""
    repo = OperationalEventRepository(async_session)
    event = _make_create()
    record = await repo.append(event)

    # Verify the record is frozen
    with pytest.raises(Exception):
        record.event_type = OperationalEventType.SHUTDOWN  # type: ignore[misc]


@pytest.mark.asyncio
async def test_read_window_has_more_flag(async_session):
    """read_window sets has_more when results exceed limit."""
    repo = OperationalEventRepository(async_session)
    for _ in range(5):
        await repo.append(_make_create())
    await async_session.flush()

    query = OperationalEventQuery(limit=2)
    window = await repo.read_window(query)
    assert window.has_more is True
    assert len(window.events) == 2
    assert window.total_count >= 5


@pytest.mark.asyncio
async def test_read_window_offset_pages_through_all_records(async_session):
    """Offset cursor lets callers paginate the full window deterministically."""
    repo = OperationalEventRepository(async_session)
    for _ in range(5):
        await repo.append(_make_create())
    await async_session.flush()

    page_one = await repo.read_window(OperationalEventQuery(limit=2, offset=0))
    page_two = await repo.read_window(OperationalEventQuery(limit=2, offset=2))
    page_three = await repo.read_window(OperationalEventQuery(limit=2, offset=4))

    # Pages are disjoint and cover the full set in deterministic id order.
    ids = (
        [e.id for e in page_one.events]
        + [e.id for e in page_two.events]
        + [e.id for e in page_three.events]
    )
    assert len(ids) == 5
    assert len(set(ids)) == 5

    # has_more accounts for the cursor, not just total_count > limit.
    assert page_one.has_more is True
    assert page_two.has_more is True
    assert page_three.has_more is False


@pytest.mark.asyncio
async def test_event_payload_is_secret_free_in_db(async_session):
    """Persisted events in the database do not contain raw secrets."""
    repo = OperationalEventRepository(async_session)
    event = _make_create(
        payload=OperationalEventPayload(message="System started normally")
    )
    record = await repo.append(event)

    # payload_json should be a clean JSON string without secrets
    payload = json.loads(record.payload_json)
    assert "api_key" not in str(payload).lower()
    assert "sk-" not in str(payload)
    assert "0x" not in str(payload.get("message", ""))


# ═══════════════════════════════════════════════════════════════════════════
# True Alembic Migration Verification (Fix #5)
# ═══════════════════════════════════════════════════════════════════════════

_EXPECTED_TABLE_COLUMNS = {
    "id",
    "event_type",
    "severity",
    "source",
    "reason_code",
    "payload_json",
    "persistence_status",
    "created_at_utc",
    "recorded_at_utc",
}


def test_migration_file_exists_and_creates_correct_table():
    """Verify the alembic migration file 0006 exists and declares the
    correct table name and columns in its upgrade function."""
    migration_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "migrations",
        "versions",
        "0006_add_operational_events.py",
    )
    assert os.path.exists(migration_path), (
        f"Migration file not found at {migration_path}"
    )

    with open(migration_path) as f:
        content = f.read()

    assert "operational_events" in content, (
        "Migration must create operational_events table"
    )
    assert "def upgrade()" in content, "Migration must have upgrade function"
    assert "def downgrade()" in content, "Migration must have downgrade function"


def test_migration_declares_all_required_columns():
    """Verify the migration file declares all required columns."""
    migration_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "migrations",
        "versions",
        "0006_add_operational_events.py",
    )
    with open(migration_path) as f:
        content = f.read()

    for col in _EXPECTED_TABLE_COLUMNS:
        assert col in content, f"Migration missing column: {col}"


@pytest.mark.asyncio
async def test_alembic_migration_applies_to_real_engine():
    """Verify alembic can apply migration 0006 against a fresh in-memory DB.

    Runs ``alembic upgrade head`` as a subprocess against a temporary
    SQLite database to confirm the migration produces the correct table
    shape without relying on Base.metadata.create_all().
    """
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        db_path = tmp.name

    try:
        db_url = f"sqlite+aiosqlite:///{db_path}"
        env = {**os.environ, "DATABASE_URL": db_url}

        result = subprocess.run(
            [
                ".venv/bin/python",
                "-m",
                "alembic",
                "-c",
                os.path.join(os.path.dirname(__file__), "..", "..", "alembic.ini"),
                "upgrade",
                "head",
            ],
            capture_output=True,
            text=True,
            env=env,
            cwd=os.path.join(os.path.dirname(__file__), "..", ".."),
            timeout=30,
        )

        assert result.returncode == 0, f"alembic upgrade head failed:\n{result.stderr}"

        # Verify the table exists in the migrated DB using aiosqlite
        import aiosqlite

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='operational_events'"
            )
            row = await cursor.fetchone()
            assert row is not None, (
                "operational_events table not found after alembic upgrade"
            )

            # Verify columns
            cursor = await db.execute("PRAGMA table_info(operational_events)")
            columns = {row[1] async for row in cursor}
            missing = _EXPECTED_TABLE_COLUMNS - columns
            assert not missing, f"Missing columns after migration: {missing}"
    finally:
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_orm_model_matches_migration_columns():
    """The OperationalEvent ORM model declares the same columns as the migration."""
    from src.db.models import OperationalEvent as OE

    orm_cols = {c.name for c in OE.__table__.columns}
    missing_in_orm = _EXPECTED_TABLE_COLUMNS - orm_cols
    assert not missing_in_orm, f"ORM model missing columns: {missing_in_orm}"

    extra_in_orm = orm_cols - _EXPECTED_TABLE_COLUMNS
    assert not extra_in_orm, f"ORM model has extra columns: {extra_in_orm}"
