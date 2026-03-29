# WI-17 Business Logic — Position Tracker (Persist Execution Outcomes as Position Records)

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — `PositionTracker` is async; DB session creation and repository calls are awaited. Queue topology (`market_queue -> prompt_queue -> execution_queue`) is unchanged.
- `.agents/rules/db-engineer.md` — `PositionRepository` follows the existing Repository pattern (`ExecutionRepository` reference); `positions` table is Alembic-managed with `Numeric(38,18)` columns for all financial fields.
- `.agents/rules/risk-auditor.md` — all financial fields in `PositionRecord` are `Decimal`; no `float` intermediary in any money path. `Decimal("0")` sentinels for FAILED records with missing financial data.
- `.agents/rules/security-auditor.md` — `dry_run=True` blocks all DB writes; the tracker logs the would-be record via `structlog` only. No credentials or private keys appear in position logs.
- `.agents/rules/test-engineer.md` — WI-17 position tracking behavior requires unit + integration coverage; full suite remains >= 80%.

## 1. Objective

Introduce `PositionTracker`, the component that persists execution outcomes from `ExecutionRouter.route()` as typed `PositionRecord` entries in a new `positions` table, providing lifecycle visibility into open, closed, and failed positions.

WI-17 is the first work item that writes execution outcomes to the database. After WI-16, the system can route a validated BUY decision into a sized, slippage-checked, optionally signed order payload — but once `ExecutionRouter.route()` returns an `ExecutionResult`, no component tracks what happened. There is no persistent record of which markets have open exposure, what entry price was locked, or how much capital is committed. WI-17 closes this gap. It does not implement exit logic (deferred to WI-19), PnL calculation, or position broadcasting.

## 2. Scope Boundaries

### In Scope

1. New `PositionStatus` enum: `OPEN | CLOSED | FAILED` — lifecycle states for tracked positions.
2. New `PositionRecord` Pydantic model: frozen, Decimal-validated schema capturing the execution snapshot at routing time.
3. New SQLAlchemy `Position` ORM model: `positions` table with `Numeric(38,18)` financial columns and composite indexes.
4. New Alembic migration: `0002_add_positions_table.py` parented on `0001`.
5. New `PositionRepository` class: async repository with session injection, following `ExecutionRepository` pattern exactly.
6. New `PositionTracker` class: async component that converts `ExecutionResult` into a persisted `PositionRecord` via `PositionRepository`.
7. Orchestrator wiring: `PositionTracker` constructed in `__init__()` and called after `ExecutionRouter.route()` in `_execution_consumer_loop()`.
8. `dry_run=True` enforcement: structured log only, zero DB writes, zero session creation.

### Out of Scope

1. Exit / close logic — position lifecycle transitions beyond initial recording are WI-19 scope.
2. PnL calculation or mark-to-market — requires live repricing infrastructure not yet available.
3. Position broadcasting or external notification — no external side effects in WI-17.
4. Portfolio-level aggregation queries into `BankrollPortfolioTracker` — future WI scope.
5. Modifications to `ExecutionRouter` internals — WI-17 consumes `ExecutionResult` as a read-only input.
6. Modifications to `LLMEvaluationResponse` schema or Gatekeeper validation logic.
7. Foreign keys between `positions` and `execution_txs` / `agent_decision_logs` — position lifecycle must not couple to broadcast lifecycle.

## 3. Target Component Architecture + Data Contracts

### 3.1 Position Tracker Component (New Class)

- **Module:** `src/agents/execution/position_tracker.py`
- **Class Name:** `PositionTracker` (exact)
- **Responsibility:** convert `ExecutionResult` into a `PositionRecord`, persist via `PositionRepository` when live, log-only when dry_run.

Isolation rule:
- `PositionTracker` must remain execution-layer only. It must not depend on context-building, prompt logic, evaluation logic, or ingestion modules.
- `PositionTracker` receives only typed `ExecutionResult` — never raw LLM outputs, evaluation prompts, or `MarketContext` objects.

### 3.2 Position Repository Component (New Class)

- **Module:** `src/db/repositories/position_repo.py`
- **Class Name:** `PositionRepository` (exact)
- **Responsibility:** CRUD operations on the `positions` table via injected `AsyncSession`.

Pattern rule:
- Follows `ExecutionRepository` pattern exactly: constructor takes `AsyncSession`, all methods are `async`, repository owns no connection lifecycle.
- Exported from `src/db/repositories/__init__.py` alongside existing repositories.

### 3.3 Data Contracts (Required)

