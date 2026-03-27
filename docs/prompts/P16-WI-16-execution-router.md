# P16-WI-16 — Execution Router Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi16-execution-router` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/web3-specialist.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/test-engineer.md`
- `.agents/rules/security-auditor.md`

## Role

You are implementing WI-16 for Phase 5: the Execution Router that connects validated BUY decisions from `LLMEvaluationResponse` into sized, slippage-checked, signed limit orders on Polymarket CLOB (Polygon L2).

This is the first WI that wires WI-14 (market data), WI-15 (signing), and WI-18 (bankroll) into a single execution path. The router is an orchestrator only — it calls `PolymarketClient.fetch_order_book()`, `BankrollSyncProvider.fetch_balance()`, and `TransactionSigner.sign_order()` but owns none of their logic. It owns Kelly sizing, slippage validation, and order-size capping.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi16.md`
4. `docs/PRD-v5.0.md` (Phase 5 section)
   If `PRD-v5.0.md` is not present, read the current Phase 5 PRD section from:
   - `docs/archive/ARCHIVE_PHASE_4.md` (`## Next Phase (Phase 5)`)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/agents/execution/polymarket_client.py` — order book fetch contract (`fetch_order_book()` → `MarketSnapshot`)
9. `src/agents/execution/bankroll_sync.py` — live balance contract (`fetch_balance()` → `BalanceReadResult`)
10. `src/agents/execution/signer.py` — signing contract (`sign_order()` → `SignedOrder`)
11. `src/agents/execution/bankroll_tracker.py` — position sizing (context boundary: router does NOT call tracker directly)
12. `src/schemas/llm.py` — `LLMEvaluationResponse`, `MarketContext`, `RecommendedAction`
13. `src/schemas/web3.py` — `OrderData`, `SignedOrder`, `OrderSide`
14. `src/core/config.py` — `AppConfig` (new fields will be added here)
15. `src/core/exceptions.py` — existing exception taxonomy (new exceptions will be added here)
16. `src/orchestrator.py` — wiring target for `ExecutionRouter` instantiation
17. Existing tests:
    - `tests/unit/test_signer.py`
    - `tests/unit/test_polymarket_client.py`
    - `tests/unit/test_bankroll_sync.py`
    - `tests/unit/test_bankroll_tracker.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-16 test files first:
   - `tests/unit/test_execution_router.py`
   - `tests/integration/test_execution_router_integration.py`
2. Write failing tests for all required behaviors:
   - `ExecutionRouter` exists in `src/agents/execution/execution_router.py` and exposes a single public method: `async def route(response: LLMEvaluationResponse, market_context: MarketContext) -> ExecutionResult`.
   - **Entry gate — non-BUY actions never reach upstream:** when `response.recommended_action` is `HOLD` or `SELL`, `route()` returns `ExecutionResult(action=SKIP)` immediately without calling `fetch_order_book()`, `fetch_balance()`, or `sign_order()`. **This test MUST prove `sign_order()` is NEVER called when action != BUY** (assert mock call count == 0).
   - **Entry gate — low confidence:** when `response.recommended_action == BUY` but `response.confidence_score < config.min_confidence`, `route()` returns `ExecutionResult(action=SKIP)`.
   - **Slippage guard:** when `best_ask > midpoint_probability + config.max_slippage_tolerance`, `route()` returns `ExecutionResult(action=FAILED, reason="slippage_exceeded")`.
   - **Kelly sizing — all Decimal:** Kelly formula `fraction = edge / odds` where `edge = midpoint_probability - threshold` and `odds = (1 - midpoint_probability) / midpoint_probability` produces correct `Decimal` results for known fixtures.
   - **Kelly — no positive edge:** when `midpoint_probability <= threshold`, `route()` returns `ExecutionResult(action=FAILED, reason="no_positive_edge")`.
   - **Kelly — degenerate midpoint:** when `midpoint_probability` is `0` or `1`, `route()` returns `ExecutionResult(action=FAILED, reason="degenerate_midpoint")`.
   - **Order size cap:** `order_size = min(kelly_fraction * bankroll, config.max_order_usdc)` — both `Decimal`.
   - **dry_run=True:** builds full `OrderData` payload, logs it, returns `ExecutionResult(action=DRY_RUN)` with `order_payload` populated. `sign_order()` is NEVER called (assert mock call count == 0).
   - **dry_run=False — happy path:** calls `sign_order()`, returns `ExecutionResult(action=EXECUTED)` with `signed_order` populated.
   - **Upstream failure — order book unavailable:** `fetch_order_book()` returns `None` → `ExecutionResult(action=FAILED, reason="order_book_unavailable")`.
   - **Upstream failure — balance fetch error:** `fetch_balance()` raises `BalanceFetchError` → `ExecutionResult(action=FAILED, reason="balance_fetch_error")`.
   - **Upstream failure — signing error:** `sign_order()` raises → `ExecutionResult(action=FAILED, reason="signing_error")`.
   - **Import boundary:** `ExecutionRouter` module has zero imports from prompt, context, ingestion, or database modules.
3. Run RED tests:
   - `pytest tests/unit/test_execution_router.py -v`
   - `pytest tests/integration/test_execution_router_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add New Exceptions to Exception Taxonomy

