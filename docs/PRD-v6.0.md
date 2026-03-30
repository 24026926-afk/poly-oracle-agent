# PRD v6.0 - Poly-Oracle-Agent Phase 6

Source inputs: `docs/PRD-v5.0.md`, `STATE.md`, `docs/system_architecture.md`, `docs/business_logic/business_logic_wi17.md`, `docs/business_logic/business_logic_wi19.md`, `docs/archive/ARCHIVE_PHASE_6.md`.

## 1. Executive Summary

Phase 6 closes the position lifecycle gap that Phase 5 left open. After Phase 5, the system could route a validated BUY decision through Kelly sizing, slippage checks, and optional signing — but once `ExecutionRouter.route()` returned an `ExecutionResult`, the outcome was ephemeral. No component tracked which markets carried open exposure, what entry price was locked, or how much capital was committed. And no component ever re-evaluated an open position against changing market conditions.

Phase 6 adds two capabilities:

1. **Position persistence** — every routed execution outcome (EXECUTED, DRY_RUN, FAILED) is recorded as a typed `PositionRecord` in a new `positions` table with full financial audit fields and Decimal-safe storage.
2. **Exit evaluation** — open positions are scanned against fresh order-book data and evaluated against conservative, rule-based exit criteria that transition positions from `OPEN` to `CLOSED` when risk thresholds are breached.

The execution order was deliberate:
- `WI-17` introduced persistent position tracking before exit logic existed, ensuring every execution outcome has an auditable record.
- `WI-19` consumed those position records to evaluate exit conditions, completing the `OPEN -> CLOSED` lifecycle arc.

Phase 6 preserves the same four-layer async architecture and the same terminal authority of `LLMEvaluationResponse`. It extends Layer 4 (Execution) with downstream lifecycle management without weakening the existing safety model: Decimal-only money paths, quarter-Kelly sizing, 3% exposure policy, repository isolation, and `dry_run=True` as a hard stop for all DB mutations and broadcast side effects. Phase 6 completion is recorded at 295 passing tests with 92% coverage.

## 2. Core Pillars

### 2.1 Position Persistence

Execution outcomes must be durable. Phase 6 introduced a persistent position record for every routed decision so the system can answer: which markets have open exposure, at what entry price, and with how much capital? This is the foundational data layer that exit evaluation, portfolio aggregation, and PnL accounting will build upon.

### 2.2 Rule-Based Exit Evaluation

Open positions must be re-evaluated. Phase 6 introduced a deterministic, rule-based exit engine that scans open positions against fresh market data and conservative exit thresholds. Exit criteria are Decimal-only comparisons — no LLM reasoning is involved in exit decisions, preserving auditability and avoiding latency or cost in the lifecycle loop.

## 3. Work Items

### WI-17: Position Tracker

**Objective**
Introduce `PositionTracker`, the component that persists execution outcomes from `ExecutionRouter.route()` as typed `PositionRecord` entries in a new `positions` table, providing lifecycle visibility into open, closed, and failed positions.

**Scope Boundaries**

In scope:
- `PositionTracker` async component converting `ExecutionResult` into persisted `PositionRecord` entries
- `PositionStatus` enum (`OPEN | CLOSED | FAILED`) and `PositionRecord` Pydantic model with Decimal-validated financial fields
- SQLAlchemy `Position` ORM model with `Numeric(38,18)` for all five financial columns
- `PositionRepository` async CRUD following the existing `ExecutionRepository` pattern (5 methods)
- Alembic migration `0002_add_positions_table.py` with three indexes
- `dry_run=True` enforcement: structured log only, zero DB writes, zero session creation
- Orchestrator wiring: constructed in `__init__()`, called after `ExecutionRouter.route()` and before broadcast

Out of scope:
- Exit / close logic (deferred to WI-19)
- PnL calculation or mark-to-market
- Position broadcasting or external notification
- Portfolio-level aggregation queries
- Modifications to `ExecutionRouter`, `LLMEvaluationResponse`, or Gatekeeper logic
- Foreign keys between `positions` and other tables

**Components Delivered**

