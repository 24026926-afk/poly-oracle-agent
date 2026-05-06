# Business Logic - WI-44 Real-Data Backtest Validation

## Objective

Run `BacktestRunner` against the WI-43 historical dataset and produce a typed baseline report that determines whether the current decision pipeline shows a defensible historical edge.

## Data Models

Pydantic schema names only:

- `BacktestConfig`
- `BacktestDecision`
- `BacktestMarketStats`
- `BacktestReport`
- `LiveReadinessVerdict`
- `BacktestValidationReport`
- `BacktestValidationSummary`
- `BacktestCalibrationBucket`
- `BacktestActionDistribution`
- `BacktestDataQualitySummary`

## Key Rules

1. WI-44 is validation-only. It must never enable live trading or recommend `DRY_RUN=false`.
2. `BacktestRunner` must be invoked with `BacktestConfig.dry_run=True`.
3. The report must answer one question directly: whether the current decision pipeline shows historical edge on real data.
4. The verdict must be typed and conservative. Weak data quality, insufficient trade count, negative PnL, unacceptable drawdown, or weak calibration must produce a non-live-ready verdict.
5. Valid verdicts must include at least `PASS`, `FAIL_NEGATIVE_PNL`, `FAIL_DRAWDOWN`, `FAIL_INSUFFICIENT_TRADES`, `FAIL_WEAK_CALIBRATION`, and `FAIL_DATA_QUALITY`.
6. The validation run must not optimize prompts, thresholds, Kelly settings, confidence thresholds, or risk parameters.
7. The report must include total snapshots, total decisions, BUY/HOLD/SKIP distribution, total trades, win rate, net PnL, max drawdown, Sharpe ratio, average EV, realized EV calibration, confidence calibration by bucket, per-market stats, and verdict.
8. All financial and calibration math must use `Decimal`.
9. Backtest output is written to JSON and markdown files under `docs/backtests/`.
10. No runtime database writes are allowed.
11. No live signer, broadcaster, Web3 transaction path, or execution mutation path may be constructed by WI-44.
12. A non-PASS verdict is a phase-level kill criterion: live execution remains prohibited and the next phase must address strategy, model, or risk redesign.

## Edge Cases

1. Empty dataset: return `FAIL_DATA_QUALITY`.
2. Dataset loads but produces too few decisions: return `FAIL_INSUFFICIENT_TRADES` or `FAIL_DATA_QUALITY`, depending on configured threshold.
3. Dataset produces decisions but zero trades: return `FAIL_INSUFFICIENT_TRADES`.
4. Net PnL below zero: return `FAIL_NEGATIVE_PNL`.
5. Max drawdown breaches configured threshold: return `FAIL_DRAWDOWN`.
6. Confidence buckets are sparse or uninformative: return `FAIL_WEAK_CALIBRATION`.
7. EV estimates do not align with realized outcomes: return `FAIL_WEAK_CALIBRATION`.
8. Malformed WI-43 dataset files: fail with typed data-quality error rather than continuing silently.
9. Backtest execution raises validation errors from `LLMEvaluationResponse`: preserve the failure reason in the report.
10. Existing `BacktestReport` lacks a metric required by WI-44: compute it in the validation layer without changing historical replay semantics.
11. Output path parent directory is missing: create the docs output directory only, not arbitrary external paths.
12. Partial report generation failure: exit non-zero and do not publish a misleading PASS verdict.

## Invariants

1. `BacktestRunner` remains dry-run only.
2. `LLMEvaluationResponse` remains the terminal Gatekeeper in replay.
3. No live signing, broadcasting, or state-mutating execution can occur.
4. No raw `float` is permitted in PnL, drawdown, Sharpe, EV, calibration, win-rate, or sizing math.
5. WI-44 does not tune or optimize strategy behavior.
6. Verdict generation is deterministic for the same input report and configuration.
7. Negative or statistically weak historical results cannot be treated as live-ready.
8. Reports must be auditable and machine-readable.
9. Secrets, wallet data, prompt text, and private keys must not appear in reports.
10. Backtest outputs are file artifacts only; runtime persistence remains untouched.
