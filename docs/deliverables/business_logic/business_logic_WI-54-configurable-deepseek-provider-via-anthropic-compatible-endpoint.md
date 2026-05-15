# Business Logic - WI-54 Configurable DeepSeek Provider via Anthropic-Compatible Endpoint

## Objective

Add DeepSeek V4 Pro as a lower-cost, operator-selectable LLM evaluation provider while preserving the canonical `ClaudeClient` class, the existing `anthropic` SDK, the terminal `LLMEvaluationResponse` Gatekeeper, reflection-audit semantics, `DRY_RUN=true` enforcement, repository boundaries, and secret-safe observability.

This WI treats provider selection as a configuration concern, not an architectural rewrite. Provider switching must not weaken any Gatekeeper, execution, or auditability invariant established in prior phases.

## Data Models

Pydantic schema names only:

- `LLMProvider`
- `LLMProviderConfig`
- `LLMProviderRuntimeContext`
- `LLMProviderUsage`
- `LLMProviderMetadata`
- `LLMProviderSelectionDecision`
- `LLMProviderSelectionReason`
- `LLMProviderConfigError`
- `LLMProviderConfigErrorReason`

## Key Rules

1. `LLMProvider` is a typed enum with values `anthropic` and `deepseek` only.
2. No new LLM SDK dependency may be added. DeepSeek must be reached through the existing `anthropic` SDK using a provider-specific `base_url`.
3. The canonical class name `ClaudeClient` is preserved. No rename, alias, or wrapper class replaces it in this WI.
4. `AppConfig` exposes typed provider fields: `llm_provider`, `deepseek_api_key`, `deepseek_base_url`, `deepseek_model`, `deepseek_max_tokens`, `deepseek_max_retries`.
5. `deepseek_base_url` defaults to `https://api.deepseek.com/anthropic`.
6. `deepseek_model` defaults to `deepseek-v4-pro`.
7. Provider configuration validation runs at config-load time and fails closed.
8. When `llm_provider=deepseek` and `deepseek_api_key` is missing or blank, startup fails with a typed configuration error and the provider must not be instantiated.
9. When `llm_provider` is set to an unknown value, configuration validation rejects it with a typed reason.
10. There is no silent fallback from DeepSeek to Anthropic. Any fallback must be explicit, operator-configured, typed, logged, and disabled by default.
11. The `AsyncAnthropic` client is instantiated with provider-specific `api_key`, `base_url`, model, max tokens, and retry settings derived from `AppConfig`.
12. Provider selection does not change request shaping, JSON extraction, retry/backoff structure, or reflection audit invocation.
13. `LLMEvaluationResponse` remains the terminal Gatekeeper for every provider-produced trade decision.
14. Reflection audit remains mandatory unless explicitly blocked by WI-52 budget or cooldown guards. A blocked reflection results in a conservative no-trade outcome, not a routed trade.
15. Provider metadata captured in audit/log records includes provider name, model name, and base URL host only.
16. Provider usage accounting captures input tokens, output tokens, and estimated cost when supplied by the provider.
17. All cost, token-price, and estimated-spend arithmetic uses `Decimal`. Raw `float` is forbidden in money, price, EV, Kelly, PnL, exposure, sizing, and cost-estimation paths.
18. When provider usage fields are missing or malformed, conservative configured defaults are used. Accounting must not silently report zero usage.
19. `dry_run` enforcement, execution routing, signing, broadcasting, and repository boundaries are unchanged by provider selection.
20. Provider switching does not modify Alembic-managed schema or persistence layout.
21. Logs and metrics remain secret-free. API keys, raw prompts, raw reasoning text, wallet material, token IDs, and condition IDs must not appear in metrics labels.
22. Prometheus-safe labels for provider observability use bounded values (provider name, model name, base URL host).
23. Existing Claude-only log event names may evolve to provider-neutral names where safe, but backward-compatible tests or aliases must remain valid for prior assertions until intentionally retired.
24. Malformed JSON returned by any provider re-enters the existing retry path and Gatekeeper validation. No provider-specific JSON repair is introduced in this WI.
25. Provider failure (auth, timeout, transport) produces a typed skip outcome and must not route execution under any condition.
26. Phase 15 documentation amendments (`AGENTS.md`, `README.md`, `docs/system_architecture.md`, `.env.example`) clarify dual-provider operation without renaming `ClaudeClient`.

## Edge Cases

1. `llm_provider=anthropic` with valid Anthropic key: existing Claude behavior is preserved with no semantic change.
2. `llm_provider=deepseek` with valid DeepSeek key: DeepSeek is used through the Anthropic SDK against the configured base URL.
3. `llm_provider=deepseek` with missing or blank DeepSeek key: startup fails closed with a typed configuration error.
4. `llm_provider=deepseek` with malformed base URL: startup fails closed with a typed configuration error.
5. `llm_provider` set to an unknown value (typo, casing mismatch): configuration validation rejects it.
6. DeepSeek endpoint returns malformed JSON: existing retry semantics and Gatekeeper validation apply; no provider-specific JSON repair is introduced.
7. DeepSeek endpoint returns valid JSON missing usage fields: accounting falls back to conservative configured defaults rather than reporting zero.
8. DeepSeek endpoint times out, errors, or rate-limits: typed skip outcome; no execution routing.
9. WI-52 budget guard exhausts mid-evaluation: provider is not invoked; conservative no-trade result emitted.
10. WI-52 market cooldown blocks evaluation: provider is not invoked; conservative no-trade result emitted.
11. Reflection audit is required and the provider call for reflection is blocked: outcome is conservative no-trade, never a routed trade.
12. Operator switches `llm_provider` between restarts: behavior changes only after restart; in-flight evaluations complete under the previously configured provider.
13. Operator sets DeepSeek base URL to a non-DeepSeek host: configuration is still accepted by type checks but logs/metrics surface the configured host; the operator owns provider selection truth.
14. Audit log inspection: provider, model, and host are observable; API key, prompt text, and reasoning text are not.
15. Tests assert dual-provider parity at the JSON extraction and Gatekeeper validation boundary using stubbed clients, never real network calls.

## Invariants

1. The canonical class `ClaudeClient` remains the single evaluation client for `src/agents/evaluation/claude_client.py`.
2. No `openai` SDK or other new LLM provider SDK is introduced in Phase 15.
3. `LLMEvaluationResponse` remains the terminal Gatekeeper regardless of provider.
4. Reflection audit remains mandatory unless explicitly blocked by typed budget/cooldown gates.
5. All money, token-cost, and estimated-spend arithmetic is `Decimal`-native.
6. Provider failure cannot route execution.
7. Provider switching cannot weaken `DRY_RUN=true`.
8. Provider switching cannot alter signing, broadcasting, repository boundaries, or Alembic-managed schema.
9. Configuration validation fails closed for unknown providers and missing DeepSeek credentials.
10. Silent provider fallback is forbidden; any fallback must be explicit, typed, logged, and operator-enabled.
11. Logs and metrics are secret-free and low-cardinality.
12. `PromptFactory` still assembles real market context only; provider selection does not introduce invented or synthetic context.
13. Tests cover Anthropic default path, DeepSeek path, missing DeepSeek key, invalid provider value, malformed DeepSeek JSON retry, provider usage normalization, and provider failure non-routing.
14. Documentation updates (`AGENTS.md`, `README.md`, `docs/system_architecture.md`, `.env.example`) describe dual-provider operation without renaming `ClaudeClient`.
15. Phase 15 cannot recommend DeepSeek as the primary provider on the basis of this WI alone; WI-55 readiness gating is required before any primary recommendation.
