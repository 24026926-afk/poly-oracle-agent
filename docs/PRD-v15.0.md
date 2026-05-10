# PRD-v15.0 — Phase 15: LLM Cost Containment and DeepSeek Provider Optionality

**Version:** 15.0
**Status:** READY FOR IMPLEMENTATION
**Phase:** 15
**Author:** Staff Architect / Quantitative Systems Engineer
**Date:** 2026-05-10
**Baseline:** Phase 14 complete — 1349 tests, 93% coverage, DigitalOcean dry-run paper-trading deployment available, post-phase multi-market tracking fix in progress

---

## 1. Objective

Prevent uncontrolled LLM spend, fix repeated single-market evaluation loops, and add DeepSeek V4 Pro as a lower-cost configurable evaluation provider while preserving `DRY_RUN=true`, Gatekeeper authority, Decimal integrity, and auditability.

Phase 15 is a financial-integrity and cognitive-runtime hardening phase. The system may use real upstream market APIs and real LLM credentials in paper trading, but no work item may enable live signing, live broadcasting, or any path that bypasses `LLMEvaluationResponse`.

---

## 2. Scope Boundaries

**In scope:**
- Hard LLM budget enforcement before any provider call, including hourly/daily call limits, token limits, and operator-configured cost ceilings.
- Per-market cognitive circuit breaker behavior that quarantines markets after repeated low-value, invalid, or non-actionable evaluations.
- Dynamic market-discovery eligibility checks that reject pathological, crossed, non-positive, or extreme-spread markets before they are activated.
- Market-context deduplication and queue backpressure so unchanged or stale contexts do not accumulate into repeated LLM calls.
- Provider-selectable LLM evaluation using the existing Anthropic Messages API surface.
- DeepSeek V4 Pro support through DeepSeek's Anthropic-compatible endpoint (`https://api.deepseek.com/anthropic`) and model id `deepseek-v4-pro`.
- Typed provider configuration, provider metadata in decision audit records where feasible, and secret-safe logs/metrics.
- Backtest and paper-trading validation of DeepSeek output quality, JSON validity, confidence calibration, EV calibration, latency, and estimated cost.
- Conservative DeepSeek threshold recommendations before DeepSeek can become the primary evaluation provider in `DRY_RUN=true`.
- Documentation updates for `.env.example`, README configuration, and system architecture after implementation.

**Out of scope:**
- `DRY_RUN=false`, live trading approval, live order signing, or live broadcast.
- Replacing the `LLMEvaluationResponse` Gatekeeper or weakening any Gatekeeper filter.
- Adding the `openai` SDK or changing the LLM stack away from the existing `anthropic` SDK in this phase.
- Renaming the canonical `ClaudeClient` class unless `AGENTS.md` is explicitly amended in a later phase.
- Full-time Claude/DeepSeek shadow mode that doubles evaluation cost. Only bounded sampling is allowed.
- Hardcoded market blacklists by `condition_id` as the primary mitigation for pathological market selection.
- Prompt-strategy redesign, new trading strategy development, or Kelly optimization beyond provider-specific calibration gates.
- Storing raw prompts, private reasoning text, API keys, wallet keys, token ids, condition ids, or other high-cardinality sensitive identifiers in metrics labels.
- Generating WI business-logic or implementation-prompt deliverables during PRD creation. Those are generated one at a time via `/wi-start`.

---

## 3. Work Items

### WI-52 — LLM Cost Guard and Cognitive Circuit Breaker

**Goal:** Add a fail-closed budget and repeated-evaluation guard before every paid LLM call so the agent cannot burn API credit when a market loop or provider issue occurs.

#### 3.1 File Structure

```
src/
├── agents/
│   └── evaluation/
│       └── claude_client.py
├── core/
│   └── config.py
├── observability/
│   └── metrics.py
├── schemas/
│   ├── llm.py
│   └── ops.py
└── orchestrator.py

docs/
└── runbooks/
    └── llm-cost-guard.md

tests/
├── unit/
│   └── test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py
└── integration/
    └── test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py
```

#### 3.2 Core Requirements

- Add typed Pydantic schemas for LLM budget configuration, budget state, provider usage, budget decisions, and market cooldown decisions.
- Add `AppConfig` fields for:
  - `enable_llm_cost_guard`
  - `llm_hourly_call_limit`
  - `llm_daily_call_limit`
  - `llm_daily_token_limit`
  - `llm_daily_cost_limit_usd`
  - `llm_market_hourly_call_limit`
  - `llm_market_cooldown_seconds`
  - `llm_repeated_hold_threshold`
  - `llm_repeated_invalid_threshold`
