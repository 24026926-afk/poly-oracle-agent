# Business Logic - WI-58 Incident Replay CLI

## Objective

Add a read-only incident replay CLI that reconstructs a bounded UTC time window from the durable operational event ledger and renders a chronological, human-readable account of what the bot did during an incident.

This WI consumes the WI-56 append-only operational event ledger and the WI-57 deterministic narrative layer. It must not mutate operational events, bypass repository boundaries, expose raw prompts or identifiers, change trading behavior, enable live trading, or weaken `LLMEvaluationResponse` as the terminal Gatekeeper before execution.

## Data Models

Pydantic schema names only:

- `OperationalEventRecord`
- `OperationalEventReadWindow`
- `OperationalEventQuery`
- `OperationalEventType`
- `OperationalEventSeverity`
- `OperationalEventSource`
- `OperationalEventReasonCode`
- `OperationalEventPayload`
- `OperationalNarrative`
- `DecisionNarrative`
- `RuntimeNarrative`
- `NarrativeRenderResult`
- `NarrativeRenderStatus`
- `NarrativeRenderFailureReason`
- `IncidentReplayFilter`
- `IncidentReplayRequest`
- `IncidentReplayLine`
- `IncidentReplaySummary`
- `IncidentReplayReport`
- `IncidentReplayStatus`
- `IncidentReplayFailureReason`

## Key Rules

1. Incident replay is read-only. It must never append, update, delete, backfill, or repair operational events.
2. All event reads must route through `OperationalEventRepository` or a repository-backed read service.
3. CLI code must not hold raw database sessions outside repository or repository-factory boundaries.
4. Replay windows are bounded by explicit `--from` and `--to` ISO-8601 timestamps.
5. Input timestamps must be timezone-aware or safely normalized to UTC according to typed validation rules.
6. `--from` must be earlier than or equal to `--to`; invalid windows fail with a clear non-zero CLI result.
7. Output ordering must be chronological by event creation time, regardless of repository default ordering.
8. Replay lines must be generated from `NarrativeRenderResult` values produced by the deterministic narrative layer.
9. The CLI must support independent and combined filters for severity, source component, event type, and reason code.
10. Empty windows are valid. They return a zero-event report with summary counts set to zero.
11. Summary counts must include total events, warnings/errors, markets seen, decisions by action, skips by reason, LLM calls, budget blocks, cooldown blocks, provider failures, and readiness changes.
12. Summary counts must be derived from typed event fields and bounded payload fields only.
13. `markets_seen` may count bounded market-discovery or market-rejection events, but must not print token IDs, condition IDs, raw market identifiers, or high-cardinality names.
14. Decisions by action must use typed aggregate actions or typed reason-code mappings, not raw LLM reasoning or prompt text.
15. Skips by reason must use stable reason codes or narrative-safe skip categories.
16. Replay output must never expose raw prompts, private reasoning, raw provider responses, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, raw exception messages, or other high-cardinality identifiers.
17. Replay output must be scanned or validated for forbidden content before it is printed.
18. Narrative rendering failures must appear as typed fallback or redacted replay lines. They must not crash valid replay windows.
19. Invalid CLI arguments, invalid enum filters, missing database configuration, and invalid timestamp windows return non-zero results with safe low-cardinality error text.
20. Missing or empty `operational_events` data returns a valid empty report when the database itself is reachable.
21. Database connectivity or repository failures return typed CLI failures. Error text must not leak connection strings, paths containing secrets, SQL text, or raw exception messages.
22. All financial, pricing, EV, PnL, spend, sizing, exposure, and token-cost values in replay inputs remain Decimal-safe at schema boundaries.
23. Replay formatting may display Decimal-derived values, but must not perform trading, sizing, EV, Kelly, PnL, exposure, or provider-cost calculations.
24. The CLI must not call Claude, DeepSeek, Grok, or any other LLM.
25. The CLI must not import or invoke execution routing, signing, broadcasting, order placement, or live wallet mutation paths.
26. Metrics and logs, if added, must use low-cardinality labels only.
27. The runbook must explain common replay commands, filters, empty-window behavior, invalid-window behavior, safe output guarantees, and how operators should use replay after incidents.

## Edge Cases

1. `--from` is after `--to`: fail non-zero with a typed invalid-window result and safe message.
2. Timestamp lacks timezone information: validation either rejects it or normalizes it to UTC explicitly and consistently.
3. Timestamp has a non-UTC offset: normalize to UTC before repository query.
4. Timestamp is malformed: fail non-zero with safe argument-validation text.
5. A filter value is not a valid enum: fail non-zero with allowed stable enum values, not raw stack traces.
6. No events exist in the window: print a valid zero-event replay report.
7. Filters exclude all events: print a valid zero-event replay report that includes the active filters.
8. Repository returns events in descending order: replay output re-sorts into chronological order before rendering.
9. Multiple events share the same timestamp: ordering remains deterministic using stable persisted event fields when available.
10. Persisted payload JSON is malformed: render a typed fallback or redacted line and continue the replay.
11. Persisted payload contains forbidden content despite earlier validation: render a redacted or failed line and do not print unsafe content.
12. Narrative rendering returns fallback or redacted status: include a safe line and count the event without exposing raw payload values.
13. Database is missing the `operational_events` table in an older deployment: fail safely or return the documented empty-state behavior only if the repository can distinguish that state.
14. Database connection fails: fail non-zero with a bounded failure reason.
15. Large result windows exceed the configured limit: return a bounded report with `has_more` semantics or a typed truncation indicator.
16. Combined filters are contradictory: return a valid zero-event report.
17. Event has unknown but valid type/reason combination: render the WI-57 conservative generic narrative.
18. Event contains warning/error severity: include it in warning/error summary counts using typed severity only.
19. Event indicates budget, cooldown, provider, or readiness transition: increment the corresponding summary count using typed event type and reason code.
20. Event indicates dry-run execution: replay must preserve the operator-visible fact that no live signing or broadcasting occurred.

## Invariants

1. Incident replay is read-only and repository-backed.
2. Operational events remain append-only.
3. No raw database sessions are used in CLI business logic outside repository boundaries.
4. Replay output is deterministic for the same event window and filters.
5. Replay output is chronological.
6. Replay output is secret-safe and high-cardinality-safe.
7. Invalid input fails closed with typed non-zero results.
8. Empty windows are valid and audit-friendly.
9. Narrative failures do not crash valid replay windows.
10. `LLMEvaluationResponse` remains the Gatekeeper and is not modified.
11. No live trading, signing, broadcasting, or `DRY_RUN=false` behavior is added or changed.
12. No LLM generates replay text.
13. Decimal integrity is preserved for any money, EV, PnL, price, spend, sizing, exposure, or token-cost values that appear in typed replay inputs.
14. Logs and metrics remain low-cardinality.
15. Tests cover timestamp validation, filtering, ordering, summary counts, empty windows, invalid input, repository-read behavior, narrative fallback behavior, and secret/high-cardinality redaction.
