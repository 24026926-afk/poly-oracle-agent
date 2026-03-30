# P21-WI-21 — Realized PnL & Settlement Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-4.1 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi21-pnl-calculator` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/risk-auditor.md`
- `.agents/rules/db-engineer.md`
- `.agents/rules/security-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-21 for Phase 7: a read-only accounting component (`PnLCalculator`) that computes realized profit/loss when a position is closed via an exit order (WI-20 `ExitOrderRouter`), and persists the settlement data to the `positions` table through `PositionRepository`.

This WI is **accounting-only**. It must compute realized PnL using Decimal-only arithmetic, persist settlement data through the repository pattern, and respect the `dry_run` gate for all DB writes. It must not influence routing, exit decisions, or any upstream component.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi21.md`
4. `docs/PRD-v7.0.md` (Phase 7 / WI-21 section)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/orchestrator.py` — **primary integration target; `_exit_scan_loop()` currently routes exits via `ExitOrderRouter` but has no settlement step**
9. `src/agents/execution/exit_order_router.py` (context boundary: frozen, no modifications)
10. `src/agents/execution/exit_strategy_engine.py` (context boundary: frozen, no modifications)
11. `src/db/repositories/position_repository.py` — **secondary integration target; `record_settlement()` is new**
12. `src/db/models.py` — **secondary integration target; 3 new nullable columns**
13. `src/schemas/position.py` — **secondary integration target; 3 new optional fields**
14. `src/schemas/execution.py` — **secondary integration target; `PnLRecord` is new**
15. `src/core/config.py`
16. `src/core/exceptions.py`
17. Existing tests:
    - `tests/unit/test_exit_order_router.py`
    - `tests/unit/test_exit_scan_loop.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-21 test files first:
   - `tests/unit/test_pnl_calculator.py`
   - `tests/integration/test_pnl_settlement_integration.py`
2. Write failing tests for all required behaviors:
   - `PnLCalculator` exists in `src/agents/execution/pnl_calculator.py` with constructor accepting `config: AppConfig` and `db_session_factory: async_sessionmaker[AsyncSession]`.
   - `settle(position: PositionRecord, exit_price: Decimal) -> PnLRecord` is the sole public async method.
   - PnL formula: `realized_pnl = (exit_price - entry_price) * position_size_tokens` using Decimal arithmetic.
   - Token quantity: `position_size_tokens = order_size_usdc / entry_price` using Decimal division.
   - Division by zero (`entry_price == Decimal("0")`) returns `PnLRecord(realized_pnl=Decimal("0"))` and logs `pnl.degenerate_entry_price` warning.
   - `PnLRecord` Pydantic model is frozen with Decimal-validated fields; `float` rejected at boundary.
   - `dry_run=True` computes full `PnLRecord`, logs `pnl.dry_run_settlement`, zero DB writes, zero session creation.
   - `dry_run=False` writes to DB via `PositionRepository.record_settlement()`.
   - `PositionRepository.record_settlement()` writes `realized_pnl`, `exit_price`, and `closed_at_utc` to an existing position row.
   - Settlement is idempotent: re-settling a position with existing `realized_pnl` logs a warning and returns without overwriting.
   - `PnLCalculationError` raised when position not found during settlement.
   - `PnLCalculationError` raised on DB persistence failure.
   - `PnLCalculator` has zero imports from prompt, context, evaluation, or ingestion modules.
   - `PositionRecord` accepts `None` for new optional fields (`realized_pnl`, `exit_price`, `closed_at_utc`).
   - `PositionRecord` rejects `float` for `realized_pnl` and `exit_price`.
