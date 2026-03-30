# P20-WI-20 — Exit Order Router Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-4.1 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi20-exit-order-router` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/web3-specialist.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/test-engineer.md`
- `.agents/rules/security-auditor.md`

## Role

You are implementing WI-20 for Phase 7: the `ExitOrderRouter`, the downstream component that consumes an `ExitResult(should_exit=True)` produced by `ExitStrategyEngine` (WI-19) and converts it into a signed SELL-side limit order payload for Polymarket CLOB submission. This mirrors the WI-16 `ExecutionRouter` pattern, adapted exclusively for the exit path.

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
- Database writes — the router produces a signed order; no DB mutation
- Kelly re-sizing — exit size is position metadata, not recalculated

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi20.md`
4. `docs/PRD-v7.0.md` (Phase 7 / WI-20 section)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/agents/execution/exit_strategy_engine.py` (context boundary: frozen, no modifications)
9. `src/agents/execution/execution_router.py` (context boundary: frozen, WI-16 BUY-side pattern reference)
10. `src/agents/execution/polymarket_client.py` (dependency: `fetch_order_book()`)
11. `src/agents/execution/signer.py` (dependency: `sign_order()`)
12. `src/agents/execution/position_tracker.py` (context boundary: frozen, no modifications)
13. `src/orchestrator.py` — **primary integration target; `_exit_scan_loop()` currently calls `scan_open_positions()` only**
14. `src/core/config.py`
15. `src/core/exceptions.py`
16. `src/schemas/execution.py`
17. `src/schemas/position.py`
18. `src/schemas/web3.py` (contains `OrderData`, `OrderSide`, `SignedOrder`, `SIGNATURE_TYPE_EOA`)
19. Existing tests:
    - `tests/unit/test_exit_strategy_engine.py`
    - `tests/unit/test_exit_scan_loop.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-20 test files first:
   - `tests/unit/test_exit_order_router.py`
   - `tests/integration/test_exit_order_router_integration.py`
2. Write failing tests for all required behaviors:
   - `ExitOrderRouter` exists in `src/agents/execution/exit_order_router.py` with constructor accepting `config: AppConfig`, `polymarket_client: PolymarketClient`, `transaction_signer: TransactionSigner | None`.
   - `route_exit(exit_result: ExitResult, position: PositionRecord) -> ExitOrderResult` is the sole public async method.
   - `should_exit=False` returns `ExitOrderResult(action=SKIP, reason="should_exit_is_false")` without any upstream call.
   - `exit_reason=ExitReason.ERROR` returns `ExitOrderResult(action=SKIP, reason="exit_reason_is_error")` without any upstream call.
   - `fetch_order_book()` returning `None` returns `ExitOrderResult(action=FAILED, reason="order_book_unavailable")`.
   - `best_bid < exit_min_bid_tolerance` returns `ExitOrderResult(action=FAILED, reason="exit_bid_below_tolerance")`.
   - `entry_price <= 0` returns `ExitOrderResult(action=FAILED, reason="degenerate_entry_price")`.
   - `OrderData` is constructed with `side=OrderSide.SELL` — never `OrderSide.BUY`.
   - `maker_amount = int(token_quantity * Decimal("1e6"))` for known inputs.
   - `taker_amount = int((token_quantity * best_bid) * Decimal("1e6"))` for known inputs.
   - `dry_run=True` builds full `OrderData` but `sign_order()` is never called; result has `action=DRY_RUN` and `signed_order=None`.
   - `dry_run=False` calls `sign_order()` and returns `SELL_ROUTED` with populated `signed_order`.
   - `signer=None` + `dry_run=False` returns `ExitOrderResult(action=FAILED, reason="signer_unavailable")`.
   - Signing exception returns `ExitOrderResult(action=FAILED, reason="signing_error")` — does not propagate.
   - `float` input in `ExitOrderResult` financial fields (`exit_price`, `order_size_usdc`) is rejected at Pydantic boundary.
   - `ExitOrderResult` model is frozen — field assignment after construction raises error.
3. Run RED tests:
   - `pytest tests/unit/test_exit_order_router.py -v`
   - `pytest tests/integration/test_exit_order_router_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add `ExitRoutingError` to Exception Taxonomy

Target:
- `src/core/exceptions.py`

