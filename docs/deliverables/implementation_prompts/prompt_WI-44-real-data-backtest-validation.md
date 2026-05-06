# Implementation Prompt - WI-44 Real-Data Backtest Validation

## Session Context

You are working in `poly-oracle-agent` on Phase 13: Real-Data Validation & 24/7 Readiness.

Current baseline:

- WI-43 prepares historical Polymarket data for `BacktestDataLoader`.
- WI-44 consumes the WI-43 dataset and produces a typed live-readiness verdict.
- The existing backtesting coordinator is `src/backtest_runner.py`.
- Backtesting must remain dry-run only.
- Phase-level kill criterion: if real-data validation does not show defensible edge, live trading remains prohibited.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v13.0.md`
- `docs/deliverables/business_logic/business_logic_WI-44-real-data-backtest-validation.md`
- `src/backtest_runner.py`
- `src/schemas/execution.py`
- WI-43 output contracts and manifest format

## Objective

Implement a validation layer that runs `BacktestRunner` against real historical data and emits a typed baseline report with a conservative live-readiness verdict.

## Inputs

- WI-43 historical dataset directory.
- WI-43 dataset manifest.
- `BacktestConfig` with `dry_run=True`.
- Existing `BacktestRunner` output.
- Phase 13 verdict requirements from `docs/PRD-v13.0.md`.

## Outputs

- `src/backtesting/validation_report.py`
- `src/backtesting/live_readiness.py`
- `scripts/run_real_data_backtest.py`
- `tests/unit/test_WI-44-real-data-backtest-validation.py`
- `tests/integration/test_WI-44-real-data-backtest-validation.py`
- `docs/backtests/phase13_baseline.json`
- `docs/backtests/phase13_baseline.md`

## Acceptance Criteria

1. `python scripts/run_real_data_backtest.py --data-dir data/historical --output docs/backtests/phase13_baseline.json` runs end to end.
2. Report includes PnL, drawdown, action distribution, calibration metrics, per-market stats, and typed verdict.
3. Negative or statistically weak results produce an explicit non-live-ready verdict.
4. Backtest path performs zero database writes.
5. Backtest path never constructs live signer, broadcaster, or state-mutating execution paths.
6. All report financial metrics use `Decimal` internally.
7. Tests cover PASS and every failure verdict path.
8. Tests cover empty dataset, insufficient trades, negative PnL, excessive drawdown, weak calibration, and data-quality failure.
9. No prompt, threshold, Kelly, or risk-parameter optimization is added.
10. Targeted WI tests pass.
11. Full regression remains compatible with the documented baseline and coverage does not fall below 80%.

## Anti-Patterns

- Do not set or recommend `DRY_RUN=false`.
- Do not tune prompts, model settings, thresholds, Kelly parameters, or risk gates in WI-44.
- Do not use raw `float` for PnL, drawdown, Sharpe, EV, calibration, win-rate, or sizing math.
- Do not bypass `BacktestRunner`.
- Do not bypass `LLMEvaluationResponse`.
- Do not write to the runtime database.
- Do not instantiate live signer or broadcaster components.
- Do not hide weak or negative results behind a PASS verdict.
- Do not include secrets, wallet details, prompt text, or private keys in reports.
- Do not treat insufficient data as live-ready.

## Dependencies

- WI-43 Historical Polymarket Dataset Pipeline.
- WI-33 Backtesting Framework.
- Existing `BacktestRunner`, `BacktestConfig`, and report schemas.
- Existing Decimal and Gatekeeper invariants.

## Target Layer

Offline validation layer. This WI sits after historical ingestion and before any operational hardening or live-trading consideration.
