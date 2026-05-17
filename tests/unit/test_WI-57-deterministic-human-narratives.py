"""
tests/unit/test_WI-57-deterministic-human-narratives.py

Unit tests for WI-57 Deterministic Human Narratives — presentation
schemas, deterministic event/reason → template mapping, secret-safe
output, fallback paths, and Gatekeeper / repository purity invariants.
"""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from src.schemas.llm import LLMEvaluationResponse
from src.schemas.ops import (
    DecisionNarrative,
    NarrativeInspectionHint,
    NarrativeRenderFailureReason,
    NarrativeRenderResult,
    NarrativeRenderStatus,
    NarrativeTemplateKey,
    OperationalEventPayload,
    OperationalEventPersistenceStatus,
    OperationalEventReasonCode,
    OperationalEventRecord,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
    OperationalNarrative,
    RuntimeNarrative,
)
from src.observability import operational_narratives as narratives_mod
from src.observability.operational_narratives import (
    render_event,
)
from src.db.repositories.operational_event_repository import (
    OperationalEventRepository,
)


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


def _summary(result: NarrativeRenderResult) -> str:
    assert result.narrative is not None
    if result.narrative.kind == "operational":
        return result.narrative.operational.summary  # type: ignore[union-attr]
    return result.narrative.decision.summary  # type: ignore[union-attr]


def _continuation(result: NarrativeRenderResult) -> str | None:
    assert result.narrative is not None
    if result.narrative.kind == "operational":
        return result.narrative.operational.continuation_state  # type: ignore[union-attr]
    return result.narrative.decision.continuation_state  # type: ignore[union-attr]


def _template_key(result: NarrativeRenderResult) -> NarrativeTemplateKey:
    assert result.narrative is not None
    if result.narrative.kind == "operational":
        return result.narrative.operational.template_key  # type: ignore[union-attr]
    return result.narrative.decision.template_key  # type: ignore[union-attr]


# ═══════════════════════════════════════════════════════════════════════════
# Schema separation & LLMEvaluationResponse purity (AC #1, #2)
# ═══════════════════════════════════════════════════════════════════════════


def test_narrative_schemas_exist_in_ops_module():
    for name in (
        "OperationalNarrative",
        "DecisionNarrative",
        "RuntimeNarrative",
        "NarrativeRenderResult",
        "NarrativeRenderStatus",
        "NarrativeRenderFailureReason",
        "NarrativeTemplateKey",
        "NarrativeInspectionHint",
    ):
        import src.schemas.ops as ops

        assert hasattr(ops, name), f"missing narrative schema: {name}"


def test_narrative_schemas_are_distinct_from_llm_evaluation_response():
    for cls in (
        OperationalNarrative,
        DecisionNarrative,
        RuntimeNarrative,
        NarrativeRenderResult,
    ):
        assert not issubclass(cls, LLMEvaluationResponse)


def test_llm_evaluation_response_has_no_presentation_fields():
    forbidden = {
        "human_summary",
        "operator_summary",
        "narrative",
        "presentation",
        "dashboard_summary",
        "digest_summary",
    }
    field_names = set(LLMEvaluationResponse.model_fields.keys())
    leaked = field_names & forbidden
    assert leaked == set(), (
        f"LLMEvaluationResponse leaked presentation fields: {leaked}"
    )


def test_narrative_render_result_schema_typed():
    rec = _make_record(
        event_type=OperationalEventType.START,
        reason_code=OperationalEventReasonCode.STARTUP,
    )
    result = render_event(rec)
    assert isinstance(result, NarrativeRenderResult)
    assert isinstance(result.status, NarrativeRenderStatus)
    assert result.failure_reason is None or isinstance(
        result.failure_reason, NarrativeRenderFailureReason
    )


