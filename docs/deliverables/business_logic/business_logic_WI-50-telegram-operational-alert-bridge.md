# Business Logic - WI-50 Telegram Operational Alert Bridge

## Objective

Notify the operator when the deployed dry-run runtime needs attention by sending bounded, deduplicated, secret-free Telegram operational alerts for restart, sustained readiness degradation, stale WebSocket health, and circuit breaker transitions.

## Data Models

Pydantic schema names only:

- `OperationalAlertType`
- `OperationalAlertSeverity`
- `OperationalAlertStatus`
- `OperationalAlert`
- `OperationalAlertState`
- `OperationalAlertEvaluation`
- `OperationalAlertDispatchResult`
- `OperationalAlertConfig`

## Key Rules

1. Operational alerts must reuse the existing `TelegramNotifier` transport and Telegram configuration fields where possible.
2. Alert schemas must accept only bounded alert types:
   - `process_started`
   - `readiness_degraded`
   - `websocket_stale`
   - `circuit_breaker_opened`
   - `circuit_breaker_closed`
3. Unknown alert types must be rejected at the Pydantic schema boundary.
4. Alert payloads may include alert type, severity, service name, first-seen timestamp, duration, and bounded reason code.
5. Alert payloads must not include API keys, wallet private keys, Telegram tokens, full environment values, prompt text, reasoning text, raw exception messages, token IDs, raw condition IDs, or high-cardinality market identifiers.
6. Sustained readiness degradation triggers only after the configured threshold, defaulting to 5 minutes.
7. WebSocket disconnected or stale PONG state triggers only after the configured threshold, defaulting to 5 minutes.
8. Alerts must be deduplicated with a cooldown so persistent failures do not spam the operator.
9. Circuit breaker alerts must use typed circuit breaker state transitions, not string parsing from logs.
10. Alert evaluation must be read-only and must not mutate trading state.
11. Alert dispatch must not block ingestion, context, evaluation, execution, health, or metrics loops.
12. Telegram send attempts must use explicit timeout and bounded retry behavior consistent with the existing notifier.
13. If Telegram is disabled or credentials are missing, the runtime continues normally and logs a structured disabled reason.
14. Startup or restart alerts must not fire in tests unless explicitly enabled by test config.

## Edge Cases

1. Telegram notifier is disabled: operational alert bridge evaluates but does not dispatch.
2. Telegram token or chat ID is missing: bridge logs a disabled reason and runtime continues.
3. Readiness flaps between ready and degraded: first-seen and recovery state remain deterministic.
4. WebSocket PONG timestamp is absent: bridge treats the state as unknown until it crosses the configured sustained threshold.
5. Repeated degraded checks occur inside cooldown: no duplicate Telegram message is sent.
6. Circuit breaker opens repeatedly without closing: only one open alert is sent per transition and cooldown policy.
7. Circuit breaker closes after being open: exactly one closed alert is sent for the transition.
8. Telegram send fails or times out: failure is logged without crashing runtime loops.
9. Alert reason contains secret-like text: schema validation rejects or redacts before dispatch.
10. Orchestrator is shutting down: alert task cancels cleanly without blocking shutdown.

## Invariants

1. Operational alerts cannot authorize trades.
2. Operational alerts cannot bypass `LLMEvaluationResponse`.
3. Operational alerts cannot weaken `DRY_RUN=true`.
4. Alert evaluation is read-only.
5. Alert dispatch is bounded and non-blocking for trading pipeline queues.
6. Alert payloads are low-cardinality and secret-free.
7. Telegram disabled or unavailable is never a fatal trading-runtime error.
8. Circuit breaker state remains the authority for circuit breaker alerts.
9. No raw `float` is introduced in money, pricing, EV, Kelly, PnL, or sizing paths.
10. A delivered alert is operational evidence only, not live-trading approval.
