# P19-WI-19 — Exit Strategy Engine Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi19-exit-strategy-engine` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/db-engineer.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/test-engineer.md`
- `.agents/rules/security-auditor.md`

## Role

You are implementing WI-19 for Phase 6: the Exit Strategy Engine that evaluates `OPEN` positions persisted by `PositionTracker` (WI-17) against typed exit criteria and determines whether to hold or close each position.

This is the first WI that reads position state from the database and makes dynamic lifecycle decisions. After WI-17, every routed order is recorded as an `OPEN` or `FAILED` `PositionRecord` in the `positions` table — but no component re-evaluates those open positions against current market conditions. Once a position is opened it stays `OPEN` forever. WI-19 closes this lifecycle gap by introducing conservative, rule-based exit evaluation that can transition a position from `OPEN` to `CLOSED` via `PositionRepository.update_status()`.

WI-19 does **not** submit exit orders to the CLOB, calculate realized PnL against settled outcomes, aggregate portfolio-level exposure, or invoke LLM reasoning for exit decisions. It produces a typed `ExitResult` that downstream components (future WIs) can use to trigger exit-order routing.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi19.md` — **primary source of truth for this WI**
4. `docs/business_logic/business_logic_wi17.md` — upstream PositionTracker contract
5. `docs/archive/ARCHIVE_PHASE_5.md`
6. `docs/system_architecture.md`
7. `docs/risk_management.md`
8. `docs/business_logic.md`
9. `src/agents/execution/position_tracker.py` — upstream producer of `PositionRecord`
10. `src/schemas/position.py` — `PositionRecord`, `PositionStatus` definitions
11. `src/schemas/execution.py` — `ExecutionAction`, `ExecutionResult`, `PositionRecord` re-export, forward-ref rebuild pattern
12. `src/agents/execution/polymarket_client.py` — `PolymarketClient`, `MarketSnapshot` (consumed for fresh midpoint/bid)
13. `src/db/repositories/position_repository.py` — `PositionRepository` with `get_open_positions()`, `update_status()` methods
14. `src/db/repositories/position_repo.py` — compatibility re-export module
15. `src/db/models.py` — `Position` ORM model (WI-17, reused as-is)
16. `src/core/config.py` — `AppConfig` (target for new exit config fields)
17. `src/core/exceptions.py` — existing exception hierarchy (target for new exit exceptions)
18. `src/orchestrator.py` — wiring target for `ExitStrategyEngine` instantiation and call site
19. Existing tests:
    - `tests/unit/test_position_tracker.py`
    - `tests/integration/test_position_tracker_integration.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`
    - `tests/conftest.py` — shared fixtures (`async_session`, `db_session_factory`, `test_config`)

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-19 test files first:
   - `tests/unit/test_exit_strategy_engine.py`
   - `tests/integration/test_exit_strategy_engine_integration.py`
2. Write failing tests for all required behaviors:

   **Schema tests (unit):**
   - `ExitReason` enum exists in `src/schemas/execution.py` with values `NO_EDGE`, `STOP_LOSS`, `TIME_DECAY`, `TAKE_PROFIT`, `STALE_MARKET`, `ERROR`.
   - `ExitSignal` Pydantic model exists in `src/schemas/execution.py` with fields: `position` (`PositionRecord`), `current_midpoint` (`Decimal`), `current_best_bid` (`Decimal`), `evaluated_at_utc` (`datetime`). Model is frozen.
   - `ExitSignal` rejects `float` for `current_midpoint` and `current_best_bid` via `field_validator` — parametrized test over both fields.
   - `ExitSignal` accepts `Decimal` for both financial fields.
   - `ExitResult` Pydantic model exists in `src/schemas/execution.py` with fields: `position_id` (`str`), `condition_id` (`str`), `should_exit` (`bool`), `exit_reason` (`ExitReason`), `entry_price` (`Decimal`), `current_midpoint` (`Decimal`), `current_best_bid` (`Decimal`), `position_age_hours` (`Decimal`), `unrealized_edge` (`Decimal`), `evaluated_at_utc` (`datetime`). Model is frozen.
   - `ExitResult` rejects `float` for each of the 5 financial fields (`entry_price`, `current_midpoint`, `current_best_bid`, `position_age_hours`, `unrealized_edge`) via `field_validator` — parametrized test over all 5 fields.
   - `ExitResult` accepts `Decimal` for each financial field.

   **Component tests (unit):**
   - `ExitStrategyEngine` exists in `src/agents/execution/exit_strategy_engine.py` and exposes two public methods:
     - `async def evaluate_position(signal: ExitSignal) -> ExitResult`
     - `async def scan_open_positions() -> list[ExitResult]`
   - **Status gate:** `evaluate_position()` with `signal.position.status == CLOSED` returns `ExitResult(should_exit=False, exit_reason=ERROR)`.
   - **Status gate:** `evaluate_position()` with `signal.position.status == FAILED` returns `ExitResult(should_exit=False, exit_reason=ERROR)`.
   - **Stop-loss triggers:** `evaluate_position()` with `current_midpoint = entry_price - Decimal("0.20")` (drop of 0.20 > configured 0.15 stop-loss) returns `ExitResult(should_exit=True, exit_reason=STOP_LOSS)`.
   - **Stop-loss does NOT trigger:** `evaluate_position()` with `current_midpoint = entry_price - Decimal("0.05")` (drop of 0.05 < 0.15 stop-loss) returns `should_exit` consistent with other criteria, NOT `STOP_LOSS`.
   - **Time-decay triggers:** `evaluate_position()` with position age >= `exit_position_max_age_hours` (48h) returns `ExitResult(should_exit=True, exit_reason=TIME_DECAY)` (assuming stop-loss is not also triggered).
   - **Time-decay does NOT trigger:** `evaluate_position()` with position age < 48h and favorable edge returns `should_exit=False`.
   - **No-edge triggers:** `evaluate_position()` with `current_midpoint <= entry_price` (edge evaporated, but above stop-loss) returns `ExitResult(should_exit=True, exit_reason=NO_EDGE)`.
   - **Take-profit triggers:** `evaluate_position()` with `current_midpoint = entry_price + Decimal("0.25")` (gain of 0.25 >= configured 0.20 take-profit) returns `ExitResult(should_exit=True, exit_reason=TAKE_PROFIT)`.
   - **Conservative hold:** `evaluate_position()` with `current_midpoint = entry_price + Decimal("0.05")` (small positive edge, young position) returns `ExitResult(should_exit=False)`.
   - **Priority ordering:** when stop-loss + time-decay both triggered → reason is `STOP_LOSS`.
   - **Priority ordering:** when time-decay + no-edge both triggered (no stop-loss) → reason is `TIME_DECAY`.
   - **Position age calculation:** age is correctly computed as `Decimal` hours from `routed_at_utc` to `evaluated_at_utc`.
   - **Unrealized edge calculation:** `unrealized_edge = current_midpoint - entry_price` is correct for profitable and underwater positions.
   - **dry_run=True — zero DB writes:** `evaluate_position()` with `dry_run=True` and `should_exit=True` does NOT call `PositionRepository.update_status()`, does NOT open a DB session for mutation. Mock on session factory asserts mutation call count == 0.
   - **dry_run=True — structured log:** `evaluate_position()` with `dry_run=True` emits a `structlog` entry with `exit_engine.dry_run_exit` event key.
   - **dry_run=False + should_exit=True — persists CLOSED:** `evaluate_position()` with `dry_run=False` and `should_exit=True` calls `PositionRepository.update_status(position_id, new_status=PositionStatus.CLOSED)`.
   - **dry_run=False + should_exit=False — no mutation:** `evaluate_position()` with `dry_run=False` and `should_exit=False` makes no `update_status()` call.
   - **Import boundary:** `ExitStrategyEngine` module has zero imports from `src/agents/context/`, `src/agents/evaluation/`, `src/agents/ingestion/`.

   **Integration tests:**
   - **scan_open_positions() reads DB:** `scan_open_positions()` calls `PositionRepository.get_open_positions()` via real async SQLite and returns results.
   - **scan_open_positions() fetches order book:** `scan_open_positions()` calls `PolymarketClient.fetch_order_book()` for each open position.
   - **scan_open_positions() handles stale market:** when `fetch_order_book()` returns `None`, produces `ExitResult(should_exit=True, exit_reason=STALE_MARKET)`.
   - **Full flow — hold:** `OPEN` position with favorable midpoint and young age → `should_exit=False`, position remains `OPEN` in DB.
   - **Full flow — stop-loss exit:** `OPEN` position with stop-loss breached → `should_exit=True`, `PositionRepository.update_status()` called, position transitions to `CLOSED`.
   - **Full flow — time-decay exit:** `OPEN` position with age >= threshold → `should_exit=True`, status transitions to `CLOSED`.
   - **Import boundary (integration):** `exit_strategy_engine.py` module has no dependency on context/evaluation/ingestion modules (AST import check).

3. Run RED tests:
   ```bash
   pytest tests/unit/test_exit_strategy_engine.py -v
   pytest tests/integration/test_exit_strategy_engine_integration.py -v
   ```
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add `ExitReason` Enum, `ExitSignal` Schema, and `ExitResult` Schema

Target:
- `src/schemas/execution.py`

Requirements:
1. Add `ExitReason(str, Enum)` with values: `NO_EDGE`, `STOP_LOSS`, `TIME_DECAY`, `TAKE_PROFIT`, `STALE_MARKET`, `ERROR`.
2. Add `ExitSignal(BaseModel)` with fields:
   - `position`: `PositionRecord`
   - `current_midpoint`: `Decimal`
   - `current_best_bid`: `Decimal`
   - `evaluated_at_utc`: `datetime`
3. Apply a `field_validator` on `ExitSignal` for `current_midpoint` and `current_best_bid` — identical pattern to `PositionRecord._reject_float_financials`: `float` inputs rejected, `Decimal` accepted, other types coerced via `Decimal(str(value))`.
4. `ExitSignal.model_config = {"frozen": True}` — immutable after creation.
5. Add `ExitResult(BaseModel)` with fields:
   - `position_id`: `str`
   - `condition_id`: `str`
   - `should_exit`: `bool`
   - `exit_reason`: `ExitReason`
   - `entry_price`: `Decimal`
   - `current_midpoint`: `Decimal`
   - `current_best_bid`: `Decimal`
   - `position_age_hours`: `Decimal`
   - `unrealized_edge`: `Decimal`
   - `evaluated_at_utc`: `datetime`
6. Apply a `field_validator` on `ExitResult` for all five financial fields (`entry_price`, `current_midpoint`, `current_best_bid`, `position_age_hours`, `unrealized_edge`) — same rejection pattern.
7. `ExitResult.model_config = {"frozen": True}` — immutable after creation.
8. Do NOT modify `ExecutionAction`, `ExecutionResult`, `PositionRecord`, or `PositionStatus` — they remain unchanged.
9. Place the new types AFTER the `PositionRecord.model_rebuild(...)` call at the bottom of the file, since they depend on `PositionRecord` being fully resolved.

### Step 2 — Add New `AppConfig` Fields

Target:
- `src/core/config.py`

Requirements:
1. Add three new fields to `AppConfig` under a `# --- Exit Strategy (WI-19) ---` comment block, placed after the `# --- Execution Router (WI-16) ---` section:
   ```python
   exit_position_max_age_hours: Decimal = Field(
       default=Decimal("48"),
       description="Max hours before an open position triggers time-decay exit",
   )
   exit_stop_loss_drop: Decimal = Field(
       default=Decimal("0.15"),
       description="Midpoint drop from entry that triggers stop-loss (0.15 = 15pp)",
   )
   exit_take_profit_gain: Decimal = Field(
       default=Decimal("0.20"),
       description="Midpoint gain from entry that triggers take-profit (0.20 = 20pp)",
   )
   ```
