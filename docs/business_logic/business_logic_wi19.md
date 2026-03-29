# WI-19 Business Logic — Exit Strategy Engine (Evaluate Open Positions for Lifecycle Transitions)

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — `ExitStrategyEngine` is async; market-data reads, repository queries, and status updates are awaited. Queue topology (`market_queue -> prompt_queue -> execution_queue`) is unchanged.
- `.agents/rules/db-engineer.md` — position state transitions delegate to `PositionRepository.update_status()`; repository pattern is preserved. No raw SQL, no session lifecycle ownership.
- `.agents/rules/risk-auditor.md` — all exit-evaluation financial fields are `Decimal`; no `float` intermediary in any pricing, PnL, or threshold comparison. Stop-loss and take-profit thresholds are `Decimal`-typed config fields.
- `.agents/rules/security-auditor.md` — `dry_run=True` blocks all position state mutations; the engine logs would-be exit decisions via `structlog` only. No credentials, private keys, or broadcast capability in exit logic.
- `.agents/rules/test-engineer.md` — WI-19 exit-strategy behavior requires unit + integration coverage; full suite remains >= 80%.

## 1. Objective

Introduce `ExitStrategyEngine`, the component that evaluates `OPEN` positions persisted by `PositionTracker` (WI-17) against a set of typed exit criteria and determines whether to hold or close each position.

WI-17 closed the gap between execution routing and persistent position state — after WI-17, every routed order is recorded as an `OPEN` or `FAILED` `PositionRecord`. However, no component re-evaluates those open positions against current market conditions. Once a position is opened it stays `OPEN` forever. WI-19 closes this lifecycle gap by introducing conservative, rule-based exit evaluation that can transition a position from `OPEN` to `CLOSED`.

WI-19 does **not** submit exit orders to the CLOB, calculate realized PnL against settled outcomes, or aggregate portfolio-level exposure. It produces a typed `ExitResult` that downstream components (future WIs) can use to trigger exit-order routing.

## 2. Scope Boundaries

### In Scope

1. New `ExitReason` enum: categorized exit-decision drivers (`NO_EDGE | STOP_LOSS | TIME_DECAY | TAKE_PROFIT | STALE_MARKET | ERROR`).
2. New `ExitSignal` Pydantic model: typed input containing the position to evaluate and a fresh market snapshot.
3. New `ExitResult` Pydantic model: typed output containing the exit decision, reason, and supporting metrics.
4. New `ExitStrategyEngine` class: async component that evaluates one `OPEN` position and produces an `ExitResult`.
5. Position state mutation: calls `PositionRepository.update_status(position_id, new_status=CLOSED)` when an exit is approved and `dry_run=False`.
6. `dry_run=True` enforcement: structured log of would-be exit decision, zero DB writes, zero state mutations.
7. Conservative hold-by-default logic: position remains `OPEN` unless at least one exit criterion is triggered.
8. New `AppConfig` fields: `exit_position_max_age_hours`, `exit_stop_loss_drop`, `exit_take_profit_gain`.
9. Orchestrator wiring: `ExitStrategyEngine` constructed in `__init__()`, invoked after `PositionTracker.record_execution()`.

### Out of Scope

1. Exit-order submission or broadcast — the engine decides to close but does not route, sign, or transmit an exit order.
2. Realized PnL or settlement accounting — position closure is a state transition only; actual settlement requires resolution data not yet available.
3. Portfolio-level risk aggregation or cross-position exposure limits — exit decisions are per-position.
4. LLM-assisted exit reasoning — exit criteria are rule-based; no Claude/Grok call is made.
5. Modifications to `PositionTracker`, `PositionRepository` internals, or `PositionRecord` schema.
6. Modifications to `ExecutionRouter` internals or `LLMEvaluationResponse` schema.
7. Modifications to Alembic migrations or `Position` ORM model columns — WI-17 schema is sufficient.
8. Retry logic for stale market data or failed repository calls.

## 3. Target Component Architecture + Data Contracts

### 3.1 Exit Strategy Engine Component (New Class)

- **Module:** `src/agents/execution/exit_strategy_engine.py`
- **Class Name:** `ExitStrategyEngine` (exact)
- **Responsibility:** evaluate a single `OPEN` `PositionRecord` against exit criteria using a fresh market snapshot, produce an `ExitResult`, and delegate state mutation to `PositionRepository` when approved.

