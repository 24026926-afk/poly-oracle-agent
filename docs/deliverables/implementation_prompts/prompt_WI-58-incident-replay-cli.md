# Implementation Prompt - WI-58 Incident Replay CLI

## Session Context

You are working in `poly-oracle-agent` on Phase 16: Operator Clarity and Runtime Audit Trail.

Current baseline:

- Phase 16 is in progress.
- WI-56 is complete and introduced the SQLite-backed append-only operational event ledger, `OperationalEventRepository`, `OperationalEventBus`, operational event schemas/enums in `src/schemas/ops.py`, representative runtime hooks, and `docs/runbooks/operational-event-ledger.md`.
- WI-57 is complete and introduced deterministic, secret-safe narrative rendering in `src/observability/operational_narratives.py` with presentation schemas in `src/schemas/ops.py`.
- Phase 16 exists because DigitalOcean dry-run paper-trading exposed that stdout and Docker logs were ephemeral and hard for a non-technical operator to reconstruct.
- WI-58 builds the first operator-facing replay surface over the ledger and narrative layers.
- `DRY_RUN=false` remains out of scope. Live signing, live broadcasting, and execution paths that bypass `LLMEvaluationResponse` remain forbidden.
- `LLMEvaluationResponse` remains the terminal Gatekeeper before execution and must not receive presentation fields.
- Incident replay is read-only. It must not append, mutate, delete, backfill, or repair operational events.
- Runtime persistence remains repository-only. Replay reads must route through `OperationalEventRepository` or a repository-backed read service.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v16.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-58-incident-replay-cli.md`
- `docs/deliverables/business_logic/business_logic_WI-57-deterministic-human-narratives.md`
- `docs/deliverables/implementation_prompts/prompt_WI-57-deterministic-human-narratives.md`
- `docs/deliverables/business_logic/business_logic_WI-56-operational-event-ledger.md`
- `docs/deliverables/implementation_prompts/prompt_WI-56-operational-event-ledger.md`
- `src/schemas/ops.py`
- `src/db/engine.py`
- `src/db/repositories/operational_event_repository.py`
- `src/observability/operational_narratives.py`
- `src/observability/operational_event_bus.py`
- `src/observability/metrics.py`
- `scripts/ops/collect_soak_evidence.py`
- `scripts/run_llm_provider_comparison.py`
- `tests/unit/test_WI-57-deterministic-human-narratives.py`
- `tests/integration/test_WI-57-deterministic-human-narratives.py`

## Objective

Build a read-only incident replay CLI that accepts a UTC time window, reads matching operational events through the repository layer, renders deterministic narrative lines in chronological order, applies optional typed filters, and prints a secret-safe summary report that helps an operator answer what happened during an incident.

## Inputs

- `--from` ISO-8601 timestamp.
- `--to` ISO-8601 timestamp.
- Optional severity filter values.
- Optional component/source filter values.
- Optional event type filter values.
- Optional reason code filter values.
- Existing `OperationalEventRepository.read_window()` behavior.
- Existing `OperationalEventQuery` and `OperationalEventReadWindow` schemas.
- Existing operational event enums in `src/schemas/ops.py`.
- Existing deterministic narrative renderer from WI-57.
- Existing database configuration and async SQLAlchemy session factory.
- Existing secret/high-cardinality scanning helpers in `src/schemas/ops.py`.
- Phase 16 PRD requirements for incident replay and future dashboard/digest reuse.

## Outputs

- `scripts/ops/replay.py`.
- Incident replay request, filter, line, summary, report, status, and failure-reason schemas in `src/schemas/ops.py`, if not already present.
- Repository-backed replay read/format helper in an appropriate observability or script-support module, if needed.
- Chronological human-readable replay output.
- Summary counts for total events, warnings/errors, markets seen, decisions by action, skips by reason, LLM calls, budget blocks, cooldown blocks, provider failures, and readiness changes.
- Safe non-zero CLI failures for invalid timestamps, invalid windows, invalid filters, and repository/database failures.
- Safe zero-event report for valid empty windows.
- `docs/runbooks/incident-replay.md`.
- `tests/unit/test_WI-58-incident-replay-cli.py`.
- `tests/integration/test_WI-58-incident-replay-cli.py`.

## Acceptance Criteria

1. CLI accepts `--from` and `--to` ISO-8601 timestamps.
2. Timestamp inputs are timezone-aware or explicitly normalized to UTC by typed validation.
3. `--from` later than `--to` returns non-zero with safe error text.
4. Malformed timestamps return non-zero with safe error text.
5. CLI supports severity filters.
6. CLI supports component/source filters.
7. CLI supports event type filters.
8. CLI supports reason code filters.
9. Filters work independently.
10. Filters work in combination.
11. Event reads go through `OperationalEventRepository` or a repository-backed read service.
12. CLI business logic does not use raw database sessions outside repository boundaries.
13. Replay output is chronological by event creation time.
14. Replay lines use deterministic narratives from WI-57.
15. Unknown but valid event/reason combinations render conservative generic summaries.
16. Narrative fallback/redaction statuses produce safe replay lines and do not crash the replay.
17. Empty windows produce a valid zero-event report.
18. Filtered zero-result windows produce a valid zero-event report.
19. Summary counts include total events.
20. Summary counts include warnings/errors.
21. Summary counts include markets seen without printing token IDs, condition IDs, raw market IDs, or high-cardinality names.
22. Summary counts include decisions by action using typed action or reason-code mappings only.
23. Summary counts include skips by reason using stable typed reasons only.
24. Summary counts include LLM calls.
25. Summary counts include budget blocks.
26. Summary counts include cooldown blocks.
27. Summary counts include provider failures.
28. Summary counts include readiness changes.
29. Replay output never includes raw prompts, private reasoning, raw provider responses, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, raw exception messages, or high-cardinality identifiers.
30. Replay output is scanned or validated for forbidden content before printing.
31. Database connection or repository failures return non-zero with typed, low-cardinality error text.
32. Missing or unavailable event table behavior is documented and test-covered.
33. The CLI does not call Claude, DeepSeek, Grok, or any LLM.
34. The CLI does not import or invoke execution routing, transaction signing, order broadcasting, or live wallet mutation paths.
35. The CLI does not append, update, delete, or backfill operational events.
36. `OperationalEventRepository` remains append/read-only and does not gain public update/delete methods.
37. No `Base.metadata.create_all()` is introduced in runtime or CLI paths.
38. No live trading, signing, broadcasting, or `DRY_RUN=false` behavior is added or changed.
39. No trading calculation, sizing calculation, EV calculation, Kelly calculation, PnL calculation, exposure calculation, or Gatekeeper decision is performed by replay code.
40. Decimal-bearing replay inputs remain Decimal-safe at schema boundaries.
41. Logs and metrics from replay, if any, use low-cardinality labels only.
42. `docs/runbooks/incident-replay.md` explains command usage, filters, empty windows, invalid windows, safe output guarantees, and incident-response workflow.
43. Targeted WI-58 unit and integration tests pass.
44. Full regression remains compatible with the documented baseline and coverage stays at or above 80%.
45. MAAP is run before commit for any change touching `src/schemas/`, `src/db/`, `src/observability/`, `src/orchestrator.py`, `src/agents/`, or other core logic.

## Anti-Patterns

- Do not enable live trading.
- Do not change `DRY_RUN=false` behavior.
- Do not add live signing or broadcasting.
- Do not bypass `LLMEvaluationResponse`.
- Do not add presentation fields to `LLMEvaluationResponse`.
- Do not use Claude, DeepSeek, Grok, or any LLM to generate replay text.
- Do not derive replay text from raw prompts, raw reasoning, raw provider responses, raw exception messages, token IDs, condition IDs, wallet addresses, or secrets.
- Do not expose high-cardinality identifiers in replay lines, summaries, logs, metrics labels, tests, dashboard-ready structures, or future digest inputs.
- Do not write operational events from the replay CLI.
- Do not add update or delete behavior to `OperationalEventRepository`.
- Do not use raw database sessions in CLI business logic outside repository boundaries.
- Do not call `Base.metadata.create_all()` in runtime or CLI paths.
- Do not add new database tables or migrations for this WI unless implementation proves they are strictly necessary and is approved first.
- Do not implement the dashboard activity feed or daily operations digest in this WI.
- Do not introduce new external package dependencies.
- Do not use raw `float` for money, spend, EV, Kelly, PnL, price, sizing, exposure, or token-cost values.
- Do not allow malformed payload JSON or narrative render failures to crash a valid replay window.
- Do not print raw stack traces, SQL text, connection strings, or unbounded exception messages to operators.

## Dependencies

- Phase 16 PRD (`docs/PRD-v16.0.md`).
- WI-56 operational event ledger deliverables and implementation.
- WI-57 deterministic narrative deliverables and implementation.
- Existing `OperationalEventRepository.read_window()` contract.
- Existing `OperationalEventQuery`, `OperationalEventReadWindow`, `OperationalEventRecord`, `OperationalEventPayload`, and operational event enums.
- Existing `NarrativeRenderResult` and `render_event`/`render_window` narrative helpers.
- Existing async SQLAlchemy database engine/session setup.
- Existing `structlog` logging conventions.
- Existing metrics registry and low-cardinality label constraints.
- Existing scripts under `scripts/ops/` for CLI style and safe report behavior.
- Future WI-59 dashboard activity feed and WI-60 daily operations digest.

## Target Layer

Operations CLI and observability presentation layer over the operational event ledger. This WI adds a read-only incident replay surface for operators. It must not change ingestion, prompt construction, LLM evaluation semantics, Gatekeeper authority, execution routing, live-trading authorization, signing, broadcasting, or database write semantics.
