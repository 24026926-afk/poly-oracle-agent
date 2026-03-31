# P24-WI-24 — Position Lifecycle Reporter Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-4.1 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi24-position-lifecycle-reporter` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/risk-auditor.md`
- `.agents/rules/db-engineer.md`
- `.agents/rules/async-architect.md`
- `.agents/rules/security-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-24 for Phase 8: a read-only reporting component (`PositionLifecycleReporter`) that produces structured performance summaries over all positions — both closed (settled) and open. The reporter reads position data from `PositionRepository`, computes aggregate statistics (total realized PnL, win/loss/breakeven counts, average hold duration, best/worst PnL), constructs per-position `PositionLifecycleEntry` records, and returns a typed `LifecycleReport`.

This WI is **read-only analytics**. It must aggregate settled-position metrics using Decimal-only arithmetic, support optional date filtering on `routed_at_utc`, and be invokable on-demand (not as a periodic background task). It must not write to the database, mutate position state, influence routing or exit decisions, or touch any upstream component.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi24.md`
4. `docs/PRD-v8.0.md` (Phase 8 / WI-24 section)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/orchestrator.py` — **integration target; `PositionLifecycleReporter` constructed in `__init__()`, invoked within `_portfolio_aggregation_loop()` after `compute_snapshot()`**
9. `src/db/repositories/position_repository.py` (dependency: add `get_all_positions()`, `get_settled_positions()`, `get_positions_by_status()`)
10. `src/db/models.py` (context: `Position` ORM model — read-only access)
11. `src/schemas/risk.py` (context: `PortfolioSnapshot` from WI-23 — add `PositionLifecycleEntry` and `LifecycleReport`)
12. `src/schemas/position.py` (context: `PositionRecord`, `PositionStatus` — frozen, no modifications)
13. `src/schemas/execution.py` (context boundary: frozen, no modifications)
14. `src/core/config.py`
15. `src/agents/execution/portfolio_aggregator.py` (context: upstream WI-23 component)
16. Existing tests:
    - `tests/unit/test_portfolio_aggregator.py`
    - `tests/integration/test_portfolio_aggregator_integration.py`
    - `tests/unit/test_exit_scan_loop.py`
    - `tests/unit/test_pnl_calculator.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-24 test files first:
   - `tests/unit/test_lifecycle_reporter.py`
   - `tests/integration/test_lifecycle_reporter_integration.py`
