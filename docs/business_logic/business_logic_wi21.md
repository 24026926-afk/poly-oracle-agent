# WI-21 Business Logic — Realized PnL & Settlement

## Active Agents + Constraints

- `.agents/rules/risk-auditor.md` — All PnL arithmetic is `Decimal`-only. Float rejected at Pydantic boundary. `Decimal(str(value))` for any non-Decimal inputs.
- `.agents/rules/db-engineer.md` — Settlement writes go through `PositionRepository.record_settlement()` only. Zero direct `AsyncSession` calls outside repositories. Alembic migration for schema changes.
- `.agents/rules/security-auditor.md` — `dry_run=True` computes and logs the full `PnLRecord` but performs zero DB writes. No credentials in structured logs.
- `.agents/rules/test-engineer.md` — WI-21 requires unit + integration coverage for PnL formula, settlement persistence, idempotency, and dry-run path. Full suite remains >= 80%.

## 1. Objective

Introduce `PnLCalculator`, a read-only accounting component that computes realized profit/loss when a position is closed via an exit order (WI-20 `ExitOrderRouter`), and persists the settlement data to the `positions` table through `PositionRepository`.

`PnLCalculator` owns:
- Realized PnL formula: `(exit_price - entry_price) * position_size_tokens`
- Token quantity derivation: `order_size_usdc / entry_price`
- Division-by-zero guard for degenerate entry prices
- Settlement idempotency enforcement
- `dry_run` gate for DB writes
- Structured audit logging of every settlement outcome

`PnLCalculator` does NOT own:
- Exit decision logic (upstream: `ExitStrategyEngine`, WI-19)
- Exit order routing or signing (upstream: `ExitOrderRouter`, WI-20)
- Order broadcast (upstream: `OrderBroadcaster`)
- Position status mutation (`OPEN → CLOSED` handled by `ExitStrategyEngine`)
- Portfolio-level PnL aggregation, tax lot accounting, or fee accounting

## 2. Scope Boundaries

### In Scope

1. New `PnLCalculator` class in `src/agents/execution/pnl_calculator.py`.
2. New `PnLRecord` Pydantic model in `src/schemas/execution.py` — frozen, Decimal-validated.
3. New `PnLCalculationError` exception in `src/core/exceptions.py`.
4. `PositionRepository.record_settlement()` — new additive repository method.
5. `Position` ORM model extension: 3 new nullable columns.
6. `PositionRecord` schema extension: 3 new optional fields.
7. Alembic migration `0003_add_pnl_columns.py`: adds `realized_pnl`, `exit_price`, and `closed_at_utc` to `positions`.
8. Orchestrator wiring: constructed in `__init__()`, called after `ExitOrderRouter.route_exit()` in `_exit_scan_loop()`.

### Out of Scope

1. Mark-to-market or unrealized PnL (WI-19 `unrealized_edge` covers open positions).
2. Portfolio-level PnL aggregation or reporting dashboards.
3. Tax lot accounting (FIFO/LIFO).
4. Fee accounting (CLOB fees, gas costs).
5. Market resolution data or oracle settlement — exit price comes from the exit order's best_bid, not market resolution.
6. Partial settlement — a position is settled in full or not at all.
7. Modifications to `ExitStrategyEngine`, `ExitOrderRouter`, or `ExecutionRouter` internals.

## 3. Target Component Architecture + Data Contracts

### 3.1 PnLCalculator Component (New Class)

- **Module:** `src/agents/execution/pnl_calculator.py`
- **Class Name:** `PnLCalculator` (exact)
- **Responsibility:** pure accounting — reads `PositionRecord` metadata and an exit price, computes realized PnL as a scalar `Decimal`, and persists settlement data through `PositionRepository`.

Isolation rules:
- `PnLCalculator` must not import LLM prompt construction, context-building, evaluation, or ingestion modules.
- `PnLCalculator` must not mutate position status (status was already transitioned by `ExitStrategyEngine`).
- `PnLCalculator` must not influence routing, exit decisions, or any upstream component.
- `PnLCalculator` does not call `PolymarketClient`, `BankrollSyncProvider`, or `TransactionSigner`.

### 3.2 Data Contracts

#### 3.2.1 `PnLRecord` model (New)

Location: `src/schemas/execution.py`

