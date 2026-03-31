# PRD v8.0 - Poly-Oracle-Agent Phase 8

Source inputs: `docs/PRD-v7.0.md`, `STATE.md`, `docs/archive/ARCHIVE_PHASE_7.md`, `docs/business_logic/business_logic_wi23.md`, `src/agents/execution/pnl_calculator.py`, `src/agents/execution/exit_strategy_engine.py`, `src/db/repositories/position_repository.py`, `src/orchestrator.py`, `src/core/config.py`, `AGENTS.md`.

## 1. Executive Summary

Phase 7 completed the downstream exit lifecycle: open positions are periodically scanned, actionable exits are routed as signed SELL orders, and realized PnL is computed and persisted at position closure. But the system still operates without portfolio-level visibility. No component aggregates exposure across open positions, no component produces lifecycle reports over closed positions, and no component raises alerts when portfolio health degrades. The operator has structlog events but no structured, queryable analytics layer.

Phase 8 adds three capabilities executed in strict dependency order:

1. **WI-23 — Portfolio Aggregator** introduces `PortfolioAggregator`, a read-only analytics component that computes a real-time aggregate snapshot of all open positions: total notional USDC, unrealized PnL, position count, and locked collateral. It runs as an optional background task with fail-open price resolution — if a live price fetch fails, entry price is used as a conservative fallback.

2. **WI-24 — Position Lifecycle Report** introduces `PositionLifecycleReporter`, a read-only reporting component that produces structured summaries over closed positions: total realized PnL, win/loss counts, average hold duration, and best/worst outcomes. It reads settled position data from `PositionRepository` and computes Decimal-only aggregate statistics.

3. **WI-25 — Alert Engine** introduces `AlertEngine`, a rule-based monitoring component that evaluates `PortfolioSnapshot` and `LifecycleReport` against configurable risk thresholds and emits typed `AlertEvent` records when limits are breached. Alert conditions include drawdown limits, stale-price concentration, and position-count ceilings. The engine is read-only — it observes and logs, but does not mutate state or halt execution.

Phase 8 preserves the four-layer async architecture and the terminal authority of `LLMEvaluationResponse`. It adds observability without introducing write paths, new queues, or execution-side mutations. All three components are read-only analytics: Decimal-only arithmetic, repository-mediated DB reads, fail-open semantics, and `dry_run`-aware structured logging.

## 2. Core Pillars

### 2.1 Portfolio-Level Exposure Visibility

Individual position tracking is insufficient for risk management. Phase 8 introduces real-time aggregation of all open-position exposure so the operator can answer: how much capital is deployed, what is the unrealized PnL across the portfolio, and how much collateral is locked? This is the foundational data layer for alert-based risk monitoring.

### 2.2 Lifecycle Performance Analytics

Closed positions carry settlement data (realized PnL, exit price, hold duration) that is only useful when aggregated. Phase 8 surfaces structured performance summaries — win/loss ratios, average PnL, and hold-time distributions — so the operator can evaluate strategy effectiveness without querying raw database rows.

### 2.3 Threshold-Based Risk Alerting

Observability data is actionable only when it triggers attention at the right moment. Phase 8 introduces a deterministic, rule-based alert engine that evaluates portfolio and lifecycle metrics against configurable thresholds and emits typed alert events. Alerts are informational — they log and notify but do not halt execution, preserving the fail-open philosophy.

## 3. Work Items

### WI-23: Portfolio Aggregator

**Objective**
Introduce `PortfolioAggregator`, a read-only analytics component that computes a real-time aggregate snapshot of all open positions: total notional exposure in USDC, unrealized PnL, position count, and locked collateral. The aggregator reads open positions from `PositionRepository.get_open_positions()`, fetches current prices from `PolymarketClient.fetch_order_book()`, and produces a typed `PortfolioSnapshot` using Decimal-only arithmetic. It runs as an optional background task in the Orchestrator on a configurable interval (default 30s), gated by the `enable_portfolio_aggregator` config flag.