Target:
- `src/core/exceptions.py`

Requirements:
1. Add `RoutingRejectedError(PolyOracleError)` — decision did not pass entry gate or sizing produced non-positive result.
2. Add `RoutingAbortedError(PolyOracleError)` — upstream dependency failed (stale book, balance error, signing error).
3. Add `SlippageExceededError(RoutingAbortedError)` — best ask exceeds slippage tolerance.
4. All exceptions must carry structured context (`token_id`, `reason`, relevant values) without exposing private keys or RPC credentials.
5. `SlippageExceededError` is a subclass of `RoutingAbortedError` — not of `PolyOracleError` directly.

### Step 2 — Add New AppConfig Fields

Target:
- `src/core/config.py`

Requirements:
1. Add `max_order_usdc: Decimal = Field(default=Decimal("50"), description="Hard cap on any single order in USDC")`.
2. Add `max_slippage_tolerance: Decimal = Field(default=Decimal("0.02"), description="Max allowed deviation of best_ask above midpoint (2%)")`.
3. Both fields are `Decimal`, not `float`.
4. Place them in a new `# --- Execution Router (WI-16) ---` section below the existing `# --- Bankroll ---` section.

### Step 3 — Create `ExecutionResult` Schema

Target:
- `src/schemas/execution.py` (new)

Requirements:
1. `ExecutionAction` enum with values: `SKIP`, `DRY_RUN`, `EXECUTED`, `FAILED`.
2. `ExecutionResult` Pydantic model with fields:
   - `action`: `ExecutionAction`
   - `reason`: `str | None` (human-readable failure/skip reason; `None` on success)
   - `order_payload`: `OrderData | None` (populated when payload was constructed; `None` on early SKIP)
   - `signed_order`: `SignedOrder | None` (populated only on `EXECUTED`)
   - `kelly_fraction`: `Decimal | None` (raw Kelly fraction before cap)
   - `order_size_usdc`: `Decimal | None` (final USDC amount after cap)
   - `midpoint_probability`: `Decimal | None` (from order book snapshot)
   - `best_ask`: `Decimal | None` (from order book snapshot)
   - `bankroll_usdc`: `Decimal | None` (live bankroll at routing time)
   - `routed_at_utc`: `datetime`
3. All financial fields are `Decimal | None`. No `float`.
4. `float` inputs in financial fields rejected at Pydantic schema boundary.

### Step 4 — Create `ExecutionRouter` Module

Target:
- `src/agents/execution/execution_router.py` (new)

Requirements:
1. New class `ExecutionRouter` with constructor accepting:
   - `config: AppConfig`
   - `polymarket_client: PolymarketClient`
   - `bankroll_provider: BankrollSyncProvider`
   - `transaction_signer: TransactionSigner`
2. Single public method: `async def route(self, response: LLMEvaluationResponse, market_context: MarketContext) -> ExecutionResult`.
3. Structured logging via `structlog` only — no `print()`.
4. Zero imports from:
   - `src/agents/evaluation/*` (except schema imports from `src/schemas/llm.py`)
   - `src/agents/context/*`
   - `src/agents/ingestion/*`
   - `src/db/*`

### Step 5 — Implement `route()` Async Contract

