# Business Logic - WI-59 Dashboard Activity Feed

## Objective

Add a read-only Streamlit dashboard activity feed that turns the durable operational event ledger into an operator-friendly runtime timeline and a current-state panel answering what the bot is doing right now.

This WI consumes the WI-56 append-only operational event ledger, the WI-57 deterministic narrative layer, and the WI-58 replay-ready typed presentation surfaces. It must preserve the dashboard's read-only SQLite URI behavior, avoid writes, expose no prompts or high-cardinality identifiers, and leave trading, Gatekeeper, signing, broadcasting, and `DRY_RUN=false` behavior unchanged.

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
- `NarrativeTemplateKey`
- `IncidentReplayLine`
- `IncidentReplaySummary`
- `DashboardActivityFeedStatus`
- `DashboardActivityFeedFailureReason`
- `DashboardActivityFeedFilter`
- `DashboardActivityFeedItem`
- `DashboardCurrentState`
- `DashboardActivityFeedResult`

## Key Rules

1. The dashboard activity feed is read-only. It must never append, update, delete, backfill, repair, or acknowledge operational events.
2. The data source is the `operational_events` ledger introduced in WI-56.
3. Dashboard database access must preserve the existing SQLite URI `mode=ro` behavior so write attempts are rejected at the connection level.
4. Missing `operational_events` tables are expected during older or freshly provisioned deployments and must render a graceful empty or unavailable state, not a crash.
5. Empty ledgers must render a clear empty timeline and a current-state panel that states no operational activity has been recorded yet.
6. Activity feed rows must be derived from typed operational event fields and deterministic WI-57 narrative output.
7. Timeline rows must include timestamp, severity, component/source, event type, reason code, and human summary.
8. Timeline ordering must be deterministic. Recent-first is allowed for dashboard scanning; chronological ordering is allowed if explicitly chosen and test-covered.
9. The current-state panel must derive state only from recent persisted events. It must not invent readiness, provider, market, decision, dry-run, or execution state when no typed event supports it.
10. Current-state derivation may summarize the latest lifecycle event, latest readiness transition, latest WebSocket health event, latest LLM/provider event, latest decision event, latest budget/cooldown block, latest circuit-breaker event, and latest alert event.
11. The current-state panel must explain whether the bot appears continued, skipped, degraded, stopped, or unknown based on typed event/narrative fields only.
12. The dashboard may use Streamlit caching or polling compatible with the existing 30-second cache pattern. Server-sent events and long-lived push channels are out of scope.
13. Operator filters, if provided, must use low-cardinality typed values only: severity, source component, event type, and reason code.
14. Timeline and panel text must never expose raw prompts, private reasoning, raw provider responses, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, raw exception messages, market identifiers, or other high-cardinality identifiers.
15. Any payload-derived values used in dashboard rows must pass the existing secret/high-cardinality scan before rendering.
16. Narrative rendering failures must produce typed fallback, redacted, failed, or empty-state rows. They must not crash the dashboard.
17. Dashboard helper functions may transform read-only ledger rows into typed feed items, but must not perform trading, sizing, EV, Kelly, PnL, exposure, or provider-cost calculations.
18. Existing dashboard sections for metrics, PnL, decision audit, and market watch must keep working unless a change is strictly necessary for layout integration.
19. Dashboard labels and summaries must remain operator-friendly, bounded, and low-cardinality.
20. The dashboard must not call Claude, DeepSeek, Grok, or any other LLM.
21. The dashboard must not import or invoke execution routing, signing, order broadcasting, wallet mutation, or live order placement paths.
22. The dashboard must not modify `LLMEvaluationResponse` or add presentation fields to cognitive or financial Gatekeeper schemas.
23. If financial values are displayed elsewhere in the dashboard as part of existing sections, Decimal integrity must be preserved at conversion boundaries.
24. Tests must cover read-only database behavior, missing table behavior, empty state, event formatting, current-state derivation, deterministic ordering, narrative fallback, and redaction.

## Edge Cases

1. SQLite database file does not exist: the dashboard renders the existing offline/degraded state and does not create the file.
2. Database exists but `operational_events` does not: activity feed renders a graceful ledger-unavailable state.
3. `operational_events` exists but contains no rows: activity feed renders an empty timeline and unknown current state.
4. Recent events exceed the configured limit: the feed returns only the bounded recent window and displays a bounded "more available" indicator if supported.
5. Repository or read helper returns rows newest-first: rendering preserves the chosen deterministic order and tests assert it.
6. Multiple events share the same timestamp: ordering is stable using persisted event id when available.
7. A row has malformed `payload_json`: narrative rendering falls back or redacts; dashboard still renders safe typed context.
8. A row payload contains forbidden content despite earlier validation: the dashboard drops or redacts unsafe summary text and never prints the unsafe value.
9. A narrative render returns `FAILED`: the timeline shows a generic safe fallback or omits the row according to typed failure behavior.
10. A latest readiness event says `DEGRADED`: current-state panel reflects degraded state and points to readiness/source category only.
11. A latest readiness event says `NOT_READY`: current-state panel reflects stopped or not-ready state without implying live execution.
12. A latest execution event is `EXECUTION_DRY_RUN`: current-state panel preserves that execution was simulated and no live signing or broadcasting occurred.
13. Latest LLM events are budget or cooldown blocks: current-state panel states evaluation was skipped or blocked by the typed guard.
14. Latest provider failure exists: panel summarizes provider failure without raw exception text or provider response.
15. Latest decision is accepted or skipped: panel summarizes aggregate action or typed skip reason only.
16. Latest market event contains only bounded counts: feed may show counts; it must not show token IDs, condition IDs, raw market ids, or unbounded market names.
17. Streamlit cache contains stale rows after manual refresh: existing refresh behavior clears cached data and re-reads from read-only SQLite.
18. Dashboard rendering receives unexpected enum values from an older database row: the row is treated as invalid/unavailable and no raw value is printed.
19. Filtering excludes all rows: dashboard renders a valid zero-result feed instead of an error.
20. Dashboard HTML rendering escapes all displayed cell values before injecting custom markup.

## Invariants

1. Dashboard activity feed is read-only.
2. SQLite dashboard access remains `mode=ro`.
3. Operational events remain append-only and repository-owned for writes.
4. Activity feed output is deterministic for the same persisted event set and filter.
5. Current-state output is derived only from recent typed events.
6. Timeline and current-state text are secret-safe and high-cardinality-safe.
7. WI-57 deterministic narratives are the source of human summaries.
8. Missing or empty ledgers are valid operator states.
9. Narrative failures do not crash the dashboard.
10. `LLMEvaluationResponse` remains the Gatekeeper and is not modified.
11. No live trading, signing, broadcasting, or `DRY_RUN=false` behavior is added or changed.
12. No LLM generates dashboard text.
13. No new external dependencies are introduced unless implementation proves they are necessary and receives explicit approval first.
14. Dashboard filters and labels remain low-cardinality.
15. Decimal integrity is preserved for any money, EV, PnL, price, spend, sizing, exposure, or token-cost values that appear in dashboard inputs.
16. Tests cover read-only behavior, empty/missing ledger states, deterministic timeline formatting, current-state derivation, redaction, and no Gatekeeper pollution.
