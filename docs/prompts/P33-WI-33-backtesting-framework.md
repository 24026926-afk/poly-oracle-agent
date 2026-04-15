# P33-WI-33 — Backtesting Framework Implementation Prompt

## Execution Target

- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi33-backtesting-framework` from `develop`, keep commits atomic, and open a PR back to `develop`

## Active Agents

- `.agents/rules/risk-auditor.md`
- `.agents/rules/async-architect.md`
- `.agents/rules/security-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-33 for Phase 10: an offline backtesting framework that replays historical CLOB JSON data through the full evaluation and dry-run execution path and emits a structured performance report.

This work item is not a toy simulator and it is not a second strategy engine. The backtester must reuse the production evaluation stack:

1. `DataAggregator`
2. `PromptFactory`
3. `ClaudeClient`
4. `LLMEvaluationResponse`
5. `ExecutionRouter` with `dry_run=True`

The live ingestion layer is bypassed entirely. Historical JSON files are the only replay input.

## Objective & Scope

### In Scope

1. `BacktestRunner` in `src/backtest_runner.py`
2. `BacktestDataLoader` in `src/backtest_runner.py`
3. `BacktestConfig` in `src/schemas/execution.py`
4. `BacktestReport` in `src/schemas/execution.py`
5. `BacktestMarketStats` in `src/schemas/execution.py`
6. `BacktestDecision` in `src/schemas/execution.py`
7. CLI entry point: `python -m src.backtest_runner --data-dir <dir> [--config <yaml>] [--output <json>]`
8. Structlog audit events: `backtest.started`, `backtest.market_loaded`, `backtest.decision`, `backtest.completed`, `backtest.error`

### Out of Scope

1. Live WebSocket or REST ingestion
2. Walk-forward optimization, Monte Carlo simulation, or parameter sweeps
3. Database persistence of backtest output
4. Gatekeeper rewrites or prompt-system rewrites
5. Parallel multi-config backtesting
6. Any live order signing or broadcasting

## Mandatory Context Hydration

Read all of the following before any edits:

