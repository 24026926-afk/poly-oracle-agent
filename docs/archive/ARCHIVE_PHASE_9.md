# ARCHIVE_PHASE_9.md — Operator Safety & Telemetry Phase (Completed 2026-04-03)

**Phase Status:** ✅ **COMPLETE**  
**Version:** 0.9.2  
**Test Coverage:** 549 tests passing, 95% coverage  
**Merged Target:** `develop`

---

## Phase 9 Summary

Phase 9 completed the operator-safety layer by adding real-time telemetry, a portfolio-level entry gate, and fee-aware settlement accounting.

Delivered in dependency order:
1. **WI-26 — Telegram Telemetry Sink** (`TelegramNotifier`)
2. **WI-27 — Global Circuit Breaker** (`CircuitBreaker`)
3. **WI-28 — Net PnL & Fee Accounting** (`PnLCalculator` extension + lifecycle analytics upgrade)

The phase preserved queue topology, Gatekeeper authority, repository isolation, and the `dry_run` safety model while extending Layer 4 with operator-facing observability and more accurate financial reporting.

---

## Completed Work Items

### WI-26: Telegram Telemetry Sink
**Status:** COMPLETE

**Deliverables:**
- Added `TelegramNotifier` in `src/agents/execution/telegram_notifier.py`
- Added Telegram config fields and orchestrator lifecycle management
- Wired alert, BUY-routing, and SELL-routing message sends into existing loops

**Outcome:**
- Operators now receive real-time text telemetry without SSH or log tailing.
- Telegram delivery remains fail-open and never blocks orchestration.

### WI-27: Global Circuit Breaker
**Status:** COMPLETE

**Deliverables:**
- Added `CircuitBreaker` and `CircuitBreakerState`
- Added config-gated orchestrator construction and entry-path BUY gating
- Wired CRITICAL drawdown alerts to breaker state transitions

**Outcome:**
- New BUY routing halts immediately on critical drawdown alerts.
- Exit scanning, SELL routing, and settlement remain fully operational.

### WI-28: Net PnL & Fee Accounting
**Status:** COMPLETE

**Deliverables:**
- Added Alembic migration `0004_add_fee_columns.py`
- Added nullable `gas_cost_usdc` and `fees_usdc` to the `positions` table and ORM model
- Extended `PositionRecord`, `PnLRecord`, `PositionLifecycleEntry`, and `LifecycleReport`
- Extended `PnLCalculator.settle()` and `PositionRepository.record_settlement()`
- Extended `PositionLifecycleReporter` with fee-aware per-entry and aggregate net-PnL reporting

**Outcome:**
- Realized settlement accounting now distinguishes gross PnL (`realized_pnl`) from fee-adjusted net PnL (`net_realized_pnl`).
- Historical rows remain valid through `NULL -> Decimal("0")` normalization.
- Lifecycle reporting now exposes explicit `total_gas_cost_usdc`, `total_fees_usdc`, and `total_net_realized_pnl` audit totals.

---

## Architecture Snapshot After Phase 9

```text
Layer 1: Ingestion
  CLOBWebSocketClient + GammaRESTClient + MarketDiscoveryEngine

Layer 2: Context
  DataAggregator + PromptFactory

Layer 3: Evaluation
  ClaudeClient + LLMEvaluationResponse Gatekeeper

Layer 4: Execution
  Entry Path:
    CircuitBreaker -> ExecutionRouter -> PositionTracker
    -> TransactionSigner -> NonceManager -> GasEstimator -> OrderBroadcaster

  Exit Path:
    ExitStrategyEngine -> ExitOrderRouter -> PnLCalculator -> OrderBroadcaster

  Analytics / Telemetry Path:
    PortfolioAggregator -> PositionLifecycleReporter -> AlertEngine
    -> CircuitBreaker.evaluate_alerts()
    -> TelegramNotifier
```

Queue topology remained unchanged:
- `market_queue -> prompt_queue -> execution_queue`
- no new queue was introduced in Phase 9

---

## MAAP Audit Findings & Clearance Summary

Phase-9 core logic was checked against the PRD, business logic, and archival invariants with focus on Decimal integrity, Gatekeeper preservation, and exit-path independence.

**Notable findings cleared during implementation:**
1. **WI-28 persisted precision drift:** live `PnLRecord` values could diverge from lifecycle-report values if the return contract used pre-persistence precision while the report used persisted `Numeric(38,18)` values. Fixed by aligning the live return path with the refreshed persisted row.
2. **WI-28 legacy fee-null compatibility:** pre-WI-28 `NULL` fee values needed explicit `Decimal("0")` normalization in reporting and schema boundaries to preserve backward-compatible net-PnL identity.

**Cleared MAAP categories:**
- Decimal violations: **CLEARED**
- Gatekeeper bypasses: **CLEARED**
- Business logic drift: **CLEARED**
- Repository-pattern violations: **CLEARED**
- `dry_run` safety violations: **CLEARED**
- Exit-path gating regressions: **CLEARED**

---

## Critical Invariants Preserved

1. **`LLMEvaluationResponse` remains the terminal Gatekeeper** before execution routing.
2. **All financial math remains Decimal-native** across gas, fee, gross PnL, and net PnL paths.
3. **`dry_run=True` still blocks signing, broadcasting, and settlement DB writes.**
4. **Repository-only DB mutation remains enforced** through `PositionRepository`.
5. **Exit-path independence remains sacrosanct:** the circuit breaker gates BUY entry only.
6. **Historical compatibility is preserved:** legacy positions with `NULL` fee columns continue to report `net_realized_pnl == realized_pnl`.

---

## Final Metrics

- `.venv/bin/pytest --asyncio-mode=auto tests/` → **549 passed**
- `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` → **95%**

---

## Phase 9 Status

✅ **SEALED**  
**Date:** 2026-04-03