2. All three fields are `Decimal`, not `float`.
3. Do NOT modify any existing config fields.

### Step 3 — Add New Exception Types

Target:
- `src/core/exceptions.py`

Requirements:
1. Add `ExitEvaluationError(PolyOracleError)`:
   ```python
   class ExitEvaluationError(PolyOracleError):
       """Raised when exit strategy evaluation fails."""

       def __init__(
           self,
           reason: str,
           position_id: str | None = None,
           cause: Exception | None = None,
       ) -> None:
           message = reason
           if position_id:
               message = f"{reason} (position_id={position_id})"
           super().__init__(message)
           self.reason = reason
           self.position_id = position_id
           self.cause = cause
   ```
2. Add `ExitMutationError(PolyOracleError)`:
   ```python
   class ExitMutationError(PolyOracleError):
       """Raised when position state transition fails."""

       def __init__(
           self,
           reason: str,
           position_id: str | None = None,
           cause: Exception | None = None,
       ) -> None:
           message = reason
           if position_id:
               message = f"{reason} (position_id={position_id})"
           super().__init__(message)
           self.reason = reason
           self.position_id = position_id
           self.cause = cause
   ```
3. Follow the existing exception pattern established by `BalanceFetchError` and `RoutingRejectedError`.

### Step 4 — Create `ExitStrategyEngine` Component