def test_narrative_template_key_enum_covers_supported_types():
    expected_keys = {
        NarrativeTemplateKey.BUDGET_DAILY,
        NarrativeTemplateKey.BUDGET_HOURLY,
        NarrativeTemplateKey.COOLDOWN_REPEATED_HOLD,
        NarrativeTemplateKey.PROVIDER_CALL_FAILED,
        NarrativeTemplateKey.MARKET_REJECTED_INELIGIBLE,
        NarrativeTemplateKey.READINESS_DEGRADED,
        NarrativeTemplateKey.DECISION_ACCEPTED_BUY,
        NarrativeTemplateKey.DECISION_SKIP_LOW_CONF,
        NarrativeTemplateKey.EXECUTION_DRY_RUN,
        NarrativeTemplateKey.CIRCUIT_BREAKER_OPEN,
        NarrativeTemplateKey.ALERT_DISPATCHED,
        NarrativeTemplateKey.ERROR_HANDLED,
        NarrativeTemplateKey.GENERIC,
    }
    for key in expected_keys:
        assert isinstance(key, NarrativeTemplateKey)


def test_narrative_inspection_hint_schema():
    hint = NarrativeInspectionHint(
        component=OperationalEventSource.EVALUATION,
        pointer="budget",
        severity=OperationalEventSeverity.WARNING,
    )
    assert hint.component == OperationalEventSource.EVALUATION
    assert hint.pointer == "budget"


def test_inspection_hint_rejects_forbidden_pointer():
    with pytest.raises(ValidationError):
        NarrativeInspectionHint(
            component=OperationalEventSource.EVALUATION,
            pointer="api_key=abcdef",
        )


# ═══════════════════════════════════════════════════════════════════════════
# Determinism & no-LLM guarantee (AC #3, #5)
# ═══════════════════════════════════════════════════════════════════════════


def test_runtime_narrative_rendering_does_not_call_any_llm():
    targets = [
        "src.agents.evaluation.claude_client.ClaudeClient",
    ]
    rec = _make_record(
        event_type=OperationalEventType.BUDGET_BLOCK,
        reason_code=OperationalEventReasonCode.BUDGET_DAILY,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
    )
    patches = []
    for target in targets:
        try:
            patches.append(patch(target, autospec=True))
        except (AttributeError, ModuleNotFoundError):
            continue
    started = [p.start() for p in patches]
    try:
        result = render_event(rec)
        assert result.status in {
            NarrativeRenderStatus.SUCCESS,
            NarrativeRenderStatus.FALLBACK,
        }
        for mock in started:
            assert mock.call_count == 0
    finally:
        for p in patches:
            p.stop()


def test_same_input_produces_identical_narrative_output():
    fixed_ts = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    rec = _make_record(
        event_type=OperationalEventType.READY_STATE_CHANGED,
        reason_code=OperationalEventReasonCode.DEGRADED,
        source=OperationalEventSource.OBSERVABILITY,
        severity=OperationalEventSeverity.WARNING,
        payload={"ready_state": "DEGRADED"},
        timestamp=fixed_ts,
    )
    a = render_event(rec)
    b = render_event(rec)
    assert a.model_dump_json() == b.model_dump_json()


def test_narratives_explain_what_happened():
    rec = _make_record(
        event_type=OperationalEventType.BUDGET_BLOCK,
        reason_code=OperationalEventReasonCode.BUDGET_DAILY,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
    )
    result = render_event(rec)
    text = _summary(result).lower()
    assert "blocked" in text or "block" in text


def test_narratives_explain_why_using_stable_reason_code_wording():
    rec = _make_record(
        event_type=OperationalEventType.BUDGET_BLOCK,
        reason_code=OperationalEventReasonCode.BUDGET_DAILY,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
    )
    result = render_event(rec)
    assert "daily" in _summary(result).lower()


def test_narratives_explain_runtime_continuation_when_inferable():
    rec = _make_record(
        event_type=OperationalEventType.BUDGET_BLOCK,
        reason_code=OperationalEventReasonCode.BUDGET_DAILY,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
    )
    result = render_event(rec)
    assert _continuation(result) == "skipped"

    rec2 = _make_record(
        event_type=OperationalEventType.READY_STATE_CHANGED,
        reason_code=OperationalEventReasonCode.DEGRADED,
        source=OperationalEventSource.OBSERVABILITY,
        severity=OperationalEventSeverity.WARNING,
    )
    assert _continuation(render_event(rec2)) == "degraded"