Isolation rule:
- `ExitStrategyEngine` must remain execution-layer only. It must not depend on context-building, prompt logic, evaluation logic, or ingestion modules.
- `ExitStrategyEngine` receives only typed `ExitSignal` — never raw LLM outputs, evaluation prompts, or `MarketContext` objects.
- It reads fresh market data via `PolymarketClient.fetch_order_book()` but owns no pricing logic.

### 3.2 Data Contracts (Required)

Exit boundary must use typed contracts (Pydantic at boundary is required). Minimum contracts:

1. `ExitReason` (enum in `src/schemas/execution.py`)
   - `NO_EDGE` — current midpoint ≤ entry price; the edge that justified entry has evaporated
   - `STOP_LOSS` — current midpoint has dropped by more than `exit_stop_loss_drop` below entry price
   - `TIME_DECAY` — position age exceeds `exit_position_max_age_hours`; stale exposure
   - `TAKE_PROFIT` — current midpoint has risen by more than `exit_take_profit_gain` above entry price
   - `STALE_MARKET` — fresh order-book fetch returned `None`; market data unavailable for evaluation
   - `ERROR` — evaluation failed internally; position held by default

2. `ExitSignal` (Pydantic model in `src/schemas/execution.py`)
   - `position`: `PositionRecord` (the `OPEN` position to evaluate)
   - `current_midpoint`: `Decimal` (fresh midpoint_probability from `PolymarketClient`)
   - `current_best_bid`: `Decimal` (fresh best_bid from order-book snapshot — realistic exit price)
   - `evaluated_at_utc`: `datetime` (timestamp of the evaluation)

   Hard rules:
   - `current_midpoint` and `current_best_bid` are `Decimal`. No `float` intermediary.
   - `float` inputs in financial fields are rejected at schema boundary via `field_validator`, identical to `PositionRecord._reject_float_financials`.
   - Model is frozen (immutable after creation).

3. `ExitResult` (Pydantic model in `src/schemas/execution.py`)
   - `position_id`: `str` (UUID of the evaluated position)
   - `condition_id`: `str` (Polymarket market identifier, for logging)
   - `should_exit`: `bool` (`True` if exit criteria met; `False` for hold)
   - `exit_reason`: `ExitReason` (which criterion triggered, or `ERROR`)
   - `entry_price`: `Decimal` (from position record, echoed for auditability)
   - `current_midpoint`: `Decimal` (market snapshot at evaluation time)
   - `current_best_bid`: `Decimal` (realistic exit price at evaluation time)
   - `position_age_hours`: `Decimal` (hours since `routed_at_utc`)
   - `unrealized_edge`: `Decimal` (`current_midpoint - entry_price`; positive = favorable)
   - `evaluated_at_utc`: `datetime`

   Hard rules:
   - All five financial fields (`entry_price`, `current_midpoint`, `current_best_bid`, `position_age_hours`, `unrealized_edge`) are `Decimal`. No `float` intermediary.
   - `float` inputs are rejected at schema boundary via `field_validator`.
   - Model is frozen (immutable after creation).

## 4. Core Method Contracts (async, typed)

### 4.1 ExitStrategyEngine — Async Evaluation Entry Point

Required public method:

- `evaluate_position(signal: ExitSignal) -> ExitResult` (async)

Behavior requirements:

1. **Status gate:** if `signal.position.status != PositionStatus.OPEN`, log warning and return `ExitResult(should_exit=False, exit_reason=ExitReason.ERROR)`. Only `OPEN` positions are evaluable.
2. **Age calculation:** compute `position_age_hours = Decimal(str((signal.evaluated_at_utc - signal.position.routed_at_utc).total_seconds())) / Decimal("3600")`.
3. **Unrealized edge:** compute `unrealized_edge = signal.current_midpoint - signal.position.entry_price`.
4. **Stop-loss check:** if `unrealized_edge <= -config.exit_stop_loss_drop` (edge has dropped by more than the configured amount), trigger exit with `ExitReason.STOP_LOSS`.
5. **Time-decay check:** if `position_age_hours >= config.exit_position_max_age_hours`, trigger exit with `ExitReason.TIME_DECAY`.
6. **No-edge check:** if `unrealized_edge <= Decimal("0")` (edge is zero or negative but above stop-loss), trigger exit with `ExitReason.NO_EDGE`.
7. **Take-profit check:** if `unrealized_edge >= config.exit_take_profit_gain`, trigger exit with `ExitReason.TAKE_PROFIT`.
8. **Hold default:** if no criterion triggered, return `ExitResult(should_exit=False)` with `exit_reason` set to the highest-priority non-triggered reason for auditability (convention: `NO_EDGE` as the default hold reason).
9. **Priority ordering:** when multiple criteria trigger simultaneously, select the reason with the highest severity: `STOP_LOSS > TIME_DECAY > NO_EDGE > TAKE_PROFIT`. This ordering prioritizes risk-protective exits.
10. **Build ExitResult:** populate all fields from the signal, computed values, and the determined reason.
11. **dry_run gate:** if `config.dry_run is True`, log the full `ExitResult` via `structlog` at INFO level, return the Pydantic model. Do NOT open a DB session, do NOT call any repository method.
12. **Live mutation:** if `config.dry_run is False` and `should_exit is True`, open a session from the injected factory, instantiate `PositionRepository`, call `update_status(position_id, new_status=PositionStatus.CLOSED)`, return `ExitResult`.
13. **Live hold:** if `config.dry_run is False` and `should_exit is False`, return `ExitResult` without any repository call.

