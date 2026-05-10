# Implementation Prompt - WI-52 LLM Cost Guard and Cognitive Circuit Breaker

## Session Context

You are working in `poly-oracle-agent` on Phase 15: LLM Cost Containment and DeepSeek Provider Optionality.

Current baseline:

- Phase 14 deployment and paper-trading operational tooling is complete.
- Phase 15 was triggered by a DigitalOcean paper-trading run that exhausted Claude usage while the bot remained stuck evaluating one market.
- Phase 15 treats uncontrolled LLM usage as a financial-integrity issue.
- `DRY_RUN=false` remains out of scope.
- `LLMEvaluationResponse` remains the terminal Gatekeeper for every provider-produced trading decision.
- The existing class name `ClaudeClient` must be preserved.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v15.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.md`
- `src/agents/evaluation/claude_client.py`
- `src/core/config.py`
- `src/schemas/llm.py`
- `src/schemas/ops.py`
- `src/observability/metrics.py`
- `src/orchestrator.py`
- `tests/integration/test_claude_client.py`

## Objective

Implement a typed LLM cost guard and per-market cognitive circuit breaker that prevents uncontrolled paid LLM usage before primary evaluation and reflection calls.

## Inputs

- Evaluation payloads consumed by `ClaudeClient`.
- Market identifier from the payload state or Gatekeeper market context when available.
- Provider name and model name.
- Provider token usage when available.
- Configured hourly and daily LLM call limits.
- Configured daily token and cost limits.
- Configured per-market hourly call limit.
- Configured repeated-HOLD and repeated-invalid thresholds.
- Configured market cooldown duration.
- Existing PromptFactory output and reflection prompt flow.

## Outputs

- Typed LLM budget and cooldown schemas.
- New `AppConfig` fields for LLM cost guard settings.
- Pre-call budget enforcement before primary LLM evaluation.
- Pre-call budget enforcement before reflection audit.
- Provider usage accounting for successful calls.
- Conservative fallback accounting for missing or malformed usage fields.
- Per-market cooldown state for repeated non-actionable or invalid outcomes.
- Structured, secret-free logs for budget allowance, budget block, usage accounting, and cooldown decisions.
- Low-cardinality metrics for LLM calls, budget blocks, cooldown blocks, token usage, estimated spend, and active cooldown count.
- `docs/runbooks/llm-cost-guard.md`
- `tests/unit/test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py`
- `tests/integration/test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py`

## Acceptance Criteria

1. No primary LLM provider call occurs when the enabled budget guard has exhausted the configured hourly call limit.
2. No primary LLM provider call occurs when the enabled budget guard has exhausted the configured daily call limit.
3. No primary or reflection provider call occurs when the enabled budget guard has exhausted the configured daily token limit.
4. No primary or reflection provider call occurs when the enabled budget guard has exhausted the configured daily cost limit.
5. No provider call occurs for a market while that market is in active cooldown.
6. Repeated non-actionable outcomes for the same market trigger a typed cooldown after the configured threshold.
7. Repeated malformed JSON or provider validation failures for the same market trigger a typed cooldown after the configured threshold.
8. Cooldown blocks only the affected market and does not suppress unrelated markets.
9. All cost, token-price, and estimated-spend math uses `Decimal`.
10. Raw Python `float` is not introduced in money, EV, Kelly, token pricing, estimated spend, budget thresholds, or cost comparisons.
11. Missing provider usage fields use conservative fallback token estimates and mark usage as estimated.
12. Malformed provider usage fields use conservative fallback token estimates and mark usage as estimated.
13. Budget/cooldown blocks resolve no-trade and do not enqueue execution work.
14. A blocked reflection cannot allow an unreflected primary candidate to route execution.
15. Budget and cooldown events are logged with bounded reason codes and no secrets.
16. Metrics use low-cardinality labels and do not include prompt text, reasoning text, token IDs, condition IDs, wallet material, API keys, or full provider payloads.
17. Config validation rejects negative limits, negative costs, malformed Decimal settings, and negative cooldown durations.
18. When `enable_llm_cost_guard=False`, current behavior remains compatible and tests cover the disabled path.
19. The runbook documents recommended low paper-trading budget settings and recovery after budget exhaustion.
20. Targeted WI tests pass.
21. Full regression remains compatible with the documented baseline and coverage stays >= 80%.

## Anti-Patterns

- Do not allow a provider call after a budget block.
- Do not allow a provider call for a market in cooldown.
- Do not use `float` for token cost, estimated spend, budget thresholds, or financial calculations.
- Do not route execution from a blocked evaluation path.
- Do not route execution from an unreflected primary candidate when reflection is blocked by budget.
- Do not persist cooldown state with ad hoc raw SQL.
- Do not use direct DB sessions from agent code.
- Do not add the `openai` SDK.
- Do not rename `ClaudeClient`.
- Do not weaken `LLMEvaluationResponse`.
- Do not log raw prompts, reasoning text, token IDs, condition IDs, wallet keys, API keys, or full provider responses.
- Do not put high-cardinality labels in Prometheus metrics.
- Do not make budget exhaustion a warning-only event.
- Do not hide provider usage parsing failures.
- Do not make retries unbounded.
- Do not modify order signing, broadcasting, or live execution authorization.

## Dependencies

- Phase 15 PRD (`docs/PRD-v15.0.md`).
- Existing `ClaudeClient` evaluation and reflection flow.
- Existing `LLMEvaluationResponse` Gatekeeper.
- Existing `PromptFactory`.
- Existing `AppConfig` Pydantic settings.
- Existing metrics infrastructure.
- Existing structlog logging conventions.
- Existing async test stack.

## Target Layer

Layer 3 evaluation safety and observability. This WI gates paid LLM evaluation calls before they occur and must not alter trading strategy, execution routing, order signing, broadcasting, database schema management, or live-trading authorization.