The aggregator uses fail-open price resolution: if a price fetch fails for any position, `entry_price` is used as a conservative fallback, yielding `unrealized_pnl = Decimal("0")` for that position. This ensures a degraded but valid snapshot is always produced — it never blocks or raises on stale data.

**Scope Boundaries**

In scope:
- New `PortfolioAggregator` class in `src/agents/execution/portfolio_aggregator.py`
- New `PortfolioSnapshot` Pydantic model in `src/schemas/risk.py` (new file) — frozen, Decimal-validated
- `compute_snapshot() -> PortfolioSnapshot` as the sole public async entry point
- Per-position computation: `position_size_tokens = order_size_usdc / entry_price`, `current_notional = current_price * position_size_tokens`, `unrealized_pnl = (current_price - entry_price) * position_size_tokens`
- Aggregation: `total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc` as running Decimal sums
- Fail-open price resolution: `fetch_order_book()` failure falls back to `entry_price` with `positions_with_stale_price` counter
- Division-by-zero guard: `entry_price == Decimal("0")` yields `position_size_tokens = Decimal("0")`
- New `AppConfig` fields: `enable_portfolio_aggregator: bool` (default `False`), `portfolio_aggregation_interval_sec: Decimal` (default `Decimal("30")`)
- `Orchestrator._portfolio_aggregation_loop()` async method with sleep-first pattern, conditional task registration
- Graceful shutdown via existing `_tasks` cancellation
- structlog audit events: `portfolio.snapshot_computed`, `portfolio.price_fetch_failed`, `portfolio_aggregation_loop.error`

Out of scope:
- New database tables, migrations, or DB writes — this component is strictly read-only
- Historical snapshot persistence or time-series storage
- Portfolio rebalancing, risk limit enforcement, or circuit-breaker logic (deferred to WI-25 for alerting)
- Real-time WebSocket streaming of snapshots to external consumers
- Fee accounting (CLOB fees, gas costs) in notional or PnL calculations
- Modifications to `PositionTracker`, `PositionRepository`, `ExitStrategyEngine`, `ExitOrderRouter`, `PnLCalculator`, or `ExecutionRouter` internals

**Components Delivered**

| Component | Location |
|---|---|
| `PortfolioAggregator` | `src/agents/execution/portfolio_aggregator.py` |
| `PortfolioSnapshot` model | `src/schemas/risk.py` |
| `Orchestrator._portfolio_aggregation_loop()` | `src/orchestrator.py` |
| Config: `enable_portfolio_aggregator` | `src/core/config.py` |
| Config: `portfolio_aggregation_interval_sec` | `src/core/config.py` |

**Key Invariants Enforced**

1. `PortfolioAggregator` is read-only. It performs zero DB writes regardless of `dry_run`. No repository write methods are called.
2. All aggregation arithmetic (`notional`, `unrealized_pnl`, `locked_collateral`, `position_size_tokens`) is `Decimal`-only. Float is rejected at Pydantic boundary.
3. Fail-open price resolution: a failed `fetch_order_book()` never blocks snapshot computation. The position's `entry_price` is used as fallback, yielding `unrealized_pnl = Decimal("0")`.
4. The portfolio aggregation task is optional and config-gated. When `enable_portfolio_aggregator=False` (default), no task is created and zero runtime overhead is incurred.
5. `PortfolioAggregator` has zero imports from prompt, context, evaluation, or ingestion modules.
6. A failed `compute_snapshot()` within the loop is caught, logged, and retried on the next interval. The loop never terminates on a single failure.
7. The aggregation task is included in `Orchestrator._tasks` (when enabled) and cancelled during `shutdown()`.

**Acceptance Criteria**

