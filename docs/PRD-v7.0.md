# PRD v7.0 - Poly-Oracle-Agent Phase 7

Source inputs: `docs/PRD-v6.0.md`, `STATE.md`, `docs/archive/ARCHIVE_PHASE_6.md`, `docs/business_logic/business_logic_wi19.md`, `src/agents/execution/exit_strategy_engine.py`, `src/agents/execution/execution_router.py`, `src/orchestrator.py`, `AGENTS.md`.

## 1. Executive Summary

Phase 6 established the position lifecycle: every routed BUY decision is persisted as a `PositionRecord`, and open positions are periodically evaluated against rule-based exit criteria that produce a typed `ExitResult`. But two critical gaps remain. First, exit evaluation currently runs inline within the execution consumer loop (Mode A), meaning an exit scan blocks the next dequeue cycle and cannot run independently of new order flow. Second, when `ExitStrategyEngine` determines `should_exit=True`, the system transitions the position status to `CLOSED` in the database — but no exit order is submitted to the CLOB, and no realized profit or loss is computed. The position is marked closed without actually being unwound or accounted for.

Phase 7 closes these gaps with three work items executed in strict dependency order:

1. **WI-22 — Periodic Exit Scan** promotes `ExitStrategyEngine.scan_open_positions()` from inline Mode A (called after each new execution) to a standalone Mode B async task in the Orchestrator with a configurable scan interval. No new exit logic is introduced — this is a wiring-only change that decouples exit evaluation from the execution consumer loop.

2. **WI-20 — Exit Order Router** introduces `ExitOrderRouter`, the component that consumes `ExitResult(should_exit=True)` and produces a signed SELL order payload for the CLOB. This mirrors the WI-16 `ExecutionRouter` pattern for the exit path: fetch fresh order-book data, compute a SELL-side order using the position's `order_size_usdc` and `current_best_bid`, enforce slippage guards, and delegate signing to `TransactionSigner`. The router does not broadcast — it produces a `SignedOrder` that the existing `OrderBroadcaster` can transmit.

3. **WI-21 — Realized PnL & Settlement** introduces `PnLCalculator`, a read-only accounting component that computes realized profit/loss when a position transitions from `OPEN` to `CLOSED`. PnL is computed as `(exit_price - entry_price) * position_size` using Decimal-only arithmetic, persisted to a new `realized_pnl` column on the `positions` table, and exposed through `PositionRepository`. This is a pure accounting layer — it reads position metadata, computes a scalar, and writes a single Decimal field. It does not influence routing, exit decisions, or any upstream component.

Phase 7 preserves the four-layer async architecture and the terminal authority of `LLMEvaluationResponse`. It extends Layer 4 (Execution) with downstream exit-order routing and settlement accounting without weakening the existing safety model: Decimal-only money paths, quarter-Kelly sizing, 3% exposure policy, repository isolation, and `dry_run=True` as a hard stop for all order submission and state mutation.

## 2. Core Pillars

### 2.1 Decoupled Exit Scanning

Exit evaluation must not block the execution consumer loop. Phase 7 promotes the exit scan to an independent async task that runs on its own cadence, decoupling exit lifecycle management from new-order ingestion throughput.

### 2.2 Exit Order Routing

Exit decisions must be actionable. Phase 7 introduces a SELL-side order router that converts typed exit decisions into signed CLOB order payloads, completing the lifecycle arc from evaluation through execution for the exit path.

### 2.3 Realized PnL Accounting

Closed positions must have auditable financial outcomes. Phase 7 introduces a Decimal-only accounting layer that computes and persists realized PnL at position closure, providing the foundational data for portfolio performance analysis and risk reporting.

## 3. Work Items

### WI-22: Periodic Exit Scan

**Objective**
Promote `ExitStrategyEngine.scan_open_positions()` from inline Mode A (called synchronously within the execution consumer loop after each new position recording) to a standalone Mode B async task in the Orchestrator with a configurable scan interval. No new exit evaluation logic is introduced — this is a wiring-only change.

**Scope Boundaries**