Target:
- `src/agents/execution/execution_router.py`

Requirements:

1. **Entry gate (first check):**
   - Check `response.recommended_action == RecommendedAction.BUY`. If not, return `ExecutionResult(action=SKIP, reason="action_is_{action}")` immediately. No upstream calls.
   - Check `response.confidence_score >= self._config.min_confidence`. If not, return `ExecutionResult(action=SKIP, reason="confidence_below_threshold")` immediately. No upstream calls.
   - **CRITICAL:** when action is not BUY, `sign_order()` must NEVER be called. The function returns before any upstream interaction.

2. **Fetch order book:**
   - Call `self._polymarket_client.fetch_order_book(market_context.condition_id)`.
   - If result is `None`: return `ExecutionResult(action=FAILED, reason="order_book_unavailable")`. Log at WARNING.

3. **Slippage guard:**
   - Compute `slippage_limit = snapshot.midpoint_probability + Decimal(str(self._config.max_slippage_tolerance))`.
   - If `snapshot.best_ask > slippage_limit`: return `ExecutionResult(action=FAILED, reason="slippage_exceeded")`. Log at WARNING with `best_ask`, `midpoint`, `tolerance`.

4. **Fetch bankroll:**
   - Call `self._bankroll_provider.fetch_balance()`.
   - If `BalanceFetchError` is raised: catch it, log at ERROR, return `ExecutionResult(action=FAILED, reason="balance_fetch_error")`. Do NOT let it propagate uncaught — the router must always return a typed `ExecutionResult`.

5. **Kelly sizing (all Decimal, no float):**
   - `threshold = Decimal(str(self._config.min_ev_threshold))`
   - `midpoint = snapshot.midpoint_probability`
   - Guard: if `midpoint <= Decimal("0") or midpoint >= Decimal("1")`: return `ExecutionResult(action=FAILED, reason="degenerate_midpoint")`.
   - `edge = midpoint - threshold`
   - Guard: if `edge <= Decimal("0")`: return `ExecutionResult(action=FAILED, reason="no_positive_edge")`.
   - `odds = (Decimal("1") - midpoint) / midpoint`
   - `kelly_raw = edge / odds`
   - `kelly_scaled = kelly_raw * Decimal(str(self._config.kelly_fraction))`

6. **Order size capping:**
   - `order_size = min(kelly_scaled * bankroll_usdc, Decimal(str(self._config.max_order_usdc)))`
   - Guard: if `order_size <= Decimal("0")`: return `ExecutionResult(action=FAILED, reason="non_positive_order_size")`.

7. **Build OrderData payload:**
   - `maker_amount = int(order_size * Decimal("1e6"))` (USDC micro-units, 6 decimals).
   - `taker_amount`: if `midpoint > 0`, `int((order_size / midpoint) * Decimal("1e6"))`, else `0`.
   - Construct `OrderData` with:
     - `salt`: `secrets.randbits(256)`
     - `maker`: `self._config.wallet_address`
     - `signer`: `self._config.wallet_address`
     - `taker`: `"0x0000000000000000000000000000000000000000"`
     - `token_id`: `int(market_context.condition_id)` (uint256)
     - `maker_amount`, `taker_amount`, `side=OrderSide.BUY`
     - `expiration=0`, `nonce=0`, `fee_rate_bps=0`, `signature_type=0`

8. **dry_run gate:**
   - If `self._config.dry_run is True`:
     - Log full order payload at INFO level (token_id, side, maker_amount, taker_amount, order_size_usdc, kelly_fraction). Never log private keys.
     - Return `ExecutionResult(action=DRY_RUN, order_payload=order_data, kelly_fraction=kelly_scaled, order_size_usdc=order_size, midpoint_probability=midpoint, best_ask=snapshot.best_ask, bankroll_usdc=bankroll_usdc)`.
     - **CRITICAL:** `sign_order()` is NEVER called in dry_run. Return before reaching signer.

9. **Sign order:**
   - Call `self._transaction_signer.sign_order(order_data)`.
   - If any exception is raised: catch it, log at ERROR, return `ExecutionResult(action=FAILED, reason="signing_error")`.