```python
class PnLRecord(BaseModel):
    """Typed realized PnL outcome returned by PnLCalculator.settle()."""

    position_id: str
    condition_id: str
    entry_price: Decimal
    exit_price: Decimal
    order_size_usdc: Decimal
    position_size_tokens: Decimal
    realized_pnl: Decimal
    closed_at_utc: datetime

    @field_validator(
        "entry_price",
        "exit_price",
        "order_size_usdc",
        "position_size_tokens",
        "realized_pnl",
        mode="before",
    )
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = {"frozen": True}
```

Hard rules:
- All five financial fields are `Decimal`. Float is rejected at Pydantic boundary.
- Model is frozen (immutable after construction).
- `position_size_tokens` is an intermediate value included for auditability.

#### 3.2.2 `PnLCalculationError` exception (New)

Location: `src/core/exceptions.py`

```python
class PnLCalculationError(PolyOracleError):
    """Raised when PnL calculation or settlement persistence fails."""

    def __init__(
        self,
        reason: str,
        position_id: str | None = None,
        condition_id: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        message = reason
        if position_id:
            message = f"{message} (position_id={position_id})"
        if condition_id:
            message = f"{message} (condition_id={condition_id})"
        super().__init__(message)
        self.reason = reason
        self.position_id = position_id
        self.condition_id = condition_id
        self.cause = cause
```

Follows the established pattern from `ExitEvaluationError` / `ExitMutationError` / `ExitRoutingError`.

### 3.3 PositionRecord Schema Extension

Location: `src/schemas/position.py`

Three new **optional** fields appended to `PositionRecord`:

```python
realized_pnl: Decimal | None = None
exit_price: Decimal | None = None
closed_at_utc: datetime | None = None
```

These fields are `None` for `OPEN` and `FAILED` positions. They are populated only after settlement.

The existing `_reject_float_financials` validator must be extended to cover `realized_pnl` and `exit_price`:

```python
@field_validator(
    "entry_price",
    "order_size_usdc",
    "kelly_fraction",
    "best_ask_at_entry",
    "bankroll_usdc_at_entry",
    "realized_pnl",     # NEW
    "exit_price",        # NEW
    mode="before",
)
```

The validator already handles `None` values correctly via the `if isinstance(value, float)` check — `None` is not `float`, so it passes through unchanged.

> [!IMPORTANT]
> The validator must explicitly allow `None` passthrough for the new nullable fields. Add a `None` guard at the top of the validator if not already present:
> ```python
> if value is None:
>     return value
> ```

## 4. Database Schema Changes

### 4.1 Position ORM Model Extension

Location: `src/db/models.py`

Three new **nullable** columns added to the `Position` class:

```python
# --- Settlement (WI-21) ---
realized_pnl: Mapped[Optional[Decimal]] = mapped_column(
    Numeric(precision=38, scale=18),
    nullable=True,
    comment="Realized PnL in USDC: (exit_price - entry_price) * position_size_tokens",
)
exit_price: Mapped[Optional[Decimal]] = mapped_column(
    Numeric(precision=38, scale=18),
    nullable=True,
    comment="SELL-side exit price (best_bid at exit routing time)",
)
closed_at_utc: Mapped[Optional[datetime]] = mapped_column(
    DateTime(timezone=True),
    nullable=True,
    comment="UTC timestamp when settlement was recorded",
)
```

Financial precision matches all existing financial columns: `Numeric(38, 18)`.

Nullable because:
- `OPEN` positions have no settlement data.
- `FAILED` positions have no settlement data.
- `CLOSED` positions have settlement data only after `PnLCalculator.settle()` runs.

### 4.2 Alembic Migration `0003_add_pnl_columns.py`

Location: `migrations/versions/0003_add_pnl_columns.py`

```python
"""add pnl settlement columns to positions

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-30 12:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "positions",
        sa.Column(
            "realized_pnl",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
    )
    op.add_column(
        "positions",
        sa.Column(
            "exit_price",
            sa.Numeric(precision=38, scale=18),
            nullable=True,
        ),
    )
    op.add_column(
        "positions",
        sa.Column(
            "closed_at_utc",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("positions", "closed_at_utc")
    op.drop_column("positions", "exit_price")
    op.drop_column("positions", "realized_pnl")
```

