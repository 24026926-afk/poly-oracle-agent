# Business Logic - WI-52 LLM Cost Guard and Cognitive Circuit Breaker

## Objective

Prevent uncontrolled paid LLM usage by enforcing typed budget limits and market-level cognitive cooldowns before any primary evaluation or reflection call can reach an external LLM provider.

This WI treats LLM spend as a financial-integrity control. A market loop, malformed provider response, stale queue, or repeated non-actionable decision must not be able to drain API credit indefinitely.

## Data Models

Pydantic schema names only:

- `LLMProviderName`
- `LLMUsageRecord`
- `LLMBudgetConfig`
- `LLMBudgetWindow`
- `LLMBudgetState`
- `LLMBudgetDecision`
- `LLMBudgetBlockReason`
- `MarketCognitiveState`
- `MarketCooldownDecision`
- `MarketCooldownReason`
- `LLMCostGuardSnapshot`

## Key Rules

1. The cost guard runs before every paid primary evaluation call.
2. The cost guard runs before every paid reflection audit call.
3. Budget checks must include both call-count limits and token/cost limits.
4. Budget windows are evaluated at hourly and daily granularity.
5. Budget state must include provider name, model name, input tokens, output tokens, total tokens, estimated cost, and timestamp when available.
6. All token-price, estimated-cost, and remaining-budget calculations use `Decimal`.
7. Raw Python `float` is forbidden in token pricing, estimated spend, budget thresholds, and cost comparisons.
8. Missing provider usage data uses conservative configured fallback token estimates rather than failing open.
9. Budget exhaustion fails closed as no-trade and no further provider call.
10. Budget exhaustion must not enqueue execution work.
11. Budget exhaustion must not bypass `LLMEvaluationResponse`; it results in a typed skip/no-evaluation path before Gatekeeper.
12. Per-market cognitive state tracks repeated `HOLD`, `SKIP`, malformed JSON, provider errors, and low-value no-trade outcomes.
13. Repeated non-actionable outcomes for the same market trigger a bounded cooldown.
14. Repeated invalid or malformed provider outputs for the same market trigger a bounded cooldown.
15. A cooldown applies only to the affected market and must not suppress other active markets.
16. Cooldown expiry is time-based and deterministic.
17. Cooldown state is in-memory for Phase 15 unless later WI explicitly persists it.
18. Logs and metrics must use bounded reason codes and low-cardinality labels.
19. Metrics may expose aggregate counts and estimated spend, but not prompt text, reasoning text, token IDs, condition IDs, wallet material, API keys, or full provider payloads.
20. If the cost guard is disabled by config, current evaluation behavior is preserved except for schema construction and no-op accounting.
21. Config validation must reject negative limits, negative prices, negative cooldowns, and malformed Decimal values.
22. The guard must be safe under async concurrent evaluation attempts.
23. The guard must never weaken `DRY_RUN=true` protections.
24. The guard must never sign, broadcast, route, or mutate live orders.

## Edge Cases

1. Hourly call limit is exhausted: block immediately with `LLM_BUDGET_EXHAUSTED`.
2. Daily call limit is exhausted: block immediately with `LLM_BUDGET_EXHAUSTED`.
3. Daily token limit is exhausted: block immediately with `LLM_TOKEN_LIMIT_EXHAUSTED`.
4. Daily cost limit is exhausted: block immediately with `LLM_COST_LIMIT_EXHAUSTED`.
5. A primary call is allowed but reflection would exceed the remaining budget: primary result is not routed directly; reflection is conservatively blocked and the decision resolves no-trade.
6. Provider response has missing usage fields: use fallback token estimate and mark the usage record as estimated.
7. Provider response has malformed usage fields: use fallback token estimate and mark the usage record as estimated.
8. Provider raises timeout before usage is known: record a provider-error event without adding invented token usage.
9. Provider returns malformed JSON repeatedly for one market: market cooldown is activated.
10. Provider returns valid `HOLD` repeatedly for one market: market cooldown is activated after configured threshold.
11. Provider returns valid rejected decisions with low confidence or low EV repeatedly: market cooldown is activated after configured threshold.
12. Market is on cooldown and a new context arrives: evaluation is skipped before provider call.
13. Cooldown expires and a materially changed market context arrives: evaluation is eligible again subject to budget.
14. System restarts: in-memory hourly/daily/cooldown state resets unless a later WI adds persistence; runbook must document this limitation.
15. Config disables cost guard: no budget block occurs, but schema validation and logs still remain safe.
16. Config limit is zero: treat as no calls allowed when guard is enabled.
17. Multiple markets emit concurrently: guard decisions remain per-market for cooldown and global for budget.
18. Metrics server is disabled: cost guard still works and logs structured events.

## Invariants

1. No paid LLM call happens after a blocking budget decision.
2. No paid LLM call happens for a market while that market is in active cooldown.
3. All cost math is Decimal-native.
4. `LLMEvaluationResponse` remains the terminal Gatekeeper for provider-produced trade decisions.
5. A blocked LLM call cannot route execution.
6. A blocked reflection cannot allow an unreflected primary candidate to route execution.
7. `DRY_RUN=false` remains out of scope for Phase 15.
8. Provider choice remains orthogonal to budget enforcement.
9. Budget and cooldown logs are audit-friendly and secret-free.
10. Metrics labels remain low-cardinality.
11. Repository boundaries are preserved; agent logic must not use raw DB sessions or raw SQL.
12. Runtime DB schema remains Alembic-managed.
13. The cost guard is a safety gate, not a trading strategy optimizer.
14. The cost guard must fail closed on malformed budget state.
15. Phase 15 cannot declare readiness until repeated-market token drain is structurally impossible under enabled guard settings.