10. **Return success:**
    - Return `ExecutionResult(action=EXECUTED, signed_order=signed_order, order_payload=order_data, kelly_fraction=kelly_scaled, order_size_usdc=order_size, midpoint_probability=midpoint, best_ask=snapshot.best_ask, bankroll_usdc=bankroll_usdc)`.

### Step 6 — Update Orchestrator Wiring

Target:
- `src/orchestrator.py`

Requirements:
1. Import `ExecutionRouter` from `src.agents.execution.execution_router`.
2. Import `PolymarketClient` from `src.agents.execution.polymarket_client`.
3. Instantiate `PolymarketClient(host=self.config.clob_rest_url)` at orchestrator startup.
4. Instantiate `ExecutionRouter(config=self.config, polymarket_client=..., bankroll_provider=self.bankroll_sync, transaction_signer=self.signer)` at orchestrator startup.
   - Note: `self.signer` may be `None` when `dry_run=True`. The router constructor should accept `TransactionSigner | None`. When dry_run is True, the signer is never called so `None` is safe. When dry_run is False and signer is `None`, the route method must return `ExecutionResult(action=FAILED, reason="signer_unavailable")`.
5. No other orchestrator changes — queue topology, task structure, and pipeline order remain unchanged.

### Step 7 — Update Existing Tests (If Needed)

Target:
- `tests/integration/test_orchestrator.py`
- `tests/integration/test_pipeline_e2e.py`

Requirements:
1. Existing orchestrator tests must account for the new `ExecutionRouter` wiring.
2. If any existing test constructs an `Orchestrator` directly, it must now work with `ExecutionRouter` present.
3. All existing test assertions must continue to pass — zero behavioral regression.

### Step 8 — GREEN Validation

