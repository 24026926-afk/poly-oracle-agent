# WI-20 Business Logic — Exit Order Router (Route Exit Decisions to Signed SELL Orders)

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — `ExitOrderRouter` is async; upstream calls (`fetch_order_book`, `sign_order`) are awaited with fail-closed semantics.
- `.agents/rules/web3-specialist.md` — order signing delegates to `TransactionSigner.sign_order()`; router never handles private keys, nonces, or raw EIP-712 encoding.
- `.agents/rules/risk-auditor.md` — exit sizing uses the position's recorded `order_size_usdc`; no Kelly recalculation on the exit path. All pricing and sizing is `Decimal`; no `float` intermediary.
- `.agents/rules/security-auditor.md` — `dry_run=True` builds and logs the full exit order payload but never calls `sign_order()` or submits to CLOB. No credentials in structured logs.
- `.agents/rules/test-engineer.md` — WI-20 routing behavior requires unit + integration coverage; full suite remains >= 80%.

## 1. Objective

Introduce `ExitOrderRouter`, the downstream component that consumes an `ExitResult(should_exit=True)` produced by `ExitStrategyEngine` (WI-19) and converts it into a signed SELL-side limit order payload for Polymarket CLOB submission. This mirrors the WI-16 `ExecutionRouter` pattern, adapted exclusively for the exit path.

`ExitOrderRouter` owns:
- Fresh order-book fetch to determine realistic exit price (`best_bid`)
- SELL-side `OrderData` construction using position metadata
- Exit-specific slippage guard (`best_bid >= exit_min_bid_tolerance`)
- `dry_run` gate enforcement
- Signing delegation to `TransactionSigner`

`ExitOrderRouter` does NOT own:
- Exit decision logic (upstream: `ExitStrategyEngine`)
- Order broadcast (downstream: `OrderBroadcaster`)
- Position status mutation (already occurred in `ExitStrategyEngine`)
- PnL computation (downstream: `PnLCalculator`, WI-21)

## 2. Scope Boundaries

### In Scope

1. New `ExitOrderRouter` class in `src/agents/execution/exit_order_router.py`.
2. New `ExitOrderAction` enum and `ExitOrderResult` Pydantic model in `src/schemas/execution.py`.
3. Fresh order-book fetch via `PolymarketClient.fetch_order_book(token_id)` using the position's `token_id` — NOT `condition_id`.
4. Exit price determination: `best_bid` from the fresh order-book snapshot.
5. SELL-side `OrderData` construction: `side=OrderSide.SELL`, sizing derived from position metadata.
6. Exit slippage guard: reject when `best_bid < exit_min_bid_tolerance`.
7. `dry_run=True`: build and log full `ExitOrderResult` with `OrderData`; never call `sign_order()`.
8. `signer=None` fail-closed behavior in live mode.
9. New `AppConfig` field: `exit_min_bid_tolerance: Decimal` (default `Decimal("0.01")`).
10. New `ExitRoutingError` exception in `src/core/exceptions.py`.
11. Orchestrator wiring: construct in `__init__()`, invoke in `_exit_scan_loop()`.

### Out of Scope

1. Order broadcast (`OrderBroadcaster` handles downstream).
2. PnL computation (deferred to WI-21 `PnLCalculator`).
3. Modifications to `ExecutionRouter`, `ExitStrategyEngine`, `PositionTracker`, or `PositionRepository` internals.
4. Kelly re-sizing for exits — size is derived from position's existing `order_size_usdc`, not recalculated.
5. Partial exits or position scaling — exits are full-position only.
6. LLM-assisted exit reasoning.
7. Database writes — the router produces a signed order; no DB mutation.

## 3. Target Component Architecture + Data Contracts

### 3.1 ExitOrderRouter Component (New Class)

- **Module:** `src/agents/execution/exit_order_router.py`
- **Class Name:** `ExitOrderRouter` (exact)
- **Responsibility:** orchestrate the SELL execution path: validate the exit decision, fetch fresh order book, apply exit slippage guard, build SELL `OrderData`, and delegate signing.

