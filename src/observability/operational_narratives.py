"""
src/observability/operational_narratives.py

WI-57 — Deterministic Human Narratives.

Converts typed OperationalEventRecord objects from the WI-56 ledger into
plain-English operator summaries via stable, deterministic mappings.

This module is presentation-only:

* It does not call Claude, DeepSeek, Grok, or any other LLM.
* It does not write to the database; persistence remains repository-owned.
* It does not perform trading, sizing, EV, or Gatekeeper calculations.
* It does not invoke the execution router, signer, or broadcaster.
* It does not change DRY_RUN semantics.
* Its output is scanned for forbidden secret/high-cardinality patterns
  before being returned to callers.

Future surfaces (WI-58 incident replay CLI, WI-59 dashboard activity feed,
WI-60 daily operations digest) consume `NarrativeRenderResult` values.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import structlog
from pydantic import ValidationError

from src.schemas.ops import (
    DecisionNarrative,
    NarrativeInspectionHint,
    NarrativeRenderFailureReason,
    NarrativeRenderResult,
    NarrativeRenderStatus,
    NarrativeTemplateKey,
    OperationalEventReadWindow,
    OperationalEventPayload,
    OperationalEventReasonCode,
    OperationalEventRecord,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
    OperationalNarrative,
    RuntimeNarrative,
    _scan_event_payload,
)

logger = structlog.get_logger(__name__)


# ── Stable continuation-state vocabulary ───────────────────────────────────

_CONT_CONTINUED = "continued"
_CONT_SKIPPED = "skipped"
_CONT_DEGRADED = "degraded"
_CONT_STOPPED = "stopped"


# ── Template registry ──────────────────────────────────────────────────────

# (event_type, reason_code) → (template_key, base summary, continuation_state)
#
# Summary strings are deterministic English. They must not reference raw
# payload values; any payload-derived insertions (provider name, market
# count, ready state, dry-run status) happen via the bounded format helper.

_TEMPLATE_REGISTRY: dict[
    tuple[OperationalEventType, OperationalEventReasonCode],
    tuple[NarrativeTemplateKey, str, Optional[str]],
] = {
    # Lifecycle
    (OperationalEventType.START, OperationalEventReasonCode.STARTUP): (
        NarrativeTemplateKey.LIFECYCLE_START,
        "The agent started up.",
        _CONT_CONTINUED,
    ),
    (OperationalEventType.SHUTDOWN, OperationalEventReasonCode.GRACEFUL_SHUTDOWN): (
        NarrativeTemplateKey.LIFECYCLE_SHUTDOWN,
        "The agent shut down gracefully.",
        _CONT_STOPPED,
    ),
    (OperationalEventType.SHUTDOWN, OperationalEventReasonCode.FORCED_SHUTDOWN): (
        NarrativeTemplateKey.LIFECYCLE_SHUTDOWN,
        "The agent was forced to shut down.",
        _CONT_STOPPED,
    ),
    (OperationalEventType.CONFIG_LOADED, OperationalEventReasonCode.CONFIG_VALID): (
        NarrativeTemplateKey.CONFIG_LOADED,
        "Configuration loaded successfully.",
        _CONT_CONTINUED,
    ),
    (OperationalEventType.CONFIG_LOADED, OperationalEventReasonCode.CONFIG_INVALID): (
        NarrativeTemplateKey.CONFIG_LOADED,
        "Configuration validation failed; the agent did not start trading.",
        _CONT_STOPPED,
    ),
    # Market discovery
    (OperationalEventType.MARKET_DISCOVERED, OperationalEventReasonCode.MARKET_FOUND): (
        NarrativeTemplateKey.MARKET_DISCOVERED,
        "A new market was discovered and considered for evaluation.",
        _CONT_CONTINUED,
    ),
    (
        OperationalEventType.MARKET_DISCOVERED,
        OperationalEventReasonCode.MARKET_ELIGIBLE,
    ): (
        NarrativeTemplateKey.MARKET_DISCOVERED,
        "A market was confirmed eligible for evaluation.",
        _CONT_CONTINUED,
    ),
    (
        OperationalEventType.MARKET_REJECTED,
        OperationalEventReasonCode.MARKET_INELIGIBLE,
    ): (
        NarrativeTemplateKey.MARKET_REJECTED_INELIGIBLE,
        "A market was skipped because it did not meet typed eligibility rules.",
        _CONT_SKIPPED,
    ),
    (
        OperationalEventType.MARKET_REJECTED,
        OperationalEventReasonCode.MARKET_NOT_FOUND,
    ): (
        NarrativeTemplateKey.MARKET_REJECTED_NOT_FOUND,
        "A market was skipped because it could not be found upstream.",
        _CONT_SKIPPED,
    ),
    (
        OperationalEventType.MARKET_REJECTED,
        OperationalEventReasonCode.MARKET_COOLDOWN,
    ): (
        NarrativeTemplateKey.MARKET_REJECTED_COOLDOWN,
        "A market was skipped because it is in a typed cooldown window.",
        _CONT_SKIPPED,
    ),
    (
        OperationalEventType.MARKET_QUARANTINE,
        OperationalEventReasonCode.MARKET_QUARANTINED,
    ): (
        NarrativeTemplateKey.MARKET_QUARANTINED,
        "A market was quarantined and removed from evaluation rotation.",
        _CONT_SKIPPED,
    ),
    # WebSocket
    (OperationalEventType.WS_CONNECTED, OperationalEventReasonCode.WS_ESTABLISHED): (
        NarrativeTemplateKey.WS_CONNECTED,
        "The market data WebSocket connection was established.",
        _CONT_CONTINUED,
    ),
    (OperationalEventType.WS_RECONNECT, OperationalEventReasonCode.WS_RECONNECTED): (
        NarrativeTemplateKey.WS_RECONNECT,
        "The market data WebSocket reconnected after a transient loss.",
        _CONT_CONTINUED,
    ),
    (OperationalEventType.WS_PONG_STALE, OperationalEventReasonCode.WS_PONG_TIMEOUT): (
        NarrativeTemplateKey.WS_PONG_STALE,
        "The market data WebSocket missed liveness pings; readiness may degrade.",
        _CONT_DEGRADED,
    ),
    # Readiness
    (OperationalEventType.READY_STATE_CHANGED, OperationalEventReasonCode.READY): (
        NarrativeTemplateKey.READINESS_READY,
        "Readiness returned to READY; trading paths are eligible again.",
        _CONT_CONTINUED,
    ),
    (OperationalEventType.READY_STATE_CHANGED, OperationalEventReasonCode.DEGRADED): (
        NarrativeTemplateKey.READINESS_DEGRADED,
        "Readiness degraded; trading paths are restricted until recovery.",
        _CONT_DEGRADED,
    ),
    (OperationalEventType.READY_STATE_CHANGED, OperationalEventReasonCode.NOT_READY): (
        NarrativeTemplateKey.READINESS_NOT_READY,
        "Readiness moved to NOT_READY; trading paths are not eligible.",
        _CONT_STOPPED,
    ),
    # Budget blocks (LLM_CALL_BLOCKED / BUDGET_BLOCK both supported)
    (OperationalEventType.BUDGET_BLOCK, OperationalEventReasonCode.BUDGET_DAILY): (
        NarrativeTemplateKey.BUDGET_DAILY,
        "An evaluation call was blocked because the daily LLM spend limit was reached.",
        _CONT_SKIPPED,
    ),
    (OperationalEventType.BUDGET_BLOCK, OperationalEventReasonCode.BUDGET_HOURLY): (
        NarrativeTemplateKey.BUDGET_HOURLY,
        "An evaluation call was blocked because the hourly LLM call budget was reached.",
        _CONT_SKIPPED,
    ),
    (OperationalEventType.BUDGET_BLOCK, OperationalEventReasonCode.BUDGET_TOKEN): (
        NarrativeTemplateKey.BUDGET_TOKEN,
        "An evaluation call was blocked because the LLM token budget was reached.",
        _CONT_SKIPPED,
    ),
    (OperationalEventType.BUDGET_BLOCK, OperationalEventReasonCode.BUDGET_COST): (
        NarrativeTemplateKey.BUDGET_COST,
        "An evaluation call was blocked because the LLM cost budget was reached.",
        _CONT_SKIPPED,
    ),
    (OperationalEventType.BUDGET_BLOCK, OperationalEventReasonCode.BUDGET_REFLECTION): (
        NarrativeTemplateKey.BUDGET_REFLECTION,
        "A reflection call was blocked because the reflection budget was reached.",
        _CONT_SKIPPED,
    ),
    # Cooldowns
    (
        OperationalEventType.COOLDOWN_BLOCK,
        OperationalEventReasonCode.COOLDOWN_REPEATED_HOLD,
    ): (
        NarrativeTemplateKey.COOLDOWN_REPEATED_HOLD,
        "A market was temporarily skipped because of repeated low-value HOLD evaluations.",
        _CONT_SKIPPED,
    ),
    (
        OperationalEventType.COOLDOWN_BLOCK,
        OperationalEventReasonCode.COOLDOWN_REPEATED_INVALID,
    ): (
        NarrativeTemplateKey.COOLDOWN_REPEATED_INVALID,
        "A market was temporarily skipped because of repeated invalid provider responses.",
        _CONT_SKIPPED,
    ),
    # Provider failures
    (
        OperationalEventType.PROVIDER_FAILURE,
        OperationalEventReasonCode.PROVIDER_CALL_FAILED,
    ): (
        NarrativeTemplateKey.PROVIDER_CALL_FAILED,
        "The evaluation provider call failed; no trade decision was produced.",
        _CONT_SKIPPED,
    ),
    (
        OperationalEventType.PROVIDER_FAILURE,
        OperationalEventReasonCode.PROVIDER_RESPONSE_MALFORMED,
    ): (
        NarrativeTemplateKey.PROVIDER_RESPONSE_MALFORMED,
        "The evaluation provider returned a malformed response; the bot did not treat it as a valid decision.",
        _CONT_SKIPPED,
    ),
    # Decisions accepted
    (OperationalEventType.DECISION_ACCEPTED, OperationalEventReasonCode.DECISION_BUY): (
        NarrativeTemplateKey.DECISION_ACCEPTED_BUY,
        "An evaluation was accepted with a BUY action.",
        _CONT_CONTINUED,
    ),
    (
        OperationalEventType.DECISION_ACCEPTED,
        OperationalEventReasonCode.DECISION_HOLD,
    ): (
        NarrativeTemplateKey.DECISION_ACCEPTED_HOLD,
        "An evaluation was accepted with a HOLD action.",
        _CONT_CONTINUED,
    ),
    # Decision skips
    (
        OperationalEventType.DECISION_SKIPPED,
        OperationalEventReasonCode.DECISION_SKIP_LOW_CONF,
    ): (
        NarrativeTemplateKey.DECISION_SKIP_LOW_CONF,
        "An evaluation was skipped because confidence was below threshold.",
        _CONT_SKIPPED,
    ),
    (
        OperationalEventType.DECISION_SKIPPED,
        OperationalEventReasonCode.DECISION_SKIP_LOW_EV,
    ): (
        NarrativeTemplateKey.DECISION_SKIP_LOW_EV,
        "An evaluation was skipped because expected value was below threshold.",
        _CONT_SKIPPED,
    ),
    (
        OperationalEventType.DECISION_SKIPPED,
        OperationalEventReasonCode.DECISION_SKIP_HIGH_SPREAD,
    ): (
        NarrativeTemplateKey.DECISION_SKIP_HIGH_SPREAD,
        "An evaluation was skipped because the bid-ask spread was too wide.",
        _CONT_SKIPPED,
    ),
    (
        OperationalEventType.DECISION_SKIPPED,
        OperationalEventReasonCode.DECISION_SKIP_EXPOSURE,
    ): (
        NarrativeTemplateKey.DECISION_SKIP_EXPOSURE,
        "An evaluation was skipped because the exposure limit would have been exceeded.",
        _CONT_SKIPPED,
    ),
    (
        OperationalEventType.DECISION_SKIPPED,
        OperationalEventReasonCode.DECISION_SKIP_TTR,
    ): (
        NarrativeTemplateKey.DECISION_SKIP_TTR,
        "An evaluation was skipped because the time-to-resolution rule blocked entry.",
        _CONT_SKIPPED,
    ),
    # Dry-run execution
    (
        OperationalEventType.EXECUTION_DRY_RUN,
        OperationalEventReasonCode.EXEC_DRY_RUN_SKIP,
    ): (
        NarrativeTemplateKey.EXECUTION_DRY_RUN,
        "Execution was simulated in dry-run mode; no live signing or broadcasting occurred.",
        _CONT_CONTINUED,
    ),
    # Circuit breaker
    (OperationalEventType.CIRCUIT_BREAKER_OPEN, OperationalEventReasonCode.CB_OPEN): (
        NarrativeTemplateKey.CIRCUIT_BREAKER_OPEN,
        "The cognitive circuit breaker opened; new BUY routing is blocked by the safety gate.",
        _CONT_DEGRADED,
    ),
    (
        OperationalEventType.CIRCUIT_BREAKER_CLOSED,
        OperationalEventReasonCode.CB_CLOSED,
    ): (
        NarrativeTemplateKey.CIRCUIT_BREAKER_CLOSED,
        "The cognitive circuit breaker closed; trading paths resumed normal eligibility.",
        _CONT_CONTINUED,
    ),
    (
        OperationalEventType.CIRCUIT_BREAKER_CLOSED,
        OperationalEventReasonCode.CB_OVERRIDE,
    ): (
        NarrativeTemplateKey.CIRCUIT_BREAKER_OVERRIDE,
        "The cognitive circuit breaker was overridden per policy; resume occurred under operator control.",
        _CONT_CONTINUED,
    ),
    # Alerts
    (OperationalEventType.ALERT_SENT, OperationalEventReasonCode.ALERT_DISPATCHED): (
        NarrativeTemplateKey.ALERT_DISPATCHED,
        "An operational alert was dispatched to the notification channel.",
        _CONT_CONTINUED,
    ),
    (
        OperationalEventType.ALERT_SENT,
        OperationalEventReasonCode.ALERT_DISPATCH_FAILED,
    ): (
        NarrativeTemplateKey.ALERT_DISPATCH_FAILED,
        "An operational alert dispatch failed; the underlying condition still applies.",
        _CONT_DEGRADED,
    ),
    # Recovery
    (
        OperationalEventType.ERROR_RECOVERED,
        OperationalEventReasonCode.ERROR_HANDLED,
    ): (
        NarrativeTemplateKey.ERROR_HANDLED,
        "The runtime recovered from a bounded error and continued.",
        _CONT_CONTINUED,
    ),
    (
        OperationalEventType.ERROR_RECOVERED,
        OperationalEventReasonCode.ERROR_UNHANDLED,
    ): (
        NarrativeTemplateKey.ERROR_UNHANDLED,
        "The runtime encountered an unhandled error category and degraded behavior.",
        _CONT_DEGRADED,
    ),
}


# Events whose narratives are surfaced as DecisionNarrative (so future
# digest / dashboard surfaces can group them).
_DECISION_EVENT_TYPES = {
    OperationalEventType.DECISION_ACCEPTED,
    OperationalEventType.DECISION_SKIPPED,
}

_ALLOWED_PAYLOAD_KEYS = frozenset(OperationalEventPayload.model_fields.keys())
_FORBIDDEN_PAYLOAD_KEY_FRAGMENTS = (
    "api_key",
    "condition_id",
    "exception",
    "private_key",
    "prompt",
    "raw_provider_response",
    "raw_response",
    "reasoning",
    "secret",
    "token_id",
    "traceback",
    "wallet",
)
_SAFE_PROVIDER_NAMES = frozenset({"anthropic", "deepseek"})
_SAFE_READY_STATES = frozenset({"READY", "DEGRADED", "NOT_READY", "SHUTDOWN"})
_SAFE_DECISION_ACTIONS = frozenset({"BUY", "HOLD", "SKIP", "SELL"})


# Typed reason_code → aggregate decision_action. The reason_code is the
# authoritative source of truth for the action surfaced on a
# DecisionNarrative; payload-supplied decision_action values are NEVER
# trusted here because a persisted payload could contradict the typed
# reason code (e.g. reason_code=DECISION_BUY with payload={"decision_action":
# "SELL"}). Mapping by reason_code prevents that contradiction.
_REASON_CODE_TO_DECISION_ACTION: dict[OperationalEventReasonCode, str] = {
    OperationalEventReasonCode.DECISION_BUY: "BUY",
    OperationalEventReasonCode.DECISION_HOLD: "HOLD",
    OperationalEventReasonCode.DECISION_SKIP_LOW_CONF: "SKIP",
    OperationalEventReasonCode.DECISION_SKIP_LOW_EV: "SKIP",
    OperationalEventReasonCode.DECISION_SKIP_HIGH_SPREAD: "SKIP",
    OperationalEventReasonCode.DECISION_SKIP_EXPOSURE: "SKIP",
    OperationalEventReasonCode.DECISION_SKIP_TTR: "SKIP",
}


# Inspection hint guidance, by template key.
_INSPECTION_HINTS: dict[NarrativeTemplateKey, NarrativeInspectionHint] = {
    NarrativeTemplateKey.READINESS_DEGRADED: NarrativeInspectionHint(
        component=OperationalEventSource.OBSERVABILITY,
        pointer="readiness",
        severity=OperationalEventSeverity.WARNING,
    ),
    NarrativeTemplateKey.READINESS_NOT_READY: NarrativeInspectionHint(
        component=OperationalEventSource.OBSERVABILITY,
        pointer="readiness",
        severity=OperationalEventSeverity.CRITICAL,
    ),
    NarrativeTemplateKey.WS_PONG_STALE: NarrativeInspectionHint(
        component=OperationalEventSource.INGESTION,
        pointer="websocket",
        severity=OperationalEventSeverity.WARNING,
    ),
    NarrativeTemplateKey.PROVIDER_CALL_FAILED: NarrativeInspectionHint(
        component=OperationalEventSource.EVALUATION,
        pointer="provider",
        severity=OperationalEventSeverity.WARNING,
    ),
    NarrativeTemplateKey.PROVIDER_RESPONSE_MALFORMED: NarrativeInspectionHint(
        component=OperationalEventSource.EVALUATION,
        pointer="provider",
        severity=OperationalEventSeverity.WARNING,
    ),
    NarrativeTemplateKey.BUDGET_DAILY: NarrativeInspectionHint(
        component=OperationalEventSource.EVALUATION,
        pointer="budget",
        severity=OperationalEventSeverity.WARNING,
    ),
    NarrativeTemplateKey.BUDGET_HOURLY: NarrativeInspectionHint(
        component=OperationalEventSource.EVALUATION,
        pointer="budget",
        severity=OperationalEventSeverity.WARNING,
    ),
    NarrativeTemplateKey.BUDGET_TOKEN: NarrativeInspectionHint(
        component=OperationalEventSource.EVALUATION,
        pointer="budget",
        severity=OperationalEventSeverity.WARNING,
    ),
    NarrativeTemplateKey.BUDGET_COST: NarrativeInspectionHint(
        component=OperationalEventSource.EVALUATION,
        pointer="budget",
        severity=OperationalEventSeverity.WARNING,
    ),
    NarrativeTemplateKey.BUDGET_REFLECTION: NarrativeInspectionHint(
        component=OperationalEventSource.EVALUATION,
        pointer="budget",
        severity=OperationalEventSeverity.WARNING,
    ),
    NarrativeTemplateKey.CIRCUIT_BREAKER_OPEN: NarrativeInspectionHint(
        component=OperationalEventSource.EVALUATION,
        pointer="breaker",
        severity=OperationalEventSeverity.CRITICAL,
    ),
    NarrativeTemplateKey.ALERT_DISPATCH_FAILED: NarrativeInspectionHint(
        component=OperationalEventSource.OBSERVABILITY,
        pointer="alerts",
        severity=OperationalEventSeverity.WARNING,
    ),
    NarrativeTemplateKey.ERROR_UNHANDLED: NarrativeInspectionHint(
        component=OperationalEventSource.ORCHESTRATOR,
        pointer="error",
        severity=OperationalEventSeverity.ERROR,
    ),
    NarrativeTemplateKey.MARKET_QUARANTINED: NarrativeInspectionHint(
        component=OperationalEventSource.CONTEXT,
        pointer="quarantine",
        severity=OperationalEventSeverity.WARNING,
    ),
}


# ── Generic fallback summary (unknown but valid combos) ────────────────────


def _generic_summary(
    event_type: OperationalEventType,
    severity: OperationalEventSeverity,
    source: OperationalEventSource,
    reason_code: OperationalEventReasonCode,
) -> str:
    """Conservative summary that exposes only typed enum values."""
    return (
        f"A {severity.value.lower()} operational event of type "
        f"{event_type.value} was recorded by the {source.value.lower()} "
        f"component with reason {reason_code.value}."
    )


# ── Timestamp normalization ────────────────────────────────────────────────


def _normalize_timestamp(value: Optional[datetime]) -> tuple[Optional[datetime], bool]:
    """Return (utc_ts, naive_seen). Naive datetimes are coerced to UTC."""
    if value is None:
        return None, False
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc), True
    return value.astimezone(timezone.utc), False


# ── Payload parsing ────────────────────────────────────────────────────────


def _parse_payload(record: OperationalEventRecord) -> tuple[Optional[dict], bool]:
    """Return (payload_dict, malformed_seen)."""
    raw = record.payload_json or "{}"
    try:
        parsed = json.loads(raw, parse_float=Decimal)
        if not isinstance(parsed, dict):
            return None, True
        return parsed, False
    except (json.JSONDecodeError, ValueError, TypeError):
        return None, True


def _scan_payload_recursive(value: object) -> list[str]:
    """Recursively scan any string content in a parsed payload.

    OperationalEventPayload.model_validator would normally reject
    forbidden content at the schema boundary, but a record can be
    persisted via raw `payload_json` that bypasses the typed payload
    validator. The narrative layer is the last presentation hop before
    operator-facing surfaces, so it must refuse to render any payload
    that contains wallet addresses, raw private keys, telegram tokens,
    api keys, or other forbidden patterns — regardless of whether the
    template would have surfaced them.
    """
    if isinstance(value, str):
        return _scan_event_payload(value)
    if isinstance(value, float):
        return ["forbidden_float"]
    if isinstance(value, dict):
        violations: list[str] = []
        for k, v in value.items():
            if not isinstance(k, str):
                violations.append("non_string_payload_key")
                violations.extend(_scan_payload_recursive(v))
                continue
            violations.extend(_scan_event_payload(k))
            normalized_key = k.lower()
            if k not in _ALLOWED_PAYLOAD_KEYS:
                violations.append(f"unknown_payload_key:{k}")
            for fragment in _FORBIDDEN_PAYLOAD_KEY_FRAGMENTS:
                if fragment in normalized_key:
                    violations.append(f"forbidden_payload_key:{k}")
            violations.extend(_scan_payload_recursive(v))
        return violations
    if isinstance(value, (list, tuple)):
        violations = []
        for item in value:
            violations.extend(_scan_payload_recursive(item))
        return violations
    return []


def _validate_payload_shape(payload: dict) -> list[str]:
    """Validate parsed persisted payload before using any payload-derived text."""
    violations = _scan_payload_recursive(payload)

    provider = payload.get("provider_name")
    if provider is not None and (
        not isinstance(provider, str)
        or provider.strip().lower() not in _SAFE_PROVIDER_NAMES
    ):
        violations.append("unsafe_provider_name")

    ready_state = payload.get("ready_state")
    if ready_state is not None and (
        not isinstance(ready_state, str)
        or ready_state.strip().upper() not in _SAFE_READY_STATES
    ):
        violations.append("unsafe_ready_state")

    decision_action = payload.get("decision_action")
    if decision_action is not None and (
        not isinstance(decision_action, str)
        or decision_action.strip().upper() not in _SAFE_DECISION_ACTIONS
    ):
        violations.append("unsafe_decision_action")

    try:
        OperationalEventPayload.model_validate(payload)
    except ValidationError:
        violations.append("invalid_payload_shape")

    return violations


# ── Public render API ──────────────────────────────────────────────────────


def render_event(record: OperationalEventRecord) -> NarrativeRenderResult:
    """Render a single operational event record into a typed result.

    Never raises for supported `OperationalEventRecord` inputs. On unknown
    mappings, malformed payloads, naive timestamps, or forbidden content,
    returns a typed FALLBACK / FAILED / REDACTED status.
    """
    try:
        return _render_event_inner(record)
    except Exception:  # defensive — narrative must never crash callers
        logger.warning(
            "narrative.render.exception",
            event_type=record.event_type.value,
            reason_code=record.reason_code.value,
        )
        return NarrativeRenderResult(
            status=NarrativeRenderStatus.FAILED,
            failure_reason=NarrativeRenderFailureReason.INVALID_INPUT,
            detail="render_exception",
        )


def _render_event_inner(record: OperationalEventRecord) -> NarrativeRenderResult:
    payload, malformed = _parse_payload(record)
    ts, naive_seen = _normalize_timestamp(record.created_at_utc)

    # If the payload JSON is malformed we still want to provide a
    # typed fallback rather than crash. We do NOT use payload fields
    # in the fallback summary.
    if malformed:
        logger.info(
            "narrative.render.malformed_payload",
            event_type=record.event_type.value,
            reason_code=record.reason_code.value,
        )
        return _build_result(
            record=record,
            template_key=NarrativeTemplateKey.GENERIC,
            summary=_generic_summary(
                record.event_type,
                record.severity,
                record.source,
                record.reason_code,
            ),
            continuation_state=None,
            timestamp_utc=ts,
            dry_run=None,
            status=NarrativeRenderStatus.FALLBACK,
            failure_reason=NarrativeRenderFailureReason.MALFORMED_PAYLOAD_JSON,
            detail="malformed_payload_json",
        )

    # Scan the parsed payload for forbidden secret / high-cardinality
    # content before any template lookup or augmentation. A persisted
    # record can carry raw payload_json that bypassed the typed
    # OperationalEventPayload validator; the narrative layer must fail
    # closed on such records by returning a REDACTED result rather than
    # SUCCESS, even when the template itself would not surface those
    # fields. This enforces WI-57 secret/high-cardinality safety on the
    # input side, not only on the rendered output.
    if payload is not None:
        payload_violations = _validate_payload_shape(payload)
        if payload_violations:
            logger.warning(
                "narrative.render.forbidden_content_in_payload",
                event_type=record.event_type.value,
                reason_code=record.reason_code.value,
            )
            safe_summary = _generic_summary(
                record.event_type,
                record.severity,
                record.source,
                record.reason_code,
            )
            return _build_redacted_result(
                record=record,
                summary=safe_summary,
                timestamp_utc=ts,
            )

    key = (record.event_type, record.reason_code)
    template_entry = _TEMPLATE_REGISTRY.get(key)

    if template_entry is None:
        return _build_result(
            record=record,
            template_key=NarrativeTemplateKey.GENERIC,
            summary=_generic_summary(
                record.event_type,
                record.severity,
                record.source,
                record.reason_code,
            ),
            continuation_state=None,
            timestamp_utc=ts,
            dry_run=_safe_dry_run(payload),
            status=NarrativeRenderStatus.FALLBACK,
            failure_reason=NarrativeRenderFailureReason.UNKNOWN_TEMPLATE,
            detail="unknown_template",
        )

    template_key, base_summary, continuation_state = template_entry
    dry_run = _safe_dry_run(payload)
    summary = _augment_summary(template_key, base_summary, payload)

    failure_reason: Optional[NarrativeRenderFailureReason] = None
    status = NarrativeRenderStatus.SUCCESS
    detail: Optional[str] = None
    if naive_seen:
        failure_reason = NarrativeRenderFailureReason.NAIVE_TIMESTAMP
        status = NarrativeRenderStatus.FALLBACK
        detail = "naive_timestamp_normalized"

    return _build_result(
        record=record,
        template_key=template_key,
        summary=summary,
        continuation_state=continuation_state,
        timestamp_utc=ts,
        dry_run=dry_run,
        status=status,
        failure_reason=failure_reason,
        detail=detail,
    )


def render_window(window: OperationalEventReadWindow) -> list[NarrativeRenderResult]:
    """Render every event in a read window. Each event is independent.

    Determinism: per-event output depends only on the event itself, not on
    surrounding sequence position.
    """
    return [render_event(record) for record in window.events]


# ── Internal helpers ───────────────────────────────────────────────────────


def _safe_dry_run(payload: Optional[dict]) -> Optional[bool]:
    if not payload:
        return None
    value = payload.get("dry_run")
    if isinstance(value, bool):
        return value
    return None


def _augment_summary(
    template_key: NarrativeTemplateKey,
    base: str,
    payload: Optional[dict],
) -> str:
    """Augment a base template with bounded payload-derived clauses.

    Only payload fields that are already validated as low-cardinality and
    secret-safe at the OperationalEventPayload schema boundary may be
    referenced here. We re-scan the resulting summary at the end.
    """
    parts: list[str] = [base]

    if payload is None:
        return base

    # provider_name (already bounded to ≤ 64 chars and secret-scanned)
    provider = payload.get("provider_name")
    if (
        isinstance(provider, str)
        and provider
        and template_key
        in {
            NarrativeTemplateKey.PROVIDER_CALL_FAILED,
            NarrativeTemplateKey.PROVIDER_RESPONSE_MALFORMED,
        }
    ):
        parts.append(f"Provider: {provider}.")

    # ready_state (already bounded to ≤ 16 chars)
    ready = payload.get("ready_state")
    if (
        isinstance(ready, str)
        and ready
        and template_key
        in {
            NarrativeTemplateKey.READINESS_READY,
            NarrativeTemplateKey.READINESS_DEGRADED,
            NarrativeTemplateKey.READINESS_NOT_READY,
        }
    ):
        parts.append(f"State: {ready}.")

    # decision_action is intentionally NOT augmented from the payload
    # here: the base summary already states the action derived from the
    # typed reason_code ("BUY action" / "HOLD action"). Re-inserting the
    # payload value would risk contradicting the reason_code if a
    # persisted payload were inconsistent. See
    # `_REASON_CODE_TO_DECISION_ACTION` and `_extract_decision_action`.

    # market_count (bounded int)
    market_count = payload.get("market_count")
    if (
        isinstance(market_count, int)
        and template_key == NarrativeTemplateKey.MARKET_DISCOVERED
    ):
        parts.append(f"Active markets: {market_count}.")

    # dry_run hint (only for execution template — keeps wording stable)
    if template_key == NarrativeTemplateKey.EXECUTION_DRY_RUN:
        # base already explicitly mentions dry-run; no augmentation needed
        pass

    return " ".join(parts)


def _build_result(
    record: OperationalEventRecord,
    template_key: NarrativeTemplateKey,
    summary: str,
    continuation_state: Optional[str],
    timestamp_utc: Optional[datetime],
    dry_run: Optional[bool],
    status: NarrativeRenderStatus,
    failure_reason: Optional[NarrativeRenderFailureReason],
    detail: Optional[str],
) -> NarrativeRenderResult:
    """Final assembly with mandatory output secret scan."""
    violations = _scan_event_payload(summary)
    if violations:
        logger.warning(
            "narrative.render.forbidden_content_detected",
            event_type=record.event_type.value,
            reason_code=record.reason_code.value,
        )
        safe_summary = _generic_summary(
            record.event_type,
            record.severity,
            record.source,
            record.reason_code,
        )
        return _build_redacted_result(
            record=record,
            summary=safe_summary,
            timestamp_utc=timestamp_utc,
        )

    inspection_hint = _INSPECTION_HINTS.get(template_key)

    if record.event_type in _DECISION_EVENT_TYPES:
        decision_action: Optional[str] = _extract_decision_action(record)

        decision_narrative = DecisionNarrative(
            event_type=record.event_type,
            severity=record.severity,
            source=record.source,
            reason_code=record.reason_code,
            template_key=template_key,
            decision_action=decision_action,
            summary=summary,
            continuation_state=continuation_state,
            inspection_hint=inspection_hint,
            timestamp_utc=timestamp_utc,
            dry_run=dry_run,
        )
        runtime = RuntimeNarrative(kind="decision", decision=decision_narrative)
    else:
        op_narrative = OperationalNarrative(
            event_type=record.event_type,
            severity=record.severity,
            source=record.source,
            reason_code=record.reason_code,
            template_key=template_key,
            summary=summary,
            continuation_state=continuation_state,
            inspection_hint=inspection_hint,
            timestamp_utc=timestamp_utc,
            dry_run=dry_run,
        )
        runtime = RuntimeNarrative(kind="operational", operational=op_narrative)

    return NarrativeRenderResult(
        status=status,
        narrative=runtime,
        failure_reason=failure_reason,
        detail=detail,
    )


def _build_redacted_result(
    record: OperationalEventRecord,
    summary: str,
    timestamp_utc: Optional[datetime],
) -> NarrativeRenderResult:
    """Build a REDACTED result with a safe generic summary."""
    op_narrative = OperationalNarrative(
        event_type=record.event_type,
        severity=record.severity,
        source=record.source,
        reason_code=record.reason_code,
        template_key=NarrativeTemplateKey.GENERIC,
        summary=summary,
        continuation_state=None,
        inspection_hint=None,
        timestamp_utc=timestamp_utc,
        dry_run=None,
    )
    runtime = RuntimeNarrative(kind="operational", operational=op_narrative)
    return NarrativeRenderResult(
        status=NarrativeRenderStatus.REDACTED,
        narrative=runtime,
        failure_reason=NarrativeRenderFailureReason.FORBIDDEN_CONTENT,
        detail="forbidden_content_redacted",
    )


def _extract_decision_action(record: OperationalEventRecord) -> Optional[str]:
    """Return the aggregate decision action derived from the typed reason code.

    The reason_code is the authoritative source of truth. The payload
    `decision_action` field is intentionally ignored here to prevent a
    persisted record from contradicting the typed reason code (for
    example reason_code=DECISION_BUY with payload={"decision_action":
    "SELL"} must never surface SELL as the decision action).
    """
    return _REASON_CODE_TO_DECISION_ACTION.get(record.reason_code)
