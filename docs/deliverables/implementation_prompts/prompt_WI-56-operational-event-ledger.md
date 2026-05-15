# Implementation Prompt - WI-56 Operational Event Ledger

## Session Context

You are working in `poly-oracle-agent` on Phase 16: Operator Clarity and Runtime Audit Trail.

Current baseline:

- Phase 15 is complete with 1824 tests and 93% coverage.
- The agent can run in DigitalOcean dry-run paper-trading mode with health, readiness, metrics, dashboard access, Telegram operational alerts, LLM budget guards, market cooldowns, and configurable Anthropic/DeepSeek provider selection.
- The previous paper-trading run exposed a major operational gap: technical stdout/Docker logs existed, but the operator did not have a durable, chronological, human-readable runtime ledger.
- Phase 16 begins the operational audit trail from implementation onward. Historical Docker log backfill is out of scope.
- WI-56 creates the foundational operational event ledger used later by deterministic narratives, incident replay, dashboard timeline, and daily bot digest WIs.
- `DRY_RUN=false` remains out of scope. Live signing, live broadcasting, and execution paths that bypass `LLMEvaluationResponse` remain forbidden.
- `LLMEvaluationResponse` remains the terminal Gatekeeper before execution and must not receive presentation fields such as `human_summary`.
- Runtime persistence must go through repositories only. Agent logic must not use raw database sessions for operational event writes.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v16.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-56-operational-event-ledger.md`
- `src/db/models.py`
- `src/db/engine.py`
- existing repository files in `src/db/repositories/`
- `src/schemas/ops.py`
- `src/observability/metrics.py`
- `src/observability/operational_alerts.py`
- `src/orchestrator.py`
- existing Alembic migrations under `migrations/versions/`
- existing WI-48, WI-50, WI-51, WI-52, and WI-53 tests for operational schemas, alerts, health, metrics, queues, and budget/cooldown behavior.

## Objective

Build a durable, SQLite-backed, append-only operational event ledger with typed schemas, Alembic migration, repository-only persistence, bounded async event buffering, and representative runtime event emission for lifecycle, discovery, WebSocket, readiness, LLM guard, decision, dry-run execution, circuit breaker, alert, and recovery events.

## Inputs

- Phase 16 PRD requirements for WI-56.
- Existing SQLAlchemy async database setup.
- Existing Alembic migration pattern.
- Existing repository pattern in `src/db/repositories/`.
- Existing operational schemas and secret-detection helpers in `src/schemas/ops.py`.
- Existing `structlog` logging conventions.
- Existing metrics registry and low-cardinality metric label constraints.
- Existing orchestrator lifecycle and background task wiring.
- Existing event sources in market discovery, WebSocket health, readiness callbacks, LLM budget/cooldown/provider paths, decision flow, dry-run execution flow, circuit breaker state, operational alerts, and recovery/error handling.

## Outputs

- Alembic migration that creates `operational_events`.
- SQLAlchemy ORM model for `OperationalEvent`.
- `OperationalEventRepository` with append and read-window methods only.
- Pydantic V2 schemas and enums for operational event creation, persisted records, payloads, query windows, batch append results, queue state, queue policy, append results, and flush results.
- Bounded async operational event bus or publisher.
- Bounded batch writer task that uses the repository for persistence.
- Config fields for enabling the ledger, queue size, batch size, flush interval, shutdown flush timeout, and overflow policy.
- Low-cardinality metrics for event append attempts, persisted events, dropped events, flush failures, queue depth, and queue overflow outcomes.
- Representative runtime event hooks in orchestrator and relevant components.
- `docs/runbooks/operational-event-ledger.md`.
- `tests/unit/test_WI-56-operational-event-ledger.py`.
- `tests/integration/test_WI-56-operational-event-ledger.py`.

## Acceptance Criteria

1. `operational_events` exists after `alembic upgrade head`.
2. The table is created only through Alembic.
3. Operational event persistence goes only through `OperationalEventRepository`.
4. `OperationalEventRepository` exposes no public update or delete methods.
5. Runtime event writes are append-only.
6. Pydantic V2 schemas validate event type, severity, source component, reason code, payload bounds, and persistence status.
7. Stable enums cover the required initial event types from the Phase 16 PRD.
8. Stable reason codes cover lifecycle, config, market discovery, quarantine, WebSocket, readiness, LLM budget/cooldown/provider, decision, dry-run execution, circuit breaker, alert, recovery, validation, queue, and persistence outcomes.
9. Event payloads reject or redact raw prompts, private reasoning, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, and high-cardinality identifiers.
10. Runtime event publisher uses a bounded `asyncio.Queue`.
11. Queue size is configurable.
12. Flush batch size is configurable and bounded.
13. Flush interval is configurable and bounded.
14. Shutdown final flush is bounded by a timeout.
15. Queue overflow behavior is deterministic, typed, logged, and test-covered.
16. Safety-critical events are prioritized over noisy diagnostic events during queue pressure.
17. WebSocket, evaluation, and execution hot paths do not wait on unbounded event persistence.
18. Event persistence failures return typed results and low-cardinality logs/metrics.
19. Safety-critical audit failures fail closed or mark readiness degraded according to configured policy.
20. Non-critical formatting or narrative-adjacent failures do not crash the trading loop.
21. Metrics labels use only low-cardinality values such as event type, severity, source component, reason code, persistence status, and queue outcome.
22. Runtime hooks emit representative `START`, `SHUTDOWN`, `CONFIG_LOADED`, `MARKET_DISCOVERED`, `MARKET_REJECTED`, `MARKET_QUARANTINE`, `WS_CONNECTED`, `WS_RECONNECT`, `WS_PONG_STALE`, `READY_STATE_CHANGED`, `LLM_CALL_STARTED`, `LLM_CALL_BLOCKED`, `BUDGET_BLOCK`, `COOLDOWN_BLOCK`, `PROVIDER_FAILURE`, `DECISION_ACCEPTED`, `DECISION_SKIPPED`, `EXECUTION_DRY_RUN`, `CIRCUIT_BREAKER_OPEN`, `CIRCUIT_BREAKER_CLOSED`, `ALERT_SENT`, and `ERROR_RECOVERED` events where the corresponding runtime signals exist.
23. Runtime hooks do not expose raw token IDs, condition IDs, prompts, reasoning, secrets, wallet addresses, or raw exception messages in human-facing event fields.
24. The ledger begins recording from Phase 16 implementation onward. Historical Docker-log backfill is not implemented.
25. `LLMEvaluationResponse` is not modified.
26. No live trading, signing, broadcasting, or `DRY_RUN=false` behavior is added or changed.
27. All money, pricing, EV, PnL, spend, sizing, exposure, and token-cost values entering events are Decimal-safe.
28. Unit tests cover schema validation, secret rejection, enum validation, append result models, queue state, overflow policy, and metrics label bounds.
29. Integration tests cover migration/table shape, repository append/read-window behavior, append-only public API, event bus batch flush, shutdown flush, queue overflow, and orchestrator wiring for representative lifecycle events.
30. Full regression remains compatible with the documented baseline and coverage stays at or above 80%.
31. MAAP is run before commit for any change touching `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, or other core logic.

