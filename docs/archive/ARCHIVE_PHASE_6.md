# ARCHIVE_PHASE_6.md — Position Lifecycle Phase (Completed 2026-03-30)

**Phase Status:** ✅ **COMPLETE**
**Version:** 0.7.0
**Test Coverage:** 295 tests passing, 92% coverage
**Merged To:** `develop`

---

## Phase 6 Summary

Phase 6 closed the position lifecycle gap by introducing persistent position tracking after execution routing and rule-based exit evaluation against live market data. After Phase 5, validated BUY decisions were routed into signed order payloads but execution outcomes were ephemeral — once a position was opened, no component re-evaluated it. Phase 6 added two capabilities:

1. **Position persistence** — every routed execution outcome (EXECUTED, DRY_RUN, FAILED) is recorded as a typed `PositionRecord` in the `positions` table with full financial audit fields.
2. **Exit evaluation** — open positions are scanned against fresh order-book data and evaluated against conservative, rule-based exit criteria (stop-loss, time-decay, no-edge, take-profit, stale-market).

---

## Completed Work Items

### WI-17: Position Tracker
**Status:** COMPLETE (2026-03-29)

**Objective:** Persist execution outcomes as typed position records for lifecycle tracking.

**Deliverables:**
- `PositionTracker` in `src/agents/execution/position_tracker.py`
- `PositionRecord` Pydantic model and `PositionStatus` enum (`OPEN | CLOSED | FAILED`) in `src/schemas/position.py`, re-exported from `src/schemas/execution.py`
- `Position` SQLAlchemy ORM model with `Numeric(38,18)` for all 5 financial columns and 3 indexes
- `PositionRepository` async CRUD in `src/db/repositories/position_repository.py` (5 methods)
- Alembic migration `0002_add_positions_table.py`

**Key Outcomes:**
- `record_execution(result, condition_id, token_id) -> PositionRecord | None` is the sole public entry point
- SKIP → `None`, EXECUTED/DRY_RUN → `OPEN`, FAILED → `FAILED` with `Decimal("0")` sentinels for None financials
- `dry_run=True` logs full record via structlog with zero DB writes and zero session creation
- Unreachable state guards: `EXECUTED+dry_run` and `DRY_RUN+live` log error and return `None`
- Orchestrator wiring: constructed in `__init__()`, called in `_execution_consumer_loop()` before dry_run gate
- MAAP audit caught 2 orchestrator wiring defects (token_id field, dry_run bypass) — both fixed and cleared
- 27 new tests (unit + integration), 257 total at WI-17 completion

### WI-19: Exit Strategy Engine
**Status:** COMPLETE (2026-03-29)

**Objective:** Evaluate open positions against typed exit criteria and determine hold-or-close decisions.

**Deliverables:**
- `ExitStrategyEngine` in `src/agents/execution/exit_strategy_engine.py`
- `ExitReason` enum (`NO_EDGE | STOP_LOSS | TIME_DECAY | TAKE_PROFIT | STALE_MARKET | ERROR`) in `src/schemas/execution.py`
- `ExitSignal` frozen Pydantic model with `field_validator` rejecting `float` on financial fields
- `ExitResult` frozen Pydantic model with `field_validator` rejecting `float` on all 5 financial fields
- `ExitEvaluationError` and `ExitMutationError` in `src/core/exceptions.py`
- New `AppConfig` fields: `exit_position_max_age_hours` (48h), `exit_stop_loss_drop` (0.15), `exit_take_profit_gain` (0.20)

**Key Outcomes:**
- Two public async methods: `evaluate_position(signal) -> ExitResult` and `scan_open_positions() -> list[ExitResult]`
- Status gate: non-OPEN positions return `ExitResult(should_exit=False, exit_reason=ERROR)`
- Exit criteria (all Decimal comparisons): stop-loss, time-decay, no-edge, take-profit
- Priority ordering: `STOP_LOSS > TIME_DECAY > NO_EDGE > TAKE_PROFIT` — deterministic when multiple criteria trigger
- Conservative hold-by-default: `should_exit=False` when no criterion is met
- `dry_run=True` early-return guard fires BEFORE any session/repository mutation; zero DB writes
- `dry_run=True` does NOT block read-path DB access in `scan_open_positions()`
- `scan_open_positions()` produces `STALE_MARKET` when `fetch_order_book()` returns `None`
- Module isolation: zero imports from context, evaluation, or ingestion modules
- Orchestrator wiring: constructed in `__init__()`, `scan_open_positions()` called in `_execution_consumer_loop()` after `PositionTracker.record_execution()` — failure does NOT block broadcast path
- 38 new tests (unit + integration), 295 total at WI-19 completion

---

## Key Architectural Decisions Made

1. **Position tracking was placed between execution routing and broadcasting, not after broadcasting**
   - `PositionTracker.record_execution()` captures the routing outcome immediately, before the dry-run gate and broadcast path. This ensures every routed decision has an auditable position record regardless of whether it is broadcast.

2. **Exit evaluation is downstream-only and never creates or modifies routing**
   - `ExitStrategyEngine` reads `PositionRecord` and market snapshots, evaluates exit criteria, and transitions `OPEN → CLOSED`. It never creates positions, recalculates Kelly sizing, submits exit orders, or modifies routing decisions.