Isolation rule:
- `ExitOrderRouter` is an orchestrator. It calls `PolymarketClient.fetch_order_book()` and `TransactionSigner.sign_order()` but owns none of their internal logic.
- `ExitOrderRouter` must not import LLM prompt construction, context-building, evaluation, or ingestion modules.
- `ExitOrderRouter` must not write to the database.
- `ExitOrderRouter` must not mutate position status.

### 3.2 Data Contracts (Required)

#### 3.2.1 `ExitOrderAction` enum (New)

Location: `src/schemas/execution.py`

```python
class ExitOrderAction(str, Enum):
    """Exit order routing outcomes."""
    SELL_ROUTED = "SELL_ROUTED"
    DRY_RUN = "DRY_RUN"
    FAILED = "FAILED"
    SKIP = "SKIP"
```

Semantics:
- `SELL_ROUTED` — order signed and ready for broadcast.
- `DRY_RUN` — full `OrderData` computed, no signing.
- `FAILED` — upstream failure (book unavailable, slippage breach, signer error).
- `SKIP` — entry gate rejection (`should_exit=False` or `exit_reason=ERROR`).

#### 3.2.2 `ExitOrderResult` model (New)

Location: `src/schemas/execution.py`

```python
class ExitOrderResult(BaseModel):
    """Typed outcome returned by ExitOrderRouter.route_exit()."""

    position_id: str
    condition_id: str
    action: ExitOrderAction
    reason: str | None = None
    order_payload: OrderData | None = None
    signed_order: SignedOrder | None = None
    exit_price: Decimal | None = None
    order_size_usdc: Decimal | None = None
    routed_at_utc: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @field_validator(
        "exit_price",
        "order_size_usdc",
        mode="before",
    )
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if value is None:
            return value
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = {"frozen": True}
```

Hard rules:
- `exit_price` and `order_size_usdc` are `Decimal`. Float is rejected at Pydantic boundary.
- Model is frozen (immutable after construction).
- `signed_order` is `None` in dry-run or failed paths.

### 3.3 New Exception Type (Required)

Location: `src/core/exceptions.py`

```python
class ExitRoutingError(PolyOracleError):
    """Raised when exit order routing fails."""

    def __init__(
        self,
        reason: str,
        position_id: str | None = None,
        condition_id: str | None = None,
        cause: Exception | None = None,
        **context: object,
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
        self.context = context
```

Follows the established pattern from `ExitEvaluationError` / `ExitMutationError`.

### 3.4 New AppConfig Field (Required)

Location: `src/core/config.py`

```python
# --- Exit Order Router (WI-20) ---
exit_min_bid_tolerance: Decimal = Field(
    default=Decimal("0.01"),
    description="Minimum acceptable best_bid for an exit SELL order. "
                "Orders below this threshold are rejected as degenerate exits.",
)
```

Hard constraints:
1. Field type is `Decimal`, not `float`.
2. Default `0.01` — reject exit orders where the market's best bid is below 1 cent (degenerate liquidity).
3. Placed in the `AppConfig` class after the existing WI-19 exit strategy fields for logical grouping.

## 4. Core Method Contracts (async, typed)

### 4.1 Constructor

```python
class ExitOrderRouter:
    def __init__(
        self,
        config: AppConfig,
        polymarket_client: PolymarketClient,
        transaction_signer: TransactionSigner | None,
    ) -> None:
```

Dependencies:
1. `config: AppConfig` — `dry_run` flag, `exit_min_bid_tolerance`, `wallet_address`.
2. `polymarket_client: PolymarketClient` — for `fetch_order_book()`.
3. `transaction_signer: TransactionSigner | None` — `None` when `dry_run=True` (signer not constructed).

No `BankrollSyncProvider` injection — exit sizing does not re-fetch bankroll. The position's recorded `order_size_usdc` is the exit size.

### 4.2 Async Route Entry Point

```python
async def route_exit(
    self,
    exit_result: ExitResult,
    position: PositionRecord,
) -> ExitOrderResult:
```

This is the sole public async method. Behavior:

#### Step 1: Entry Gate

Skip immediately if the exit decision is not actionable:

```python
if not exit_result.should_exit:
    return ExitOrderResult(
        position_id=position.id,
        condition_id=position.condition_id,
        action=ExitOrderAction.SKIP,
        reason="should_exit_is_false",
    )

if exit_result.exit_reason == ExitReason.ERROR:
    return ExitOrderResult(
        position_id=position.id,
        condition_id=position.condition_id,
        action=ExitOrderAction.SKIP,
        reason="exit_reason_is_error",
    )
```