## Anti-Patterns

- Do not enable live trading.
- Do not change `DRY_RUN=false` behavior.
- Do not add live signing or broadcasting.
- Do not bypass `LLMEvaluationResponse`.
- Do not add `human_summary` or any presentation field to `LLMEvaluationResponse`.
- Do not use an LLM to generate runtime narratives.
- Do not write operational events directly from agent logic with raw database sessions.
- Do not expose public update or delete methods on `OperationalEventRepository`.
- Do not call `Base.metadata.create_all()` in runtime paths.
- Do not allow unbounded event queues, unbounded flush batches, or unbounded shutdown waits.
- Do not block WebSocket, evaluation, or execution hot paths on slow event persistence.
- Do not store raw prompts, private reasoning, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, raw exception messages, or high-cardinality identifiers in event fields, reports, logs, metrics labels, or dashboard feeds.
- Do not use raw `float` for money, pricing, EV, Kelly, PnL, spend, sizing, exposure, or token-cost values.
- Do not add high-cardinality metrics labels.
- Do not make the dashboard, replay CLI, or daily digest in this WI beyond what is needed to keep future consumers possible.
- Do not backfill historical Docker logs.
- Do not introduce PostgreSQL, cryptographic hash-chain ledgers, or new external package dependencies for this WI.

## Dependencies

- Phase 16 PRD (`docs/PRD-v16.0.md`).
- Existing SQLite and SQLAlchemy async database infrastructure.
- Existing Alembic migration setup.
- Existing repository pattern.
- Existing `src/schemas/ops.py` operational schema conventions and secret scanning patterns.
- Existing `structlog` logging conventions.
- Existing metrics registry and Prometheus-safe label rules.
- Existing orchestrator lifecycle and background task management.
- Existing health/readiness, metrics, Telegram operational alert, LLM budget guard, market quarantine, queue backpressure, and provider failure signals.
- Future WI-57 deterministic narratives, WI-58 incident replay, WI-59 dashboard feed, and WI-60 daily digest will consume this ledger.

## Target Layer

Observability and persistence infrastructure spanning the repository layer, operational schemas, metrics, and orchestrator lifecycle wiring. This WI adds the durable operational event foundation for later narrative, replay, dashboard, and digest features. It must not change trading strategy, prompt construction, Gatekeeper authority, execution routing semantics, live-trading authorization, signing, or broadcasting.
