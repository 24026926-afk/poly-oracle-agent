# WI-16 Business Logic — Execution Router (Route BUY Decisions to Signed Limit Orders)

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — `ExecutionRouter` is async; all upstream calls (`fetch_order_book`, `fetch_balance`, `sign_order`) are awaited with explicit timeout budgets.
- `.agents/rules/web3-specialist.md` — order signing delegates to `TransactionSigner`; router never handles private keys, nonces, or raw EIP-712 encoding.
- `.agents/rules/risk-auditor.md` — Kelly sizing, slippage guard, and order cap are all `Decimal`; no `float` intermediary in any financial path.
- `.agents/rules/security-auditor.md` — `dry_run=True` builds and logs the order payload but never calls `sign_order()` or submits to CLOB. No credentials in structured logs.
- `.agents/rules/test-engineer.md` — WI-16 routing behavior requires unit + integration coverage; full suite remains >= 80%.

## 1. Objective

Introduce `ExecutionRouter`, the orchestrator that connects a validated `LLMEvaluationResponse` with `action=BUY` to a signed limit order ready for Polymarket CLOB broadcast. The router calls — but does not own — order book fetching (WI-14 `PolymarketClient`), live bankroll reading (WI-18 `BankrollSyncProvider`), and order signing (WI-15 `TransactionSigner`). It owns Kelly sizing, slippage validation, and order-size capping only.

WI-16 is the first work item that wires the execution path end-to-end. It does not persist positions to the database (deferred to WI-17) and does not broadcast the signed order to the CLOB (broadcast is a downstream concern).

## 2. Scope Boundaries

### In Scope

1. New `ExecutionRouter` class: async orchestrator that converts a BUY decision into a sized, slippage-checked, signed limit order.
2. Entry gate: only `LLMEvaluationResponse` instances with `recommended_action=BUY` and `confidence_score >= AppConfig.min_confidence` activate routing.
3. Kelly sizing formula: `fraction = edge / odds` where `edge = midpoint_probability - threshold` and `odds = (1 - midpoint_probability) / midpoint_probability` — all `Decimal`, no `float`.
4. Slippage guard: reject order if `best_ask > midpoint_probability + max_slippage_tolerance` (configurable `Decimal` from `AppConfig`).
5. Order size capped at `min(kelly_fraction * bankroll, AppConfig.max_order_usdc)` — both `Decimal`.
6. `dry_run=True` bypass: build and log the full order payload but never call `sign_order()` or submit to CLOB.
7. Fail-closed semantics on any upstream failure (stale/missing order book, balance fetch error, signing error).
8. New `AppConfig` fields: `max_order_usdc` (`Decimal`) and `max_slippage_tolerance` (`Decimal`).

### Out of Scope

1. Order broadcast / CLOB submission — the router produces a signed order but does not transmit it.
2. Position tracking or database writes (deferred to WI-17).
3. SELL or HOLD routing — only BUY decisions are routed in this WI.
4. Modifications to `PolymarketClient`, `BankrollSyncProvider`, or `TransactionSigner` internals.
5. Modifications to `LLMEvaluationResponse` schema or Gatekeeper validation logic.
6. Gas estimation, nonce management, or transaction-level retry logic.
7. Multi-market or batched order execution.

## 3. Target Component Architecture + Data Contracts

### 3.1 Execution Router Component (New Class)

- **Module:** `src/agents/execution/router.py`
- **Class Name:** `ExecutionRouter` (exact)
- **Responsibility:** orchestrate the BUY execution path: validate the decision, fetch order book, fetch bankroll, compute Kelly size, apply slippage guard, cap order size, build order payload, and delegate signing.

Isolation rule:
- `ExecutionRouter` is an orchestrator. It calls `PolymarketClient.fetch_order_book()`, `BankrollSyncProvider.fetch_balance()`, and `TransactionSigner.sign_order()` but owns none of their internal logic.
- `ExecutionRouter` must not import LLM prompt construction, context-building, or ingestion modules.
- `ExecutionRouter` must not write to the database.

### 3.2 Orchestration Boundary (Required)

WI-16 introduces a strict orchestration-only boundary:

