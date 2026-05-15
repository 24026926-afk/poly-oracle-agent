# Business Logic - WI-55 DeepSeek Backtest Calibration and Paper-Trading Readiness Gate

## Objective

Prove whether DeepSeek V4 Pro can be used safely and economically as the primary LLM evaluation provider in `DRY_RUN=true` paper trading before any operator recommendation is made.

This WI is a calibration and readiness gate, not a trading-strategy rewrite. It must compare provider behavior through the existing backtest and validation paths, preserve `LLMEvaluationResponse` as the terminal Gatekeeper, keep all money and cost arithmetic Decimal-native, and prevent DeepSeek from being recommended as primary unless deterministic readiness criteria pass.

## Data Models

Pydantic schema names only:

- `LLMProvider`
- `LLMProviderUsage`
- `LLMProviderMetadata`
- `LLMProviderRuntimeContext`
- `LLMProviderComparisonConfig`
- `LLMProviderComparisonRun`
- `LLMProviderComparisonResult`
- `LLMProviderDecisionMetrics`
- `LLMProviderCalibrationMetrics`
- `LLMProviderCostMetrics`
- `LLMProviderLatencyMetrics`
- `LLMProviderReadinessVerdict`
- `LLMProviderReadinessReason`
- `LLMProviderCalibrationRecommendation`
- `LLMProviderComparisonReport`
- `BacktestConfig`
- `BacktestReport`
- `BacktestDecision`
- `BacktestValidationReport`
- `LiveReadinessVerdict`
- `LLMEvaluationResponse`

## Key Rules

1. Provider comparison extends the existing real-data backtest and validation paths. It must not duplicate the backtesting engine.
2. Provider comparison runs only in `DRY_RUN=true`. It must not sign, broadcast, or authorize live orders.
3. DeepSeek and Anthropic provider calls continue to flow through the canonical `ClaudeClient` class and the existing `anthropic` SDK provider configuration.
4. `LLMEvaluationResponse` remains the terminal Gatekeeper for every provider-produced decision.
5. DeepSeek cannot be recommended as primary unless its typed readiness verdict is `PROVIDER_READY_FOR_DRY_RUN_PRIMARY`.
6. Readiness verdict derivation is deterministic and ordered from most severe rejection to least severe recommendation.
7. Minimum readiness gates include JSON validity, Gatekeeper validation, calibration quality, EV/PnL quality where outcomes exist, latency, and cost reduction.
8. Provider comparison reports include JSON validity rate, Gatekeeper pass/fail counts, decision distribution, confidence distribution, EV calibration, realized PnL/EV calibration, latency distribution, token usage, estimated cost, budget block counts, and cooldown block counts.
9. All money, provider cost, token-price, EV, realized PnL, and calibration arithmetic uses `Decimal`.
10. Raw `float` is forbidden in money, price, EV, Kelly, PnL, exposure, sizing, token-pricing, and estimated-cost paths.
11. Claude comparison is sampled only when explicitly enabled by configuration. Full-time Claude/DeepSeek shadow mode is prohibited by default.
12. Sampled Claude audit mode must use a bounded sample fraction and must not double provider spend by default.
13. Missing provider usage data uses conservative configured defaults for accounting. It must not silently report zero cost or zero tokens.
14. Budget guard and market cooldown block counts are captured as comparison metrics, not treated as provider successes.
15. A provider blocked by budget or cooldown must not be counted as a valid JSON decision or Gatekeeper pass.
16. Calibration recommendations are provider-specific and may include confidence threshold, EV threshold, max output tokens, and cooldown/budget settings.
17. Calibration recommendations do not change live configuration automatically. Operator action is required after reviewing the report.
18. Reports are written only under approved docs paths, preferably `docs/backtests/`.
19. Reports must not contain raw prompts, raw reasoning text, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, or other high-cardinality sensitive identifiers.
20. Metrics and logs use bounded provider, model, verdict, and reason labels only.
21. Historical outcome calibration is used only when lookahead-safe outcomes exist in the dataset.
22. When historical outcomes are unavailable, the report must distinguish missing outcome coverage from provider readiness success.
23. DeepSeek rejection for JSON validity, negative EV/PnL calibration, excessive cost, or excessive latency is fail-closed and blocks primary recommendation.
24. `DRY_RUN=false` remains out of scope regardless of readiness verdict.
25. A passing DeepSeek readiness verdict authorizes only a paper-trading primary-provider recommendation, never live trading.