- All cost, token-price, and estimated-spend calculations must use `Decimal`, never raw `float`.
- Budget enforcement must happen before `ClaudeClient._get_primary_candidate()` and before reflection audit calls.
- If a budget or cooldown gate blocks evaluation, the system must return/log a typed skip reason such as `LLM_BUDGET_EXHAUSTED`, `MARKET_LLM_COOLDOWN`, or `MARKET_REPEATED_NON_ACTIONABLE`.
- Budget exhaustion must fail closed as no-trade. It must not enqueue anything into execution.
- Market cooldown state must be keyed by bounded internal market identifiers in memory; logs/metrics must avoid raw high-cardinality identifiers unless existing audit logs already store them.
- The cost guard must account for both primary evaluation and reflection calls.
- Usage accounting must include provider name, model name, input tokens, output tokens, and estimated cost when token usage is available.
- If provider usage fields are missing or malformed, budget accounting must use conservative configured defaults rather than fail open.
- Add Prometheus-safe metrics for aggregate LLM calls, budget blocks, cooldown blocks, and estimated spend without leaking prompt text, reasoning, token ids, condition ids, or secrets.
- Add a runbook documenting how to set low paper-trading budgets and how to recover after budget exhaustion.

#### 3.3 Definition of Done — WI-52

- [ ] No LLM provider call occurs when the enabled budget guard has already exhausted the configured call, token, or cost limit.
- [ ] Repeated non-actionable evaluations for the same market trigger a typed cooldown and suppress further provider calls for the configured interval.
- [ ] All budget and cost arithmetic is Decimal-native.
- [ ] Budget/cooldown blocks cannot route execution and cannot bypass `LLMEvaluationResponse`.
- [ ] Metrics and logs are secret-free and low-cardinality.
- [ ] Tests cover hourly call limit, daily call limit, token limit, cost limit, per-market call limit, repeated HOLD cooldown, invalid-output cooldown, provider-usage fallback, and disabled-guard behavior.

---

### WI-53 — Market Eligibility, Evaluation Deduplication, and Queue Backpressure

**Goal:** Reject pathological markets before activation and stop unchanged market contexts or stale prompt payloads from repeatedly reaching the LLM evaluation layer.

#### 4.1 File Structure

```
src/
├── agents/
│   ├── context/
│   │   └── aggregator.py
│   ├── execution/
│   │   └── polymarket_client.py
│   └── ingestion/
│       └── market_discovery.py
├── core/
│   └── config.py
├── schemas/
│   └── market.py
└── orchestrator.py

docs/
└── runbooks/
    └── market-eligibility-and-backpressure.md

tests/
├── unit/
│   └── test_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.py
└── integration/
    └── test_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.py
```

#### 4.2 Core Requirements

- Add typed schemas for market eligibility preflight results, market quarantine decisions, market evaluation fingerprints, queue coalescing decisions, and stale-context skip reasons.
- Add `AppConfig` fields for:
  - `enable_market_discovery_preflight`
  - `market_discovery_preflight_timeout_ms`
  - `market_discovery_max_preflight_candidates`
  - `market_discovery_quarantine_seconds`
  - `market_discovery_max_spread_pct`
  - `enable_market_evaluation_dedupe`
  - `market_eval_min_interval_seconds`
  - `market_eval_min_midpoint_delta`
  - `market_eval_min_spread_delta`
  - `prompt_queue_maxsize`
  - `prompt_queue_coalesce_by_market`
- `MarketDiscoveryEngine` must not rely on hardcoded `condition_id` blacklists as the primary mitigation for pathological markets.
- `MarketDiscoveryEngine` must run a read-only CLOB preflight for candidate YES tokens before activation when preflight is enabled.
- Preflight must use explicit timeouts, bounded candidate count, and bounded concurrency. A slow or failing quote lookup must not stall the discovery loop.
- Preflight must reject candidates with missing token context, unavailable order book, non-positive bid/ask, crossed books, or spread above the configured threshold.
- Spread and price comparisons in preflight must use `Decimal` conversions before comparison.
- A rejected market must receive a typed reason such as `MISSING_TOKEN_CONTEXT`, `ORDER_BOOK_UNAVAILABLE`, `NON_POSITIVE_QUOTE`, `CROSSED_BOOK`, `SPREAD_TOO_WIDE`, or `PREFLIGHT_TIMEOUT`.
- Repeated preflight failures for the same market must place that market in a bounded in-memory quarantine for the configured interval.
- If all candidates fail preflight, the system must activate no market for that cycle and must not enqueue LLM work for stale or pathological markets.
- Discovery logs and metrics must summarize counts by bounded reason code. They must not expose raw token IDs or high-cardinality market identifiers in metrics labels.
- `DataAggregator` must not emit a new evaluation payload when midpoint, spread, and configured time window indicate no material market change.
- Prompt queue behavior must be bounded. When the queue is full, the runtime must either coalesce by market or drop stale older payloads with a typed reason. It must not grow unbounded.
- Coalescing must preserve the latest market context per market and discard older stale contexts.
- Deduplication must be per market, not global. One inactive market must not suppress evaluation for another active market.
- Deduplication and backpressure must run before LLM cost guard enforcement where possible so stale contexts do not consume budget checks unnecessarily.
- All price/spread comparisons that affect financial decisions must use Decimal-safe conversion before comparison.
- Existing time-trigger and volatility-trigger behavior may remain, but must respect dedupe/backpressure gates.
- The implementation must preserve multi-market tracking and must not reintroduce single-market-only assumptions.
- Add metrics for discovery preflight pass/fail counts, market quarantine counts, emitted contexts, deduped contexts, dropped stale contexts, coalesced contexts, and prompt queue depth.

