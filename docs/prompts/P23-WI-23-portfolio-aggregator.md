# P23-WI-23 — Portfolio Aggregator Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-4.1 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi23-portfolio-aggregator` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/risk-auditor.md`
- `.agents/rules/db-engineer.md`
- `.agents/rules/async-architect.md`
- `.agents/rules/security-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-23 for Phase 8: a read-only analytics component (`PortfolioAggregator`) that computes a real-time aggregate snapshot of all open positions — total notional USDC, unrealized PnL, position count, and locked collateral — and exposes the result as a typed `PortfolioSnapshot`.

This WI is **read-only analytics**. It must aggregate open-position metrics using Decimal-only arithmetic, fetch current prices from `PolymarketClient.fetch_order_book()` with fail-open fallback to `entry_price`, and run as an optional config-gated background task in the Orchestrator. It must not write to the database, mutate position state, influence routing or exit decisions, or touch any upstream component.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi23.md`
4. `docs/PRD-v8.0.md` (Phase 8 / WI-23 section)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/orchestrator.py` — **primary integration target; `_portfolio_aggregation_loop()` is new, conditional on `enable_portfolio_aggregator`**
9. `src/agents/execution/polymarket_client.py` (dependency: `fetch_order_book()` for current prices)
10. `src/db/repositories/position_repository.py` (dependency: `get_open_positions()` for open position reads)
11. `src/db/models.py` (context: `Position` ORM model — read-only access)
12. `src/schemas/position.py` (context: `PositionRecord`, `PositionStatus` — frozen, no modifications)
13. `src/schemas/execution.py` (context boundary: frozen, no modifications)
14. `src/core/config.py`
15. Existing tests:
    - `tests/unit/test_exit_scan_loop.py`
    - `tests/unit/test_pnl_calculator.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-23 test files first:
   - `tests/unit/test_portfolio_aggregator.py`
   - `tests/integration/test_portfolio_aggregator_integration.py`
2. Write failing tests for all required behaviors:
   - `PortfolioSnapshot` Pydantic model exists in `src/schemas/risk.py`, is frozen, with Decimal-validated fields.
   - `PortfolioSnapshot` rejects `float` in `total_notional_usdc` at Pydantic boundary.
   - `PortfolioSnapshot` rejects `float` in `total_unrealized_pnl` at Pydantic boundary.
   - `PortfolioSnapshot` rejects `float` in `total_locked_collateral_usdc` at Pydantic boundary.
   - `PortfolioSnapshot` accepts `Decimal` in all financial fields and is immutable after construction.
   - `PortfolioAggregator` exists in `src/agents/execution/portfolio_aggregator.py` with constructor accepting `config: AppConfig`, `polymarket_client: PolymarketClient`, and `db_session_factory: async_sessionmaker[AsyncSession]`.
   - `compute_snapshot() -> PortfolioSnapshot` is the sole public async method.
   - `compute_snapshot()` with zero open positions returns a zero-valued `PortfolioSnapshot`.
   - `compute_snapshot()` with one open position and successful price fetch returns correct `total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc`.
   - `compute_snapshot()` with multiple open positions aggregates correctly (verify summation).
   - `compute_snapshot()` when `fetch_order_book()` returns `None` — falls back to `entry_price`, `unrealized_pnl == Decimal("0")` for that position, `positions_with_stale_price == 1`.
   - `compute_snapshot()` when all price fetches fail — `positions_with_stale_price == position_count`, `total_unrealized_pnl == Decimal("0")`.
   - `compute_snapshot()` with `entry_price == Decimal("0")` — `position_size_tokens == Decimal("0")`, no division-by-zero error.
   - `compute_snapshot()` with profitable position (`current_price > entry_price`) → positive `total_unrealized_pnl`.
   - `compute_snapshot()` with losing position (`current_price < entry_price`) → negative `total_unrealized_pnl`.
   - `portfolio.snapshot_computed` structlog event emitted with correct fields after successful snapshot.
   - `portfolio.price_fetch_failed` structlog event emitted when price fetch returns `None`.
   - `AppConfig` accepts `enable_portfolio_aggregator` as `bool` with default `False`.
   - `AppConfig` accepts `portfolio_aggregation_interval_sec` as `Decimal` with default `Decimal("30")`.
   - `Orchestrator.start()` with `enable_portfolio_aggregator=True` creates `PortfolioAggregatorTask`.
   - `Orchestrator.start()` with `enable_portfolio_aggregator=False` does NOT create `PortfolioAggregatorTask`.
   - `PortfolioAggregator` has zero imports from prompt, context, evaluation, or ingestion modules.
   - `PortfolioAggregator` performs zero DB writes.