Entry gate rejects non-exit decisions before any upstream call. This matches the WI-16 pattern where non-BUY actions are skipped immediately.

#### Step 2: Fresh Order Book Fetch

```python
snapshot = await self._polymarket_client.fetch_order_book(position.token_id)
if snapshot is None:
    return ExitOrderResult(
        position_id=position.id,
        condition_id=position.condition_id,
        action=ExitOrderAction.FAILED,
        reason="order_book_unavailable",
    )
```

Critical: uses `position.token_id` (the YES token ID), not `condition_id`. The CLOB order book is indexed by token ID.

#### Step 3: Extract Exit Price (best_bid)

```python
best_bid = Decimal(str(snapshot.best_bid))
```

For SELL orders, the relevant price is `best_bid` — the highest price a buyer is willing to pay. This is the exit price the position would receive.

#### Step 4: Exit Slippage Guard

```python
min_bid = Decimal(str(self._config.exit_min_bid_tolerance))
if best_bid < min_bid:
    return ExitOrderResult(
        position_id=position.id,
        condition_id=position.condition_id,
        action=ExitOrderAction.FAILED,
        reason="exit_bid_below_tolerance",
        exit_price=best_bid,
    )
```

This prevents selling at degenerate prices. Unlike the WI-16 BUY-side slippage guard (which compares `best_ask` to `midpoint + tolerance`), the exit slippage guard is an absolute floor on `best_bid`.

Rationale: on the exit path, the system is unwinding a position. If the best bid is near zero, the market is illiquid or the position is essentially worthless. Executing a SELL at < 1 cent would realize a near-total loss with no benefit.

#### Step 5: SELL-Side Order Sizing

Exit sizing is derived from position metadata — no Kelly recalculation:

```python
order_size_usdc = Decimal(str(position.order_size_usdc))
entry_price = Decimal(str(position.entry_price))
```

SELL-side order amounts:
```python
# maker_amount: tokens being sold (position's token quantity in micro-units)
# For a SELL, the maker provides tokens and receives USDC
# Token quantity at entry: order_size_usdc / entry_price
# Micro-unit conversion: * Decimal("1e6")
if entry_price > _ZERO:
    token_quantity = order_size_usdc / entry_price
    maker_amount = int(token_quantity * _USDC_SCALE)  # Decimal("1e6")
else:
    # Degenerate entry price — cannot compute token quantity
    return ExitOrderResult(
        position_id=position.id,
        condition_id=position.condition_id,
        action=ExitOrderAction.FAILED,
        reason="degenerate_entry_price",
        exit_price=best_bid,
        order_size_usdc=order_size_usdc,
    )

# taker_amount: USDC received (token_quantity * best_bid)
taker_amount = int((token_quantity * best_bid) * _USDC_SCALE)
```

All arithmetic is `Decimal`. No `float` intermediary.

#### Step 6: Build OrderData

```python
order_data = OrderData(
    salt=secrets.randbits(256),
    maker=self._config.wallet_address,
    signer=self._config.wallet_address,
    taker="0x0000000000000000000000000000000000000000",
    token_id=int(position.token_id),
    maker_amount=maker_amount,
    taker_amount=taker_amount,
    expiration=0,
    nonce=0,
    fee_rate_bps=0,
    side=OrderSide.SELL,         # ← SELL, never BUY
    signature_type=SIGNATURE_TYPE_EOA,
)
```

Critical invariant: `side=OrderSide.SELL`. A BUY-side exit order is a logic error.

#### Step 7: dry_run Gate

```python
if self._config.dry_run:
    logger.info(
        "exit_order_router.dry_run_order_built",
        dry_run=True,
        position_id=position.id,
        condition_id=position.condition_id,
        side=order_data.side.name,
        maker_amount=order_data.maker_amount,
        taker_amount=order_data.taker_amount,
        exit_price=str(best_bid),
        order_size_usdc=str(order_size_usdc),
    )
    return ExitOrderResult(
        position_id=position.id,
        condition_id=position.condition_id,
        action=ExitOrderAction.DRY_RUN,
        order_payload=order_data,
        exit_price=best_bid,
        order_size_usdc=order_size_usdc,
    )
```