Target:
- `src/agents/execution/exit_strategy_engine.py` (new)

Requirements:
1. New class `ExitStrategyEngine` with constructor accepting:
   - `config: AppConfig`
   - `polymarket_client: PolymarketClient`
   - `db_session_factory: async_sessionmaker[AsyncSession]`
2. Two public methods:
   - `async def evaluate_position(self, signal: ExitSignal) -> ExitResult`
   - `async def scan_open_positions(self) -> list[ExitResult]`
3. Structured logging via `structlog` only — no `print()`.
4. **Module isolation — zero imports from:**
   - `src/agents/context/*` (prompt, aggregator)
   - `src/agents/evaluation/*` (claude_client, grok_client)
   - `src/agents/ingestion/*` (ws_client, rest_client, discovery)
5. **Allowed imports:**
   - `src/core/config` (`AppConfig`)
   - `src/core/exceptions` (`ExitEvaluationError`, `ExitMutationError`)
   - `src/schemas/execution` (`ExitSignal`, `ExitResult`, `ExitReason`, `PositionRecord`, `PositionStatus`)
   - `src/db/repositories/position_repository` (`PositionRepository`)
   - `src/agents/execution/polymarket_client` (`PolymarketClient`)
   - `structlog`, `datetime`, `decimal` (stdlib / logging)