3. Run RED tests:
   - `pytest tests/unit/test_portfolio_aggregator.py -v`
   - `pytest tests/integration/test_portfolio_aggregator_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add `enable_portfolio_aggregator` and `portfolio_aggregation_interval_sec` to AppConfig

Target:
- `src/core/config.py`

Requirements:
1. Add `enable_portfolio_aggregator: bool` field with default `False`.
2. Add `portfolio_aggregation_interval_sec: Decimal` field with default `Decimal("30")`.
3. `enable_portfolio_aggregator` is the config gate — when `False`, no `PortfolioAggregatorTask` is created in the Orchestrator. Zero runtime overhead when disabled.
4. `portfolio_aggregation_interval_sec` type is `Decimal`, consistent with `exit_scan_interval_seconds`.
5. Converted to `float` only at the `asyncio.sleep()` call boundary inside `_portfolio_aggregation_loop()`.
6. Both fields are read once at construction/loop time. Not dynamically adjustable at runtime.

### Step 2 — Create `PortfolioSnapshot` Model in `src/schemas/risk.py`

Target:
- `src/schemas/risk.py` (new file)

Requirements:
1. New schema module `src/schemas/risk.py`.
2. `PortfolioSnapshot` is a frozen Pydantic `BaseModel` with fields:
   - `snapshot_at_utc: datetime`
   - `position_count: int`
   - `total_notional_usdc: Decimal`
   - `total_unrealized_pnl: Decimal`
   - `total_locked_collateral_usdc: Decimal`
   - `positions_with_stale_price: int`
   - `dry_run: bool`
3. Add `_reject_float_financials` field validator covering `total_notional_usdc`, `total_unrealized_pnl`, and `total_locked_collateral_usdc`.
4. `model_config = {"frozen": True}` — immutable after construction.
5. `float` values are rejected at Pydantic boundary for all three financial fields.
6. `src/schemas/risk.py` is a leaf schema module. It must only import `pydantic`, `decimal`, `datetime`, `typing`. It must NOT import any `src/` module.

### Step 3 — Create `PortfolioAggregator` Module

Target:
- `src/agents/execution/portfolio_aggregator.py` (new)

Requirements:
1. New class `PortfolioAggregator` with constructor accepting:
   - `config: AppConfig`
   - `polymarket_client: PolymarketClient`
   - `db_session_factory: async_sessionmaker[AsyncSession]`
2. Single public method: `async def compute_snapshot(self) -> PortfolioSnapshot`.
3. **Load open positions:** `PositionRepository(session).get_open_positions()` inside an `async with self._db_session_factory() as session` block. If the list is empty, return a zero-valued `PortfolioSnapshot` immediately (no price fetches needed).
4. **Fetch current prices (fail-open):** For each open position, call `self._polymarket_client.fetch_order_book(position.token_id)`.
   - If `snapshot is not None`: use `snapshot.midpoint_probability` as `current_price`.
   - If `snapshot is None`: use `Decimal(str(position.entry_price))` as `current_price`. Increment `positions_with_stale_price`. Emit `portfolio.price_fetch_failed` warning with `position_id`, `token_id`, `fallback="entry_price"`.
5. **Per-position computation (Decimal-only):**
   ```
   entry_price_d = Decimal(str(position.entry_price))
   order_size_usdc_d = Decimal(str(position.order_size_usdc))

   if entry_price_d == Decimal("0"):
       position_size_tokens = Decimal("0")
   else:
       position_size_tokens = order_size_usdc_d / entry_price_d

   current_notional = current_price * position_size_tokens
   unrealized_pnl = (current_price - entry_price_d) * position_size_tokens
   locked_collateral = order_size_usdc_d
   ```
6. **Aggregate:** Running Decimal sums for `total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc`, `position_count`, `positions_with_stale_price`.
7. **Build `PortfolioSnapshot`:** Construct with `snapshot_at_utc=datetime.now(timezone.utc)` and `dry_run=self._config.dry_run`.
8. **Log snapshot:** Emit `portfolio.snapshot_computed` at `info` level with fields: `position_count`, `total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc`, `positions_with_stale_price`, `dry_run`.
9. Return the `PortfolioSnapshot`.
10. Price fetches are sequential (not `asyncio.gather`) to respect CLOB rate limits.
11. All arithmetic is `Decimal`. No `float()` conversion at any step.
12. Structured logging via `structlog` only — no `print()`.
13. Zero imports from:
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

### Step 4 — Integrate into Orchestrator

Target:
- `src/orchestrator.py`

Requirements:
1. **Constructor wiring:** Construct `PortfolioAggregator(config=self.config, polymarket_client=self.polymarket_client, db_session_factory=AsyncSessionLocal)` in `Orchestrator.__init__()`, after `self.pnl_calculator` construction.
2. **New async loop method:**
   ```python
   async def _portfolio_aggregation_loop(self) -> None:
       """Periodic portfolio snapshot aggregation (WI-23)."""
       while True:
           await asyncio.sleep(
               float(self.config.portfolio_aggregation_interval_sec)
           )
           try:
               await self.portfolio_aggregator.compute_snapshot()
           except Exception as exc:
               logger.error(
                   "portfolio_aggregation_loop.error",
                   error=str(exc),
               )
   ```
3. **Sleep-first pattern:** `asyncio.sleep()` is the first statement inside the `while True` loop body. Consistent with `_discovery_loop()` and `_exit_scan_loop()`. The first snapshot fires after one full interval, giving the pipeline time to discover markets and record positions.
4. **Conditional task registration in `start()`:**
   ```python
   if self.config.enable_portfolio_aggregator:
       self._tasks.append(
           asyncio.create_task(
               self._portfolio_aggregation_loop(),
               name="PortfolioAggregatorTask",
           )
       )
   ```
   Appended after the existing 6 tasks. When `enable_portfolio_aggregator=False` (default), no task is created.
5. **Exception handling:** `compute_snapshot()` failure is caught by `except Exception`, logged via `portfolio_aggregation_loop.error`, and does NOT re-raise or terminate the loop. `asyncio.CancelledError` propagates naturally for clean shutdown.
6. **Shutdown:** No changes needed. `PortfolioAggregatorTask` (if created) is in `self._tasks` and is automatically cancelled and awaited during `shutdown()`.
7. **Task count:** When `enable_portfolio_aggregator=True`, `self._tasks` contains 7 entries. When `False`, 6 entries (unchanged from Phase 7).

### Step 5 — GREEN Validation

Run:
```bash
pytest tests/unit/test_portfolio_aggregator.py -v
pytest tests/integration/test_portfolio_aggregator_integration.py -v
pytest tests/unit/test_exit_scan_loop.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. **Read-only analytics.** `PortfolioAggregator` performs zero DB writes regardless of `dry_run`. No repository write methods (`insert_position`, `update_status`, `record_settlement`) are called. No session `commit()` or `flush()` is issued.
2. **Decimal financial integrity.** All aggregation arithmetic (`position_size_tokens`, `current_notional`, `unrealized_pnl`, `locked_collateral`, `total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc`) is `Decimal`-only. Float is rejected at Pydantic boundary via `PortfolioSnapshot` validator. No float intermediary in any arithmetic step.
3. **Fail-open price resolution.** A failed `fetch_order_book()` never blocks snapshot computation. The position's `entry_price` is used as fallback, yielding `unrealized_pnl = Decimal("0")` for that position. `positions_with_stale_price` tracks degradation. A snapshot with all-stale prices is valid, not an error.
4. **Config-gated optional task.** When `enable_portfolio_aggregator=False` (default), no `PortfolioAggregatorTask` is created. Zero runtime overhead. No change to the existing 6-task pipeline.
5. **Division-by-zero guard.** `entry_price == Decimal("0")` yields `position_size_tokens = Decimal("0")`, which cascades to `current_notional = Decimal("0")` and `unrealized_pnl = Decimal("0")`. No exception raised.
6. **No bypass of `LLMEvaluationResponse` terminal Gatekeeper.** `PortfolioAggregator` operates far downstream: after evaluation, routing, exit evaluation, exit routing, settlement, and broadcasting. It is a passive observer.
7. **Module isolation.** Zero imports from prompt, context, evaluation, or ingestion modules. Zero imports from `ExitStrategyEngine`, `ExitOrderRouter`, `PnLCalculator`, `ExecutionRouter`, `OrderBroadcaster`, `TransactionSigner`, or `BankrollSyncProvider`.
8. **Fail-open loop semantics.** A failed `compute_snapshot()` call within the loop is caught, logged via `portfolio_aggregation_loop.error`, and retried on the next interval. The loop never terminates on a single failure.
9. **Sleep-first pattern.** `asyncio.sleep()` is the first statement inside the loop body, not the last. Consistent with `_discovery_loop()` and `_exit_scan_loop()`.
10. **Shutdown preserved.** `PortfolioAggregatorTask` (if created) is cancelled and awaited via the existing `self._tasks` lifecycle in `shutdown()`. No additional shutdown code.
11. **No queue topology changes.** `market_queue -> prompt_queue -> execution_queue`. No new queue introduced.
12. **Async pipeline preserved.** WI-23 adds one optional async task following the existing `create_task` + `gather` pattern. No blocking calls.
13. **No database schema changes.** Zero new tables, zero new columns, zero Alembic migrations. WI-23 reads existing data only.
14. **Frozen upstream components.** `PositionTracker`, `PositionRepository` (existing methods), `ExitStrategyEngine`, `ExitOrderRouter`, `PnLCalculator`, `ExecutionRouter`, `PolymarketClient`, `OrderBroadcaster`, and all schemas in `src/schemas/execution.py` and `src/schemas/position.py` are byte-identical before and after WI-23.
15. **`dry_run` behavior is passthrough.** The `dry_run` flag is included in `PortfolioSnapshot` for audit context. No new `dry_run` gate is needed because the component is inherently read-only.