| Component | Location |
|---|---|
| `PositionTracker` | `src/agents/execution/position_tracker.py` |
| `PositionRecord` model | `src/schemas/position.py` (re-exported from `src/schemas/execution.py`) |
| `PositionStatus` enum | `src/schemas/position.py` (re-exported from `src/schemas/execution.py`) |
| `Position` ORM model | `src/db/models.py` |
| `PositionRepository` | `src/db/repositories/position_repository.py` |
| Alembic migration | `migrations/versions/0002_add_positions_table.py` |

**Key Invariants Enforced**

1. `PositionTracker` operates strictly downstream of validated `ExecutionResult` — it cannot bypass Gatekeeper or originate a position record on its own.
2. All five financial fields (`entry_price`, `order_size_usdc`, `kelly_fraction`, `best_ask_at_entry`, `bankroll_usdc_at_entry`) are `Decimal`. Float is rejected at Pydantic boundary via `field_validator`.
3. Status derivation is deterministic: `EXECUTED/DRY_RUN -> OPEN`, `FAILED -> FAILED`, `SKIP -> None`. WI-17 never writes `CLOSED`.
4. `dry_run=True` blocks all DB writes via an early-return guard. The full `PositionRecord` is logged via `structlog` but no `AsyncSession` is created and no repository method is called.
5. `FAILED` results with `None` financial fields use `Decimal("0")` sentinels — no null Decimal columns reach the database.
6. Unreachable state guards (`EXECUTED+dry_run`, `DRY_RUN+live`) log error and return `None`.
7. `PositionTracker` has zero imports from prompt, context, evaluation, or ingestion modules. It receives only typed `ExecutionResult`.
8. Repository pattern is extended, not modified — `PositionRepository` follows the same contract as `ExecutionRepository`.

**Acceptance Criteria Met**

1. `PositionTracker` exists in `src/agents/execution/position_tracker.py` with `record_execution(result, condition_id, token_id) -> PositionRecord | None` as the sole public async entry point.
2. `PositionRecord` Pydantic model is frozen, Decimal-validated, with float-rejecting validators on all five financial fields.
3. SQLAlchemy `Position` ORM model uses `Numeric(38,18)` for financial columns and has three indexes.
4. Alembic migration `0002_add_positions_table.py` creates the `positions` table; `alembic upgrade head` succeeds on a fresh database.
5. `PositionRepository` implements five async methods following the `ExecutionRepository` pattern.
6. `dry_run=True` produces zero DB writes and zero session creation.
7. `PositionTracker` is constructed in `Orchestrator.__init__()` and called in `_execution_consumer_loop()` after `ExecutionRouter.route()`.
8. A `record_execution()` failure does not block the broadcast path (fail-open for audit, not safety).
9. MAAP audit caught and resolved 2 orchestrator wiring defects (token_id field, dry_run bypass).
10. WI-17 added 27 tests and moved the project to 257 total tests at 92% coverage.

### WI-19: Exit Strategy Engine

**Objective**
Introduce `ExitStrategyEngine`, the component that evaluates `OPEN` positions persisted by `PositionTracker` (WI-17) against typed exit criteria using fresh market data, and determines whether to hold or close each position.

**Scope Boundaries**

In scope:
- `ExitStrategyEngine` async component evaluating single positions and batch-scanning all open positions
- `ExitReason` enum (`NO_EDGE | STOP_LOSS | TIME_DECAY | TAKE_PROFIT | STALE_MARKET | ERROR`)
- `ExitSignal` and `ExitResult` frozen Pydantic models with Decimal-validated financial fields
- Four Decimal-only exit criteria: stop-loss, time-decay, no-edge, take-profit
- Deterministic priority ordering: `STOP_LOSS > TIME_DECAY > NO_EDGE > TAKE_PROFIT`
- Conservative hold-by-default semantics
- Position state mutation via `PositionRepository.update_status()` for `OPEN -> CLOSED` transitions
- `dry_run=True` enforcement: structured log of exit decision, zero DB writes, zero state mutations
- Three new `AppConfig` exit-threshold fields (all `Decimal`)
- `ExitEvaluationError` and `ExitMutationError` typed exceptions
- Orchestrator wiring: constructed in `__init__()`, `scan_open_positions()` called after `PositionTracker.record_execution()`

Out of scope:
- Exit-order submission, signing, or broadcast
- Realized PnL or settlement accounting
- Portfolio-level risk aggregation or cross-position exposure limits
- LLM-assisted exit reasoning
- Modifications to `PositionTracker`, `PositionRepository` internals, `PositionRecord` schema, or `ExecutionRouter`
- Retry logic for stale market data or failed repository calls