#### 4.3 Definition of Done — WI-53

- [ ] Pathological markets are rejected by dynamic preflight checks before activation, without hardcoded condition-id blacklists.
- [ ] Markets with missing quotes, non-positive quotes, crossed books, or configured extreme spread are skipped with typed reasons.
- [ ] Repeated preflight failures quarantine only the failing market and do not suppress unrelated markets.
- [ ] Repeated identical market contexts do not enqueue repeated LLM evaluations.
- [ ] Material midpoint or spread movement still emits a fresh evaluation payload.
- [ ] Prompt queue size is bounded by config.
- [ ] Queue-full behavior is deterministic, typed, logged, and test-covered.
- [ ] Dedupe/backpressure are per-market and compatible with concurrent market tracking.
- [ ] Tests cover preflight pass, preflight timeout, unavailable order book, non-positive quote, crossed book, spread-too-wide skip, quarantine expiry, unchanged context suppression, material price movement, material spread movement, per-market isolation, queue coalescing, queue full fallback, and disabled-dedupe behavior.

---

### WI-54 — Configurable DeepSeek Provider via Anthropic-Compatible Endpoint

**Goal:** Add DeepSeek V4 Pro as a lower-cost configurable LLM provider without adding a new SDK or weakening the existing evaluation pipeline.

#### 5.1 File Structure

```
src/
├── agents/
│   └── evaluation/
│       └── claude_client.py
├── core/
│   └── config.py
├── schemas/
│   └── llm.py
└── orchestrator.py

.env.example
README.md
docs/
└── system_architecture.md

tests/
├── unit/
│   └── test_WI-54-configurable-deepseek-provider.py
└── integration/
    └── test_WI-54-configurable-deepseek-provider.py
```

#### 5.2 Core Requirements

- Add typed provider enum values for `anthropic` and `deepseek`.
- Add `AppConfig` fields for:
  - `llm_provider`
  - `deepseek_api_key`
  - `deepseek_base_url` defaulting to `https://api.deepseek.com/anthropic`
  - `deepseek_model` defaulting to `deepseek-v4-pro`
  - `deepseek_max_tokens`
  - `deepseek_max_retries`
- Do not add `openai` or any other new provider SDK dependency in this WI.
- Instantiate the existing `AsyncAnthropic` client with provider-specific `api_key`, `base_url`, model, max token, and retry values.
- Preserve the canonical class name `ClaudeClient` because `AGENTS.md` currently lists it as the only valid class name for `src/agents/evaluation/claude_client.py`.
- Update log event names that are semantically Claude-only where appropriate, while preserving backward-compatible tests or aliases if required.
- Normalize provider metadata into audit logs: provider, model, base URL host only, input tokens, output tokens, and estimated cost when available.
- `LLMEvaluationResponse` remains the terminal Gatekeeper for all providers.
- Reflection audit remains mandatory unless explicitly blocked by budget/cooldown, in which case the result must be conservative no-trade.
- Missing DeepSeek key when `llm_provider=deepseek` must fail config validation or fail closed at startup. It must not silently fall back to Anthropic unless an explicit operator fallback flag exists.
- If DeepSeek returns malformed JSON, existing retry and Gatekeeper validation semantics apply.
- Provider selection must not change `dry_run` enforcement, execution routing, signing, broadcasting, or repository boundaries.
- Update `.env.example`, README, and `docs/system_architecture.md` to document provider selection and DeepSeek configuration without including real keys.

#### 5.3 Definition of Done — WI-54

