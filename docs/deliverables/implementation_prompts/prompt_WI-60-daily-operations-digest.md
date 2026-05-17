# Implementation Prompt - WI-60 Daily Operations Digest

## Session Context

You are working in `poly-oracle-agent` on Phase 16: Operator Clarity and Runtime Audit Trail.

Current baseline:

- Phase 16 is in progress and WI-60 is the final planned work item.
- WI-56 is complete and introduced the SQLite-backed append-only operational event ledger, `OperationalEventRepository`, `OperationalEventBus`, operational event schemas/enums in `src/schemas/ops.py`, representative runtime hooks, and `docs/runbooks/operational-event-ledger.md`.
- WI-57 is complete and introduced deterministic, secret-safe narrative rendering in `src/observability/operational_narratives.py` with presentation schemas in `src/schemas/ops.py`.
- WI-58 is complete and introduced a read-only incident replay service in `src/observability/incident_replay.py`, replay schemas in `src/schemas/ops.py`, `scripts/ops/replay.py`, and `docs/runbooks/incident-replay.md`.
- WI-59 is complete and introduced `src/observability/dashboard_activity_feed.py`, dashboard feed/current-state schemas in `src/schemas/ops.py`, and a read-only Streamlit runtime timeline.
- WI-60 adds the daily bot operations digest over the same operational ledger and deterministic presentation layers.
- The digest writes to `03_Daily/YYYY-MM-DD-bot.md`; it must never overwrite manual coding notes at `03_Daily/YYYY-MM-DD.md`.
- `DRY_RUN=false` remains out of scope. Live signing, live broadcasting, and execution paths that bypass `LLMEvaluationResponse` remain forbidden.
- `LLMEvaluationResponse` remains the terminal Gatekeeper before execution and must not receive presentation fields.
- Digest generation is a reporting surface. It must not append, mutate, delete, backfill, or repair operational events.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v16.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-60-daily-operations-digest.md`
- `docs/deliverables/business_logic/business_logic_WI-59-dashboard-activity-feed.md`
- `docs/deliverables/implementation_prompts/prompt_WI-59-dashboard-activity-feed.md`
- `docs/deliverables/business_logic/business_logic_WI-58-incident-replay-cli.md`
- `docs/deliverables/implementation_prompts/prompt_WI-58-incident-replay-cli.md`
- `docs/deliverables/business_logic/business_logic_WI-57-deterministic-human-narratives.md`
- `docs/deliverables/implementation_prompts/prompt_WI-57-deterministic-human-narratives.md`
- `docs/deliverables/business_logic/business_logic_WI-56-operational-event-ledger.md`
- `docs/deliverables/implementation_prompts/prompt_WI-56-operational-event-ledger.md`
- `src/schemas/ops.py`
- `src/db/repositories/operational_event_repository.py`
- `src/db/repositories/position_repository.py`
- `src/observability/operational_narratives.py`
- `src/observability/incident_replay.py`
- `src/observability/dashboard_activity_feed.py`
- `src/agents/execution/telegram_notifier.py`
- `src/core/config.py`
- `scripts/ops/replay.py`
- `tests/unit/test_WI-58-incident-replay-cli.py`
- `tests/unit/test_WI-59-dashboard-activity-feed.py`
- `tests/integration/test_WI-58-incident-replay-cli.py`
- `tests/integration/test_WI-59-dashboard-activity-feed.py`

## Objective

Build a deterministic daily operations digest generator that reads a bounded daily window from the operational event ledger, summarizes run lifecycle, readiness, provider, market, decision, LLM guard, spend, paper PnL, top events, unresolved warnings/errors, and recommended operator checks, writes the result to `03_Daily/YYYY-MM-DD-bot.md`, and optionally sends a short Telegram summary when explicitly configured.

## Inputs

- A digest date or explicit UTC time window.
- Vault daily-note directory `03_Daily/`.
- Existing `OperationalEventRepository.read_window()` behavior.
- Existing operational event enums and payload schemas in `src/schemas/ops.py`.
- Existing deterministic narrative renderer from WI-57.
- Existing incident replay summary counting patterns from WI-58.
- Existing dashboard current-state derivation patterns from WI-59.
- Existing `PositionRepository` for repository-backed paper PnL when available.
- Existing `TelegramNotifier` and Telegram timeout behavior.
- Existing `AppConfig` settings and secret-handling conventions.
- Existing secret/high-cardinality scanning helpers in `src/schemas/ops.py`.

## Outputs

- `src/observability/daily_ops_digest.py`.
- `scripts/ops/generate_daily_ops_digest.py`.
- Daily digest request, status, failure-reason, summary, Telegram, write-result, and report schemas in `src/schemas/ops.py`.
- Config fields for enabling optional digest Telegram delivery, if needed.
- A generated digest file at `03_Daily/YYYY-MM-DD-bot.md`.
- Optional short Telegram digest summary when Telegram alerts are enabled and digest delivery is configured.
- `docs/runbooks/daily-operations-digest.md`.
- `tests/unit/test_WI-60-daily-operations-digest.py`.
- `tests/integration/test_WI-60-daily-operations-digest.py`.

## Acceptance Criteria

1. CLI accepts a digest date or explicit UTC window.
2. Timestamp/window inputs are timezone-aware or normalized to UTC by typed validation.
3. Output path is constrained to `03_Daily/YYYY-MM-DD-bot.md`.
4. Manual coding notes at `03_Daily/YYYY-MM-DD.md` are never created, overwritten, truncated, appended, renamed, or deleted.
5. Re-running with the same inputs produces identical digest content.
6. Existing bot digest files may be replaced only at the validated bot digest path for idempotent cron/systemd operation.
7. Event reads go through `OperationalEventRepository` or a repository-backed read service.
8. Position/PnL reads go through `PositionRepository` or a repository-backed read service.
9. Digest handles missing `operational_events` table gracefully.
10. Digest handles empty/no-run days gracefully.
11. Digest handles partial runs with missing shutdown events gracefully.
12. Digest includes run start/stop times when typed lifecycle events exist.
13. Digest includes uptime when typed lifecycle spans support it.
14. Digest does not invent stop times, provider, readiness, dry-run, PnL, or spend data.
15. Digest includes active provider when supported by typed event payloads.
16. Digest includes dry-run status when supported by typed events.
17. Digest includes latest readiness status when supported by typed readiness events.
18. Digest includes number of markets seen.
19. Digest includes number of markets rejected/quarantined.
20. Digest includes decisions by type.
21. Digest includes skips by reason.
22. Digest includes LLM calls.
23. Digest includes budget blocks.
24. Digest includes cooldown blocks.
25. Digest includes provider failures.
26. Digest includes estimated LLM spend when Decimal-backed data exists.
27. Estimated LLM spend uses `Decimal` end to end and never raw `float`.
28. Digest includes paper PnL when repository-backed position data exists.
29. Paper PnL uses `Decimal` end to end and never raw `float`.
30. Missing spend or PnL data is rendered as unavailable, not fabricated zero.
31. Digest includes deterministic top operational events.
32. Digest includes unresolved warnings/errors using typed event and recovery semantics.
33. Digest includes deterministic recommended next operator checks.
34. Digest top events and checks use deterministic WI-57 narratives or typed mappings only.
35. Digest output never includes raw prompts, private reasoning, raw provider responses, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, raw exception messages, connection strings, SQL text, or high-cardinality identifiers.
36. Digest output is scanned or validated for forbidden content before writing to disk.
37. Telegram summary, when enabled, is scanned or validated for forbidden content before sending.
38. Telegram summary is optional and disabled unless Telegram alerts are enabled and digest delivery is explicitly configured.
39. Telegram summary uses existing Telegram notifier patterns and explicit timeout behavior.
40. Telegram failures return typed delivery failures and do not corrupt or delete a successfully written digest file.
41. CLI returns safe non-zero status for invalid dates/windows.
42. CLI returns safe non-zero status for invalid or unsafe output paths.
43. CLI returns safe non-zero status for repository/database failures.
44. CLI returns safe non-zero status if forbidden content would be written or sent.
45. Database connection or repository failures use bounded low-cardinality messages and do not print raw exception text.
46. The digest service does not call Claude, DeepSeek, Grok, or any other LLM.
47. The digest service does not import or invoke execution routing, transaction signing, order broadcasting, order placement, or live wallet mutation paths.
48. The digest service does not append, update, delete, repair, or backfill operational events.
49. `OperationalEventRepository` remains append/read-only and does not gain public update/delete methods.
50. No `Base.metadata.create_all()` is introduced in runtime, CLI, or digest paths.
51. No live trading, signing, broadcasting, or `DRY_RUN=false` behavior is added or changed.
52. `LLMEvaluationResponse` is not modified and receives no presentation fields.
53. Logs and metrics from digest generation, if any, use low-cardinality labels only.
54. `docs/runbooks/daily-operations-digest.md` explains manual generation, cron/systemd usage, output path, Telegram configuration, no-run days, partial runs, safe output guarantees, and troubleshooting.
55. Targeted WI-60 unit and integration tests pass.
56. Full regression remains compatible with the documented baseline and coverage stays at or above 80%.
57. MAAP is run before commit for any change touching `src/schemas/`, `src/db/`, `src/observability/`, `src/orchestrator.py`, `src/agents/`, or other core logic.

## Anti-Patterns

- Do not enable live trading.
- Do not change `DRY_RUN=false` behavior.
- Do not add live signing or broadcasting.
- Do not bypass `LLMEvaluationResponse`.
- Do not add presentation fields to `LLMEvaluationResponse`.
- Do not use Claude, DeepSeek, Grok, or any LLM to generate digest text.
- Do not derive digest text from raw prompts, raw reasoning, raw provider responses, raw exception messages, token IDs, condition IDs, wallet addresses, or secrets.
- Do not expose high-cardinality identifiers in digest files, Telegram summaries, logs, metrics labels, tests, or runbooks.
- Do not overwrite `03_Daily/YYYY-MM-DD.md`.
- Do not write digest output outside `03_Daily/YYYY-MM-DD-bot.md`.
- Do not write operational events from digest code.
- Do not add update or delete behavior to `OperationalEventRepository`.
- Do not use raw database sessions in digest business logic outside repository boundaries.
- Do not call `Base.metadata.create_all()` in runtime, CLI, or digest paths.
- Do not add new database tables or migrations for this WI unless implementation proves they are strictly necessary and is approved first.
- Do not introduce new external package dependencies.
- Do not use raw `float` for money, spend, EV, Kelly, PnL, price, sizing, exposure, or token-cost values.
- Do not treat missing spend, PnL, provider, readiness, or dry-run evidence as known values.
- Do not allow malformed payload JSON, missing tables, empty windows, or Telegram failures to crash valid digest generation.
- Do not print raw stack traces, SQL text, connection strings, output paths containing secrets, or unbounded exception messages to operators.

## Dependencies

- Phase 16 PRD (`docs/PRD-v16.0.md`).
- WI-56 operational event ledger deliverables and implementation.
- WI-57 deterministic narrative deliverables and implementation.
- WI-58 incident replay deliverables and implementation.
- WI-59 dashboard activity feed deliverables and implementation.
- Existing `OperationalEventRepository.read_window()` contract.
- Existing `PositionRepository` read methods for settled/open positions and Decimal-backed PnL fields.
- Existing `OperationalEventQuery`, `OperationalEventReadWindow`, `OperationalEventRecord`, `OperationalEventPayload`, and operational event enums.
- Existing `NarrativeRenderResult` and `render_event`/`render_window` narrative helpers.
- Existing `IncidentReplaySummary` counting patterns, where reusable.
- Existing `DashboardCurrentState` derivation patterns, where reusable.
- Existing async SQLAlchemy database engine/session setup.
- Existing `TelegramNotifier` timeout and failure-swallowing behavior.
- Existing `structlog` logging conventions.
- Existing low-cardinality and secret/high-cardinality scan constraints.
- Existing scripts under `scripts/ops/` for CLI style and safe report behavior.

## Target Layer

Operations reporting and observability presentation layer over the operational event ledger and position repository. This WI adds deterministic daily operator review output and optional Telegram summary delivery. It must not change ingestion, prompt construction, LLM evaluation semantics, Gatekeeper authority, execution routing, live-trading authorization, signing, broadcasting, or database write semantics.