Position boundary must use typed contracts (Pydantic at boundary is required). Minimum contracts:

1. `PositionStatus` (enum in `src/schemas/execution.py`)
   - `OPEN` — committed or simulated exposure
   - `CLOSED` — reserved for WI-19; WI-17 never writes this status
   - `FAILED` — routing failed; recorded for audit, represents zero exposure

2. `PositionRecord` (Pydantic model in `src/schemas/execution.py`)
   - `id`: `str` (UUID4, generated at creation)
   - `condition_id`: `str` (Polymarket market identifier)
   - `token_id`: `str` (YES token ID from MarketContext)
   - `status`: `PositionStatus`
   - `side`: `str` (always `"BUY"` — WI-17 is BUY-only, mirrors ExecutionRouter)
   - `entry_price`: `Decimal` (midpoint_probability at routing time)
   - `order_size_usdc`: `Decimal` (USDC committed to this position)
   - `kelly_fraction`: `Decimal` (scaled Kelly fraction used for sizing)
   - `best_ask_at_entry`: `Decimal` (best_ask snapshot at routing time)
   - `bankroll_usdc_at_entry`: `Decimal` (bankroll balance at routing time)
   - `execution_action`: `ExecutionAction` (original router outcome)
   - `reason`: `str | None` (failure/skip reason if applicable)
   - `routed_at_utc`: `datetime` (timestamp from `ExecutionResult`)
   - `recorded_at_utc`: `datetime` (timestamp when record was persisted)

Hard rules:
- All five financial fields (`entry_price`, `order_size_usdc`, `kelly_fraction`, `best_ask_at_entry`, `bankroll_usdc_at_entry`) are `Decimal`. No `float` intermediary.
- `float` inputs in financial fields are rejected at schema boundary via `field_validator`, identical to `ExecutionResult._reject_float_financials`.
- Model is frozen (immutable after creation).

3. SQLAlchemy `Position` ORM model (in `src/db/models.py`)
   - Table name: `positions`
   - Financial columns: `Numeric(precision=38, scale=18)` — never `Float`
   - Indexes: `ix_positions_condition_id`, `ix_positions_status`, `ix_positions_condition_id_status` (composite)
   - No foreign keys to other tables — standalone by design

## 4. Core Method Contracts (async, typed)

### 4.1 PositionTracker — Async Record Entry Point

Required public method:

- `record_execution(result: ExecutionResult, condition_id: str, token_id: str) -> PositionRecord | None` (async)

Behavior requirements:

1. **SKIP gate:** if `result.action == ExecutionAction.SKIP`, return `None` immediately. No log, no DB write. SKIPs represent non-BUY or low-confidence decisions with no position semantics.
2. **Status derivation:** map `ExecutionAction` to `PositionStatus`:
   - `EXECUTED` → `OPEN`
   - `DRY_RUN` → `OPEN`
   - `FAILED` → `FAILED`
3. **Field mapping:** build `PositionRecord` from `ExecutionResult` fields (see §4.3).
4. **dry_run gate:** if `config.dry_run is True`, log the full would-be `PositionRecord` via `structlog` at INFO level, return the Pydantic model. Do NOT open a DB session, do NOT call any repository method.
5. **Live persist:** if `config.dry_run is False`, open a session from the injected factory, instantiate `PositionRepository`, call `insert_position()`, return the resulting `PositionRecord`.
6. **Unreachable state guards:** `EXECUTED` + `dry_run=True` and `DRY_RUN` + `dry_run=False` are unreachable by `ExecutionRouter` contract. If encountered, log error and return `None`.

### 4.2 PositionRepository — Async CRUD Methods

Required methods:

1. `insert_position(position: PositionORM) -> PositionORM` (async) — persist a new record, return flushed ORM instance.
2. `get_by_id(position_id: str) -> PositionORM | None` (async) — fetch by primary key.
3. `get_open_by_condition_id(condition_id: str) -> list[PositionORM]` (async) — return all `OPEN` positions for a given market.
4. `get_open_positions() -> list[PositionORM]` (async) — return all positions with `status = OPEN`.
5. `update_status(position_id: str, *, new_status: PositionStatus) -> PositionORM | None` (async) — transition status and return updated ORM or `None`. WI-17 does not call this method; provided for WI-19.

### 4.3 Field Mapping (ExecutionResult → PositionRecord)

