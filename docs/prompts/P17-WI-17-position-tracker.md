# P17-WI-17 — Position Tracker Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi17-position-tracker` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/db-engineer.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/test-engineer.md`
- `.agents/rules/security-auditor.md`

## Role

You are implementing WI-17 for Phase 6: the Position Tracker that persists execution outcomes from `ExecutionRouter.route()` as typed `PositionRecord` entries in a new `positions` table, providing lifecycle visibility into open, closed, and failed positions.

This is the first WI that writes execution outcomes to the database. After WI-16, the system can route a validated BUY decision into a sized, slippage-checked, optionally signed order payload — but once `ExecutionRouter.route()` returns an `ExecutionResult`, no component tracks what happened. WI-17 closes this gap. It does not implement exit logic (deferred to WI-19), PnL calculation, or position broadcasting.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi17.md`
4. `docs/archive/ARCHIVE_PHASE_5.md`
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/agents/execution/execution_router.py` — `ExecutionResult` producer (`route()` → `ExecutionResult`)
9. `src/schemas/execution.py` — `ExecutionAction`, `ExecutionResult` contracts
10. `src/db/models.py` — existing ORM models (`MarketSnapshot`, `AgentDecisionLog`, `ExecutionTx`, `Base`, `TxStatus` enum pattern)
11. `src/db/repositories/execution_repo.py` — `ExecutionRepository` pattern reference (constructor takes `AsyncSession`, all methods async, repo owns no connection lifecycle)
12. `src/db/repositories/__init__.py` — existing exports
13. `src/db/engine.py` — `AsyncSessionLocal`, `engine`
14. `src/core/config.py` — `AppConfig` (`dry_run` flag)
15. `src/orchestrator.py` — wiring target for `PositionTracker` instantiation and `_execution_consumer_loop()` call site
16. `migrations/versions/0001_initial_schema.py` — Alembic parent revision
17. Existing tests:
    - `tests/unit/test_execution_router.py`
    - `tests/integration/test_execution_router_integration.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`
    - `tests/integration/test_alembic_migrations.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-17 test files first:
   - `tests/unit/test_position_tracker.py`
   - `tests/integration/test_position_tracker_integration.py`
2. Write failing tests for all required behaviors:
   - `PositionRecord` exists in `src/schemas/execution.py` as a frozen Pydantic model with fields: `id`, `condition_id`, `token_id`, `status` (`PositionStatus`), `side`, `entry_price` (`Decimal`), `order_size_usdc` (`Decimal`), `kelly_fraction` (`Decimal`), `best_ask_at_entry` (`Decimal`), `bankroll_usdc_at_entry` (`Decimal`), `execution_action` (`ExecutionAction`), `reason` (`str | None`), `routed_at_utc` (`datetime`), `recorded_at_utc` (`datetime`).
   - `PositionStatus` enum exists in `src/schemas/execution.py` with values `OPEN`, `CLOSED`, `FAILED`.
   - `PositionRecord` rejects `float` for each of the 5 financial fields (`entry_price`, `order_size_usdc`, `kelly_fraction`, `best_ask_at_entry`, `bankroll_usdc_at_entry`) via `field_validator` — parametrized test over all 5 fields.
   - `PositionRecord` accepts `Decimal` for each financial field.
   - `PositionTracker` exists in `src/agents/execution/position_tracker.py` and exposes a single public method: `async def record_execution(result: ExecutionResult, condition_id: str, token_id: str) -> PositionRecord | None`.
   - **Status mapping — EXECUTED → OPEN:** `record_execution()` with `result.action=EXECUTED` returns `PositionRecord(status=OPEN)`.
   - **Status mapping — DRY_RUN → OPEN:** `record_execution()` with `result.action=DRY_RUN` returns `PositionRecord(status=OPEN)`.
   - **Status mapping — FAILED → FAILED:** `record_execution()` with `result.action=FAILED` returns `PositionRecord(status=FAILED)`.
   - **SKIP returns None:** `record_execution()` with `result.action=SKIP` returns `None`. No log, no DB write.
   - **dry_run=True — zero DB writes:** `record_execution()` with `dry_run=True` and `result.action=DRY_RUN` does NOT instantiate a repository, does NOT open a DB session. Mock on session factory asserts call count == 0.
   - **dry_run=True — structured log:** `record_execution()` with `dry_run=True` emits a `structlog` entry with position fields.
   - **dry_run=False — EXECUTED persists:** `record_execution()` with `dry_run=False` and `result.action=EXECUTED` calls `PositionRepository.insert_position()`.
   - **dry_run=False — FAILED persists:** `record_execution()` with `dry_run=False` and `result.action=FAILED` calls `PositionRepository.insert_position()`.
   - **Decimal("0") sentinels:** `record_execution()` with `result.action=FAILED` and `None` financial fields populates `Decimal("0")` for `entry_price`, `order_size_usdc`, `kelly_fraction`, `best_ask_at_entry`, `bankroll_usdc_at_entry`.
   - **Unreachable states:** `EXECUTED` + `dry_run=True` logs error and returns `None`. `DRY_RUN` + `dry_run=False` logs error and returns `None`.
   - **Import boundary:** `PositionTracker` module has zero imports from `src/agents/context/`, `src/agents/evaluation/`, `src/agents/ingestion/`.
   - **Repository round-trip (integration):** `PositionRepository.insert_position()` persists and round-trips all fields via real async SQLite.
   - **Repository filter (integration):** `get_open_by_condition_id()` returns only `OPEN` positions for the given market.
   - **Repository filter (integration):** `get_open_positions()` returns all `OPEN` positions across markets.
   - **Repository transition (integration):** `update_status()` transitions `OPEN` → `CLOSED`.
   - **Full flow EXECUTED (integration):** `ExecutionResult(EXECUTED)` → `record_execution()` → `get_open_positions()` returns the new record.
   - **Full flow FAILED (integration):** `ExecutionResult(FAILED)` → `record_execution()` → record has `status=FAILED`.
3. Run RED tests:
   - `pytest tests/unit/test_position_tracker.py -v`
   - `pytest tests/integration/test_position_tracker_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` or `migrations/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add `PositionStatus` Enum and `PositionRecord` Schema

