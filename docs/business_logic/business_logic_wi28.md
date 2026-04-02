# WI-28 Business Logic — Net PnL & Fee Accounting

## Active Agents + Constraints

- `.agents/rules/risk-auditor.md` — All fee and PnL arithmetic is `Decimal`-only. No `float` in gas, fee, gross PnL, or net PnL paths. Any new schema validators must reject `float` and coerce via `Decimal(str(value))`.
- `.agents/rules/db-engineer.md` — Database work is additive only: one Alembic migration, two nullable columns on `positions`, and repository-mediated persistence only. No raw SQL and no direct `AsyncSession` use outside `PositionRepository`.
- `.agents/rules/async-architect.md` — WI-28 introduces no new queue, no new background task, and no new blocking I/O. `PnLCalculator` remains an on-demand Layer 4 accounting step inside the existing exit path.
- `.agents/rules/security-auditor.md` — `dry_run=True` must still compute and log the full fee-aware accounting result while performing zero DB writes. Existing settlement safety boundaries remain unchanged.
- `.agents/rules/test-engineer.md` — WI-28 requires unit + integration coverage for fee defaults, nullable historical-row compatibility, net PnL formula correctness, settlement persistence, and report serialization.

## 1. Objective

Extend WI-21 settlement accounting so the system can report true Net PnL after transaction costs.

Today, `PnLCalculator` computes gross realized PnL from entry and exit prices only:

```python
gross_realized_pnl = (exit_price - entry_price) * position_size_tokens
```

WI-28 adds two explicit cost inputs:

1. `gas_cost_usdc` — Polygon transaction cost normalized into USDC
2. `fees_usdc` — Polymarket CLOB maker/taker fees in USDC

The final Net PnL is then:

```python
net_realized_pnl = gross_realized_pnl - gas_cost_usdc - fees_usdc
```

This work item is accounting-focused and intentionally additive:

- `realized_pnl` remains the existing gross settlement value for backward compatibility and audit continuity
- gas and fee primitives are persisted on the `positions` row
- net PnL is derived deterministically from persisted fields and exposed in the risk/reporting layer
- missing historical gas/fee values default to `Decimal("0")` so existing rows remain valid without backfill

## 2. Scope Boundaries

### In Scope

1. Update `src/agents/execution/pnl_calculator.py` to accept optional gas/fee inputs and compute fee-adjusted Net PnL.
2. Update `src/schemas/risk.py` so lifecycle/risk reporting can surface fee-aware per-position and aggregate Net PnL.
3. Add Alembic migration `migrations/versions/0004_add_fee_columns.py`.
4. Add `gas_cost_usdc` and `fees_usdc` nullable columns to the `positions` table.
5. Extend the `Position` ORM model and `PositionRecord` schema to carry the new fee fields.
6. Extend `PositionRepository.record_settlement()` to persist gas and fee values alongside existing settlement fields.
7. Update read/reporting paths to normalize legacy `NULL` fee values to `Decimal("0")`.

### Out of Scope

1. Live gas estimation or on-chain gas-to-USDC conversion logic.
2. Live Polymarket fee schedule lookup or automatic maker/taker fee discovery.
3. New execution routing logic in `ExecutionRouter` or `ExitOrderRouter`.
4. Tax-lot accounting, FIFO/LIFO, or partial-fill fee allocation.
5. New alert thresholds or drawdown policy changes based on net PnL.
6. Backfilling old rows with historical gas/fee data.
7. Renaming or repurposing canonical existing classes.

## 3. Target Components + Data Contracts

### 3.1 Primary Target Components

#### A. `src/agents/execution/pnl_calculator.py`

`PnLCalculator` remains the canonical Layer 4 accounting component and gains two optional settlement inputs:

```python
async def settle(
    self,
    position: PositionRecord,
    exit_price: Decimal,
    gas_cost_usdc: Decimal | None = None,
    fees_usdc: Decimal | None = None,
) -> PnLRecord:
```

Required behavior:

1. Compute `gross_realized_pnl` exactly as WI-21 does today.
2. Normalize:
   - `normalized_gas_cost = gas_cost_usdc if gas_cost_usdc is not None else Decimal("0")`
   - `normalized_fees = fees_usdc if fees_usdc is not None else Decimal("0")`
3. Compute:

```python
net_realized_pnl = gross_realized_pnl - normalized_gas_cost - normalized_fees
```

4. Persist `realized_pnl` as gross PnL to preserve WI-21 storage semantics.
5. Persist `gas_cost_usdc` and `fees_usdc` on the `positions` row.
6. Return/log a fee-aware settlement record so the caller can audit both gross and net outcomes.
7. Preserve the existing `dry_run` write gate: compute everything, write nothing.

