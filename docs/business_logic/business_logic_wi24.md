# WI-24 Business Logic — Position Lifecycle Reporter

## Active Agents + Constraints

- `.agents/rules/risk-auditor.md` — All financial aggregation (realized PnL, best/worst PnL) uses `Decimal` arithmetic. No floats in computation or storage. `Decimal(str(value))` for any non-Decimal inputs.
- `.agents/rules/db-engineer.md` — Settled position reads go through `PositionRepository.get_settled_positions()` only. Zero direct `AsyncSession` calls outside repositories. No new tables or migrations required — WI-24 is read-only. The new `get_settled_positions()` method is additive and does not modify any existing repository method.
- `.agents/rules/security-auditor.md` — `dry_run=True` computes the full `LifecycleReport` but affects logging verbosity only (component is already read-only). No credentials, private keys, or write paths touched.
- `.agents/rules/async-architect.md` — `PositionLifecycleReporter` is stateless and on-demand. It does NOT run as a periodic background task. It is invoked within `_portfolio_aggregation_loop()` after `PortfolioAggregator.compute_snapshot()` completes, or called ad-hoc by the operator. No new `asyncio.create_task()` or queue introduced.
- `.agents/rules/test-engineer.md` — WI-24 requires unit + integration coverage for report aggregation, win/loss classification, hold-duration calculation, zero-settled-positions edge case, and repository method correctness. Full suite remains >= 80%.

## 1. Objective

Introduce `PositionLifecycleReporter`, a read-only reporting component that produces structured performance summaries over all positions — both closed (settled) and open. The reporter reads position data from `PositionRepository`, computes aggregate statistics, and returns a typed `LifecycleReport`.

`PositionLifecycleReporter` owns:
- On-demand report generation via `generate_report()` async entry point
- Aggregation of total realized PnL across settled positions
- Win/loss/breakeven classification based on `realized_pnl` sign
- Average hold duration computation from `closed_at_utc - routed_at_utc`
- Best and worst single-position PnL identification
- Per-position `PositionLifecycleEntry` construction with full entry/exit/PnL detail
- Optional time range filtering via `start_date` / `end_date` parameters
- `dry_run` flag propagation for audit context
- Structured audit logging of report results

`PositionLifecycleReporter` does NOT own:
- Position lifecycle management (upstream: `PositionTracker`, WI-17)
- Exit evaluation or exit routing (upstream: `ExitStrategyEngine` WI-19, `ExitOrderRouter` WI-20)
- PnL settlement or DB writes (upstream: `PnLCalculator`, WI-21)
- Portfolio aggregation or unrealized PnL (upstream: `PortfolioAggregator`, WI-23)
- Alert generation or threshold evaluation (downstream: `AlertEngine`, WI-25)
- Order execution, signing, or broadcasting
- Tax lot accounting, fee accounting, or FIFO/LIFO
- Historical report persistence or cross-report comparison

## 2. Scope Boundaries

### In Scope

1. New `PositionLifecycleReporter` class in `src/agents/execution/lifecycle_reporter.py`.
2. New `LifecycleReport` Pydantic model in `src/schemas/risk.py` (extends existing file from WI-23).
3. New `PositionLifecycleEntry` Pydantic model in `src/schemas/risk.py` — per-position detail record.
4. `generate_report(start_date: datetime | None = None, end_date: datetime | None = None) -> LifecycleReport` as the sole public async entry point.
5. New `PositionRepository.get_settled_positions() -> list[Position]` — reads `CLOSED` positions where `realized_pnl IS NOT NULL`.
6. New `PositionRepository.get_all_positions() -> list[Position]` — reads all positions regardless of status.
7. New `PositionRepository.get_positions_by_status(status: str) -> list[Position]` — reads positions filtered by status string.
8. Aggregate statistics: `total_realized_pnl`, `winning_count`, `losing_count`, `breakeven_count`, `avg_hold_duration_hours`, `best_pnl`, `worst_pnl`, `total_settled_count`.
9. Per-position lifecycle entries in the report: `PositionLifecycleEntry` with `position_id`, `slug`, `entry_price`, `exit_price`, `size_tokens`, `realized_pnl`, `status`, `opened_at_utc`, `settled_at_utc`.
10. Optional time range filter: when `start_date` and/or `end_date` are provided, only positions with `routed_at_utc` within the range are included.
11. `dry_run=True`: full report is computed; flag included in output for audit context. No side effects (component is inherently read-only).
12. structlog audit events: `lifecycle.report_generated`, `lifecycle.report_empty`.
13. Orchestrator wiring: constructed in `__init__()`, invoked within `_portfolio_aggregation_loop()` after `compute_snapshot()` and available for ad-hoc calls.

### Out of Scope