---

## Required Test Matrix

At minimum, WI-23 tests must prove:

### Unit Tests
1. `PortfolioSnapshot` accepts `Decimal` in all financial fields and is frozen (immutable after construction).
2. `PortfolioSnapshot` rejects `float` in `total_notional_usdc` at Pydantic boundary.
3. `PortfolioSnapshot` rejects `float` in `total_unrealized_pnl` at Pydantic boundary.
4. `PortfolioSnapshot` rejects `float` in `total_locked_collateral_usdc` at Pydantic boundary.
5. `compute_snapshot()` with zero open positions returns `PortfolioSnapshot(position_count=0, total_notional_usdc=Decimal("0"), total_unrealized_pnl=Decimal("0"), total_locked_collateral_usdc=Decimal("0"))`.
6. `compute_snapshot()` with one open position and successful price fetch returns correct `total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc` (known inputs, verify exact Decimal values).
7. `compute_snapshot()` with multiple open positions aggregates correctly (verify Decimal summation across 2-3 positions).
8. `compute_snapshot()` when `fetch_order_book()` returns `None` for a position — falls back to `entry_price`, `unrealized_pnl == Decimal("0")` for that position, `positions_with_stale_price == 1`.
9. `compute_snapshot()` when all price fetches fail — `positions_with_stale_price == position_count`, `total_unrealized_pnl == Decimal("0")`.
10. `compute_snapshot()` with `entry_price == Decimal("0")` — `position_size_tokens == Decimal("0")`, no division-by-zero error, position contributes zero to all aggregates.
11. `compute_snapshot()` correctly computes `position_size_tokens = order_size_usdc / entry_price` for known inputs.
12. `compute_snapshot()` with profitable position: `current_price > entry_price` → positive `total_unrealized_pnl`.
13. `compute_snapshot()` with losing position: `current_price < entry_price` → negative `total_unrealized_pnl`.
14. `portfolio.snapshot_computed` structlog event emitted with correct fields after successful snapshot.
15. `portfolio.price_fetch_failed` structlog event emitted when price fetch returns `None`.
16. `AppConfig` accepts `enable_portfolio_aggregator` as `bool` with default `False`.
17. `AppConfig` accepts `portfolio_aggregation_interval_sec` as `Decimal` with default `Decimal("30")`.
18. `AppConfig` accepts `portfolio_aggregation_interval_sec` overridden via environment variable (e.g., `PORTFOLIO_AGGREGATION_INTERVAL_SEC=60`).