- [ ] `llm_provider=anthropic` preserves current behavior.
- [ ] `llm_provider=deepseek` uses the existing Anthropic SDK against the configured DeepSeek Anthropic-compatible base URL.
- [ ] No `openai` dependency is added.
- [ ] Provider config is typed, validated, and secret-safe.
- [ ] All provider responses still pass through JSON extraction, reflection audit, and `LLMEvaluationResponse`.
- [ ] Tests cover Anthropic default path, DeepSeek path, missing DeepSeek key, invalid provider value, malformed DeepSeek JSON retry, provider token usage normalization, and no execution routing on provider failure.

---

### WI-55 — DeepSeek Backtest Calibration and Paper-Trading Readiness Gate

**Goal:** Prove DeepSeek can be used safely and economically in paper trading before it becomes the primary provider.

#### 6.1 File Structure

```
src/
├── backtest_runner.py
├── backtesting/
│   └── validation.py
├── schemas/
│   ├── backtesting.py
│   └── llm.py
└── core/
    └── config.py

scripts/
└── run_llm_provider_comparison.py

docs/
├── backtests/
│   └── phase15-deepseek-calibration-report.md
└── runbooks/
    └── deepseek-paper-trading-readiness.md

tests/
├── unit/
│   └── test_WI-55-deepseek-backtest-calibration-and-paper-trading-readiness-gate.py
└── integration/
    └── test_WI-55-deepseek-backtest-calibration-and-paper-trading-readiness-gate.py
```

#### 6.2 Core Requirements

- Extend the existing real-data validation path to run by provider without duplicating backtesting logic.
- Produce a provider comparison report with:
  - JSON validity rate
  - Gatekeeper validation pass/fail counts
  - decision distribution (`BUY`, `HOLD`, `SKIP`, `SELL` if applicable)
  - confidence distribution
  - EV calibration
  - realized PnL/EV calibration where historical outcomes exist
  - latency distribution
  - token usage and estimated cost
  - budget/cooldown block counts
- Use bounded Claude sampling only if enabled, such as a configured sample fraction. Full-time shadow mode is prohibited by default because it doubles cost.
- Define a typed readiness verdict for provider use, such as:
  - `PROVIDER_READY_FOR_DRY_RUN_PRIMARY`
  - `PROVIDER_READY_FOR_SAMPLED_AUDIT_ONLY`
  - `PROVIDER_NEEDS_THRESHOLD_RECALIBRATION`
  - `PROVIDER_REJECTED_FOR_JSON_VALIDITY`
  - `PROVIDER_REJECTED_FOR_NEGATIVE_EV`
  - `PROVIDER_REJECTED_FOR_COST_OR_LATENCY`
- DeepSeek cannot be recommended as primary unless it meets minimum JSON validity, calibration, EV, and cost-reduction thresholds.
- Calibration recommendations must include provider-specific suggested values for confidence threshold, EV threshold, max output tokens, and cooldown/budget settings.
- All report metrics involving money, cost, EV, PnL, or token pricing must use Decimal.
- Reports must be constrained to `docs/backtests/` or another approved docs path and must not contain raw prompts, raw reasoning, secrets, token ids, or condition ids.
- Add a runbook for enabling DeepSeek as primary in `DRY_RUN=true` only after the readiness verdict passes.

#### 6.3 Definition of Done — WI-55

- [ ] Provider comparison can run without live order signing or broadcasting.
- [ ] DeepSeek readiness verdict is typed and deterministic.
- [ ] Calibration report is generated under an approved docs path and contains no secrets or raw prompt/reasoning payloads.
- [ ] Cost comparison uses Decimal and includes provider/model-specific token usage.
- [ ] DeepSeek primary recommendation requires passing JSON validity, Gatekeeper, calibration, EV, latency, and cost criteria.
- [ ] Tests cover readiness verdict ordering, report redaction, Decimal cost math, failed JSON validity, negative EV rejection, sampled Claude audit mode, and `DRY_RUN=true` invariant.

---

## 4. Phase Definition of Done

All WI-level DoDs must pass, plus:

