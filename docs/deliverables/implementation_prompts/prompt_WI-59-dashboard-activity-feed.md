# Implementation Prompt - WI-59 Dashboard Activity Feed

## Session Context

You are working in `poly-oracle-agent` on Phase 16: Operator Clarity and Runtime Audit Trail.

Current baseline:

- Phase 16 is in progress.
- WI-56 is complete and introduced the SQLite-backed append-only operational event ledger, `OperationalEventRepository`, `OperationalEventBus`, operational event schemas/enums in `src/schemas/ops.py`, representative runtime hooks, and `docs/runbooks/operational-event-ledger.md`.
- WI-57 is complete and introduced deterministic, secret-safe narrative rendering in `src/observability/operational_narratives.py` with presentation schemas in `src/schemas/ops.py`.
- WI-58 is complete and introduced a read-only incident replay service in `src/observability/incident_replay.py`, replay schemas in `src/schemas/ops.py`, `scripts/ops/replay.py`, and `docs/runbooks/incident-replay.md`.
- The existing Streamlit dashboard lives in `src/ui/dashboard.py`, uses direct read-only SQLite URI access, caches read queries with a short TTL, and renders performance, PnL, decision audit, and market watch sections.
- WI-59 adds the dashboard-facing activity timeline and current-state panel over the same operational ledger and deterministic narrative layer.
- `DRY_RUN=false` remains out of scope. Live signing, live broadcasting, and execution paths that bypass `LLMEvaluationResponse` remain forbidden.
- `LLMEvaluationResponse` remains the terminal Gatekeeper before execution and must not receive presentation fields.
- The dashboard surface is read-only. It must not append, mutate, delete, backfill, or repair operational events.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v16.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-59-dashboard-activity-feed.md`
- `docs/deliverables/business_logic/business_logic_WI-58-incident-replay-cli.md`
- `docs/deliverables/implementation_prompts/prompt_WI-58-incident-replay-cli.md`
- `docs/deliverables/business_logic/business_logic_WI-57-deterministic-human-narratives.md`
- `docs/deliverables/implementation_prompts/prompt_WI-57-deterministic-human-narratives.md`
- `docs/deliverables/business_logic/business_logic_WI-56-operational-event-ledger.md`
- `docs/deliverables/implementation_prompts/prompt_WI-56-operational-event-ledger.md`
- `src/ui/dashboard.py`
- `src/schemas/ops.py`
- `src/db/repositories/operational_event_repository.py`
- `src/observability/operational_narratives.py`
- `src/observability/incident_replay.py`
- `tests/unit/test_WI-58-incident-replay-cli.py`
- `tests/integration/test_WI-58-incident-replay-cli.py`

## Objective

Build a read-only dashboard activity timeline and current-state panel backed by the operational event ledger. The dashboard must render recent runtime events using deterministic WI-57 narratives, handle missing or empty ledgers gracefully, preserve read-only SQLite behavior, and show a bounded operator answer to what the bot is doing right now.

## Inputs

- Existing `DASHBOARD_DB_PATH` and read-only SQLite URI behavior in `src/ui/dashboard.py`.
- Existing `fetch_table_names()` and `get_connection()` dashboard helpers.
- Existing `operational_events` table created by WI-56.
- Existing operational event enums and read-window schemas in `src/schemas/ops.py`.
- Existing deterministic narrative renderer from WI-57.
- Existing replay line and summary schemas from WI-58, where reusable.
- Optional low-cardinality dashboard filters: severity, source component, event type, and reason code.
- Existing Streamlit cache TTL and manual cache refresh pattern.
- Existing secret/high-cardinality scanning helpers in `src/schemas/ops.py`.
- Phase 16 PRD requirements for dashboard timeline visibility and future WI-60 digest reuse.

## Outputs

- `src/ui/dashboard.py` updated with an activity timeline section.
- `src/ui/dashboard.py` updated with a "What is the bot doing right now?" current-state panel.
- Dashboard feed/result/current-state schemas in `src/schemas/ops.py`, if not already present.
- Read-only dashboard fetch/format helpers for recent operational events.
- Timeline rows containing timestamp, severity, component/source, event type, reason code, narrative status, and human summary.
- Current-state output derived from recent operational events only.
- Graceful dashboard empty state for missing `operational_events`.
- Graceful dashboard empty state for zero matching events.
- Secret-safe rendering and HTML escaping for all displayed event values.
- `tests/unit/test_WI-59-dashboard-activity-feed.py`.
- `tests/integration/test_WI-59-dashboard-activity-feed.py`.

## Acceptance Criteria

1. Dashboard renders a clearly labeled runtime or activity timeline section.
2. Activity timeline reads from the operational event ledger.
3. Dashboard preserves existing read-only SQLite URI behavior with `mode=ro`.
4. Dashboard does not create the SQLite database when it is absent.
5. Dashboard does not write to `operational_events` or any other table.
6. Missing `operational_events` table renders a graceful unavailable or empty-state panel.
7. Existing empty database behavior continues to render without crashing.
8. Empty `operational_events` table renders a valid zero-event timeline.
9. Timeline rows include timestamp.
10. Timeline rows include severity.
11. Timeline rows include source component.
12. Timeline rows include event type.
13. Timeline rows include reason code.
14. Timeline rows include a deterministic WI-57 human summary.
15. Timeline rows include narrative fallback/redaction status when useful for operator interpretation.
16. Timeline ordering is deterministic and test-covered.
17. Multiple events with identical timestamps render in stable order.
18. Timeline query is bounded by a configured or hard-coded safe limit no larger than the repository/query cap.
19. Streamlit caching or polling remains compatible with the existing short-TTL dashboard pattern.
20. Manual refresh continues to clear cached dashboard data.
21. Dashboard renders a "What is the bot doing right now?" panel.
22. Current-state panel derives state from recent operational events only.
23. Current-state panel handles no-event state without inventing readiness, provider, market, decision, or dry-run data.
24. Current-state panel summarizes latest lifecycle state when available.
25. Current-state panel summarizes latest readiness state when available.
26. Current-state panel summarizes latest WebSocket health event when available.
27. Current-state panel summarizes latest LLM/provider/budget/cooldown event when available.
28. Current-state panel summarizes latest decision event when available.
29. Current-state panel summarizes latest dry-run execution event when available and preserves that no live signing or broadcasting occurred.
30. Current-state panel summarizes latest circuit-breaker or alert state when available.
31. Timeline output uses deterministic narrative rendering from WI-57 rather than free-form payload text.
32. Timeline and current-state output never include raw prompts, private reasoning, raw provider responses, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, raw exception messages, market identifiers, or other high-cardinality identifiers.
33. Output is scanned or validated for forbidden content before rendering.
34. All displayed values are escaped before custom HTML rendering.
35. Dashboard filters, if added, use low-cardinality typed values only.
36. Filtered zero-result windows render a valid zero-event state.
37. Malformed payload JSON in persisted rows produces a safe fallback or redacted row and does not crash the dashboard.
38. Unknown but valid event/reason combinations render WI-57 conservative generic summaries.
39. Dashboard code does not call Claude, DeepSeek, Grok, or any LLM.
40. Dashboard code does not import or invoke execution routing, transaction signing, order broadcasting, order placement, or live wallet mutation paths.
41. Dashboard code does not append, update, delete, repair, or backfill operational events.
42. `OperationalEventRepository` remains append/read-only and does not gain public update/delete methods.
43. No `Base.metadata.create_all()` is introduced in runtime or dashboard paths.
44. No live trading, signing, broadcasting, or `DRY_RUN=false` behavior is added or changed.
45. `LLMEvaluationResponse` is not modified and receives no presentation fields.
46. No trading calculation, sizing calculation, EV calculation, Kelly calculation, PnL calculation, exposure calculation, provider-cost calculation, or Gatekeeper decision is performed by the activity feed.
47. Decimal-bearing values used by existing dashboard sections remain Decimal-safe at conversion boundaries.
48. Logs and metrics from the feed, if any, use low-cardinality labels only.
49. Existing dashboard metrics, PnL chart, decision audit, and market watch sections remain functional.
50. Targeted WI-59 unit and integration tests pass.
51. Full regression remains compatible with the documented baseline and coverage stays at or above 80%.
52. MAAP is run before commit for any change touching `src/schemas/`, `src/db/`, `src/observability/`, `src/orchestrator.py`, `src/agents/`, or other core logic.

## Anti-Patterns

- Do not enable live trading.
- Do not change `DRY_RUN=false` behavior.
- Do not add live signing or broadcasting.
- Do not bypass `LLMEvaluationResponse`.
- Do not add presentation fields to `LLMEvaluationResponse`.
- Do not use Claude, DeepSeek, Grok, or any LLM to generate dashboard text.
- Do not derive dashboard summaries from raw prompts, raw reasoning, raw provider responses, raw exception messages, token IDs, condition IDs, wallet addresses, or secrets.
- Do not expose high-cardinality identifiers in timeline rows, current-state panels, filters, logs, metrics labels, or tests.
- Do not write operational events from dashboard code.
- Do not add update or delete behavior to `OperationalEventRepository`.
- Do not use writable SQLite connections for dashboard reads.
- Do not call `Base.metadata.create_all()` in runtime or dashboard paths.
- Do not add new database tables or migrations for this WI unless implementation proves they are strictly necessary and is approved first.
- Do not implement the daily operations digest in this WI.
- Do not invoke the incident replay CLI as a subprocess from Streamlit.
- Do not introduce new external package dependencies.
- Do not use raw `float` for money, spend, EV, Kelly, PnL, price, sizing, exposure, or token-cost values.
- Do not allow malformed payload JSON or narrative render failures to crash dashboard rendering.
- Do not print raw stack traces, SQL text, connection strings, or unbounded exception messages to operators.

## Dependencies

- Phase 16 PRD (`docs/PRD-v16.0.md`).
- WI-56 operational event ledger deliverables and implementation.
- WI-57 deterministic narrative deliverables and implementation.
- WI-58 incident replay deliverables and implementation.
- Existing `operational_events` table shape and persisted event fields.
- Existing `OperationalEventQuery`, `OperationalEventReadWindow`, `OperationalEventRecord`, `OperationalEventPayload`, and operational event enums.
- Existing `NarrativeRenderResult` and `render_event`/`render_window` narrative helpers.
- Existing `IncidentReplayLine` and `IncidentReplaySummary` schemas, where reusable for feed formatting.
- Existing Streamlit dashboard read-only SQLite access pattern.
- Existing dashboard theme, table, empty-state, cache, and refresh helpers.
- Existing `structlog` logging conventions.
- Existing low-cardinality and secret/high-cardinality scan constraints.
- Future WI-60 daily operations digest.

## Target Layer

Read-only Streamlit dashboard and observability presentation layer over the operational event ledger. This WI adds operator timeline visibility and current runtime-state interpretation. It must not change ingestion, prompt construction, LLM evaluation semantics, Gatekeeper authority, execution routing, live-trading authorization, signing, broadcasting, or database write semantics.
