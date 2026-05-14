# Business Logic - WI-53 Market Eligibility, Evaluation Deduplication, and Queue Backpressure

## Objective

Reject pathological markets before activation and prevent unchanged, stale, or duplicate market contexts from repeatedly reaching the LLM evaluation layer.

This WI treats repeated evaluation of the same non-moving or invalid market as a financial-integrity risk. Market discovery, context emission, and prompt queue behavior must cooperate so stale inputs cannot drain LLM budget or create misleading audit trails.

## Data Models

Pydantic schema names only:

- `MarketEligibilityPreflightResult`
- `MarketEligibilityStatus`
- `MarketEligibilitySkipReason`
- `MarketQuarantineDecision`
- `MarketQuarantineReason`
- `MarketEvaluationFingerprint`
- `MarketEvaluationDedupeDecision`
- `MarketEvaluationDedupeReason`
- `PromptQueueBackpressureDecision`
- `PromptQueueBackpressureReason`
- `StaleContextSkipReason`
- `PromptQueueDepthSnapshot`

## Key Rules

1. Market discovery preflight is read-only and must never sign, broadcast, route, or mutate execution state.
2. When enabled, preflight runs before a market is activated for streaming or evaluation.
3. Preflight candidate count is bounded by config.
4. Preflight quote lookups use explicit timeout behavior.
5. Preflight concurrency is bounded so a slow market cannot stall the discovery loop.
6. Preflight rejects candidates with missing YES token context.
7. Preflight rejects unavailable or malformed order books.
8. Preflight rejects non-positive bid or ask quotes.
9. Preflight rejects crossed books where bid is greater than or equal to ask.
10. Preflight rejects spreads above the configured maximum spread threshold.
11. All quote, midpoint, spread, and spread-threshold comparisons use `Decimal`.
12. Raw Python `float` is forbidden in price, midpoint, spread, EV, Kelly, PnL, exposure, sizing, and cost paths.
13. Rejected markets receive typed skip reasons such as `MISSING_TOKEN_CONTEXT`, `ORDER_BOOK_UNAVAILABLE`, `NON_POSITIVE_QUOTE`, `CROSSED_BOOK`, `SPREAD_TOO_WIDE`, or `PREFLIGHT_TIMEOUT`.
14. Repeated preflight failures for the same market place only that market in bounded in-memory quarantine.
15. Quarantine expiry is deterministic and time-based.
16. Quarantine must not suppress unrelated markets.
17. If all candidates fail preflight, no market is activated for that discovery cycle.
18. If no market is activated, no stale prompt payload is enqueued for LLM evaluation.
19. `MarketDiscoveryEngine` must not use hardcoded `condition_id` blacklists as the primary mitigation.
20. Context dedupe is per market, not global.
21. A market evaluation fingerprint represents the material state used to decide whether another evaluation is justified.
22. Unchanged midpoint, unchanged spread, and insufficient elapsed time suppress new evaluation payloads when dedupe is enabled.
23. Material midpoint movement emits a fresh evaluation payload.
24. Material spread movement emits a fresh evaluation payload.
25. Existing time-trigger and volatility-trigger behavior may remain, but must respect the dedupe gate.
26. Prompt queue size is bounded by config.
27. Queue-full behavior is deterministic and typed.
28. Queue-full behavior either coalesces by market or drops older stale payloads according to config.
29. Coalescing preserves the latest context for each market and discards older stale contexts.
30. Queue backpressure runs before LLM budget enforcement where possible so stale payloads do not consume cost-guard checks.
31. Logs and metrics summarize bounded reason codes and aggregate counts.
32. Metrics must not expose raw token IDs, condition IDs, prompt text, reasoning text, wallet material, API keys, or other high-cardinality sensitive identifiers.
33. Multi-market tracking must be preserved.
34. The implementation must not reintroduce single-market-only assumptions.
35. Config validation must reject negative timeouts, intervals, deltas, spread thresholds, queue sizes, and quarantine durations.

## Edge Cases

1. Candidate has no YES token ID: reject with `MISSING_TOKEN_CONTEXT`.
2. Candidate quote lookup times out: reject with `PREFLIGHT_TIMEOUT`.
3. Candidate order book fetch returns no usable data: reject with `ORDER_BOOK_UNAVAILABLE`.
4. Candidate bid or ask is zero or negative: reject with `NON_POSITIVE_QUOTE`.
5. Candidate bid is greater than or equal to ask: reject with `CROSSED_BOOK`.
6. Candidate spread is above config: reject with `SPREAD_TOO_WIDE`.
7. Candidate fails repeatedly within the quarantine window: skip by quarantine without another expensive or slow preflight attempt.
8. Candidate quarantine expires: allow preflight eligibility to be tested again.
9. All candidates fail or are quarantined: discovery returns no activation and emits no prompt work.
10. Preflight is disabled: current discovery behavior remains compatible except for schema-safe no-op decisions.
11. Dedupe is disabled: current context emission behavior remains compatible except prompt queue bounds still apply when configured.
12. Same market emits identical midpoint and spread before the minimum interval: suppress evaluation payload.
13. Same market emits a material midpoint delta before the minimum interval: emit evaluation payload.
14. Same market emits a material spread delta before the minimum interval: emit evaluation payload.
15. Market A is unchanged while Market B moves materially: only Market A is deduped.
16. Prompt queue is full with coalescing enabled: replace stale payload for the same market with the latest context.
17. Prompt queue is full without a matching market entry: apply deterministic stale-drop fallback with typed reason.
18. Prompt queue receives payloads for multiple markets concurrently: queue state remains bounded and per-market semantics are preserved.
19. Metrics server is disabled: preflight, dedupe, and backpressure still work and log structured events.
20. System restarts: in-memory quarantine and dedupe fingerprints reset unless a later WI adds persistence; runbook must document this limitation.

## Invariants

1. Pathological markets are rejected before activation when preflight is enabled.
2. No hardcoded market blacklist is the primary safety mechanism.
3. No stale or duplicate context can grow the prompt queue unbounded.
4. Queue backpressure is typed, deterministic, and auditable.
5. Dedupe is per market and compatible with concurrent market tracking.
6. Material market movement still reaches evaluation.
7. All financial comparisons are Decimal-native.
8. `LLMEvaluationResponse` remains the terminal Gatekeeper for provider-produced trade decisions.
9. Dedupe and backpressure cannot route execution.
10. Dedupe and backpressure cannot weaken `DRY_RUN=true`.
11. Repository boundaries are preserved; agent logic must not use raw DB sessions or raw SQL.
12. Runtime DB schema remains Alembic-managed.
13. Logs and metrics remain secret-free and low-cardinality.
14. Phase 15 cannot declare readiness while repeated unchanged contexts can drain LLM budget.
