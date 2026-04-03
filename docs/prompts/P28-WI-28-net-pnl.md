# P28-WI-28 — Net PnL & Fee Accounting Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi28-net-pnl` (branched from current `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/risk-auditor.md`
- `.agents/rules/db-engineer.md`
- `.agents/rules/async-architect.md`
- `.agents/rules/security-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-28 for Phase 9: an additive accounting extension to WI-21 that adds gas and fee tracking to position settlement so the system can report true Net PnL after transaction costs.

Today, `PnLCalculator.settle()` computes gross realized PnL only:

```python
gross_realized_pnl = (exit_price - entry_price) * position_size_tokens
```

WI-28 adds two explicit cost inputs — `gas_cost_usdc` and `fees_usdc` — and derives:

```python
net_realized_pnl = gross_realized_pnl - gas_cost_usdc - fees_usdc
```

This WI is accounting-focused and intentionally additive. The existing `realized_pnl` column retains its WI-21 gross PnL semantics for backward compatibility. `net_realized_pnl` is derived deterministically from three persisted primitives and is **NOT** stored as a new DB column — it is computed at read time in schemas and reporting.

---

## Objective & Scope

### In Scope
1. Add nullable `gas_cost_usdc` and `fees_usdc` (`Numeric(38,18)`) columns to the `positions` table via Alembic migration `0004`.
2. Extend `Position` ORM model, `PositionRecord` Pydantic schema, and `PnLRecord` schema with the new fee fields.
3. Update `PnLCalculator.settle()` to accept optional gas/fee inputs and compute fee-adjusted Net PnL.
4. Extend `PositionRepository.record_settlement()` to persist gas and fee values.
5. Extend `PositionLifecycleEntry` and `LifecycleReport` in `src/schemas/risk.py` with fee-aware reporting fields.
6. Update `PositionLifecycleReporter` to coalesce legacy `NULL` fee fields to `Decimal("0")` and derive net PnL.

### Out of Scope
1. Live gas estimation or on-chain gas-to-USDC conversion.
2. Live Polymarket fee schedule lookup.
3. Changes to `ExecutionRouter`, `ExitOrderRouter`, or any routing logic.
4. New alert thresholds or drawdown policy changes based on net PnL.
5. Backfilling historical rows with gas/fee data.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi28.md`
4. `src/agents/execution/pnl_calculator.py` — **primary target**
5. `src/schemas/position.py` — **target: add `gas_cost_usdc`, `fees_usdc` fields + float rejection**
6. `src/schemas/execution.py` — **target: extend `PnLRecord` with fee-aware fields**
7. `src/schemas/risk.py` — **target: extend `PositionLifecycleEntry` + `LifecycleReport`**
8. `src/db/models.py` — **target: extend `Position` ORM with two nullable columns**
9. `src/db/repositories/position_repository.py` — **target: extend `record_settlement()` signature**
10. `src/agents/execution/lifecycle_reporter.py` — **target: update aggregation to surface fee-aware totals**
11. `migrations/versions/0003_add_pnl_columns.py` — **context: parent revision for migration chain**
12. `src/core/config.py` — **context only; no new config fields required for WI-28**
13. `src/orchestrator.py` — **context only; WI-28 does NOT alter orchestrator wiring**
14. Existing test files (verify no regression):
    - `tests/unit/test_pnl_calculator.py`
    - `tests/integration/test_pnl_settlement_integration.py`
    - `tests/unit/test_schemas.py`
    - `tests/unit/test_lifecycle_reporter.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL INVARIANT: Strict Decimal Math

Every monetary field and every arithmetic step MUST use `Decimal`. `float` anywhere in gas, fee, gross PnL, or net PnL handling is a bug. Missing legacy values (`NULL` from pre-WI-28 rows) MUST normalize to `Decimal("0")` to prevent breaking historical rows.

The non-negotiable backward-compatibility rule:

```python
# For any pre-WI-28 position where gas_cost_usdc=NULL and fees_usdc=NULL:
legacy_net_realized_pnl == legacy_realized_pnl
```

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code. No implementation code can be written until the failing tests are committed and verified.

---

## Phase 1: Test Suite (RED Phase)

Create two new test files. All tests MUST fail (RED) before any production code is modified.

### Step 1.1 — Create `tests/unit/test_wi28_net_pnl.py`

Write unit tests covering the following behaviors:

**A. Schema float rejection on new fee fields:**

1. `PositionRecord(gas_cost_usdc=1.5, ...)` raises `ValidationError` — float rejected.
2. `PositionRecord(fees_usdc=0.25, ...)` raises `ValidationError` — float rejected.
3. `PositionRecord(gas_cost_usdc=Decimal("1.5"), fees_usdc=Decimal("0.25"), ...)` succeeds.
4. `PositionRecord(gas_cost_usdc=None, fees_usdc=None, ...)` succeeds — nullable defaults preserved.
5. `PnLRecord(gas_cost_usdc=1.0, ...)` raises `ValidationError` — float rejected.
6. `PnLRecord(fees_usdc=0.5, ...)` raises `ValidationError` — float rejected.
7. `PnLRecord(gas_cost_usdc=Decimal("1.0"), fees_usdc=Decimal("0.5"), net_realized_pnl=Decimal("3.5"), ...)` succeeds.
8. `PositionLifecycleEntry(gas_cost_usdc=1.0, ...)` raises `ValidationError` — float rejected.
9. `PositionLifecycleEntry(fees_usdc=0.5, ...)` raises `ValidationError` — float rejected.
10. `LifecycleReport(total_gas_cost_usdc=1.0, ...)` raises `ValidationError` — float rejected.
11. `LifecycleReport(total_fees_usdc=0.5, ...)` raises `ValidationError` — float rejected.
12. `LifecycleReport(total_net_realized_pnl=3.0, ...)` raises `ValidationError` — float rejected.

**B. `PnLCalculator.settle()` net PnL formula:**

13. **Explicit gas + fees:** `settle(position, exit_price=Decimal("0.70"), gas_cost_usdc=Decimal("0.50"), fees_usdc=Decimal("0.25"))` — assert `pnl_record.realized_pnl` equals gross value, `pnl_record.gas_cost_usdc == Decimal("0.50")`, `pnl_record.fees_usdc == Decimal("0.25")`, and `pnl_record.net_realized_pnl == gross - Decimal("0.50") - Decimal("0.25")`.
14. **None defaults (legacy compatibility):** `settle(position, exit_price=Decimal("0.70"))` with no gas/fee args — assert `pnl_record.gas_cost_usdc == Decimal("0")`, `pnl_record.fees_usdc == Decimal("0")`, `pnl_record.net_realized_pnl == pnl_record.realized_pnl`.
15. **Degenerate entry_price == 0:** `settle(position_with_zero_entry, exit_price=Decimal("0.70"), gas_cost_usdc=Decimal("1.0"), fees_usdc=Decimal("0.5"))` — assert `pnl_record.net_realized_pnl == Decimal("0") - Decimal("1.0") - Decimal("0.5")` (i.e., `Decimal("-1.5")`).
16. **Only gas, no fees:** `settle(position, exit_price=..., gas_cost_usdc=Decimal("2.0"), fees_usdc=None)` — assert `pnl_record.fees_usdc == Decimal("0")`, net adjusted by gas only.
17. **Only fees, no gas:** `settle(position, exit_price=..., gas_cost_usdc=None, fees_usdc=Decimal("1.0"))` — assert `pnl_record.gas_cost_usdc == Decimal("0")`, net adjusted by fees only.
18. **dry_run=True computes full record but does not persist:** Assert all fee-aware fields are populated on the returned `PnLRecord` when `dry_run=True`.

**C. Lifecycle reporting aggregation:**

19. `PositionLifecycleEntry` with `realized_pnl=Decimal("5.0")`, `gas_cost_usdc=Decimal("0.50")`, `fees_usdc=Decimal("0.25")` — assert `net_realized_pnl == Decimal("4.25")`.
20. `PositionLifecycleEntry` with `realized_pnl=None` (OPEN position) — assert `net_realized_pnl is None`.
21. `PositionLifecycleEntry` with `gas_cost_usdc=Decimal("0")`, `fees_usdc=Decimal("0")`, `realized_pnl=Decimal("3.0")` — assert `net_realized_pnl == Decimal("3.0")` (legacy-compatible identity).
22. `LifecycleReport` correctly sums `total_gas_cost_usdc`, `total_fees_usdc`, and `total_net_realized_pnl` across entries.
23. Empty report: `total_gas_cost_usdc == Decimal("0")`, `total_fees_usdc == Decimal("0")`, `total_net_realized_pnl == Decimal("0")`.

### Step 1.2 — Create `tests/integration/test_wi28_net_pnl_integration.py`

Write integration tests covering database interaction:

1. **Settlement persistence writes fee columns:** Insert an OPEN position, call `record_settlement(... gas_cost_usdc=Decimal("0.50"), fees_usdc=Decimal("0.25"))`, then reload from DB and assert `position.gas_cost_usdc == Decimal("0.50")` and `position.fees_usdc == Decimal("0.25")`.
2. **Legacy row compatibility:** Insert an OPEN position (no WI-28 columns set, both `NULL`), settle it via `record_settlement()` without passing gas/fee args, reload and assert `position.gas_cost_usdc` and `position.fees_usdc` are persisted as `Decimal("0")` (normalized from None).
3. **Pre-WI-28 row loads into lifecycle report:** Insert a CLOSED position row where `gas_cost_usdc IS NULL` and `fees_usdc IS NULL`, run `PositionLifecycleReporter.generate_report()`, assert the entry's `net_realized_pnl == realized_pnl` (legacy identity).
4. **Full settlement + report round-trip:** Insert position → settle with explicit gas/fees → generate lifecycle report → assert per-entry and aggregate totals match expected net PnL formula.
5. **dry_run=True does not persist fee columns:** Run `PnLCalculator.settle()` with `dry_run=True` and explicit gas/fees, assert the DB row remains unchanged (both fee columns still `NULL`/unchanged).
6. **Alembic migration round-trip:** Verify migration `0004` upgrade adds both columns and downgrade drops them. (Use `op.get_bind().execute(text("PRAGMA table_info(positions)"))` or equivalent introspection to validate column presence.)

### Step 1.3 — Run RED gate

```bash
.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi28_net_pnl.py tests/integration/test_wi28_net_pnl_integration.py -v
```

**All new tests MUST fail.** Commit the failing test suite:

```
git add tests/unit/test_wi28_net_pnl.py tests/integration/test_wi28_net_pnl_integration.py
git commit -m "test(wi28): add RED test suite for net PnL & fee accounting"
```

---

## Phase 2: Implementation (GREEN Phase)

Implement production code to make all RED tests pass. Execute steps in order.

### Step 2.1 — Alembic Migration

Create `migrations/versions/0004_add_fee_columns.py`:

```python
"""add gas and fee columns to positions

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-03 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column(
            "gas_cost_usdc",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
    )
    op.add_column(
        "positions",
        sa.Column(
            "fees_usdc",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("positions", "fees_usdc")
    op.drop_column("positions", "gas_cost_usdc")
```

### Step 2.2 — Extend `Position` ORM Model

In `src/db/models.py`, add two nullable columns to the `Position` class, placed after `closed_at_utc`:

```python
gas_cost_usdc: Mapped[Optional[Decimal]] = mapped_column(
    Numeric(precision=38, scale=18),
    nullable=True,
    comment="Polygon gas cost normalized into USDC at settlement time",
)
fees_usdc: Mapped[Optional[Decimal]] = mapped_column(
    Numeric(precision=38, scale=18),
    nullable=True,
    comment="Polymarket CLOB maker/taker fees in USDC at settlement time",
)
```

### Step 2.3 — Extend `PositionRecord` Schema

In `src/schemas/position.py`, add two optional fields to `PositionRecord`:

```python
gas_cost_usdc: Decimal | None = None
fees_usdc: Decimal | None = None
```

Add `"gas_cost_usdc"` and `"fees_usdc"` to the `@field_validator` decorator's field list alongside the existing nullable fields (`"realized_pnl"`, `"exit_price"`):

```python
@field_validator(
    "entry_price",
    "order_size_usdc",
    "kelly_fraction",
    "best_ask_at_entry",
    "bankroll_usdc_at_entry",
    "realized_pnl",
    "exit_price",
    "gas_cost_usdc",
    "fees_usdc",
    mode="before",
)
```

### Step 2.4 — Extend `PnLRecord` Schema

In `src/schemas/execution.py`, add three new fields to `PnLRecord`:

```python
gas_cost_usdc: Decimal
fees_usdc: Decimal
net_realized_pnl: Decimal
```

Add `"gas_cost_usdc"`, `"fees_usdc"`, and `"net_realized_pnl"` to the `@field_validator` decorator's field list:

```python
@field_validator(
    "entry_price",
    "exit_price",
    "order_size_usdc",
    "position_size_tokens",
    "realized_pnl",
    "gas_cost_usdc",
    "fees_usdc",
    "net_realized_pnl",
    mode="before",
)
```

### Step 2.5 — Extend `PositionLifecycleEntry` and `LifecycleReport`

In `src/schemas/risk.py`:

**`PositionLifecycleEntry`** — add:

```python
gas_cost_usdc: Decimal = Decimal("0")
fees_usdc: Decimal = Decimal("0")
net_realized_pnl: Decimal | None = None
```

Add `"gas_cost_usdc"` and `"fees_usdc"` to the non-nullable `_reject_float_financials` validator field list. Add `"net_realized_pnl"` to the nullable `_reject_float_nullable_financials` validator field list.

**`LifecycleReport`** — add:

```python
total_gas_cost_usdc: Decimal
total_fees_usdc: Decimal
total_net_realized_pnl: Decimal
```

Add all three to the existing `_reject_float_financials` validator field list.

### Step 2.6 — Extend `PositionRepository.record_settlement()`

In `src/db/repositories/position_repository.py`, update `record_settlement()`:

1. Add optional keyword parameters:
   ```python
   gas_cost_usdc: Decimal | None = None,
   fees_usdc: Decimal | None = None,
   ```
2. After setting the existing settlement fields, persist the normalized fee values:
   ```python
   position.gas_cost_usdc = gas_cost_usdc if gas_cost_usdc is not None else Decimal("0")
   position.fees_usdc = fees_usdc if fees_usdc is not None else Decimal("0")
   ```
3. Add the fee values to the `position.settlement_recorded` structlog event.

### Step 2.7 — Implement Fee-Aware `PnLCalculator.settle()`

In `src/agents/execution/pnl_calculator.py`, update the `settle()` method:

1. Add optional parameters to the signature:
   ```python
   async def settle(
       self,
       position: PositionRecord,
       exit_price: Decimal,
       gas_cost_usdc: Decimal | None = None,
       fees_usdc: Decimal | None = None,
   ) -> PnLRecord:
   ```

2. After computing `realized_pnl` (gross), add normalization and net computation:
   ```python
   normalized_gas_cost = Decimal(str(gas_cost_usdc)) if gas_cost_usdc is not None else _ZERO
   normalized_fees = Decimal(str(fees_usdc)) if fees_usdc is not None else _ZERO
   net_realized_pnl = realized_pnl - normalized_gas_cost - normalized_fees
   ```

3. Update the `PnLRecord` construction to include the new fields:
   ```python
   pnl_record = PnLRecord(
       position_id=str(position.id),
       condition_id=str(position.condition_id),
       entry_price=entry_price,
       exit_price=exit_price_decimal,
       order_size_usdc=order_size_usdc,
       position_size_tokens=position_size_tokens,
       realized_pnl=realized_pnl,
       gas_cost_usdc=normalized_gas_cost,
       fees_usdc=normalized_fees,
       net_realized_pnl=net_realized_pnl,
       closed_at_utc=closed_at_utc,
   )
   ```

4. Update the `pnl.calculated` structlog event to include:
   ```python
   gas_cost_usdc=str(normalized_gas_cost),
   fees_usdc=str(normalized_fees),
   net_realized_pnl=str(net_realized_pnl),
   ```

5. Update the `record_settlement()` call to pass through the fee values:
   ```python
   settled = await repo.record_settlement(
       position_id=pnl_record.position_id,
       realized_pnl=pnl_record.realized_pnl,
       exit_price=pnl_record.exit_price,
       closed_at_utc=pnl_record.closed_at_utc,
       gas_cost_usdc=pnl_record.gas_cost_usdc,
       fees_usdc=pnl_record.fees_usdc,
   )
   ```

6. **Do NOT modify the `dry_run` guard.** The existing gate computes everything and returns `pnl_record` before the persistence block. Fee-aware fields are populated on the returned record in both live and dry-run modes.

### Step 2.8 — Update `PositionLifecycleReporter`

In `src/agents/execution/lifecycle_reporter.py`:

1. **`_build_lifecycle_entries()`** — For each position, coalesce legacy `NULL` fee fields:
   ```python
   gas_cost = Decimal(str(position.gas_cost_usdc)) if position.gas_cost_usdc is not None else _ZERO
   fees = Decimal(str(position.fees_usdc)) if position.fees_usdc is not None else _ZERO
   ```

   Derive `net_realized_pnl` for settled positions only:
   ```python
   if position.realized_pnl is not None:
       realized_pnl_d = Decimal(str(position.realized_pnl))
       net_realized_pnl = realized_pnl_d - gas_cost - fees
   else:
       net_realized_pnl = None
   ```

   Pass `gas_cost_usdc=gas_cost`, `fees_usdc=fees`, `net_realized_pnl=net_realized_pnl` to the `PositionLifecycleEntry` constructor.

2. **`_compute_aggregate_statistics()`** — Extend the return tuple to include three additional aggregates:
   ```python
   total_gas_cost_usdc = _ZERO
   total_fees_usdc = _ZERO
   total_net_realized_pnl = _ZERO
   ```

   Inside the settled-position loop, accumulate:
   ```python
   gas_cost = Decimal(str(position.gas_cost_usdc)) if position.gas_cost_usdc is not None else _ZERO
   fees = Decimal(str(position.fees_usdc)) if position.fees_usdc is not None else _ZERO
   total_gas_cost_usdc += gas_cost
   total_fees_usdc += fees
   net_pnl = pnl_value - gas_cost - fees
   total_net_realized_pnl += net_pnl
   ```

   Return the three new values in the tuple.

3. **`generate_report()`** — Unpack the extended tuple and pass the new aggregate fields to the `LifecycleReport` constructor:
   ```python
   total_gas_cost_usdc=total_gas_cost_usdc,
   total_fees_usdc=total_fees_usdc,
   total_net_realized_pnl=total_net_realized_pnl,
   ```

   Also update the empty-report return to include:
   ```python
   total_gas_cost_usdc=_ZERO,
   total_fees_usdc=_ZERO,
   total_net_realized_pnl=_ZERO,
   ```

   Update the `lifecycle.report_generated` structlog event to include the three new aggregate values.

### Step 2.9 — Run GREEN gate

```bash
.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi28_net_pnl.py tests/integration/test_wi28_net_pnl_integration.py -v
```

**All new WI-28 tests MUST pass.** Commit the implementation:

```
git add migrations/versions/0004_add_fee_columns.py src/db/models.py src/schemas/position.py src/schemas/execution.py src/schemas/risk.py src/db/repositories/position_repository.py src/agents/execution/pnl_calculator.py src/agents/execution/lifecycle_reporter.py
git commit -m "feat(wi28): add net PnL & fee accounting to settlement path"
```

---

## Phase 3: Refactor & Regression

### Step 3.1 — Full regression

```bash
.venv/bin/pytest --asyncio-mode=auto tests/ -q
```

**ALL tests must pass** (target: 521 + new WI-28 tests). Fix any regressions before proceeding. Do not suppress or skip pre-existing tests.

### Step 3.2 — Coverage verification

```bash
.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m
```

Coverage MUST remain at or above **94%**. If coverage drops, add targeted tests for uncovered lines before proceeding.

### Step 3.3 — Regression commit

If any fixes were needed in Phase 3, commit them atomically:

```
git commit -m "fix(wi28): address regression findings from full test suite"
```

---

## Regression Gate Summary

| Gate | Command | Pass Criteria |
|---|---|---|
| RED | `.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi28_net_pnl.py tests/integration/test_wi28_net_pnl_integration.py -v` | All new tests FAIL |
| GREEN | Same command | All new tests PASS |
| Regression | `.venv/bin/pytest --asyncio-mode=auto tests/ -q` | ALL tests pass (521 + WI-28 additions) |
| Coverage | `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` | >= 94% |

---

## Definition of Done

Before declaring WI-28 complete:

1. All new WI-28 unit and integration tests pass GREEN.
2. Full regression suite passes with zero failures.
3. Coverage >= 94%.
4. `STATE.md` updated: test count, coverage, WI-28 marked complete.
5. `CLAUDE.md` updated: active WI status.
6. `README.md` updated if any new environment variables or commands are introduced (unlikely for WI-28).
7. Memory Consolidation executed.

---

## Files Modified (Summary)

| File | Change |
|---|---|
| `migrations/versions/0004_add_fee_columns.py` | **NEW** — Alembic migration adding `gas_cost_usdc` and `fees_usdc` to `positions` |
| `src/db/models.py` | Extend `Position` with 2 nullable `Numeric(38,18)` columns |
| `src/schemas/position.py` | Extend `PositionRecord` with 2 optional `Decimal` fields + float rejection |
| `src/schemas/execution.py` | Extend `PnLRecord` with `gas_cost_usdc`, `fees_usdc`, `net_realized_pnl` + float rejection |
| `src/schemas/risk.py` | Extend `PositionLifecycleEntry` (3 fields) + `LifecycleReport` (3 aggregate fields) + float rejection |
| `src/db/repositories/position_repository.py` | Extend `record_settlement()` signature with optional gas/fee params |
| `src/agents/execution/pnl_calculator.py` | Extend `settle()` with fee normalization + net PnL computation |
| `src/agents/execution/lifecycle_reporter.py` | Coalesce `NULL` fees, derive net PnL, compute aggregate fee totals |
| `tests/unit/test_wi28_net_pnl.py` | **NEW** — ~23 unit tests |
| `tests/integration/test_wi28_net_pnl_integration.py` | **NEW** — ~6 integration tests |

## Files NOT Modified

| File | Reason |
|---|---|
| `src/orchestrator.py` | WI-28 is Layer 4 accounting only; no new tasks, queues, or loop wiring |
| `src/core/config.py` | No new config fields required |
| `src/agents/execution/execution_router.py` | BUY routing unchanged |
| `src/agents/execution/exit_order_router.py` | SELL routing unchanged |
| `src/agents/execution/circuit_breaker.py` | Entry gate unchanged |
| `src/agents/execution/alert_engine.py` | Alert thresholds unchanged |