In scope:
- New `_exit_scan_loop()` async method in `Orchestrator`, registered as a named `asyncio.Task` alongside existing pipeline tasks
- New `AppConfig` field: `exit_scan_interval_seconds: Decimal` (default: `Decimal("60")`)
- Removal of the inline `scan_open_positions()` call from `_execution_consumer_loop()` (steps 5 in the current flow)
- Fire-and-forget error handling: a failed scan iteration logs and continues, never kills the task
- `dry_run` behavior inherited from `ExitStrategyEngine` internals — no new gate needed
- Graceful shutdown: the exit scan task is cancelled alongside other tasks in `Orchestrator.shutdown()`

Out of scope:
- New exit evaluation logic, criteria, or thresholds
- Modifications to `ExitStrategyEngine` internals, `ExitResult`, `ExitSignal`, or `ExitReason`
- Modifications to `PositionTracker`, `PositionRepository`, or `ExecutionRouter`
- Queue topology changes — no new queue is introduced
- Exit order routing or PnL accounting (deferred to WI-20 and WI-21)

**Components Delivered**

| Component | Location |
|---|---|
| `Orchestrator._exit_scan_loop()` | `src/orchestrator.py` |
| Config: `exit_scan_interval_seconds` | `src/core/config.py` |

**Key Invariants Enforced**

1. `ExitStrategyEngine` internals are unmodified — only the call site changes from inline to periodic.
2. The execution consumer loop no longer calls `scan_open_positions()`, eliminating the inline scan latency from the dequeue cycle.
3. The exit scan loop is an independent async task that does not share state with the execution consumer beyond the injected `ExitStrategyEngine` instance.
4. A failed `scan_open_positions()` call within the loop is caught, logged, and retried on the next interval. The loop never terminates on a single failure.
5. `dry_run` enforcement is unchanged — `ExitStrategyEngine` handles its own write gates internally.
6. The exit scan task is included in `Orchestrator._tasks` and cancelled during `shutdown()`.

**Acceptance Criteria**

1. `Orchestrator._exit_scan_loop()` exists as an async method that calls `ExitStrategyEngine.scan_open_positions()` on a recurring interval.
2. `AppConfig.exit_scan_interval_seconds` is a `Decimal` field with default `Decimal("60")`.
3. The exit scan loop is registered as a named `asyncio.Task("ExitScanTask")` in `Orchestrator.start()`.
4. The inline `scan_open_positions()` call is removed from `_execution_consumer_loop()`.
5. A failed scan iteration emits a structured error log and does not terminate the loop.
6. The exit scan task is cancelled during `Orchestrator.shutdown()`.
7. `ExitStrategyEngine` constructor, methods, and internals are unmodified.
8. Full regression remains green with coverage >= 80%.

---

### WI-20: Exit Order Router

**Objective**
Introduce `ExitOrderRouter`, the component that consumes an `ExitResult(should_exit=True)` with a `CLOSED` position and produces a signed SELL-side order payload for submission to the Polymarket CLOB. This mirrors the WI-16 `ExecutionRouter` pattern, adapted for the exit path.

**Scope Boundaries**

In scope:
- New `ExitOrderRouter` class in `src/agents/execution/exit_order_router.py`
- `ExitOrderResult` Pydantic model in `src/schemas/execution.py` with frozen, Decimal-validated fields
- `ExitOrderAction` enum: `SELL_ROUTED | DRY_RUN | FAILED | SKIP`
- Fresh order-book fetch via `PolymarketClient.fetch_order_book()` to determine realistic exit price (`best_bid`)
- SELL-side `OrderData` construction: `side=OrderSide.SELL`, `maker_amount` derived from position's token quantity, `taker_amount` from best-bid pricing
- Exit-specific slippage guard: reject when `best_bid < exit_min_bid_tolerance` (position would be sold too cheaply)
- Signing delegation to `TransactionSigner.sign_order()` when `dry_run=False`
- `dry_run=True` returns a typed `DRY_RUN` result with full `OrderData` payload; no signing, no broadcast
- `ExitRoutingError` typed exception in `src/core/exceptions.py`
- Integration into `_exit_scan_loop()`: after `scan_open_positions()`, iterate results where `should_exit=True` and route each through `ExitOrderRouter`
- New `AppConfig` field: `exit_min_bid_tolerance: Decimal` (default: `Decimal("0.01")`)