## Edge Cases

1. DeepSeek returns malformed JSON above the configured tolerance: verdict is `PROVIDER_REJECTED_FOR_JSON_VALIDITY`.
2. DeepSeek returns valid JSON but repeatedly fails `LLMEvaluationResponse`: verdict is rejection or sampled-audit-only according to deterministic ordering.
3. DeepSeek has acceptable validity but poor confidence or EV calibration: verdict is `PROVIDER_NEEDS_THRESHOLD_RECALIBRATION`.
4. DeepSeek has negative realized PnL/EV calibration where outcomes exist: verdict is `PROVIDER_REJECTED_FOR_NEGATIVE_EV`.
5. DeepSeek is cheaper but materially slower than configured latency bounds: verdict is `PROVIDER_REJECTED_FOR_COST_OR_LATENCY`.
6. DeepSeek is faster or cheaper but does not meet Gatekeeper or calibration thresholds: cost advantage does not override safety rejection.
7. Anthropic sampling is disabled: comparison still produces DeepSeek metrics and clearly records that Claude audit sampling was not run.
8. Claude sampling fraction is enabled at a bounded rate: only sampled contexts call Anthropic; unsampled contexts must not incur Claude cost.
9. Provider usage fields are missing or malformed: conservative token and cost defaults are applied and surfaced in accounting metrics.
10. Budget guard blocks a provider call: report records a typed budget block and no provider decision is fabricated.
11. Market cooldown blocks a provider call: report records a typed cooldown block and no provider decision is fabricated.
12. Historical outcomes are missing for a subset of markets: outcome-based calibration excludes those rows and reports coverage explicitly.
13. Historical data contains invalid, stale, crossed, or non-positive quotes: existing validation gates skip them with typed reasons.
14. Report generation path attempts to escape approved docs directories: generation fails closed.
15. Report text contains a detected secret or high-cardinality token identifier: generation fails closed or redacts before write.
16. `DRY_RUN=false` appears in comparison config: validation fails before provider calls or backtest execution.
17. DeepSeek passes readiness: recommendation remains limited to `DRY_RUN=true` primary paper trading.

## Invariants

1. `LLMEvaluationResponse` remains the terminal Gatekeeper for all provider decisions.
2. `ClaudeClient` remains the canonical evaluation client class for Anthropic and DeepSeek.
3. No live signing, broadcasting, or `DRY_RUN=false` authorization is introduced by this WI.
4. Backtest comparison uses existing backtesting and validation paths instead of duplicating trading logic.
5. All money, EV, PnL, token-cost, and estimated-spend arithmetic is Decimal-native.
6. Provider readiness verdicts are typed, deterministic, and fail closed.
7. DeepSeek cannot be recommended as primary unless JSON validity, Gatekeeper validation, calibration, EV/PnL, latency, and cost criteria pass.
8. Claude audit sampling is bounded and disabled by default to avoid full-time shadow-mode spend.
9. Reports are secret-free, prompt-free, reasoning-free, and constrained to approved docs paths.
10. Metrics and logs remain low-cardinality and secret-safe.
11. Missing provider usage fields cannot become zero-cost accounting.
12. Calibration recommendations are advisory and never mutate runtime provider settings automatically.
13. A passing readiness gate applies only to paper trading in `DRY_RUN=true`.
14. Tests cover verdict ordering, report redaction, Decimal cost math, JSON validity failure, negative EV rejection, sampled Claude audit mode, and the `DRY_RUN=true` invariant.