Target:
- `src/schemas/execution.py`

Requirements:
1. Add `PositionStatus(str, Enum)` with values: `OPEN`, `CLOSED`, `FAILED`.
2. Add `PositionRecord(BaseModel)` with all fields specified in `business_logic_wi17.md` §3.3.
3. Financial fields: `entry_price`, `order_size_usdc`, `kelly_fraction`, `best_ask_at_entry`, `bankroll_usdc_at_entry` — all `Decimal`.
4. Apply a `field_validator` identical to `ExecutionResult._reject_float_financials` across all 5 financial fields. `float` inputs rejected, `Decimal` accepted, other types coerced via `Decimal(str(value))`.
5. `model_config = {"frozen": True}` — immutable after creation.
6. Do NOT modify `ExecutionAction` or `ExecutionResult` — they remain unchanged.

### Step 2 — Add `Position` ORM Model

Target:
- `src/db/models.py`

Requirements:
1. Add a new `PositionStatus` enum (ORM-level) mirroring the schema enum: `OPEN`, `CLOSED`, `FAILED`. Name it `PositionStatusEnum` to avoid clash with the Pydantic schema enum, or re-use the same `enum.Enum` if the project already shares enums between ORM and schema layers (follow existing `TxStatus`/`DecisionAction` pattern).
2. Add `Position(Base)` ORM model with `__tablename__ = "positions"`.
3. Columns (follow exact types):
   - `id`: `String(36)`, PK, default `_new_uuid`
   - `condition_id`: `String(256)`, NOT NULL, indexed
   - `token_id`: `String(256)`, NOT NULL
   - `status`: `String(16)`, NOT NULL (stores enum string value)
   - `side`: `String(8)`, NOT NULL
   - `entry_price`: `Numeric(precision=38, scale=18)`, NOT NULL
   - `order_size_usdc`: `Numeric(precision=38, scale=18)`, NOT NULL
   - `kelly_fraction`: `Numeric(precision=38, scale=18)`, NOT NULL
   - `best_ask_at_entry`: `Numeric(precision=38, scale=18)`, NOT NULL
   - `bankroll_usdc_at_entry`: `Numeric(precision=38, scale=18)`, NOT NULL
   - `execution_action`: `String(16)`, NOT NULL
   - `reason`: `String(512)`, NULLABLE
   - `routed_at_utc`: `DateTime(timezone=True)`, NOT NULL
   - `recorded_at_utc`: `DateTime(timezone=True)`, NOT NULL, default `_utcnow`