1. `PortfolioAggregator` exists in `src/agents/execution/portfolio_aggregator.py` with `compute_snapshot() -> PortfolioSnapshot` as the sole public async entry point.
2. `PortfolioSnapshot` Pydantic model exists in `src/schemas/risk.py`, is frozen, Decimal-validated, with fields: `snapshot_at_utc`, `position_count`, `total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc`, `positions_with_stale_price`, `dry_run`.
3. `PortfolioSnapshot` rejects `float` in financial fields at Pydantic boundary.
4. Open positions are loaded via `PositionRepository.get_open_positions()`.
5. Current prices are fetched via `PolymarketClient.fetch_order_book(token_id)` per position.
6. Price-fetch failure falls back to `entry_price` with `positions_with_stale_price` incremented.
7. `total_notional_usdc`, `total_unrealized_pnl`, and `total_locked_collateral_usdc` are correct Decimal sums across all open positions.
8. Division by zero (`entry_price == 0`) yields `position_size_tokens = Decimal("0")`.
9. `AppConfig.enable_portfolio_aggregator` is `bool` with default `False`.
10. `AppConfig.portfolio_aggregation_interval_sec` is `Decimal` with default `Decimal("30")`.
11. `Orchestrator._portfolio_aggregation_loop()` exists as an async method with sleep-first pattern.
12. The portfolio task is registered only when `enable_portfolio_aggregator=True`.
13. A failed `compute_snapshot()` does not terminate the aggregation loop.
14. `PortfolioAggregator` has zero imports from prompt, context, evaluation, or ingestion modules.
15. `PortfolioAggregator` performs zero DB writes.
16. Full regression remains green with coverage >= 80%.

---

### WI-24: Position Lifecycle Report

**Objective**
Introduce `PositionLifecycleReporter`, a read-only reporting component that produces structured performance summaries over settled (closed) positions. The reporter reads all `CLOSED` positions with non-null `realized_pnl` from `PositionRepository`, computes aggregate statistics — total realized PnL, win/loss counts, average hold duration, best and worst single-position outcomes — and returns a typed `LifecycleReport`. This provides the operator with quantifiable strategy performance metrics without requiring manual database queries.

The reporter is stateless and on-demand: it computes a fresh report each time `generate_report()` is called, reading directly from the current database state. It does not maintain caches, rolling windows, or historical snapshots.

**Scope Boundaries**

In scope:
- New `PositionLifecycleReporter` class in `src/agents/execution/lifecycle_reporter.py`
- New `LifecycleReport` Pydantic model in `src/schemas/risk.py` — frozen, Decimal-validated
- `generate_report() -> LifecycleReport` as the sole public async entry point
- New `PositionRepository.get_settled_positions() -> list[Position]` — reads `CLOSED` positions where `realized_pnl IS NOT NULL`
- Aggregate statistics: `total_realized_pnl`, `winning_count`, `losing_count`, `breakeven_count`, `avg_hold_duration_hours`, `best_pnl`, `worst_pnl`, `total_settled_count`
- Win/loss classification: `realized_pnl > 0` is a win, `realized_pnl < 0` is a loss, `realized_pnl == 0` is breakeven
- Hold duration: `closed_at_utc - routed_at_utc` per position, averaged across all settled positions
- Division-by-zero guard: zero settled positions returns a zero-valued `LifecycleReport`
- `dry_run` flag included in report for audit context (component is read-only regardless)
- structlog audit events: `lifecycle.report_generated`, `lifecycle.report_empty`
- Orchestrator wiring: constructed in `__init__()`, available for on-demand invocation (not a periodic task)

Out of scope:
- New database tables, migrations, or DB writes — this component is strictly read-only
- Time-windowed or rolling-window reports (full history only)
- Per-market or per-category breakdowns
- Tax lot accounting, FIFO/LIFO, or fee-adjusted PnL
- Historical report persistence or comparison across report generations
- Modifications to `PnLCalculator`, `ExitStrategyEngine`, `ExitOrderRouter`, or `ExecutionRouter` internals
- Sharpe ratio, max drawdown, or advanced risk-adjusted return metrics (future phase)

**Components Delivered**

| Component | Location |
|---|---|
| `PositionLifecycleReporter` | `src/agents/execution/lifecycle_reporter.py` |
| `LifecycleReport` model | `src/schemas/risk.py` |
| `PositionRepository.get_settled_positions()` | `src/db/repositories/position_repository.py` |

**Key Invariants Enforced**