### Step 5 — Implement `evaluate_position()` Async Contract

Target:
- `src/agents/execution/exit_strategy_engine.py`

Requirements:

1. **Status gate (first check):**
   - If `signal.position.status != PositionStatus.OPEN`, log warning (`"exit_engine.non_open_position"` with `position_id` and `status`), return `ExitResult(should_exit=False, exit_reason=ExitReason.ERROR, ...)` with all financial fields populated from the signal and zero-valued computed fields.

2. **Age calculation (Decimal-safe):**
   ```python
   age_seconds = Decimal(str(
       (signal.evaluated_at_utc - signal.position.routed_at_utc).total_seconds()
   ))
   position_age_hours = age_seconds / Decimal("3600")
   ```

3. **Unrealized edge:**
   ```python
   unrealized_edge = signal.current_midpoint - signal.position.entry_price
   ```

4. **Exit criteria evaluation (all Decimal comparisons):**
   - `stop_loss_triggered = unrealized_edge <= -self._config.exit_stop_loss_drop`
   - `time_decay_triggered = position_age_hours >= self._config.exit_position_max_age_hours`
   - `no_edge_triggered = unrealized_edge <= Decimal("0")`
   - `take_profit_triggered = unrealized_edge >= self._config.exit_take_profit_gain`

5. **Exit decision:**
   - `should_exit = stop_loss_triggered or time_decay_triggered or no_edge_triggered or take_profit_triggered`