### 4.2 ExitStrategyEngine — Batch Scan Entry Point

Required public method:

- `scan_open_positions() -> list[ExitResult]` (async)

Behavior requirements:

1. Open a session from the injected factory, instantiate `PositionRepository`, call `get_open_positions()`.
2. For each `OPEN` position, fetch a fresh order-book snapshot via `PolymarketClient.fetch_order_book(position.token_id)`.
3. If the snapshot is `None` (stale/unavailable market), produce `ExitResult(should_exit=True, exit_reason=ExitReason.STALE_MARKET)` for that position.
4. If the snapshot is available, build an `ExitSignal` and delegate to `evaluate_position()`.
5. Collect and return all `ExitResult` instances.
6. If the repository call or session creation fails, raise `ExitEvaluationError` — the caller catches this.
7. `dry_run` enforcement is inherited from `evaluate_position()` — no additional gate needed here.

### 4.3 Exit Criteria Formulas

All exit criteria use `Decimal`-only arithmetic:

```
unrealized_edge = current_midpoint - entry_price

stop_loss_triggered  = unrealized_edge <= -exit_stop_loss_drop
time_decay_triggered = position_age_hours >= exit_position_max_age_hours
no_edge_triggered    = unrealized_edge <= Decimal("0")
take_profit_triggered = unrealized_edge >= exit_take_profit_gain
```

Hard constraints:
1. Every variable is `Decimal`. No `float()` conversion at any step.
2. `exit_stop_loss_drop` is `Decimal(str(config.exit_stop_loss_drop))`.
3. `exit_take_profit_gain` is `Decimal(str(config.exit_take_profit_gain))`.
4. `position_age_hours` uses `Decimal` division of `total_seconds()` by `3600`.
5. Comparison operators are applied to `Decimal` operands only.

### 4.4 New AppConfig Fields (Required)

Three new fields must be added to `AppConfig` in `src/core/config.py`:

1. `exit_position_max_age_hours: Decimal = Field(default=Decimal("48"), description="Max hours before an open position triggers time-decay exit")`
2. `exit_stop_loss_drop: Decimal = Field(default=Decimal("0.15"), description="Midpoint drop from entry price that triggers stop-loss exit (e.g. 0.15 = 15pp)")`
3. `exit_take_profit_gain: Decimal = Field(default=Decimal("0.20"), description="Midpoint gain from entry price that triggers take-profit exit (e.g. 0.20 = 20pp)")`

Hard constraints:
1. All three fields are `Decimal`, not `float`.
2. `exit_position_max_age_hours` defaults to 48 (conservative — two full market days).
3. `exit_stop_loss_drop` defaults to `0.15` (15 percentage points below entry).
4. `exit_take_profit_gain` defaults to `0.20` (20 percentage points above entry).

### 4.5 Error Types (Required)

New typed exceptions in `src/core/exceptions.py`:

1. `ExitEvaluationError(PolyOracleError)` — exit evaluation failed (market data unavailable, age calculation error, unexpected state).
2. `ExitMutationError(PolyOracleError)` — position state transition failed (DB error, repository unavailable, position already closed).

All exceptions must include structured context (position_id, condition_id, reason) for logging.

## 5. Pipeline Integration Design

WI-19 integration has two modes:

### Mode A — Inline evaluation (after new position recording)