1. New database tables, migrations, or DB writes — this component is strictly read-only.
2. Rolling-window or incremental reports — each `generate_report()` call computes from current DB state.
3. Per-market or per-category breakdowns.
4. Tax lot accounting, FIFO/LIFO, or fee-adjusted PnL.
5. Historical report persistence or comparison across report generations.
6. Sharpe ratio, max drawdown, or advanced risk-adjusted return metrics (future phase).
7. Modifications to `PnLCalculator`, `ExitStrategyEngine`, `ExitOrderRouter`, or `ExecutionRouter` internals.
8. Periodic background task — this is an on-demand reporter, not a scheduled loop.

## 3. Target Component Architecture + Data Contracts

### 3.1 PositionLifecycleReporter Component (New Class)

- **Module:** `src/agents/execution/lifecycle_reporter.py`
- **Class Name:** `PositionLifecycleReporter` (exact)
- **Responsibility:** Read all settled and open positions from `PositionRepository`, compute aggregate performance statistics and per-position lifecycle entries, and return a typed `LifecycleReport`.

Isolation rules:
- `PositionLifecycleReporter` must not import LLM prompt construction, context-building, evaluation, or ingestion modules.
- `PositionLifecycleReporter` must not mutate position status or write to the database.
- `PositionLifecycleReporter` must not influence routing, exit decisions, or any upstream component.
- `PositionLifecycleReporter` does not call `TransactionSigner`, `OrderBroadcaster`, or `BankrollSyncProvider`.

### 3.2 Data Contracts

#### 3.2.1 `PositionLifecycleEntry` model (New)

Location: `src/schemas/risk.py` (existing file, add below `PortfolioSnapshot`)

```python
class PositionLifecycleEntry(BaseModel):
    """Per-position detail record for the lifecycle report."""

    position_id: str
    slug: str                          # condition_id used as slug identifier
    entry_price: Decimal
    exit_price: Decimal | None         # None for OPEN positions
    size_tokens: Decimal               # order_size_usdc / entry_price
    realized_pnl: Decimal | None       # None for OPEN positions
    status: str                        # "OPEN" or "CLOSED"
    opened_at_utc: datetime
    settled_at_utc: datetime | None    # None for OPEN positions

    @field_validator(
        "entry_price",
        "size_tokens",
        mode="before",
    )
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @field_validator(
        "exit_price",
        "realized_pnl",
        mode="before",
    )
    @classmethod
    def _reject_float_nullable_financials(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = {"frozen": True}
```

Hard rules:
- All financial fields (`entry_price`, `exit_price`, `size_tokens`, `realized_pnl`) are `Decimal`. Float is rejected at Pydantic boundary.
- `exit_price`, `realized_pnl`, and `settled_at_utc` are `None` for `OPEN` positions.
- Model is frozen (immutable after construction).

Field definitions:

| Field | Type | Description |
|-------|------|-------------|
| `position_id` | `str` | UUID primary key of the position |
| `slug` | `str` | `condition_id` serving as a human-readable market identifier |
| `entry_price` | `Decimal` | Price at which the position was opened |
| `exit_price` | `Decimal | None` | Price at which the position was closed; `None` if still open |
| `size_tokens` | `Decimal` | `order_size_usdc / entry_price` — token quantity of the position |
| `realized_pnl` | `Decimal | None` | Settled PnL in USDC; `None` if still open |
| `status` | `str` | Position status: `"OPEN"` or `"CLOSED"` |
| `opened_at_utc` | `datetime` | UTC timestamp when the position was routed (`routed_at_utc`) |
| `settled_at_utc` | `datetime | None` | UTC timestamp when settlement was recorded (`closed_at_utc`); `None` if still open |

#### 3.2.2 `LifecycleReport` model (New)

Location: `src/schemas/risk.py` (existing file, add below `PositionLifecycleEntry`)