6. **Reason mapping with priority ordering** (`STOP_LOSS > TIME_DECAY > NO_EDGE > TAKE_PROFIT`):
   ```python
   if stop_loss_triggered:
       exit_reason = ExitReason.STOP_LOSS
   elif time_decay_triggered:
       exit_reason = ExitReason.TIME_DECAY
   elif no_edge_triggered:
       exit_reason = ExitReason.NO_EDGE
   elif take_profit_triggered:
       exit_reason = ExitReason.TAKE_PROFIT
   else:
       exit_reason = ExitReason.NO_EDGE  # default hold reason for auditability
   ```

7. **Build ExitResult** with all fields populated from signal, position, and computed values.

8. **Log evaluation start:**
   - `"exit_engine.evaluating"` at INFO level with `position_id`, `condition_id`, `entry_price`.

9. **Log decision:**
   - If `should_exit`: `"exit_engine.exit_triggered"` at INFO level with `position_id`, `exit_reason`, `unrealized_edge`, `position_age_hours`.
   - If not `should_exit`: `"exit_engine.hold"` at INFO level with `position_id`, `unrealized_edge`, `position_age_hours`.

10. **dry_run gate (early return, BEFORE any DB interaction):**
    - If `self._config.dry_run is True`:
      - If `should_exit`: log `"exit_engine.dry_run_exit"` at INFO level with `position_id`, `exit_reason`, and all result fields.
      - Return the `ExitResult` Pydantic model.
      - **CRITICAL:** Do NOT create an `AsyncSession`. Do NOT call `PositionRepository.update_status()`. Zero DB writes.

11. **Live mutation:**
    - If `self._config.dry_run is False` and `should_exit is True`:
      - Open a session: `async with self._db_session_factory() as session:`.
      - Instantiate `PositionRepository(session)`.
      - Call `await repo.update_status(result.position_id, new_status=PositionStatus.CLOSED.value)`.
      - `await session.commit()`.
      - Log `"exit_engine.position_closed"` at INFO level with `position_id`, `condition_id`, `exit_reason`.
      - If `update_status()` returns `None` or raises, log `"exit_engine.mutation_failed"` at ERROR level and raise `ExitMutationError`.

12. **Live hold:**
    - If `self._config.dry_run is False` and `should_exit is False`:
      - Return `ExitResult` without any repository call or session creation.

### Step 6 — Implement `scan_open_positions()` Async Contract

Target:
- `src/agents/execution/exit_strategy_engine.py`

Requirements:

1. Open a session from the injected factory, instantiate `PositionRepository`, call `get_open_positions()`.
2. For each `OPEN` position ORM row:
   - Convert the ORM `Position` to a `PositionRecord` Pydantic model. Map fields directly:
     - `id`, `condition_id`, `token_id`, `side`, `reason`, `routed_at_utc`, `recorded_at_utc` — direct copy.
     - `status` — convert via `PositionStatus(position_orm.status)`.
     - `execution_action` — convert via `ExecutionAction(position_orm.execution_action)`.
     - Financial fields (`entry_price`, `order_size_usdc`, `kelly_fraction`, `best_ask_at_entry`, `bankroll_usdc_at_entry`) — wrap in `Decimal(str(...))` if not already `Decimal`.
   - Fetch a fresh order-book snapshot via `self._polymarket_client.fetch_order_book(position_record.token_id)`.
   - If snapshot is `None`:
     - Produce an `ExitResult(should_exit=True, exit_reason=ExitReason.STALE_MARKET, ...)` for that position.
     - Log `"exit_engine.stale_market"` at WARNING level with `position_id` and `token_id`.
     - Use `Decimal("0")` sentinels for `current_midpoint`, `current_best_bid`, `unrealized_edge`.
   - If snapshot is available:
     - Build an `ExitSignal` from the `PositionRecord` and snapshot fields (`midpoint_probability`, `best_bid`).
     - Delegate to `self.evaluate_position(signal)`.
3. Collect all `ExitResult` instances into a list.
4. Log `"exit_engine.scan_complete"` at INFO level with `total`, `exits` (count of `should_exit=True`), `holds` (count of `should_exit=False`), `errors`.
5. Return the list.
6. If the repository call or session creation fails, raise `ExitEvaluationError`.
7. `dry_run` enforcement is inherited from `evaluate_position()` — no additional gate needed here.

