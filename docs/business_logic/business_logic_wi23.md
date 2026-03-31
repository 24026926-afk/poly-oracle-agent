# WI-23 Business Logic — Portfolio Aggregator

## Active Agents + Constraints

- `.agents/rules/risk-auditor.md` — All financial aggregation (notional, PnL, collateral) uses `Decimal` arithmetic. No floats in computation or storage. `Decimal(str(value))` for any non-Decimal inputs.
- `.agents/rules/db-engineer.md` — Open position reads go through `PositionRepository.get_open_positions()` only. Zero direct `AsyncSession` calls outside repositories. No new tables or migrations required — WI-23 is read-only.
- `.agents/rules/security-auditor.md` — `dry_run=True` computes the full `PortfolioSnapshot` but affects logging verbosity only (component is already read-only). No credentials, private keys, or write paths touched.
- `.agents/rules/async-architect.md` — `PortfolioAggregator` runs as an optional background task in the Orchestrator, following the `_discovery_loop()` / `_exit_scan_loop()` lifecycle pattern: `asyncio.create_task()` in `start()`, cancellation in `shutdown()`. No new queue introduced.
- `.agents/rules/test-engineer.md` — WI-23 requires unit + integration coverage for snapshot computation, fail-open price fallback, config wiring, and background loop lifecycle. Full suite remains >= 80%.

## 1. Objective

Introduce `PortfolioAggregator`, a read-only analytics component that computes a real-time aggregate snapshot of all open positions: total notional exposure in USDC, unrealized PnL, position count, and locked collateral.

`PortfolioAggregator` owns:
- Periodic (configurable, default 30s) or on-demand snapshot computation
- Aggregation of per-position notional, unrealized PnL, and collateral
- Fail-open price resolution: if live price fetch fails, fall back to `entry_price` (PnL=0 for that position)
- `PortfolioSnapshot` Pydantic model construction and structured audit logging
- `dry_run` logging distinction (component is inherently read-only)

`PortfolioAggregator` does NOT own:
- Position lifecycle management (upstream: `PositionTracker`, WI-17)
- Exit evaluation or exit routing (upstream: `ExitStrategyEngine` WI-19, `ExitOrderRouter` WI-20)
- PnL settlement or DB writes (upstream: `PnLCalculator`, WI-21)
- Order execution, signing, or broadcasting
- Portfolio rebalancing, risk limits enforcement, or position sizing decisions
- Tax lot accounting, fee accounting, or historical PnL reporting

## 2. Scope Boundaries

### In Scope

1. New `PortfolioAggregator` class in `src/agents/execution/portfolio_aggregator.py`.
2. New `PortfolioSnapshot` Pydantic model in `src/schemas/risk.py` (new file).
3. New `Orchestrator._portfolio_aggregation_loop()` async method — the standalone periodic aggregation task.
4. New `AppConfig` fields: `enable_portfolio_aggregator: bool` (default `False`), `portfolio_aggregation_interval_sec: Decimal` (default `Decimal("30")`).
5. Registration of `_portfolio_aggregation_loop()` as `asyncio.create_task(..., name="PortfolioAggregatorTask")` in `Orchestrator.start()`, conditional on `config.enable_portfolio_aggregator`.
6. Fire-and-forget error handling within the loop body: a failed aggregation iteration logs and continues.
7. Graceful shutdown: `PortfolioAggregatorTask` (if created) is cancelled alongside all other tasks in `Orchestrator.shutdown()`.
8. On-demand `compute_snapshot()` public method for ad-hoc callers.
9. structlog audit events for loop lifecycle and snapshot results.

### Out of Scope

1. New database tables, migrations, or DB writes — this component is strictly read-only.
2. Historical snapshot persistence or time-series storage.
3. Portfolio rebalancing, risk limit enforcement, or position sizing adjustment.
4. Real-time WebSocket streaming of snapshots to external consumers.
5. Unrealized PnL for non-`OPEN` positions (only `OPEN` positions are aggregated).
6. Fee accounting (CLOB fees, gas costs) in notional or PnL calculations.
7. Modifications to `PositionTracker`, `PositionRepository`, `ExitStrategyEngine`, `ExitOrderRouter`, `PnLCalculator`, or `ExecutionRouter` internals.
8. Dynamic interval adjustment — the interval is read once from `AppConfig` at construction time.