Out of scope:
- Order broadcasting — `ExitOrderRouter` produces a `SignedOrder`; broadcasting is delegated to `OrderBroadcaster` (existing, unchanged)
- Realized PnL calculation (deferred to WI-21)
- Modifications to `ExecutionRouter` (entry-path router), `ExitStrategyEngine`, `PositionTracker`, or `PositionRepository` internals
- Kelly re-sizing for exits — exit size is derived from the position's existing `order_size_usdc`, not recalculated
- Partial exits or position scaling — exits are full-position only
- LLM-assisted exit-order reasoning

**Components Delivered**

| Component | Location |
|---|---|
| `ExitOrderRouter` | `src/agents/execution/exit_order_router.py` |
| `ExitOrderResult` model | `src/schemas/execution.py` |
| `ExitOrderAction` enum | `src/schemas/execution.py` |
| `ExitRoutingError` | `src/core/exceptions.py` |
| Config: `exit_min_bid_tolerance` | `src/core/config.py` |

**Key Invariants Enforced**

1. `ExitOrderRouter` operates strictly downstream of `ExitStrategyEngine`. It cannot bypass Gatekeeper authority or originate exit decisions.
2. Exit order sizing is derived from the position's recorded `order_size_usdc` — no Kelly recalculation occurs on the exit path.
3. All pricing, sizing, and slippage arithmetic is `Decimal`-only. Float is rejected at Pydantic boundary.
4. `dry_run=True` blocks signing and broadcast. The full `OrderData` payload is computed and returned for audit.
5. `signer=None` in dry-run mode is tolerated; in live mode it returns `FAILED(reason="signer_unavailable")`, mirroring WI-16 behavior.
6. Exit orders use `OrderSide.SELL` — never `OrderSide.BUY`. A BUY-side exit order is a logic error.
7. `ExitOrderRouter` has zero imports from prompt, context, evaluation, or ingestion modules.
8. Failure semantics are fail-open: a failed exit-order routing does not block the exit scan loop or the execution consumer. The position remains `CLOSED` (the status transition already occurred in `ExitStrategyEngine`).
9. Module isolation: `ExitOrderRouter` receives typed `ExitResult` and `PositionRecord` only — never raw LLM outputs or `MarketContext`.

**Acceptance Criteria**

1. `ExitOrderRouter` exists in `src/agents/execution/exit_order_router.py` with `route_exit(exit_result, position) -> ExitOrderResult` as the sole public async method.
2. `ExitOrderResult` Pydantic model is frozen, Decimal-validated, with fields: `position_id`, `condition_id`, `action` (`ExitOrderAction`), `reason`, `order_payload` (`OrderData | None`), `signed_order` (`SignedOrder | None`), `exit_price` (`Decimal | None`), `order_size_usdc` (`Decimal | None`), `routed_at_utc`.
3. `ExitOrderAction` enum has values `SELL_ROUTED`, `DRY_RUN`, `FAILED`, `SKIP`.
4. `route_exit()` fetches a fresh order-book snapshot via `PolymarketClient.fetch_order_book()`.
5. Exit slippage guard rejects when `best_bid < exit_min_bid_tolerance`.
6. `OrderData` is constructed with `side=OrderSide.SELL`.
7. `dry_run=True` returns `ExitOrderResult(action=DRY_RUN)` with full `OrderData`; no signer call.
8. `dry_run=False` delegates signing to `TransactionSigner.sign_order()` and returns `ExitOrderResult(action=SELL_ROUTED)`.
9. `signer=None` + `dry_run=False` returns `ExitOrderResult(action=FAILED, reason="signer_unavailable")`.
10. Order-book unavailable returns `ExitOrderResult(action=FAILED, reason="order_book_unavailable")`.
11. `ExitOrderRouter` is constructed in `Orchestrator.__init__()` and invoked within `_exit_scan_loop()` after `scan_open_positions()`.
12. Exit-order routing failure does not terminate the exit scan loop.
13. `ExitOrderRouter` has zero imports from prompt, context, evaluation, or ingestion modules.
14. `AppConfig` gains `exit_min_bid_tolerance: Decimal` (default `Decimal("0.01")`).
15. `ExitRoutingError` exception exists in `src/core/exceptions.py` with structured context fields.
16. Full regression remains green with coverage >= 80%.