### Step 7 — Update `test_config` Fixture

Target:
- `tests/conftest.py`

Requirements:
1. Add the three new WI-19 config fields to the `AppConfig.model_construct(...)` call in the `test_config` fixture:
   ```python
   exit_position_max_age_hours=Decimal("48"),
   exit_stop_loss_drop=Decimal("0.15"),
   exit_take_profit_gain=Decimal("0.20"),
   ```
2. All existing fixture values remain unchanged.

### Step 8 — Update Orchestrator Wiring

Target:
- `src/orchestrator.py`

Requirements:
1. Import `ExitStrategyEngine` from `src.agents.execution.exit_strategy_engine`.
2. Construct `ExitStrategyEngine` in `Orchestrator.__init__()`, after `PositionTracker`:
   ```python
   self.exit_strategy_engine = ExitStrategyEngine(
       config=self.config,
       polymarket_client=self.polymarket_client,
       db_session_factory=AsyncSessionLocal,
   )
   ```
3. `ExitStrategyEngine` is constructed regardless of `dry_run` mode — the engine enforces the write gate internally.
4. In `_execution_consumer_loop()`, add the exit scan call **after** `PositionTracker.record_execution()` and **before** the dry_run gate. The call must be inside the existing `try/except` block so failures are caught:
   ```python
   # After position_tracker.record_execution():
   try:
       await self.exit_strategy_engine.scan_open_positions()
   except Exception as exc:
       logger.error("execution.exit_scan_error", error=str(exc))
   ```
5. **CRITICAL:** An `exit_strategy_engine.scan_open_positions()` failure must NOT block or abort the broadcast path. It is fire-and-forget safe.
6. No other orchestrator changes — queue topology, task structure, and pipeline order remain unchanged.

### Step 9 — Update Existing Tests (If Needed)

Target:
- `tests/integration/test_orchestrator.py`
- `tests/integration/test_pipeline_e2e.py`

Requirements:
1. Existing orchestrator tests must account for the new `ExitStrategyEngine` wiring.
2. If any existing test constructs an `Orchestrator` directly, it must now work with `ExitStrategyEngine` present.
3. All existing test assertions must continue to pass — zero behavioral regression.
4. If the `test_config` fixture is used by existing tests, ensure the new `Decimal` fields don't break anything (they should not, since `model_construct` is permissive).

### Step 10 — GREEN Validation