1. `AGENTS.md`
2. `STATE.md`
3. `docs/PRD-v10.0.md` and focus on the WI-33 section
4. `docs/business_logic/business_logic_wi33.md`
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/schemas/execution.py`
9. `src/agents/context/aggregator.py`
10. `src/agents/context/prompt_factory.py`
11. `src/agents/evaluation/claude_client.py`
12. `src/agents/execution/execution_router.py`
13. Existing orchestrator tests and adjacent Phase 10 tests to protect regressions

Do not proceed if this context is not loaded.

## Critical Invariants

### 1. Hard `dry_run=True` Gate

Backtesting must always run with `dry_run=True`. This is a hard invariant. Reject `dry_run=False` immediately.

### 2. Strict Chronological Replay

Replay order must be strictly chronological across the historical dataset. No shuffling, no random sampling, and no replay model that breaks timestamp order.

### 3. Zero Imports from Live Ingestion Modules

`src/backtest_runner.py` must have zero imports from:

1. `src/agents/ingestion/ws_client.py`
2. `src/agents/ingestion/rest_client.py`

Historical JSON is the only approved input source for WI-33.

### 4. Zero Database Writes

The backtest path must not write to SQLite under any condition. Report output is JSON file only.

### 5. Strict Decimal Math

All financial math must remain `Decimal`-only. No `float` in bankroll handling, sizing, report metrics, or per-market summaries.

### 6. Full Gatekeeper Integrity

`LLMEvaluationResponse` remains the terminal authority in backtest mode. WI-33 must not duplicate or bypass production validation logic.

### 7. Sequential Single-Run Execution

The replay loop inside a single backtest run is sequential. No `asyncio.gather` or parallel snapshot processing within one run.

## 3-Phase Agentic TDD Mandate

You must follow RED → GREEN → REGRESSION in order. No production edits before the RED tests exist and fail for the expected reasons.

## Phase 1 — RED

Create the failing test suite first.

### Test File A

Create `tests/unit/test_wi33_backtest_data_loader.py`.

Required RED coverage:

1. Valid `{token_id}_{date}.json` files are parsed into replay-ready historical snapshots.
2. Multiple files for the same `token_id` are merged correctly.
3. `start_date` and `end_date` filters are applied correctly.
4. Loaded snapshots are sorted chronologically.
5. Empty directories or empty valid files are handled deterministically.
6. Malformed JSON raises `BacktestDataError`.
7. Missing required fields raise `BacktestDataError`.
8. Invalid numeric values raise `BacktestDataError`.
9. Invalid timestamps raise `BacktestDataError`.
10. Loaded monetary fields remain `Decimal`-safe at the schema boundary.

### Test File B

Create `tests/integration/test_wi33_backtest_runner.py`.

Required RED coverage:

1. `BacktestRunner` refuses to run when `dry_run=False`.
2. Historical snapshots are replayed in strict chronological order.
3. `DataAggregator`, `PromptFactory`, and `ClaudeClient` are invoked for each replayed snapshot.
4. The full Gatekeeper path is exercised for each replayed snapshot.
5. Gatekeeper-passed decisions call `ExecutionRouter` with `dry_run=True`.
6. Gatekeeper-failed decisions do not route live execution and are recorded as HOLD-style outcomes.
7. `BacktestDecision` records are produced for every replayed snapshot.
8. `BacktestReport` contains the PRD-required fields:
   `total_trades`, `win_rate`, `net_pnl_usdc`, `max_drawdown_usdc`, `sharpe_ratio`, `per_market_stats`, `decisions`, `started_at_utc`, `completed_at_utc`, `config_snapshot`.
9. The backtest path performs zero database writes.
10. The CLI entry point writes JSON output for a completed run.
11. `src/backtest_runner.py` has no imports from live WS/REST ingestion modules.

### RED Gate

Run the new WI-33 tests and confirm they fail before implementation begins:

1. `pytest --asyncio-mode=auto tests/unit/test_wi33_backtest_data_loader.py -v`
2. `pytest --asyncio-mode=auto tests/integration/test_wi33_backtest_runner.py -v`

Commit only the RED tests once failure is confirmed.

## Phase 2 — GREEN

Implement the minimum production changes required to make the RED suite pass.

### Step 1 — Add the WI-33 Schemas

In `src/schemas/execution.py`, add:

1. `BacktestConfig`
2. `BacktestReport`
3. `BacktestMarketStats`
4. `BacktestDecision`

Requirements:

1. Match the PRD field list exactly.
2. Keep the models frozen.
3. Reject `float` on monetary and threshold fields.
4. Preserve the default Quarter-Kelly value of `Decimal("0.25")` in `BacktestConfig`.

### Step 2 — Implement `BacktestDataLoader`

In `src/backtest_runner.py`, implement `BacktestDataLoader` so it:

1. Reads `{token_id}_{date}.json` files from `data_dir`.
2. Parses and validates historical snapshot records.
3. Applies optional date filtering.
4. Merges records by `token_id`.
5. Returns replay input in chronological order.
6. Raises `BacktestDataError` for malformed files instead of silently degrading.

### Step 3 — Implement `BacktestRunner`

In `src/backtest_runner.py`, implement `BacktestRunner` so it:

1. Enforces `dry_run=True` as a hard gate.
2. Loads replay data through `BacktestDataLoader`.
3. Replays snapshots chronologically.
4. Calls `DataAggregator`, `PromptFactory`, `ClaudeClient`, and `LLMEvaluationResponse` per snapshot.
5. Calls `ExecutionRouter` only for Gatekeeper-passed decisions and only with `dry_run=True`.
6. Records `BacktestDecision` for every replayed snapshot.
7. Computes `BacktestMarketStats` and `BacktestReport` using strict `Decimal` arithmetic.
8. Performs zero database writes.

### Step 4 — Add the CLI Entry Point

Support:

1. `python -m src.backtest_runner --data-dir <dir>`
2. Optional `--config <yaml>`
3. Optional `--output <json>`

Requirements:

1. JSON report output is the only persistence path.
2. Respect repo dependency policy when implementing YAML loading. Do not add a new dependency without approval.
3. Emit the PRD-required structlog events.

### Step 5 — Hold the Isolation Line

Before leaving GREEN, explicitly verify:

1. No imports from live ingestion modules were introduced.
2. No database write path was introduced.
3. No `float` entered any financial path.
4. No Gatekeeper bypass was introduced.

## Phase 3 — REGRESSION

The Definition of Done for WI-33 is stricter than the PRD floor. Preserve the repo’s current baseline from `STATE.md`.

### Required Verification

1. Re-run the WI-33 targeted tests and confirm green.
2. Run the full regression suite:
   `pytest --asyncio-mode=auto tests/`
3. Keep the existing baseline intact:
   at least `649` tests passing.
4. Run coverage:
   `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m`
5. Keep coverage at or above `94%`.
6. Confirm `src/backtest_runner.py` contains zero imports from live WS/REST ingestion modules.
7. Confirm the backtest path performs zero database writes.
8. Confirm the report output path is JSON only.

### MAAP Review Requirement

Because WI-33 touches `src/schemas/` and `src/`, MAAP applies before commit:

1. Maker runs the tests and stages the change.
2. Maker outputs the staged diff.
3. Checker reviews for:
   Decimal violations,
   Gatekeeper bypasses,
   business-logic drift from Kelly and risk rules,
   and any backtest-to-live coupling through ingestion or persistence.
4. Any finding in those categories must be fixed before commit.

## Definition of Done

WI-33 is complete only when all of the following are true:

1. `BacktestRunner` exists and is the public replay entry point.
2. `BacktestDataLoader` exists and loads historical JSON correctly.
3. Replay is strict and chronological.
4. The full pipeline runs per snapshot.
5. `ExecutionRouter` is called only with `dry_run=True`.
6. `BacktestReport`, `BacktestMarketStats`, and `BacktestDecision` match the PRD.
7. JSON report output works from the CLI entry point.
8. No live ingestion imports exist in the backtest module.
9. No database writes occur anywhere in the backtest path.
10. Full regression remains at `649+` passing tests.
11. Coverage remains at `94%` or higher.