---

### WI-21: Realized PnL & Settlement

**Objective**
Introduce `PnLCalculator`, a read-only accounting component that computes realized profit/loss when a position transitions from `OPEN` to `CLOSED`, and persists the result to the `positions` table. This provides the foundational accounting data for portfolio performance analysis.

**Scope Boundaries**

In scope:
- New `PnLCalculator` class in `src/agents/execution/pnl_calculator.py`
- `PnLRecord` Pydantic model in `src/schemas/execution.py` with frozen, Decimal-validated fields
- Realized PnL formula: `realized_pnl = (exit_price - entry_price) * position_size_tokens`, all Decimal
- Position size in tokens: `position_size_tokens = order_size_usdc / entry_price` (Decimal division)
- Alembic migration `0003_add_pnl_columns.py` adding `realized_pnl Numeric(38,18)`, `exit_price Numeric(38,18)`, and `closed_at_utc DateTime` columns to the `positions` table
- `PositionRepository.record_settlement(position_id, realized_pnl, exit_price, closed_at_utc)` — new repository method for writing settlement data
- `Position` ORM model extended with three new nullable columns
- `PositionRecord` schema extended with three new optional fields: `realized_pnl: Decimal | None`, `exit_price: Decimal | None`, `closed_at_utc: datetime | None`
- Integration: `PnLCalculator.settle(position_record, exit_price) -> PnLRecord` called after `ExitOrderRouter.route_exit()` produces a routed or dry-run result
- `dry_run=True` computes and returns `PnLRecord` via structlog; zero DB writes
- `PnLCalculationError` typed exception in `src/core/exceptions.py`

Out of scope:
- Mark-to-market or unrealized PnL computation (WI-19 `unrealized_edge` is sufficient for open positions)
- Portfolio-level PnL aggregation or reporting dashboards
- Tax lot accounting or FIFO/LIFO cost basis methods
- Modifications to `ExitStrategyEngine`, `ExitOrderRouter`, or `ExecutionRouter` internals
- Market resolution data or oracle settlement — exit price is taken from the exit order's fill price, not from market resolution
- Fee accounting (CLOB fees, gas costs) — these are out of scope for Phase 7
- Partial settlement — a position is settled in full or not at all

**Components Delivered**

| Component | Location |
|---|---|
| `PnLCalculator` | `src/agents/execution/pnl_calculator.py` |
| `PnLRecord` model | `src/schemas/execution.py` |
| `PnLCalculationError` | `src/core/exceptions.py` |
| `PositionRepository.record_settlement()` | `src/db/repositories/position_repository.py` |
| Alembic migration | `migrations/versions/0003_add_pnl_columns.py` |
| `Position` ORM extension | `src/db/models.py` (3 new nullable columns) |

**Key Invariants Enforced**

1. `PnLCalculator` is a pure accounting component. It reads `PositionRecord` metadata and an exit price, computes a scalar, and writes through `PositionRepository`. It does not influence routing, exit decisions, or any upstream component.
2. All PnL arithmetic is `Decimal`-only. Float is rejected at Pydantic boundary.
3. `realized_pnl` is the only new financial column. It uses `Numeric(38,18)` matching all existing financial columns.
4. `dry_run=True` computes the full `PnLRecord` and logs it; zero DB writes.
5. `PnLCalculator` has zero imports from prompt, context, evaluation, or ingestion modules.
6. `record_settlement()` is an additive repository method — it does not modify existing `insert_position()`, `update_status()`, or `get_open_positions()` methods.
7. Division-by-zero guard: if `entry_price == Decimal("0")`, `PnLCalculator` returns `PnLRecord` with `realized_pnl=Decimal("0")` and logs a warning.
8. Settlement is idempotent: calling `record_settlement()` on a position that already has `realized_pnl` set logs a warning and returns without overwriting.

**Acceptance Criteria**