Run:
```bash
pytest tests/unit/test_exit_strategy_engine.py -v
pytest tests/integration/test_exit_strategy_engine_integration.py -v
pytest tests/unit/test_position_tracker.py -v
pytest tests/integration/test_position_tracker_integration.py -v
pytest tests/unit/test_execution_router.py -v
pytest tests/integration/test_execution_router_integration.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. `ExitStrategyEngine` is a downstream consumer only — it reads `PositionRecord` and market snapshots, evaluates exit criteria, and transitions state. It never creates positions, recalculates Kelly sizing, or modifies routing.
2. `ExitStrategyEngine` is isolated — zero imports from context, prompt, evaluation, ingestion, or market-data modules other than `PolymarketClient` (read-only market data).
3. `LLMEvaluationResponse` remains the terminal pre-execution Gatekeeper — `ExitStrategyEngine` operates strictly downstream of validated execution outcomes.
4. `dry_run=True` blocks ALL position state mutations — no `update_status()` call, no session creation for writes. Enforced by early-return guard BEFORE any DB mutation.
5. `dry_run=True` does NOT block read-path DB access — `scan_open_positions()` may read `OPEN` positions from the database even in dry_run mode; only writes are gated.
6. All financial fields in `ExitSignal` and `ExitResult` are `Decimal` — `float` rejected at Pydantic boundary via `field_validator`.
7. `PositionRepository.update_status()` is the ONLY mutation path — `ExitStrategyEngine` never calls `insert_position()` or raw SQL.
8. `ExitStrategyEngine` transitions `OPEN` → `CLOSED` only — it never writes `OPEN`, `FAILED`, or any other status.
9. Conservative hold-by-default — position remains `OPEN` unless at least one exit criterion is met.
10. Priority ordering `STOP_LOSS > TIME_DECAY > NO_EDGE > TAKE_PROFIT` is deterministic — when multiple criteria trigger, the highest-severity reason wins.
11. An `evaluate_position()` or `scan_open_positions()` failure in the consumer loop must NOT block or abort the broadcast path.
12. No queue topology changes; preserve async 4-layer pipeline order.
13. `PositionTracker`, `ExecutionRouter`, `PolymarketClient`, `BankrollSyncProvider`, `TransactionSigner` internals are unmodified.
14. No new order signing, broadcasting, or on-chain state mutation capability is introduced.
15. `PositionRecord` and `PositionStatus` schemas are unmodified — WI-19 consumes them as read-only inputs.
16. `Position` ORM model and Alembic migrations are unmodified — WI-17 schema is sufficient.

---

## Required Test Matrix

At minimum, WI-19 tests must prove:

**Schema validation (unit):**
1. `ExitReason` enum has values `NO_EDGE`, `STOP_LOSS`, `TIME_DECAY`, `TAKE_PROFIT`, `STALE_MARKET`, `ERROR`.
2. `ExitSignal` rejects `float` for `current_midpoint` — parametrized across both financial fields.
3. `ExitSignal` accepts `Decimal` for both financial fields.
4. `ExitSignal` is frozen — assignment after creation raises.
5. `ExitResult` rejects `float` for `entry_price` — parametrized across all 5 financial fields.
6. `ExitResult` accepts `Decimal` for each financial field.
7. `ExitResult` is frozen — assignment after creation raises.

**Exit criteria (unit):**
8. Stop-loss triggers when `unrealized_edge <= -exit_stop_loss_drop` (e.g., entry=0.65, midpoint=0.48, drop=0.15).
9. Stop-loss does NOT trigger when `unrealized_edge > -exit_stop_loss_drop` (e.g., entry=0.65, midpoint=0.55).
10. Time-decay triggers when `position_age_hours >= exit_position_max_age_hours` (48h).
11. Time-decay does NOT trigger when `position_age_hours < exit_position_max_age_hours`.
12. No-edge triggers when `current_midpoint <= entry_price`.
13. Take-profit triggers when `unrealized_edge >= exit_take_profit_gain` (0.20).
14. `should_exit=False` when no criterion is met (small positive edge, young position).
15. Priority: stop-loss + time-decay → reason is `STOP_LOSS`.
16. Priority: time-decay + no-edge (no stop-loss) → reason is `TIME_DECAY`.

**Status gate (unit):**
17. `CLOSED` position returns `ExitResult(should_exit=False, exit_reason=ERROR)`.
18. `FAILED` position returns `ExitResult(should_exit=False, exit_reason=ERROR)`.

**Computed fields (unit):**
19. `position_age_hours` correctly calculated in `Decimal` hours.
20. `unrealized_edge` correct for profitable position.
21. `unrealized_edge` correct for underwater position.

**dry_run enforcement (unit):**
22. **[CRITICAL] `dry_run=True` — zero state mutations:** mock on `db_session_factory` asserts mutation call count == 0. No `update_status()` called.
23. **[CRITICAL] `dry_run=True` — structured log emitted:** capture `structlog` output and assert `exit_engine.dry_run_exit` event is logged.
24. `dry_run=False` + `should_exit=True` calls `PositionRepository.update_status(position_id, new_status="CLOSED")`.
25. `dry_run=False` + `should_exit=False` makes no `update_status()` call.

**Isolation (unit):**
26. Import-boundary test: `exit_strategy_engine.py` has no dependency on context/evaluation/ingestion modules (AST check).

**Integration:**
27. `scan_open_positions()` reads `OPEN` positions from real async SQLite via `get_open_positions()`.
28. `scan_open_positions()` fetches order book for each open position.
29. `scan_open_positions()` produces `STALE_MARKET` exit when `fetch_order_book()` returns `None`.
30. Full flow — hold: `OPEN` position, favorable midpoint, young age → `should_exit=False`, position remains `OPEN` in DB.
31. Full flow — stop-loss exit: `OPEN` position, stop-loss breached → `should_exit=True`, position transitions to `CLOSED` in DB.
32. Full flow — time-decay exit: `OPEN` position, age >= threshold → `should_exit=True`, position transitions to `CLOSED` in DB.
33. Import-boundary integration: `exit_strategy_engine.py` module passes AST import check.
34. Full suite regression: `pytest --asyncio-mode=auto tests/ -q` passes, coverage >= 80%.

---

## Deliverables

1. RED-phase failing test summary.
2. GREEN implementation summary by file.
3. Passing targeted test summary + full regression summary.
4. Final staged `git diff` for MAAP checker review.

---

## MAAP Reflection Pass (Checker Prompt)

Use the following exact prompt with the final staged `git diff`:

```text
You are the MAAP Checker for WI-19 (Exit Strategy Engine) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi19.md
2) docs/business_logic/business_logic_wi17.md (upstream contract)
3) docs/archive/ARCHIVE_PHASE_5.md invariants
4) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
5) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Decimal violations (any float usage in ExitSignal/ExitResult financial fields, exit criteria comparisons, or age calculation)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation upstream)
- Business logic drift (deviation from WI-19 scope — no exit order submission, no PnL accounting, no LLM exit reasoning, no portfolio aggregation)
- dry_run safety violations (any PositionRepository.update_status() call, session creation for writes, or position state mutation when dry_run=True)
- Isolation violations (ExitStrategyEngine importing from context, prompt, evaluation, or ingestion modules)
- Repository pattern violations (raw SQL outside PositionRepository, session lifecycle owned by engine instead of context manager)
- Position state violations (ExitStrategyEngine writing OPEN, FAILED, or any status other than CLOSED; calling insert_position())