1. `PositionLifecycleReporter` is read-only. It performs zero DB writes. No repository write methods are called.
2. All financial aggregation (`total_realized_pnl`, `best_pnl`, `worst_pnl`) is `Decimal`-only. Float is rejected at Pydantic boundary.
3. `get_settled_positions()` is an additive repository method — it does not modify existing `insert_position()`, `update_status()`, `get_open_positions()`, or `record_settlement()` methods.
4. Win/loss classification is deterministic: strictly `> 0`, `< 0`, `== 0`. No configurable threshold.
5. Hold duration is computed from existing `routed_at_utc` and `closed_at_utc` fields — no new timestamp columns.
6. Zero settled positions produces a valid zero-valued `LifecycleReport`, not an error.
7. `PositionLifecycleReporter` has zero imports from prompt, context, evaluation, or ingestion modules.

**Acceptance Criteria**

1. `PositionLifecycleReporter` exists in `src/agents/execution/lifecycle_reporter.py` with `generate_report() -> LifecycleReport` as the sole public async entry point.
2. `LifecycleReport` Pydantic model exists in `src/schemas/risk.py`, is frozen, Decimal-validated, with fields: `report_at_utc`, `total_settled_count`, `winning_count`, `losing_count`, `breakeven_count`, `total_realized_pnl`, `avg_hold_duration_hours`, `best_pnl`, `worst_pnl`, `dry_run`.
3. `LifecycleReport` rejects `float` in financial fields at Pydantic boundary.
4. `PositionRepository.get_settled_positions()` returns `CLOSED` positions where `realized_pnl IS NOT NULL`.
5. `total_realized_pnl = sum(realized_pnl)` across all settled positions using Decimal arithmetic.
6. `winning_count`, `losing_count`, `breakeven_count` sum to `total_settled_count`.
7. `avg_hold_duration_hours` is computed from `(closed_at_utc - routed_at_utc)` averaged across settled positions.
8. `best_pnl` is `max(realized_pnl)` and `worst_pnl` is `min(realized_pnl)` across settled positions.
9. Zero settled positions returns `LifecycleReport` with all counts `0` and all financial fields `Decimal("0")`.
10. `PositionLifecycleReporter` is constructed in `Orchestrator.__init__()`.
11. `PositionLifecycleReporter` has zero imports from prompt, context, evaluation, or ingestion modules.
12. `PositionLifecycleReporter` performs zero DB writes.
13. `get_settled_positions()` is additive — existing repository methods are unmodified.
14. Full regression remains green with coverage >= 80%.

---

### WI-25: Alert Engine

**Objective**
Introduce `AlertEngine`, a rule-based monitoring component that evaluates `PortfolioSnapshot` (WI-23) and `LifecycleReport` (WI-24) against configurable risk thresholds and emits typed `AlertEvent` records when limits are breached. The engine provides automated risk observability so the operator is notified of portfolio degradation without manual log inspection.

The alert engine is strictly observational. It reads analytics snapshots, evaluates deterministic threshold rules, and emits structured log events. It does not mutate position state, halt execution, enforce circuit breakers, or modify any upstream component. Alert actions are deferred to the operator or to a future circuit-breaker phase.

**Scope Boundaries**

In scope:
- New `AlertEngine` class in `src/agents/execution/alert_engine.py`
- New `AlertEvent` Pydantic model in `src/schemas/risk.py` — frozen, with severity and threshold metadata
- New `AlertSeverity` enum (`INFO | WARNING | CRITICAL`) in `src/schemas/risk.py`
- `evaluate(snapshot: PortfolioSnapshot, report: LifecycleReport) -> list[AlertEvent]` as the sole public method
- Configurable threshold rules:
  - **Drawdown alert:** `total_unrealized_pnl` below configurable `alert_drawdown_usdc` threshold
  - **Stale-price alert:** `positions_with_stale_price / position_count` exceeds configurable `alert_stale_price_pct` threshold
  - **Position-count alert:** `position_count` exceeds configurable `alert_max_open_positions` ceiling
  - **Losing-streak alert:** `losing_count / total_settled_count` exceeds configurable `alert_loss_rate_pct` threshold