4. **CRITICAL:** Financial columns use `Numeric(precision=38, scale=18)` — NEVER `Float`. Import `Numeric` from `sqlalchemy`.
5. Add `__table_args__` with indexes:
   - `Index("ix_positions_condition_id", "condition_id")`
   - `Index("ix_positions_status", "status")`
   - `Index("ix_positions_condition_id_status", "condition_id", "status")`
6. No foreign keys to other tables. No relationships. Standalone by design.

### Step 3 — Create Alembic Migration

Target:
- `migrations/versions/0002_add_positions_table.py`

Requirements:
1. Revision ID: `0002`.
2. `down_revision = "0001"` (parent: initial schema).
3. `upgrade()`: `op.create_table("positions", ...)` with all columns from Step 2.
   - Financial columns: `sa.Numeric(precision=38, scale=18)`, nullable=False.
   - Create all three indexes.
4. `downgrade()`: `op.drop_table("positions")`.
5. `alembic upgrade head` must succeed on a fresh database.

### Step 4 — Create `PositionRepository`

Target:
- `src/db/repositories/position_repo.py` (new)

Requirements:
1. Follow `ExecutionRepository` pattern exactly:
   - Constructor: `__init__(self, session: AsyncSession)` — stores session as `self._session`.
   - All methods are `async`.
   - Repository owns no connection lifecycle.
2. Required methods:
   - `async def insert_position(self, position: Position) -> Position` — `self._session.add(position)`, `await self._session.flush()`, return position. Log via `structlog`.
   - `async def get_by_id(self, position_id: str) -> Position | None` — `select(Position).where(Position.id == position_id)`, `scalar_one_or_none()`.
   - `async def get_open_by_condition_id(self, condition_id: str) -> list[Position]` — `select(Position).where(Position.condition_id == condition_id, Position.status == "OPEN")`, return `list(result.scalars().all())`.
   - `async def get_open_positions(self) -> list[Position]` — `select(Position).where(Position.status == "OPEN")`, return `list(result.scalars().all())`.
   - `async def update_status(self, position_id: str, *, new_status: str) -> Position | None` — fetch by id, update `status` field, `await self._session.flush()`, return updated or `None`. WI-17 does NOT call this method; provided for WI-19.
3. Use `structlog` for debug logging (consistent with `ExecutionRepository`).
4. Import `Position` from `src.db.models`.

### Step 5 — Export `PositionRepository`

Target:
- `src/db/repositories/__init__.py`

Requirements:
1. Add `from src.db.repositories.position_repo import PositionRepository`.
2. Add `"PositionRepository"` to `__all__`.

### Step 6 — Create `PositionTracker` Component

Target:
- `src/agents/execution/position_tracker.py` (new)

Requirements:
1. New class `PositionTracker` with constructor accepting:
   - `config: AppConfig`
   - `db_session_factory: async_sessionmaker[AsyncSession]`