Additional required checks:
- ExitReason enum exists in src/schemas/execution.py with values NO_EDGE, STOP_LOSS, TIME_DECAY, TAKE_PROFIT, STALE_MARKET, ERROR
- ExitSignal is frozen Pydantic model with field_validator rejecting float on current_midpoint and current_best_bid
- ExitResult is frozen Pydantic model with field_validator rejecting float on all 5 financial fields
- ExitStrategyEngine exists in src/agents/execution/exit_strategy_engine.py
- evaluate_position() is async, accepts ExitSignal, returns ExitResult
- scan_open_positions() is async, returns list[ExitResult]
- Status gate: non-OPEN positions return ExitResult(should_exit=False, exit_reason=ERROR)
- Exit criteria use Decimal-only comparisons with priority STOP_LOSS > TIME_DECAY > NO_EDGE > TAKE_PROFIT
- Conservative hold-by-default: should_exit=False when no criterion is met
- dry_run=True early-return guard fires BEFORE any session/repository mutation
- dry_run=True does NOT block read-path DB access in scan_open_positions()
- scan_open_positions() produces STALE_MARKET when fetch_order_book returns None
- ExitStrategyEngine constructed in Orchestrator.__init__() regardless of dry_run
- scan_open_positions() called in _execution_consumer_loop() after PositionTracker.record_execution()
- Exit evaluation failure does NOT block broadcast path
- No modification to PositionTracker, ExecutionRouter, PolymarketClient, BankrollSyncProvider, or TransactionSigner internals
- No modification to Position ORM model or Alembic migrations
- No new send/broadcast/approve/transfer capability introduced
- AppConfig gains exit_position_max_age_hours (Decimal, default 48), exit_stop_loss_drop (Decimal, default 0.15), exit_take_profit_gain (Decimal, default 0.20)
- ExitEvaluationError and ExitMutationError exist in src/core/exceptions.py

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-19/invariants
4) Explicit statement on each MAAP critical category:
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Business logic drift: CLEARED/FLAGGED
   - dry_run safety violations: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
   - Repository pattern violations: CLEARED/FLAGGED
   - Position state violations: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