| PositionRecord field | Source |
|---|---|
| `id` | `uuid4()` |
| `condition_id` | `condition_id` parameter |
| `token_id` | `token_id` parameter |
| `status` | Derived from `result.action` per §4.1 step 2 |
| `side` | `"BUY"` (hardcoded; WI-17 is BUY-only) |
| `entry_price` | `result.midpoint_probability` |
| `order_size_usdc` | `result.order_size_usdc` |
| `kelly_fraction` | `result.kelly_fraction` |
| `best_ask_at_entry` | `result.best_ask` |
| `bankroll_usdc_at_entry` | `result.bankroll_usdc` |
| `execution_action` | `result.action` |
| `reason` | `result.reason` |
| `routed_at_utc` | `result.routed_at_utc` |
| `recorded_at_utc` | `datetime.now(timezone.utc)` |

Hard rule:
- For `FAILED` results where financial fields are `None` (failure before Kelly sizing), use `Decimal("0")` sentinel values for the required Decimal fields.

### 4.4 Alembic Migration (Required)

- **Revision file:** `migrations/versions/0002_add_positions_table.py`
- **Parent:** `0001` (initial schema)
- **Upgrade:** `op.create_table("positions", ...)` with all columns and indexes from §3.3
- **Downgrade:** `op.drop_table("positions")`

## 5. Pipeline Integration Design

WI-17 integration point is within the execution consumer loop, between `ExecutionRouter.route()` (WI-16) and the broadcast path:

```
execution_consumer_loop:
  1. Dequeue item from execution_queue
  2. Extract LLMEvaluationResponse + MarketContext from item
  3. ExecutionRouter.route(response, market_context) → ExecutionResult   (WI-16)
  4. PositionTracker.record_execution(result, condition_id, token_id)    ← WI-17 (here)
  5. If EXECUTED and not dry_run, proceed to broadcast (existing path)
```

Note: The tracker call is fire-and-forget safe — a failed `record_execution()` is caught by the existing `except Exception` handler. A position write failure must never block or abort the broadcast path.

### 5.1 Constructor Dependencies (Injected)

`PositionTracker.__init__` receives:

1. `config: AppConfig` — `dry_run` flag for write gating.
2. `db_session_factory: async_sessionmaker[AsyncSession]` — injected session factory for repository construction.

`PositionTracker` is constructed in `Orchestrator.__init__()` regardless of `dry_run` mode — the tracker itself enforces the write gate internally.

### 5.2 Failure Semantics (Fail Open for Broadcast Path)

Unlike `ExecutionRouter` (which is fail-closed), `PositionTracker` failures must not block execution:

1. If `record_execution()` raises, the exception is caught by the consumer loop.
2. The broadcast path proceeds as if the position was recorded.
3. A structured error log is emitted with the failure reason.

This is deliberate: position persistence is an audit concern, not a safety gate. The safety gates are upstream (Gatekeeper, slippage guard, dry_run).

### 5.3 dry_run Behavior

When `config.dry_run is True`:

1. `record_execution()` builds the full `PositionRecord` Pydantic model.
2. The record is logged via `structlog` at INFO level (condition_id, token_id, status, entry_price, order_size_usdc, kelly_fraction).
3. No `AsyncSession` is created. No `PositionRepository` is instantiated. Zero DB writes.
4. This is enforced by an early-return guard at the top of the write path, not by a downstream check.
5. The Pydantic `PositionRecord` is returned to the caller.

### 5.4 Tracker Isolation Rule

The `PositionTracker` module must not:

1. Import or call prompt construction, context-building, or ingestion modules.
2. Import or call evaluation logic (`ClaudeClient`, `GrokClient`).
3. Accept raw `LLMEvaluationResponse` or `MarketContext` objects — only typed `ExecutionResult`.
4. Call `PositionRepository.update_status()` — closing positions is WI-19 scope.

Allowed imports:
- `src/core/config` (`AppConfig`)
- `src/core/exceptions` (if new exception types are needed)
- `src/schemas/execution` (`ExecutionResult`, `ExecutionAction`, `PositionRecord`, `PositionStatus`)
- `src/db/repositories/position_repo` (`PositionRepository`)
- `structlog`, `uuid`, `datetime`, `decimal` (stdlib / logging)

## 6. Invariants Preserved