- New `AppConfig` fields for alert thresholds (all with sensible defaults)
- Integration into `_portfolio_aggregation_loop()`: after `compute_snapshot()`, optionally evaluate alerts when both snapshot and report are available
- structlog audit events: `alert.triggered` (per alert), `alert.evaluation_complete` (summary)
- `dry_run` flag included in `AlertEvent` for audit context

Out of scope:
- Circuit-breaker logic — alerts do not pause, halt, or modify execution behavior
- External notification channels (Slack, email, PagerDuty) — alerts are structlog events only
- Alert persistence to database — events are emitted via structured logging, not written to tables
- Alert deduplication, cooldown, or suppression logic
- Modifications to `PortfolioAggregator`, `PositionLifecycleReporter`, or any upstream component internals
- Dynamic threshold adjustment at runtime
- Historical alert querying or dashboarding

**Components Delivered**

| Component | Location |
|---|---|
| `AlertEngine` | `src/agents/execution/alert_engine.py` |
| `AlertEvent` model | `src/schemas/risk.py` |
| `AlertSeverity` enum | `src/schemas/risk.py` |
| Config: `alert_drawdown_usdc` | `src/core/config.py` |
| Config: `alert_stale_price_pct` | `src/core/config.py` |
| Config: `alert_max_open_positions` | `src/core/config.py` |
| Config: `alert_loss_rate_pct` | `src/core/config.py` |

**Key Invariants Enforced**

1. `AlertEngine` is read-only and observational. It performs zero DB writes and zero state mutations. It does not halt, pause, or modify execution behavior.
2. Alert rules are deterministic Decimal comparisons — no LLM reasoning, no probabilistic thresholds.
3. All financial comparisons (`drawdown_usdc`, `unrealized_pnl`) use `Decimal`-only arithmetic. Float is rejected at Pydantic boundary for `AlertEvent` financial fields.
4. `AlertEngine` receives typed `PortfolioSnapshot` and `LifecycleReport` only — never raw database rows, LLM outputs, or `MarketContext`.
5. A failed alert evaluation does not block the portfolio aggregation loop. Exceptions are caught and logged.
6. `AlertEngine` has zero imports from prompt, context, evaluation, or ingestion modules.
7. Alert thresholds are read from `AppConfig` at construction time. They are not dynamically adjustable at runtime.
8. When `PortfolioSnapshot` reports `position_count == 0`, ratio-based alerts (stale-price percentage) are skipped to avoid division-by-zero.

**Acceptance Criteria**

1. `AlertEngine` exists in `src/agents/execution/alert_engine.py` with `evaluate(snapshot, report) -> list[AlertEvent]` as the sole public method.
2. `AlertEvent` Pydantic model exists in `src/schemas/risk.py`, is frozen, with fields: `alert_at_utc`, `severity` (`AlertSeverity`), `rule_name`, `message`, `threshold_value`, `actual_value`, `dry_run`.
3. `AlertSeverity` enum has values `INFO`, `WARNING`, `CRITICAL`.
4. Drawdown alert triggers when `snapshot.total_unrealized_pnl < -config.alert_drawdown_usdc`.
5. Stale-price alert triggers when `snapshot.positions_with_stale_price / snapshot.position_count > config.alert_stale_price_pct` (skipped when `position_count == 0`).
6. Position-count alert triggers when `snapshot.position_count > config.alert_max_open_positions`.
7. Losing-streak alert triggers when `report.losing_count / report.total_settled_count > config.alert_loss_rate_pct` (skipped when `total_settled_count == 0`).
8. `AppConfig` gains four alert threshold fields with sensible defaults: `alert_drawdown_usdc: Decimal` (default `Decimal("100")`), `alert_stale_price_pct: Decimal` (default `Decimal("0.50")`), `alert_max_open_positions: int` (default `20`), `alert_loss_rate_pct: Decimal` (default `Decimal("0.60")`).
9. `AlertEngine` is constructed in `Orchestrator.__init__()`.
10. Alert evaluation is invoked within `_portfolio_aggregation_loop()` after `compute_snapshot()` and `generate_report()`.
11. `alert.triggered` structlog event is emitted at the alert's severity level for each triggered alert.
12. `alert.evaluation_complete` structlog event is emitted at `INFO` level with `total_alerts` count after each evaluation.
13. Alert evaluation failure does not terminate the portfolio aggregation loop.
14. `AlertEngine` performs zero DB writes and zero state mutations.
15. `AlertEngine` has zero imports from prompt, context, evaluation, or ingestion modules.
16. Full regression remains green with coverage >= 80%.