def test_narratives_include_inspection_hint_when_applicable():
    rec = _make_record(
        event_type=OperationalEventType.READY_STATE_CHANGED,
        reason_code=OperationalEventReasonCode.DEGRADED,
        source=OperationalEventSource.OBSERVABILITY,
        severity=OperationalEventSeverity.WARNING,
    )
    result = render_event(rec)
    assert result.narrative is not None
    assert result.narrative.operational is not None
    assert result.narrative.operational.inspection_hint is not None
    assert result.narrative.operational.inspection_hint.pointer == "readiness"


# ═══════════════════════════════════════════════════════════════════════════
# Budget block coverage (AC #10)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "reason_code",
    [
        OperationalEventReasonCode.BUDGET_DAILY,
        OperationalEventReasonCode.BUDGET_HOURLY,
        OperationalEventReasonCode.BUDGET_TOKEN,
        OperationalEventReasonCode.BUDGET_COST,
        OperationalEventReasonCode.BUDGET_REFLECTION,
    ],
)
def test_budget_block_renders_stable_summary_for_each_budget_reason(reason_code):
    rec = _make_record(
        event_type=OperationalEventType.BUDGET_BLOCK,
        reason_code=reason_code,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    text = _summary(result).lower()
    assert "block" in text
    assert _continuation(result) == "skipped"


# ═══════════════════════════════════════════════════════════════════════════
# Cooldown coverage (AC #11)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "reason_code",
    [
        OperationalEventReasonCode.COOLDOWN_REPEATED_HOLD,
        OperationalEventReasonCode.COOLDOWN_REPEATED_INVALID,
    ],
)
def test_cooldown_block_renders_repeated_hold_and_invalid_summaries(reason_code):
    rec = _make_record(
        event_type=OperationalEventType.COOLDOWN_BLOCK,
        reason_code=reason_code,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    text = _summary(result).lower()
    assert "skip" in text or "skipped" in text


# ═══════════════════════════════════════════════════════════════════════════
# Provider failures (AC #12)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "reason_code",
    [
        OperationalEventReasonCode.PROVIDER_CALL_FAILED,
        OperationalEventReasonCode.PROVIDER_RESPONSE_MALFORMED,
    ],
)
def test_provider_failure_renders_safe_summary(reason_code):
    rec = _make_record(
        event_type=OperationalEventType.PROVIDER_FAILURE,
        reason_code=reason_code,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        payload={"provider_name": "deepseek"},
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    text = _summary(result).lower()
    assert "provider" in text


# ═══════════════════════════════════════════════════════════════════════════
# Market rejection / quarantine (AC #13)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "event_type,reason_code",
    [
        (
            OperationalEventType.MARKET_REJECTED,
            OperationalEventReasonCode.MARKET_INELIGIBLE,
        ),
        (
            OperationalEventType.MARKET_REJECTED,
            OperationalEventReasonCode.MARKET_NOT_FOUND,
        ),
        (
            OperationalEventType.MARKET_REJECTED,
            OperationalEventReasonCode.MARKET_COOLDOWN,
        ),
        (
            OperationalEventType.MARKET_QUARANTINE,
            OperationalEventReasonCode.MARKET_QUARANTINED,
        ),
    ],
)
def test_market_rejection_or_quarantine_renders_summary(event_type, reason_code):
    rec = _make_record(
        event_type=event_type,
        reason_code=reason_code,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.CONTEXT,
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    text = _summary(result).lower()
    assert "market" in text


# ═══════════════════════════════════════════════════════════════════════════
# Readiness changes (AC #14)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "reason_code,expected_continuation,expects_hint",
    [
        (OperationalEventReasonCode.READY, "continued", False),
        (OperationalEventReasonCode.DEGRADED, "degraded", True),
        (OperationalEventReasonCode.NOT_READY, "stopped", True),
    ],
)
def test_readiness_change_renders_summary_with_inspection_hint(
    reason_code, expected_continuation, expects_hint
):
    rec = _make_record(
        event_type=OperationalEventType.READY_STATE_CHANGED,
        reason_code=reason_code,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.OBSERVABILITY,
        payload={"ready_state": reason_code.value},
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    assert _continuation(result) == expected_continuation
    op = result.narrative.operational  # type: ignore[union-attr]
    assert (op.inspection_hint is not None) == expects_hint


# ═══════════════════════════════════════════════════════════════════════════
# Decision events (AC #15)
# ═══════════════════════════════════════════════════════════════════════════


def test_decision_accepted_renders_aggregate_action_summary():
    rec = _make_record(
        event_type=OperationalEventType.DECISION_ACCEPTED,
        reason_code=OperationalEventReasonCode.DECISION_BUY,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        payload={"decision_action": "BUY"},
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    assert result.narrative.kind == "decision"  # type: ignore[union-attr]
    assert result.narrative.decision.decision_action == "BUY"  # type: ignore[union-attr]
    assert "buy" in _summary(result).lower()


@pytest.mark.parametrize(
    "reason_code",
    [
        OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
        OperationalEventReasonCode.DECISION_SKIP_LOW_EV,
        OperationalEventReasonCode.DECISION_SKIP_HIGH_SPREAD,
        OperationalEventReasonCode.DECISION_SKIP_EXPOSURE,
        OperationalEventReasonCode.DECISION_SKIP_TTR,
    ],
)
def test_decision_skipped_renders_typed_skip_reason(reason_code):
    rec = _make_record(
        event_type=OperationalEventType.DECISION_SKIPPED,
        reason_code=reason_code,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    assert _continuation(result) == "skipped"


# ═══════════════════════════════════════════════════════════════════════════
# Dry-run execution (AC #16)
# ═══════════════════════════════════════════════════════════════════════════


def test_dry_run_execution_narrative_explicitly_states_no_live_signing_or_broadcast():
    rec = _make_record(
        event_type=OperationalEventType.EXECUTION_DRY_RUN,
        reason_code=OperationalEventReasonCode.EXEC_DRY_RUN_SKIP,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EXECUTION,
        payload={"dry_run": True, "decision_action": "BUY"},
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    text = _summary(result).lower()
    assert "dry-run" in text or "dry run" in text or "simulated" in text
    assert "no live" in text


# ═══════════════════════════════════════════════════════════════════════════
# Circuit breaker (AC #17)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "event_type,reason_code",
    [
        (OperationalEventType.CIRCUIT_BREAKER_OPEN, OperationalEventReasonCode.CB_OPEN),
        (
            OperationalEventType.CIRCUIT_BREAKER_CLOSED,
            OperationalEventReasonCode.CB_CLOSED,
        ),
        (
            OperationalEventType.CIRCUIT_BREAKER_CLOSED,
            OperationalEventReasonCode.CB_OVERRIDE,
        ),
    ],
)
def test_circuit_breaker_transitions_render_summary(event_type, reason_code):
    rec = _make_record(
        event_type=event_type,
        reason_code=reason_code,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    text = _summary(result).lower()
    assert "circuit breaker" in text or "breaker" in text


# ═══════════════════════════════════════════════════════════════════════════
# Alerts (AC #18)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "reason_code",
    [
        OperationalEventReasonCode.ALERT_DISPATCHED,
        OperationalEventReasonCode.ALERT_DISPATCH_FAILED,
    ],
)
def test_alert_outcome_narratives_omit_telegram_identifiers_and_tokens(reason_code):
    rec = _make_record(
        event_type=OperationalEventType.ALERT_SENT,
        reason_code=reason_code,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.OBSERVABILITY,
    )
    result = render_event(rec)
    text = _summary(result)
    assert "telegram" not in text.lower()
    assert "chat_id" not in text.lower()
    assert "bot" not in text.lower() or ":" not in text  # no telegram token shape


# ═══════════════════════════════════════════════════════════════════════════
# Recovery / error (AC #19)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "reason_code,expected_continuation",
    [
        (OperationalEventReasonCode.ERROR_HANDLED, "continued"),
        (OperationalEventReasonCode.ERROR_UNHANDLED, "degraded"),
    ],
)
def test_recovery_narratives_omit_raw_exception_text(
    reason_code, expected_continuation
):
    rec = _make_record(
        event_type=OperationalEventType.ERROR_RECOVERED,
        reason_code=reason_code,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.ORCHESTRATOR,
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    text = _summary(result).lower()
    assert "exception" not in text
    assert "traceback" not in text
    assert _continuation(result) == expected_continuation


# ═══════════════════════════════════════════════════════════════════════════
# Unknown combinations (AC #20)
# ═══════════════════════════════════════════════════════════════════════════


def test_unknown_event_reason_combination_returns_generic_conservative_summary():
    # WS_CONNECTED with an unrelated reason code → falls back
    rec = _make_record(
        event_type=OperationalEventType.WS_CONNECTED,
        reason_code=OperationalEventReasonCode.BUDGET_DAILY,  # nonsense pairing
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.INGESTION,
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.FALLBACK
    assert result.failure_reason == NarrativeRenderFailureReason.UNKNOWN_TEMPLATE
    assert _template_key(result) == NarrativeTemplateKey.GENERIC


def test_missing_template_for_supported_enum_returns_conservative_summary():
    rec = _make_record(
        event_type=OperationalEventType.START,
        reason_code=OperationalEventReasonCode.QUEUE_FULL,  # not in registry for START
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.ORCHESTRATOR,
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.FALLBACK
    assert _template_key(result) == NarrativeTemplateKey.GENERIC


# ═══════════════════════════════════════════════════════════════════════════
# Malformed payload, secrets in payload, timestamp normalization (AC #21, #22)
# ═══════════════════════════════════════════════════════════════════════════


def test_malformed_payload_json_returns_typed_failure_or_safe_fallback():
    rec = _make_record(
        event_type=OperationalEventType.START,
        reason_code=OperationalEventReasonCode.STARTUP,
        payload_json="{not valid json",
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.FALLBACK
    assert result.failure_reason == NarrativeRenderFailureReason.MALFORMED_PAYLOAD_JSON


def test_payload_with_forbidden_secret_returns_redacted_fallback_or_typed_failure():
    # Inject a wallet-address-shaped string into payload_json (bypassing
    # the OperationalEventPayload validator that would normally block this).
    # WI-57 secret/high-cardinality safety requires the narrative layer to
    # fail closed (REDACTED) for such persisted records, not return SUCCESS,
    # even though the template would not have echoed the field.
    forbidden = "0x" + "a" * 40
    rec = _make_record(
        event_type=OperationalEventType.START,
        reason_code=OperationalEventReasonCode.STARTUP,
        payload_json=json.dumps({"message": f"hello {forbidden}"}),
    )
    result = render_event(rec)
    text = _summary(result)
    assert forbidden not in text
    assert result.status == NarrativeRenderStatus.REDACTED
    assert result.failure_reason == NarrativeRenderFailureReason.FORBIDDEN_CONTENT


def test_payload_with_nested_forbidden_secret_returns_redacted():
    # The scan must recurse into nested dicts and lists; a forbidden
    # pattern hidden inside a list value still triggers REDACTED.
    forbidden = "0x" + "c" * 40
    rec = _make_record(
        event_type=OperationalEventType.START,
        reason_code=OperationalEventReasonCode.STARTUP,
        payload_json=json.dumps({"outer": {"inner": [f"see {forbidden}"]}}),
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.REDACTED
    assert result.failure_reason == NarrativeRenderFailureReason.FORBIDDEN_CONTENT
    assert forbidden not in _summary(result)


def test_payload_json_float_budget_is_decimal_safe_before_rendering():
    # Persisted JSON bypasses OperationalEventPayload construction. The
    # renderer must still parse JSON floats as Decimal so budget/spend inputs
    # never enter the presentation path as raw Python float values.
    rec = _make_record(
        event_type=OperationalEventType.BUDGET_BLOCK,
        reason_code=OperationalEventReasonCode.BUDGET_DAILY,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        payload_json='{"budget_remaining": 1.23}',
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    assert "1.23" not in _summary(result)


def test_payload_with_forbidden_raw_field_name_returns_redacted():
    rec = _make_record(
        event_type=OperationalEventType.PROVIDER_FAILURE,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_FAILED,
        severity=OperationalEventSeverity.ERROR,
        source=OperationalEventSource.EVALUATION,
        payload_json=json.dumps({"raw_provider_response": "malformed output"}),
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.REDACTED
    assert result.failure_reason == NarrativeRenderFailureReason.FORBIDDEN_CONTENT
    assert "malformed output" not in _summary(result)


def test_payload_with_unknown_condition_id_key_returns_redacted():
    rec = _make_record(
        event_type=OperationalEventType.MARKET_REJECTED,
        reason_code=OperationalEventReasonCode.MARKET_INELIGIBLE,
        source=OperationalEventSource.CONTEXT,
        payload_json=json.dumps({"condition_id": "0x" + "d" * 64}),
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.REDACTED
    assert result.failure_reason == NarrativeRenderFailureReason.FORBIDDEN_CONTENT
    assert "condition_id" not in _summary(result)


def test_provider_name_must_be_stable_low_cardinality_label():
    rec = _make_record(
        event_type=OperationalEventType.PROVIDER_FAILURE,
        reason_code=OperationalEventReasonCode.PROVIDER_CALL_FAILED,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        payload_json=json.dumps({"provider_name": "tenant-market-slug-abc123"}),
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.REDACTED
    assert result.failure_reason == NarrativeRenderFailureReason.FORBIDDEN_CONTENT
    assert "tenant-market-slug-abc123" not in _summary(result)


def test_payload_decision_action_cannot_contradict_typed_reason_code():
    # Persisted payload claims SELL, but the typed reason_code is
    # DECISION_BUY. The reason_code is the source of truth: the rendered
    # narrative must surface BUY and must never expose SELL.
    rec = _make_record(
        event_type=OperationalEventType.DECISION_ACCEPTED,
        reason_code=OperationalEventReasonCode.DECISION_BUY,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        payload={"decision_action": "SELL"},
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    assert result.narrative is not None
    assert result.narrative.kind == "decision"  # type: ignore[union-attr]
    assert result.narrative.decision.decision_action == "BUY"  # type: ignore[union-attr]
    text = _summary(result)
    assert "SELL" not in text
    assert "BUY" in text


def test_decision_skipped_decision_action_derived_from_typed_reason_code():
    # DECISION_SKIPPED with an inconsistent payload must still surface
    # "SKIP" as the aggregate decision action, derived from reason_code.
    rec = _make_record(
        event_type=OperationalEventType.DECISION_SKIPPED,
        reason_code=OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
        severity=OperationalEventSeverity.INFO,
        source=OperationalEventSource.EVALUATION,
        payload={"decision_action": "BUY"},
    )
    result = render_event(rec)
    assert result.status == NarrativeRenderStatus.SUCCESS
    assert result.narrative is not None
    assert result.narrative.kind == "decision"  # type: ignore[union-attr]
    assert result.narrative.decision.decision_action == "SKIP"  # type: ignore[union-attr]


def test_naive_or_missing_timestamp_normalized_or_omitted_safely():
    naive_ts = datetime(2026, 5, 15, 12, 0, 0)  # no tzinfo
    rec = _make_record(
        event_type=OperationalEventType.START,
        reason_code=OperationalEventReasonCode.STARTUP,
        timestamp=naive_ts,
    )
    result = render_event(rec)
    assert result.narrative is not None
    ts = result.narrative.operational.timestamp_utc  # type: ignore[union-attr]
    assert ts is not None and ts.tzinfo == timezone.utc
    assert result.failure_reason == NarrativeRenderFailureReason.NAIVE_TIMESTAMP
    assert result.status == NarrativeRenderStatus.FALLBACK


# ═══════════════════════════════════════════════════════════════════════════
# Secret & high-cardinality scanning of OUTPUT (AC #22, #23)
# ═══════════════════════════════════════════════════════════════════════════


def test_narrative_output_is_scanned_before_return():
    # Monkey-patch the template registry to inject a forbidden value;
    # the scanner must downgrade the result to REDACTED with a safe summary.
    poisoned = "leaked api_key=AAAA"
    backup = narratives_mod._TEMPLATE_REGISTRY[
        (OperationalEventType.START, OperationalEventReasonCode.STARTUP)
    ]
    try:
        narratives_mod._TEMPLATE_REGISTRY[
            (OperationalEventType.START, OperationalEventReasonCode.STARTUP)
        ] = (NarrativeTemplateKey.LIFECYCLE_START, poisoned, "continued")
        rec = _make_record(
            event_type=OperationalEventType.START,
            reason_code=OperationalEventReasonCode.STARTUP,
        )
        result = render_event(rec)
        assert result.status == NarrativeRenderStatus.REDACTED
        assert result.failure_reason == NarrativeRenderFailureReason.FORBIDDEN_CONTENT
        assert "api_key" not in _summary(result)
    finally:
        narratives_mod._TEMPLATE_REGISTRY[
            (OperationalEventType.START, OperationalEventReasonCode.STARTUP)
        ] = backup


@pytest.mark.parametrize(
    "poison",
    [
        "5555555555:AAEhBOweik6ad9r_DKa1H4nrUuJoxxxxxxx",  # telegram token
        "0x" + "a" * 64,  # private key 0x
        "0x" + "b" * 40,  # wallet address
        "12345678901234567",  # token id
        "raw_prompt: hello",
        "api_key=xyz",
    ],
)
def test_narrative_output_never_contains_secret_or_id_patterns(poison):
    backup = narratives_mod._TEMPLATE_REGISTRY[
        (OperationalEventType.START, OperationalEventReasonCode.STARTUP)
    ]
    try:
        narratives_mod._TEMPLATE_REGISTRY[
            (OperationalEventType.START, OperationalEventReasonCode.STARTUP)
        ] = (NarrativeTemplateKey.LIFECYCLE_START, f"leak {poison}", "continued")
        rec = _make_record(
            event_type=OperationalEventType.START,
            reason_code=OperationalEventReasonCode.STARTUP,
        )
        result = render_event(rec)
        assert poison not in _summary(result)
        assert result.status == NarrativeRenderStatus.REDACTED
    finally:
        narratives_mod._TEMPLATE_REGISTRY[
            (OperationalEventType.START, OperationalEventReasonCode.STARTUP)
        ] = backup


def test_narrative_output_never_contains_raw_exception_message():
    rec = _make_record(
        event_type=OperationalEventType.ERROR_RECOVERED,
        reason_code=OperationalEventReasonCode.ERROR_UNHANDLED,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.ORCHESTRATOR,
        payload={"message": "an error occurred"},
    )
    result = render_event(rec)
    text = _summary(result).lower()
    assert "traceback" not in text
    assert "an error occurred" not in text


# ═══════════════════════════════════════════════════════════════════════════
# Persistence / repository discipline (AC #24, #25, #26)
# ═══════════════════════════════════════════════════════════════════════════


def test_narrative_render_does_not_mutate_input_event_record():
    rec = _make_record(
        event_type=OperationalEventType.START,
        reason_code=OperationalEventReasonCode.STARTUP,
    )
    before = rec.model_dump_json()
    render_event(rec)
    assert rec.model_dump_json() == before


def test_narrative_module_does_not_expose_repository_write_or_delete_methods():
    for name in dir(narratives_mod):
        if name.startswith("_"):
            continue
        lower = name.lower()
        for forbidden in ("write", "update", "delete", "persist", "insert", "save"):
            assert forbidden not in lower, (
                f"Narrative module exposes forbidden API: {name}"
            )


def test_operational_event_repository_unchanged_for_write_or_delete_methods():
    members = {n for n in dir(OperationalEventRepository) if not n.startswith("_")}
    forbidden = {"update", "delete", "remove", "purge", "truncate", "modify"}
    overlap = members & forbidden
    assert overlap == set(), (
        f"OperationalEventRepository gained forbidden methods: {overlap}"
    )


def test_narrative_repository_helper_is_read_only_if_present():
    # If any read-only helper exists, it must operate over read_window only.
    public = [
        n
        for n in dir(narratives_mod)
        if not n.startswith("_") and callable(getattr(narratives_mod, n))
    ]
    for name in public:
        fn = getattr(narratives_mod, name)
        try:
            inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        for forbidden in ("write", "append", "persist", "save"):
            assert forbidden not in name.lower()


def test_narrative_layer_does_not_hold_raw_db_session():
    src = open(narratives_mod.__file__, "r", encoding="utf-8").read()
    assert "AsyncSession" not in src
    assert "sqlalchemy" not in src.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Metrics / log cardinality (AC #27)
# ═══════════════════════════════════════════════════════════════════════════


def test_metrics_emitted_by_narrative_layer_use_low_cardinality_labels():
    # Currently the renderer does not emit metrics directly; assert no
    # high-cardinality metric registrations exist in module source. Defensive
    # scanner denylist strings are allowed here; they are not emitted labels.
    src = open(narratives_mod.__file__, "r", encoding="utf-8").read()
    assert "MetricEvent(" not in src
    assert "metrics_registry" not in src
    assert ".record_" not in src


def test_log_events_from_narrative_layer_use_low_cardinality_keys():
    src = open(narratives_mod.__file__, "r", encoding="utf-8").read()
    # Log keys are bounded to enum values; ensure no payload-derived keys.
    assert "payload_json=" not in src


# ═══════════════════════════════════════════════════════════════════════════
# Trading & Gatekeeper invariants (AC #28, #29)
# ═══════════════════════════════════════════════════════════════════════════


def test_narrative_layer_does_not_perform_trading_or_sizing_calculations():
    src = open(narratives_mod.__file__, "r", encoding="utf-8").read()
    for token in ("Kelly", "kelly_quarter", "expected_value =", "position_size_pct ="):
        assert token not in src


def test_narrative_layer_does_not_invoke_execution_router_or_signer():
    src = open(narratives_mod.__file__, "r", encoding="utf-8").read()
    # Strip module docstring before scanning so anti-pattern descriptions
    # do not trip the substring check.
    import ast as _ast

    tree = _ast.parse(src)
    body_src = "\n".join(
        _ast.unparse(node)
        for node in tree.body
        if not (isinstance(node, _ast.Expr) and isinstance(node.value, _ast.Constant))
    )
    for token in ("ExecutionRouter", "WalletSigner", "signer.sign", "broadcast("):
        assert token not in body_src


def test_narrative_layer_does_not_change_dry_run_semantics():
    src = open(narratives_mod.__file__, "r", encoding="utf-8").read()
    # dry_run is only read from payload; never assigned to config
    assert "DRY_RUN =" not in src
    assert "os.environ" not in src


# ═══════════════════════════════════════════════════════════════════════════
# Decimal safety (AC #30)
# ═══════════════════════════════════════════════════════════════════════════


def test_decimal_bearing_narrative_inputs_remain_decimal_at_schema_boundary():
    with pytest.raises(ValidationError):
        OperationalEventPayload(budget_remaining=1.23)  # type: ignore[arg-type]
    # Decimal is accepted
    p = OperationalEventPayload(budget_remaining=Decimal("1.23"))
    assert p.budget_remaining == Decimal("1.23")


def test_narrative_does_not_convert_decimal_to_float():
    src = open(narratives_mod.__file__, "r", encoding="utf-8").read()
    assert "float(" not in src


# ═══════════════════════════════════════════════════════════════════════════
# Sequence stability (Edge Case #18)
# ═══════════════════════════════════════════════════════════════════════════


def test_rendering_a_sequence_of_events_is_stable_and_order_independent_per_event():
    fixed_ts = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
    rec_a = _make_record(
        event_type=OperationalEventType.BUDGET_BLOCK,
        reason_code=OperationalEventReasonCode.BUDGET_DAILY,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.EVALUATION,
        timestamp=fixed_ts,
    )
    rec_b = _make_record(
        event_type=OperationalEventType.READY_STATE_CHANGED,
        reason_code=OperationalEventReasonCode.DEGRADED,
        severity=OperationalEventSeverity.WARNING,
        source=OperationalEventSource.OBSERVABILITY,
        timestamp=fixed_ts,
    )
    rendered_forward = [render_event(rec_a), render_event(rec_b)]
    rendered_reverse = [render_event(rec_b), render_event(rec_a)]
    # Per-record narrative text is order-independent
    assert _summary(rendered_forward[0]) == _summary(rendered_reverse[1])
    assert _summary(rendered_forward[1]) == _summary(rendered_reverse[0])