```
execution_consumer_loop:
  1. Dequeue item from execution_queue
  2. Extract LLMEvaluationResponse + MarketContext from item
  3. ExecutionRouter.route(response, market_context) → ExecutionResult   (WI-16)
  4. PositionTracker.record_execution(result, condition_id, token_id)    (WI-17)
  5. ExitStrategyEngine.scan_open_positions()                            ← WI-19 (here)
  6. If EXECUTED and not dry_run, proceed to broadcast (existing path)
```

### Mode B — Periodic scan (standalone evaluation loop)

```
exit_scan_loop (new async task in Orchestrator):
  1. Sleep for config.exit_scan_interval_seconds
  2. ExitStrategyEngine.scan_open_positions()                            ← WI-19
  3. Log summary of hold/close decisions
  4. Repeat
```

Mode B is the preferred integration for production: exit evaluation should not block or delay the execution consumer loop. Mode A is acceptable for initial implementation (simpler wiring) and can be promoted to Mode B in a follow-up WI.

**Important:** The exit scan call is fire-and-forget safe — a failed `scan_open_positions()` or `evaluate_position()` is caught by the existing `except Exception` handler. An exit evaluation failure must never block or abort the execution consumer or broadcast path.

### 5.1 Constructor Dependencies (Injected)

`ExitStrategyEngine.__init__` receives:

1. `config: AppConfig` — exit thresholds, `dry_run` flag.
2. `polymarket_client: PolymarketClient` — for `fetch_order_book()` to get fresh midpoint/bid.
3. `db_session_factory: async_sessionmaker[AsyncSession]` — injected session factory for repository construction.

`ExitStrategyEngine` is constructed in `Orchestrator.__init__()` regardless of `dry_run` mode — the engine itself enforces the write gate internally.

### 5.2 Failure Semantics (Fail Open)

Unlike `ExecutionRouter` (which is fail-closed), `ExitStrategyEngine` failures must not block execution:

1. If `evaluate_position()` raises, the exception is caught by the caller. The position remains `OPEN`.
2. If `scan_open_positions()` raises, all positions remain in their current state.
3. A structured error log is emitted with the failure reason and position_id.
4. The broadcast path and execution consumer loop proceed unaffected.

This is deliberate: exit evaluation is a risk-management optimization, not a safety gate. The safety gates are upstream (Gatekeeper, slippage guard, dry_run). A missed exit evaluation will be retried on the next scan cycle.

### 5.3 dry_run Behavior

When `config.dry_run is True`:

1. `evaluate_position()` computes the full `ExitResult` Pydantic model including all criteria checks.
2. The result is logged via `structlog` at INFO level (`position_id`, `condition_id`, `should_exit`, `exit_reason`, `unrealized_edge`, `position_age_hours`).
3. No `AsyncSession` is created for mutation. No `PositionRepository.update_status()` is called. Zero DB writes.
4. `scan_open_positions()` may still read from the DB (to enumerate `OPEN` positions), but no writes occur.
5. This is enforced by an early-return guard at the top of the mutation path, not by a downstream check.
6. The Pydantic `ExitResult` is returned to the caller.

### 5.4 structlog Audit Events

| Event Key | Level | When | Key Fields |
|---|---|---|---|
| `exit_engine.evaluating` | `info` | Start of `evaluate_position()` | `position_id`, `condition_id`, `entry_price` |
| `exit_engine.exit_triggered` | `info` | `should_exit=True` | `position_id`, `exit_reason`, `unrealized_edge`, `position_age_hours` |
| `exit_engine.hold` | `info` | `should_exit=False` | `position_id`, `unrealized_edge`, `position_age_hours` |
| `exit_engine.dry_run_exit` | `info` | `should_exit=True` + `dry_run` | `position_id`, `exit_reason`, all result fields |
| `exit_engine.position_closed` | `info` | Live mutation succeeded | `position_id`, `condition_id`, `exit_reason` |
| `exit_engine.mutation_failed` | `error` | `update_status()` failed | `position_id`, `error` |
| `exit_engine.stale_market` | `warning` | Order book unavailable | `position_id`, `token_id` |
| `exit_engine.non_open_position` | `warning` | Status gate rejected | `position_id`, `status` |
| `exit_engine.scan_complete` | `info` | `scan_open_positions()` done | `total`, `exits`, `holds`, `errors` |

### 5.5 Engine Isolation Rule

The `ExitStrategyEngine` module must not:

1. Import or call prompt construction, context-building, or ingestion modules.
2. Import or call evaluation logic (`ClaudeClient`, `GrokClient`).
3. Accept raw `LLMEvaluationResponse` or `MarketContext` objects — only typed `ExitSignal`.
4. Modify `PositionTracker`, `ExecutionRouter`, or `TransactionSigner` state.
5. Submit, sign, or broadcast orders — exit decision only, no execution.
6. Call `PositionRepository.insert_position()` — closing transitions use `update_status()` only.

Allowed imports:
- `src/core/config` (`AppConfig`)
- `src/core/exceptions` (`ExitEvaluationError`, `ExitMutationError`)
- `src/schemas/execution` (`ExitSignal`, `ExitResult`, `ExitReason`, `PositionRecord`, `PositionStatus`)
- `src/db/repositories/position_repo` (`PositionRepository`)
- `src/agents/execution/polymarket_client` (`PolymarketClient`)
- `structlog`, `datetime`, `decimal` (stdlib / logging)

## 6. Invariants Preserved

1. `LLMEvaluationResponse` remains the terminal pre-execution Gatekeeper — `ExitStrategyEngine` operates strictly downstream of validated execution outcomes. No exit path bypasses Gatekeeper authority.
2. `PositionTracker` remains the sole writer of entry-time position records — `ExitStrategyEngine` only reads `PositionRecord` and transitions `status` via `update_status()`.
3. Kelly formula parameters and `ExecutionRouter` sizing logic are unchanged — `ExitStrategyEngine` reads `entry_price`/`order_size_usdc` as immutable position metadata, never recalculates.
4. `Decimal` financial-integrity rules remain mandatory for all exit evaluation fields. No `float` in any pricing, threshold, or edge computation.
5. Async 4-layer queue topology remains unchanged — `ExitStrategyEngine` lives within Layer 4 (Execution).
6. `dry_run=True` continues to block all Layer 4 DB writes; exit decisions are logged but never persisted as state transitions.
7. Repository pattern is preserved — `ExitStrategyEngine` delegates all position state mutations to `PositionRepository.update_status()`.
8. `PolymarketClient`, `BankrollSyncProvider`, `TransactionSigner`, and `ExecutionRouter` internals are unmodified — zero coupling beyond reading market snapshots.
9. `PositionRecord` and `PositionStatus` schemas are unmodified — WI-19 consumes them as read-only inputs.
10. No order broadcast or CLOB submission capability is introduced — exit is a decision layer, not an execution layer.

## 7. Strict Acceptance Criteria (Maker Agent)

1. `ExitStrategyEngine` is the canonical exit-evaluation class in `src/agents/execution/exit_strategy_engine.py`.
2. `evaluate_position(signal: ExitSignal) -> ExitResult` is the primary public async method for single-position evaluation.
3. `scan_open_positions() -> list[ExitResult]` is the public async method for batch evaluation of all `OPEN` positions.
4. `ExitReason` enum exists in `src/schemas/execution.py` with values `NO_EDGE`, `STOP_LOSS`, `TIME_DECAY`, `TAKE_PROFIT`, `STALE_MARKET`, `ERROR`.
5. `ExitSignal` Pydantic model exists with fields `position`, `current_midpoint`, `current_best_bid`, `evaluated_at_utc` — frozen, Decimal-validated.
6. `ExitResult` Pydantic model exists with fields `position_id`, `condition_id`, `should_exit`, `exit_reason`, `entry_price`, `current_midpoint`, `current_best_bid`, `position_age_hours`, `unrealized_edge`, `evaluated_at_utc` — frozen, Decimal-validated.
7. `float` values are rejected by `ExitSignal` and `ExitResult` field validators for all financial fields.
8. Stop-loss triggers when `unrealized_edge <= -config.exit_stop_loss_drop`.
9. Time-decay triggers when `position_age_hours >= config.exit_position_max_age_hours`.
10. No-edge triggers when `unrealized_edge <= Decimal("0")` (and stop-loss not triggered).
11. Take-profit triggers when `unrealized_edge >= config.exit_take_profit_gain`.
12. Priority ordering: `STOP_LOSS > TIME_DECAY > NO_EDGE > TAKE_PROFIT`.
13. `should_exit=False` when no exit criterion is triggered (conservative hold).
14. `dry_run=True` produces zero DB writes and zero state mutations; only structured log output.
15. `dry_run=False` + `should_exit=True` calls `PositionRepository.update_status(position_id, new_status=CLOSED)`.
16. `dry_run=False` + `should_exit=False` makes no repository mutation call (position remains `OPEN`).
17. Non-`OPEN` positions return `ExitResult(should_exit=False, exit_reason=ERROR)`.
18. `scan_open_positions()` fetches fresh order-book data for each `OPEN` position.
19. `scan_open_positions()` produces `STALE_MARKET` exit when order-book fetch returns `None`.
20. `ExitStrategyEngine` is constructed in `Orchestrator.__init__()`.
21. `scan_open_positions()` or `evaluate_position()` is called after `PositionTracker.record_execution()` in the execution consumer loop.
22. An exit evaluation failure does not prevent the broadcast path from proceeding.
23. `ExitStrategyEngine` has zero imports from prompt, context, evaluation, or ingestion modules.
24. `AppConfig` gains `exit_position_max_age_hours: Decimal` (default `48`), `exit_stop_loss_drop: Decimal` (default `0.15`), `exit_take_profit_gain: Decimal` (default `0.20`).
25. New exceptions `ExitEvaluationError` and `ExitMutationError` in `src/core/exceptions.py`.
26. All nine structlog audit events from §5.4 are emitted at the correct log level.
27. Full regression remains green (`pytest --asyncio-mode=auto tests/`) with coverage >= 80%.