Requirements:
1. Add `ExitRoutingError` exception class, subclassing `PolyOracleError`.
2. Constructor accepts `reason: str`, `position_id: str | None = None`, `condition_id: str | None = None`, `cause: Exception | None = None`, and `**context: object`.
3. Structured message carries `position_id` and `condition_id` without exposing RPC credentials or private keys.
4. Follows the established pattern from `ExitEvaluationError` / `ExitMutationError`.

### Step 2 — Add `ExitOrderAction` Enum + `ExitOrderResult` Model

Target:
- `src/schemas/execution.py`

Requirements:
1. Add `ExitOrderAction(str, Enum)` with values: `SELL_ROUTED`, `DRY_RUN`, `FAILED`, `SKIP`.
2. Add `ExitOrderResult(BaseModel)` with fields:
   - `position_id: str`
   - `condition_id: str`
   - `action: ExitOrderAction`
   - `reason: str | None = None`
   - `order_payload: OrderData | None = None`
   - `signed_order: SignedOrder | None = None`
   - `exit_price: Decimal | None = None`
   - `order_size_usdc: Decimal | None = None`
   - `routed_at_utc: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))`
3. Add `_reject_float_financials` field validator on `exit_price` and `order_size_usdc`:
   - `float` input raises `ValueError("Float financial values are forbidden; use Decimal")`.
   - `Decimal` passes through; other types converted via `Decimal(str(value))`.
4. `model_config = {"frozen": True}` — immutable after construction.

### Step 3 — Add `AppConfig.exit_min_bid_tolerance` Field

Target:
- `src/core/config.py`

Requirements:
1. Add `exit_min_bid_tolerance: Decimal = Field(default=Decimal("0.01"), description="Minimum acceptable best_bid for an exit SELL order. Orders below this threshold are rejected as degenerate exits.")`.
2. Field type is `Decimal`, not `float`.
3. Placed after the existing WI-19/WI-22 exit strategy fields for logical grouping.

### Step 4 — Implement `ExitOrderRouter` Class

Target:
- `src/agents/execution/exit_order_router.py` (new)

Requirements:
1. New class `ExitOrderRouter` with constructor:
   ```python
   def __init__(
       self,
       config: AppConfig,
       polymarket_client: PolymarketClient,
       transaction_signer: TransactionSigner | None,
   ) -> None:
   ```
2. Single public method: `async def route_exit(self, exit_result: ExitResult, position: PositionRecord) -> ExitOrderResult`.
3. Implement all 10 steps of `route_exit()` logic in exact order:

   **Step 4a — Entry Gate:**
   - `should_exit=False` → return `SKIP(reason="should_exit_is_false")`.
   - `exit_reason=ExitReason.ERROR` → return `SKIP(reason="exit_reason_is_error")`.

   **Step 4b — Fresh Order Book Fetch:**
   - `snapshot = await self._polymarket_client.fetch_order_book(position.token_id)`.
   - `None` → return `FAILED(reason="order_book_unavailable")`.
   - Critical: uses `position.token_id`, NOT `condition_id`.

   **Step 4c — Extract Exit Price:**
   - `best_bid = Decimal(str(snapshot.best_bid))`.

   **Step 4d — Exit Slippage Guard:**
   - `best_bid < exit_min_bid_tolerance` → return `FAILED(reason="exit_bid_below_tolerance")`.

   **Step 4e — SELL-Side Order Sizing:**
   - `order_size_usdc = Decimal(str(position.order_size_usdc))`.
   - `entry_price = Decimal(str(position.entry_price))`.
   - `entry_price <= 0` → return `FAILED(reason="degenerate_entry_price")`.
   - `token_quantity = order_size_usdc / entry_price`.
   - `maker_amount = int(token_quantity * Decimal("1e6"))`.
   - `taker_amount = int((token_quantity * best_bid) * Decimal("1e6"))`.
   - All arithmetic is `Decimal`. No `float` intermediary.

   **Step 4f — Build OrderData:**
   - `side=OrderSide.SELL` — never BUY.
   - `salt=secrets.randbits(256)`.
   - `maker=self._config.wallet_address`.
   - `signer=self._config.wallet_address`.
   - `taker="0x0000000000000000000000000000000000000000"`.
   - `token_id=int(position.token_id)`.
   - `expiration=0`, `nonce=0`, `fee_rate_bps=0`.
   - `signature_type=SIGNATURE_TYPE_EOA`.

   **Step 4g — dry_run Gate:**
   - If `self._config.dry_run`: log `exit_order_router.dry_run_order_built` with full audit fields, return `ExitOrderResult(action=DRY_RUN, order_payload=order_data, signed_order=None, ...)`.
   - `sign_order()` is NEVER called when `dry_run=True`.

   **Step 4h — signer=None Guard:**
   - If `self._transaction_signer is None`: log `exit_order_router.signer_unavailable`, return `FAILED(reason="signer_unavailable")`.

   **Step 4i — Sign Order:**
   - `signed_order = self._transaction_signer.sign_order(order_data)`.
   - Signing exception → catch, log `exit_order_router.signing_error`, return `FAILED(reason="signing_error")`. Do NOT propagate.

   **Step 4j — Return Success:**
   - Log `exit_order_router.sell_routed` with full audit fields.
   - Return `ExitOrderResult(action=SELL_ROUTED, order_payload=order_data, signed_order=signed_order, ...)`.

