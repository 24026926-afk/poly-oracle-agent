# WI-33 Business Logic — Backtesting Framework

## Active Agents + Constraints

- `.agents/rules/risk-auditor.md` — All backtest financial math is `Decimal`-only. This applies to bankroll handling, position sizing, win rate, net PnL, maximum drawdown, and Sharpe ratio. The Quarter-Kelly default remains `0.25` unless explicitly overridden through `BacktestConfig`.
- `.agents/rules/async-architect.md` — `BacktestRunner` is an offline replay path, not a new live runtime topology. The replay loop is single-process and sequential. No `asyncio.gather`, no background fan-out, and no queue rewiring inside a single backtest run.
- `.agents/rules/security-auditor.md` — `dry_run=True` is a hard safety gate, not a convenience flag. Backtesting must never sign, broadcast, or persist anything. JSON report output is the only allowed side effect.
- `.agents/rules/test-engineer.md` — WI-33 requires RED-first coverage for historical JSON loading, malformed file handling, strict replay ordering, `dry_run` enforcement, full pipeline integration, and zero-live-side-effect guarantees.

## 1. Objective

WI-33 introduces an offline simulator that replays historical CLOB JSON snapshots through the same decision pipeline used in production. The backtester exists to validate strategy changes before real capital is exposed.

The canonical offline path is:

1. `BacktestDataLoader` reads historical JSON files from disk.
2. `BacktestRunner` replays snapshots in strict chronological order.
3. Each snapshot flows through `DataAggregator`, `PromptFactory`, `ClaudeClient`, and the `LLMEvaluationResponse` Gatekeeper.
4. Gatekeeper-passed decisions are routed through `ExecutionRouter` with `dry_run=True`.
5. The run produces a `BacktestReport` and writes JSON output only.

This work item is additive. It does not modify the live ingestion path and it does not create a second decision model.

## 2. Scope Boundaries

### In Scope

1. `BacktestRunner` in `src/backtest_runner.py`.
2. `BacktestDataLoader` in `src/backtest_runner.py`.
3. `BacktestConfig` in `src/schemas/execution.py`.
4. `BacktestReport` in `src/schemas/execution.py`.
5. `BacktestMarketStats` in `src/schemas/execution.py`.
6. `BacktestDecision` in `src/schemas/execution.py`.
7. CLI entry point: `python -m src.backtest_runner --data-dir <dir> [--config <yaml>] [--output <json>]`.
8. Structlog audit events: `backtest.started`, `backtest.market_loaded`, `backtest.decision`, `backtest.completed`, and `backtest.error`.

### Out of Scope

1. Live market data injection.
2. WebSocket or REST ingestion for backtest input.
3. Walk-forward optimization, cross-validation, Monte Carlo simulation, or bootstrapping.
4. Parallel config sweeps.
5. Real-time dashboarding.
6. Database persistence of backtest artifacts.
7. Modifications to `ClaudeClient`, `PromptFactory`, `LLMEvaluationResponse`, or Gatekeeper internals.

## 3. Canonical Components

### 3.1 `BacktestDataLoader`

`BacktestDataLoader` is the only approved historical input loader for WI-33.

Its responsibilities are:

1. Read historical JSON files from `BacktestConfig.data_dir`.
2. Accept files using the PRD filename pattern: `{token_id}_{date}.json`.
3. Parse each file as one market’s historical order-book snapshot stream.
4. Validate each record contains:
   `timestamp_utc`, `best_bid`, `best_ask`, and `midpoint`.
5. Apply optional `start_date` and `end_date` filters from `BacktestConfig`.
6. Merge multiple files for the same `token_id`.
7. Return replay-ready snapshots sorted in ascending chronological order.
8. Raise `BacktestDataError` for malformed files instead of silently skipping bad data.

`BacktestDataLoader` is a file-system reader only. It has no authority to call live services, no authority to mutate state, and no authority to persist anything.

### 3.2 `BacktestRunner`

`BacktestRunner` is the offline replay coordinator for WI-33.

Its responsibilities are:

1. Enforce `dry_run=True` as a hard gate before any replay begins.
2. Load historical snapshots through `BacktestDataLoader`.
3. Replay snapshots in strict chronological order.
4. Pass each snapshot through the production decision stack:
   `DataAggregator` → `PromptFactory` → `ClaudeClient` → `LLMEvaluationResponse`.
5. Call `ExecutionRouter` only for Gatekeeper-passed decisions, and only with `dry_run=True`.
6. Record one `BacktestDecision` per replayed snapshot.
7. Aggregate all replay outcomes into a `BacktestReport`.
8. Return structured results for JSON serialization by the CLI entry point.

`BacktestRunner` is not a live orchestrator clone. It is a deterministic offline replay path that reuses production evaluation and routing logic without live side effects.

## 4. Required Data Contracts

### 4.1 `BacktestConfig`

`BacktestConfig` is the frozen input contract for one backtest run. It must contain:

1. `data_dir: str`
2. `start_date: date | None`
3. `end_date: date | None`
4. `initial_bankroll_usdc: Decimal`
5. `kelly_fraction: Decimal` with default `Decimal("0.25")`
6. `min_confidence: Decimal` with default `Decimal("0.75")`
7. `min_ev_threshold: Decimal` with default `Decimal("0.02")`
8. `dry_run: bool` with default `True`

Business rules:

1. `dry_run` defaults to `True`, but the stronger invariant is that a backtest run must reject `False`.
2. `kelly_fraction` is the only sanctioned sizing override for backtest runs. The production default remains Quarter-Kelly.
3. `min_confidence` and `min_ev_threshold` are the only sanctioned Gatekeeper threshold overrides documented in the PRD.
4. All monetary and threshold fields must reject `float` and preserve `Decimal` precision.