### Integration Tests
19. `Orchestrator.start()` with `enable_portfolio_aggregator=True` creates `PortfolioAggregatorTask` — verify task name in `self._tasks`.
20. `Orchestrator.start()` with `enable_portfolio_aggregator=False` does NOT create `PortfolioAggregatorTask` — task list has exactly 6 entries.
21. `PortfolioAggregatorTask` is cancelled cleanly during `Orchestrator.shutdown()` without raising.
22. Full `compute_snapshot()` with in-memory SQLite and mocked `PolymarketClient` — snapshot matches expected values end-to-end.
23. `_portfolio_aggregation_loop()` fires after the configured interval and calls `compute_snapshot()`.
24. `_portfolio_aggregation_loop()` catches `Exception` from `compute_snapshot()` and does NOT re-raise — loop continues to next iteration.
25. `PortfolioAggregator` module has no dependency on prompt/context/evaluation/ingestion modules (import boundary check).
26. `PortfolioAggregator` performs zero DB writes during `compute_snapshot()` (verify no INSERT/UPDATE/DELETE statements issued).

### Regression Gate
27. Full suite: `pytest --asyncio-mode=auto tests/ -q` — all tests pass.
28. Coverage: `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` — >= 80%.

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
You are the MAAP Checker for WI-23 (Portfolio Aggregator) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi23.md
2) docs/PRD-v8.0.md (WI-23 section)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Read-only violation (any DB write, session commit/flush, repository write method call, position status mutation)
- Decimal violations (any float usage in aggregation arithmetic, position_size_tokens derivation, or money-path logic)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation)
- Fail-open violation (price fetch failure blocking snapshot computation, or raising instead of falling back to entry_price)
- Config gate violation (PortfolioAggregatorTask created when enable_portfolio_aggregator=False, or missing when True)
- Loop safety (compute_snapshot() exception escaping the try/except, loop terminating on failure)
- Sleep-first violation (asyncio.sleep is NOT the first statement in the loop body)
- Division-by-zero (entry_price == Decimal("0") causing an unhandled exception instead of yielding Decimal("0"))
- Isolation violations (PortfolioAggregator importing prompt, context, evaluation, ingestion, ExitStrategyEngine, ExitOrderRouter, PnLCalculator, ExecutionRouter, OrderBroadcaster, TransactionSigner, or BankrollSyncProvider modules)
- Regression (any modification to PositionRepository existing methods, PositionTracker, ExitStrategyEngine, ExitOrderRouter, PnLCalculator, ExecutionRouter, PolymarketClient, or existing schemas; coverage < 80%)