## 4. Architecture Impact

### 4.1 Layer 4 Extension

Phase 8 extends Layer 4 (Execution) with a read-only analytics sublayer that operates downstream of the existing entry and exit paths:

```text
Layer 1: Ingestion
  CLOBWebSocketClient + GammaRESTClient + MarketDiscoveryEngine

Layer 2: Context
  DataAggregator + PromptFactory

Layer 3: Evaluation
  ClaudeClient + LLMEvaluationResponse Gatekeeper

Layer 4: Execution
  ┌─ Entry Path ─────────────────────────────────────────────────────────┐
  │ BankrollSyncProvider -> ExecutionRouter -> PositionTracker           │
  │ -> TransactionSigner -> NonceManager -> GasEstimator                │
  │ -> OrderBroadcaster                                                 │
  └─────────────────────────────────────────────────────────────────────┘
  ┌─ Exit Path ──────────────────────────────────────────────────────────┐
  │ ExitStrategyEngine -> ExitOrderRouter -> PnLCalculator              │
  │ -> TransactionSigner -> OrderBroadcaster                            │
  └─────────────────────────────────────────────────────────────────────┘
  ┌─ Analytics Path (NEW) ───────────────────────────────────────────────┐
  │ PortfolioAggregator -> PositionLifecycleReporter -> AlertEngine     │
  └─────────────────────────────────────────────────────────────────────┘
```

Queue topology unchanged: `market_queue -> prompt_queue -> execution_queue`. The analytics path runs in the optional `PortfolioAggregatorTask` and does not consume from or produce to any queue.

### 4.2 Orchestrator Task Topology (After Phase 8)

```text
Orchestrator.start():
  Task 1: IngestionTask           — CLOBWebSocketClient.run()
  Task 2: ContextTask             — DataAggregator.start()
  Task 3: EvaluationTask          — ClaudeClient.start()
  Task 4: ExecutionTask           — _execution_consumer_loop()
  Task 5: DiscoveryTask           — _discovery_loop()
  Task 6: ExitScanTask            — _exit_scan_loop()                  [WI-22]
  Task 7: PortfolioAggregatorTask — _portfolio_aggregation_loop()      [WI-23, conditional]
```

Task 7 is created only when `config.enable_portfolio_aggregator=True`. When disabled (default), the task list remains 6 entries — identical to post-Phase 7 state.

### 4.3 Portfolio Aggregation Loop Flow (After Phase 8)

```text
_portfolio_aggregation_loop:
  1. Sleep for config.portfolio_aggregation_interval_sec               [WI-23]
  2. PortfolioAggregator.compute_snapshot()                            [WI-23]
     -> PortfolioSnapshot
  3. PositionLifecycleReporter.generate_report()                       [WI-24]
     -> LifecycleReport
  4. AlertEngine.evaluate(snapshot, report)                            [WI-25]
     -> list[AlertEvent]
  5. Log summary of snapshot, report, and alerts
  6. Repeat
```

Steps 3-4 are fail-open: if `generate_report()` or `evaluate()` raises, the loop catches the exception, logs, and continues. A failed lifecycle report does not block alert evaluation from being skipped — both are independent try/except blocks.

### 4.4 Preserved Boundaries

Phase 8 does not alter:
- **Gatekeeper authority** — `LLMEvaluationResponse` remains the sole terminal validation boundary before execution-aware routing.
- **Decimal financial integrity** — all analytics arithmetic is Decimal-native with float rejection at Pydantic boundary.
- **Quarter-Kelly and exposure policy** — `kelly_fraction=0.25` and `min(kelly_size, 0.03 * bankroll)` are unchanged. Analytics components read position metadata; they do not influence sizing.
- **Repository pattern** — `PositionRepository` remains the sole access path for the `positions` table. `get_settled_positions()` is additive. No raw SQL or direct session manipulation.
- **Async pipeline** — Phase 8 adds one optional async task. No blocking execution paths introduced.
- **Entry-path routing** — `ExecutionRouter` internals are unmodified.
- **Exit-path routing** — `ExitOrderRouter`, `ExitStrategyEngine`, and `PnLCalculator` internals are unmodified.
- **Position state transitions** — `OPEN -> CLOSED` lifecycle is unchanged. Analytics components never mutate position status.