2. Single public method: `async def record_execution(self, result: ExecutionResult, condition_id: str, token_id: str) -> PositionRecord | None`.
3. Structured logging via `structlog` only — no `print()`.
4. **Module isolation — zero imports from:**
   - `src/agents/context/*` (prompt, aggregator)
   - `src/agents/evaluation/*` (claude_client, grok_client)
   - `src/agents/ingestion/*` (ws_client, rest_client, discovery)
5. **Allowed imports:**
   - `src/core/config` (`AppConfig`)
   - `src/schemas/execution` (`ExecutionResult`, `ExecutionAction`, `PositionRecord`, `PositionStatus`)
   - `src/db/repositories/position_repo` (`PositionRepository`)
   - `src/db/models` (`Position`)
   - `structlog`, `uuid`, `datetime`, `decimal` (stdlib / logging)

### Step 7 — Implement `record_execution()` Async Contract

Target:
- `src/agents/execution/position_tracker.py`

Requirements:

1. **SKIP gate (first check):**
   - If `result.action == ExecutionAction.SKIP`, return `None` immediately. No log, no DB write.

2. **Unreachable state guards:**
   - If `result.action == ExecutionAction.EXECUTED` and `self._config.dry_run is True`: log error (`"position_tracker.unreachable_executed_in_dry_run"`), return `None`.
   - If `result.action == ExecutionAction.DRY_RUN` and `self._config.dry_run is False`: log error (`"position_tracker.unreachable_dry_run_in_live"`), return `None`.

3. **Status derivation:**
   - `EXECUTED` → `PositionStatus.OPEN`
   - `DRY_RUN` → `PositionStatus.OPEN`
   - `FAILED` → `PositionStatus.FAILED`

4. **Build PositionRecord:**
   - `id = str(uuid.uuid4())`
   - Map all fields from `ExecutionResult` per `business_logic_wi17.md` §4.3.
   - `side = "BUY"` (hardcoded; WI-17 is BUY-only).
   - `recorded_at_utc = datetime.now(timezone.utc)`.
   - For `FAILED` results where financial fields (`midpoint_probability`, `order_size_usdc`, `kelly_fraction`, `best_ask`, `bankroll_usdc`) are `None`: substitute `Decimal("0")` sentinel values.

5. **dry_run gate (early return, BEFORE any DB interaction):**
   - If `self._config.dry_run is True`:
     - Log the full `PositionRecord` fields via `structlog` at INFO level (`"position_tracker.dry_run_record"`, including condition_id, token_id, status, entry_price, order_size_usdc, kelly_fraction).
     - Return the `PositionRecord` Pydantic model.
     - **CRITICAL:** Do NOT create an `AsyncSession`. Do NOT instantiate `PositionRepository`. Zero DB writes.

6. **Live persist:**
   - Open a session: `async with self._db_session_factory() as session:`.
   - Instantiate `PositionRepository(session)`.
   - Build ORM `Position` instance from the `PositionRecord` fields.
   - Call `await repo.insert_position(position_orm)`.
   - `await session.commit()`.
   - Log `"position_tracker.position_recorded"` at INFO level.
   - Return the `PositionRecord`.

### Step 8 — Update Orchestrator Wiring

Target:
- `src/orchestrator.py`

Requirements:
1. Import `PositionTracker` from `src.agents.execution.position_tracker`.
2. Construct `PositionTracker` in `Orchestrator.__init__()`, after `ExecutionRouter`:
   ```python
   self.position_tracker = PositionTracker(
       config=self.config,
       db_session_factory=AsyncSessionLocal,
   )
   ```
3. `PositionTracker` is constructed regardless of `dry_run` mode — the tracker enforces the write gate internally.
4. In `_execution_consumer_loop()`, add the `record_execution()` call **after** `ExecutionRouter.route()` would return and **before** broadcast. The tracker call must be inside the existing `try/except` block so failures are caught:
   ```python
   # After route result is obtained:
   try:
       await self.position_tracker.record_execution(
           result=execution_result,
           condition_id=str(market_context.condition_id),
           token_id=str(market_context.condition_id),
       )
   except Exception as exc:
       logger.error("execution.position_tracking_error", error=str(exc))
   ```