Key constraints:
1. Parent migration is `0002` (`0002_add_open_positions_table.py`).
2. All three columns are `nullable=True` — existing rows are unaffected.
3. Financial columns use `Numeric(38, 18)` matching the existing `positions` table schema.
4. `closed_at_utc` uses `DateTime(timezone=True)` matching existing timestamp columns.

### 4.3 PositionRepository.record_settlement() (New Method)

Location: `src/db/repositories/position_repository.py`

```python
async def record_settlement(
    self,
    *,
    position_id: str,
    realized_pnl: Decimal,
    exit_price: Decimal,
    closed_at_utc: datetime,
) -> Position | None:
    """Write settlement data to an existing CLOSED position.

    Idempotent: if realized_pnl is already set, log a warning
    and return without overwriting.
    """
    position = await self.get_by_id(position_id)
    if position is None:
        return None

    # Idempotency guard
    if position.realized_pnl is not None:
        logger.warning(
            "position.settlement_already_recorded",
            position_id=position_id,
            existing_pnl=str(position.realized_pnl),
        )
        return position

    position.realized_pnl = realized_pnl
    position.exit_price = exit_price
    position.closed_at_utc = closed_at_utc
    await self._session.flush()
    logger.debug(
        "position.settlement_recorded",
        position_id=position.id,
        realized_pnl=str(realized_pnl),
        exit_price=str(exit_price),
    )
    return position
```

Hard rules:
1. **Additive** — does not modify existing `insert_position()`, `update_status()`, or `get_open_positions()` methods.
2. **Idempotent** — if `realized_pnl` is already set, logs a warning and returns the existing row without overwriting. This prevents double-settlement.
3. **Does not change `status`** — the `OPEN → CLOSED` transition was already performed by `ExitStrategyEngine`. Settlement writes financial data only.
4. Uses `flush()` (not `commit()`) — the caller controls commit timing.
5. Returns `Position | None` — `None` if position_id not found.

## 5. Core Method Contracts (async, typed)

### 5.1 Constructor

```python
class PnLCalculator:
    def __init__(
        self,
        config: AppConfig,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
```

Dependencies:
1. `config: AppConfig` — `dry_run` flag.
2. `db_session_factory: async_sessionmaker[AsyncSession]` — for constructing `PositionRepository` within a session context.

No `PolymarketClient`, `TransactionSigner`, or `BankrollSyncProvider` — this is a pure accounting component.

### 5.2 Async Settlement Entry Point

```python
async def settle(
    self,
    position: PositionRecord,
    exit_price: Decimal,
) -> PnLRecord:
```

This is the sole public async method. Behavior:

#### Step 1: Validate Inputs

```python
exit_price_d = Decimal(str(exit_price))
entry_price_d = Decimal(str(position.entry_price))
order_size_usdc_d = Decimal(str(position.order_size_usdc))
```

All inputs are coerced to `Decimal(str(...))` for safety, even if already `Decimal`. No `float` intermediary at any step.

#### Step 2: Compute Position Size in Tokens

```python
_ZERO = Decimal("0")

if entry_price_d == _ZERO:
    logger.warning(
        "pnl.degenerate_entry_price",
        position_id=position.id,
        entry_price=str(entry_price_d),
    )
    position_size_tokens = _ZERO
else:
    position_size_tokens = order_size_usdc_d / entry_price_d
```

Division-by-zero guard: if `entry_price == Decimal("0")`, set `position_size_tokens = Decimal("0")`, which cascades to `realized_pnl = Decimal("0")`.

#### Step 3: Compute Realized PnL

```python
realized_pnl = (exit_price_d - entry_price_d) * position_size_tokens
```

Formula: `realized_pnl = (exit_price - entry_price) * position_size_tokens`

- Positive PnL = profitable exit (sold higher than bought)
- Negative PnL = loss (sold lower than bought)
- Zero PnL = breakeven or degenerate entry price

All arithmetic is `Decimal`. No `float()` conversion at any step.

#### Step 4: Build PnLRecord

```python
closed_at_utc = datetime.now(timezone.utc)
pnl_record = PnLRecord(
    position_id=position.id,
    condition_id=position.condition_id,
    entry_price=entry_price_d,
    exit_price=exit_price_d,
    order_size_usdc=order_size_usdc_d,
    position_size_tokens=position_size_tokens,
    realized_pnl=realized_pnl,
    closed_at_utc=closed_at_utc,
)
```