3. Run RED tests:
   - `pytest tests/unit/test_pnl_calculator.py -v`
   - `pytest tests/integration/test_pnl_settlement_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add `PnLCalculationError` to Exception Taxonomy

Target:
- `src/core/exceptions.py`

Requirements:
1. Add `PnLCalculationError` exception class inheriting from `PolyOracleError`.
2. Constructor accepts `reason: str`, `position_id: str | None = None`, `condition_id: str | None = None`, `cause: Exception | None = None`.
3. Structured message carries `position_id` and `condition_id` without exposing credentials.
4. Follows the established pattern from `ExitEvaluationError` / `ExitMutationError` / `ExitRoutingError`.

### Step 2 — Add `PnLRecord` Model to Execution Schemas

Target:
- `src/schemas/execution.py`

Requirements:
1. Add `PnLRecord` Pydantic `BaseModel` with fields: `position_id: str`, `condition_id: str`, `entry_price: Decimal`, `exit_price: Decimal`, `order_size_usdc: Decimal`, `position_size_tokens: Decimal`, `realized_pnl: Decimal`, `closed_at_utc: datetime`.
2. Add `_reject_float_financials` field validator covering all five financial fields (`entry_price`, `exit_price`, `order_size_usdc`, `position_size_tokens`, `realized_pnl`).
3. `model_config = {"frozen": True}` — immutable after construction.
4. `float` values are rejected at Pydantic boundary for all financial fields.

### Step 3 — Extend `PositionRecord` Schema with Settlement Fields

Target:
- `src/schemas/position.py`

Requirements:
1. Add three new **optional** fields to `PositionRecord`: `realized_pnl: Decimal | None = None`, `exit_price: Decimal | None = None`, `closed_at_utc: datetime | None = None`.
2. Extend the existing `_reject_float_financials` validator to cover `realized_pnl` and `exit_price`.
3. Add a `None` guard at the top of the validator if not already present: `if value is None: return value`.
4. `None` is valid for `OPEN` and `FAILED` positions. Fields are populated only after settlement.

### Step 4 — Extend `Position` ORM Model with Settlement Columns

Target:
- `src/db/models.py`

Requirements:
1. Add three new **nullable** columns to the `Position` class:
   - `realized_pnl: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=38, scale=18), nullable=True)`
   - `exit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(precision=38, scale=18), nullable=True)`
   - `closed_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)`
2. Financial column precision is `Numeric(38, 18)` matching all existing financial columns.
3. `closed_at_utc` uses `DateTime(timezone=True)` matching existing timestamp columns.

### Step 5 — Create Alembic Migration `0003_add_pnl_columns.py`

Target:
- `migrations/versions/0003_add_pnl_columns.py`

Requirements:
1. Parent migration is `0002` (`0002_add_open_positions_table.py`).
2. `upgrade()` adds three nullable columns to `positions`: `realized_pnl Numeric(38, 18)`, `exit_price Numeric(38, 18)`, `closed_at_utc DateTime(timezone=True)`.
3. `downgrade()` drops all three columns.
4. All three columns are `nullable=True` — existing rows are unaffected.

### Step 6 — Add `record_settlement()` to `PositionRepository`

Target:
- `src/db/repositories/position_repository.py`

Requirements:
1. Add `async def record_settlement(self, *, position_id: str, realized_pnl: Decimal, exit_price: Decimal, closed_at_utc: datetime) -> Position | None`.
2. **Additive** — does not modify existing `insert_position()`, `update_status()`, or `get_open_positions()` methods.
3. **Idempotent** — if `realized_pnl` is already set on the position, log `position.settlement_already_recorded` warning and return the existing row without overwriting.
4. Does not change `status` — the `OPEN -> CLOSED` transition was already performed by `ExitStrategyEngine`.
5. Uses `flush()` (not `commit()`) — the caller controls commit timing.
6. Returns `Position | None` — `None` if `position_id` not found.

### Step 7 — Create `PnLCalculator` Module

Target:
- `src/agents/execution/pnl_calculator.py` (new)

Requirements:
1. New class `PnLCalculator` with constructor accepting `config: AppConfig` and `db_session_factory: async_sessionmaker[AsyncSession]`.
2. Single public method: `async def settle(self, position: PositionRecord, exit_price: Decimal) -> PnLRecord`.
3. All inputs coerced to `Decimal(str(...))` for safety, even if already `Decimal`.
4. Token quantity: `position_size_tokens = order_size_usdc / entry_price`. Division-by-zero guard: if `entry_price == Decimal("0")`, set `position_size_tokens = Decimal("0")` and log `pnl.degenerate_entry_price` warning.
5. PnL formula: `realized_pnl = (exit_price - entry_price) * position_size_tokens`. All arithmetic is `Decimal`. No `float()` conversion.
6. Build `PnLRecord` with `closed_at_utc = datetime.now(timezone.utc)`.
7. Emit `pnl.calculated` structured log at `info` level with all audit fields (always, regardless of `dry_run`).
8. **`dry_run` gate:** If `self._config.dry_run` is `True`, emit `pnl.dry_run_settlement` log and return `PnLRecord` immediately. Zero DB writes. No session created, no repository instantiated.
9. **Live persistence:** Open a session via `self._db_session_factory()`, construct `PositionRepository`, call `record_settlement()`, and `commit()`. If position not found, raise `PnLCalculationError(reason="position_not_found_for_settlement")`. If any DB error, raise `PnLCalculationError(reason="settlement_persistence_failed")`.
10. Emit `pnl.persisted` structured log after successful DB write (live only).
11. Structured logging via `structlog` only — no `print()`.
12. Zero imports from:
    - `src/agents/evaluation/*`
    - `src/agents/context/*`
    - `src/agents/ingestion/*`
    - `src/agents/execution/polymarket_client.py`
    - `src/agents/execution/bankroll_sync.py`
    - `src/agents/execution/signer.py`
    - `src/agents/execution/execution_router.py`
    - `src/agents/execution/exit_order_router.py`

### Step 8 — Integrate into Orchestrator

Target:
- `src/orchestrator.py`

Requirements:
1. Construct `PnLCalculator(config=self.config, db_session_factory=AsyncSessionLocal)` in `Orchestrator.__init__()`, immediately after `self.exit_order_router` construction.
2. In `_exit_scan_loop()`, after `ExitOrderRouter.route_exit()` produces a `SELL_ROUTED` or `DRY_RUN` result with a non-None `exit_price`:
   ```python
   if exit_order_result.action in (
       ExitOrderAction.SELL_ROUTED,
       ExitOrderAction.DRY_RUN,
   ) and exit_order_result.exit_price is not None:
       try:
           pnl_record = await self.pnl_calculator.settle(
               position=position,
               exit_price=exit_order_result.exit_price,
           )
       except Exception as exc:
           logger.error(
               "exit_scan.pnl_settlement_error",
               position_id=exit_result.position_id,
               error=str(exc),
           )
   ```
3. PnL failure does not block the exit scan loop or downstream broadcast.
4. No other orchestrator changes — queue topology, task structure, and pipeline order remain unchanged.

### Step 9 — GREEN Validation

Run:
```bash
pytest tests/unit/test_pnl_calculator.py -v
pytest tests/integration/test_pnl_settlement_integration.py -v
pytest tests/unit/test_exit_scan_loop.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. **Accounting only.** `PnLCalculator` is a pure accounting component. It reads `PositionRecord` metadata and an exit price, computes a scalar `Decimal`, and writes through `PositionRepository`. It does not influence routing, exit decisions, or any upstream component.
2. **Decimal financial integrity.** All PnL arithmetic (`position_size_tokens`, `realized_pnl`), settlement values, `PnLRecord` fields, and database columns are `Decimal` / `Numeric(38,18)`. Float is rejected at Pydantic boundary. No float intermediary in any arithmetic step.
3. **`dry_run=True` blocks DB writes.** Full `PnLRecord` is computed and logged; zero persistence. No session created, no repository instantiated.
4. **Repository pattern.** `PositionRepository.record_settlement()` is the sole path for settlement columns. Additive method — does not modify existing `insert_position()`, `update_status()`, or `get_open_positions()` methods.
5. **Settlement idempotency.** Re-settling a position with existing `realized_pnl` logs a warning and returns without overwriting. No double-counting.
6. **Position status immutability.** `PnLCalculator` writes financial settlement data only (`realized_pnl`, `exit_price`, `closed_at_utc`). It never changes `status`. The `OPEN -> CLOSED` transition is upstream (`ExitStrategyEngine`).
7. **Module isolation.** Zero imports from prompt, context, evaluation, or ingestion modules. Zero imports from `PolymarketClient`, `BankrollSyncProvider`, `TransactionSigner`, `ExecutionRouter`, or `ExitOrderRouter`.
8. **No bypass of `LLMEvaluationResponse` terminal Gatekeeper.** `PnLCalculator` operates far downstream: after evaluation, routing, exit evaluation, exit routing, and broadcasting.
9. **Fail-open semantics.** A failed PnL settlement does not block the exit scan loop, the exit order broadcast, or the execution consumer. Missing PnL can be backfilled later.
10. **No queue topology changes.** `market_queue -> prompt_queue -> execution_queue`. No new queue.
11. **Async pipeline preserved.** `PnLCalculator` runs within the existing `_exit_scan_loop()` async task. No new tasks introduced.
12. **Alembic migration chain.** `0003` descends from `0002`. All three new columns are `nullable=True`.
13. **ExitOrderRouter, ExitStrategyEngine, ExecutionRouter are frozen.** Zero modifications.

---

## Required Test Matrix

At minimum, WI-21 tests must prove:

### Unit Tests
1. PnL formula correctness: known inputs `(entry=0.45, exit=0.65, size_usdc=25)` -> expected `realized_pnl` value.
2. PnL formula with loss: `exit_price < entry_price` -> negative `realized_pnl`.
3. PnL formula breakeven: `exit_price == entry_price` -> `realized_pnl == Decimal("0")`.
4. Division by zero: `entry_price == Decimal("0")` -> `realized_pnl == Decimal("0")`, `pnl.degenerate_entry_price` warning logged.
5. `position_size_tokens = order_size_usdc / entry_price` produces correct `Decimal` for known inputs.
6. `PnLRecord` model is frozen — field assignment after construction raises error.
7. `PnLRecord` rejects `float` in financial fields at Pydantic boundary.
8. `PnLRecord` accepts `Decimal` in financial fields.
9. `dry_run=True` computes and returns `PnLRecord`; zero DB writes; `pnl.dry_run_settlement` logged.
10. `dry_run=False` writes to DB via `PositionRepository.record_settlement()`.
11. `pnl.calculated` event emitted with all required fields (both `dry_run` and live).
12. `pnl.persisted` event emitted after successful DB write (live only).
13. `PositionRepository.record_settlement()` writes all 3 columns.
14. `PositionRepository.record_settlement()` idempotency: position with existing `realized_pnl` -> warning logged, no overwrite.
15. `PositionRepository.record_settlement()` returns `None` when `position_id` not found.
16. `PnLCalculationError` raised when position not found during settlement.
17. `PnLCalculationError` raised on DB persistence failure.
18. `PositionRecord` accepts `None` for new optional fields (`realized_pnl`, `exit_price`, `closed_at_utc`).
19. `PositionRecord` rejects `float` for `realized_pnl` and `exit_price`.

### Integration Tests
20. End-to-end `dry_run=True` — full pipeline from `PositionRecord` through `PnLCalculator`, record computed, zero DB writes.
21. End-to-end `dry_run=False` — settlement persisted, `PnLRecord` values match DB row.
22. `PnLCalculator` module has no dependency on prompt/context/evaluation/ingestion modules (import boundary check).
23. Settlement after routing failure — `PnLCalculator` not called when `ExitOrderResult.action == FAILED`.
24. Alembic migration `0003` applies cleanly on top of `0002`.
25. Alembic migration `0003` downgrade removes all 3 columns.
26. Orchestrator constructs `PnLCalculator` in `__init__()` with correct dependencies.

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
You are the MAAP Checker for WI-21 (Realized PnL & Settlement) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi21.md
2) docs/PRD-v7.0.md (WI-21 section)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Decimal violations (any float usage in PnL formula, token quantity derivation, settlement values, or money-path logic)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation)
- DB write isolation (PnLCalculator writing outside PositionRepository.record_settlement())
- dry_run violation (any DB write, session creation, or repository instantiation when dry_run=True)
- Alembic migration missing or wrong column types (realized_pnl and exit_price must be Numeric(38,18); closed_at_utc must be DateTime(timezone=True); parent must be 0002)
- Blocking upstream path on PnL failure (PnL error must not block exit scan loop, broadcast, or execution consumer)
- Isolation violations (PnLCalculator importing prompt, context, evaluation, ingestion, PolymarketClient, BankrollSyncProvider, TransactionSigner, ExecutionRouter, or ExitOrderRouter modules)
- Regression (any modification to ExitStrategyEngine, ExitOrderRouter, ExecutionRouter, PositionTracker, or existing PositionRepository methods; coverage < 80%)

Additional required checks:
- PnLCalculator class exists in src/agents/execution/pnl_calculator.py
- settle(position: PositionRecord, exit_price: Decimal) -> PnLRecord is the sole public async method
- PnLRecord Pydantic model is frozen with float-rejecting validators for all 5 financial fields
- PnLCalculationError exception exists in src/core/exceptions.py with reason, position_id, condition_id, cause fields
- realized_pnl = (exit_price - entry_price) * position_size_tokens uses Decimal-only arithmetic
- position_size_tokens = order_size_usdc / entry_price uses Decimal division
- Division-by-zero guard: entry_price == Decimal("0") returns PnLRecord(realized_pnl=Decimal("0")) with warning log
- PositionRepository.record_settlement() is additive (existing methods unmodified)
- record_settlement() is idempotent: existing realized_pnl -> warning + no overwrite
- record_settlement() uses flush() not commit() — caller controls commit timing
- Position ORM model has 3 new nullable columns: realized_pnl Numeric(38,18), exit_price Numeric(38,18), closed_at_utc DateTime(timezone=True)
- PositionRecord schema has 3 new optional fields: realized_pnl, exit_price, closed_at_utc with None guard in validator
- dry_run=True path: pnl.calculated + pnl.dry_run_settlement emitted, PnLRecord returned, zero DB writes, zero session creation
- dry_run=False path: pnl.calculated + pnl.persisted emitted, record_settlement() called, session.commit() called
- PnLCalculator constructed in Orchestrator.__init__() after exit_order_router
- PnLCalculator.settle() called in _exit_scan_loop() after route_exit() produces SELL_ROUTED or DRY_RUN with non-None exit_price
- PnL failure caught per-position in _exit_scan_loop() — does not terminate loop or block broadcast
- structlog events: pnl.calculated, pnl.degenerate_entry_price, pnl.dry_run_settlement, pnl.persisted, pnl.position_not_found, pnl.persistence_failed all present with required fields
- Zero new async tasks introduced
- Queue topology unchanged

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-21/invariants
4) Explicit statement on each MAAP critical category:
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - DB write isolation: CLEARED/FLAGGED
   - dry_run violation: CLEARED/FLAGGED
   - Alembic migration: CLEARED/FLAGGED
   - Blocking upstream path: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
   - Regression: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