**Components Delivered**

| Component | Location |
|---|---|
| `ExitStrategyEngine` | `src/agents/execution/exit_strategy_engine.py` |
| `ExitReason` enum | `src/schemas/execution.py` |
| `ExitSignal` model | `src/schemas/execution.py` |
| `ExitResult` model | `src/schemas/execution.py` |
| `ExitEvaluationError` | `src/core/exceptions.py` |
| `ExitMutationError` | `src/core/exceptions.py` |
| Config: `exit_position_max_age_hours` | `src/core/config.py` (default: `Decimal("48")`) |
| Config: `exit_stop_loss_drop` | `src/core/config.py` (default: `Decimal("0.15")`) |
| Config: `exit_take_profit_gain` | `src/core/config.py` (default: `Decimal("0.20")`) |

**Key Invariants Enforced**

1. `ExitStrategyEngine` operates strictly downstream of validated execution outcomes. No exit path bypasses Gatekeeper authority.
2. Exit criteria are rule-based and deterministic — no LLM reasoning, no prompt construction, no external API calls beyond `PolymarketClient.fetch_order_book()`.
3. All exit-evaluation arithmetic (edge, age, threshold comparisons) is `Decimal`-only. Float is rejected at Pydantic boundary.
4. Conservative hold-by-default: positions remain `OPEN` unless at least one criterion is triggered.
5. Priority ordering is deterministic when multiple criteria trigger simultaneously: `STOP_LOSS > TIME_DECAY > NO_EDGE > TAKE_PROFIT`.
6. `dry_run=True` early-return guard fires before any session creation or mutation. Read-path DB access in `scan_open_positions()` is permitted (to enumerate open positions).
7. Only `OPEN -> CLOSED` transitions are allowed. `ExitStrategyEngine` never creates positions, never writes `OPEN` or `FAILED`, and only calls `update_status()`.
8. `ExitStrategyEngine` has zero imports from prompt, context, evaluation, or ingestion modules.
9. Failure semantics are fail-open: a failed `scan_open_positions()` or `evaluate_position()` does not block the execution consumer or broadcast path. Missed evaluations are retried on the next scan cycle.
10. Stale-market handling is a first-class exit reason — when `fetch_order_book()` returns `None`, the engine produces `ExitResult(should_exit=True, exit_reason=STALE_MARKET)`.

**Acceptance Criteria Met**

1. `ExitStrategyEngine` exists in `src/agents/execution/exit_strategy_engine.py` with two public async methods: `evaluate_position(signal) -> ExitResult` and `scan_open_positions() -> list[ExitResult]`.
2. `ExitSignal` and `ExitResult` Pydantic models are frozen, Decimal-validated, with float-rejecting validators.
3. Stop-loss triggers when `unrealized_edge <= -exit_stop_loss_drop`.
4. Time-decay triggers when `position_age_hours >= exit_position_max_age_hours`.
5. No-edge triggers when `unrealized_edge <= Decimal("0")` (below stop-loss threshold).
6. Take-profit triggers when `unrealized_edge >= exit_take_profit_gain`.
7. Non-`OPEN` positions return `ExitResult(should_exit=False, exit_reason=ERROR)`.
8. `dry_run=True` produces zero DB writes and zero state mutations.
9. `dry_run=False` + `should_exit=True` calls `PositionRepository.update_status(position_id, new_status=CLOSED)`.
10. `scan_open_positions()` produces `STALE_MARKET` exit when order-book fetch returns `None`.
11. `ExitStrategyEngine` is constructed in `Orchestrator.__init__()` and invoked after `PositionTracker.record_execution()` in the execution consumer loop.
12. Exit evaluation failure does not block the broadcast path.
13. WI-19 added 38 tests and completed Phase 6 at 295 total tests and 92% coverage.

## 4. Architecture Impact

### 4.1 Layer 4 Extension

Phase 6 extends Layer 4 (Execution) with two new downstream components while preserving the existing execution pipeline:

```text
Layer 1: Ingestion
  CLOBWebSocketClient + GammaRESTClient + MarketDiscoveryEngine

Layer 2: Context
  DataAggregator + PromptFactory

Layer 3: Evaluation
  ClaudeClient + LLMEvaluationResponse Gatekeeper

Layer 4: Execution
  BankrollSyncProvider -> ExecutionRouter -> PositionTracker -> ExitStrategyEngine
  -> TransactionSigner -> NonceManager -> GasEstimator -> OrderBroadcaster
```