2. Write failing tests for all required behaviors:
   - `PositionLifecycleEntry` Pydantic model exists in `src/schemas/risk.py`, is frozen, with Decimal-validated fields.
   - `PositionLifecycleEntry` rejects `float` in `entry_price` at Pydantic boundary.
   - `PositionLifecycleEntry` rejects `float` in `size_tokens` at Pydantic boundary.
   - `PositionLifecycleEntry` rejects `float` in nullable fields (`exit_price`, `realized_pnl`) at Pydantic boundary.
   - `PositionLifecycleEntry` accepts `None` for `exit_price`, `realized_pnl`, `settled_at_utc` (OPEN positions).
   - `LifecycleReport` Pydantic model exists in `src/schemas/risk.py`, is frozen, with Decimal-validated fields.
   - `LifecycleReport` rejects `float` in `total_realized_pnl` at Pydantic boundary.
   - `LifecycleReport` rejects `float` in `avg_hold_duration_hours` at Pydantic boundary.
   - `LifecycleReport` rejects `float` in `best_pnl` at Pydantic boundary.
   - `LifecycleReport` rejects `float` in `worst_pnl` at Pydantic boundary.
   - `PositionLifecycleReporter` exists in `src/agents/execution/lifecycle_reporter.py` with constructor accepting `config: AppConfig` and `db_session_factory: async_sessionmaker[AsyncSession]`.
   - `generate_report(start_date=None, end_date=None) -> LifecycleReport` is the sole public async method.
   - `generate_report()` with zero positions returns a zero-valued `LifecycleReport` with empty `entries`.
   - `generate_report()` with one settled position returns correct aggregates (known inputs, verify exact Decimal values).
   - `generate_report()` with multiple settled positions aggregates correctly (verify summation, win/loss/breakeven classification).
   - `generate_report()` correctly classifies winning (`realized_pnl > 0`), losing (`realized_pnl < 0`), and breakeven (`realized_pnl == 0`) positions.
   - `generate_report()` computes `avg_hold_duration_hours` correctly from `closed_at_utc - routed_at_utc`.
   - `generate_report()` identifies `best_pnl` (max) and `worst_pnl` (min) across settled positions.
   - `generate_report()` with `entry_price == Decimal("0")` — `size_tokens == Decimal("0")`, no division-by-zero error.
   - `generate_report()` with OPEN positions — includes them in `entries` with `None` for `exit_price`, `realized_pnl`, `settled_at_utc`.
   - `generate_report()` with `start_date` filter — only positions with `routed_at_utc >= start_date` are included.
   - `generate_report()` with `end_date` filter — only positions with `routed_at_utc <= end_date` are included.
   - `generate_report()` with invalid date filter (start > end) — fail-open: log warning and return all positions (no exception).
   - `lifecycle.report_generated` structlog event emitted with correct fields after successful report.
   - `lifecycle.report_empty` structlog event emitted when no positions exist.
   - `PositionRepository.get_all_positions()` returns all positions regardless of status.
   - `PositionRepository.get_settled_positions()` returns only CLOSED positions with non-null `realized_pnl`.
   - `PositionLifecycleReporter` has zero imports from prompt, context, evaluation, or ingestion modules.
   - `PositionLifecycleReporter` performs zero DB writes.
   - Orchestrator constructs `PositionLifecycleReporter` in `__init__()`.
   - Orchestrator invokes `generate_report()` within `_portfolio_aggregation_loop()` after `compute_snapshot()`.