## 8. Verification Checklist

1. Unit test: `ExitSignal` rejects `float` for `current_midpoint` and `current_best_bid` (parametrized).
2. Unit test: `ExitSignal` accepts `Decimal` for all financial fields.
3. Unit test: `ExitResult` rejects `float` for all five financial fields (parametrized).
4. Unit test: `ExitResult` accepts `Decimal` for all financial fields.
5. Unit test: `ExitResult` model is frozen — assignment raises `ValidationError`.
6. Unit test: position age is correctly calculated as `Decimal` hours from `routed_at_utc` to `evaluated_at_utc`.
7. Unit test: stop-loss triggers when `unrealized_edge <= -exit_stop_loss_drop` (e.g., entry=0.65, midpoint=0.48, drop=0.15 → triggered).
8. Unit test: stop-loss does NOT trigger when `unrealized_edge > -exit_stop_loss_drop` (e.g., entry=0.65, midpoint=0.55 → not triggered).
9. Unit test: time-decay triggers when `position_age_hours >= exit_position_max_age_hours`.
10. Unit test: time-decay does NOT trigger when `position_age_hours < exit_position_max_age_hours`.
11. Unit test: no-edge triggers when `current_midpoint <= entry_price` (edge evaporated).
12. Unit test: take-profit triggers when `unrealized_edge >= exit_take_profit_gain`.
13. Unit test: `should_exit=False` when no criterion is met (midpoint slightly above entry, young position).
14. Unit test: priority ordering — stop-loss + time-decay both triggered → reason is `STOP_LOSS`.
15. Unit test: priority ordering — time-decay + no-edge both triggered → reason is `TIME_DECAY`.
16. Unit test: non-`OPEN` position (CLOSED) returns `ExitResult(should_exit=False, exit_reason=ERROR)`.
17. Unit test: non-`OPEN` position (FAILED) returns `ExitResult(should_exit=False, exit_reason=ERROR)`.
18. Unit test: `dry_run=True` path does not open DB session for mutation.
19. Unit test: `dry_run=True` path emits `exit_engine.dry_run_exit` structured log with all result fields.
20. Unit test: `dry_run=False` + `should_exit=True` calls `update_status(position_id, new_status=CLOSED)`.
21. Unit test: `dry_run=False` + `should_exit=False` makes no `update_status()` call.
22. Unit test: `unrealized_edge` is computed correctly for profitable and underwater positions.
23. Integration test: `scan_open_positions()` calls `PositionRepository.get_open_positions()` via real async SQLite.
24. Integration test: `scan_open_positions()` fetches order book for each open position.
25. Integration test: `scan_open_positions()` produces `STALE_MARKET` exit when `fetch_order_book()` returns `None`.
26. Integration test: full flow — `OPEN` position, favorable midpoint, young age → `should_exit=False`, position remains `OPEN`.
27. Integration test: full flow — `OPEN` position, stop-loss breached → `should_exit=True`, status transitions to `CLOSED`.
28. Integration test: full flow — `OPEN` position, age >= threshold → `should_exit=True`, status transitions to `CLOSED`.
29. Integration test: `ExitStrategyEngine` module has no dependency on context/evaluation/ingestion modules (import boundary check).
30. Full suite:
    - `pytest --asyncio-mode=auto tests/`
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m`