Queue topology unchanged: `market_queue -> prompt_queue -> execution_queue`. Position tracking and exit evaluation are synchronous steps within the execution consumer loop, not separate queue consumers.

### 4.2 Execution Consumer Loop Flow

```text
execution_consumer_loop:
  1. Dequeue item from execution_queue
  2. Extract LLMEvaluationResponse + MarketContext from item
  3. ExecutionRouter.route(response, market_context) -> ExecutionResult       [WI-16]
  4. PositionTracker.record_execution(result, condition_id, token_id)         [WI-17]
  5. ExitStrategyEngine.scan_open_positions()                                 [WI-19]
  6. If EXECUTED and not dry_run, proceed to broadcast                        [existing]
```

`PositionTracker` is placed before the dry-run gate and broadcast path so both live and dry-run execution outcomes are tracked. `ExitStrategyEngine` scans all open positions on each loop iteration, evaluating against fresh market data.

### 4.3 Preserved Boundaries

Phase 6 does not alter:
- **Gatekeeper authority** — `LLMEvaluationResponse` remains the sole terminal validation boundary before execution-aware routing.
- **Decimal financial integrity** — all position, exit signal, and exit result financial fields are Decimal-native with float rejection at Pydantic boundary.
- **Quarter-Kelly and exposure policy** — `kelly_fraction=0.25` and `min(kelly_size, 0.03 * bankroll)` are unchanged. `PositionTracker` records the Kelly fraction used; `ExitStrategyEngine` reads it as immutable metadata.
- **Repository pattern** — `PositionRepository` is the sole mutation path for the `positions` table. No raw SQL or direct session manipulation.
- **Async pipeline** — Phase 6 added no blocking execution path. All new operations are `async`.

### 4.4 Database Schema Extension

Phase 6 added one table and zero modifications to existing tables:

| Table | Migration | Columns | Financial Precision |
|---|---|---|---|
| `positions` | `0002_add_positions_table.py` | 15 columns, 3 indexes | `Numeric(38,18)` for all 5 financial fields |

Existing tables (`market_snapshots`, `agent_decision_logs`, `execution_txs`) are untouched. No foreign keys between `positions` and other tables — standalone by design.

## 5. Risk and Safety Notes

### 5.1 dry_run Behavior

`dry_run=True` remains a hard stop for all Layer 4 side effects:

| Component | dry_run=True behavior |
|---|---|
| `PositionTracker` | Builds full `PositionRecord`, logs via `structlog`, returns model. Zero DB writes, zero session creation. |
| `ExitStrategyEngine` | Computes full `ExitResult` including all criteria checks and logs via `structlog`. Zero state mutations. Read-path DB access for `scan_open_positions()` is permitted. |

The dry-run guard is enforced by an early-return at the top of the write/mutation path, not by a downstream check.

### 5.2 Failure Semantics

Both Phase 6 components use **fail-open** semantics — failures do not block the execution consumer or broadcast path:

| Component | Failure behavior | Rationale |
|---|---|---|
| `PositionTracker` | Exception caught by consumer loop; broadcast proceeds | Position persistence is an audit concern, not a safety gate. Safety is upstream. |
| `ExitStrategyEngine` | Exception caught by consumer loop; position remains `OPEN` | Exit evaluation is a risk-management optimization. Missed evaluations are retried on the next scan cycle. |

This is the correct design: the safety gates are upstream (Gatekeeper, slippage guard, dry_run). Position tracking and exit evaluation add observability and risk management but must never impede the core execution path.

### 5.3 Position State Mutation Rules

- `PositionTracker` writes `OPEN` and `FAILED` only. It never writes `CLOSED`.
- `ExitStrategyEngine` transitions `OPEN -> CLOSED` only. It never creates positions and never writes `OPEN` or `FAILED`.
- `CLOSED -> OPEN` is not a valid transition. Once closed, a position is terminal.
- All state mutations go through `PositionRepository.update_status()` — no direct ORM manipulation.

### 5.4 Exit Evaluation Safety