4. Structured logging via `structlog` only — no `print()`.
5. Module isolation — zero imports from:
   - `src/agents/evaluation/*`
   - `src/agents/context/*`
   - `src/agents/ingestion/*`
   - `src/db/*`
6. Allowed imports:
   - `src.agents.execution.polymarket_client` → `PolymarketClient`
   - `src.agents.execution.signer` → `TransactionSigner`
   - `src.core.config` → `AppConfig`
   - `src.core.exceptions` → `ExitRoutingError`
   - `src.schemas.execution` → `ExitResult`, `ExitReason`, `ExitOrderAction`, `ExitOrderResult`
   - `src.schemas.position` → `PositionRecord`
   - `src.schemas.web3` → `OrderData`, `OrderSide`, `SignedOrder`, `SIGNATURE_TYPE_EOA`
   - `structlog`, `secrets`, `decimal.Decimal`, `datetime`

### Step 5 — Orchestrator Wiring

Target:
- `src/orchestrator.py`

Requirements:

**5a — Constructor (`__init__`):**
1. Import `ExitOrderRouter` from `src.agents.execution.exit_order_router`.
2. Construct `self.exit_order_router = ExitOrderRouter(config=self.config, polymarket_client=self.polymarket_client, transaction_signer=self.signer)`.
3. Placement: immediately after `self.exit_strategy_engine` construction.

**5b — `_exit_scan_loop()` Integration:**
1. After `scan_open_positions()` returns `list[ExitResult]`, iterate over results where `should_exit=True`.
2. For each actionable exit result, resolve the corresponding `PositionRecord` and call `await self.exit_order_router.route_exit(exit_result, position)`.
3. Wrap each `route_exit()` call in `try/except Exception` — log `exit_scan.routing_error` and `continue`. A single routing failure must not block remaining exits.
4. If `exit_order_result.action == ExitOrderAction.SELL_ROUTED` and `signed_order is not None` and `not self.config.dry_run` and `self.broadcaster is not None`: broadcast via `self.broadcaster.broadcast(...)`. Wrap broadcast in `try/except Exception` with `exit_scan.broadcast_error` log.
5. Preserve the existing sleep-first pattern, `scan_open_positions()` error handling, and `exit_scan_loop.completed` / `exit_scan_loop.error` structlog events.

### Step 6 — GREEN Validation