1. `PnLCalculator` exists in `src/agents/execution/pnl_calculator.py` with `settle(position, exit_price) -> PnLRecord` as the sole public async method.
2. `PnLRecord` Pydantic model is frozen, Decimal-validated, with fields: `position_id`, `condition_id`, `entry_price`, `exit_price`, `order_size_usdc`, `position_size_tokens`, `realized_pnl`, `closed_at_utc`.
3. `realized_pnl = (exit_price - entry_price) * position_size_tokens` using Decimal arithmetic.
4. `position_size_tokens = order_size_usdc / entry_price` using Decimal division.
5. Division by zero (`entry_price == 0`) returns `PnLRecord(realized_pnl=Decimal("0"))` and logs a warning.
6. Alembic migration `0003_add_pnl_columns.py` adds `realized_pnl`, `exit_price`, and `closed_at_utc` as nullable columns to `positions`.
7. `Position` ORM model has three new nullable columns: `realized_pnl Numeric(38,18)`, `exit_price Numeric(38,18)`, `closed_at_utc DateTime`.
8. `PositionRepository.record_settlement()` writes `realized_pnl`, `exit_price`, and `closed_at_utc` to an existing position row.
9. Settlement is idempotent: re-settling a position with existing `realized_pnl` logs a warning and returns without overwriting.
10. `dry_run=True` computes full `PnLRecord`, logs via structlog, zero DB writes.
11. `PnLCalculator` is constructed in `Orchestrator.__init__()` and called after `ExitOrderRouter.route_exit()` in `_exit_scan_loop()`.
12. PnL calculation failure does not block the exit scan loop.
13. `PnLCalculator` has zero imports from prompt, context, evaluation, or ingestion modules.
14. `PnLCalculationError` exception exists in `src/core/exceptions.py` with structured context fields.
15. `float` values are rejected by `PnLRecord` field validators for all financial fields.
16. Full regression remains green with coverage >= 80%.

## 4. Architecture Impact

### 4.1 Layer 4 Extension

Phase 7 extends Layer 4 (Execution) with exit-order routing, PnL settlement, and a decoupled exit scan loop:

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
  ┌─ Exit Path (NEW) ───────────────────────────────────────────────────┐
  │ ExitStrategyEngine -> ExitOrderRouter -> PnLCalculator              │
  │ -> TransactionSigner -> OrderBroadcaster                            │
  └─────────────────────────────────────────────────────────────────────┘
```

Queue topology unchanged: `market_queue -> prompt_queue -> execution_queue`. The exit path runs in a separate async task (`ExitScanTask`) and does not consume from or produce to any queue.

### 4.2 Orchestrator Task Topology (After Phase 7)

```text
Orchestrator.start():
  Task 1: IngestionTask      — CLOBWebSocketClient.run()
  Task 2: ContextTask        — DataAggregator.start()
  Task 3: EvaluationTask     — ClaudeClient.start()
  Task 4: ExecutionTask      — _execution_consumer_loop()
  Task 5: DiscoveryTask      — _discovery_loop()
  Task 6: ExitScanTask (NEW) — _exit_scan_loop()               [WI-22]
```

### 4.3 Exit Scan Loop Flow (After Phase 7)

```text
_exit_scan_loop:
  1. Sleep for config.exit_scan_interval_seconds                [WI-22]
  2. ExitStrategyEngine.scan_open_positions() -> list[ExitResult] [WI-19]
  3. For each ExitResult where should_exit=True:
     a. ExitOrderRouter.route_exit(exit_result, position)       [WI-20]
        -> ExitOrderResult (SELL_ROUTED | DRY_RUN | FAILED)
     b. If SELL_ROUTED or DRY_RUN:
        PnLCalculator.settle(position, exit_price)              [WI-21]
        -> PnLRecord (persisted or logged)
     c. If SELL_ROUTED and not dry_run:
        OrderBroadcaster.broadcast(signed_order)                [existing]
  4. Log summary of scan results
  5. Repeat
```

### 4.4 Execution Consumer Loop Flow (After Phase 7)

```text
_execution_consumer_loop:
  1. Dequeue item from execution_queue
  2. Extract LLMEvaluationResponse + MarketContext from item
  3. ExecutionRouter.route(response, market_context) -> ExecutionResult   [WI-16]
  4. PositionTracker.record_execution(result, condition_id, token_id)     [WI-17]
  5. (exit scan removed — now in ExitScanTask)                           [WI-22]
  6. If EXECUTED and not dry_run, proceed to broadcast                   [existing]
