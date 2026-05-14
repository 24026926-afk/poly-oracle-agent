# Implementation Prompt - WI-53 Market Eligibility, Evaluation Deduplication, and Queue Backpressure

## Session Context

You are working in `poly-oracle-agent` on Phase 15: LLM Cost Containment and DeepSeek Provider Optionality.

Current baseline:

- Phase 14 deployment and paper-trading operational tooling is complete.
- WI-52 added an LLM cost guard and cognitive circuit breaker.
- Phase 15 was triggered by a DigitalOcean paper-trading run that exhausted Claude usage while the bot remained stuck evaluating one market.
- Phase 15 treats uncontrolled repeated LLM evaluation as a financial-integrity issue.
- `DRY_RUN=false` remains out of scope.
- `LLMEvaluationResponse` remains the terminal Gatekeeper for every provider-produced trading decision.
- Existing canonical class names must be preserved.
- Multi-market tracking must remain supported.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v15.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.md`
- `docs/deliverables/business_logic/business_logic_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.md`
- `src/agents/ingestion/market_discovery.py`
- `src/agents/context/aggregator.py`
- `src/agents/execution/polymarket_client.py`
- `src/core/config.py`
- `src/schemas/market.py`
- `src/observability/metrics.py`
- `src/orchestrator.py`
- Existing market discovery, aggregator, orchestrator, and metrics tests.

## Objective

Implement dynamic market eligibility preflight, per-market evaluation deduplication, and bounded prompt queue backpressure so pathological markets and unchanged contexts cannot repeatedly reach the LLM evaluation layer.

## Inputs

- Gamma market discovery candidates.
- Candidate market token context, especially YES token ID.
- Read-only CLOB order book data from `PolymarketClient`.
- Market snapshots consumed by `DataAggregator`.
- Per-market midpoint, spread, and timestamp state.
- Configured preflight enablement, timeout, candidate limit, quarantine duration, and maximum spread threshold.
- Configured dedupe enablement, minimum evaluation interval, midpoint delta, and spread delta.
- Configured prompt queue max size and coalescing mode.
- Existing market queue and prompt queue runtime flow.
- Existing WI-52 LLM cost guard behavior.

## Outputs

- Typed market eligibility preflight schemas.
- Typed market quarantine schemas.
- Typed market evaluation fingerprint and dedupe decision schemas.
- Typed prompt queue backpressure and stale-context skip schemas.
- New `AppConfig` fields for market preflight, dedupe, and prompt queue bounds.
- Read-only bounded preflight inside `MarketDiscoveryEngine`.
- In-memory market quarantine for repeated preflight failures.
- Per-market dedupe in `DataAggregator` before prompt payload emission.
- Bounded prompt queue behavior in orchestrator runtime wiring.
- Deterministic queue coalescing or stale-drop behavior.
- Structured, secret-free logs for preflight pass/fail, quarantine, dedupe, stale drop, coalescing, and queue depth.
- Low-cardinality metrics for preflight pass/fail counts, quarantine counts, emitted contexts, deduped contexts, dropped stale contexts, coalesced contexts, and prompt queue depth.
- `docs/runbooks/market-eligibility-and-backpressure.md`
- `tests/unit/test_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.py`
- `tests/integration/test_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.py`

## Acceptance Criteria

1. Market discovery preflight runs before activation when `enable_market_discovery_preflight=True`.
2. Preflight uses explicit timeouts and bounded candidate count.
3. Preflight uses bounded concurrency or an equivalent bounded async strategy.
4. A candidate with missing token context is skipped with a typed reason.
5. A candidate with unavailable order book data is skipped with a typed reason.
6. A candidate with non-positive bid or ask is skipped with a typed reason.
7. A candidate with crossed bid/ask is skipped with a typed reason.
8. A candidate with spread above config is skipped with a typed reason.
9. All preflight price and spread comparisons use `Decimal`.
10. Repeated preflight failures quarantine only the failing market.
11. Quarantine expiry allows the affected market to be checked again.
12. If all candidates fail preflight, no market is activated for that cycle.
13. No hardcoded `condition_id` blacklist is introduced as the primary mitigation.
14. Repeated unchanged contexts for the same market do not enqueue repeated LLM evaluations when dedupe is enabled.
15. Material midpoint movement emits a fresh evaluation payload.
16. Material spread movement emits a fresh evaluation payload.
17. Dedupe is per market and does not suppress unrelated active markets.
18. Prompt queue size is bounded by config.
19. Queue-full behavior is deterministic, typed, logged, and test-covered.
20. Queue coalescing preserves the latest context for the affected market.
21. Queue stale-drop fallback discards older stale payloads with a typed reason.
22. Dedupe and backpressure run before LLM cost guard enforcement where possible.
23. Metrics use low-cardinality labels and do not include prompt text, reasoning text, token IDs, condition IDs, wallet material, API keys, or full provider payloads.
24. Existing time-trigger and volatility-trigger behavior remains compatible with the new dedupe gate.
25. Multi-market tracking is preserved and no single-market-only assumption is introduced.
26. Config validation rejects negative thresholds, timeouts, intervals, deltas, queue sizes, and quarantine durations.
27. When `enable_market_discovery_preflight=False`, current discovery behavior remains compatible.
28. When `enable_market_evaluation_dedupe=False`, current context emission behavior remains compatible except configured queue bounds still apply.
29. The runbook documents preflight, quarantine, dedupe, queue coalescing, and operational recovery from a stuck market.
30. Targeted WI tests pass.
31. Full regression remains compatible with the documented baseline and coverage stays >= 80%.

## Anti-Patterns

- Do not use hardcoded `condition_id` blacklists as the primary safety mechanism.
- Do not allow missing, non-positive, crossed, or extreme-spread quotes to activate a market when preflight is enabled.
- Do not use `float` for price, midpoint, spread, EV, Kelly, PnL, exposure, sizing, or cost calculations.
- Do not allow an unbounded prompt queue.
- Do not enqueue repeated unchanged contexts for the same market when dedupe is enabled.
- Do not make dedupe global across all markets.
- Do not allow one quarantined market to suppress unrelated markets.
- Do not let slow preflight quote lookups stall the discovery loop indefinitely.
- Do not add unbounded retries.
- Do not route execution from preflight, dedupe, or queue backpressure paths.
- Do not weaken `LLMEvaluationResponse`.
- Do not weaken `DRY_RUN=true`.
- Do not use direct DB sessions or raw SQL from agent code.
- Do not add new package dependencies.
- Do not rename canonical classes.
- Do not log raw prompts, reasoning text, token IDs, condition IDs, wallet keys, API keys, or full provider responses.
- Do not put high-cardinality identifiers in Prometheus metric labels.
- Do not hide queue drops or coalescing as silent behavior.

## Dependencies

- Phase 15 PRD (`docs/PRD-v15.0.md`).
- WI-52 LLM cost guard and cognitive circuit breaker.
- Existing `MarketDiscoveryEngine`.
- Existing `DataAggregator`.
- Existing read-only `PolymarketClient`.
- Existing `AppConfig` Pydantic settings.
- Existing `MarketSnapshot` and market schema boundaries.
- Existing orchestrator queue wiring.
- Existing metrics infrastructure.
- Existing structlog logging conventions.
- Existing async test stack.

## Target Layer

Layer 1 ingestion eligibility, Layer 2 context emission, and orchestrator queue backpressure. This WI prevents invalid or unchanged market contexts from reaching paid LLM evaluation and must not alter trading strategy, Gatekeeper authority, order signing, broadcasting, database schema management, or live-trading authorization.