- [ ] Full regression test suite passes: `python -m pytest --asyncio-mode=auto tests/`.
- [ ] Coverage remains at or above 80%.
- [ ] MAAP is run before any commit touching `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, or `src/backtest_runner.py`.
- [ ] No money, price, EV, token-cost, PnL, Kelly, exposure, or sizing calculation uses raw `float`.
- [ ] `DRY_RUN=false` remains unavailable as a Phase 15 outcome.
- [ ] No execution path bypasses `LLMEvaluationResponse`.
- [ ] Budget guard blocks happen before paid provider calls.
- [ ] Market discovery preflight rejects pathological wide-spread markets before activation.
- [ ] Market dedupe/backpressure prevents repeated unchanged contexts from draining LLM budget.
- [ ] `llm_provider=deepseek` can run in `DRY_RUN=true` with the existing Anthropic SDK and the DeepSeek Anthropic-compatible base URL.
- [ ] DeepSeek is not recommended as primary unless WI-55 readiness verdict passes.
- [ ] README, `.env.example`, `docs/system_architecture.md`, and relevant runbooks are updated.
- [ ] No real API keys, wallet keys, Telegram tokens, prompt text, reasoning text, token ids, or condition ids are committed in docs, tests, metrics labels, or fixtures.

---

## 5. Constraints & Non-Negotiables

Phase 15 remains governed by `AGENTS.md`:

- `LLMEvaluationResponse` is the terminal Gatekeeper schema before execution.
- `PromptFactory` must assemble real market context only.
- Decisions below configured confidence, EV, or risk thresholds are skipped.
- `dry_run` must be checked before signing, broadcasting, or state-mutating execution.
- All WebSocket, RPC, HTTP, and LLM paths must use explicit timeout or bounded retry behavior.
- Runtime DB access must use repositories only.
- Pydantic V2 schemas must validate data at boundaries.
- `structlog` is the only production logging system.
- Canonical class names remain unchanged unless `AGENTS.md` is explicitly amended.
- The current stack constraint permits `httpx`, `websockets`, `web3.py`, and `anthropic` for HTTP/chain/LLM paths. Phase 15 must use DeepSeek through the existing `anthropic` SDK, not through a new `openai` dependency.

**Project constraint amendment required by Phase 15 implementation:**

`AGENTS.md`, README, and `docs/system_architecture.md` must be updated during WI-54 to clarify that `ClaudeClient` may operate in provider mode using either Anthropic Claude or DeepSeek through an Anthropic-compatible endpoint. This amendment must not rename `ClaudeClient` in Phase 15.

---

## 6. Dependencies to Add

No new Python package dependencies are planned for Phase 15.

DeepSeek support must use the already-present `anthropic` SDK with configurable `base_url`. If implementation proves that the existing SDK version cannot support provider-specific base URLs safely, that finding must be escalated before adding any dependency.

---

## 7. Deliverables Summary

Per `AGENTS.md`, this `/prd` step creates only the phase PRD and updates `STATE.md`. WI business-logic and implementation-prompt deliverables are generated one at a time via `/wi-start {WI}`.

| WI | Deliverable planned via `/wi-start` | Status |
|---|---|---|
| WI-52 — LLM Cost Guard and Cognitive Circuit Breaker | `docs/deliverables/business_logic/business_logic_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.md`; `docs/deliverables/implementation_prompts/prompt_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.md` | Not generated by PRD |
| WI-53 — Market Eligibility, Evaluation Deduplication, and Queue Backpressure | `docs/deliverables/business_logic/business_logic_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.md`; `docs/deliverables/implementation_prompts/prompt_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.md` | Not generated by PRD |
| WI-54 — Configurable DeepSeek Provider via Anthropic-Compatible Endpoint | `docs/deliverables/business_logic/business_logic_WI-54-configurable-deepseek-provider-via-anthropic-compatible-endpoint.md`; `docs/deliverables/implementation_prompts/prompt_WI-54-configurable-deepseek-provider-via-anthropic-compatible-endpoint.md` | Not generated by PRD |
| WI-55 — DeepSeek Backtest Calibration and Paper-Trading Readiness Gate | `docs/deliverables/business_logic/business_logic_WI-55-deepseek-backtest-calibration-and-paper-trading-readiness-gate.md`; `docs/deliverables/implementation_prompts/prompt_WI-55-deepseek-backtest-calibration-and-paper-trading-readiness-gate.md` | Not generated by PRD |

---

## 8. State & Documentation Updates on Phase Completion

When Phase 15 completes:

- Update `STATE.md` with completed WIs, final test count, coverage, provider readiness verdict, and chosen default provider.
- Archive the phase completion record under `04_Archive/poly-oracle-agent/Phase-15/`.
- Update README configuration tables for LLM provider selection, budget guard, market discovery preflight, dedupe/backpressure, and DeepSeek readiness flow.
- Update `docs/system_architecture.md` to show provider-selectable evaluation while preserving `LLMEvaluationResponse` as the terminal Gatekeeper.
- Update `.env.example` with safe placeholder DeepSeek and LLM-budget configuration.
- Document any `AGENTS.md` amendments required by the accepted Phase 15 implementation.
- Keep `DRY_RUN=true` as the only approved runtime mode unless a later phase explicitly approves live-trading readiness.