Run:
```bash
pytest tests/unit/test_exit_order_router.py -v
pytest tests/integration/test_exit_order_router_integration.py -v
pytest tests/unit/test_exit_scan_loop.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. **SELL-only.** Exit orders use `OrderSide.SELL` — never `OrderSide.BUY`. A BUY-side exit order is a logic error.
2. **No Kelly re-sizing.** Exit sizing uses the position's recorded `order_size_usdc` and `entry_price`. No Kelly recalculation occurs on the exit path.
3. **Decimal financial integrity.** All exit pricing, sizing, and order amounts are `Decimal`. Float is rejected at Pydantic boundary. USDC micro-unit conversion uses `Decimal("1e6")`.
4. **`dry_run=True` blocks signing.** Full `OrderData` is computed and logged; `sign_order()` is never called; `signed_order` is `None`.
5. **`signer=None` fail-closed in live mode.** Tolerated when `dry_run=True` (short-circuited before signer check); returns `FAILED(reason="signer_unavailable")` when `dry_run=False`.
6. **Zero DB writes.** `ExitOrderRouter` produces a signed order; it does not mutate position status, write PnL, or touch any repository.
7. **Module isolation.** Zero imports from prompt, context, evaluation, or ingestion modules. Zero imports from `src/db/`.
8. **Gatekeeper authority preserved.** `LLMEvaluationResponse` remains the terminal pre-execution gate. `ExitOrderRouter` operates strictly downstream of `ExitStrategyEngine`.
9. **Frozen upstream components.** `ExitStrategyEngine`, `PositionTracker`, `PositionRepository`, and `ExecutionRouter` internals are unmodified.
10. **Fail-open in scan loop.** A failed `route_exit()` call is caught, logged, and the scan continues to remaining exits. The loop never terminates on a routing failure.
11. **No queue topology changes.** `market_queue -> prompt_queue -> execution_queue`. No new queue.
12. **Async pipeline preserved.** `ExitOrderRouter` runs within `_exit_scan_loop()`. No new tasks or queues.
13. **No hardcoded `condition_id`.** `token_id` and `condition_id` read from `PositionRecord`.
14. **`ExitOrderResult` is frozen.** Immutable after construction.

---

## Required Test Matrix

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

---

## Deliverables

1. RED-phase failing test summary.
2. GREEN implementation summary by file.
3. Passing targeted test summary + full regression summary.
4. Final staged `git diff` for MAAP checker review.

---

## MAAP Reflection Pass (Checker Prompt for Gemini 2.5 Pro)

Use the following exact prompt with the final staged `git diff`:

```text
You are the MAAP Checker for WI-20 (Exit Order Router) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi20.md
2) docs/PRD-v7.0.md (WI-20 section)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Decimal violations (any float usage in exit_price, order_size_usdc, maker_amount, taker_amount, or any money-path logic)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation upstream)
- SELL-only violation (any BUY-side exit order — OrderSide.BUY in exit routing path)
- dry_run violations (sign_order() called when dry_run=True)
- signer=None not fail-closed in live mode (signer=None + dry_run=False must return FAILED, not proceed)
- DB write isolation (ExitOrderRouter writing to any repository or database table)
- Kelly re-sizing (exit router recalculating Kelly fraction or bankroll-based sizing instead of using position metadata)
- Isolation violations (any import from src/agents/context/, src/agents/evaluation/, src/agents/ingestion/, or src/db/ in exit_order_router.py)
- Regression (any modification to ExitStrategyEngine, PositionTracker, PositionRepository, ExecutionRouter, or coverage < 80%)

Additional required checks:
- ExitOrderRouter class exists in src/agents/execution/exit_order_router.py
- route_exit(exit_result, position) is async and returns ExitOrderResult
- ExitOrderAction enum has values SELL_ROUTED, DRY_RUN, FAILED, SKIP in src/schemas/execution.py
- ExitOrderResult Pydantic model is frozen, Decimal-validated, with _reject_float_financials validator
- Entry gate skips should_exit=False and exit_reason=ERROR before any upstream call
- Order book fetched via PolymarketClient.fetch_order_book(position.token_id) — NOT condition_id
- Exit slippage guard: best_bid < exit_min_bid_tolerance returns FAILED
- OrderData constructed with side=OrderSide.SELL
- SELL sizing: token_quantity = order_size_usdc / entry_price (position metadata, Decimal-only)
- maker_amount = int(token_quantity * Decimal("1e6"))
- taker_amount = int((token_quantity * best_bid) * Decimal("1e6"))
- dry_run=True returns DRY_RUN with full OrderData, sign_order() never called
- signer=None + dry_run=False returns FAILED(reason="signer_unavailable")
- Signing exception returns FAILED(reason="signing_error"), does not propagate
- Degenerate entry_price <= 0 returns FAILED(reason="degenerate_entry_price")
- ExitRoutingError exists in src/core/exceptions.py with reason, position_id, condition_id, cause fields
- AppConfig.exit_min_bid_tolerance exists as Decimal with default Decimal("0.01")
- ExitOrderRouter constructed in Orchestrator.__init__() and invoked in _exit_scan_loop()
- Routing failure in _exit_scan_loop() is caught and does not terminate the loop
- Zero new imports from prompt/context/evaluation/ingestion/db modules in exit_order_router.py
- ExitStrategyEngine, PositionTracker, PositionRepository, ExecutionRouter are byte-identical before and after WI-20

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-20/invariants
4) Explicit statement on each MAAP critical category:
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - SELL-only violation: CLEARED/FLAGGED
   - dry_run violations: CLEARED/FLAGGED
   - signer=None not fail-closed: CLEARED/FLAGGED
   - DB write isolation: CLEARED/FLAGGED
   - Kelly re-sizing: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
   - Regression: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
