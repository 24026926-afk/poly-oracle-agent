# Implementation Prompt - WI-54 Configurable DeepSeek Provider via Anthropic-Compatible Endpoint

## Session Context

You are working in `poly-oracle-agent` on Phase 15: LLM Cost Containment and DeepSeek Provider Optionality.

Current baseline:

- Phase 14 deployment and paper-trading operational tooling is complete.
- WI-52 added a hard LLM cost guard and a cognitive circuit breaker that fail closed before paid provider calls.
- WI-53 added dynamic market eligibility preflight, per-market evaluation deduplication, and bounded prompt queue backpressure so unchanged or pathological market contexts cannot drain LLM budget.
- Phase 15 was triggered by a DigitalOcean paper-trading run that exhausted Claude usage while the bot remained stuck evaluating one market.
- `DRY_RUN=false` remains out of scope. Live signing, live broadcasting, and execution paths that bypass `LLMEvaluationResponse` remain forbidden.
- `LLMEvaluationResponse` remains the terminal Gatekeeper for every provider-produced trading decision.
- The canonical class name `ClaudeClient` for `src/agents/evaluation/claude_client.py` must be preserved.
- DeepSeek support must use the already-present `anthropic` SDK with configurable `base_url`. No new LLM SDK dependency is permitted in this WI.
- Multi-market tracking must remain supported and unaffected by provider selection.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v15.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-54-configurable-deepseek-provider-via-anthropic-compatible-endpoint.md`
- `docs/deliverables/business_logic/business_logic_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.md`
- `docs/deliverables/business_logic/business_logic_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.md`
- `src/agents/evaluation/claude_client.py`
- `src/agents/context/prompt_factory.py`
- `src/core/config.py`
- `src/schemas/llm.py`
- `src/observability/metrics.py`
- `src/orchestrator.py`
- `.env.example`
- Existing Claude evaluation, reflection audit, cost guard, prompt factory, and Gatekeeper tests.

## Objective

Make the LLM evaluation provider operator-selectable between Anthropic Claude and DeepSeek V4 Pro using the existing `anthropic` SDK and a provider-specific `base_url`, while preserving canonical class names, `LLMEvaluationResponse` Gatekeeper authority, reflection-audit semantics, `DRY_RUN=true` enforcement, Decimal integrity, repository boundaries, and secret-safe observability.

## Inputs

- Operator-configured `llm_provider` value selecting Anthropic or DeepSeek.
- Existing Anthropic configuration (`anthropic_api_key`, model, max tokens, max retries).
- New DeepSeek configuration (`deepseek_api_key`, `deepseek_base_url`, `deepseek_model`, `deepseek_max_tokens`, `deepseek_max_retries`).
- Existing market context produced by `PromptFactory`.
- Existing WI-52 budget guard and cognitive circuit breaker outcomes.
- Existing WI-53 dedupe and prompt queue backpressure outcomes.
- Existing reflection audit pipeline.
- Existing Gatekeeper `LLMEvaluationResponse` schema and validation surface.

## Outputs

- Typed `LLMProvider` enum and provider configuration schemas.
- Typed provider runtime context, usage, metadata, and selection decision schemas.
- Typed provider configuration error schema for fail-closed startup.
- New `AppConfig` provider fields with safe defaults, validation, and secret-safe handling.
- A provider-aware `ClaudeClient` that instantiates the existing `AsyncAnthropic` client with provider-specific `api_key`, `base_url`, model, max tokens, and retries — without renaming the class.
- Provider metadata captured in audit/log records (provider name, model name, base URL host only).
- Provider usage normalized into `Decimal`-native accounting consumed by the WI-52 cost guard.
- Structured, secret-free logs for provider selection, provider failures, and provider usage normalization fallback.
- Low-cardinality metrics for provider selection counts, provider failure counts, and provider-tagged usage where it does not duplicate WI-52 metrics.
- `.env.example` updated with safe DeepSeek and provider-selection placeholders.
- `README.md` configuration table updated for provider selection and DeepSeek settings.
- `docs/system_architecture.md` updated to describe provider-selectable evaluation while preserving `LLMEvaluationResponse` as the terminal Gatekeeper.
- `AGENTS.md` amendment clarifying that `ClaudeClient` may operate in provider mode against either Anthropic or DeepSeek through an Anthropic-compatible endpoint, without renaming the class.
- `tests/unit/test_WI-54-configurable-deepseek-provider.py`
- `tests/integration/test_WI-54-configurable-deepseek-provider.py`

## Acceptance Criteria

1. `LLMProvider` enum exposes `anthropic` and `deepseek` values only.
2. `AppConfig` exposes `llm_provider`, `deepseek_api_key`, `deepseek_base_url`, `deepseek_model`, `deepseek_max_tokens`, and `deepseek_max_retries` with typed validation and safe defaults.
3. `deepseek_base_url` defaults to `https://api.deepseek.com/anthropic`.
4. `deepseek_model` defaults to `deepseek-v4-pro`.
5. `llm_provider=anthropic` preserves current Claude evaluation behavior with no semantic change.
6. `llm_provider=deepseek` instantiates the existing `AsyncAnthropic` client with the configured DeepSeek `api_key`, `base_url`, model, max tokens, and retries.
7. Missing or blank `deepseek_api_key` when `llm_provider=deepseek` fails configuration validation or fails closed at startup with a typed reason.
8. An unknown `llm_provider` value fails configuration validation with a typed reason.
9. No `openai` SDK or other new LLM provider SDK is introduced.
10. The canonical class name `ClaudeClient` is preserved.
11. Provider selection does not change `PromptFactory` behavior, JSON extraction logic, reflection audit invocation, or Gatekeeper validation.
12. `LLMEvaluationResponse` remains the terminal Gatekeeper for every provider-produced trade decision.
13. Reflection audit remains mandatory unless explicitly blocked by WI-52 budget or cooldown gates, in which case the outcome is conservative no-trade.
14. Provider failure (auth, timeout, transport, malformed JSON exhausted retries) produces a typed skip outcome and cannot route execution.
15. Provider usage accounting normalizes input tokens, output tokens, and estimated cost into `Decimal`-native values consumed by the WI-52 cost guard.
16. When provider usage fields are missing or malformed, conservative configured defaults are used; accounting must not silently report zero usage.
17. Provider metadata in logs and audit records includes only provider name, model name, and base URL host.
18. Logs, metrics, and audit records must not contain API keys, raw prompts, raw reasoning text, wallet material, token IDs, or condition IDs.
19. Metric labels for provider observability are low-cardinality and bounded.
20. `dry_run` enforcement, execution routing, signing, broadcasting, repository boundaries, and Alembic-managed schema are unchanged.
21. There is no silent fallback from DeepSeek to Anthropic. Any fallback is explicit, typed, logged, operator-enabled, and disabled by default.
22. `.env.example`, `README.md`, `docs/system_architecture.md`, and `AGENTS.md` are updated to document dual-provider operation without renaming `ClaudeClient`.
23. Targeted WI tests pass, covering: Anthropic default path, DeepSeek path, missing DeepSeek key, invalid provider value, malformed DeepSeek JSON retry, provider usage normalization, provider failure non-routing, and reflection-blocked conservative outcome.
24. Full regression remains compatible with the documented baseline and coverage stays at or above 80%.
25. MAAP is run before commit for any change touching `src/agents/`, `src/schemas/`, `src/core/config.py`, or `src/orchestrator.py`.