### 4.5 Database Schema Extension

Phase 8 adds zero new tables and zero new migrations. All three components are read-only consumers of existing data:

| Component | DB Access | Method |
|---|---|---|
| `PortfolioAggregator` | Read `OPEN` positions | `PositionRepository.get_open_positions()` (existing) |
| `PositionLifecycleReporter` | Read `CLOSED` settled positions | `PositionRepository.get_settled_positions()` (new, additive) |
| `AlertEngine` | None — reads typed analytics models only | N/A |

## 5. Risk and Safety Notes

### 5.1 dry_run Behavior

`dry_run=True` remains a hard stop for all Layer 4 side effects. Phase 8 components are read-only and require no new dry_run gates:

| Component | dry_run=True behavior |
|---|---|
| `PortfolioAggregator` | Computes full `PortfolioSnapshot`. Reads open positions (read-path permitted). Zero DB writes (component is inherently read-only). `dry_run` flag included in snapshot for audit. |
| `PositionLifecycleReporter` | Computes full `LifecycleReport`. Reads settled positions (read-path permitted). Zero DB writes. `dry_run` flag included in report for audit. |
| `AlertEngine` | Evaluates all threshold rules. Emits `alert.triggered` structlog events. Zero DB writes, zero state mutations. `dry_run` flag included in `AlertEvent` for audit. |

No Phase 8 component introduces a write path, so no new dry_run gate is needed. The flag is propagated for audit consistency.

### 5.2 Failure Semantics

All Phase 8 components use **fail-open** semantics — failures never block the analytics loop, the exit scan loop, or the execution consumer:

| Component | Failure behavior | Rationale |
|---|---|---|
| `PortfolioAggregator` | Per-position price fetch failure falls back to `entry_price`. Full `compute_snapshot()` failure is caught in loop body. | A degraded snapshot (stale prices) is more useful than no snapshot. |
| `PositionLifecycleReporter` | `generate_report()` exception caught in loop body. | Lifecycle reporting is informational. A skipped report is retried next cycle. |
| `AlertEngine` | `evaluate()` exception caught in loop body. | Alerts are observational. A missed evaluation is retried next cycle. |
| Price fetch for individual position | Falls back to `entry_price`; `positions_with_stale_price` incremented | Conservative: PnL=0 for stale positions, not an error. |

### 5.3 Read-Only Safety

Phase 8 is the first phase where all delivered components are strictly read-only:
- Zero new Alembic migrations.
- Zero new database tables or columns.
- Zero calls to repository write methods (`insert_position`, `update_status`, `record_settlement`).
- The sole additive repository method (`get_settled_positions`) is a SELECT-only query.
- No component calls `TransactionSigner`, `OrderBroadcaster`, or `PolymarketClient` for write operations.

This means Phase 8 cannot introduce data corruption, double-settlement, or position state inconsistencies. The worst-case failure mode is a missed analytics snapshot or a skipped alert — both of which are retried on the next cycle.

## 6. Metrics

| Metric | Target |
|---|---|
| Coverage | >= 80% (maintain existing 93%) |
| Regression gate | `pytest --asyncio-mode=auto tests/ -q` green |
| WI-23 tests | Unit + integration for snapshot computation, price fallback, config gating, loop lifecycle |
| WI-24 tests | Unit + integration for report aggregation, win/loss classification, duration calc, zero-positions edge case |
| WI-25 tests | Unit + integration for each alert rule, threshold config, division-by-zero guards, loop integration |

## 7. Strict Constraints

The following constraints are mandatory and non-negotiable for all Phase 8 work:

1. **Gatekeeper remains immutable:**
   `LLMEvaluationResponse` remains the sole terminal validation boundary before execution-aware routing. No Phase 8 component bypasses, replaces, or weakens that authority. Analytics components operate strictly downstream and are read-only observers.

2. **Decimal financial integrity remains immutable:**
   All portfolio aggregation, lifecycle reporting, and alert threshold comparisons remain Decimal-native. Float is rejected at Pydantic boundary. No float intermediary in any arithmetic step.

3. **Quarter-Kelly and exposure policy remain immutable:**
   Phase 8 does not alter `kelly_fraction=0.25` or the system-wide `min(kelly_size, 0.03 * bankroll)` exposure policy. Analytics components read position metadata; they do not influence sizing or routing decisions.

4. **`dry_run=True` remains a hard execution stop:**
   Dry run blocks all order signing, CLOB broadcast, and state-mutating DB writes. Phase 8 components are inherently read-only, so no new dry_run gate is required. The flag is propagated in output models for audit consistency.

5. **Repository pattern remains the sole DB access path:**
   `PositionRepository` is the only component that touches the `positions` table. `get_settled_positions()` is additive. No raw SQL, no direct session manipulation.

6. **Async pipeline behavior remains immutable:**
   Phase 8 preserves the existing non-blocking, queue-driven four-layer architecture. The optional `PortfolioAggregatorTask` is an independent async task — it does not block, replace, or interfere with the execution consumer loop or the exit scan loop.

7. **Module isolation remains enforced:**
   `PortfolioAggregator`, `PositionLifecycleReporter`, and `AlertEngine` have zero imports from prompt, context, evaluation, or ingestion modules. They receive and produce only typed contracts from `src/schemas/`.

8. **Read-only analytics boundary:**
   All Phase 8 components are strictly read-only. They perform zero DB writes, zero state mutations, zero order submissions, and zero position status transitions. They observe, compute, and log — nothing more.

## 8. Success Criteria For Phase 8

Phase 8 is complete when all of the following are true:

1. A typed `PortfolioSnapshot` is computed on a configurable interval, aggregating total notional, unrealized PnL, position count, and locked collateral across all open positions using Decimal-only arithmetic.
2. Price-fetch failures for individual positions fall back to `entry_price` with `positions_with_stale_price` tracked, producing a degraded but valid snapshot.
3. A typed `LifecycleReport` is computed on demand from settled positions, providing total realized PnL, win/loss counts, average hold duration, and best/worst outcomes.
4. `AlertEngine` evaluates portfolio and lifecycle metrics against configurable thresholds and emits typed `AlertEvent` records via structlog when limits are breached.
5. All three components are strictly read-only: zero DB writes, zero state mutations, zero order submissions.
6. The portfolio aggregation task is optional and config-gated (`enable_portfolio_aggregator`). When disabled, zero runtime overhead.
7. All three components use fail-open semantics — failures do not block the analytics loop, the exit scan loop, or the execution consumer.
8. Full regression remains green and project coverage stays at or above 80%.
9. All prior architectural invariants remain in force: Decimal safety, repository isolation, Gatekeeper authority, no hardcoded market identifiers, `dry_run` execution blocking, and async-only pipeline.

## 9. Next Phase

Phase 9 should address execution hardening and advanced risk management. Potential scope includes:

- **Circuit-breaker logic** — extending `AlertEngine` with the ability to pause new order routing when critical thresholds are breached, requiring explicit operator acknowledgment to resume.
- **Fee-aware PnL** — extending `PnLCalculator` to account for CLOB trading fees and Polygon gas costs in realized PnL computation, improving accuracy of lifecycle reports.
- **Advanced risk metrics** — Sharpe ratio, maximum drawdown, and rolling-window analytics for strategy evaluation beyond simple win/loss counts.
- **External notification channels** — Slack, email, or webhook integration for `AlertEvent` delivery beyond structlog.
- **Portfolio-level exposure limits** — enforcing hard cross-position exposure caps (not just per-order 3%) with automatic rejection of new orders that would breach portfolio limits.

Detailed scope, work items, and acceptance criteria to be finalized in the Phase 9 PRD.