When `dry_run=True`:
- Full `OrderData` is computed and returned for audit.
- `sign_order()` is never called.
- `signed_order` field is `None`.
- All sizing, slippage, and validation errors still raise — dry_run does not suppress computation failures.

#### Step 8: signer=None Guard

```python
if self._transaction_signer is None:
    logger.error(
        "exit_order_router.signer_unavailable",
        position_id=position.id,
        condition_id=position.condition_id,
    )
    return ExitOrderResult(
        position_id=position.id,
        condition_id=position.condition_id,
        action=ExitOrderAction.FAILED,
        reason="signer_unavailable",
        order_payload=order_data,
        exit_price=best_bid,
        order_size_usdc=order_size_usdc,
    )
```

This mirrors the WI-16 pattern: `signer=None` is tolerated in dry-run (short-circuited at step 7), but in live mode it returns `FAILED`.

#### Step 9: Sign Order

```python
try:
    signed_order = self._transaction_signer.sign_order(order_data)
except Exception as exc:
    logger.error(
        "exit_order_router.signing_error",
        position_id=position.id,
        condition_id=position.condition_id,
        error=str(exc),
    )
    return ExitOrderResult(
        position_id=position.id,
        condition_id=position.condition_id,
        action=ExitOrderAction.FAILED,
        reason="signing_error",
        order_payload=order_data,
        exit_price=best_bid,
        order_size_usdc=order_size_usdc,
    )
```

Signing exceptions are caught and returned as `FAILED`, not propagated. This matches the PRD requirement that exit-order routing failure does not terminate the exit scan loop.

#### Step 10: Return Success

```python
logger.info(
    "exit_order_router.sell_routed",
    position_id=position.id,
    condition_id=position.condition_id,
    side=order_data.side.name,
    maker_amount=order_data.maker_amount,
    taker_amount=order_data.taker_amount,
    exit_price=str(best_bid),
    order_size_usdc=str(order_size_usdc),
)
return ExitOrderResult(
    position_id=position.id,
    condition_id=position.condition_id,
    action=ExitOrderAction.SELL_ROUTED,
    order_payload=order_data,
    signed_order=signed_order,
    exit_price=best_bid,
    order_size_usdc=order_size_usdc,
)
```

## 5. Pipeline Integration Design

### 5.1 Orchestrator Wiring

`ExitOrderRouter` is constructed in `Orchestrator.__init__()` and invoked in `_exit_scan_loop()`.

#### Construction (in `__init__`):

```python
self.exit_order_router = ExitOrderRouter(
    config=self.config,
    polymarket_client=self.polymarket_client,
    transaction_signer=self.signer,  # None when dry_run=True
)
```

Placement: immediately after `self.exit_strategy_engine` construction (line ~102 in current orchestrator).

#### Invocation (in `_exit_scan_loop`):

After `ExitStrategyEngine.scan_open_positions()` returns `list[ExitResult]`, iterate over results where `should_exit=True`:

```python
async def _exit_scan_loop(self) -> None:
    """Periodic exit scan + exit order routing."""
    while True:
        await asyncio.sleep(self.config.exit_scan_interval_seconds)
        try:
            exit_results = await self.exit_strategy_engine.scan_open_positions()
        except Exception as exc:
            logger.error("exit_scan.scan_failed", error=str(exc))
            continue

        for exit_result in exit_results:
            if not exit_result.should_exit:
                continue

            # Reconstruct PositionRecord from ExitResult fields
            # ExitResult carries position_id and condition_id;
            # the full PositionRecord is available from the scan internals.
            # The orchestrator must pass both ExitResult and the
            # corresponding PositionRecord to route_exit().

            try:
                exit_order_result = await self.exit_order_router.route_exit(
                    exit_result=exit_result,
                    position=position,  # PositionRecord from scan
                )
            except Exception as exc:
                logger.error(
                    "exit_scan.routing_error",
                    position_id=exit_result.position_id,
                    error=str(exc),
                )
                continue  # fail-open: remaining exits still processed

            # WI-21 PnL settlement goes here (future)

            # Broadcast exit order if routed and not dry_run
            if (
                exit_order_result.action == ExitOrderAction.SELL_ROUTED
                and exit_order_result.signed_order is not None
                and not self.config.dry_run
                and self.broadcaster is not None
            ):
                try:
                    await self.broadcaster.broadcast(
                        signed_order=exit_order_result.signed_order,
                        decision_id=f"exit_{exit_result.position_id}",
                    )
                except Exception as exc:
                    logger.error(
                        "exit_scan.broadcast_error",
                        position_id=exit_result.position_id,
                        error=str(exc),
                    )
```

