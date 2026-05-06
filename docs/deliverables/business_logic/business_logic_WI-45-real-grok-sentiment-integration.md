# Business Logic - WI-45 Real Grok Sentiment Integration

## Objective

Replace deterministic mock sentiment for eligible categories with a real xAI/Grok API path while preserving neutral fallback behavior and the terminal authority of `LLMEvaluationResponse`.

## Data Models

Pydantic schema names only:

- `SentimentResponse`
- `MarketCategory`
- `LLMEvaluationResponse`
- `GrokLiveConfig`
- `GrokRequestEnvelope`
- `GrokResponseEnvelope`
- `GrokFailureReason`

## Key Rules

1. `GrokClient` remains the canonical sentiment client class.
2. `grok_mocked=True` remains the safe default for local tests and CI unless explicit configuration enables live sentiment.
3. Live mode must be explicitly gated by configuration; absence of a key or disabled live gate must not make accidental network calls.
4. Live requests must use `httpx.AsyncClient` with explicit timeout and bounded retries.
5. The only accepted live output is a validated `SentimentResponse`.
6. `sentiment_score` must remain `Decimal` and bounded by the existing schema.
7. Failures must return `NEUTRAL_SENTIMENT`, not raise into the evaluation pipeline.
8. Failure classes include timeout, HTTP error, 429, malformed JSON, missing fields, schema validation error, missing API key, and safety refusal.
9. API keys must come from `AppConfig.grok_api_key` only.
10. Secrets must never be logged, persisted, exposed in exceptions, or added to fixtures.
11. Real sentiment applies only to configured eligible categories, currently `CRYPTO` and `POLITICS`.
12. Sentiment is an upstream context signal only. It cannot bypass `LLMEvaluationResponse`, confidence thresholds, EV thresholds, risk gates, or execution safety checks.

## Edge Cases

1. `grok_mocked=True`: return deterministic mock sentiment and do not create live HTTP requests.
2. Live mode enabled but API key missing: return neutral fallback with typed reason.
3. Live mode enabled but xAI returns 401 or 403: return neutral fallback with auth failure reason.
4. Live mode enabled but xAI returns 429: return neutral fallback after bounded retry policy.
5. Live response is markdown-wrapped JSON: extract only if it validates cleanly.
6. Live response contains non-JSON text: return neutral fallback.
7. Live response contains a JSON number decoded as `float`: reject at schema boundary if it would enter financial/sentiment arithmetic unsafely.
8. `sentiment_score` outside [-1.0, 1.0]: reject and return neutral fallback.
9. `tweet_volume_delta` missing or non-integer: reject and return neutral fallback.
10. `top_narrative_summary` missing, empty, or too long: reject and return neutral fallback.
11. Category is `SPORTS` or `GENERAL`: skip live sentiment and use neutral/fundamental-only path.
12. Request timeout inside Claude chain budget: return neutral promptly so the evaluation path remains bounded.

## Invariants

1. `LLMEvaluationResponse` remains the terminal Gatekeeper.
2. Sentiment can inform prompts but cannot authorize execution.
3. Failure is conservative and non-blocking: neutral fallback preserves pipeline availability.
4. No secret value is logged or persisted.
5. All external I/O has explicit timeout and bounded retry behavior.
6. Mock mode remains deterministic.
7. CI tests must not require real network access or real xAI credentials.
8. `PromptFactory` receives only validated `SentimentResponse` or neutral fallback.
9. The live path must not invent sentiment values when source data is unavailable.
10. No database writes are introduced by Grok sentiment fetching.