## 3. Target Component Architecture + Data Contracts

### 3.1 PortfolioAggregator Component (New Class)

- **Module:** `src/agents/execution/portfolio_aggregator.py`
- **Class Name:** `PortfolioAggregator` (exact)
- **Responsibility:** Read all open positions from `PositionRepository`, fetch current prices from `PolymarketClient`, compute aggregate portfolio metrics, and return a typed `PortfolioSnapshot`.

Isolation rules:
- `PortfolioAggregator` must not import LLM prompt construction, context-building, evaluation, or ingestion modules.
- `PortfolioAggregator` must not mutate position status or write to the database.
- `PortfolioAggregator` must not influence routing, exit decisions, or any upstream component.
- `PortfolioAggregator` does not call `TransactionSigner`, `OrderBroadcaster`, or `BankrollSyncProvider`.

### 3.2 Data Contracts

#### 3.2.1 `PortfolioSnapshot` model (New)

Location: `src/schemas/risk.py` (new file)

```python
class PortfolioSnapshot(BaseModel):
    """Typed aggregate portfolio state at a point in time."""

    snapshot_at_utc: datetime
    position_count: int
    total_notional_usdc: Decimal
    total_unrealized_pnl: Decimal
    total_locked_collateral_usdc: Decimal
    positions_with_stale_price: int
    dry_run: bool

    @field_validator(
        "total_notional_usdc",
        "total_unrealized_pnl",
        "total_locked_collateral_usdc",
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
- All three financial fields are `Decimal`. Float is rejected at Pydantic boundary.
- Model is frozen (immutable after construction).
- `positions_with_stale_price` tracks how many positions fell back to entry_price due to price fetch failure.
- `dry_run` reflects config state for downstream audit consumers.

Field definitions:

| Field | Type | Description |
|-------|------|-------------|
| `snapshot_at_utc` | `datetime` | UTC timestamp when the snapshot was computed |
| `position_count` | `int` | Number of `OPEN` positions included in the snapshot |
| `total_notional_usdc` | `Decimal` | Sum of `current_price * position_size_tokens` across all open positions |
| `total_unrealized_pnl` | `Decimal` | Sum of `(current_price - entry_price) * position_size_tokens` across all open positions |
| `total_locked_collateral_usdc` | `Decimal` | Sum of `order_size_usdc` across all open positions (USDC committed at entry) |
| `positions_with_stale_price` | `int` | Count of positions where price fetch failed and `entry_price` was used as fallback |
| `dry_run` | `bool` | Whether the system is in dry-run mode |

### 3.3 Per-Position Intermediate Calculations

For each open position, the aggregator computes:

```
position_size_tokens = order_size_usdc / entry_price   (Decimal division)
current_notional     = current_price * position_size_tokens
unrealized_pnl       = (current_price - entry_price) * position_size_tokens
locked_collateral    = order_size_usdc                 (USDC committed at entry)
```

Where `current_price` is:
- `MarketSnapshot.midpoint_probability` from `PolymarketClient.fetch_order_book(token_id)` if the fetch succeeds.
- `entry_price` (fallback) if the fetch fails or returns `None`. This produces `unrealized_pnl = Decimal("0")` for that position.

Division-by-zero guard: if `entry_price == Decimal("0")`, set `position_size_tokens = Decimal("0")`, which cascades to `current_notional = Decimal("0")` and `unrealized_pnl = Decimal("0")`.

## 4. Core Method Contracts (async, typed)

### 4.1 Constructor

```python
class PortfolioAggregator:
    def __init__(
        self,
        config: AppConfig,
        polymarket_client: PolymarketClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
```

Dependencies:
1. `config: AppConfig` — `dry_run` flag.
2. `polymarket_client: PolymarketClient` — for fetching current prices via `fetch_order_book()`.
3. `db_session_factory: async_sessionmaker[AsyncSession]` — for constructing `PositionRepository` within a session context.

No `TransactionSigner`, `OrderBroadcaster`, or `BankrollSyncProvider` — this is a read-only analytics component.

### 4.2 Async Snapshot Entry Point

```python
async def compute_snapshot(self) -> PortfolioSnapshot:
```

This is the sole public async method. Behavior:

#### Step 1: Load Open Positions

```python
async with self._db_session_factory() as session:
    repo = PositionRepository(session)
    open_positions: list[Position] = await repo.get_open_positions()
```

Read all `OPEN` positions from the database. If the list is empty, return a zero-valued `PortfolioSnapshot` immediately (no price fetches needed).

#### Step 2: Fetch Current Prices (Fail-Open)

For each open position, fetch the current price:

```python
snapshot = await self._polymarket_client.fetch_order_book(position.token_id)

if snapshot is not None:
    current_price = snapshot.midpoint_probability
    stale = False
else:
    current_price = Decimal(str(position.entry_price))
    stale = True
    logger.warning(
        "portfolio.price_fetch_failed",
        position_id=position.id,
        token_id=position.token_id,
        fallback="entry_price",
    )
```

Fail-open semantics: if `fetch_order_book()` returns `None` (timeout, connection error, empty book, crossed book), use `entry_price` as the current price. This yields `unrealized_pnl = Decimal("0")` for that position — a conservative, safe fallback.

Price fetches are sequential (not `asyncio.gather`) to respect CLOB rate limits. With a typical portfolio of <50 positions and 500ms timeout per fetch, the full scan completes in <25 seconds.

#### Step 3: Compute Per-Position Metrics

```python
_ZERO = Decimal("0")

entry_price_d = Decimal(str(position.entry_price))
order_size_usdc_d = Decimal(str(position.order_size_usdc))

if entry_price_d == _ZERO:
    position_size_tokens = _ZERO
else:
    position_size_tokens = order_size_usdc_d / entry_price_d

current_notional = current_price * position_size_tokens
unrealized_pnl = (current_price - entry_price_d) * position_size_tokens
locked_collateral = order_size_usdc_d
```

All arithmetic is `Decimal`. No `float()` conversion at any step.

#### Step 4: Aggregate

```python
total_notional_usdc += current_notional
total_unrealized_pnl += unrealized_pnl
total_locked_collateral_usdc += locked_collateral
position_count += 1
if stale:
    positions_with_stale_price += 1
```

Running sums accumulated across all open positions.

#### Step 5: Build PortfolioSnapshot

```python
snapshot = PortfolioSnapshot(
    snapshot_at_utc=datetime.now(timezone.utc),
    position_count=position_count,
    total_notional_usdc=total_notional_usdc,
    total_unrealized_pnl=total_unrealized_pnl,
    total_locked_collateral_usdc=total_locked_collateral_usdc,
    positions_with_stale_price=positions_with_stale_price,
    dry_run=self._config.dry_run,
)
```

#### Step 6: Log Snapshot

```python
logger.info(
    "portfolio.snapshot_computed",
    position_count=snapshot.position_count,
    total_notional_usdc=str(snapshot.total_notional_usdc),
    total_unrealized_pnl=str(snapshot.total_unrealized_pnl),
    total_locked_collateral_usdc=str(snapshot.total_locked_collateral_usdc),
    positions_with_stale_price=snapshot.positions_with_stale_price,
    dry_run=snapshot.dry_run,
)
```

This event is emitted regardless of `dry_run` — the computation always happens.

#### Step 7: Return

```python
return snapshot
```

### 4.3 Pseudocode (Complete)

```python
async def compute_snapshot(self) -> PortfolioSnapshot:
    """Compute aggregate portfolio metrics from all open positions."""
    _ZERO = Decimal("0")
    snapshot_time = datetime.now(timezone.utc)

    async with self._db_session_factory() as session:
        repo = PositionRepository(session)
        open_positions = await repo.get_open_positions()

    if not open_positions:
        empty = PortfolioSnapshot(
            snapshot_at_utc=snapshot_time,
            position_count=0,
            total_notional_usdc=_ZERO,
            total_unrealized_pnl=_ZERO,
            total_locked_collateral_usdc=_ZERO,
            positions_with_stale_price=0,
            dry_run=self._config.dry_run,
        )
        logger.info(
            "portfolio.snapshot_computed",
            position_count=0,
            total_notional_usdc="0",
            total_unrealized_pnl="0",
            total_locked_collateral_usdc="0",
            positions_with_stale_price=0,
            dry_run=self._config.dry_run,
        )
        return empty

    total_notional_usdc = _ZERO
    total_unrealized_pnl = _ZERO
    total_locked_collateral_usdc = _ZERO
    position_count = 0
    positions_with_stale_price = 0

    for position in open_positions:
        entry_price_d = Decimal(str(position.entry_price))
        order_size_usdc_d = Decimal(str(position.order_size_usdc))

        # Fetch current price (fail-open)
        snapshot = await self._polymarket_client.fetch_order_book(
            position.token_id
        )
        if snapshot is not None:
            current_price = snapshot.midpoint_probability
        else:
            current_price = entry_price_d
            positions_with_stale_price += 1
            logger.warning(
                "portfolio.price_fetch_failed",
                position_id=str(position.id),
                token_id=position.token_id,
                fallback="entry_price",
            )

        # Per-position computation
        if entry_price_d == _ZERO:
            position_size_tokens = _ZERO
        else:
            position_size_tokens = order_size_usdc_d / entry_price_d

        current_notional = current_price * position_size_tokens
        unrealized_pnl = (current_price - entry_price_d) * position_size_tokens

        total_notional_usdc += current_notional
        total_unrealized_pnl += unrealized_pnl
        total_locked_collateral_usdc += order_size_usdc_d
        position_count += 1

    result = PortfolioSnapshot(
        snapshot_at_utc=snapshot_time,
        position_count=position_count,
        total_notional_usdc=total_notional_usdc,
        total_unrealized_pnl=total_unrealized_pnl,
        total_locked_collateral_usdc=total_locked_collateral_usdc,
        positions_with_stale_price=positions_with_stale_price,
        dry_run=self._config.dry_run,
    )
    logger.info(
        "portfolio.snapshot_computed",
        position_count=result.position_count,
        total_notional_usdc=str(result.total_notional_usdc),
        total_unrealized_pnl=str(result.total_unrealized_pnl),
        total_locked_collateral_usdc=str(result.total_locked_collateral_usdc),
        positions_with_stale_price=result.positions_with_stale_price,
        dry_run=result.dry_run,
    )
    return result
```

## 5. Pipeline Integration Design

### 5.1 Orchestrator Wiring

#### Construction (in `Orchestrator.__init__()`):

```python
self.portfolio_aggregator = PortfolioAggregator(
    config=self.config,
    polymarket_client=self.polymarket_client,
    db_session_factory=AsyncSessionLocal,
)
```

Placement: after `self.pnl_calculator` construction.

#### New Async Loop Method (on `Orchestrator`):

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

Sleep-first pattern, consistent with `_discovery_loop()` and `_exit_scan_loop()`. The first snapshot fires after one full interval, giving the pipeline time to discover markets and record positions.

#### Task Registration (in `Orchestrator.start()`):

Conditional on config flag:

```python
if self.config.enable_portfolio_aggregator:
    self._tasks.append(
        asyncio.create_task(
            self._portfolio_aggregation_loop(),
            name="PortfolioAggregatorTask",
        )
    )
```

Appended after the existing 6 tasks. When `enable_portfolio_aggregator` is `False` (default), no task is created — zero runtime overhead.

### 5.2 New AppConfig Fields (Required)

Two new fields must be added to `AppConfig` in `src/core/config.py`:

```python
# --- Portfolio Aggregator (WI-23) ---
enable_portfolio_aggregator: bool = Field(
    default=False,
    description="Enable periodic portfolio snapshot aggregation",
)
portfolio_aggregation_interval_sec: Decimal = Field(
    default=Decimal("30"),
    description="Seconds between periodic portfolio snapshot computations",
)
```

Hard constraints:
1. `enable_portfolio_aggregator` defaults to `False` — opt-in feature. Does not affect existing pipeline when disabled.
2. `portfolio_aggregation_interval_sec` type is `Decimal`, consistent with `exit_scan_interval_seconds`.
3. Default is `Decimal("30")` — one snapshot every 30 seconds. Conservative: frequent enough for monitoring without overwhelming the CLOB endpoint.
4. Converted to `float` only at the `asyncio.sleep()` call boundary.
5. Both fields are read once at construction/loop time. Not dynamically adjustable at runtime.

### 5.3 Orchestrator.shutdown() — No Change Required

`shutdown()` already cancels all tasks in `self._tasks` and gathers with `return_exceptions=True`. Because `PortfolioAggregatorTask` is appended to the same `_tasks` list (when enabled), it is automatically cancelled and awaited during shutdown.

### 5.4 Task List (After WI-23)

When `enable_portfolio_aggregator=True`, the `_tasks` list contains 7 entries:

```python
self._tasks = [
    asyncio.create_task(self.ws_client.run(), name="IngestionTask"),
    asyncio.create_task(self.aggregator.start(), name="ContextTask"),
    asyncio.create_task(self.claude_client.start(), name="EvaluationTask"),
    asyncio.create_task(self._execution_consumer_loop(), name="ExecutionTask"),
    asyncio.create_task(self._discovery_loop(), name="DiscoveryTask"),
    asyncio.create_task(self._exit_scan_loop(), name="ExitScanTask"),
    # Conditional:
    asyncio.create_task(self._portfolio_aggregation_loop(), name="PortfolioAggregatorTask"),
]
```

When `enable_portfolio_aggregator=False` (default), the list remains 6 entries — identical to post-WI-22 state.

## 6. Failure Semantics (Fail-Open, Never Kill the Loop)

| Failure scenario | Behavior | Rationale |
|---|---|---|
| `fetch_order_book(token_id)` returns `None` (timeout, error, empty book) | Use `entry_price` as `current_price`. `unrealized_pnl = Decimal("0")` for that position. Increment `positions_with_stale_price`. Log `portfolio.price_fetch_failed` warning. | Fail-open: a missing price should not block the entire snapshot. Entry-price fallback is conservative (PnL=0). |
| `get_open_positions()` raises `Exception` | Caught by `except Exception` in loop body. `portfolio_aggregation_loop.error` logged. Loop sleeps and retries next cycle. | DB read failure is transient. Next cycle may succeed. |
| `entry_price == Decimal("0")` for a position | `position_size_tokens = Decimal("0")`. `current_notional = Decimal("0")`, `unrealized_pnl = Decimal("0")`. No exception raised. | Division-by-zero guard. Degenerate positions contribute zero to aggregates. |
| Unexpected `Exception` during `compute_snapshot()` | Caught in loop body. Logged. Loop continues. | Defensive catch-all. No exception should kill the loop. |
| `asyncio.CancelledError` (shutdown) | Propagates naturally. Loop exits. `gather()` collects it. | Standard asyncio shutdown pattern. |
| All price fetches fail | `PortfolioSnapshot` with `positions_with_stale_price == position_count`. `total_unrealized_pnl == Decimal("0")`. Logged normally. | Degraded but still valid snapshot. Monitoring can alert on high stale count. |
| `config.portfolio_aggregation_interval_sec` is zero or negative | `asyncio.sleep(0)` creates a tight loop. **Not guarded by WI-23.** A Pydantic validator could enforce `> 0`, but is out of scope unless the Maker adds one. | Misconfiguration. Document as a known edge case. |

Critical rule: **`_portfolio_aggregation_loop()` must never re-raise an exception from `compute_snapshot()`.** The `except Exception` block is not optional — it is a hard requirement.

## 7. dry_run Behavior

WI-23 introduces **no new dry_run gate**. The portfolio aggregator is inherently read-only — it performs zero DB writes regardless of `dry_run`. The `dry_run` flag is included in the `PortfolioSnapshot` for downstream audit consumers only.

| Phase | dry_run=True | dry_run=False |
|---|---|---|
| `get_open_positions()` — DB read | Reads `OPEN` positions (read-path permitted) | Reads `OPEN` positions |
| `fetch_order_book()` — CLOB read | Fetches current prices (read-only) | Fetches current prices |
| Per-position Decimal arithmetic | Computes full metrics | Computes full metrics |
| `PortfolioSnapshot` construction | Includes `dry_run=True` field | Includes `dry_run=False` field |
| DB writes | **Zero** (component is read-only) | **Zero** (component is read-only) |
| Snapshot log event | Emitted | Emitted |

The `dry_run` enforcement is documented for consistency with the project's audit logging pattern, not because `PortfolioAggregator` has a write path to gate.

## 8. structlog Audit Events

### 8.1 New Events (WI-23)

| Event Key | Level | When | Key Fields |
|---|---|---|---|
| `portfolio.snapshot_computed` | `INFO` | After a successful `compute_snapshot()` call completes | `position_count`, `total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc`, `positions_with_stale_price`, `dry_run` |
| `portfolio.price_fetch_failed` | `WARNING` | `fetch_order_book()` returns `None` for a position | `position_id`, `token_id`, `fallback` |
| `portfolio_aggregation_loop.error` | `ERROR` | `compute_snapshot()` raised an exception in the periodic loop | `error` |

### 8.2 Preserved Events (Unchanged)

All existing events from WI-14 (`PolymarketClient`), WI-19 (`ExitStrategyEngine`), WI-22 (`_exit_scan_loop`), and all other components are unaffected by WI-23. No events are removed.

## 9. Module Isolation Rules

### 9.1 PortfolioAggregator Import Boundary

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

**Allowed imports:**
- `src/core/config` → `AppConfig`
- `src/agents/execution/polymarket_client` → `PolymarketClient`, `MarketSnapshot`
- `src/db/repositories/position_repository` → `PositionRepository`
- `src/db/models` → `Position`
- `src/schemas/risk` → `PortfolioSnapshot`
- `sqlalchemy.ext.asyncio` → `AsyncSession`, `async_sessionmaker`
- `structlog`, `decimal.Decimal`, `datetime`

### 9.2 risk.py Schema Module Import Boundary

`src/schemas/risk.py` is a leaf schema module. It must only import:
- `pydantic` (`BaseModel`, `field_validator`)
- `decimal` (`Decimal`)
- `datetime` (`datetime`)
- `typing` (`Any`)

It must NOT import any `src/` module. No cross-schema imports.

## 10. Invariants Preserved

1. **Gatekeeper authority** — `LLMEvaluationResponse` remains the terminal pre-execution gate. `PortfolioAggregator` operates downstream and read-only. No bypass.
2. **Decimal financial integrity** — all aggregation arithmetic is `Decimal`. Float is rejected at Pydantic boundary. No float intermediary in any arithmetic step.
3. **Quarter-Kelly policy** — `PortfolioAggregator` does not perform Kelly sizing. It reads position metadata (already Kelly-capped at entry) and computes accounting aggregates.
4. **`dry_run=True` blocks DB writes** — `PortfolioAggregator` performs zero DB writes regardless of `dry_run`. The flag is included for audit logging only.
5. **Repository pattern** — all DB reads go through `PositionRepository.get_open_positions()`. No direct session queries.
6. **Read-only semantics** — `PortfolioAggregator` never calls any repository write method (`insert_position`, `update_status`, `record_settlement`). Zero mutations.
7. **Async pipeline** — runs within the existing Orchestrator task lifecycle. No new queues. Task is optional (config-gated).
8. **Entry-path routing** — `ExecutionRouter` internals are unmodified.
9. **Exit-path routing** — `ExitOrderRouter` and `ExitStrategyEngine` internals are unmodified.
10. **PnL settlement** — `PnLCalculator` internals are unmodified.
11. **Module isolation** — zero imports from prompt, context, evaluation, or ingestion modules.
12. **Shutdown sequence** — `PortfolioAggregatorTask` is cancelled via the existing `self._tasks` lifecycle.
13. **Queue topology unchanged** — `market_queue -> prompt_queue -> execution_queue`. No new queue.
14. **Fail-open price resolution** — a failed price fetch never blocks snapshot computation or the aggregation loop.

## 11. Strict Acceptance Criteria (Maker Agent)

1. `PortfolioAggregator` exists in `src/agents/execution/portfolio_aggregator.py` as the canonical aggregation class.
2. `compute_snapshot() -> PortfolioSnapshot` is the sole public async entry point.
3. `PortfolioSnapshot` Pydantic model exists in `src/schemas/risk.py`, is frozen, Decimal-validated, with fields: `snapshot_at_utc`, `position_count`, `total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc`, `positions_with_stale_price`, `dry_run`.
4. `PortfolioSnapshot` rejects `float` in financial fields (`total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc`) at Pydantic boundary.
5. Open positions are loaded via `PositionRepository.get_open_positions()` — no direct session queries.
6. Current prices are fetched via `PolymarketClient.fetch_order_book(token_id)` — one call per open position.
7. If `fetch_order_book()` returns `None`, `entry_price` is used as fallback (`unrealized_pnl = Decimal("0")` for that position), `positions_with_stale_price` is incremented, and `portfolio.price_fetch_failed` warning is logged.
8. `total_notional_usdc = sum(current_price * position_size_tokens)` across all open positions, using Decimal arithmetic.
9. `total_unrealized_pnl = sum((current_price - entry_price) * position_size_tokens)` across all open positions, using Decimal arithmetic.
10. `total_locked_collateral_usdc = sum(order_size_usdc)` across all open positions.
11. `position_size_tokens = order_size_usdc / entry_price` using Decimal division, with division-by-zero guard (`entry_price == Decimal("0")` yields `position_size_tokens = Decimal("0")`).
12. `AppConfig.enable_portfolio_aggregator` exists as a `bool` field with default `False`.
13. `AppConfig.portfolio_aggregation_interval_sec` exists as a `Decimal` field with default `Decimal("30")`.
14. `Orchestrator._portfolio_aggregation_loop()` exists as an `async` method that calls `self.portfolio_aggregator.compute_snapshot()` inside a `while True` loop.
15. `asyncio.sleep(float(self.config.portfolio_aggregation_interval_sec))` is the first statement inside the loop body (sleep-first pattern).
16. `_portfolio_aggregation_loop()` is registered as `asyncio.create_task(self._portfolio_aggregation_loop(), name="PortfolioAggregatorTask")` in `Orchestrator.start()` **only if** `self.config.enable_portfolio_aggregator is True`.
17. When `enable_portfolio_aggregator=False`, no `PortfolioAggregatorTask` is created. Zero runtime overhead.
18. A `compute_snapshot()` exception inside the loop is caught by `except Exception`, logged via `portfolio_aggregation_loop.error`, and does NOT re-raise or terminate the loop.
19. `PortfolioAggregatorTask` (if created) is cancelled during `Orchestrator.shutdown()` via the existing `self._tasks` cancellation loop.
20. `PortfolioAggregator` is constructed in `Orchestrator.__init__()` with `config`, `polymarket_client`, and `db_session_factory`.
21. `PortfolioAggregator` has zero imports from prompt, context, evaluation, or ingestion modules.
22. `PortfolioAggregator` performs zero DB writes.
23. No modifications to `PositionRepository`, `PositionTracker`, `ExitStrategyEngine`, `ExitOrderRouter`, `PnLCalculator`, or `ExecutionRouter`.
24. `portfolio.snapshot_computed` structlog event is emitted at `INFO` level after each successful snapshot with fields: `position_count`, `total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc`, `positions_with_stale_price`, `dry_run`.
25. `portfolio.price_fetch_failed` structlog event is emitted at `WARNING` level when a price fetch fails, with fields: `position_id`, `token_id`, `fallback`.
26. `portfolio_aggregation_loop.error` structlog event is emitted at `ERROR` level when `compute_snapshot()` raises, with field: `error`.
27. No new database tables, migrations, or schema changes.
28. Full regression remains green (`pytest --asyncio-mode=auto tests/`) with coverage >= 80%.

## 12. Verification Checklist (Test Matrix)

### Unit Tests

1. Unit test: `PortfolioSnapshot` accepts `Decimal` in all financial fields and is frozen.
2. Unit test: `PortfolioSnapshot` rejects `float` in `total_notional_usdc` at Pydantic boundary.
3. Unit test: `PortfolioSnapshot` rejects `float` in `total_unrealized_pnl` at Pydantic boundary.
4. Unit test: `PortfolioSnapshot` rejects `float` in `total_locked_collateral_usdc` at Pydantic boundary.
5. Unit test: `compute_snapshot()` with zero open positions returns `PortfolioSnapshot(position_count=0, total_notional_usdc=Decimal("0"), ...)`.
6. Unit test: `compute_snapshot()` with one open position and successful price fetch returns correct `total_notional_usdc`, `total_unrealized_pnl`, `total_locked_collateral_usdc`.
7. Unit test: `compute_snapshot()` with multiple open positions aggregates correctly (verify summation).
8. Unit test: `compute_snapshot()` when `fetch_order_book()` returns `None` for a position — falls back to `entry_price`, `unrealized_pnl == Decimal("0")` for that position, `positions_with_stale_price == 1`.
9. Unit test: `compute_snapshot()` when all price fetches fail — `positions_with_stale_price == position_count`, `total_unrealized_pnl == Decimal("0")`.
10. Unit test: `compute_snapshot()` with `entry_price == Decimal("0")` — `position_size_tokens == Decimal("0")`, no division-by-zero error.
11. Unit test: `compute_snapshot()` correctly computes `position_size_tokens = order_size_usdc / entry_price` for known inputs.
12. Unit test: `compute_snapshot()` with profitable position: `exit_price > entry_price` → positive `total_unrealized_pnl`.
13. Unit test: `compute_snapshot()` with losing position: `current_price < entry_price` → negative `total_unrealized_pnl`.
14. Unit test: `portfolio.snapshot_computed` structlog event emitted with correct fields after successful snapshot.
15. Unit test: `portfolio.price_fetch_failed` structlog event emitted when price fetch returns `None`.
16. Unit test: `AppConfig` accepts `enable_portfolio_aggregator` as `bool` with default `False`.
17. Unit test: `AppConfig` accepts `portfolio_aggregation_interval_sec` as `Decimal` with default `Decimal("30")`.
18. Unit test: `AppConfig` accepts `portfolio_aggregation_interval_sec` overridden via environment variable.

### Integration Tests

19. Integration test: `Orchestrator.start()` with `enable_portfolio_aggregator=True` creates `PortfolioAggregatorTask` — verify task name in `self._tasks`.
20. Integration test: `Orchestrator.start()` with `enable_portfolio_aggregator=False` does NOT create `PortfolioAggregatorTask` — task list has exactly 6 entries.
21. Integration test: `PortfolioAggregatorTask` is cancelled cleanly during `Orchestrator.shutdown()` without raising.
22. Integration test: full `compute_snapshot()` with in-memory SQLite and mocked `PolymarketClient` — snapshot matches expected values end-to-end.
23. Integration test: `_portfolio_aggregation_loop()` fires after the configured interval and calls `compute_snapshot()`.
24. Integration test: `_portfolio_aggregation_loop()` catches `Exception` from `compute_snapshot()` and does NOT re-raise — loop continues to next iteration.
25. Integration test: `PortfolioAggregator` module has no dependency on prompt/context/evaluation/ingestion modules (import boundary check).
26. Integration test: `PortfolioAggregator` performs zero DB writes during `compute_snapshot()` (verify no INSERT/UPDATE/DELETE statements).

### Regression Gate

27. Full suite: `pytest --asyncio-mode=auto tests/ -q` — all tests pass.
28. Coverage: `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` — >= 80%.