#### Step 5: Log Settlement Computation

```python
logger.info(
    "pnl.calculated",
    position_id=pnl_record.position_id,
    condition_id=pnl_record.condition_id,
    entry_price=str(pnl_record.entry_price),
    exit_price=str(pnl_record.exit_price),
    order_size_usdc=str(pnl_record.order_size_usdc),
    position_size_tokens=str(pnl_record.position_size_tokens),
    realized_pnl=str(pnl_record.realized_pnl),
)
```

This event is emitted regardless of `dry_run` — the computation always happens.

#### Step 6: dry_run Gate

```python
if self._config.dry_run:
    logger.info(
        "pnl.dry_run_settlement",
        dry_run=True,
        position_id=pnl_record.position_id,
        realized_pnl=str(pnl_record.realized_pnl),
        exit_price=str(pnl_record.exit_price),
    )
    return pnl_record
```

When `dry_run=True`:
- Full `PnLRecord` is computed and returned.
- All logging occurs.
- Zero DB writes. No session created, no repository instantiated.
- Return immediately.

#### Step 7: Persist Settlement (Live Only)

```python
try:
    async with self._db_session_factory() as session:
        repo = PositionRepository(session)
        updated = await repo.record_settlement(
            position_id=pnl_record.position_id,
            realized_pnl=pnl_record.realized_pnl,
            exit_price=pnl_record.exit_price,
            closed_at_utc=pnl_record.closed_at_utc,
        )
        if updated is None:
            logger.error(
                "pnl.position_not_found",
                position_id=pnl_record.position_id,
            )
            raise PnLCalculationError(
                reason="position_not_found_for_settlement",
                position_id=pnl_record.position_id,
                condition_id=pnl_record.condition_id,
            )
        await session.commit()
    logger.info(
        "pnl.persisted",
        position_id=pnl_record.position_id,
        realized_pnl=str(pnl_record.realized_pnl),
    )
except PnLCalculationError:
    raise
except Exception as exc:
    logger.error(
        "pnl.persistence_failed",
        position_id=pnl_record.position_id,
        error=str(exc),
    )
    raise PnLCalculationError(
        reason="settlement_persistence_failed",
        position_id=pnl_record.position_id,
        condition_id=pnl_record.condition_id,
        cause=exc,
    ) from exc

return pnl_record
```

## 6. Pipeline Integration Design

### 6.1 Orchestrator Wiring

#### Construction (in `Orchestrator.__init__()`):

```python
self.pnl_calculator = PnLCalculator(
    config=self.config,
    db_session_factory=AsyncSessionLocal,
)
```

Placement: immediately after `self.exit_order_router` construction.

#### Invocation (in `_exit_scan_loop()`):

Called after `ExitOrderRouter.route_exit()` produces a `SELL_ROUTED` or `DRY_RUN` result:

```python
# Inside _exit_scan_loop, after route_exit():
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
        # PnL failure does not block the scan loop or downstream broadcast
```

The exit price used for settlement is `ExitOrderResult.exit_price` — the `best_bid` at exit routing time.

### 6.2 Exit Scan Loop Flow (Complete, After Phase 7)

```text
_exit_scan_loop:
  1. Sleep for config.exit_scan_interval_seconds           [WI-22]
  2. ExitStrategyEngine.scan_open_positions()               [WI-19]
     -> list[ExitResult]
  3. For each ExitResult where should_exit=True:
     a. ExitOrderRouter.route_exit(exit_result, position)   [WI-20]
        -> ExitOrderResult (SELL_ROUTED | DRY_RUN | FAILED)
     b. If SELL_ROUTED or DRY_RUN:
        PnLCalculator.settle(position, exit_price)          [WI-21]
        -> PnLRecord (persisted or logged)
     c. If SELL_ROUTED and not dry_run:
        OrderBroadcaster.broadcast(signed_order)            [existing]
  4. Log summary of scan results
  5. Repeat
```

### 6.3 Failure Semantics (Fail-Open)

PnL settlement is an **accounting concern**, not a safety gate. A failed settlement must never block:
- The exit scan loop from processing remaining positions.
- The exit order broadcast from proceeding.
- The execution consumer loop.