- Exit criteria are deterministic rule-based Decimal comparisons — no LLM involvement, no probabilistic reasoning.
- Conservative hold-by-default: a position stays open unless at least one exit criterion is explicitly triggered.
- Stale-market detection surfaces positions in illiquid or delisted markets rather than silently holding.
- No exit orders are submitted — WI-19 produces a typed decision only. Actual exit-order routing is deferred to a future phase.

## 6. Metrics

| Metric | Value |
|---|---|
| Total tests | 295 |
| Passing | 295/295 |
| Coverage | 92% (target >= 80%) |
| WI-17 tests added | 27 (unit + integration) |
| WI-19 tests added | 38 (unit + integration) |
| MAAP defects caught | 2 (both resolved) |
| Regression gate | `pytest --asyncio-mode=auto tests/ -q` green |

## 7. Strict Constraints

The following constraints are mandatory and non-negotiable for all Phase 6 work:

1. **Gatekeeper remains immutable:**
   `LLMEvaluationResponse` remains the sole terminal validation boundary before execution-aware routing. No Phase 6 component bypasses, replaces, or weakens that authority. Position tracking and exit evaluation operate strictly downstream.

2. **Decimal financial integrity remains immutable:**
   All position-record, exit-signal, exit-result, edge, age, and threshold calculations remain Decimal-native. Float is rejected at Pydantic boundary. USDC micro-unit conversion uses `Decimal("1e6")` only.

3. **Quarter-Kelly and exposure policy remain immutable:**
   Phase 6 does not alter `kelly_fraction=0.25` or the system-wide `min(kelly_size, 0.03 * bankroll)` exposure policy. `PositionTracker` records the fraction used; `ExitStrategyEngine` reads it as immutable metadata.

4. **`dry_run=True` remains a hard execution stop:**
   Dry run blocks all DB writes (position inserts and status mutations) and all broadcast side effects. Phase 6 components may compute, log, and return typed artifacts in dry run, but they may not persist state changes.

5. **Repository pattern remains the sole DB access path:**
   `PositionRepository` is the only component that touches the `positions` table. No raw SQL, no direct session manipulation, no bypassing the repository layer.

6. **Async pipeline behavior remains immutable:**
   Phase 6 preserves the existing non-blocking, queue-driven four-layer architecture. New components operate within the existing execution consumer loop and do not introduce synchronous bottlenecks or alternate routing.

7. **Module isolation remains enforced:**
   `PositionTracker` and `ExitStrategyEngine` have zero imports from prompt, context, evaluation, or ingestion modules. They receive and produce only typed contracts from `src/schemas/`.

## 8. Success Criteria For Phase 6

Phase 6 is complete when all of the following are true:

1. Every routed execution outcome (EXECUTED, DRY_RUN, FAILED) is persisted as a typed `PositionRecord` with Decimal-safe financial fields and full audit metadata.
2. Open positions are scannable and evaluable against fresh order-book data using deterministic, rule-based exit criteria.
3. Exit evaluation correctly identifies stop-loss, time-decay, no-edge, take-profit, and stale-market conditions with deterministic priority ordering.
4. Position state transitions follow `OPEN -> CLOSED` only, mediated exclusively through `PositionRepository.update_status()`.
5. `dry_run=True` blocks all position inserts and status mutations while permitting read-path access and returning fully computed typed artifacts.
6. Position tracking and exit evaluation failures are fail-open and never block the execution consumer or broadcast path.
7. Full regression remains green and project coverage stays at or above 80%.
8. All prior architectural invariants remain in force: Decimal safety, repository isolation, Gatekeeper authority, no hardcoded market identifiers, `dry_run` execution blocking, and async-only pipeline.

## 9. Next Phase

Phase 7 should close the remaining lifecycle and observability gaps. Potential scope includes:

- **Exit order routing** — submitting CLOB exit orders for positions that `ExitStrategyEngine` has marked for closure, composing with the existing `ExecutionRouter` and `TransactionSigner` infrastructure.
- **Realized PnL accounting** — computing actual profit/loss against settled market outcomes, requiring resolution data and settlement integration.
- **Portfolio-level exposure aggregation** — aggregating open position exposure across markets to enforce portfolio-wide risk limits beyond the per-order cap.
- **Risk dashboards and observability** — surfacing position lifecycle, exit-decision audit trails, and portfolio metrics for operational visibility.

Detailed scope, work items, and acceptance criteria to be finalized in the Phase 7 PRD.