## Anti-Patterns

- Do not add the `openai` SDK or any other new LLM provider SDK.
- Do not rename, alias, or wrap `ClaudeClient` into a different canonical class name in this WI.
- Do not bypass `LLMEvaluationResponse` for any provider.
- Do not skip reflection audit silently. If reflection is blocked, emit a conservative no-trade outcome.
- Do not let provider failures, timeouts, or malformed JSON route execution.
- Do not silently fall back from DeepSeek to Anthropic.
- Do not use `float` for token cost, estimated spend, EV, Kelly, PnL, exposure, or sizing.
- Do not log API keys, raw prompts, raw reasoning text, wallet material, token IDs, or condition IDs.
- Do not put high-cardinality identifiers in metric labels.
- Do not allow unbounded retries in provider calls; retries must be configured and bounded.
- Do not change `DRY_RUN` enforcement, signing, broadcasting, repository boundaries, or schema management.
- Do not invent provider usage data when usage fields are missing; use conservative configured defaults and log a typed normalization event.
- Do not change `PromptFactory` to inject synthetic market context to compensate for provider differences.
- Do not introduce real network calls into unit or integration tests; stub the provider client.
- Do not commit real DeepSeek or Anthropic credentials in `.env.example`, fixtures, docs, or tests.

## Dependencies

- Phase 15 PRD (`docs/PRD-v15.0.md`).
- WI-52 LLM cost guard and cognitive circuit breaker.
- WI-53 market eligibility, evaluation deduplication, and queue backpressure.
- Existing `ClaudeClient` and reflection audit logic.
- Existing `PromptFactory` and Gatekeeper `LLMEvaluationResponse` validation.
- Existing `AppConfig` Pydantic settings.
- Existing `anthropic` SDK (`AsyncAnthropic` with configurable `base_url`).
- Existing structlog logging conventions.
- Existing Prometheus-safe metrics infrastructure.
- Existing async test stack and stubbed-provider patterns.

## Target Layer

Layer 3 evaluation provider selection and provider configuration. This WI changes how `ClaudeClient` is instantiated and how provider metadata and usage are recorded. It does not change Layer 1 ingestion, Layer 2 context emission, Gatekeeper validation, execution routing, repository boundaries, Alembic schema management, or live-trading authorization.