1. Allowed operations:
   - Read order book via `PolymarketClient.fetch_order_book(token_id)`
   - Read bankroll via `BankrollSyncProvider.fetch_balance(request)`
   - Delegate signing via `TransactionSigner.sign_order(order, neg_risk)`
   - Compute Kelly fraction, apply slippage guard, cap order size (all `Decimal`)
2. Forbidden operations:
   - Direct RPC calls, private key access, or raw EIP-712 encoding
   - Token approvals, transfers, or any on-chain state mutation
   - Database writes (position tracking is WI-17)
   - Prompt construction or LLM invocation
   - Modification of upstream component state
3. Delegation lifecycle:
   - Router receives injected instances of `PolymarketClient`, `BankrollSyncProvider`, `TransactionSigner`, and `AppConfig` at construction
   - Each upstream call is independently failable; any failure aborts the entire routing attempt
   - Router never retries failed upstream calls — fail-closed, one-shot

### 3.3 Data Contracts (Required)

Routing boundary must use typed contracts (Pydantic at boundary is required). Minimum contracts:

1. `RouteOrderRequest`
   - `evaluation`: `LLMEvaluationResponse` (the validated BUY decision)
   - `token_id`: `str` (Polymarket condition/token ID for the order book)
   - `neg_risk`: `bool` (neg-risk flag for EIP-712 domain; default `False`)
   - `nonce`: `int` (order nonce; default `0`)
   - `fee_rate_bps`: `int` (fee rate in basis points; default `0`)

2. `RouteOrderResult`
   - `signed_order`: `SignedOrder | None` (populated when `dry_run=False` and signing succeeds; `None` when `dry_run=True`)
   - `order_payload`: `OrderData` (the unsigned order payload — always populated, including in dry_run)
   - `kelly_fraction`: `Decimal` (raw Kelly fraction before cap)
   - `order_size_usdc`: `Decimal` (final USDC amount after cap)
   - `midpoint_probability`: `Decimal` (from order book snapshot)
   - `best_ask`: `Decimal` (from order book snapshot)
   - `bankroll_usdc`: `Decimal` (live bankroll at routing time)
   - `is_dry_run`: `bool`
   - `routed_at_utc`: `datetime`

Hard rules:
- All financial fields (`kelly_fraction`, `order_size_usdc`, `midpoint_probability`, `best_ask`, `bankroll_usdc`) are `Decimal`. No `float` intermediary.
- `float` inputs in financial fields are rejected at schema boundary.

## 4. Core Method Contracts (async, typed)

### 4.1 Async Route Entry Point

Required public method:

- `route_order(request: RouteOrderRequest) -> RouteOrderResult` (async)

Behavior requirements:

1. **Entry gate check:** verify `request.evaluation.recommended_action == RecommendedAction.BUY` and `request.evaluation.confidence_score >= config.min_confidence`. If either fails, raise `RoutingRejectedError` with reason.
2. **Fetch order book:** call `polymarket_client.fetch_order_book(request.token_id)`. If result is `None` (stale/failed), raise `RoutingAbortedError("Order book unavailable")`.
3. **Slippage guard:** compute `slippage_limit = snapshot.midpoint_probability + config.max_slippage_tolerance`. If `snapshot.best_ask > slippage_limit`, raise `SlippageExceededError` with `best_ask`, `midpoint`, and `tolerance`.
4. **Fetch bankroll:** call `bankroll_provider.fetch_balance(...)`. If `BalanceFetchError` is raised, let it propagate — do not catch.
5. **Kelly sizing:** compute `edge = snapshot.midpoint_probability - Decimal(str(config.min_ev_threshold))` and `odds = (Decimal("1") - snapshot.midpoint_probability) / snapshot.midpoint_probability`. Then `kelly_raw = edge / odds`. All `Decimal`, no `float`.
6. **Kelly fraction scaling:** apply `kelly_scaled = kelly_raw * Decimal(str(config.kelly_fraction))` (Quarter-Kelly).
7. **Order size capping:** compute `order_size = min(kelly_scaled * bankroll_usdc, Decimal(str(config.max_order_usdc)))`. If `order_size <= Decimal("0")`, raise `RoutingRejectedError("Non-positive order size")`.
8. **Build order payload:** construct `OrderData` with computed `maker_amount` (USDC micro-units: `int(order_size * Decimal("1e6"))`), `taker_amount` (tokens at midpoint), `token_id`, `side=OrderSide.BUY`, and remaining fields from request.
9. **dry_run gate:** if `config.dry_run is True`, log the full order payload via structured logger, populate `RouteOrderResult` with `signed_order=None` and `is_dry_run=True`, and return immediately. Do NOT call `sign_order()`.
10. **Sign order:** call `transaction_signer.sign_order(order_payload, neg_risk=request.neg_risk)`. If `DryRunActiveError` or any signer exception is raised, let it propagate.
11. **Return result:** populate and return `RouteOrderResult` with all fields.

