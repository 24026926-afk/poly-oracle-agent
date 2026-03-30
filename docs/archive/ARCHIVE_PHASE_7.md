# ARCHIVE_PHASE_7.md — Exit Path Decoupling Phase (Completed 2026-03-30)

**Phase Status:** ✅ **COMPLETE**  
**Version:** 0.8.0  
**Test Coverage:** 362 tests passing, 93% coverage  
**Merged Target:** `develop`

---

## Phase 7 Summary

Phase 7 completed the downstream exit lifecycle by decoupling periodic exit scanning from entry execution flow, routing actionable exits into SELL-side orders, and persisting realized settlement accounting for closed positions.

Delivered in dependency order:
1. **WI-22 — Periodic Exit Scan** (`ExitScanTask` in orchestrator)
2. **WI-20 — Exit Order Router** (`ExitOrderRouter`)
3. **WI-21 — Realized PnL & Settlement** (`PnLCalculator`)

The phase preserved the queue topology and Gatekeeper authority while extending Layer 4 with fail-open exit orchestration and accounting.

---

## Completed Work Items

### WI-22: Periodic Exit Scan
**Status:** COMPLETE

**Deliverables:**
- Added `Orchestrator._exit_scan_loop()` as independent async task
- Added `AppConfig.exit_scan_interval_seconds: Decimal = Decimal("60")`
- Removed inline `scan_open_positions()` from `_execution_consumer_loop()`
- Added structured logs: `exit_scan_loop.completed`, `exit_scan_loop.error`

**Outcome:**
- Exit scanning now runs on a fixed cadence without blocking execution queue consumption.

### WI-20: Exit Order Router
**Status:** COMPLETE

**Deliverables:**
- Added `ExitOrderRouter` in `src/agents/execution/exit_order_router.py`
- Added `ExitOrderAction` and `ExitOrderResult` contracts
- Added `ExitRoutingError` exception and `exit_min_bid_tolerance` config
- Implemented SELL-only routing with Decimal-only sizing and dry-run routing path

**Outcome:**
- `ExitResult(should_exit=True)` now produces typed SELL routing artifacts (`SELL_ROUTED | DRY_RUN | FAILED | SKIP`) with fail-open loop behavior.

### WI-21: Realized PnL & Settlement
**Status:** COMPLETE

**Deliverables:**
- Added `PnLCalculator` in `src/agents/execution/pnl_calculator.py`
- Added frozen `PnLRecord` schema with float-rejecting Decimal validators
- Added `PnLCalculationError` to exception hierarchy
- Extended `PositionRecord` with settlement fields: `realized_pnl`, `exit_price`, `closed_at_utc`
- Extended `Position` ORM with nullable settlement columns
- Added Alembic migration `0003_add_pnl_columns.py` (parent `0002`)
- Added additive repository method `PositionRepository.record_settlement()` with idempotency guard
- Wired orchestrator to settle PnL after exit routing for `SELL_ROUTED`/`DRY_RUN` with non-null `exit_price`

**Outcome:**
- Closed positions now have auditable realized settlement fields persisted through repository-only DB access.

---

## Architecture Snapshot After Phase 7

```text
Layer 1: Ingestion
  CLOBWebSocketClient + GammaRESTClient + MarketDiscoveryEngine

Layer 2: Context
  DataAggregator + PromptFactory

Layer 3: Evaluation
  ClaudeClient + LLMEvaluationResponse Gatekeeper

Layer 4: Execution
  Entry Path:
    BankrollSyncProvider -> ExecutionRouter -> PositionTracker
    -> TransactionSigner -> NonceManager -> GasEstimator -> OrderBroadcaster
  Exit Path:
    ExitStrategyEngine -> ExitOrderRouter -> PnLCalculator -> OrderBroadcaster
```

Queue topology unchanged:
- `market_queue -> prompt_queue -> execution_queue`
- Exit lifecycle runs in `ExitScanTask` (no new queue introduced)

---

## MAAP Audit Findings & Clearance Summary

Phase-7 WI scope was checked against PRD/business-logic constraints with focus on:
- Decimal integrity in money-path logic
- Gatekeeper boundary preservation
- Repository-only DB mutation paths
- dry-run safety
- fail-open exit loop semantics

**Findings requiring fixes during implementation:**
1. None in core logic after WI-21 final patch set.

**Cleared categories:**
- Decimal violations: **CLEARED**
- Gatekeeper bypasses: **CLEARED**
- DB write isolation: **CLEARED**
- dry_run violations: **CLEARED**
- Alembic chain/type correctness: **CLEARED**
- Blocking upstream path on PnL failure: **CLEARED**
- Module isolation violations: **CLEARED**
- Regression against frozen components: **CLEARED**

---

## Critical Invariants Preserved

1. **Decimal-only financial integrity** for exit routing and PnL settlement paths.
2. **`LLMEvaluationResponse` terminal Gatekeeper** remains unchanged and authoritative.
3. **`dry_run=True` blocks side effects** (signing, broadcast, settlement DB writes).
4. **Repository pattern enforced** (`PositionRepository.record_settlement()` is sole settlement write path).
5. **Position status ownership preserved**:
   - `ExitStrategyEngine`: `OPEN -> CLOSED`
   - `PnLCalculator`: settlement fields only, no status mutation
6. **Fail-open exit scan behavior** preserved for routing and settlement errors.
7. **Async architecture and queue topology unchanged**.

---

## Final Metrics

- `pytest --asyncio-mode=auto tests/ -q` → **362 passed**
- `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` → **93%**

---

## Phase 7 Status

✅ **SEALED**  
**Date:** 2026-03-30