3. Run RED tests:
   - `pytest tests/unit/test_lifecycle_reporter.py -v`
   - `pytest tests/integration/test_lifecycle_reporter_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add Repository Methods to `PositionRepository`

Target:
- `src/db/repositories/position_repository.py`

Requirements:
1. Add `get_all_positions() -> list[Position]` — reads all positions regardless of status.
   ```python
   async def get_all_positions(self) -> list[Position]:
       stmt = select(Position)
       result = await self._session.execute(stmt)
       return list(result.scalars().all())
   ```
2. Add `get_settled_positions() -> list[Position]` — reads CLOSED positions where `realized_pnl IS NOT NULL`.
   ```python
   async def get_settled_positions(self) -> list[Position]:
       stmt = select(Position).where(
           Position.status == "CLOSED",
           Position.realized_pnl.isnot(None),
       )
       result = await self._session.execute(stmt)
       return list(result.scalars().all())
   ```
3. Add `get_positions_by_status(status: str) -> list[Position]` — reads positions filtered by status string.
   ```python
   async def get_positions_by_status(self, status: str) -> list[Position]:
       stmt = select(Position).where(Position.status == status)
       result = await self._session.execute(stmt)
       return list(result.scalars().all())
   ```
4. These methods are additive. Do NOT modify any existing repository method.

### Step 2 — Add `PositionLifecycleEntry` and `LifecycleReport` to `src/schemas/risk.py`

Target:
- `src/schemas/risk.py` (existing file — append below `PortfolioSnapshot`)

Requirements:
1. `PositionLifecycleEntry` is a frozen Pydantic `BaseModel` with fields:
   - `position_id: str`
   - `slug: str` (condition_id used as slug identifier)
   - `entry_price: Decimal`
   - `exit_price: Decimal | None` (None for OPEN positions)
   - `size_tokens: Decimal` (order_size_usdc / entry_price)
   - `realized_pnl: Decimal | None` (None for OPEN positions)
   - `status: str` ("OPEN" or "CLOSED")
   - `opened_at_utc: datetime`
   - `settled_at_utc: datetime | None` (None for OPEN positions)
2. Add `_reject_float_financials` validator for `entry_price`, `size_tokens`.
3. Add `_reject_float_nullable_financials` validator for `exit_price`, `realized_pnl` (allows `None`).
4. `model_config = {"frozen": True}`.
5. `LifecycleReport` is a frozen Pydantic `BaseModel` with fields:
   - `report_at_utc: datetime`
   - `total_settled_count: int`
   - `winning_count: int`
   - `losing_count: int`
   - `breakeven_count: int`
   - `total_realized_pnl: Decimal`
   - `avg_hold_duration_hours: Decimal`
   - `best_pnl: Decimal`
   - `worst_pnl: Decimal`
   - `entries: list[PositionLifecycleEntry]`
   - `dry_run: bool`
6. Add `_reject_float_financials` validator for `total_realized_pnl`, `avg_hold_duration_hours`, `best_pnl`, `worst_pnl`.
7. `model_config = {"frozen": True}`.
8. Invariant: `winning_count + losing_count + breakeven_count == total_settled_count`.

### Step 3 — Create `PositionLifecycleReporter` Module

Target:
- `src/agents/execution/lifecycle_reporter.py` (new)

Requirements:
1. New class `PositionLifecycleReporter` with constructor accepting:
   - `config: AppConfig`
   - `db_session_factory: async_sessionmaker[AsyncSession]`
2. No `PolymarketClient` dependency — this is a read-only reporting component that requires no live market data.
3. Single public method: `async def generate_report(self, start_date: datetime | None = None, end_date: datetime | None = None) -> LifecycleReport`.
4. **Load positions:** `PositionRepository(session).get_all_positions()` inside an `async with self._db_session_factory() as session` block.
5. **Optional date filtering (fail-open):**
   - If `start_date is not None`: filter to positions where `routed_at_utc >= start_date`.
   - If `end_date is not None`: filter to positions where `routed_at_utc <= end_date`.
   - If date filter is invalid (e.g., `start_date > end_date`), log a warning and return all positions — do NOT raise an exception.
6. **Zero positions:** If no positions remain after filtering, emit `lifecycle.report_empty` and return a zero-valued `LifecycleReport` with empty `entries` list.
7. **Separate settled vs. open:**
   ```python
   settled = [p for p in all_positions if p.status == "CLOSED" and p.realized_pnl is not None]
   open_positions = [p for p in all_positions if p.status == "OPEN"]
   ```
8. **Per-position lifecycle entries (Decimal-only):** For each position (settled + open):
   ```python
   entry_price_d = Decimal(str(position.entry_price))
   order_size_usdc_d = Decimal(str(position.order_size_usdc))

   if entry_price_d == Decimal("0"):
       size_tokens = Decimal("0")
   else:
       size_tokens = order_size_usdc_d / entry_price_d
   ```
   Construct `PositionLifecycleEntry` with:
   - `position_id=str(position.id)`
   - `slug=position.condition_id`
   - `entry_price=entry_price_d`
   - `exit_price=Decimal(str(position.exit_price)) if position.exit_price is not None else None`
   - `size_tokens=size_tokens`
   - `realized_pnl=Decimal(str(position.realized_pnl)) if position.realized_pnl is not None else None`
   - `status=position.status`
   - `opened_at_utc=position.routed_at_utc`
   - `settled_at_utc=position.closed_at_utc`
9. **Aggregate statistics (settled positions only, Decimal-only):**
   - If no settled positions: all aggregates are zero.
   - Otherwise:
     - `total_settled_count = len(settled)`
     - `total_realized_pnl = sum(realized_pnl_values)`
     - Win/loss/breakeven classification: `realized_pnl > 0` win, `< 0` loss, `== 0` breakeven.
     - `avg_hold_duration_hours = total_hold_seconds / total_settled_count / Decimal("3600")` using `Decimal(str((closed_at_utc - routed_at_utc).total_seconds()))`.
     - `best_pnl = max(pnl_values)`, `worst_pnl = min(pnl_values)`.
10. **Build `LifecycleReport`:** Construct with `report_at_utc=datetime.now(timezone.utc)` and `dry_run=self._config.dry_run`.
11. **Log report:** Emit `lifecycle.report_generated` at `info` level with fields: `total_settled_count`, `winning_count`, `losing_count`, `breakeven_count`, `total_realized_pnl`, `avg_hold_duration_hours`, `best_pnl`, `worst_pnl`, `entry_count`, `dry_run`.
12. Return the `LifecycleReport`.
13. All arithmetic is `Decimal`. No `float()` conversion at any step.
14. Structured logging via `structlog` only — no `print()`.
15. Zero imports from:
    - `src/agents/evaluation/*`
    - `src/agents/context/*`
    - `src/agents/ingestion/*`
    - `src/agents/execution/exit_strategy_engine.py`
    - `src/agents/execution/exit_order_router.py`
    - `src/agents/execution/pnl_calculator.py`
    - `src/agents/execution/execution_router.py`
    - `src/agents/execution/order_broadcaster.py`
    - `src/agents/execution/signer.py`
    - `src/agents/execution/bankroll_sync.py`
    - `src/agents/execution/portfolio_aggregator.py`

### Step 4 — Integrate into Orchestrator

Target:
- `src/orchestrator.py`

Requirements:
1. **Constructor wiring:** Construct `PositionLifecycleReporter(config=self.config, db_session_factory=AsyncSessionLocal)` in `Orchestrator.__init__()`, after `self.portfolio_aggregator` construction.
2. **Invoke in `_portfolio_aggregation_loop()`:** After `self.portfolio_aggregator.compute_snapshot()`, call `await self.lifecycle_reporter.generate_report()`. Wrap in its own try/except to ensure a lifecycle reporter failure does not affect the portfolio aggregation loop.
   ```python
   async def _portfolio_aggregation_loop(self) -> None:
       while True:
           await asyncio.sleep(
               float(self.config.portfolio_aggregation_interval_sec)
           )
           try:
               await self.portfolio_aggregator.compute_snapshot()
           except Exception as exc:
               logger.error("portfolio_aggregation_loop.error", error=str(exc))
           try:
               await self.lifecycle_reporter.generate_report()
           except Exception as exc:
               logger.error("lifecycle_report_loop.error", error=str(exc))
   ```
3. **No new task or config gate.** `PositionLifecycleReporter` piggybacks on the existing `_portfolio_aggregation_loop()` — it runs whenever `enable_portfolio_aggregator=True`. No new `enable_lifecycle_reporter` config field.
4. **No new `asyncio.create_task()`.** The reporter is invoked inline within the existing loop.
5. **Exception handling:** `generate_report()` failure is caught independently, logged via `lifecycle_report_loop.error`, and does NOT re-raise or affect the portfolio aggregation loop. Each component fails independently.
6. **Shutdown:** No changes needed. The reporter is invoked inline (not a separate task), so it terminates when the loop's task is cancelled.
7. **Task count unchanged:** When `enable_portfolio_aggregator=True`, `self._tasks` still contains 7 entries (same as after WI-23). When `False`, 6 entries.

### Step 5 — GREEN Validation

Run:
```bash
pytest tests/unit/test_lifecycle_reporter.py -v
pytest tests/integration/test_lifecycle_reporter_integration.py -v
pytest tests/unit/test_portfolio_aggregator.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. **Read-only analytics.** `PositionLifecycleReporter` performs zero DB writes regardless of `dry_run`. No repository write methods (`insert_position`, `update_status`, `record_settlement`) are called. No session `commit()` or `flush()` is issued.
2. **Decimal financial integrity.** All aggregation arithmetic (`size_tokens`, `total_realized_pnl`, `avg_hold_duration_hours`, `best_pnl`, `worst_pnl`) is `Decimal`-only. Float is rejected at Pydantic boundary via `PositionLifecycleEntry` and `LifecycleReport` validators. No float intermediary in any arithmetic step.
3. **Fail-open date filtering.** Invalid date ranges (start > end) never block report generation. The reporter logs a warning and returns all positions. No exception raised.
4. **Division-by-zero guard.** `entry_price == Decimal("0")` yields `size_tokens = Decimal("0")`. No exception raised.
5. **No bypass of `LLMEvaluationResponse` terminal Gatekeeper.** `PositionLifecycleReporter` operates far downstream: it is a passive observer of settled position data.
6. **Module isolation.** Zero imports from prompt, context, evaluation, or ingestion modules. Zero imports from `ExitStrategyEngine`, `ExitOrderRouter`, `PnLCalculator`, `ExecutionRouter`, `OrderBroadcaster`, `TransactionSigner`, `BankrollSyncProvider`, or `PortfolioAggregator`.
7. **Fail-open loop semantics.** A failed `generate_report()` call within the orchestrator loop is caught, logged via `lifecycle_report_loop.error`, and does NOT re-raise or terminate the loop. Independent of `compute_snapshot()` error handling.
8. **No new periodic task.** The reporter is invoked inline within `_portfolio_aggregation_loop()`, not as a separate `asyncio.create_task()`. Task count is unchanged from WI-23.
9. **Shutdown preserved.** No additional shutdown code needed. The reporter terminates when its parent task is cancelled.
10. **No queue topology changes.** `market_queue -> prompt_queue -> execution_queue`. No new queue introduced.
11. **Async pipeline preserved.** WI-24 adds inline reporting within an existing async task. No blocking calls.
12. **No database schema changes.** Zero new tables, zero new columns, zero Alembic migrations. WI-24 reads existing data only.
13. **Additive repository methods only.** `get_all_positions()`, `get_settled_positions()`, and `get_positions_by_status()` are new methods. No existing repository method is modified.
14. **Frozen upstream components.** `PositionTracker`, `PositionRepository` (existing methods), `ExitStrategyEngine`, `ExitOrderRouter`, `PnLCalculator`, `ExecutionRouter`, `PolymarketClient`, `OrderBroadcaster`, `PortfolioAggregator`, and all schemas in `src/schemas/execution.py` and `src/schemas/position.py` are byte-identical before and after WI-24.
15. **`dry_run` behavior is passthrough.** The `dry_run` flag is included in `LifecycleReport` for audit context. No new `dry_run` gate is needed because the component is inherently read-only.
16. **Win/loss/breakeven invariant.** `winning_count + losing_count + breakeven_count == total_settled_count` must hold in every `LifecycleReport`.

---

## Required Test Matrix

At minimum, WI-24 tests must prove:

### Unit Tests — Schema Validation
1. `PositionLifecycleEntry` accepts valid `Decimal` values and is frozen (immutable after construction).
2. `PositionLifecycleEntry` rejects `float` in `entry_price` at Pydantic boundary.
3. `PositionLifecycleEntry` rejects `float` in `size_tokens` at Pydantic boundary.
4. `PositionLifecycleEntry` rejects `float` in `exit_price` at Pydantic boundary.
5. `PositionLifecycleEntry` rejects `float` in `realized_pnl` at Pydantic boundary.
6. `PositionLifecycleEntry` accepts `None` for `exit_price`, `realized_pnl`, `settled_at_utc` (OPEN position shape).
7. `LifecycleReport` accepts `Decimal` in all financial fields and is frozen.
8. `LifecycleReport` rejects `float` in `total_realized_pnl` at Pydantic boundary.
9. `LifecycleReport` rejects `float` in `avg_hold_duration_hours` at Pydantic boundary.
10. `LifecycleReport` rejects `float` in `best_pnl` at Pydantic boundary.
11. `LifecycleReport` rejects `float` in `worst_pnl` at Pydantic boundary.

### Unit Tests — Reporter Logic (Mocked Repository)
12. `generate_report()` with zero positions returns zero-valued `LifecycleReport` with empty `entries`.
13. `generate_report()` with one settled position returns correct `total_realized_pnl`, `winning_count`, `avg_hold_duration_hours`, `best_pnl`, `worst_pnl` (known inputs, verify exact Decimal values).
14. `generate_report()` with multiple settled positions aggregates correctly (verify Decimal summation across 2-3 positions).
15. `generate_report()` correctly classifies winning (`pnl > 0`), losing (`pnl < 0`), and breakeven (`pnl == 0`) positions.
16. `generate_report()` computes `avg_hold_duration_hours` correctly: `Decimal(total_seconds) / count / 3600`.
17. `generate_report()` identifies `best_pnl` (max) and `worst_pnl` (min) across settled positions.
18. `generate_report()` with OPEN positions — includes them in `entries` with `None` for `exit_price`, `realized_pnl`, `settled_at_utc`; they do NOT count toward aggregate stats.
19. `generate_report()` with `entry_price == Decimal("0")` — `size_tokens == Decimal("0")`, no division-by-zero error.
20. `generate_report()` with `start_date` filter — only positions with `routed_at_utc >= start_date` included.
21. `generate_report()` with `end_date` filter — only positions with `routed_at_utc <= end_date` included.
22. `generate_report()` with `start_date` and `end_date` both set — intersection filtering works correctly.
23. `generate_report()` with invalid date range (start > end) — fail-open, logs warning, returns all positions.
24. `lifecycle.report_generated` structlog event emitted with correct fields after successful report.
25. `lifecycle.report_empty` structlog event emitted when no positions exist.

### Unit Tests — Repository Methods
26. `PositionRepository.get_all_positions()` returns all positions regardless of status.
27. `PositionRepository.get_settled_positions()` returns only CLOSED positions with non-null `realized_pnl`.
28. `PositionRepository.get_positions_by_status("OPEN")` returns only OPEN positions.
29. `PositionRepository.get_positions_by_status("CLOSED")` returns only CLOSED positions.

### Integration Tests
30. Full `generate_report()` with in-memory SQLite and real `PositionRepository` — report matches expected values end-to-end.
31. `generate_report()` with mixed OPEN and CLOSED positions — aggregates only settled, includes all in entries.
32. Orchestrator constructs `PositionLifecycleReporter` in `__init__()`.
33. `_portfolio_aggregation_loop()` invokes `generate_report()` after `compute_snapshot()`.
34. `generate_report()` failure within the loop is caught independently — does NOT terminate the loop or affect `compute_snapshot()`.
35. `PositionLifecycleReporter` module has no dependency on prompt/context/evaluation/ingestion modules (import boundary check).
36. `PositionLifecycleReporter` performs zero DB writes during `generate_report()` (verify no INSERT/UPDATE/DELETE statements issued).

### Regression Gate
37. Full suite: `pytest --asyncio-mode=auto tests/ -q` — all tests pass.
38. Coverage: `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` — >= 80%.

---

## Deliverables

1. RED-phase failing test summary.
2. GREEN implementation summary by file.
3. Passing targeted test summary + full regression summary.
4. Final staged `git diff` for MAAP checker review.

---

## MAAP Reflection Pass (Checker Prompt for Gemini 2.5 Pro)

Use the following exact prompt with the final staged `git diff`:

```text
You are the MAAP Checker for WI-24 (Position Lifecycle Reporter) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi24.md
2) docs/PRD-v8.0.md (WI-24 section)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Read-only violation (any DB write, session commit/flush, repository write method call, position status mutation)
- Decimal violations (any float usage in aggregation arithmetic, size_tokens derivation, PnL summation, or hold-duration computation)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation)
- Fail-open violation (invalid date range raising an exception instead of logging and returning all positions)
- Division-by-zero (entry_price == Decimal("0") causing an unhandled exception instead of yielding size_tokens = Decimal("0"))
- Win/loss invariant violation (winning_count + losing_count + breakeven_count != total_settled_count)
- Isolation violations (PositionLifecycleReporter importing prompt, context, evaluation, ingestion, ExitStrategyEngine, ExitOrderRouter, PnLCalculator, ExecutionRouter, OrderBroadcaster, TransactionSigner, BankrollSyncProvider, or PortfolioAggregator modules)
- Loop independence (generate_report() failure affecting compute_snapshot() or vice versa within _portfolio_aggregation_loop())
- Task count regression (new asyncio.create_task for lifecycle reporter — should be inline only)
- Repository modification (any change to existing PositionRepository methods — only additive methods allowed)
- Regression (any modification to PositionTracker, ExitStrategyEngine, ExitOrderRouter, PnLCalculator, ExecutionRouter, PolymarketClient, PortfolioAggregator, or existing schemas; coverage < 80%)

Additional required checks:
- PositionLifecycleReporter class exists in src/agents/execution/lifecycle_reporter.py
- generate_report(start_date, end_date) -> LifecycleReport is the sole public async method
- PositionLifecycleEntry Pydantic model exists in src/schemas/risk.py, is frozen, with float-rejecting validators for entry_price, size_tokens, exit_price (nullable), realized_pnl (nullable)
- LifecycleReport Pydantic model exists in src/schemas/risk.py, is frozen, with float-rejecting validators for total_realized_pnl, avg_hold_duration_hours, best_pnl, worst_pnl
- Positions loaded via PositionRepository.get_all_positions() — no direct session queries
- Optional date filtering on routed_at_utc with fail-open for invalid ranges
- Settled positions: status == "CLOSED" and realized_pnl is not None
- size_tokens = order_size_usdc / entry_price with division-by-zero guard
- total_realized_pnl = sum(realized_pnl) across settled positions using Decimal arithmetic
- avg_hold_duration_hours = total_hold_seconds / count / 3600 using Decimal arithmetic
- best_pnl = max(realized_pnl), worst_pnl = min(realized_pnl) across settled positions
- Win/loss classification: realized_pnl > 0 win, < 0 loss, == 0 breakeven
- winning_count + losing_count + breakeven_count == total_settled_count invariant
- PositionRepository.get_all_positions() added (returns all positions)
- PositionRepository.get_settled_positions() added (returns CLOSED with non-null realized_pnl)
- PositionRepository.get_positions_by_status(status) added (returns positions by status)
- No existing PositionRepository methods modified
- PositionLifecycleReporter constructed in Orchestrator.__init__() with config and db_session_factory
- generate_report() invoked within _portfolio_aggregation_loop() after compute_snapshot()
- generate_report() failure handled independently from compute_snapshot() failure
- lifecycle.report_generated structlog event emitted with required fields
- lifecycle.report_empty structlog event emitted when no positions found
- lifecycle_report_loop.error structlog event emitted when generate_report() raises in the loop
- No new asyncio.create_task — reporter is inline within existing loop
- Task count unchanged: 7 when enable_portfolio_aggregator=True, 6 when False
- Zero new database tables, columns, or Alembic migrations
- Zero new queues
- Queue topology unchanged: market_queue -> prompt_queue -> execution_queue
- dry_run flag included in LifecycleReport for audit context

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-24/invariants
4) Explicit statement on each MAAP critical category:
   - Read-only violation: CLEARED/FLAGGED
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Fail-open violation: CLEARED/FLAGGED
   - Division-by-zero: CLEARED/FLAGGED
   - Win/loss invariant: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
   - Loop independence: CLEARED/FLAGGED
   - Task count regression: CLEARED/FLAGGED
   - Repository modification: CLEARED/FLAGGED
   - Regression: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