1. `LLMEvaluationResponse` remains the terminal pre-execution Gatekeeper — `PositionTracker` operates strictly downstream of the validated `ExecutionResult`.
2. Kelly formula parameters and `ExecutionRouter` sizing logic are unchanged — `PositionTracker` reads the result, never recalculates.
3. `Decimal` financial-integrity rules remain mandatory for all position record fields. No `float` in any money path.
4. Async 4-layer queue topology remains unchanged — `PositionTracker` lives within Layer 4 (Execution).
5. `dry_run=True` continues to block all Layer 4 DB writes; position record is logged but never persisted.
6. Repository pattern is extended, not modified — `PositionRepository` follows the same `ExecutionRepository` contract.
7. `PolymarketClient`, `BankrollSyncProvider`, `TransactionSigner`, and `ExecutionRouter` internals are unmodified — zero coupling beyond consuming `ExecutionResult`.
8. WI-17 writes `OPEN` and `FAILED` only — `CLOSED` status is never written by `PositionTracker` (reserved for WI-19).

## 7. Strict Acceptance Criteria (Maker Agent)

1. `PositionTracker` is the canonical tracking class in `src/agents/execution/position_tracker.py`.
2. `record_execution(result, condition_id, token_id) -> PositionRecord | None` is the sole public async entry point.
3. `PositionStatus` enum exists in `src/schemas/execution.py` with values `OPEN`, `CLOSED`, `FAILED`.
4. `PositionRecord` Pydantic model exists with all fields from §3.3, frozen, Decimal-validated.
5. `float` values are rejected by `PositionRecord` field validators for all five financial fields.
6. SQLAlchemy `Position` ORM model exists in `src/db/models.py` with `Numeric(38,18)` columns.
7. Alembic migration `0002_add_positions_table.py` creates the `positions` table with all indexes; `alembic upgrade head` succeeds on a fresh database.
8. `PositionRepository` exists in `src/db/repositories/position_repo.py` with all five methods from §4.2.
9. `PositionRepository` is exported from `src/db/repositories/__init__.py`.
10. `record_execution()` returns `PositionRecord(status=OPEN)` for `EXECUTED` and `DRY_RUN` results.
11. `record_execution()` returns `PositionRecord(status=FAILED)` for `FAILED` results.
12. `record_execution()` returns `None` for `SKIP` results.
13. `dry_run=True` produces zero DB writes and zero session creation; only structured log output.
14. `dry_run=False` + `EXECUTED` persists an `OPEN` record via `PositionRepository.insert_position()`.
15. `dry_run=False` + `FAILED` persists a `FAILED` record via repository.
16. `FAILED` results with `None` financial fields use `Decimal("0")` sentinels.
17. `PositionTracker` is constructed in `Orchestrator.__init__()`.
18. `record_execution()` is called in `_execution_consumer_loop()` after `ExecutionRouter.route()` and before broadcast.
19. A `record_execution()` failure does not prevent the broadcast path from proceeding.
20. `PositionTracker` has zero imports from prompt, context, evaluation, ingestion, or market-data modules.
21. `PositionTracker` never calls `PositionRepository.update_status()`.
22. Full regression remains green (`pytest --asyncio-mode=auto tests/`) with coverage >= 80%.

## 8. Verification Checklist

1. Unit test: `PositionRecord` rejects `float` for each of the 5 financial fields (parametrized).
2. Unit test: `PositionRecord` accepts `Decimal` for each financial field.
3. Unit test: `record_execution()` maps `EXECUTED` → `PositionStatus.OPEN`.
4. Unit test: `record_execution()` maps `DRY_RUN` → `PositionStatus.OPEN`.
5. Unit test: `record_execution()` maps `FAILED` → `PositionStatus.FAILED`.
6. Unit test: `record_execution()` returns `None` for `SKIP`.
7. Unit test: `dry_run=True` path does not instantiate repository and does not open DB session.
8. Unit test: `dry_run=True` path emits structured log with all position fields.
9. Unit test: `FAILED` result with `None` financial fields populates `Decimal("0")` sentinels.
10. Unit test: unreachable states (`EXECUTED` + `dry_run=True`, `DRY_RUN` + `dry_run=False`) log error and return `None`.
11. Integration test: `PositionRepository.insert_position()` round-trips all fields via real async SQLite.
12. Integration test: `PositionRepository.get_open_by_condition_id()` filters correctly.
13. Integration test: `PositionRepository.get_open_positions()` returns only `OPEN` records.
14. Integration test: `PositionRepository.update_status()` transitions `OPEN` → `CLOSED`.
15. Integration test: full flow — `ExecutionResult(EXECUTED)` → `record_execution()` → `get_open_positions()` returns the new record.
16. Integration test: full flow — `ExecutionResult(FAILED)` → `record_execution()` → record has `status=FAILED`.
17. Integration test: `PositionTracker` module has no dependency on context/evaluation/ingestion modules (import boundary check).
18. Full suite:
    - `pytest --asyncio-mode=auto tests/`
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m`