| Failure | Behavior | Consequence |
|---------|----------|-------------|
| `entry_price == 0` | Returns `PnLRecord(realized_pnl=Decimal("0"))` | Logged warning, no exception |
| Position not found in DB | Raises `PnLCalculationError` | Caught in scan loop, continue |
| DB persistence error | Raises `PnLCalculationError` | Caught in scan loop, continue |
| Previously settled (idempotency) | Returns existing row, logs warning | No overwrite, no exception |
| `dry_run=True` | Returns computed `PnLRecord`, zero DB writes | Normal flow |

Missing PnL can be backfilled later from position metadata and exit-order audit logs.

### 6.4 dry_run Behavior

When `config.dry_run is True`:

1. Full PnL computation runs: token quantity derivation, realized PnL formula, `PnLRecord` construction.
2. `pnl.calculated` event is emitted with all audit fields.
3. `pnl.dry_run_settlement` event is emitted.
4. Zero DB writes — no session created, no repository instantiated.
5. `PnLRecord` is returned to the caller for downstream logging.

### 6.5 Module Isolation Rules

The `PnLCalculator` module (`src/agents/execution/pnl_calculator.py`) must not:

1. Import or call LLM prompt construction, context-building, evaluation, or ingestion modules.
2. Import `PolymarketClient`, `BankrollSyncProvider`, `TransactionSigner`, `ExecutionRouter`, or `ExitOrderRouter`.
3. Mutate position status (`OPEN → CLOSED` is upstream).
4. Influence exit decisions or routing.

Allowed imports:
- `src.core.config` → `AppConfig`
- `src.core.exceptions` → `PnLCalculationError`
- `src.db.repositories.position_repository` → `PositionRepository`
- `src.schemas.execution` → `PnLRecord`
- `src.schemas.position` → `PositionRecord`
- `sqlalchemy.ext.asyncio` → `AsyncSession`, `async_sessionmaker`
- `structlog`, `decimal.Decimal`, `datetime`

## 7. Required structlog Audit Events

| Event Key | Level | When | Required Fields |
|-----------|-------|------|-----------------|
| `pnl.calculated` | `INFO` | After PnL formula completes (always) | `position_id`, `condition_id`, `entry_price`, `exit_price`, `order_size_usdc`, `position_size_tokens`, `realized_pnl` |
| `pnl.degenerate_entry_price` | `WARNING` | `entry_price == Decimal("0")` | `position_id`, `entry_price` |
| `pnl.dry_run_settlement` | `INFO` | `dry_run=True`, full record computed | `dry_run=True`, `position_id`, `realized_pnl`, `exit_price` |
| `pnl.persisted` | `INFO` | Settlement data written to DB (live only) | `position_id`, `realized_pnl` |
| `pnl.position_not_found` | `ERROR` | `record_settlement()` returns `None` | `position_id` |
| `pnl.persistence_failed` | `ERROR` | DB write raises exception | `position_id`, `error` |

## 8. Invariants Preserved

1. **Gatekeeper authority** — `LLMEvaluationResponse` remains the terminal pre-execution gate. `PnLCalculator` operates far downstream: after evaluation, routing, exit evaluation, exit routing, and broadcasting. No bypass.
2. **Decimal financial integrity** — all PnL arithmetic, settlement values, and database columns are `Decimal` / `Numeric(38,18)`. Float is rejected at Pydantic boundary. No float intermediary in any arithmetic step.
3. **Quarter-Kelly policy** — `PnLCalculator` does not perform Kelly sizing. It reads position metadata (already Kelly-capped at entry) and computes an accounting scalar.
4. **`dry_run=True` blocks DB writes** — full `PnLRecord` is computed and logged; zero persistence. No session created.
5. **Repository pattern** — `PositionRepository.record_settlement()` is the sole path for settlement columns. Additive method — does not modify existing methods.
6. **Settlement idempotency** — re-settling a position with existing `realized_pnl` logs a warning and returns without overwriting. No double-counting.
7. **Async pipeline** — `PnLCalculator` runs within the existing `_exit_scan_loop()` async task. No new tasks or queues introduced.
8. **Entry-path routing** — `ExecutionRouter` internals are unmodified.
9. **Exit-path routing** — `ExitOrderRouter` internals are unmodified. `PnLCalculator` reads `ExitOrderResult.exit_price` only.
10. **Module isolation** — zero imports from prompt, context, evaluation, or ingestion modules.
11. **Position status immutability** — `PnLCalculator` writes financial settlement data only. It never changes `status`. The `OPEN → CLOSED` transition is upstream.