### 4.2 Kelly Sizing Formula (Required)

The Kelly criterion computation is the core sizing logic owned by `ExecutionRouter`:

```
edge = midpoint_probability - threshold
odds = (1 - midpoint_probability) / midpoint_probability
kelly_raw = edge / odds
kelly_scaled = kelly_raw * kelly_fraction       # Quarter-Kelly (0.25)
order_size = min(kelly_scaled * bankroll, max_order_usdc)
```

Hard constraints:
1. Every variable in the formula is `Decimal`. No `float()` conversion at any step.
2. `threshold` is `Decimal(str(config.min_ev_threshold))`.
3. `kelly_fraction` multiplier is `Decimal(str(config.kelly_fraction))`.
4. If `midpoint_probability <= threshold` (no edge), `kelly_raw <= 0` — raise `RoutingRejectedError("No positive edge")`.
5. If `midpoint_probability` is `0` or `1`, the formula is degenerate — raise `RoutingRejectedError("Degenerate midpoint")`.
6. Division by zero guard: `odds` denominator is `midpoint_probability`; checked non-zero in step 5.

### 4.3 New AppConfig Fields (Required)

Two new fields must be added to `AppConfig` in `src/core/config.py`:

1. `max_order_usdc: Decimal = Field(default=Decimal("50"), description="Hard cap on any single order in USDC")`
2. `max_slippage_tolerance: Decimal = Field(default=Decimal("0.02"), description="Max allowed deviation of best_ask above midpoint (2%)")`

Hard constraints:
1. Both fields are `Decimal`, not `float`.
2. `max_order_usdc` defaults to `50` USDC (conservative for initial deployment).
3. `max_slippage_tolerance` defaults to `0.02` (2 percentage points above midpoint).

### 4.4 Error Types (Required)

New typed exceptions in `src/core/exceptions.py`:

1. `RoutingRejectedError(PolyOracleError)` — decision did not pass entry gate or sizing produced non-positive result.
2. `RoutingAbortedError(PolyOracleError)` — upstream dependency failed (stale book, balance error, signing error).
3. `SlippageExceededError(RoutingAbortedError)` — best ask exceeds slippage tolerance.

All exceptions must include structured context (token_id, reason, relevant values) for logging.

## 5. Pipeline Integration Design

WI-16 integration point is between Gatekeeper validation (step 3) and order broadcast (future WI):

```
evaluation_cycle:
  1. fetch market data (WI-14)
  2. build prompt + evaluate (WI-11/12/13)
  3. Gatekeeper validation (LLMEvaluationResponse)
  4. fetch live USDC balance  (WI-18)
  5. compute_position_size() using live balance
  6. validate_trade() using live balance
  7. ExecutionRouter.route_order()  ← WI-16 (here)
     a. re-fetch order book (fresh snapshot for slippage check)
     b. re-fetch bankroll (fresh balance for Kelly sizing)
     c. Kelly sizing + slippage guard + order cap
     d. build OrderData
     e. sign order (WI-15) — skipped if dry_run
  8. broadcast signed order (future WI)
```

Note: The router independently fetches the order book and bankroll to guarantee freshness at signing time. These may differ from earlier pipeline reads if market conditions changed during evaluation.

### 5.1 Constructor Dependencies (Injected)