### 5.2 PositionRecord Availability

`ExitStrategyEngine.scan_open_positions()` currently returns `list[ExitResult]`. The `ExitResult` contains `position_id` and `condition_id` but not the full `PositionRecord` needed by `route_exit()` (which requires `token_id`, `order_size_usdc`, `entry_price`).

Two approaches (implementer's choice):

**Option A (preferred):** Modify the exit scan loop to build a lookup dict from the open positions query before evaluating. The `ExitStrategyEngine` internally queries `PositionRepository.get_open_positions()` and converts to `PositionRecord` — this data is available during the scan. The orchestrator can separately query open positions and build a `{position_id: PositionRecord}` map, or the `scan_open_positions()` return type can be enriched to `list[tuple[ExitResult, PositionRecord]]`.

**Option B:** `ExitOrderRouter.route_exit()` accepts `ExitResult` only, and internally reconstructs the required position fields from `ExitResult` fields. However, `ExitResult` lacks `token_id`, `order_size_usdc`, and `entry_price` — some of which it does carry (`entry_price`, `current_best_bid`) but not all.

Recommendation: extend `scan_open_positions()` to return paired results, or have the orchestrator perform a parallel open-position query before routing. The exact approach is an implementation detail; the contract is that `route_exit()` receives a `PositionRecord` with all required fields.

### 5.3 Failure Semantics (Fail-Open)

Unlike the WI-16 entry-path router (which uses fail-closed semantics — errors propagate and kill the routing attempt), the exit-order router uses **fail-open** semantics within the scan loop:

| Failure | Action in `ExitOrderResult` | Scan Loop Behavior |
|---------|-----------------------------|--------------------|
| `should_exit=False` | `SKIP` | Continue to next result |
| `exit_reason=ERROR` | `SKIP` | Continue to next result |
| Order book unavailable | `FAILED` | Continue to next result |
| `best_bid < exit_min_bid_tolerance` | `FAILED` | Continue to next result |
| Degenerate entry price | `FAILED` | Continue to next result |
| `signer=None` in live mode | `FAILED` | Continue to next result |
| Signing exception | `FAILED` | Continue to next result |
| Broadcast exception | Signed order exists | Log error, continue |

The position remains `CLOSED` regardless of routing outcome. The status transition already occurred in `ExitStrategyEngine`. A failed routing attempt means the exit order was not submitted, but the position is correctly marked closed.

### 5.4 dry_run Behavior

When `config.dry_run is True`:

1. `ExitOrderRouter` executes the full computation path: order book fetch, slippage check, sizing, and `OrderData` construction.
2. The order payload is logged via structlog at INFO level with all audit fields.
3. `sign_order()` is **never called**. The router short-circuits after payload construction.
4. `ExitOrderResult.action` is `DRY_RUN`; `signed_order` is `None`.
5. All validation and sizing errors still apply — dry_run does not suppress computation failures.

### 5.5 Router Isolation Rule

The `ExitOrderRouter` module (`src/agents/execution/exit_order_router.py`) must not:

1. Import or call LLM prompt construction, context-building, evaluation, or ingestion modules.
2. Import or embed `Web3` provider logic, private key handling, or raw EIP-712 encoding.
3. Write to the database (position mutation, PnL settlement, etc.).
4. Mutate position status.
5. Implement retry logic for any upstream call.
6. Perform Kelly re-sizing — exit size is position metadata, not recalculated.

Allowed imports:
- `src.agents.execution.polymarket_client` → `PolymarketClient`
- `src.agents.execution.signer` → `TransactionSigner`
- `src.core.config` → `AppConfig`
- `src.core.exceptions` → `ExitRoutingError`
- `src.schemas.execution` → `ExitResult`, `ExitReason`, `ExitOrderAction`, `ExitOrderResult`
- `src.schemas.position` → `PositionRecord`
- `src.schemas.web3` → `OrderData`, `OrderSide`, `SignedOrder`, `SIGNATURE_TYPE_EOA`
- `structlog`, `secrets`, `decimal.Decimal`, `datetime`

## 6. Required structlog Audit Events

| Event Key | Level | When | Required Fields |
|-----------|-------|------|-----------------|
| `exit_order_router.skipped` | `INFO` | Entry gate rejects (`should_exit=False` or `exit_reason=ERROR`) | `position_id`, `reason` |
| `exit_order_router.order_book_unavailable` | `WARNING` | `fetch_order_book()` returns `None` | `position_id`, `token_id` |
| `exit_order_router.exit_bid_below_tolerance` | `WARNING` | `best_bid < exit_min_bid_tolerance` | `position_id`, `best_bid`, `tolerance` |
| `exit_order_router.degenerate_entry_price` | `WARNING` | `entry_price <= 0` | `position_id`, `entry_price` |
| `exit_order_router.dry_run_order_built` | `INFO` | `dry_run=True`, full payload computed | `dry_run=True`, `position_id`, `condition_id`, `side`, `maker_amount`, `taker_amount`, `exit_price`, `order_size_usdc` |
| `exit_order_router.signer_unavailable` | `ERROR` | `signer=None` in live mode | `position_id`, `condition_id` |
| `exit_order_router.signing_error` | `ERROR` | `sign_order()` raises | `position_id`, `condition_id`, `error` |
| `exit_order_router.sell_routed` | `INFO` | Successfully signed SELL order | `position_id`, `condition_id`, `side`, `maker_amount`, `taker_amount`, `exit_price`, `order_size_usdc` |

## 7. Invariants Preserved

1. **Gatekeeper authority** — `LLMEvaluationResponse` remains the terminal pre-execution gate. `ExitOrderRouter` operates strictly downstream of `ExitStrategyEngine`, which operates downstream of the Gatekeeper. No bypass.
2. **Decimal financial integrity** — all exit pricing, sizing, and order amounts are `Decimal`. Float is rejected at Pydantic boundary. USDC micro-unit conversion uses `Decimal("1e6")`.
3. **Quarter-Kelly policy** — exit sizing does not recalculate Kelly. The position's recorded `order_size_usdc` (already Kelly-capped at entry) is used as the exit size.
4. **`dry_run=True` blocks signing** — full `OrderData` is computed and logged; `sign_order()` is never called; `signed_order` is `None`.
5. **Repository pattern** — `ExitOrderRouter` performs zero DB writes. Position status mutation is upstream (`ExitStrategyEngine`); PnL settlement is downstream (`PnLCalculator`).
6. **Async pipeline** — `ExitOrderRouter` runs within the existing `_exit_scan_loop()` async task. No new tasks or queues introduced.
7. **Entry-path routing** — `ExecutionRouter` internals are unmodified.
8. **SELL-only** — exit orders use `OrderSide.SELL` exclusively. A BUY-side exit order is a logic error.
9. **Module isolation** — zero imports from prompt, context, evaluation, or ingestion modules.
10. **No hardcoded `condition_id`** — token_id and condition_id are read from `PositionRecord`, which was populated from the market discovery pipeline at entry time.

## 8. Strict Acceptance Criteria (Maker Agent)

1. `ExitOrderRouter` exists in `src/agents/execution/exit_order_router.py` as the canonical exit-order routing class.
2. `route_exit(exit_result: ExitResult, position: PositionRecord) -> ExitOrderResult` is the sole public async entry point.
3. `ExitOrderAction` enum has values `SELL_ROUTED`, `DRY_RUN`, `FAILED`, `SKIP` in `src/schemas/execution.py`.
4. `ExitOrderResult` Pydantic model is frozen, Decimal-validated, with fields: `position_id`, `condition_id`, `action`, `reason`, `order_payload`, `signed_order`, `exit_price`, `order_size_usdc`, `routed_at_utc`.
5. Entry gate skips when `should_exit=False` (returns `SKIP`) or `exit_reason=ERROR` (returns `SKIP`).
6. Order book is fetched via `PolymarketClient.fetch_order_book(position.token_id)`; `None` result returns `FAILED(reason="order_book_unavailable")`.
7. Exit slippage guard rejects when `best_bid < exit_min_bid_tolerance` with `FAILED(reason="exit_bid_below_tolerance")`.
8. `OrderData` is constructed with `side=OrderSide.SELL`.
9. SELL sizing: `token_quantity = order_size_usdc / entry_price` (position metadata, Decimal-only).
10. `maker_amount = int(token_quantity * Decimal("1e6"))` — USDC micro-unit conversion.
11. `taker_amount = int(token_quantity * best_bid * Decimal("1e6"))`.
12. `dry_run=True` returns `ExitOrderResult(action=DRY_RUN)` with full `OrderData`; no `sign_order()` call.
13. `dry_run=False` delegates signing to `TransactionSigner.sign_order()` and returns `SELL_ROUTED`.
14. `signer=None` + `dry_run=False` returns `FAILED(reason="signer_unavailable")`.
15. Signing exception returns `FAILED(reason="signing_error")` — does not propagate.
16. Degenerate `entry_price <= 0` returns `FAILED(reason="degenerate_entry_price")`.
17. `ExitOrderRouter` is constructed in `Orchestrator.__init__()` and invoked within `_exit_scan_loop()`.
18. Exit-order routing failure does not terminate the exit scan loop.
19. `ExitOrderRouter` has zero imports from prompt, context, evaluation, or ingestion modules.
20. `AppConfig` gains `exit_min_bid_tolerance: Decimal` (default `Decimal("0.01")`).
21. `ExitRoutingError` exception exists in `src/core/exceptions.py` with `reason`, `position_id`, `condition_id`, `cause` fields.
22. All financial fields in `ExitOrderResult` reject `float` at Pydantic boundary.
23. Full regression remains green with coverage >= 80%.

## 9. Verification Checklist (Test Matrix)

### Unit Tests

1. `should_exit=False` returns `SKIP` without any upstream call.
2. `exit_reason=ERROR` returns `SKIP` without any upstream call.
3. `fetch_order_book()` returning `None` returns `FAILED(reason="order_book_unavailable")`.
4. `best_bid < exit_min_bid_tolerance` returns `FAILED(reason="exit_bid_below_tolerance")` with correct context.
5. `best_bid >= exit_min_bid_tolerance` proceeds to order construction.
6. `entry_price <= 0` returns `FAILED(reason="degenerate_entry_price")`.
7. SELL-side `OrderData` has `side=OrderSide.SELL` (never BUY).
8. `maker_amount` computed as `int(token_quantity * Decimal("1e6"))` for known inputs.
9. `taker_amount` computed as `int((token_quantity * best_bid) * Decimal("1e6"))` for known inputs.
10. `dry_run=True` builds `OrderData` but `sign_order()` is never called; result has `action=DRY_RUN` and `signed_order=None`.
11. `dry_run=False` calls `sign_order()` and returns `SELL_ROUTED` with populated `signed_order`.
12. `signer=None` + `dry_run=False` returns `FAILED(reason="signer_unavailable")`.
13. Signing exception returns `FAILED(reason="signing_error")`.
14. `float` input in `ExitOrderResult` financial fields is rejected at Pydantic boundary.
15. All financial fields in returned `ExitOrderResult` are `Decimal` type.
16. `ExitOrderResult` model is frozen — field assignment after construction raises error.

### Integration Tests

17. End-to-end `dry_run=True` — full pipeline from `ExitResult` through router, all sizing computed, no signing call.
18. End-to-end `dry_run=False` with mocked upstream — signed SELL order returned with correct amounts.
19. `ExitOrderRouter` module has no dependency on prompt/context/evaluation/ingestion modules (import boundary check).
20. Upstream failure cascade — each failure point (book, signer) returns `FAILED` with correct `ExitOrderAction`.
21. Multiple exit results — one fails, remaining succeed. Scan loop continues.
22. Orchestrator constructs `ExitOrderRouter` in `__init__()` with correct dependencies.

### Full Suite

23. `pytest --asyncio-mode=auto tests/`
24. `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m`