#### B. `src/schemas/risk.py`

`risk.py` is the operator-facing reporting surface for WI-28. It must be extended additively so reports can expose true fee-adjusted performance without silently changing old field semantics.

`PositionLifecycleEntry` additions:

```python
gas_cost_usdc: Decimal = Decimal("0")
fees_usdc: Decimal = Decimal("0")
net_realized_pnl: Decimal | None = None
```

Rules:

- `realized_pnl` remains the stored gross PnL field
- `net_realized_pnl` is derived as `realized_pnl - gas_cost_usdc - fees_usdc`
- `OPEN` positions keep `net_realized_pnl=None`
- `gas_cost_usdc` and `fees_usdc` must deserialize `None`/legacy `NULL` as `Decimal("0")` before any arithmetic

`LifecycleReport` additions:

```python
total_gas_cost_usdc: Decimal
total_fees_usdc: Decimal
total_net_realized_pnl: Decimal
```

Semantic rules:

- `total_realized_pnl` remains the existing gross aggregate
- `total_net_realized_pnl` is the new operator-facing net aggregate
- `total_gas_cost_usdc` and `total_fees_usdc` are explicit audit totals, not inferred by subtraction in downstream UI code
- existing fields stay additive and backward-compatible; WI-28 must not silently reinterpret old report fields

#### C. Alembic Migration

Create `migrations/versions/0004_add_fee_columns.py` with parent revision `0003`.

Migration requirements:

```python
gas_cost_usdc = sa.Column("gas_cost_usdc", sa.Numeric(precision=38, scale=18), nullable=True)
fees_usdc = sa.Column("fees_usdc", sa.Numeric(precision=38, scale=18), nullable=True)
```

Upgrade:

- add `gas_cost_usdc`
- add `fees_usdc`

Downgrade:

- drop `fees_usdc`
- drop `gas_cost_usdc`

Both columns are nullable so historical rows remain valid without rewrite or backfill.

### 3.2 Supporting Model/Schema Changes

The following supporting changes are required for the target components above to function correctly:

1. `src/db/models.py`
   - extend `Position` with:

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

2. `src/schemas/position.py`
   - extend `PositionRecord` with:

```python
gas_cost_usdc: Decimal | None = None
fees_usdc: Decimal | None = None
```

   - extend existing float-rejection validator to cover both new fields

3. `src/db/repositories/position_repository.py`
   - extend `record_settlement()` signature to accept optional `gas_cost_usdc` and `fees_usdc`
   - persist normalized `Decimal("0")` values when caller omits them

4. `src/agents/execution/lifecycle_reporter.py`
   - when reading rows, coalesce legacy `NULL` fee fields to `Decimal("0")`
   - derive `net_realized_pnl` per settled position
   - compute report totals for gas, fees, and net PnL

5. `src/schemas/execution.py`
   - if `PnLRecord` remains the `PnLCalculator` return contract, extend it additively with:

```python
gas_cost_usdc: Decimal
fees_usdc: Decimal
net_realized_pnl: Decimal
```

   - this keeps fee-aware settlement observable and testable at the accounting boundary

## 4. Core Logic

### 4.1 Canonical Formula

WI-28 keeps the existing gross PnL calculation and layers costs on top:

```python
entry_price = Decimal(str(position.entry_price))
order_size_usdc = Decimal(str(position.order_size_usdc))
exit_price_decimal = Decimal(str(exit_price))

if entry_price == Decimal("0"):
    position_size_tokens = Decimal("0")
else:
    position_size_tokens = order_size_usdc / entry_price

gross_realized_pnl = (exit_price_decimal - entry_price) * position_size_tokens
normalized_gas_cost = (
    Decimal(str(gas_cost_usdc))
    if gas_cost_usdc is not None
    else Decimal("0")
)
normalized_fees = (
    Decimal(str(fees_usdc))
    if fees_usdc is not None
    else Decimal("0")
)
net_realized_pnl = gross_realized_pnl - normalized_gas_cost - normalized_fees
```

### 4.2 Persistence Rules

Persisted on `positions`:

- `realized_pnl` = gross realized PnL
- `exit_price`
- `closed_at_utc`
- `gas_cost_usdc`
- `fees_usdc`

Not persisted as a new DB column in WI-28:

- `net_realized_pnl`

Rationale:

- Net PnL is a deterministic derivative of three persisted values
- storing the cost primitives preserves auditability and future recomputation
- avoiding a third settlement column keeps the migration minimal and historical-row compatibility simple

### 4.3 Historical Row Compatibility

For any position created before WI-28:

- `gas_cost_usdc` is `NULL`
- `fees_usdc` is `NULL`

All read paths must normalize both to `Decimal("0")` before arithmetic. Therefore:

```python
legacy_net_realized_pnl == legacy_realized_pnl
```

This is the non-negotiable backward-compatibility rule for WI-28.

### 4.4 Reporting Semantics

WI-28 is additive and audit-first:

- `realized_pnl` in stored position data remains gross
- `total_realized_pnl` in `LifecycleReport` remains the gross aggregate
- `net_realized_pnl` and `total_net_realized_pnl` are the new explicit fee-adjusted values
- operator-facing "true profitability" views must use the new net fields, not re-derive costs downstream

## 5. Invariants

1. **Strict `Decimal` math only**
   Every monetary field and every arithmetic step uses `Decimal`. `float` anywhere in gas, fee, gross PnL, or net PnL handling is a bug.

2. **Historical data must not break**
   Missing legacy `gas_cost_usdc` and `fees_usdc` values default to `Decimal("0")` on read and settlement persistence paths.

3. **Migration is additive**
   WI-28 adds two nullable columns only. No existing column is renamed, repurposed, or dropped.

4. **Gross PnL semantics remain stable**
   `positions.realized_pnl` keeps its WI-21 meaning: price-delta PnL before fees and gas.

5. **Net PnL is derived deterministically**
   `net_realized_pnl` must always equal `realized_pnl - gas_cost_usdc - fees_usdc` after normalization.

6. **`dry_run` behavior remains unchanged**
   Fee-aware accounting is computed in `dry_run`, but settlement persistence is still blocked before any DB mutation.

7. **Repository isolation remains mandatory**
   No agent or calculator writes settlement data through raw SQL or ad hoc session manipulation. `PositionRepository` remains the sole write path.

8. **No change to execution gating or sizing**
   WI-28 does not alter `LLMEvaluationResponse`, Kelly sizing, exposure caps, circuit breaker behavior, or the BUY/SELL routing decision path.

## 6. Acceptance Criteria

1. `docs/business_logic/business_logic_wi28.md` defines WI-28 as an additive accounting extension to WI-21.
2. `migrations/versions/0004_add_fee_columns.py` exists, revises `0003`, and adds nullable `gas_cost_usdc` and `fees_usdc` `Numeric(38,18)` columns to `positions`.
3. `src/db/models.py` `Position` model includes nullable `gas_cost_usdc` and `fees_usdc` fields.
4. `src/schemas/position.py` `PositionRecord` includes optional `gas_cost_usdc` and `fees_usdc` with float rejection.
5. `src/agents/execution/pnl_calculator.py` accepts optional `gas_cost_usdc` and `fees_usdc` parameters.
6. `PnLCalculator` computes net PnL by subtracting normalized gas and fee values from gross `realized_pnl`.
7. Missing gas/fee inputs default to `Decimal("0")`; no historical row or legacy caller breaks.
8. `src/schemas/risk.py` exposes additive fee-aware reporting fields for per-position net PnL and aggregate net totals.
9. `PositionRepository.record_settlement()` persists gas and fee values through the repository path only.
10. `dry_run=True` still computes the full fee-aware accounting record while performing zero DB writes.
11. All new money fields reject `float` and use `Decimal` coercion only.

## 7. Test Plan

### Unit Tests

1. `PnLCalculator.settle()` with explicit gas + fee inputs computes correct gross and net values.
2. `PnLCalculator.settle()` with `gas_cost_usdc=None` and `fees_usdc=None` returns net equal to gross.
3. Degenerate `entry_price == Decimal("0")` still yields `net_realized_pnl = Decimal("0") - gas_cost_usdc - fees_usdc`.
4. `PositionRecord`, `PositionLifecycleEntry`, `LifecycleReport`, and any extended `PnLRecord` reject `float`.
5. Lifecycle report math correctly sums `total_gas_cost_usdc`, `total_fees_usdc`, and `total_net_realized_pnl`.
6. Legacy `NULL` gas/fee rows deserialize as zero and do not raise.

### Integration Tests

1. Settlement persistence writes `gas_cost_usdc` and `fees_usdc` to the `positions` row.
2. A pre-WI-28 position row with both new columns `NULL` still loads into a valid lifecycle report.
3. `dry_run=True` computes fee-aware PnL but leaves the DB unchanged.
4. Alembic upgrade/downgrade cleanly adds and removes the two fee columns.

## 8. Non-Negotiable Design Decision

WI-28 stores the cost primitives (`gas_cost_usdc`, `fees_usdc`) and derives Net PnL from them. It does **not** rely on downstream UI code to guess costs, and it does **not** require historical backfill before deployment.

That is the core business rule:

```python
true_net_pnl = realized_pnl - gas_cost_usdc - fees_usdc
```

with both cost inputs defaulting to:

```python
Decimal("0")
```

when older data does not have them.