5. **CRITICAL:** A `record_execution()` failure must NOT block or abort the broadcast path. It is fire-and-forget safe.
6. No other orchestrator changes — queue topology, task structure, and pipeline order remain unchanged.

### Step 9 — Update Existing Tests (If Needed)

Target:
- `tests/integration/test_orchestrator.py`
- `tests/integration/test_pipeline_e2e.py`
- `tests/integration/test_alembic_migrations.py`

Requirements:
1. Existing orchestrator tests must account for the new `PositionTracker` wiring.
2. If any existing test constructs an `Orchestrator` directly, it must now work with `PositionTracker` present.
3. Alembic migration test should verify that `alembic upgrade head` succeeds with the new `0002` migration.
4. All existing test assertions must continue to pass — zero behavioral regression.

### Step 10 — GREEN Validation

Run:
```bash
pytest tests/unit/test_position_tracker.py -v
pytest tests/integration/test_position_tracker_integration.py -v
pytest tests/unit/test_execution_router.py -v
pytest tests/integration/test_execution_router_integration.py -v
pytest tests/integration/test_orchestrator.py -v
pytest tests/integration/test_alembic_migrations.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. `PositionTracker` is a downstream consumer only — it reads `ExecutionResult` and persists, never recalculates Kelly sizing or modifies routing.
2. `PositionTracker` is isolated — zero imports from context, prompt, evaluation, ingestion, or market-data modules.
3. `LLMEvaluationResponse` remains the terminal pre-execution Gatekeeper — `PositionTracker` operates strictly downstream of the validated `ExecutionResult`.
4. `dry_run=True` blocks ALL DB writes — no session creation, no repository instantiation. Enforced by early-return guard BEFORE any DB interaction.
5. All financial fields in `PositionRecord` are `Decimal` — `float` rejected at Pydantic boundary via `field_validator`.
6. All financial columns in `positions` table are `Numeric(precision=38, scale=18)` — NEVER `Float`.
7. `PositionRepository` follows `ExecutionRepository` pattern exactly — constructor takes `AsyncSession`, all methods async, no raw SQL outside repo.
8. `PositionTracker` writes `OPEN` and `FAILED` only — `CLOSED` is NEVER written in WI-17. `update_status()` is provided for WI-19 but NEVER called by `PositionTracker`.
9. `SKIP` results produce no position record, no log, no side effect.
10. A `record_execution()` failure in the consumer loop must NOT block or abort the broadcast path.
11. No queue topology changes; preserve async 4-layer pipeline order.
12. `ExecutionRouter`, `PolymarketClient`, `BankrollSyncProvider`, `TransactionSigner` internals are unmodified.
13. `positions` table has NO foreign keys to `execution_txs` or `agent_decision_logs` — standalone by design.

---

## Required Test Matrix

At minimum, WI-17 tests must prove:

1. `PositionRecord` rejects `float` for `entry_price` — parametrized across all 5 financial fields.
2. `PositionRecord` accepts `Decimal` for each financial field.
3. `PositionStatus` enum has values `OPEN`, `CLOSED`, `FAILED`.
4. `record_execution()` with `EXECUTED` returns `PositionRecord(status=OPEN)`.
5. `record_execution()` with `DRY_RUN` returns `PositionRecord(status=OPEN)`.
6. `record_execution()` with `FAILED` returns `PositionRecord(status=FAILED)`.
7. `record_execution()` with `SKIP` returns `None`.
8. **[CRITICAL] `dry_run=True` — zero DB writes:** mock on `db_session_factory` asserts call count == 0. No `PositionRepository` instantiated.
9. **[CRITICAL] `dry_run=True` — structured log emitted:** capture `structlog` output and assert position fields are logged.
10. `dry_run=False` + `EXECUTED` calls `PositionRepository.insert_position()` with `status="OPEN"`.
11. `dry_run=False` + `FAILED` calls `PositionRepository.insert_position()` with `status="FAILED"`.
12. `FAILED` result with `None` financial fields uses `Decimal("0")` sentinels for all 5 fields.
13. Unreachable state `EXECUTED` + `dry_run=True` logs error and returns `None`.
14. Unreachable state `DRY_RUN` + `dry_run=False` logs error and returns `None`.
15. Import-boundary test: `position_tracker.py` has no dependency on context/evaluation/ingestion modules.
16. `PositionRepository.insert_position()` round-trips all fields via real async SQLite (integration).
17. `PositionRepository.get_open_by_condition_id()` filters correctly (integration).
18. `PositionRepository.get_open_positions()` returns only `OPEN` records (integration).
19. `PositionRepository.update_status()` transitions `OPEN` → `CLOSED` (integration).
20. Full flow `EXECUTED` → `record_execution()` → `get_open_positions()` returns the new record (integration).
21. Full flow `FAILED` → `record_execution()` → record has `status=FAILED` (integration).
22. Full suite regression: `pytest --asyncio-mode=auto tests/ -q` passes, coverage >= 80%.

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
You are the MAAP Checker for WI-17 (Position Tracker) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi17.md
2) docs/archive/ARCHIVE_PHASE_5.md invariants
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Decimal violations (any float usage in PositionRecord financial fields, ORM Numeric columns, or PositionTracker mapping logic)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation upstream)
- Business logic drift (deviation from WI-17 scope — no exit logic, no PnL, no CLOSED writes, no broadcast)
- dry_run safety violations (any DB session creation, repository instantiation, or DB write when dry_run=True)
- Isolation violations (PositionTracker importing from context, prompt, evaluation, or ingestion modules)
- Repository pattern violations (raw SQL outside PositionRepository, session lifecycle owned by repository instead of caller)

Additional required checks:
- PositionStatus enum exists in src/schemas/execution.py with values OPEN, CLOSED, FAILED
- PositionRecord is frozen Pydantic model with field_validator rejecting float on all 5 financial fields
- Position ORM model in src/db/models.py uses Numeric(38,18) for all financial columns — NEVER Float
- positions table has NO foreign keys to execution_txs or agent_decision_logs
- Alembic migration 0002_add_positions_table.py creates positions table with 3 indexes; parented on 0001
- PositionRepository exists in src/db/repositories/position_repo.py with 5 methods (insert_position, get_by_id, get_open_by_condition_id, get_open_positions, update_status)
- PositionRepository is exported from src/db/repositories/__init__.py
- PositionTracker exists in src/agents/execution/position_tracker.py
- record_execution() is async, accepts (ExecutionResult, str, str), returns PositionRecord | None
- record_execution() returns None for SKIP — no log, no DB write
- EXECUTED maps to OPEN; DRY_RUN maps to OPEN; FAILED maps to FAILED
- FAILED results with None financial fields use Decimal("0") sentinels
- Unreachable states (EXECUTED+dry_run, DRY_RUN+live) log error and return None
- dry_run=True early-return guard fires BEFORE any session/repository creation
- PositionTracker constructed in Orchestrator.__init__() regardless of dry_run
- record_execution() called in _execution_consumer_loop() after ExecutionRouter.route() and before broadcast
- record_execution() failure does NOT block broadcast path
- PositionTracker NEVER calls update_status() (CLOSED writes are WI-19)
- No modification to ExecutionRouter, PolymarketClient, BankrollSyncProvider, or TransactionSigner internals
- No new send/broadcast/approve/transfer capability introduced

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-17/invariants
4) Explicit statement on each MAAP critical category:
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Business logic drift: CLEARED/FLAGGED
   - dry_run safety violations: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
   - Repository pattern violations: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