### 4.2 `BacktestDecision`

`BacktestDecision` is the audit record for a single replayed snapshot. It must contain:

1. `token_id: str`
2. `timestamp_utc: datetime`
3. `decision: bool`
4. `action: str`
5. `position_size_usdc: Decimal`
6. `ev: Decimal`
7. `confidence: Decimal`
8. `gatekeeper_result: str`
9. `reason: str`

Business rules:

1. One `BacktestDecision` is recorded per evaluated snapshot.
2. Gatekeeper failure records a HOLD-style outcome rather than routing execution.
3. Gatekeeper pass records the decision plus the dry-run routing result.
4. The decision record is the primary audit trail for later report generation.

### 4.3 `BacktestMarketStats`

`BacktestMarketStats` is the per-market summary block inside the final report. It must contain:

1. `token_id: str`
2. `total_decisions: int`
3. `trades_executed: int`
4. `win_rate: Decimal`
5. `net_pnl_usdc: Decimal`

Business rules:

1. Stats are keyed by `token_id`.
2. They are derived from recorded backtest decisions, not from duplicated strategy logic.
3. All numeric calculations remain `Decimal`-safe.

### 4.4 `BacktestReport`

`BacktestReport` is the frozen result contract for a completed run. It must contain:

1. `total_trades: int`
2. `win_rate: Decimal`
3. `net_pnl_usdc: Decimal`
4. `max_drawdown_usdc: Decimal`
5. `sharpe_ratio: Decimal`
6. `per_market_stats: dict[str, BacktestMarketStats]`
7. `decisions: list[BacktestDecision]`
8. `started_at_utc: datetime`
9. `completed_at_utc: datetime`
10. `config_snapshot: BacktestConfig`

Business rules:

1. The report is the only persisted output of a backtest run.
2. The report must be serializable to JSON without losing Decimal correctness at the schema boundary.
3. The report must be generated from replay outcomes, not from a separate shortcut calculation path.

## 5. Replay Rules

The replay model for WI-33 is fixed:

1. Historical input comes from JSON files on disk only.
2. Replay order is chronological, not randomized.
3. The Gatekeeper remains the final decision authority.
4. Execution routing happens only after a Gatekeeper pass.
5. Routing remains dry-run only.
6. The replay loop is sequential and deterministic.
7. There is no market discovery step in backtest mode.
8. There is no live WebSocket subscription in backtest mode.

For multi-market datasets, chronological ordering is global. A backtest must not process each market independently and then stitch results together later if that would break timestamp order across markets.

## 6. Critical Invariants

### 6.1 Hard `dry_run` Gate

`BacktestRunner` must run with `dry_run=True` under every code path. This is a hard invariant, not a default-value suggestion. If a config attempts to run with `dry_run=False`, the runner must stop immediately.

### 6.2 Strict Chronological Replay

Replay order must follow historical time exactly. No randomization, batching that reorders events, or per-market replay that ignores cross-market timestamp ordering is allowed.

### 6.3 Zero Imports from Live WS/REST Ingestion Modules

WI-33 must not depend on `CLOBWebSocketClient` or `GammaRESTClient`. Historical JSON is the only source of replay data. Importing live ingestion modules into `src/backtest_runner.py` is a design violation.

### 6.4 Zero Database Writes

Backtesting is read-only with respect to system state. No repository writes, no ORM session writes, no migrations, and no persistence side effects are allowed. Output is JSON only.

### 6.5 Strict Decimal Math

All bankroll, sizing, PnL, win-rate, drawdown, and Sharpe-ratio calculations must remain `Decimal`-only. `float` is prohibited in every financially sensitive path.

### 6.6 Production Gatekeeper Intact

Backtest mode must invoke the full `LLMEvaluationResponse` validation chain. WI-33 must reuse production validation rules rather than duplicate or simplify them. The only sanctioned backtest-time threshold overrides are those explicitly carried in `BacktestConfig`.

### 6.7 Single-Process, Single-Thread Replay

Inside a single run, the replay engine is serial and deterministic. No `asyncio.gather`, task fan-out, or parallel execution across markets is allowed inside the replay loop.

### 6.8 JSON Output Only

The report is written as JSON to disk. No database table, queue, cache, or external sink is part of the persistence model for WI-33.

## 7. Implementation Intent

The business intent of WI-33 is not to create a toy simulator. It is to create a faithful offline execution path that answers one question:

Would the current production decision stack have approved and sized these historical opportunities, and what would the resulting performance report look like under dry-run replay?

That means:

1. No alternative sizing formula.
2. No custom backtest-only Gatekeeper.
3. No bypass around `ExecutionRouter`.
4. No live ingestion dependencies.
5. No database coupling.

## 8. Acceptance Translation

WI-33 is business-logic complete only when all of the following are true:

1. `BacktestRunner` is the single public replay entry point.
2. `BacktestDataLoader` is the single historical JSON loader.
3. Historical snapshots are replayed chronologically.
4. The full evaluation pipeline is invoked per snapshot.
5. Gatekeeper validation is identical in authority to production.
6. `ExecutionRouter` is called only with `dry_run=True`.
7. `BacktestReport` contains the PRD-required fields.
8. Malformed files raise `BacktestDataError`.
9. No live ingestion imports exist in the backtest module.
10. No database writes occur anywhere in the backtest path.
11. All financial math remains strict `Decimal`.