3. **Exit criteria are rule-based, not LLM-driven**
   - WI-19 uses deterministic Decimal comparisons against configured thresholds. No LLM reasoning is involved in exit decisions. This preserves auditability and avoids adding latency or cost to the lifecycle loop.

4. **Stale-market handling was added as a first-class exit reason**
   - When `fetch_order_book()` returns `None` for an open position, the engine produces `STALE_MARKET` instead of silently skipping. This ensures positions in illiquid or delisted markets are surfaced.

5. **Phase 6 preserved the existing queue topology and Gatekeeper authority**
   - `LLMEvaluationResponse` remains the terminal pre-execution Gatekeeper. Position tracking and exit evaluation operate strictly downstream of validated execution outcomes.

---

## Pipeline Architecture After Phase 6

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

Queue topology unchanged:
- `market_queue -> prompt_queue -> execution_queue`
- Position tracking and exit evaluation are synchronous steps within the execution consumer loop, not separate queue consumers.

---

## MAAP Audit Findings & Fixes

### WI-17 Finding 1: Orchestrator token_id Wiring Defect
**Issue:** Initial orchestrator wiring passed `condition_id` where `token_id` was required by `PositionTracker.record_execution()`.

**Fix:** Corrected to pass `token_id` from the execution result context.

**Why it mattered:** Position records would have been created with the wrong token identifier, breaking downstream order-book lookups in the exit engine.

### WI-17 Finding 2: dry_run Bypass in Consumer Loop
**Issue:** `PositionTracker.record_execution()` was placed after the dry_run gate in the consumer loop, so dry-run positions were never recorded.

**Fix:** Moved the call before the dry_run gate so both live and dry-run execution outcomes are tracked.

**Why it mattered:** dry_run mode is the primary development and testing mode. Without position records, the exit engine would have nothing to evaluate.

### Phase-Wide Audit Themes Cleared
- **Decimal violations:** CLEARED — all financial fields in `PositionRecord`, `ExitSignal`, and `ExitResult` are Decimal with float-rejecting validators
- **Gatekeeper bypasses:** CLEARED — no execution-eligible path bypasses `LLMEvaluationResponse`
- **Business logic drift:** CLEARED — WI-19 scope is strictly evaluate-and-transition, no exit order submission or PnL accounting
- **dry_run safety violations:** CLEARED — early-return guard blocks all DB mutations when `dry_run=True`
- **Isolation violations:** CLEARED — `ExitStrategyEngine` has zero imports from context/evaluation/ingestion
- **Repository pattern violations:** CLEARED — all DB access through `PositionRepository`
- **Position state violations:** CLEARED — only `OPEN → CLOSED` transitions, never writes `OPEN` or `FAILED`

---

## Invariants Established / Preserved

1. **Decimal-only financial math**
   - Position records, exit signals, exit results, age calculations, and edge comparisons all remain Decimal-safe. Float is rejected at Pydantic boundary.

2. **Pydantic Gatekeeper remains terminal**
   - `PositionTracker` and `ExitStrategyEngine` operate downstream of validated execution outcomes. No execution-eligible path bypasses Gatekeeper validation.

3. **dry_run blocks execution side effects**
   - `PositionTracker`: logs full record, zero DB writes when `dry_run=True`
   - `ExitStrategyEngine`: early-return before any session/mutation when `dry_run=True`; read-path access permitted for `scan_open_positions()`

4. **Repository pattern for all DB access**
   - `PositionRepository` is the sole mutation path for the `positions` table
   - `ExitStrategyEngine` only calls `update_status()` — never `insert_position()` or raw SQL

5. **Conservative hold-by-default**
   - Positions remain `OPEN` unless at least one exit criterion is met. Priority ordering is deterministic.

6. **Module isolation preserved**
   - `PositionTracker` and `ExitStrategyEngine` have zero imports from context, prompt, evaluation, or ingestion modules.

7. **Async architecture remains intact**
   - Phase 6 added no blocking execution path. Position tracking and exit evaluation are async operations within the existing consumer loop.

---

## Database Schema Changes

### Migration: `0002_add_positions_table.py`
- New `positions` table with 15 columns
- 5 financial columns use `Numeric(38,18)` for Decimal precision
- 3 indexes: `ix_positions_status`, `ix_positions_condition_id`, `ix_positions_routed_at_utc`
- Parent migration: `0001_initial_schema`
- No modifications to existing tables (`market_snapshots`, `agent_decision_logs`, `execution_txs`)

---

## Final Metrics

- **Total Tests:** 295
- **Passing:** 295/295 ✅
- **Coverage:** 92% ✅
- **Regression Gate:** `pytest --asyncio-mode=auto tests/ -q` green

---

## Next Phase (Phase 7)

**Potential scope:** Exit order routing (submitting CLOB exit orders for positions marked CLOSED), realized PnL accounting, portfolio-level exposure aggregation, and LLM-assisted exit reasoning. Detailed scope to be finalized in the Phase 7 PRD.

---

## Phase 6 Status

✅ **SEALED**
**Date:** 2026-03-30
