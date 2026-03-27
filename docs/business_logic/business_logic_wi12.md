# WI-12 Business Logic — Prompt Chaining & Grok Sentiment Oracle

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — All new network I/O remains async and non-blocking; no queue-order changes.
- `.agents/rules/risk-auditor.md` — Gatekeeper remains terminal; no EV/Kelly rule drift; Decimal-safe handling for sentiment if used numerically.
- `.agents/rules/test-engineer.md` — New chain behavior must be test-covered; full suite remains green with coverage >= 80%.

## 1. Objective

WI-12 introduces a Sentiment Oracle stage into the Phase 4 chained evaluation path so real-time social signal can inform Claude's final decision context without weakening existing financial controls.

This WI is upstream-only cognitive enrichment. It MUST NOT:
1. bypass `LLMEvaluationResponse` validation,
2. alter the 5 safety filters or thresholds,
3. alter Kelly/exposure formulas.

## 2. Trigger Logic (Category-Gated Sentiment Calls)

`ClaudeClient._route_market()` from WI-11 is the sole trigger source.

Mandatory routing policy:
- `CRYPTO` -> MUST call Grok sentiment.
- `POLITICS` -> MUST call Grok sentiment.
- `SPORTS` -> MUST skip Grok (local-only path).
- `GENERAL` -> MUST skip Grok (local-only path).

Rationale:
- CRYPTO/POLITICS are sentiment-sensitive domains where short-horizon narrative shifts are materially relevant.
- SPORTS/GENERAL remain local for latency/token efficiency and to preserve throughput.

## 3. Grok Prompt Payload Contract (Last 60 Minutes of X)

For eligible categories, Grok receives a strict prompt to analyze X/Twitter discourse for the trailing 60-minute window.

### 3.1 Prompt Template

System prompt:
"You are a market sentiment extraction engine. Return only strict JSON."

User payload fields:
- `condition_id`
- `market_title`
- `market_category`
- `reference_timestamp_utc`
- `analysis_window_minutes=60`
- optional context: `tags`, `midpoint`, `spread`

Instruction block:
1. Analyze public X discourse for this market in the last 60 minutes.
2. Estimate directional sentiment and participation momentum.
3. Return exactly one JSON object with required keys.

Required JSON shape:
```json
{
  "sentiment_score": 0.0,
  "tweet_volume_delta": 0,
  "top_narrative_summary": "..."
}
```

Field definitions:
- `sentiment_score`: range `[-1.0, 1.0]`, where -1 = strongly bearish / negative, +1 = strongly bullish / positive.
- `tweet_volume_delta`: signed integer percent delta vs prior 60-minute baseline (e.g., `+35` = 35% more volume than previous hour).
- `top_narrative_summary`: concise natural-language summary of dominant narrative (1-2 sentences).

### 3.2 Validation Boundary

Raw Grok JSON is validated by a Pydantic `SentimentResponse` model before entering Stage B context.
Malformed/missing payloads are treated as sentiment failure and trigger neutral fallback.

## 4. Chained Integration Flow

WI-12 chain is:

1. Stage 0: `ClaudeClient._route_market(item)` -> `MarketCategory`
2. Stage A: `GrokClient` sentiment fetch (only for CRYPTO/POLITICS) + schema validation to `SentimentResponse`
3. Stage A fallback path: neutral sentiment object when category is non-eligible or Grok fails
4. Stage B context assembly: `PromptFactory` injects sentiment artifact into final evaluation prompt context
5. Final evaluation: Claude response validated by `LLMEvaluationResponse.model_validate_json(...)`
6. Persist + route decisions exactly as current pipeline

Critical invariant:
- Stage B prompt MUST consume only structured Stage A artifacts (market snapshot + category + validated sentiment), not ad hoc raw side-channel inputs.

## 5. Latency + Failure Semantics (Non-Negotiable)

1. Grok timeout is strict `2.0s` per evaluation item.
2. On timeout, network error, non-JSON, or schema failure: continue chain with neutral sentiment.
3. Neutral fallback values:
   - `sentiment_score = Decimal("0.0")`
   - `tweet_volume_delta = 0`
   - `top_narrative_summary = "Sentiment unavailable in 2.0s window; neutral fallback applied."`
4. Sentiment failure MUST NOT block or fail the full evaluation pipeline.
5. All fallback events must be logged with reason code (`SKIPPED_CATEGORY`, `TIMEOUT`, `HTTP_ERROR`, `SCHEMA_ERROR`).

## 6. Financial Integrity + Gatekeeper Invariants

1. Sentiment is an upstream signal only; Gatekeeper remains final execution boundary.
2. No EV/Kelly formula changes are in scope for WI-12.
3. If sentiment is used in any mathematical weighting path (current or future), it MUST be converted and handled as `Decimal` (no float math in weighting logic).
4. `dry_run` enforcement at Layer 4 remains unchanged and mandatory.

## 7. Acceptance Criteria

1. Category-gated Grok behavior is deterministic and enforced exactly (`CRYPTO|POLITICS` only).
2. Grok call has strict timeout `2.0s` and never stalls queue flow.
3. `SentimentResponse` schema validation always occurs before PromptFactory injection.
4. Final Claude output still routes through `LLMEvaluationResponse` as terminal gate.
5. PromptFactory includes a dedicated sentiment context block for Stage B.
6. Audit logs include sentiment source status and effective sentiment values used in evaluation context.
7. Full tests pass with no coverage regression below 80%.

## 8. Verification Checklist

1. Unit test category trigger matrix (`CRYPTO/POLITICS` call; `SPORTS/GENERAL` skip).
2. Unit test timeout fallback path -> neutral sentiment.
3. Unit test malformed Grok JSON -> neutral sentiment.
4. Unit test PromptFactory includes sentiment block and fallback narrative when applicable.
5. Integration test Claude pipeline still validates via `LLMEvaluationResponse`.
6. Full suite: `pytest --asyncio-mode=auto tests/`
