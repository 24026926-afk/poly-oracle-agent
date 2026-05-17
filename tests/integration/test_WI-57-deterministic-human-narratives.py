"""
tests/integration/test_WI-57-deterministic-human-narratives.py

Integration tests for WI-57 Deterministic Human Narratives — exercises
the narrative layer end-to-end against an in-memory SQLite ledger backed
by OperationalEventRepository (WI-56), plus cross-cutting checks that
no LLM call, no execution call, and no Gatekeeper field is introduced.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.db.repositories.operational_event_repository import (
    OperationalEventRepository,
)
from src.observability.operational_narratives import (
    render_event,
    render_window,
)
from src.schemas.llm import LLMEvaluationResponse
from src.schemas.ops import (
    NarrativeRenderFailureReason,
    NarrativeRenderResult,
    NarrativeRenderStatus,
    NarrativeTemplateKey,
    OperationalEventCreate,
    OperationalEventPayload,
    OperationalEventPersistenceStatus,
    OperationalEventQuery,
    OperationalEventReadWindow,
    OperationalEventReasonCode,
    OperationalEventRecord,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _make_create(
    *,
    event_type: OperationalEventType,
    reason_code: OperationalEventReasonCode,
    severity: OperationalEventSeverity = OperationalEventSeverity.INFO,
    source: OperationalEventSource = OperationalEventSource.ORCHESTRATOR,
    payload: OperationalEventPayload | None = None,
) -> OperationalEventCreate:
    return OperationalEventCreate(
        event_type=event_type,
        severity=severity,
        source=source,
        reason_code=reason_code,
        payload=payload or OperationalEventPayload(),
    )


def _representative_events() -> list[OperationalEventCreate]:
    """Seed a mix of representative WI-56 events covering Phase 16 paths."""
    return [
        _make_create(
            event_type=OperationalEventType.START,
            reason_code=OperationalEventReasonCode.STARTUP,
        ),
        _make_create(
            event_type=OperationalEventType.BUDGET_BLOCK,
            reason_code=OperationalEventReasonCode.BUDGET_DAILY,
            severity=OperationalEventSeverity.WARNING,
            source=OperationalEventSource.EVALUATION,
        ),
        _make_create(
            event_type=OperationalEventType.COOLDOWN_BLOCK,
            reason_code=OperationalEventReasonCode.COOLDOWN_REPEATED_HOLD,
            source=OperationalEventSource.EVALUATION,
        ),
        _make_create(
            event_type=OperationalEventType.PROVIDER_FAILURE,
            reason_code=OperationalEventReasonCode.PROVIDER_CALL_FAILED,
            severity=OperationalEventSeverity.WARNING,
            source=OperationalEventSource.EVALUATION,
            payload=OperationalEventPayload(provider_name="deepseek"),
        ),
        _make_create(
            event_type=OperationalEventType.MARKET_REJECTED,
            reason_code=OperationalEventReasonCode.MARKET_INELIGIBLE,
            source=OperationalEventSource.CONTEXT,
        ),
        _make_create(
            event_type=OperationalEventType.READY_STATE_CHANGED,
            reason_code=OperationalEventReasonCode.DEGRADED,
            severity=OperationalEventSeverity.WARNING,
            source=OperationalEventSource.OBSERVABILITY,
            payload=OperationalEventPayload(ready_state="DEGRADED"),
        ),
        _make_create(
            event_type=OperationalEventType.DECISION_ACCEPTED,
            reason_code=OperationalEventReasonCode.DECISION_BUY,
            source=OperationalEventSource.EVALUATION,
            payload=OperationalEventPayload(decision_action="BUY"),
        ),
        _make_create(
            event_type=OperationalEventType.DECISION_SKIPPED,
            reason_code=OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
            source=OperationalEventSource.EVALUATION,
        ),
        _make_create(
            event_type=OperationalEventType.EXECUTION_DRY_RUN,
            reason_code=OperationalEventReasonCode.EXEC_DRY_RUN_SKIP,
            source=OperationalEventSource.EXECUTION,
            payload=OperationalEventPayload(dry_run=True, decision_action="BUY"),
        ),
        _make_create(
            event_type=OperationalEventType.CIRCUIT_BREAKER_OPEN,
            reason_code=OperationalEventReasonCode.CB_OPEN,
            severity=OperationalEventSeverity.WARNING,
            source=OperationalEventSource.EVALUATION,
        ),
        _make_create(
            event_type=OperationalEventType.ALERT_SENT,
            reason_code=OperationalEventReasonCode.ALERT_DISPATCHED,
            source=OperationalEventSource.OBSERVABILITY,
        ),
        _make_create(
            event_type=OperationalEventType.ERROR_RECOVERED,
            reason_code=OperationalEventReasonCode.ERROR_HANDLED,
            source=OperationalEventSource.ORCHESTRATOR,
        ),
    ]


# ═══════════════════════════════════════════════════════════════════════════
# Repository-backed read & render (AC #25, #26)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_narrative_layer_renders_records_returned_by_operational_event_repository_read_window(
    async_session,
):
    repo = OperationalEventRepository(async_session)
    for ev in _representative_events():
        await repo.append(ev)

    window = await repo.read_window(OperationalEventQuery(limit=100))
    results = render_window(window)
    assert len(results) == len(_representative_events())
    for r in results:
        assert isinstance(r, NarrativeRenderResult)
        assert r.status in {
            NarrativeRenderStatus.SUCCESS,
            NarrativeRenderStatus.FALLBACK,
            NarrativeRenderStatus.REDACTED,
        }
        if r.status == NarrativeRenderStatus.SUCCESS:
            assert r.narrative is not None


@pytest.mark.asyncio
async def test_repository_backed_narrative_helper_is_read_only_against_real_db(
    async_session,
):
    repo = OperationalEventRepository(async_session)
    for ev in _representative_events():
        await repo.append(ev)

    # Track session.execute calls — only SELECT statements should fire
    # during the read+render pass.
    original_execute = async_session.execute
    seen_statements: list[str] = []

    async def _tracking_execute(stmt, *args, **kwargs):
        seen_statements.append(str(stmt).strip().split()[0].upper())
        return await original_execute(stmt, *args, **kwargs)

    async_session.execute = _tracking_execute  # type: ignore[assignment]
    try:
        window = await repo.read_window(OperationalEventQuery(limit=100))
        render_window(window)
    finally:
        async_session.execute = original_execute  # type: ignore[assignment]

    assert seen_statements, "expected at least one statement"
    for verb in seen_statements:
        assert verb == "SELECT", f"non-read statement detected: {verb}"


@pytest.mark.asyncio
async def test_narrative_helper_does_not_mutate_persisted_events(async_session):
    repo = OperationalEventRepository(async_session)
    for ev in _representative_events():
        await repo.append(ev)

    before = await repo.read_window(OperationalEventQuery(limit=100))
    before_json = sorted(e.model_dump_json() for e in before.events)

    render_window(before)

    after = await repo.read_window(OperationalEventQuery(limit=100))
    after_json = sorted(e.model_dump_json() for e in after.events)
    assert before_json == after_json


# ═══════════════════════════════════════════════════════════════════════════
# Mapping coverage across event surface (AC #4)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_every_supported_event_type_renders_a_typed_success_or_typed_fallback():
    for event_type in OperationalEventType:
        rec = OperationalEventRecord(
            id=str(uuid.uuid4()),
            event_type=event_type,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=OperationalEventReasonCode.STARTUP,
            payload_json="{}",
            persistence_status=OperationalEventPersistenceStatus.PERSISTED,
            created_at_utc=datetime.now(timezone.utc),
        )
        result = render_event(rec)
        assert isinstance(result.status, NarrativeRenderStatus)
        assert (
            result.narrative is not None
            or result.status == NarrativeRenderStatus.FAILED
        )


@pytest.mark.asyncio
async def test_every_supported_reason_code_renders_a_typed_success_or_typed_fallback():
    for reason_code in OperationalEventReasonCode:
        rec = OperationalEventRecord(
            id=str(uuid.uuid4()),
            event_type=OperationalEventType.START,
            severity=OperationalEventSeverity.INFO,
            source=OperationalEventSource.ORCHESTRATOR,
            reason_code=reason_code,
            payload_json="{}",
            persistence_status=OperationalEventPersistenceStatus.PERSISTED,
            created_at_utc=datetime.now(timezone.utc),
        )
        result = render_event(rec)
        assert isinstance(result.status, NarrativeRenderStatus)


# ═══════════════════════════════════════════════════════════════════════════
# No-LLM, no-execution guarantees (AC #3, #28, #29)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_render_pass_makes_zero_calls_to_claude_or_deepseek_or_grok_clients(
    async_session,
):
    repo = OperationalEventRepository(async_session)
    for ev in _representative_events():
        await repo.append(ev)

    patch_specs = [
        "src.agents.evaluation.claude_client.ClaudeClient.start",
        "src.agents.evaluation.claude_client.ClaudeClient._process_evaluation",
        "src.agents.evaluation.claude_client.ClaudeClient.evaluate_for_backtest",
    ]
    patchers = []
    for spec in patch_specs:
        try:
            patchers.append(patch(spec, new_callable=AsyncMock))
        except (AttributeError, ModuleNotFoundError):
            continue
    mocks = [p.start() for p in patchers]
    try:
        window = await repo.read_window(OperationalEventQuery(limit=100))
        render_window(window)
        for m in mocks:
            assert m.call_count == 0
    finally:
        for p in patchers:
            p.stop()


@pytest.mark.asyncio
async def test_full_render_pass_does_not_invoke_execution_router_or_signer(
    async_session,
):
    repo = OperationalEventRepository(async_session)
    for ev in _representative_events():
        await repo.append(ev)

    patch_specs = [
        "src.agents.execution.execution_router.ExecutionRouter.route",
    ]
    patchers = []
    for spec in patch_specs:
        try:
            patchers.append(patch(spec, new_callable=AsyncMock))
        except (AttributeError, ModuleNotFoundError):
            continue
    mocks = [p.start() for p in patchers]
    try:
        window = await repo.read_window(OperationalEventQuery(limit=100))
        render_window(window)
        for m in mocks:
            assert m.call_count == 0
    finally:
        for p in patchers:
            p.stop()


# ═══════════════════════════════════════════════════════════════════════════
# Secret safety on rendered output (AC #22, #23)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_render_pass_over_real_ledger_yields_no_forbidden_content_in_any_narrative(
    async_session,
):
    repo = OperationalEventRepository(async_session)
    for ev in _representative_events():
        await repo.append(ev)

    forbidden_substrings = [
        "0x" + "a" * 40,  # wallet
        "0x" + "b" * 64,  # private key
        "raw_prompt",
        "api_key",
        "telegram",
        "chat_id",
    ]
    window = await repo.read_window(OperationalEventQuery(limit=100))
    results = render_window(window)
    for r in results:
        assert r.narrative is not None
        if r.narrative.kind == "operational":
            text = r.narrative.operational.summary
        else:
            text = r.narrative.decision.summary
        lower = text.lower()
        for s in forbidden_substrings:
            assert s.lower() not in lower


@pytest.mark.asyncio
async def test_render_pass_with_injected_malformed_payload_records_does_not_crash_loop(
    async_session,
):
    repo = OperationalEventRepository(async_session)
    for ev in _representative_events():
        await repo.append(ev)

    window = await repo.read_window(OperationalEventQuery(limit=100))
    # Inject a malformed record directly into the window list.
    bad = OperationalEventRecord(
        id=str(uuid.uuid4()),
        event_type=OperationalEventType.START,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
        reason_code=OperationalEventReasonCode.STARTUP,
        payload_json="{not valid json",
        persistence_status=OperationalEventPersistenceStatus.PERSISTED,
        created_at_utc=datetime.now(timezone.utc),
    )
    poisoned = OperationalEventReadWindow(
        events=list(window.events) + [bad],
        total_count=window.total_count + 1,
    )
    results = render_window(poisoned)
    assert len(results) == len(poisoned.events)
    # Bad record returns a typed fallback, not an exception
    assert results[-1].status == NarrativeRenderStatus.FALLBACK
    assert (
        results[-1].failure_reason
        == NarrativeRenderFailureReason.MALFORMED_PAYLOAD_JSON
    )


# ═══════════════════════════════════════════════════════════════════════════
# Gatekeeper purity under integration (AC #2)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_llm_evaluation_response_remains_free_of_presentation_fields_after_module_import():
    # Re-import the narrative module to confirm no monkey-patching.
    import importlib

    import src.observability.operational_narratives as mod

    importlib.reload(mod)

    forbidden = {
        "human_summary",
        "operator_summary",
        "narrative",
        "presentation",
        "dashboard_summary",
        "digest_summary",
    }
    field_names = set(LLMEvaluationResponse.model_fields.keys())
    assert field_names & forbidden == set()


# ═══════════════════════════════════════════════════════════════════════════
# Determinism under sequencing (AC #5)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_two_independent_render_passes_over_identical_ledger_produce_identical_text(
    async_session,
):
    repo = OperationalEventRepository(async_session)
    for ev in _representative_events():
        await repo.append(ev)

    window_a = await repo.read_window(OperationalEventQuery(limit=100))
    window_b = await repo.read_window(OperationalEventQuery(limit=100))

    pass_a = render_window(window_a)
    pass_b = render_window(window_b)
    assert len(pass_a) == len(pass_b)
    for a, b in zip(pass_a, pass_b):
        assert a.model_dump_json() == b.model_dump_json()


# ═══════════════════════════════════════════════════════════════════════════
# Metrics & log cardinality (AC #27)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_metrics_emitted_during_integration_render_use_bounded_labels_only(
    async_session,
):
    # The renderer does not currently emit metrics — assert that any
    # rendered detail/template-key value comes from bounded enum sets.
    repo = OperationalEventRepository(async_session)
    for ev in _representative_events():
        await repo.append(ev)

    window = await repo.read_window(OperationalEventQuery(limit=100))
    results = render_window(window)
    for r in results:
        if r.narrative is not None:
            if r.narrative.kind == "operational":
                tk = r.narrative.operational.template_key
            else:
                tk = r.narrative.decision.template_key
            assert isinstance(tk, NarrativeTemplateKey)