Run:
```bash
pytest tests/unit/test_execution_router.py -v
pytest tests/integration/test_execution_router_integration.py -v
pytest tests/unit/test_signer.py -v
pytest tests/unit/test_polymarket_client.py -v
pytest tests/unit/test_bankroll_sync.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. `ExecutionRouter` is an orchestrator only — it calls upstream components but owns none of their internal logic.
2. `ExecutionRouter` is isolated — zero imports from context, prompt, ingestion, or database modules.
3. `LLMEvaluationResponse` remains the terminal pre-execution Gatekeeper — the router only activates on `action=BUY` with `confidence >= threshold`.
4. `sign_order()` is NEVER called when `recommended_action != BUY` — the entry gate returns `SKIP` before any upstream interaction.
5. `sign_order()` is NEVER called when `dry_run=True` — the dry_run gate returns `DRY_RUN` before reaching the signer.
6. Kelly sizing formula uses `Decimal` exclusively — no `float()` conversion at any step.
7. Slippage guard rejects when `best_ask > midpoint_probability + max_slippage_tolerance`.
8. Order size is `min(kelly_fraction * bankroll, max_order_usdc)` — both `Decimal`.
9. Any upstream failure returns `ExecutionResult(action=FAILED)` — no retry, no fallback, nothing enqueued.
10. No database writes occur anywhere in the routing path (position tracking is WI-17).
11. No queue topology changes; preserve async 4-layer pipeline order.
12. `PolymarketClient`, `BankrollSyncProvider`, and `TransactionSigner` internals are unmodified.

---

## Required Test Matrix

At minimum, WI-16 tests must prove:

1. **[CRITICAL] `sign_order()` never called on non-BUY:** `route()` with `action=HOLD` or `action=SELL` returns `ExecutionResult(action=SKIP)`. Mock on `sign_order` asserts call count == 0.
2. **[CRITICAL] `sign_order()` never called on dry_run:** `route()` with `action=BUY` and `dry_run=True` returns `ExecutionResult(action=DRY_RUN)`. Mock on `sign_order` asserts call count == 0.
3. BUY with confidence below `min_confidence` returns `ExecutionResult(action=SKIP)` — no upstream calls.
4. Kelly formula correctness for known fixtures: `midpoint=Decimal("0.65")`, `threshold=Decimal("0.02")`, `kelly_fraction=Decimal("0.25")` → verify exact `Decimal` result.
5. Kelly with no positive edge (`midpoint <= threshold`) returns `FAILED`.
6. Kelly with degenerate midpoint (0 or 1) returns `FAILED`.
7. Slippage guard: `best_ask > midpoint + tolerance` returns `FAILED(reason="slippage_exceeded")`.
8. Slippage guard: `best_ask <= midpoint + tolerance` proceeds to Kelly sizing.
9. Order size cap: `order_size = min(kelly * bankroll, max_order_usdc)` — test both branches (kelly-limited and cap-limited).
10. `OrderData.maker_amount` computed as `int(order_size * Decimal("1e6"))` matches expected micro-units.
11. `fetch_order_book()` returning `None` → `FAILED(reason="order_book_unavailable")`.
12. `fetch_balance()` raising `BalanceFetchError` → `FAILED(reason="balance_fetch_error")`.
13. `sign_order()` raising exception → `FAILED(reason="signing_error")`.
14. Happy path `dry_run=False`: `EXECUTED` with `signed_order` populated.
15. Import-boundary test: `execution_router.py` has no dependency on prompt/context/ingestion/database modules.
16. All financial fields in `ExecutionResult` are `Decimal` type.
17. `AppConfig.max_order_usdc` is `Decimal("50")` by default.
18. `AppConfig.max_slippage_tolerance` is `Decimal("0.02")` by default.
19. Signer is `None` + `dry_run=True` → `DRY_RUN` (succeeds, signer not needed).
20. Signer is `None` + `dry_run=False` → `FAILED(reason="signer_unavailable")`.
21. Full suite regression: `pytest --asyncio-mode=auto tests/ -q` passes, coverage >= 80%.

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
You are the MAAP Checker for WI-16 (Execution Router) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi16.md
2) Phase 5 PRD section (docs/PRD-v5.0.md WI-16 section, or ARCHIVE_PHASE_4.md Next Phase section if PRD-v5.0 is unavailable)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Decimal violations (any float usage in Kelly sizing, order amount, slippage, or bankroll paths)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation)
- Business logic drift (deviation from WI-16 orchestrator-only scope, dry_run rules, or fail-closed semantics)
- Signing safety violations (any path where sign_order() is called when action != BUY or dry_run=True)
- Isolation violations (ExecutionRouter importing prompt, context, ingestion, or database modules)

Additional required checks:
- KELLY FORMULA CORRECTNESS: Verify the exact formula: edge = midpoint_probability - threshold, odds = (1 - midpoint_probability) / midpoint_probability, fraction = edge / odds. All operands and results must be Decimal. No float() conversion at any step. Threshold is Decimal(str(config.min_ev_threshold)). Kelly fraction multiplier is Decimal(str(config.kelly_fraction)).
- SLIPPAGE GUARD PRESENCE: Verify that best_ask > midpoint_probability + max_slippage_tolerance produces a FAILED result. max_slippage_tolerance comes from AppConfig as Decimal.
- DRY_RUN BYPASS: Verify dry_run=True builds and logs OrderData but NEVER calls sign_order(). Verify a test exists that asserts sign_order mock call count == 0 under dry_run.
- NON-BUY SIGNING GUARD: Verify that when recommended_action != BUY, sign_order() is NEVER called. Verify a test exists that asserts sign_order mock call count == 0 for HOLD and SELL actions.
- ExecutionRouter class exists in src/agents/execution/execution_router.py
- route() is async, accepts (LLMEvaluationResponse, MarketContext), returns ExecutionResult
- route() is the only public method
- Order size capped at min(kelly_fraction * bankroll, config.max_order_usdc) — both Decimal
- AppConfig gains max_order_usdc: Decimal (default 50) and max_slippage_tolerance: Decimal (default 0.02)
- RoutingRejectedError, RoutingAbortedError, SlippageExceededError exist in src/core/exceptions.py
- No database writes in any routing path
- No modification to PolymarketClient, BankrollSyncProvider, or TransactionSigner internals
- No new send/broadcast/approve/transfer capability introduced

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-16/invariants
4) Explicit statement on each MAAP critical category:
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Business logic drift: CLEARED/FLAGGED
   - Signing safety violations: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
   - Kelly formula correctness: CLEARED/FLAGGED
   - Slippage guard presence: CLEARED/FLAGGED
   - dry_run bypass: CLEARED/FLAGGED
   - Non-BUY signing guard: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
