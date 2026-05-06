# Implementation Prompt - WI-45 Real Grok Sentiment Integration

## Session Context

You are working in `poly-oracle-agent` on Phase 13: Real-Data Validation & 24/7 Readiness.

Current baseline:

- `GrokClient` exists in `src/agents/evaluation/grok_client.py`.
- Current design is mock-first with deterministic `_MOCK_SENTIMENT`.
- `SentimentResponse` exists in `src/schemas/llm.py`.
- `PromptFactory` injects validated sentiment into evaluation prompts.
- `LLMEvaluationResponse` remains the terminal Gatekeeper.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v13.0.md`
- `docs/deliverables/business_logic/business_logic_WI-45-real-grok-sentiment-integration.md`
- `src/agents/evaluation/grok_client.py`
- `src/agents/evaluation/claude_client.py`
- `src/agents/context/prompt_factory.py`
- `src/schemas/llm.py`
- `src/core/config.py`

## Objective

Implement the real xAI/Grok sentiment path behind explicit configuration while preserving deterministic mock mode and neutral fallback behavior.

## Inputs

- `AppConfig.grok_api_key`
- `AppConfig.grok_base_url`
- `AppConfig.grok_model`
- `AppConfig.grok_mocked`
- New explicit live timeout, retry, and enablement config as needed.
- Market category and market metadata passed to `GrokClient.analyze_sentiment`.

## Outputs

- Updated `src/agents/evaluation/grok_client.py`
- Updated `src/core/config.py`
- `tests/unit/test_WI-45-real-grok-sentiment.py`
- `tests/integration/test_WI-45-real-grok-sentiment.py`

## Acceptance Criteria

1. Mock mode still returns deterministic `_MOCK_SENTIMENT` for existing tests.
2. Live mode posts to the configured xAI/Grok endpoint with bounded timeout and retry behavior.
3. Missing key, timeout, HTTP error, 429, malformed JSON, schema error, missing fields, or safety refusal returns `NEUTRAL_SENTIMENT`.
4. Failure paths log structured reasons without raising into the evaluation pipeline.
5. No secret value appears in logs, fixtures, committed files, exceptions, health endpoints, or metrics.
6. `PromptFactory` receives validated `SentimentResponse` only.
7. Live sentiment applies only to configured eligible categories.
8. Sentiment cannot bypass `LLMEvaluationResponse`, confidence thresholds, EV thresholds, or execution safety gates.
9. Integration tests use mocked HTTP responses; CI does not require real xAI credentials or network access.
10. Targeted WI tests pass.
11. Full regression remains compatible with the documented baseline and coverage does not fall below 80%.

## Anti-Patterns

- Do not make live Grok calls when `grok_mocked=True`.
- Do not make live Grok calls without explicit live enablement and API key.
- Do not log API keys or request authorization headers.
- Do not invent sentiment when live source data is unavailable.
- Do not raise live sentiment failures into the Claude evaluation path.
- Do not bypass `SentimentResponse` validation.
- Do not bypass `LLMEvaluationResponse`.
- Do not expand eligible categories without explicit business logic.
- Do not require real network access in CI.
- Do not introduce database writes in the sentiment path.

## Dependencies

- WI-12 Chained Prompt Factory.
- Existing `GrokClient`.
- Existing `SentimentResponse`.
- Existing `ClaudeClient` sentiment fetch path.
- Existing `PromptFactory` sentiment block behavior.
- Existing `httpx` async client stack.

## Target Layer

Evaluation support layer. Sentiment is upstream of prompt construction and Gatekeeper validation; it is not an execution or risk-approval path.