Additional required checks:
- PortfolioAggregator class exists in src/agents/execution/portfolio_aggregator.py
- compute_snapshot() -> PortfolioSnapshot is the sole public async method
- PortfolioSnapshot Pydantic model exists in src/schemas/risk.py, is frozen, with float-rejecting validators for 3 financial fields
- PortfolioSnapshot fields: snapshot_at_utc, position_count, total_notional_usdc, total_unrealized_pnl, total_locked_collateral_usdc, positions_with_stale_price, dry_run
- Open positions loaded via PositionRepository.get_open_positions() — no direct session queries
- Current prices fetched via PolymarketClient.fetch_order_book(token_id) per position
- Price-fetch failure falls back to entry_price with positions_with_stale_price incremented and portfolio.price_fetch_failed warning logged
- total_notional_usdc = sum(current_price * position_size_tokens) using Decimal arithmetic
- total_unrealized_pnl = sum((current_price - entry_price) * position_size_tokens) using Decimal arithmetic
- total_locked_collateral_usdc = sum(order_size_usdc)
- position_size_tokens = order_size_usdc / entry_price with division-by-zero guard
- AppConfig.enable_portfolio_aggregator is bool with default False
- AppConfig.portfolio_aggregation_interval_sec is Decimal with default Decimal("30")
- Orchestrator._portfolio_aggregation_loop() exists as async method with sleep-first pattern
- PortfolioAggregatorTask conditionally registered in start() only when enable_portfolio_aggregator=True
- PortfolioAggregatorTask cancelled during shutdown() via existing self._tasks lifecycle
- portfolio.snapshot_computed structlog event emitted with required fields
- portfolio.price_fetch_failed structlog event emitted on per-position price failure
- portfolio_aggregation_loop.error structlog event emitted when compute_snapshot() raises
- PortfolioAggregator constructed in Orchestrator.__init__() with config, polymarket_client, db_session_factory
- Zero new database tables, columns, or Alembic migrations
- Zero new queues
- Queue topology unchanged: market_queue -> prompt_queue -> execution_queue
- Task count: 7 when enable_portfolio_aggregator=True, 6 when False

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-23/invariants
4) Explicit statement on each MAAP critical category:
   - Read-only violation: CLEARED/FLAGGED
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Fail-open violation: CLEARED/FLAGGED
   - Config gate violation: CLEARED/FLAGGED
   - Loop safety: CLEARED/FLAGGED
   - Sleep-first violation: CLEARED/FLAGGED
   - Division-by-zero: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
   - Regression: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