`ExecutionRouter.__init__` receives:

1. `config: AppConfig` — risk parameters, dry_run flag, new max_order_usdc and max_slippage_tolerance.
2. `polymarket_client: PolymarketClient` — for `fetch_order_book()`.
3. `bankroll_provider: BankrollSyncProvider` — for `fetch_balance()`.
4. `transaction_signer: TransactionSigner` — for `sign_order()`.

No default construction. All four dependencies are required and injected by the `Orchestrator`.

### 5.2 Failure Semantics (Fail Closed)

On any upstream failure or validation rejection:

1. Emit structured error log (include `token_id`, failure source, relevant values; never log private keys or RPC credentials).
2. Raise typed exception (`RoutingRejectedError`, `RoutingAbortedError`, `SlippageExceededError`) — no fallback, no retry, no partial order.
3. Calling code (`Orchestrator`) catches the exception and logs the aborted routing attempt. No order is enqueued.

Specific failure modes:

| Failure | Exception | Behavior |
|---------|-----------|----------|
| `action != BUY` or low confidence | `RoutingRejectedError` | Abort before any upstream call |
| `fetch_order_book()` returns `None` | `RoutingAbortedError` | Abort — stale/missing book |
| `best_ask > midpoint + tolerance` | `SlippageExceededError` | Abort — adverse slippage |
| `fetch_balance()` raises `BalanceFetchError` | `BalanceFetchError` (propagated) | Abort — balance unavailable |
| Kelly fraction <= 0 or degenerate midpoint | `RoutingRejectedError` | Abort — no positive edge |
| `order_size <= 0` after cap | `RoutingRejectedError` | Abort — non-viable size |
| `sign_order()` raises (any) | Exception propagated | Abort — signing failed |

### 5.3 dry_run Behavior

When `config.dry_run is True`:

1. The router executes the full computation path: order book fetch, bankroll fetch, Kelly sizing, slippage check, order cap, and `OrderData` construction.
2. The order payload is logged via structured logger at INFO level (token_id, side, maker_amount, taker_amount, order_size_usdc, kelly_fraction).
3. `sign_order()` is **never called**. The router short-circuits after payload construction.
4. `RouteOrderResult.signed_order` is `None`; `RouteOrderResult.is_dry_run` is `True`.
5. All validation and sizing errors still raise — dry_run does not suppress computation failures.

### 5.4 Router Isolation Rule

The `ExecutionRouter` module must not:

1. Import or call LLM prompt construction, context-building, or ingestion modules.
2. Import or embed `Web3` provider logic, private key handling, or raw EIP-712 encoding.
3. Write to the database (position tracking is WI-17).
4. Catch and swallow upstream exceptions (fail-closed; exceptions propagate).
5. Implement retry logic for any upstream call.

Router input is a typed `RouteOrderRequest` containing the validated decision and token metadata. Router output is a typed `RouteOrderResult` containing the signed (or unsigned in dry_run) order and sizing metadata.

## 6. Invariants Preserved

1. `LLMEvaluationResponse` remains the terminal pre-execution Gatekeeper — the router only activates on BUY with confidence above threshold.
2. Kelly formula parameters remain unchanged (`kelly_fraction=0.25`, `max_exposure_pct=0.03` cap still intact in upstream `BankrollPortfolioTracker`).
3. `Decimal` financial-integrity rules remain mandatory for all sizing, pricing, and order amount paths.
4. Async 4-layer queue topology remains unchanged — `ExecutionRouter` lives within Layer 4 (Execution).
5. `dry_run=True` continues to block all Layer 4 side effects; order payload is built and logged but never signed or submitted.
6. Repository pattern and DB boundaries remain unchanged — no DB writes in WI-16.
7. `PolymarketClient`, `BankrollSyncProvider`, and `TransactionSigner` internals are unmodified — zero coupling beyond their public contracts.
8. `LLMEvaluationResponse` schema is unmodified.
9. No PRD-v5.0 section changes; WI-16 is a Phase 5 execution-path integration item.

## 7. Strict Acceptance Criteria (Maker Agent)

