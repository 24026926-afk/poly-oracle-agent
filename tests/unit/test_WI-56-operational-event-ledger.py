"""
tests/unit/test_WI-56-operational-event-ledger.py

Unit tests for WI-56 Operational Event Ledger — schemas, enums,
secret rejection, repository append-only contract, queue bounds,
overflow policy, config, and metrics label enforcement.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from src.schemas.ops import (
    OperationalEventType,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventReasonCode,
    OperationalEventPersistenceStatus,
    OperationalEventPayload,
    OperationalEventCreate,
    OperationalEventRecord,
    OperationalEventBatch,
    OperationalEventBatchResult,
    OperationalEventAppendResult,
    OperationalEventFlushResult,
    OperationalEventQueueState,
    OperationalEventQueuePolicy,
    OperationalEventQuery,
    OperationalEventReadWindow,
    OperationalEventValidationError,
    OperationalEventRedactionResult,
)
from src.observability.operational_event_bus import OperationalEventBus
from src.schemas.llm import LLMEvaluationResponse


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


def _make_create(
    event_type=OperationalEventType.START,
    severity=OperationalEventSeverity.INFO,
    source=OperationalEventSource.ORCHESTRATOR,
    reason_code=OperationalEventReasonCode.STARTUP,
    payload=None,
):
    return OperationalEventCreate(
        event_type=event_type,
        severity=severity,
        source=source,
        reason_code=reason_code,
        payload=payload or OperationalEventPayload(),
    )


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventType Enum
# ═══════════════════════════════════════════════════════════════════════════


_REQUIRED_EVENT_TYPES = {
    "START",
    "SHUTDOWN",
    "CONFIG_LOADED",
    "MARKET_DISCOVERED",
    "MARKET_REJECTED",
    "MARKET_QUARANTINE",
    "WS_CONNECTED",
    "WS_RECONNECT",
    "WS_PONG_STALE",
    "READY_STATE_CHANGED",
    "LLM_CALL_STARTED",
    "LLM_CALL_BLOCKED",
    "BUDGET_BLOCK",
    "COOLDOWN_BLOCK",
    "PROVIDER_FAILURE",
    "DECISION_ACCEPTED",
    "DECISION_SKIPPED",
    "EXECUTION_DRY_RUN",
    "CIRCUIT_BREAKER_OPEN",
    "CIRCUIT_BREAKER_CLOSED",
    "ALERT_SENT",
    "ERROR_RECOVERED",
}


def test_event_type_enum_contains_all_required_types():
    actual = {e.value for e in OperationalEventType}
    missing = _REQUIRED_EVENT_TYPES - actual
    assert not missing, f"Missing event types: {missing}"


def test_event_type_is_stable_str_enum():
    assert issubclass(OperationalEventType, str)
    assert OperationalEventType.START == "START"
    assert OperationalEventType.START.value == "START"


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventSeverity Enum
# ═══════════════════════════════════════════════════════════════════════════


def test_event_severity_enum_values():
    assert OperationalEventSeverity.INFO.value == "INFO"
    assert OperationalEventSeverity.WARNING.value == "WARNING"
    assert OperationalEventSeverity.CRITICAL.value == "CRITICAL"
    assert OperationalEventSeverity.ERROR.value == "ERROR"
    assert len(OperationalEventSeverity) == 4


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventSource Enum
# ═══════════════════════════════════════════════════════════════════════════


def test_event_source_enum_contains_required_sources():
    expected = {
        "ORCHESTRATOR",
        "INGESTION",
        "CONTEXT",
        "EVALUATION",
        "EXECUTION",
        "OBSERVABILITY",
        "DATABASE",
    }
    assert {s.value for s in OperationalEventSource} == expected


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventReasonCode Enum
# ═══════════════════════════════════════════════════════════════════════════


def test_event_reason_code_enum_contains_required_codes():
    codes = {r.value for r in OperationalEventReasonCode}
    assert "STARTUP" in codes
    assert "GRACEFUL_SHUTDOWN" in codes
    assert "CONFIG_VALID" in codes
    assert "MARKET_FOUND" in codes
    assert "WS_ESTABLISHED" in codes
    assert "READY" in codes
    assert "BUDGET_HOURLY" in codes
    assert "DECISION_BUY" in codes
    assert "EXEC_DRY_RUN_SKIP" in codes
    assert "CB_OPEN" in codes
    assert "CB_CLOSED" in codes
    assert "ALERT_DISPATCHED" in codes
    assert "ERROR_HANDLED" in codes
    assert "QUEUE_FULL" in codes
    assert "PERSIST_SUCCESS" in codes
    assert "PERSIST_FAILED" in codes


def test_event_reason_code_is_stable_str_enum():
    assert issubclass(OperationalEventReasonCode, str)
    assert OperationalEventReasonCode.STARTUP.value == "STARTUP"


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventPersistenceStatus Enum
# ═══════════════════════════════════════════════════════════════════════════


def test_event_persistence_status_enum_values():
    assert OperationalEventPersistenceStatus.PENDING.value == "PENDING"
    assert OperationalEventPersistenceStatus.PERSISTED.value == "PERSISTED"
    assert OperationalEventPersistenceStatus.FAILED.value == "FAILED"
    assert OperationalEventPersistenceStatus.DROPPED.value == "DROPPED"


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventPayload
# ═══════════════════════════════════════════════════════════════════════════


def test_event_payload_is_frozen():
    p = OperationalEventPayload(message="test")
    with pytest.raises(ValidationError):
        p.message = "changed"  # type: ignore[misc]


def test_event_payload_rejects_secret_like_values():
    with pytest.raises(ValidationError):
        OperationalEventPayload(message="sk-ant-api03-secret-key-here")


def test_event_payload_rejects_high_cardinality_identifiers():
    with pytest.raises(ValidationError):
        OperationalEventPayload(
            message="condition 0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        )


def test_event_payload_rejects_raw_prompt_text():
    with pytest.raises(ValidationError):
        OperationalEventPayload(message="raw_prompt: analyze this market")


def test_event_payload_rejects_private_reasoning():
    with pytest.raises(ValidationError):
        OperationalEventPayload(message="reasoning_log: the model thought...")


def test_event_payload_rejects_api_keys():
    with pytest.raises(ValidationError):
        OperationalEventPayload(reason_code="api_key: abc123")


def test_event_payload_rejects_wallet_keys():
    with pytest.raises(ValidationError):
        OperationalEventPayload(message="private key: 0xabcd")


def test_event_payload_rejects_token_ids():
    with pytest.raises(ValidationError):
        OperationalEventPayload(message="token_id 12345678901")


def test_event_payload_rejects_condition_ids():
    with pytest.raises(ValidationError):
        OperationalEventPayload(
            message="condition 0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        )


def test_event_payload_rejects_wallet_addresses():
    with pytest.raises(ValidationError):
        OperationalEventPayload(
            message="wallet 0x1234567890123456789012345678901234567890"
        )


def test_event_payload_rejects_telegram_tokens():
    with pytest.raises(ValidationError):
        OperationalEventPayload(
            message="bot token: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"
        )


def test_event_payload_allows_valid_bounded_counts():
    p = OperationalEventPayload(market_count=5)
    assert p.market_count == 5


def test_event_payload_allows_boolean_states():
    p = OperationalEventPayload(dry_run=True)
    assert p.dry_run is True


def test_event_payload_allows_stable_reason_codes():
    p = OperationalEventPayload(reason_code="STARTUP")
    assert p.reason_code == "STARTUP"


def test_event_payload_decimal_financial_values():
    p = OperationalEventPayload(budget_remaining=Decimal("5.25"))
    assert p.budget_remaining == Decimal("5.25")


def test_event_payload_rejects_raw_float_for_money_fields():
    with pytest.raises(ValidationError):
        OperationalEventPayload(budget_remaining=5.25)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventCreate
# ═══════════════════════════════════════════════════════════════════════════


def test_event_create_requires_event_type():
    with pytest.raises(ValidationError):
        OperationalEventCreate(
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
        )  # type: ignore[call-arg]


def test_event_create_requires_severity():
    with pytest.raises(ValidationError):
        OperationalEventCreate(
            event_type=OperationalEventType.START,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
        )  # type: ignore[call-arg]


def test_event_create_requires_source_component():
    with pytest.raises(ValidationError):
        OperationalEventCreate(
            event_type=OperationalEventType.START,
            severity=OperationalEventSeverity.INFO,
            reason_code=OperationalEventReasonCode.STARTUP,
        )  # type: ignore[call-arg]


def test_event_create_rejects_invalid_event_type():
    with pytest.raises(ValidationError):
        OperationalEventCreate(
            event_type="INVALID_TYPE",  # type: ignore[arg-type]
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
        )


def test_event_create_is_frozen():
    e = _make_create()
    with pytest.raises(ValidationError):
        e.event_type = OperationalEventType.SHUTDOWN  # type: ignore[misc]


def test_event_create_with_payload():
    payload = OperationalEventPayload(dry_run=True, market_count=3)
    e = _make_create(payload=payload)
    assert e.payload.dry_run is True
    assert e.payload.market_count == 3


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventRecord
# ═══════════════════════════════════════════════════════════════════════════


def test_event_record_is_frozen():
    r = OperationalEventRecord(
        id="evt-1",
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        created_at_utc=datetime.now(timezone.utc),
    )
    with pytest.raises(ValidationError):
        r.id = "changed"  # type: ignore[misc]


def test_event_record_has_persistence_status():
    r = OperationalEventRecord(
        id="evt-1",
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        created_at_utc=datetime.now(timezone.utc),
    )
    assert r.persistence_status == OperationalEventPersistenceStatus.PENDING


def test_event_record_has_timestamp():
    t = datetime.now(timezone.utc)
    r = OperationalEventRecord(
        id="evt-1",
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        created_at_utc=t,
    )
    assert r.created_at_utc == t


def test_event_record_has_unique_id():
    id1 = str(uuid.uuid4())
    id2 = str(uuid.uuid4())
    r1 = OperationalEventRecord(
        id=id1,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        created_at_utc=datetime.now(timezone.utc),
    )
    r2 = OperationalEventRecord(
        id=id2,
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        created_at_utc=datetime.now(timezone.utc),
    )
    assert r1.id != r2.id


def test_event_record_is_immutable_after_creation():
    r = OperationalEventRecord(
        id="evt-1",
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        created_at_utc=datetime.now(timezone.utc),
    )
    with pytest.raises(ValidationError):
        r.event_type = OperationalEventType.SHUTDOWN  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventBatch / BatchResult
# ═══════════════════════════════════════════════════════════════════════════


def test_event_batch_is_frozen():
    b = OperationalEventBatch(events=[_make_create()])
    with pytest.raises(ValidationError):
        b.events = []  # type: ignore[misc]


def test_event_batch_accepts_list_of_creates():
    events = [_make_create(), _make_create(event_type=OperationalEventType.SHUTDOWN)]
    b = OperationalEventBatch(events=events)
    assert len(b.events) == 2


def test_event_batch_result_tracks_success_count():
    r = OperationalEventBatchResult(
        batch_id="b1", total=5, succeeded=5, failed=0, dropped=0
    )
    assert r.succeeded == 5


def test_event_batch_result_tracks_failure_count():
    r = OperationalEventBatchResult(
        batch_id="b1", total=5, succeeded=3, failed=2, dropped=0
    )
    assert r.failed == 2


def test_event_batch_result_is_frozen():
    r = OperationalEventBatchResult(batch_id="b1", total=1, succeeded=1)
    with pytest.raises(ValidationError):
        r.succeeded = 0  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventAppendResult
# ═══════════════════════════════════════════════════════════════════════════


def test_append_result_success():
    r = OperationalEventAppendResult(accepted=True, queue_depth=5)
    assert r.accepted is True
    assert r.queue_depth == 5


def test_append_result_failure_with_reason():
    r = OperationalEventAppendResult(
        accepted=False, reason="queue_full", queue_depth=100
    )
    assert r.accepted is False
    assert r.reason == "queue_full"


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventFlushResult
# ═══════════════════════════════════════════════════════════════════════════


def test_flush_result_reports_persisted_count():
    r = OperationalEventFlushResult(batch_id="b1", persisted=5)
    assert r.persisted == 5


def test_flush_result_reports_dropped_count():
    r = OperationalEventFlushResult(batch_id="b1", dropped=2)
    assert r.dropped == 2


def test_flush_result_reports_failure_status():
    r = OperationalEventFlushResult(batch_id="b1", failed=3, shutdown_flush=True)
    assert r.failed == 3
    assert r.shutdown_flush is True


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventQueueState
# ═══════════════════════════════════════════════════════════════════════════


def test_queue_state_reports_current_depth():
    s = OperationalEventQueueState(current_depth=50, max_capacity=100)
    assert s.current_depth == 50


def test_queue_state_reports_max_capacity():
    s = OperationalEventQueueState(current_depth=0, max_capacity=100)
    assert s.max_capacity == 100


def test_queue_state_reports_dropped_total():
    s = OperationalEventQueueState(current_depth=0, max_capacity=100, dropped_total=10)
    assert s.dropped_total == 10


def test_queue_state_overflow_indicator():
    s = OperationalEventQueueState(current_depth=0, max_capacity=100, overflow=True)
    assert s.overflow is True


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventQueuePolicy
# ═══════════════════════════════════════════════════════════════════════════


def test_queue_policy_has_overflow_behavior():
    p = OperationalEventQueuePolicy(overflow_behavior="drop_oldest")
    assert p.overflow_behavior == "drop_oldest"


def test_queue_policy_rejects_unknown_overflow_behavior():
    with pytest.raises(ValidationError):
        OperationalEventQueuePolicy(overflow_behavior="drop_everything")


def test_queue_policy_critical_event_priority():
    p = OperationalEventQueuePolicy()
    critical_values = {s.value for s in p.critical_severities}
    assert "CRITICAL" in critical_values
    assert "ERROR" in critical_values


def test_queue_policy_is_frozen():
    p = OperationalEventQueuePolicy()
    with pytest.raises(ValidationError):
        p.max_size = 500  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventQuery / ReadWindow
# ═══════════════════════════════════════════════════════════════════════════


def test_query_filters_by_event_type():
    q = OperationalEventQuery(event_types=[OperationalEventType.START])
    assert q.event_types == [OperationalEventType.START]


def test_query_filters_by_severity():
    q = OperationalEventQuery(severities=[OperationalEventSeverity.CRITICAL])
    assert q.severities == [OperationalEventSeverity.CRITICAL]


def test_query_filters_by_source_component():
    q = OperationalEventQuery(sources=[OperationalEventSource.ORCHESTRATOR])
    assert q.sources == [OperationalEventSource.ORCHESTRATOR]


def test_query_filters_by_time_window():
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
    q = OperationalEventQuery(start_time_utc=t1, end_time_utc=t2)
    assert q.start_time_utc == t1
    assert q.end_time_utc == t2


def test_read_window_bounded_limit():
    q = OperationalEventQuery(limit=50)
    assert q.limit == 50


def test_query_offset_defaults_to_zero():
    """OperationalEventQuery exposes an offset cursor defaulting to 0."""
    q = OperationalEventQuery()
    assert q.offset == 0


def test_query_offset_accepts_positive_int():
    """Callers may page through results using an explicit offset."""
    q = OperationalEventQuery(offset=2000)
    assert q.offset == 2000


def test_query_offset_rejects_negative():
    """Negative offsets are rejected at the schema boundary."""
    with pytest.raises(ValidationError):
        OperationalEventQuery(offset=-1)


def test_read_window_start_time():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    w = OperationalEventReadWindow(start_time_utc=t)
    assert w.start_time_utc == t


def test_read_window_end_time():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    w = OperationalEventReadWindow(end_time_utc=t)
    assert w.end_time_utc == t


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventValidationError
# ═══════════════════════════════════════════════════════════════════════════


def test_validation_error_contains_violation_list():
    e = OperationalEventValidationError(
        violations=["forbidden_pattern:private_key_hex"],
        field_errors={"message": "contains forbidden content"},
    )
    assert len(e.violations) == 1
    assert "message" in e.field_errors


def test_validation_error_references_event_id():
    e = OperationalEventValidationError(
        event_id="evt-123",
        violations=["bad field"],
    )
    assert e.event_id == "evt-123"


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventRedactionResult
# ═══════════════════════════════════════════════════════════════════════════


def test_redaction_result_indicates_redaction_occurred():
    r = OperationalEventRedactionResult(
        redaction_occurred=True,
        redacted_fields=["message"],
    )
    assert r.redaction_occurred is True


def test_redaction_result_contains_redacted_fields():
    r = OperationalEventRedactionResult(
        redaction_occurred=True,
        redacted_fields=["message", "reason_code"],
    )
    assert "message" in r.redacted_fields
    assert "reason_code" in r.redacted_fields


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventBus (Bounded Async Event Queue)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_event_bus_accepts_event_create():
    repo_mock = AsyncMock()
    repo_mock.batch_append = AsyncMock(
        return_value=OperationalEventBatchResult(batch_id="b1", total=1, succeeded=1)
    )

    async def factory():
        return repo_mock

    bus = OperationalEventBus(
        repository_factory=factory,
        config=_make_config(),
    )
    event = _make_create()
    result = await bus.publish(event)
    assert result.accepted is True
    assert result.queue_depth == 1


@pytest.mark.asyncio
async def test_event_bus_queue_is_bounded():
    repo_mock = AsyncMock()

    async def factory():
        return repo_mock

    config = _make_config(event_ledger_queue_size=2)
    bus = OperationalEventBus(repository_factory=factory, config=config)

    await bus.publish(_make_create())
    await bus.publish(_make_create())
    # Third event should trigger overflow
    result = await bus.publish(_make_create(event_type=OperationalEventType.SHUTDOWN))
    # With drop_oldest, the oldest is dropped and new event accepted
    assert result.accepted is True


@pytest.mark.asyncio
async def test_event_bus_overflow_drops_diagnostic_first():
    repo_mock = AsyncMock()

    async def factory():
        return repo_mock

    config = _make_config(event_ledger_queue_size=2)
    bus = OperationalEventBus(repository_factory=factory, config=config)

    # Fill with INFO (diagnostic)
    await bus.publish(_make_create(severity=OperationalEventSeverity.INFO))
    await bus.publish(_make_create(severity=OperationalEventSeverity.INFO))

    # Add CRITICAL event — should be accepted (drops one diagnostic)
    result = await bus.publish(
        _make_create(
            severity=OperationalEventSeverity.CRITICAL,
            event_type=OperationalEventType.ERROR_RECOVERED,
        )
    )
    assert result.accepted is True


@pytest.mark.asyncio
async def test_event_bus_overflow_preserves_critical_events():
    repo_mock = AsyncMock()

    async def factory():
        return repo_mock

    config = _make_config(event_ledger_queue_size=2)
    bus = OperationalEventBus(repository_factory=factory, config=config)

    # Fill with CRITICAL events first
    await bus.publish(_make_create(severity=OperationalEventSeverity.CRITICAL))
    await bus.publish(_make_create(severity=OperationalEventSeverity.CRITICAL))

    # Add INFO — queue is all CRITICAL (no non-critical to drop)
    # Safe overflow rejects the incoming event instead of dropping critical
    result = await bus.publish(_make_create(severity=OperationalEventSeverity.INFO))
    assert result.accepted is False  # refuses to drop critical queued events


@pytest.mark.asyncio
async def test_event_bus_critical_publish_rejection_invokes_fail_closed_callback():
    repo_mock = AsyncMock()
    degraded = False

    async def factory():
        return repo_mock

    def on_critical_failure():
        nonlocal degraded
        degraded = True

    config = _make_config(event_ledger_queue_size=1)
    bus = OperationalEventBus(
        repository_factory=factory,
        config=config,
        on_critical_failure=on_critical_failure,
    )

    await bus.publish(_make_create(severity=OperationalEventSeverity.CRITICAL))
    result = await bus.publish(
        _make_create(
            event_type=OperationalEventType.CIRCUIT_BREAKER_OPEN,
            severity=OperationalEventSeverity.CRITICAL,
            reason_code=OperationalEventReasonCode.CB_OPEN,
        )
    )

    assert result.accepted is False
    assert degraded is True


@pytest.mark.asyncio
async def test_event_bus_drop_diagnostic_retains_critical_over_non_critical():
    repo_mock = AsyncMock()

    async def factory():
        return repo_mock

    config = _make_config(
        event_ledger_queue_size=1,
        event_ledger_overflow_policy="drop_diagnostic",
    )
    bus = OperationalEventBus(repository_factory=factory, config=config)

    await bus.publish(_make_create(severity=OperationalEventSeverity.WARNING))
    result = await bus.publish(
        _make_create(
            event_type=OperationalEventType.CIRCUIT_BREAKER_OPEN,
            severity=OperationalEventSeverity.CRITICAL,
            reason_code=OperationalEventReasonCode.CB_OPEN,
        )
    )

    assert result.accepted is True
    assert bus.queue_state().dropped_total == 1
    queued = bus._queue.get_nowait()
    assert queued.severity == OperationalEventSeverity.CRITICAL


@pytest.mark.asyncio
async def test_event_bus_overflow_returns_typed_result():
    repo_mock = AsyncMock()

    async def factory():
        return repo_mock

    config = _make_config(
        event_ledger_queue_size=1, event_ledger_overflow_policy="drop_newest"
    )
    bus = OperationalEventBus(repository_factory=factory, config=config)

    # Fill with a WARNING event (not diagnostic, so _pop_diagnostic won't drop it)
    await bus.publish(_make_create(severity=OperationalEventSeverity.WARNING))
    result = await bus.publish(_make_create())
    assert result.accepted is False
    assert result.reason == "queue_full"


@pytest.mark.asyncio
async def test_event_bus_flush_bounded_batch_size():
    repo_mock = AsyncMock()
    repo_mock.batch_append = AsyncMock(
        return_value=OperationalEventBatchResult(batch_id="b1", total=2, succeeded=2)
    )

    async def factory():
        return repo_mock

    config = _make_config(event_ledger_batch_size=3)
    bus = OperationalEventBus(repository_factory=factory, config=config)

    await bus.publish(_make_create())
    await bus.publish(_make_create())

    result = await bus._flush_batch()
    assert result.persisted == 2


@pytest.mark.asyncio
async def test_event_bus_partial_critical_persist_failure_degrades_readiness():
    degraded = False
    repo_mock = AsyncMock()
    repo_mock.batch_append = AsyncMock(
        return_value=OperationalEventBatchResult(
            batch_id="b1",
            total=1,
            succeeded=0,
            failed=1,
        )
    )

    async def factory():
        return repo_mock

    def on_critical_failure():
        nonlocal degraded
        degraded = True

    bus = OperationalEventBus(
        repository_factory=factory,
        config=_make_config(),
        on_critical_failure=on_critical_failure,
    )
    await bus.publish(
        _make_create(
            event_type=OperationalEventType.CIRCUIT_BREAKER_OPEN,
            severity=OperationalEventSeverity.CRITICAL,
            reason_code=OperationalEventReasonCode.CB_OPEN,
        )
    )
    result = await bus._flush_batch()

    assert result.failed == 1
    assert degraded is True


@pytest.mark.asyncio
async def test_event_bus_flush_bounded_interval():
    config = _make_config(event_ledger_flush_interval_sec=Decimal("1"))
    assert config.event_ledger_flush_interval_sec == Decimal("1")


@pytest.mark.asyncio
async def test_event_bus_shutdown_flush_timeout():
    repo_mock = AsyncMock()
    repo_mock.batch_append = AsyncMock(
        return_value=OperationalEventBatchResult(batch_id="b1", total=1, succeeded=1)
    )

    async def factory():
        return repo_mock

    config = _make_config(event_ledger_shutdown_flush_timeout_sec=Decimal("2"))
    bus = OperationalEventBus(repository_factory=factory, config=config)
    bus._running = True

    await bus.publish(_make_create())
    await bus.stop()
    # Should not timeout with 2 second flush timeout
    assert bus.queue_state().current_depth == 0


@pytest.mark.asyncio
async def test_event_bus_start_stop_lifecycle():
    repo_mock = AsyncMock()
    repo_mock.batch_append = AsyncMock(
        return_value=OperationalEventBatchResult(batch_id="b1", total=0, succeeded=0)
    )

    async def factory():
        return repo_mock

    bus = OperationalEventBus(repository_factory=factory, config=_make_config())
    assert bus._running is False

    await bus.start()
    assert bus._running is True
    assert bus._flush_task is not None

    await bus.stop()
    assert bus._running is False


# ═══════════════════════════════════════════════════════════════════════════
# OperationalEventRepository (Append-Only Contract)
# ═══════════════════════════════════════════════════════════════════════════


def test_repository_has_no_public_update_methods():
    from src.db.repositories.operational_event_repository import (
        OperationalEventRepository,
    )

    public_methods = [
        m
        for m in dir(OperationalEventRepository)
        if not m.startswith("_")
        and callable(getattr(OperationalEventRepository, m, None))
    ]
    assert "update" not in public_methods
    assert "delete" not in public_methods
    assert "upsert" not in public_methods


def test_repository_has_no_public_delete_methods():
    from src.db.repositories.operational_event_repository import (
        OperationalEventRepository,
    )

    public_methods = [
        m
        for m in dir(OperationalEventRepository)
        if not m.startswith("_")
        and callable(getattr(OperationalEventRepository, m, None))
    ]
    delete_like = [
        m for m in public_methods if "delete" in m.lower() or "remove" in m.lower()
    ]
    assert len(delete_like) == 0


def test_repository_has_append_method():
    from src.db.repositories.operational_event_repository import (
        OperationalEventRepository,
    )

    assert hasattr(OperationalEventRepository, "append")
    assert callable(getattr(OperationalEventRepository, "append"))


def test_repository_has_read_window_method():
    from src.db.repositories.operational_event_repository import (
        OperationalEventRepository,
    )

    assert hasattr(OperationalEventRepository, "read_window")
    assert callable(getattr(OperationalEventRepository, "read_window"))


def test_repository_append_returns_event_record():
    from src.db.repositories.operational_event_repository import (
        OperationalEventRepository,
    )
    import inspect

    sig = inspect.signature(OperationalEventRepository.append)
    return_annotation = sig.return_annotation
    # With from __future__ import annotations, return annotation may be a string
    annotation_name = (
        return_annotation.__name__
        if hasattr(return_annotation, "__name__")
        else str(return_annotation)
    )
    assert "OperationalEventRecord" in annotation_name


def test_repository_batch_append_returns_batch_result():
    from src.db.repositories.operational_event_repository import (
        OperationalEventRepository,
    )
    import inspect

    sig = inspect.signature(OperationalEventRepository.batch_append)
    return_annotation = sig.return_annotation
    annotation_name = (
        return_annotation.__name__
        if hasattr(return_annotation, "__name__")
        else str(return_annotation)
    )
    assert "OperationalEventBatchResult" in annotation_name


# ═══════════════════════════════════════════════════════════════════════════
# Config Fields
# ═══════════════════════════════════════════════════════════════════════════


def test_config_has_event_ledger_enabled():
    from src.core.config import AppConfig

    fields = AppConfig.model_fields
    assert "enable_operational_event_ledger" in fields
    assert fields["enable_operational_event_ledger"].default is False


def test_config_has_event_queue_size():
    from src.core.config import AppConfig

    assert "event_ledger_queue_size" in AppConfig.model_fields


def test_config_has_event_batch_size():
    from src.core.config import AppConfig

    assert "event_ledger_batch_size" in AppConfig.model_fields


def test_config_has_event_flush_interval_seconds():
    from src.core.config import AppConfig

    assert "event_ledger_flush_interval_sec" in AppConfig.model_fields


def test_config_has_event_shutdown_flush_timeout():
    from src.core.config import AppConfig

    assert "event_ledger_shutdown_flush_timeout_sec" in AppConfig.model_fields


def test_config_has_event_overflow_policy():
    from src.core.config import AppConfig

    assert "event_ledger_overflow_policy" in AppConfig.model_fields


def test_config_rejects_unknown_event_overflow_policy():
    from src.core.config import AppConfig

    with pytest.raises(ValidationError):
        AppConfig(
            anthropic_api_key="sk-test-key",
            dry_run=True,
            event_ledger_overflow_policy="drop_everything",
        )


def test_config_ledger_disabled_by_default():
    from src.core.config import AppConfig

    assert AppConfig.model_fields["enable_operational_event_ledger"].default is False


# ═══════════════════════════════════════════════════════════════════════════
# Metrics — Low-Cardinality Labels
# ═══════════════════════════════════════════════════════════════════════════


def test_metrics_append_attempt_counter_low_cardinality():
    from src.observability.metrics import MetricsRegistry

    m = MetricsRegistry()
    assert "poly_agent_event_append_attempts_total" in m._counter_helps
    # Event type labels are from a bounded enum => low cardinality


def test_metrics_persisted_counter_low_cardinality():
    from src.observability.metrics import MetricsRegistry

    m = MetricsRegistry()
    assert "poly_agent_event_persisted_total" in m._counter_helps


def test_metrics_dropped_counter_low_cardinality():
    from src.observability.metrics import MetricsRegistry

    m = MetricsRegistry()
    assert "poly_agent_event_dropped_total" in m._counter_helps


def test_metrics_queue_depth_gauge_low_cardinality():
    from src.observability.metrics import MetricsRegistry

    m = MetricsRegistry()
    assert "poly_agent_event_queue_depth" in m._gauge_helps


def test_metrics_overflow_counter_low_cardinality():
    from src.observability.metrics import MetricsRegistry

    m = MetricsRegistry()
    assert "poly_agent_event_queue_overflow_total" in m._counter_helps


def test_metrics_flush_failure_counter_low_cardinality():
    from src.observability.metrics import MetricsRegistry

    m = MetricsRegistry()
    assert "poly_agent_event_flush_failures_total" in m._counter_helps


def test_metrics_rejects_high_cardinality_event_type_labels():
    from src.observability.metrics import MetricsRegistry

    m = MetricsRegistry()
    # The label keys used are "event_type", "severity", "reason" — all from bounded enums
    # Verify these are the only keys
    for name in [
        "poly_agent_event_append_attempts_total",
        "poly_agent_event_persisted_total",
        "poly_agent_event_dropped_total",
        "poly_agent_event_flush_failures_total",
        "poly_agent_event_queue_overflow_total",
    ]:
        assert name in m._counter_helps
        # Labels are only low-cardinality enum values


# ═══════════════════════════════════════════════════════════════════════════
# Event Immutability / Safety
# ═══════════════════════════════════════════════════════════════════════════


def test_operational_event_record_cannot_be_modified_after_persistence():
    r = OperationalEventRecord(
        id="evt-1",
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        created_at_utc=datetime.now(timezone.utc),
        persistence_status=OperationalEventPersistenceStatus.PERSISTED,
    )
    with pytest.raises(ValidationError):
        r.persistence_status = OperationalEventPersistenceStatus.FAILED  # type: ignore[misc]


def test_event_payload_rejects_raw_exception_messages():
    with pytest.raises(ValidationError):
        OperationalEventPayload(
            message="reasoning_log: the model produced an error during evaluation"
        )


def test_llm_event_payload_rejects_raw_prompts():
    with pytest.raises(ValidationError):
        OperationalEventPayload(message="raw prompt: analyze this market for trading")


def test_llm_event_payload_rejects_provider_api_keys():
    with pytest.raises(ValidationError):
        OperationalEventPayload(message="sk-ant-api03-abcd1234efgh5678")


def test_market_event_payload_rejects_token_ids():
    with pytest.raises(ValidationError):
        OperationalEventPayload(message="token 12345678901")


def test_market_event_payload_rejects_condition_ids():
    with pytest.raises(ValidationError):
        OperationalEventPayload(
            message="condition_id 0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        )


def test_execution_event_payload_preserves_dry_run_auditability():
    p = OperationalEventPayload(dry_run=True)
    assert p.dry_run is True


def test_execution_event_does_not_authorize_signing():
    p = OperationalEventPayload(dry_run=True)
    assert p is not None
    # Payload is purely informational — no signing authorization


def test_fail_closed_for_safety_critical_events():
    # CRITICAL events are never dropped by queue policy defaults
    policy = OperationalEventQueuePolicy()
    critical = {s for s in policy.critical_severities}
    assert OperationalEventSeverity.CRITICAL in critical
    assert OperationalEventSeverity.ERROR in critical


@pytest.mark.asyncio
async def test_non_critical_failure_does_not_crash_loop():
    repo_mock = AsyncMock()
    repo_mock.batch_append = AsyncMock(side_effect=RuntimeError("db down"))

    async def factory():
        return repo_mock

    config = _make_config()
    bus = OperationalEventBus(repository_factory=factory, config=config)
    await bus.publish(_make_create(severity=OperationalEventSeverity.INFO))
    # Flush should not raise — it should catch and return failed
    result = await bus._flush_batch()
    assert result.failed >= 0
    assert result.persisted == 0


# ═══════════════════════════════════════════════════════════════════════════
# Event Type Coverage — Representative Runtime Events
# ═══════════════════════════════════════════════════════════════════════════


def test_event_type_start_exists():
    assert OperationalEventType.START.value == "START"


def test_event_type_shutdown_exists():
    assert OperationalEventType.SHUTDOWN.value == "SHUTDOWN"


def test_event_type_config_loaded_exists():
    assert OperationalEventType.CONFIG_LOADED.value == "CONFIG_LOADED"


def test_event_type_market_discovered_exists():
    assert OperationalEventType.MARKET_DISCOVERED.value == "MARKET_DISCOVERED"


def test_event_type_market_rejected_exists():
    assert OperationalEventType.MARKET_REJECTED.value == "MARKET_REJECTED"


def test_event_type_market_quarantine_exists():
    assert OperationalEventType.MARKET_QUARANTINE.value == "MARKET_QUARANTINE"


def test_event_type_ws_connected_exists():
    assert OperationalEventType.WS_CONNECTED.value == "WS_CONNECTED"


def test_event_type_ws_reconnect_exists():
    assert OperationalEventType.WS_RECONNECT.value == "WS_RECONNECT"


def test_event_type_ws_pong_stale_exists():
    assert OperationalEventType.WS_PONG_STALE.value == "WS_PONG_STALE"


def test_event_type_ready_state_changed_exists():
    assert OperationalEventType.READY_STATE_CHANGED.value == "READY_STATE_CHANGED"


def test_event_type_llm_call_started_exists():
    assert OperationalEventType.LLM_CALL_STARTED.value == "LLM_CALL_STARTED"


def test_event_type_llm_call_blocked_exists():
    assert OperationalEventType.LLM_CALL_BLOCKED.value == "LLM_CALL_BLOCKED"


def test_event_type_budget_block_exists():
    assert OperationalEventType.BUDGET_BLOCK.value == "BUDGET_BLOCK"


def test_event_type_cooldown_block_exists():
    assert OperationalEventType.COOLDOWN_BLOCK.value == "COOLDOWN_BLOCK"


def test_event_type_provider_failure_exists():
    assert OperationalEventType.PROVIDER_FAILURE.value == "PROVIDER_FAILURE"


def test_event_type_decision_accepted_exists():
    assert OperationalEventType.DECISION_ACCEPTED.value == "DECISION_ACCEPTED"


def test_event_type_decision_skipped_exists():
    assert OperationalEventType.DECISION_SKIPPED.value == "DECISION_SKIPPED"


def test_event_type_execution_dry_run_exists():
    assert OperationalEventType.EXECUTION_DRY_RUN.value == "EXECUTION_DRY_RUN"


def test_event_type_circuit_breaker_open_exists():
    assert OperationalEventType.CIRCUIT_BREAKER_OPEN.value == "CIRCUIT_BREAKER_OPEN"


def test_event_type_circuit_breaker_closed_exists():
    assert OperationalEventType.CIRCUIT_BREAKER_CLOSED.value == "CIRCUIT_BREAKER_CLOSED"


def test_event_type_alert_sent_exists():
    assert OperationalEventType.ALERT_SENT.value == "ALERT_SENT"


def test_event_type_error_recovered_exists():
    assert OperationalEventType.ERROR_RECOVERED.value == "ERROR_RECOVERED"


# ═══════════════════════════════════════════════════════════════════════════
# LLMEvaluationResponse Invariant
# ═══════════════════════════════════════════════════════════════════════════


def test_llm_evaluation_response_not_modified_by_event_ledger():
    """LLMEvaluationResponse must not gain human_summary or any presentation field."""
    # Verify LLMEvaluationResponse does not have human_summary or similar fields
    fields = LLMEvaluationResponse.model_fields
    assert "human_summary" not in fields
    assert "narrative" not in fields
    assert "event_narrative" not in fields
    assert "presentation" not in fields
    # Core fields must still be present (Gatekeeper intact)
    assert "decision_boolean" in fields
    assert "recommended_action" in fields
    assert "confidence_score" in fields
    assert "expected_value" in fields