```

### 4.5 Preserved Boundaries

Phase 7 does not alter:
- **Gatekeeper authority** — `LLMEvaluationResponse` remains the sole terminal validation boundary before execution-aware routing.
- **Decimal financial integrity** — all exit-order, PnL, and settlement fields are Decimal-native with float rejection at Pydantic boundary.
- **Quarter-Kelly and exposure policy** — `kelly_fraction=0.25` and `min(kelly_size, 0.03 * bankroll)` are unchanged. Exit sizing is derived from position metadata, not recalculated.
- **Repository pattern** — `PositionRepository` remains the sole mutation path for the `positions` table. `record_settlement()` is additive.
- **Async pipeline** — Phase 7 adds one new async task. No blocking execution paths introduced.
- **Entry-path routing** — `ExecutionRouter` internals are unmodified.

### 4.6 Database Schema Extension

Phase 7 adds zero new tables and one migration modifying the existing `positions` table:

| Migration | Change | Financial Precision |
|---|---|---|
| `0003_add_pnl_columns.py` | 3 nullable columns added to `positions` | `Numeric(38,18)` for `realized_pnl` and `exit_price` |

Existing tables (`market_snapshots`, `agent_decision_logs`, `execution_txs`) and existing `positions` columns are untouched.

## 5. Risk and Safety Notes

### 5.1 dry_run Behavior

`dry_run=True` remains a hard stop for all Layer 4 side effects:

| Component | dry_run=True behavior |
|---|---|
| `ExitStrategyEngine` | Computes full `ExitResult`. Zero state mutations. Read-path DB access for `scan_open_positions()` is permitted. (Unchanged from Phase 6.) |
| `ExitOrderRouter` | Computes full `ExitOrderResult` with `OrderData` payload. Zero signing, zero broadcast. Returns `DRY_RUN` action. |
| `PnLCalculator` | Computes full `PnLRecord`. Logs via structlog. Zero DB writes for settlement data. |
| `Orchestrator._exit_scan_loop()` | Runs normally — `dry_run` enforcement is delegated to each component's internal guard. |

### 5.2 Failure Semantics

| Component | Failure behavior | Rationale |
|---|---|---|
| `_exit_scan_loop()` | Exception caught per iteration; loop continues on next interval. | Exit scanning is a lifecycle optimization. A single failed scan is retried next interval. |
| `ExitOrderRouter` | Exception caught per exit result; remaining exits still processed. Position remains `CLOSED`. | The status transition already occurred. A failed routing attempt means the order is not submitted, but the position is correctly marked closed. |
| `PnLCalculator` | Exception caught per settlement; remaining settlements still processed. Position remains `CLOSED` with `realized_pnl=NULL`. | PnL is an accounting concern, not a safety gate. Missing PnL can be backfilled. |
| `OrderBroadcaster` (exit orders) | Existing fail-open behavior. | Broadcast failure is already handled by the existing broadcaster infrastructure. |

### 5.3 Position State Mutation Rules (Updated)

- `PositionTracker` writes `OPEN` and `FAILED` only. It never writes `CLOSED`. (Unchanged.)
- `ExitStrategyEngine` transitions `OPEN -> CLOSED` only. (Unchanged.)
- `ExitOrderRouter` does not mutate position status. It reads `CLOSED` positions and produces order payloads.
- `PnLCalculator` writes settlement fields (`realized_pnl`, `exit_price`, `closed_at_utc`) to `CLOSED` positions only. It does not change `status`.
- `CLOSED -> OPEN` is not a valid transition. Once closed, a position is terminal.
- All state mutations go through `PositionRepository` methods.

### 5.4 Exit Order Safety

- Exit orders use `OrderSide.SELL` exclusively. A BUY-side exit order is a logic error.
- Exit sizing is derived from the position's recorded `order_size_usdc`, not recalculated via Kelly. This prevents the exit path from inadvertently increasing exposure.
- Exit slippage guard (`exit_min_bid_tolerance`) prevents selling at degenerate prices.
- No partial exits — a position is unwound in full or not at all, eliminating partial-fill accounting complexity.

## 6. Metrics

| Metric | Target |
|---|---|
| Coverage | >= 80% (maintain existing 92%) |
| Regression gate | `pytest --asyncio-mode=auto tests/ -q` green |
| WI-22 tests | Unit + integration for loop lifecycle, config, inline removal |
| WI-20 tests | Unit + integration for SELL routing, slippage, dry-run, signing |
| WI-21 tests | Unit + integration for PnL formula, settlement, migration, idempotency |

## 7. Strict Constraints

The following constraints are mandatory and non-negotiable for all Phase 7 work:

1. **Gatekeeper remains immutable:**
   `LLMEvaluationResponse` remains the sole terminal validation boundary before execution-aware routing. No Phase 7 component bypasses, replaces, or weakens that authority. Exit-order routing and PnL settlement operate strictly downstream.

2. **Decimal financial integrity remains immutable:**
   All exit-order, PnL, settlement, and position financial calculations remain Decimal-native. Float is rejected at Pydantic boundary. USDC micro-unit conversion uses `Decimal("1e6")` only.

3. **Quarter-Kelly and exposure policy remain immutable:**
   Phase 7 does not alter `kelly_fraction=0.25` or the system-wide `min(kelly_size, 0.03 * bankroll)` exposure policy. Exit sizing reads recorded position metadata; it does not recalculate Kelly fractions.

4. **`dry_run=True` remains a hard execution stop:**
   Dry run blocks all order signing, CLOB broadcast, and settlement DB writes. Phase 7 components may compute, log, and return typed artifacts in dry run, but they may not persist state changes or submit orders.

5. **Repository pattern remains the sole DB access path:**
   `PositionRepository` is the only component that touches the `positions` table. `record_settlement()` is additive — it does not modify existing methods. No raw SQL, no direct session manipulation.

6. **Async pipeline behavior remains immutable:**
   Phase 7 preserves the existing non-blocking, queue-driven four-layer architecture. The new `ExitScanTask` is an independent async task — it does not block, replace, or interfere with the execution consumer loop.

7. **Module isolation remains enforced:**
   `ExitOrderRouter` and `PnLCalculator` have zero imports from prompt, context, evaluation, or ingestion modules. They receive and produce only typed contracts from `src/schemas/`.

8. **Entry-path routing is read-only for Phase 7:**
   `ExecutionRouter` internals are unmodified. Phase 7 extends the exit path only.

## 8. Success Criteria For Phase 7

Phase 7 is complete when all of the following are true:

1. Exit evaluation runs as an independent async task (`ExitScanTask`) on a configurable interval, decoupled from the execution consumer loop.
2. Exit decisions (`ExitResult.should_exit=True`) are converted into signed SELL-side `OrderData` payloads via `ExitOrderRouter`, with slippage protection and full dry-run support.
3. Realized PnL is computed at position closure using Decimal-only arithmetic and persisted to the `positions` table via `PositionRepository.record_settlement()`.
4. `dry_run=True` blocks all signing, broadcast, and settlement DB writes while permitting full computation and structured logging.
5. All three components (`ExitOrderRouter`, `PnLCalculator`, `_exit_scan_loop`) use fail-open semantics — failures do not block the exit scan loop or the execution consumer.
6. Position state transitions remain `OPEN -> CLOSED` only. Settlement writes (`realized_pnl`, `exit_price`, `closed_at_utc`) are additive and do not change status.
7. Full regression remains green and project coverage stays at or above 80%.
8. All prior architectural invariants remain in force: Decimal safety, repository isolation, Gatekeeper authority, no hardcoded market identifiers, `dry_run` execution blocking, and async-only pipeline.

## 9. Next Phase

Phase 8 should address risk management and operational observability. Potential scope includes:

- **Portfolio-level exposure aggregation** — enforcing cross-position exposure limits beyond the per-order 3% cap, requiring real-time summation of all `OPEN` position sizes against current bankroll.
- **Risk dashboard and observability** — surfacing position lifecycle events, exit-decision audit trails, PnL summaries, and portfolio health metrics for operational visibility.
- **Fee-aware PnL** — extending `PnLCalculator` to account for CLOB trading fees and Polygon gas costs in realized PnL computation.
- **Alerting and circuit breakers** — automated alerts when portfolio drawdown, position count, or stale-market exposure exceeds configured thresholds, with optional circuit-breaker logic to pause new order routing.

Detailed scope, work items, and acceptance criteria to be finalized in the Phase 8 PRD.
