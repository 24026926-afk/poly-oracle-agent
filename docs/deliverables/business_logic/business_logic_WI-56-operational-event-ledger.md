# Business Logic - WI-56 Operational Event Ledger

## Objective

Add a durable, append-only operational event ledger that records important runtime behavior from Phase 16 implementation onward so a non-technical operator can reconstruct what the autonomous dry-run server did, why it did it, and whether the bot continued, skipped, degraded, or stopped.

This WI establishes the persistence and event-ingestion foundation for Phase 16. It must use typed validation, Alembic-managed SQLite persistence, repository-only writes, bounded async buffering, and secret-safe event payloads. It must not change live trading behavior, enable `DRY_RUN=false`, modify signing or broadcasting, or add presentation fields to `LLMEvaluationResponse`.

## Data Models

Pydantic schema names only:

- `OperationalEventType`
- `OperationalEventSeverity`
- `OperationalEventSource`
- `OperationalEventReasonCode`
- `OperationalEventPersistenceStatus`
- `OperationalEventPayload`
- `OperationalEventCreate`
- `OperationalEventRecord`
- `OperationalEventBatch`
- `OperationalEventBatchResult`
- `OperationalEventQueueState`
- `OperationalEventQueuePolicy`
- `OperationalEventQuery`
- `OperationalEventReadWindow`
- `OperationalEventAppendResult`
- `OperationalEventFlushResult`
- `OperationalEventValidationError`
- `OperationalEventRedactionResult`

## Key Rules

1. `operational_events` is created only through Alembic. Runtime code must never call `Base.metadata.create_all()`.
2. Runtime event persistence goes only through `OperationalEventRepository`.
3. `OperationalEventRepository` is append-oriented. It may expose append and read methods, but no public update or delete methods.
4. Runtime code may append events only. Operational events are immutable after persistence.
5. Event writes are accepted through an async event bus or publisher backed by a bounded `asyncio.Queue`.
6. Event flushing uses bounded batches and a bounded flush interval so event persistence does not block WebSocket ingestion, context aggregation, LLM evaluation, or execution hot paths.
7. Queue overflow behavior is deterministic, typed, logged, and test-covered.
8. Safety-critical lifecycle and risk events are prioritized over noisy diagnostic events during queue pressure.
9. Event type, severity, source component, reason code, persistence status, and queue outcome are stable enum values.
10. Initial event type coverage includes `START`, `SHUTDOWN`, `CONFIG_LOADED`, `MARKET_DISCOVERED`, `MARKET_REJECTED`, `MARKET_QUARANTINE`, `WS_CONNECTED`, `WS_RECONNECT`, `WS_PONG_STALE`, `READY_STATE_CHANGED`, `LLM_CALL_STARTED`, `LLM_CALL_BLOCKED`, `BUDGET_BLOCK`, `COOLDOWN_BLOCK`, `PROVIDER_FAILURE`, `DECISION_ACCEPTED`, `DECISION_SKIPPED`, `EXECUTION_DRY_RUN`, `CIRCUIT_BREAKER_OPEN`, `CIRCUIT_BREAKER_CLOSED`, `ALERT_SENT`, and `ERROR_RECOVERED`.
11. Event payloads are structured, bounded, and secret-safe.
12. Human-facing event fields must not contain raw prompts, private reasoning, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, or high-cardinality identifiers.
13. Schema validators reject or redact forbidden secret-like and high-cardinality values before event data reaches human-facing logs, metrics, reports, or dashboard feeds.
14. Metrics labels remain low-cardinality. Allowed labels include event type, severity, source component, reason code, persistence status, and bounded queue outcome.
15. Event payloads may include bounded counts, configured boolean states, stable reason codes, active provider names, dry-run status, readiness status, and aggregate decision actions.
16. Event payloads must not include raw exception messages when they may contain secrets or high-cardinality identifiers. Use bounded error categories and reason codes instead.
17. LLM-related event payloads must not include raw prompts, raw responses, private reasoning, or provider API keys.
18. Market-related event payloads must not expose token IDs, condition IDs, or raw market identifiers in human-facing fields.
19. Execution-related event payloads must preserve `dry_run` auditability and must not authorize signing or broadcasting.
20. `LLMEvaluationResponse` remains the terminal Gatekeeper before execution. The ledger records runtime facts; it does not approve trades.
21. Ledger failures for safety-critical events must fail closed where configured by business rule.
22. Ledger failures for non-critical narrative or formatting details must not crash the trading loop.
23. All spend, EV, price, PnL, sizing, exposure, and token-cost values that enter events use `Decimal` before validation.
24. Technical logs continue to use `structlog`; the ledger is a durable operational audit source, not a replacement for all technical debugging logs.
25. The ledger starts from Phase 16 implementation onward. Historical Docker log backfill is out of scope.

## Edge Cases

1. Database is temporarily unavailable during a non-critical event: queue flush returns a typed failure and the runtime continues when allowed by safety policy.
2. Database is unavailable during a safety-critical event: the runtime fails closed or marks readiness degraded according to configured audit-integrity policy.
3. Queue reaches capacity: deterministic overflow policy applies and emits a bounded queue-overflow result without unbounded memory growth.
4. A critical event arrives while the queue is full: lower-priority diagnostic events may be dropped according to policy so the critical event can be retained.
5. An event payload contains a raw prompt or reasoning text: validation rejects the event before persistence.
6. An event payload contains a token ID, condition ID, wallet address, private key pattern, Telegram token, or API key-like string: validation rejects or redacts according to schema policy before persistence.
7. A runtime path attempts to mutate or delete an operational event: no public repository method exists and tests enforce append-only behavior.
8. A migration is missing in a fresh deployment: `alembic upgrade head` is the only supported path to create the ledger table.
9. Event flush is cancelled during shutdown: shutdown attempts a bounded final flush and reports a typed flush result.
10. The final shutdown flush times out: remaining events are reported with a bounded failure reason, and shutdown does not hang indefinitely.
11. Event reason code is unknown or absent: validation fails unless an explicit generic stable reason code is used.
12. A large event payload is submitted: schema validation rejects it before queueing or persistence.
13. Multiple producers publish concurrently: queue behavior remains safe, bounded, and deterministic.
14. Metrics emission fails: ledger persistence continues and metrics failure is logged with bounded labels.
15. Narrative formatting fails in a future WI: persisted events remain valid typed records.

## Invariants

1. Operational events are append-only.
2. Runtime persistence goes through repositories only.
3. Alembic is the only schema-management path.
4. Event queue size, batch size, and flush interval are bounded.
5. Hot paths must not wait on unbounded event persistence.
6. Secret-safe validation happens at schema boundaries.
7. Human-facing operational event fields contain no prompts, private reasoning, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, or high-cardinality identifiers.
8. Metrics labels are low-cardinality.
9. `LLMEvaluationResponse` is not modified.
10. The ledger records runtime behavior but never authorizes execution.
11. `DRY_RUN=false`, live signing, and live broadcasting remain out of scope.
12. Money, pricing, EV, PnL, spend, sizing, exposure, and token-cost math remains Decimal-native.
13. Fail-closed safety behavior is preserved for safety-critical audit failures.
14. Non-critical formatting or narrative failures do not crash the trading loop.
15. Tests cover migration shape, append behavior, read-window queries, queue flush, queue overflow, secret rejection, reason-code validation, disabled behavior, and repository append-only semantics.