```python
class LifecycleReport(BaseModel):
    """Typed aggregate lifecycle performance report."""

    report_at_utc: datetime
    total_settled_count: int
    winning_count: int
    losing_count: int
    breakeven_count: int
    total_realized_pnl: Decimal
    avg_hold_duration_hours: Decimal
    best_pnl: Decimal
    worst_pnl: Decimal
    entries: list[PositionLifecycleEntry]
    dry_run: bool

    @field_validator(
        "total_realized_pnl",
        "avg_hold_duration_hours",
        "best_pnl",
        "worst_pnl",
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
- All four financial/duration fields are `Decimal`. Float is rejected at Pydantic boundary.
- Model is frozen (immutable after construction).
- `entries` contains the full list of `PositionLifecycleEntry` records included in the report.
- `winning_count + losing_count + breakeven_count == total_settled_count` (invariant).
- `dry_run` reflects config state for downstream audit consumers.

Field definitions:

| Field | Type | Description |
|-------|------|-------------|
| `report_at_utc` | `datetime` | UTC timestamp when the report was generated |
| `total_settled_count` | `int` | Number of `CLOSED` positions with non-null `realized_pnl` |
| `winning_count` | `int` | Positions where `realized_pnl > Decimal("0")` |
| `losing_count` | `int` | Positions where `realized_pnl < Decimal("0")` |
| `breakeven_count` | `int` | Positions where `realized_pnl == Decimal("0")` |
| `total_realized_pnl` | `Decimal` | Sum of `realized_pnl` across all settled positions |
| `avg_hold_duration_hours` | `Decimal` | Average `(closed_at_utc - routed_at_utc)` in hours across settled positions |
| `best_pnl` | `Decimal` | `max(realized_pnl)` across settled positions; `Decimal("0")` if none |
| `worst_pnl` | `Decimal` | `min(realized_pnl)` across settled positions; `Decimal("0")` if none |
| `entries` | `list[PositionLifecycleEntry]` | Per-position detail records (settled + open, depending on query) |
| `dry_run` | `bool` | Whether the system is in dry-run mode |

## 4. Core Method Contracts (async, typed)

### 4.1 Constructor

```python
class PositionLifecycleReporter:
    def __init__(
        self,
        config: AppConfig,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
```

Dependencies:
1. `config: AppConfig` — `dry_run` flag.
2. `db_session_factory: async_sessionmaker[AsyncSession]` — for constructing `PositionRepository` within a session context.

No `PolymarketClient`, `TransactionSigner`, `OrderBroadcaster`, or `BankrollSyncProvider` — this is a read-only reporting component that requires no live market data.

### 4.2 Async Report Entry Point

```python
async def generate_report(
    self,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> LifecycleReport:
```

This is the sole public async method. Behavior:

#### Step 1: Load Positions

```python
async with self._db_session_factory() as session:
    repo = PositionRepository(session)
    all_positions: list[Position] = await repo.get_all_positions()
```

Read all positions from the database. Apply optional time range filter on `routed_at_utc`:

```python
if start_date is not None:
    all_positions = [p for p in all_positions if p.routed_at_utc >= start_date]
if end_date is not None:
    all_positions = [p for p in all_positions if p.routed_at_utc <= end_date]
```

Separate into settled and open:

```python
settled = [p for p in all_positions if p.status == "CLOSED" and p.realized_pnl is not None]
open_positions = [p for p in all_positions if p.status == "OPEN"]
```

If no positions exist at all, return a zero-valued `LifecycleReport` with an empty `entries` list.

#### Step 2: Build Per-Position Lifecycle Entries

For each position (settled + open), construct a `PositionLifecycleEntry`:

```python
_ZERO = Decimal("0")

entry_price_d = Decimal(str(position.entry_price))
order_size_usdc_d = Decimal(str(position.order_size_usdc))

if entry_price_d == _ZERO:
    size_tokens = _ZERO
else:
    size_tokens = order_size_usdc_d / entry_price_d

entry = PositionLifecycleEntry(
    position_id=str(position.id),
    slug=position.condition_id,
    entry_price=entry_price_d,
    exit_price=Decimal(str(position.exit_price)) if position.exit_price is not None else None,
    size_tokens=size_tokens,
    realized_pnl=Decimal(str(position.realized_pnl)) if position.realized_pnl is not None else None,
    status=position.status,
    opened_at_utc=position.routed_at_utc,
    settled_at_utc=position.closed_at_utc,
)
```

Division-by-zero guard: if `entry_price == Decimal("0")`, set `size_tokens = Decimal("0")`.

#### Step 3: Compute Aggregate Statistics (Settled Positions Only)

```python
_ZERO = Decimal("0")

if not settled:
    total_realized_pnl = _ZERO
    winning_count = 0
    losing_count = 0
    breakeven_count = 0
    avg_hold_duration_hours = _ZERO
    best_pnl = _ZERO
    worst_pnl = _ZERO
    total_settled_count = 0
else:
    total_settled_count = len(settled)
    pnl_values: list[Decimal] = []
    winning_count = 0
    losing_count = 0
    breakeven_count = 0
    total_hold_seconds = Decimal("0")

    for position in settled:
        pnl = Decimal(str(position.realized_pnl))
        pnl_values.append(pnl)

        if pnl > _ZERO:
            winning_count += 1
        elif pnl < _ZERO:
            losing_count += 1
        else:
            breakeven_count += 1

        if position.closed_at_utc is not None and position.routed_at_utc is not None:
            delta = position.closed_at_utc - position.routed_at_utc
            total_hold_seconds += Decimal(str(delta.total_seconds()))

    total_realized_pnl = sum(pnl_values, _ZERO)
    best_pnl = max(pnl_values)
    worst_pnl = min(pnl_values)
    avg_hold_duration_hours = total_hold_seconds / Decimal(str(total_settled_count)) / Decimal("3600")
```

All arithmetic is `Decimal`. No `float()` conversion at any step.

Win/loss classification is deterministic:
- `realized_pnl > Decimal("0")` → win
- `realized_pnl < Decimal("0")` → loss
- `realized_pnl == Decimal("0")` → breakeven

#### Step 4: Build LifecycleReport

```python
report = LifecycleReport(
    report_at_utc=datetime.now(timezone.utc),
    total_settled_count=total_settled_count,
    winning_count=winning_count,
    losing_count=losing_count,
    breakeven_count=breakeven_count,
    total_realized_pnl=total_realized_pnl,
    avg_hold_duration_hours=avg_hold_duration_hours,
    best_pnl=best_pnl,
    worst_pnl=worst_pnl,
    entries=lifecycle_entries,
    dry_run=self._config.dry_run,
)
```

#### Step 5: Log Report

```python
logger.info(
    "lifecycle.report_generated",
    total_settled_count=report.total_settled_count,
    winning_count=report.winning_count,
    losing_count=report.losing_count,
    breakeven_count=report.breakeven_count,
    total_realized_pnl=str(report.total_realized_pnl),
    avg_hold_duration_hours=str(report.avg_hold_duration_hours),
    best_pnl=str(report.best_pnl),
    worst_pnl=str(report.worst_pnl),
    entry_count=len(report.entries),
    dry_run=report.dry_run,
)
```

If no positions at all:

```python
logger.info(
    "lifecycle.report_empty",
    dry_run=self._config.dry_run,
)
```

#### Step 6: Return

```python
return report
```

### 4.3 Pseudocode (Complete)

```python
async def generate_report(
    self,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> LifecycleReport:
    """Generate a structured lifecycle report over all positions."""
    _ZERO = Decimal("0")
    report_time = datetime.now(timezone.utc)

    async with self._db_session_factory() as session:
        repo = PositionRepository(session)
        all_positions = await repo.get_all_positions()

    # Apply optional time range filter
    if start_date is not None:
        all_positions = [p for p in all_positions if p.routed_at_utc >= start_date]
    if end_date is not None:
        all_positions = [p for p in all_positions if p.routed_at_utc <= end_date]

    if not all_positions:
        logger.info("lifecycle.report_empty", dry_run=self._config.dry_run)
        return LifecycleReport(
            report_at_utc=report_time,
            total_settled_count=0,
            winning_count=0,
            losing_count=0,
            breakeven_count=0,
            total_realized_pnl=_ZERO,
            avg_hold_duration_hours=_ZERO,
            best_pnl=_ZERO,
            worst_pnl=_ZERO,
            entries=[],
            dry_run=self._config.dry_run,
        )

    # Separate settled vs open
    settled = [p for p in all_positions if p.status == "CLOSED" and p.realized_pnl is not None]
    open_positions = [p for p in all_positions if p.status == "OPEN"]

    # Build per-position lifecycle entries
    lifecycle_entries: list[PositionLifecycleEntry] = []
    for position in all_positions:
        entry_price_d = Decimal(str(position.entry_price))
        order_size_usdc_d = Decimal(str(position.order_size_usdc))

        if entry_price_d == _ZERO:
            size_tokens = _ZERO
        else:
            size_tokens = order_size_usdc_d / entry_price_d

        lifecycle_entries.append(
            PositionLifecycleEntry(
                position_id=str(position.id),
                slug=position.condition_id,
                entry_price=entry_price_d,
                exit_price=(
                    Decimal(str(position.exit_price))
                    if position.exit_price is not None
                    else None
                ),
                size_tokens=size_tokens,
                realized_pnl=(
                    Decimal(str(position.realized_pnl))
                    if position.realized_pnl is not None
                    else None
                ),
                status=position.status,
                opened_at_utc=position.routed_at_utc,
                settled_at_utc=position.closed_at_utc,
            )
        )

    # Compute aggregate statistics from settled positions
    if not settled:
        total_realized_pnl = _ZERO
        winning_count = 0
        losing_count = 0
        breakeven_count = 0
        avg_hold_duration_hours = _ZERO
        best_pnl = _ZERO
        worst_pnl = _ZERO
        total_settled_count = 0
    else:
        total_settled_count = len(settled)
        pnl_values: list[Decimal] = []
        winning_count = 0
        losing_count = 0
        breakeven_count = 0
        total_hold_seconds = Decimal("0")

        for position in settled:
            pnl = Decimal(str(position.realized_pnl))
            pnl_values.append(pnl)

            if pnl > _ZERO:
                winning_count += 1
            elif pnl < _ZERO:
                losing_count += 1
            else:
                breakeven_count += 1

            if position.closed_at_utc is not None and position.routed_at_utc is not None:
                delta = position.closed_at_utc - position.routed_at_utc
                total_hold_seconds += Decimal(str(delta.total_seconds()))

        total_realized_pnl = sum(pnl_values, _ZERO)
        best_pnl = max(pnl_values)
        worst_pnl = min(pnl_values)
        avg_hold_duration_hours = (
            total_hold_seconds / Decimal(str(total_settled_count)) / Decimal("3600")
        )

    result = LifecycleReport(
        report_at_utc=report_time,
        total_settled_count=total_settled_count,
        winning_count=winning_count,
        losing_count=losing_count,
        breakeven_count=breakeven_count,
        total_realized_pnl=total_realized_pnl,
        avg_hold_duration_hours=avg_hold_duration_hours,
        best_pnl=best_pnl,
        worst_pnl=worst_pnl,
        entries=lifecycle_entries,
        dry_run=self._config.dry_run,
    )
    logger.info(
        "lifecycle.report_generated",
        total_settled_count=result.total_settled_count,
        winning_count=result.winning_count,
        losing_count=result.losing_count,
        breakeven_count=result.breakeven_count,
        total_realized_pnl=str(result.total_realized_pnl),
        avg_hold_duration_hours=str(result.avg_hold_duration_hours),
        best_pnl=str(result.best_pnl),
        worst_pnl=str(result.worst_pnl),
        entry_count=len(result.entries),
        dry_run=result.dry_run,
    )
    return result
```

## 5. Pipeline Integration Design

### 5.1 Orchestrator Wiring

#### Construction (in `Orchestrator.__init__()`):

```python
self.lifecycle_reporter = PositionLifecycleReporter(
    config=self.config,
    db_session_factory=AsyncSessionLocal,
)
```

Placement: after `self.portfolio_aggregator` construction.

#### Invocation within `_portfolio_aggregation_loop()`:

After `compute_snapshot()` succeeds, invoke `generate_report()`:

```python
async def _portfolio_aggregation_loop(self) -> None:
    """Periodic portfolio snapshot aggregation (WI-23) and lifecycle reporting (WI-24)."""
    while True:
        await asyncio.sleep(
            float(self.config.portfolio_aggregation_interval_sec)
        )
        try:
            snapshot = await self.portfolio_aggregator.compute_snapshot()
        except Exception as exc:
            logger.error(
                "portfolio_aggregation_loop.error",
                error=str(exc),
            )
            continue

        try:
            report = await self.lifecycle_reporter.generate_report()
        except Exception as exc:
            logger.error(
                "lifecycle_report_loop.error",
                error=str(exc),
            )
            # report generation failure does not block the loop
```

Fail-open semantics: `generate_report()` failure is caught, logged, and does not terminate the loop. The snapshot from `compute_snapshot()` is still valid even if the report fails.

### 5.2 New PositionRepository Methods (Additive)

Three new query methods are added to `PositionRepository`. All existing methods remain unmodified.

#### `get_settled_positions()`

```python
async def get_settled_positions(self) -> list[Position]:
    """Return all CLOSED positions with non-null realized_pnl."""
    stmt = select(Position).where(
        Position.status == "CLOSED",
        Position.realized_pnl.isnot(None),
    )
    result = await self._session.execute(stmt)
    return list(result.scalars().all())
```

#### `get_all_positions()`

```python
async def get_all_positions(self) -> list[Position]:
    """Return all positions regardless of status."""
    stmt = select(Position)
    result = await self._session.execute(stmt)
    return list(result.scalars().all())
```

#### `get_positions_by_status(status: str)`

```python
async def get_positions_by_status(self, status: str) -> list[Position]:
    """Return all positions with the given status."""
    stmt = select(Position).where(Position.status == status)
    result = await self._session.execute(stmt)
    return list(result.scalars().all())
```

Hard constraints:
1. All three methods are SELECT-only queries. No INSERT, UPDATE, or DELETE.
2. They are additive — `insert_position()`, `get_by_id()`, `get_open_by_condition_id()`, `get_open_positions()`, `update_status()`, and `record_settlement()` remain completely unmodified.
3. Each method creates its own `select()` statement. No shared mutable query objects.

### 5.3 No New AppConfig Fields

WI-24 does not introduce new configuration fields. The reporter uses only the existing `dry_run` flag from `AppConfig`. Time range filtering is controlled by method parameters, not configuration.

### 5.4 No New Periodic Task

`PositionLifecycleReporter` does NOT get its own `asyncio.create_task()`. It runs within the existing `_portfolio_aggregation_loop()` (after `compute_snapshot()`), leveraging the already-configured `portfolio_aggregation_interval_sec` timing. Ad-hoc invocation is also supported for CLI/manual reporting.

## 6. Failure Semantics (Fail-Open, Never Kill the Loop)

| Failure scenario | Behavior | Rationale |
|---|---|---|
| `get_all_positions()` raises `Exception` | Caught by `except Exception` in loop body. `lifecycle_report_loop.error` logged. Loop sleeps and retries next cycle. | DB read failure is transient. Next cycle may succeed. |
| `entry_price == Decimal("0")` for a position | `size_tokens = Decimal("0")`. No division-by-zero error. | Degenerate positions get zero token size. |
| `closed_at_utc` or `routed_at_utc` is `None` for a settled position | That position's hold duration is excluded from the average. | Defensive: missing timestamps should not crash the report. |
| Zero settled positions (only open positions exist) | Aggregate stats are all zero/`Decimal("0")`. `entries` list still contains open-position entries. | Valid report with no settled data yet. |
| Zero positions total (empty database) | `lifecycle.report_empty` logged. Zero-valued report with empty `entries` returned. | Startup case — report is valid but empty. |
| `generate_report()` raises in `_portfolio_aggregation_loop()` | Caught in loop body. Logged. Loop continues to next iteration. | Report generation is informational; failure must not kill the analytics loop. |
| `asyncio.CancelledError` (shutdown) | Propagates naturally from the loop. | Standard asyncio shutdown pattern. |

Critical rule: **A `generate_report()` exception within `_portfolio_aggregation_loop()` must never re-raise.** The `except Exception` block is not optional — it is a hard requirement.

## 7. dry_run Behavior

WI-24 introduces **no new dry_run gate**. The lifecycle reporter is inherently read-only — it performs zero DB writes regardless of `dry_run`. The `dry_run` flag is included in the `LifecycleReport` for downstream audit consumers only.

| Phase | dry_run=True | dry_run=False |
|---|---|---|
| `get_all_positions()` — DB read | Reads all positions (read-path permitted) | Reads all positions |
| Per-position Decimal arithmetic | Computes full metrics | Computes full metrics |
| `LifecycleReport` construction | Includes `dry_run=True` field | Includes `dry_run=False` field |
| DB writes | **Zero** (component is read-only) | **Zero** (component is read-only) |
| Report log event | Emitted | Emitted |

## 8. structlog Audit Events

### 8.1 New Events (WI-24)

| Event Key | Level | When | Key Fields |
|---|---|---|---|
| `lifecycle.report_generated` | `INFO` | After a successful `generate_report()` call completes | `total_settled_count`, `winning_count`, `losing_count`, `breakeven_count`, `total_realized_pnl`, `avg_hold_duration_hours`, `best_pnl`, `worst_pnl`, `entry_count`, `dry_run` |
| `lifecycle.report_empty` | `INFO` | `generate_report()` finds zero positions | `dry_run` |
| `lifecycle_report_loop.error` | `ERROR` | `generate_report()` raised an exception in the aggregation loop | `error` |

### 8.2 Preserved Events (Unchanged)

All existing events from WI-23 (`PortfolioAggregator`), WI-22 (`_exit_scan_loop`), and all other components are unaffected by WI-24. No events are removed.

## 9. Module Isolation Rules

### 9.1 PositionLifecycleReporter Import Boundary

**Must NOT import:**
- `src/agents/context/` (prompt construction, context-building, `DataAggregator`)
- `src/agents/evaluation/` (`ClaudeClient`, `GrokClient`)
- `src/agents/ingestion/` (`CLOBWebSocketClient`, `GammaRESTClient`, `MarketDiscoveryEngine`)
- `src/schemas/llm.py` (`LLMEvaluationResponse`, `MarketContext`)
- `src/agents/execution/exit_strategy_engine.py` (`ExitStrategyEngine`)
- `src/agents/execution/exit_order_router.py` (`ExitOrderRouter`)
- `src/agents/execution/pnl_calculator.py` (`PnLCalculator`)
- `src/agents/execution/execution_router.py` (`ExecutionRouter`)
- `src/agents/execution/order_broadcaster.py` (`OrderBroadcaster`)
- `src/agents/execution/transaction_signer.py` (`TransactionSigner`)
- `src/agents/execution/polymarket_client.py` (`PolymarketClient`) — no live market data needed

**Allowed imports:**
- `src/core/config` → `AppConfig`
- `src/db/repositories/position_repository` → `PositionRepository`
- `src/db/models` → `Position`
- `src/schemas/risk` → `LifecycleReport`, `PositionLifecycleEntry`
- `sqlalchemy.ext.asyncio` → `AsyncSession`, `async_sessionmaker`
- `structlog`, `decimal.Decimal`, `datetime`

### 9.2 risk.py Schema Extension

`src/schemas/risk.py` gains `PositionLifecycleEntry` and `LifecycleReport`. The module remains a leaf schema module. It must only import:
- `pydantic` (`BaseModel`, `field_validator`)
- `decimal` (`Decimal`)
- `datetime` (`datetime`)
- `typing` (`Any`)

It must NOT import any `src/` module. No cross-schema imports.

## 10. Invariants Preserved

1. **Gatekeeper authority** — `LLMEvaluationResponse` remains the terminal pre-execution gate. `PositionLifecycleReporter` operates downstream and read-only. No bypass.
2. **Decimal financial integrity** — all reporting arithmetic is `Decimal`. Float is rejected at Pydantic boundary. No float intermediary in any arithmetic step.
3. **Quarter-Kelly policy** — `PositionLifecycleReporter` does not perform Kelly sizing. It reads position metadata (already Kelly-capped at entry) and computes reporting aggregates.
4. **`dry_run=True` blocks DB writes** — `PositionLifecycleReporter` performs zero DB writes regardless of `dry_run`. The flag is included for audit logging only.
5. **Repository pattern** — all DB reads go through `PositionRepository` methods. No direct session queries.
6. **Read-only semantics** — `PositionLifecycleReporter` never calls any repository write method (`insert_position`, `update_status`, `record_settlement`). Zero mutations.
7. **Additive repository changes** — `get_settled_positions()`, `get_all_positions()`, and `get_positions_by_status()` are new SELECT-only methods. All existing methods are unmodified.
8. **Async pipeline** — runs within the existing `_portfolio_aggregation_loop()` task. No new tasks or queues.
9. **Entry-path routing** — `ExecutionRouter` internals are unmodified.
10. **Exit-path routing** — `ExitOrderRouter` and `ExitStrategyEngine` internals are unmodified.
11. **PnL settlement** — `PnLCalculator` internals are unmodified.
12. **Module isolation** — zero imports from prompt, context, evaluation, or ingestion modules.
13. **Shutdown sequence** — no new tasks to cancel. Reporter runs within the existing `PortfolioAggregatorTask`.
14. **Queue topology unchanged** — `market_queue -> prompt_queue -> execution_queue`. No new queue.

## 11. Strict Acceptance Criteria (Maker Agent)

1. `PositionLifecycleReporter` exists in `src/agents/execution/lifecycle_reporter.py` as the canonical reporting class.
2. `generate_report(start_date, end_date) -> LifecycleReport` is the sole public async entry point.
3. `PositionLifecycleEntry` Pydantic model exists in `src/schemas/risk.py`, is frozen, Decimal-validated, with fields: `position_id`, `slug`, `entry_price`, `exit_price`, `size_tokens`, `realized_pnl`, `status`, `opened_at_utc`, `settled_at_utc`.
4. `LifecycleReport` Pydantic model exists in `src/schemas/risk.py`, is frozen, Decimal-validated, with fields: `report_at_utc`, `total_settled_count`, `winning_count`, `losing_count`, `breakeven_count`, `total_realized_pnl`, `avg_hold_duration_hours`, `best_pnl`, `worst_pnl`, `entries`, `dry_run`.
5. `LifecycleReport` rejects `float` in financial fields (`total_realized_pnl`, `avg_hold_duration_hours`, `best_pnl`, `worst_pnl`) at Pydantic boundary.
6. `PositionLifecycleEntry` rejects `float` in financial fields (`entry_price`, `exit_price`, `size_tokens`, `realized_pnl`) at Pydantic boundary.
7. `PositionRepository.get_settled_positions()` returns `CLOSED` positions where `realized_pnl IS NOT NULL`.
8. `PositionRepository.get_all_positions()` returns all positions regardless of status.
9. `PositionRepository.get_positions_by_status(status)` returns positions filtered by the given status string.
10. All three new repository methods are SELECT-only. Existing methods are completely unmodified.
11. `total_realized_pnl = sum(realized_pnl)` across all settled positions using Decimal arithmetic.
12. Win/loss classification: `realized_pnl > 0` is a win, `< 0` is a loss, `== 0` is breakeven.
13. `winning_count + losing_count + breakeven_count == total_settled_count`.
14. `avg_hold_duration_hours` is computed from `(closed_at_utc - routed_at_utc)` averaged across settled positions, using Decimal arithmetic.
15. `best_pnl = max(realized_pnl)` and `worst_pnl = min(realized_pnl)` across settled positions.
16. Zero settled positions returns `LifecycleReport` with all counts `0` and all financial fields `Decimal("0")`.
17. Zero positions total returns a zero-valued `LifecycleReport` with empty `entries` list and `lifecycle.report_empty` logged.
18. `entry_price == Decimal("0")` yields `size_tokens = Decimal("0")`. No division-by-zero error.
19. Optional `start_date`/`end_date` filter restricts positions by `routed_at_utc`.
20. `entries` list in `LifecycleReport` contains `PositionLifecycleEntry` for all matching positions (settled + open).
21. `PositionLifecycleReporter` is constructed in `Orchestrator.__init__()` with `config` and `db_session_factory`.
22. `generate_report()` is invoked within `_portfolio_aggregation_loop()` after `compute_snapshot()`, inside a separate `try/except`.
23. A `generate_report()` exception inside the loop is caught by `except Exception`, logged via `lifecycle_report_loop.error`, and does NOT re-raise or terminate the loop.
24. `PositionLifecycleReporter` has zero imports from prompt, context, evaluation, or ingestion modules.
25. `PositionLifecycleReporter` performs zero DB writes.
26. No modifications to `PnLCalculator`, `ExitStrategyEngine`, `ExitOrderRouter`, `ExecutionRouter`, or `PortfolioAggregator` internals.
27. `lifecycle.report_generated` structlog event is emitted at `INFO` level after each successful report with fields: `total_settled_count`, `winning_count`, `losing_count`, `breakeven_count`, `total_realized_pnl`, `avg_hold_duration_hours`, `best_pnl`, `worst_pnl`, `entry_count`, `dry_run`.
28. `lifecycle.report_empty` structlog event is emitted at `INFO` level when no positions exist, with field: `dry_run`.
29. `lifecycle_report_loop.error` structlog event is emitted at `ERROR` level when `generate_report()` raises, with field: `error`.
30. No new database tables, migrations, or schema changes.
31. No new `AppConfig` fields required.
32. Full regression remains green (`pytest --asyncio-mode=auto tests/`) with coverage >= 80%.

## 12. Verification Checklist (Test Matrix)

### Unit Tests

1. Unit test: `PositionLifecycleEntry` accepts `Decimal` in all financial fields and is frozen.
2. Unit test: `PositionLifecycleEntry` rejects `float` in `entry_price` at Pydantic boundary.
3. Unit test: `PositionLifecycleEntry` rejects `float` in `exit_price` at Pydantic boundary.
4. Unit test: `PositionLifecycleEntry` rejects `float` in `size_tokens` at Pydantic boundary.
5. Unit test: `PositionLifecycleEntry` rejects `float` in `realized_pnl` at Pydantic boundary.
6. Unit test: `PositionLifecycleEntry` accepts `None` for `exit_price`, `realized_pnl`, `settled_at_utc` (open position).
7. Unit test: `LifecycleReport` accepts `Decimal` in all financial fields and is frozen.
8. Unit test: `LifecycleReport` rejects `float` in `total_realized_pnl` at Pydantic boundary.
9. Unit test: `LifecycleReport` rejects `float` in `avg_hold_duration_hours` at Pydantic boundary.
10. Unit test: `LifecycleReport` rejects `float` in `best_pnl` at Pydantic boundary.
11. Unit test: `LifecycleReport` rejects `float` in `worst_pnl` at Pydantic boundary.
12. Unit test: `generate_report()` with zero positions returns zero-valued `LifecycleReport` with empty `entries`.
13. Unit test: `generate_report()` with one settled position returns correct aggregate stats and one entry.
14. Unit test: `generate_report()` with multiple settled positions aggregates correctly (verify summation, win/loss counts).
15. Unit test: `generate_report()` with mix of settled and open positions — aggregates use settled only, entries include all.
16. Unit test: `generate_report()` win/loss classification — positive PnL → win, negative → loss, zero → breakeven.
17. Unit test: `generate_report()` with `entry_price == Decimal("0")` — `size_tokens == Decimal("0")`, no error.
18. Unit test: `generate_report()` computes `avg_hold_duration_hours` correctly from `closed_at_utc - routed_at_utc`.
19. Unit test: `generate_report()` returns correct `best_pnl` and `worst_pnl` from settled positions.
20. Unit test: `generate_report()` with `start_date` filter excludes positions opened before the date.
21. Unit test: `generate_report()` with `end_date` filter excludes positions opened after the date.
22. Unit test: `generate_report()` with both `start_date` and `end_date` filters applies both.
23. Unit test: `lifecycle.report_generated` structlog event emitted with correct fields after successful report.
24. Unit test: `lifecycle.report_empty` structlog event emitted when no positions exist.

### Integration Tests

25. Integration test: `PositionRepository.get_settled_positions()` returns only `CLOSED` positions with non-null `realized_pnl`.
26. Integration test: `PositionRepository.get_all_positions()` returns all positions regardless of status.
27. Integration test: `PositionRepository.get_positions_by_status("OPEN")` returns only `OPEN` positions.
28. Integration test: `PositionRepository.get_positions_by_status("CLOSED")` returns only `CLOSED` positions.
29. Integration test: new repository methods do not modify existing methods — `get_open_positions()`, `insert_position()`, `update_status()`, `record_settlement()` behave identically.
30. Integration test: full `generate_report()` with in-memory SQLite — report matches expected values end-to-end.
31. Integration test: `generate_report()` within `_portfolio_aggregation_loop()` — verify invocation after `compute_snapshot()`.
32. Integration test: `generate_report()` exception within `_portfolio_aggregation_loop()` is caught and does NOT terminate the loop.
33. Integration test: `PositionLifecycleReporter` module has no dependency on prompt/context/evaluation/ingestion modules (import boundary check).
34. Integration test: `PositionLifecycleReporter` performs zero DB writes during `generate_report()` (verify no INSERT/UPDATE/DELETE statements).

### Regression Gate

35. Full suite: `pytest --asyncio-mode=auto tests/ -q` — all tests pass.
36. Coverage: `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` — >= 80%.