1. `ExecutionRouter` is the canonical routing class in `src/agents/execution/router.py`.
2. `route_order(request: RouteOrderRequest) -> RouteOrderResult` is the sole public async entry point.
3. Entry gate rejects non-BUY actions and confidence below `AppConfig.min_confidence` with `RoutingRejectedError`.
4. Order book is fetched via `PolymarketClient.fetch_order_book()`; `None` result raises `RoutingAbortedError`.
5. Slippage guard rejects when `best_ask > midpoint_probability + max_slippage_tolerance` with `SlippageExceededError`.
6. Bankroll is fetched via `BankrollSyncProvider.fetch_balance()`; `BalanceFetchError` propagates uncaught.
7. Kelly sizing uses `edge = midpoint - threshold`, `odds = (1 - midpoint) / midpoint`, `fraction = edge / odds` — all `Decimal`.
8. Kelly fraction is scaled by `config.kelly_fraction` (Quarter-Kelly).
9. Order size is `min(kelly_scaled * bankroll, config.max_order_usdc)` — both `Decimal`.
10. Non-positive order size or non-positive edge raises `RoutingRejectedError`.
11. `dry_run=True` builds and logs order payload but never calls `sign_order()`.
12. `dry_run=False` delegates to `TransactionSigner.sign_order()` and returns `SignedOrder` in result.
13. `AppConfig` gains `max_order_usdc: Decimal` (default `50`) and `max_slippage_tolerance: Decimal` (default `0.02`).
14. New exceptions `RoutingRejectedError`, `RoutingAbortedError`, `SlippageExceededError` in `src/core/exceptions.py`.
15. `ExecutionRouter` has zero imports from prompt, context, ingestion, or database modules.
16. No database writes occur anywhere in the routing path.
17. `float` inputs in financial fields of routing contracts are rejected at Pydantic schema boundary.
18. Full regression remains green (`pytest --asyncio-mode=auto tests/`) with coverage >= 80%.

## 8. Verification Checklist

1. Unit test: non-BUY action (`HOLD`, `SELL`) raises `RoutingRejectedError` without any upstream call.
2. Unit test: BUY with confidence below `min_confidence` raises `RoutingRejectedError` without any upstream call.
3. Unit test: `fetch_order_book()` returning `None` raises `RoutingAbortedError`.
4. Unit test: `best_ask` exceeding slippage tolerance raises `SlippageExceededError` with correct context.
5. Unit test: `best_ask` within slippage tolerance proceeds to Kelly sizing.
6. Unit test: Kelly formula produces correct `Decimal` fraction for known inputs (e.g., midpoint=0.65, threshold=0.02 → expected fraction).
7. Unit test: Kelly fraction <= 0 (no edge) raises `RoutingRejectedError`.
8. Unit test: degenerate midpoint (0 or 1) raises `RoutingRejectedError`.
9. Unit test: order size correctly capped at `min(kelly * bankroll, max_order_usdc)`.
10. Unit test: `dry_run=True` builds `OrderData` but `sign_order()` is never called; result has `signed_order=None` and `is_dry_run=True`.
11. Unit test: `dry_run=False` calls `sign_order()` and returns populated `SignedOrder` in result.
12. Unit test: `BalanceFetchError` from `fetch_balance()` propagates without being caught.
13. Unit test: signer exception propagates without being caught.
14. Unit test: `float` input in `RouteOrderRequest` financial fields is rejected at schema boundary.
15. Unit test: all financial fields in `RouteOrderResult` are `Decimal` type.
16. Unit test: `OrderData.maker_amount` computed as `int(order_size * Decimal("1e6"))` matches expected micro-units.
17. Integration test: end-to-end `dry_run=True` — full pipeline from `LLMEvaluationResponse` through router, all sizing computed, no signing call.
18. Integration test: end-to-end `dry_run=False` with mocked upstream — signed order returned with correct amounts.
19. Integration test: `ExecutionRouter` module has no dependency on prompt/context/ingestion/database modules (import boundary check).
20. Integration test: upstream failure cascade — each failure point (book, balance, signer) correctly aborts with typed exception.
21. Full suite:
    - `pytest --asyncio-mode=auto tests/`
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m`