## 9. Strict Acceptance Criteria (Maker Agent)

1. `PnLCalculator` exists in `src/agents/execution/pnl_calculator.py` as the canonical settlement class.
2. `settle(position: PositionRecord, exit_price: Decimal) -> PnLRecord` is the sole public async entry point.
3. `PnLRecord` Pydantic model is frozen, Decimal-validated, with fields: `position_id`, `condition_id`, `entry_price`, `exit_price`, `order_size_usdc`, `position_size_tokens`, `realized_pnl`, `closed_at_utc`.
4. `realized_pnl = (exit_price - entry_price) * position_size_tokens` using Decimal arithmetic.
5. `position_size_tokens = order_size_usdc / entry_price` using Decimal division.
6. Division by zero (`entry_price == Decimal("0")`) returns `PnLRecord(realized_pnl=Decimal("0"))` and logs `pnl.degenerate_entry_price` warning.
7. Alembic migration `0003_add_pnl_columns.py` adds 3 nullable columns to `positions`: `realized_pnl Numeric(38,18)`, `exit_price Numeric(38,18)`, `closed_at_utc DateTime(timezone=True)`.
8. `Position` ORM model has 3 new nullable columns matching migration.
9. `PositionRecord` schema gains 3 new optional fields: `realized_pnl: Decimal | None`, `exit_price: Decimal | None`, `closed_at_utc: datetime | None`.
10. `PositionRepository.record_settlement(position_id, realized_pnl, exit_price, closed_at_utc)` writes settlement data to an existing position row.
11. Settlement is idempotent: re-settling a position with existing `realized_pnl` logs a warning and returns without overwriting.
12. `dry_run=True` computes full `PnLRecord`, logs via structlog, zero DB writes, zero session creation.
13. `PnLCalculator` is constructed in `Orchestrator.__init__()` and called after `ExitOrderRouter.route_exit()` in `_exit_scan_loop()`.
14. PnL calculation failure does not block the exit scan loop or downstream broadcast.
15. `PnLCalculator` has zero imports from prompt, context, evaluation, or ingestion modules.
16. `PnLCalculationError` exception exists in `src/core/exceptions.py` with `reason`, `position_id`, `condition_id`, `cause` fields.
17. `float` values are rejected by `PnLRecord` field validators for all financial fields.
18. `float` values are rejected by `PositionRecord` validators for new `realized_pnl` and `exit_price` fields.
19. Full regression remains green with coverage >= 80%.

## 10. Verification Checklist (Test Matrix)

### Unit Tests

1. PnL formula correctness: known inputs `(entry=0.45, exit=0.65, size_usdc=25)` → expected `realized_pnl` value.
2. PnL formula with loss: `exit_price < entry_price` → negative `realized_pnl`.
3. PnL formula breakeven: `exit_price == entry_price` → `realized_pnl == Decimal("0")`.
4. Division by zero: `entry_price == Decimal("0")` → `realized_pnl == Decimal("0")`, warning logged.
5. `position_size_tokens = order_size_usdc / entry_price` produces correct `Decimal` for known inputs.
6. `PnLRecord` model is frozen — field assignment after construction raises error.
7. `PnLRecord` rejects `float` in financial fields at Pydantic boundary.
8. `PnLRecord` accepts `Decimal` in financial fields.
9. `dry_run=True` computes and returns `PnLRecord`; zero DB writes; `pnl.dry_run_settlement` logged.
10. `dry_run=False` writes to DB via `PositionRepository.record_settlement()`.
11. `pnl.calculated` event emitted with all required fields (both dry_run and live).
12. `pnl.persisted` event emitted after successful DB write (live only).
13. `PositionRepository.record_settlement()` writes all 3 columns.
14. `PositionRepository.record_settlement()` idempotency: position with existing `realized_pnl` → warning logged, no overwrite.
15. `PositionRepository.record_settlement()` returns `None` when position_id not found.
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

### Full Suite

27. `pytest --asyncio-mode=auto tests/`
28. `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m`
