# P18-WI-18 — Bankroll Sync Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi18-bankroll-sync` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/web3-specialist.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/test-engineer.md`
- `.agents/rules/security-auditor.md`

## Role

You are implementing WI-18 for Phase 5: a live, read-only USDC balance feed from the Polygon L2 blockchain that replaces the static `initial_bankroll_usdc` config value in the Kelly sizing formula.

This WI is read-only. It must fetch the wallet's on-chain USDC balance before every evaluation cycle. It must not approve, transfer, or mutate any on-chain state.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi18.md`
4. `docs/PRD-v5.0.md` (Phase 5 section)
   If `PRD-v5.0.md` is not present, read the current Phase 5 PRD section from:
   - `docs/archive/ARCHIVE_PHASE_4.md` (`## Next Phase (Phase 5)`)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/agents/execution/bankroll_tracker.py` — **this is the primary integration target; `get_total_bankroll()` currently returns static config**
9. `src/agents/execution/signer.py` (context boundary: bankroll sync must remain isolated from signer)
10. `src/agents/execution/polymarket_client.py` (context boundary: bankroll sync must remain isolated from market-data modules)
11. `src/orchestrator.py`
12. `src/core/config.py`
13. `src/core/exceptions.py`
14. Existing tests:
    - `tests/unit/test_bankroll_tracker.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-18 test files first:
   - `tests/unit/test_bankroll_sync.py`
   - `tests/integration/test_bankroll_sync_integration.py`
2. Write failing tests for all required behaviors:
   - `BankrollSyncProvider` exists in `src/agents/execution/bankroll_sync.py` and exposes a single public method: `async def fetch_balance() -> Decimal`.
   - `dry_run=True` path returns `AppConfig.initial_bankroll_usdc` as `Decimal`, does not instantiate `Web3`, does not issue any RPC call.
   - `dry_run=False` path issues `balanceOf(wallet_address)` against canonical Polygon USDC contract and returns `Decimal`.
   - On-chain `uint256` result is converted via `Decimal(raw) / Decimal("1e6")` — no `float` intermediary.
   - RPC timeout (>500ms) raises `BalanceFetchError` — no fallback.
   - RPC connection/network error raises `BalanceFetchError` — no fallback.
   - Malformed RPC response raises `BalanceFetchError` — no fallback.
   - Zero balance (`uint256 = 0`) is valid and returns `Decimal("0")`.
   - `BankrollSyncProvider` has zero imports from `signer.py`, `polymarket_client.py`, or evaluation/context modules.
   - `BankrollPortfolioTracker.get_total_bankroll()` delegates to `BankrollSyncProvider.fetch_balance()` (no longer returns `self._config.initial_bankroll_usdc` directly).
3. Run RED tests:
   - `pytest tests/unit/test_bankroll_sync.py -v`
   - `pytest tests/integration/test_bankroll_sync_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add `BalanceFetchError` to Exception Taxonomy

Target:
- `src/core/exceptions.py`

Requirements:
1. Add `BalanceFetchError` exception class.
2. It must be a distinct exception, not a subclass of `ExposureLimitError` or other existing exceptions.
3. Structured message should carry `wallet_address` and `reason` without exposing RPC credentials.

### Step 2 — Create `BankrollSyncProvider` Module

Target:
- `src/agents/execution/bankroll_sync.py` (new)

Requirements:
1. New class `BankrollSyncProvider` with constructor accepting `config: AppConfig`.
2. Single public method: `async def fetch_balance(self) -> Decimal`.
3. Module-level constants (values are exact, names may vary):
   - `POLYGON_USDC_PROXY = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"` (native USDC on Polygon PoS)
   - `USDC_DECIMALS = 6`
4. Minimal ERC-20 ABI: only `balanceOf(address)` — no other functions encoded.
5. Contract address is a constant, not configurable via env var.
6. Structured logging via `structlog` only — no `print()`.
7. Zero imports from:
   - `src/agents/execution/signer.py`
   - `src/agents/execution/polymarket_client.py`
   - `src/agents/evaluation/*`
   - `src/agents/context/*`
   - `src/agents/ingestion/*`

### Step 3 — Implement `fetch_balance()` Async Contract

Target:
- `src/agents/execution/bankroll_sync.py`

Requirements:
1. **`dry_run` gate (first check):**
   - Read `self._config.dry_run`.
   - If `True`: return `self._config.initial_bankroll_usdc` immediately. Log the mock return. Do NOT instantiate `Web3`, do NOT open any network connection.
2. **Web3 provider construction:**
   - Instantiate `Web3(Web3.HTTPProvider(self._config.polygon_rpc_url))` only after `dry_run` gate passes.
   - Use the provider to construct a contract instance with the minimal `balanceOf` ABI.
3. **On-chain read with timeout:**
   - Call `contract.functions.balanceOf(wallet_address).call()` (or async equivalent).
   - Wrap the entire RPC call in `asyncio.wait_for(..., timeout=0.5)`.
   - The 500ms budget is consistent with WI-14 `PolymarketClient` timeout.
4. **uint256 → Decimal conversion:**
   - `balance_usdc = Decimal(raw_uint256) / Decimal("1e6")`
   - No `float()` anywhere in the conversion path.
   - Assert `balance_usdc >= 0`; negative result is a fatal `AssertionError`.
5. **Failure semantics (fail closed):**
   - `asyncio.TimeoutError` → raise `BalanceFetchError("RPC timeout ...")`.
   - `Web3` connection error, `ValueError`, or any other RPC error → raise `BalanceFetchError(...)`.
   - Malformed/non-integer response → raise `BalanceFetchError(...)`.
   - **Never** fall back to `self._config.initial_bankroll_usdc` on failure. **Never** return a cached/stale value. **Never** swallow the exception.
6. **Structured logging:**
   - Success: log `bankroll_sync.balance_fetched` with `balance_usdc`, `block_number`, `latency_ms`.
   - Failure: log `bankroll_sync.fetch_failed` with `wallet_address`, `reason`, `latency_ms`. Never log RPC URL credentials or full error tracebacks containing URLs.

### Step 4 — Integrate into `BankrollPortfolioTracker`

Target:
- `src/agents/execution/bankroll_tracker.py`

Requirements:
1. Add `BankrollSyncProvider` as a constructor dependency of `BankrollPortfolioTracker`.
2. **Replace the static config lookup in `get_total_bankroll()`:**
   - Current (REMOVE): `return self._config.initial_bankroll_usdc`
   - New (REPLACE WITH): `return await self._bankroll_sync.fetch_balance()`
3. `get_total_bankroll()` must NOT catch `BalanceFetchError`. The exception must propagate to `compute_position_size()` and `validate_trade()` callers, blocking the evaluation cycle.
4. All downstream callers of `get_total_bankroll()` already `await` it — verify no signature change is needed.

**CRITICAL CHECK:** After this step, grep the entire `src/` tree for any remaining direct reference to `config.initial_bankroll_usdc` in bankroll-computation paths (excluding `dry_run` mock return in `BankrollSyncProvider`). There must be zero.

### Step 5 — Update Orchestrator Wiring

Target:
- `src/orchestrator.py`

Requirements:
1. Instantiate `BankrollSyncProvider(config=config)` at orchestrator startup.
2. Pass the provider instance to `BankrollPortfolioTracker(...)` constructor.
3. No other orchestrator changes — queue topology, task structure, and pipeline order remain unchanged.

### Step 6 — Update Existing `BankrollPortfolioTracker` Tests

Target:
- `tests/unit/test_bankroll_tracker.py`

Requirements:
1. Existing tracker tests must be updated to inject a mock `BankrollSyncProvider` (or equivalent fixture).
2. Tests that previously relied on `config.initial_bankroll_usdc` as the tracker's bankroll source must now mock `fetch_balance()` instead.
3. All existing tracker test assertions must continue to pass — zero behavioral regression.

### Step 7 — GREEN Validation

Run:
```bash
pytest tests/unit/test_bankroll_sync.py -v
pytest tests/integration/test_bankroll_sync_integration.py -v
pytest tests/unit/test_bankroll_tracker.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. `BankrollSyncProvider` is strictly read-only — no `approve`, `transfer`, `transferFrom`, no gas, no nonce, no state mutation.
2. `BankrollSyncProvider` is isolated — zero imports from signer, polymarket client, evaluation, context, or ingestion modules.
3. `dry_run=True` must return mock balance from config and NEVER instantiate Web3 or contact the RPC node.
4. All balance math is `Decimal` only — `float` is rejected at Pydantic boundary and never used in conversion.
5. uint256 → USDC conversion is `Decimal(raw) / Decimal("1e6")` only.
6. RPC call is wrapped in `asyncio.wait_for(..., timeout=0.5)`.
7. Fetch failure raises `BalanceFetchError` — no silent fallback to config, cache, or stale value.
8. `BankrollPortfolioTracker.get_total_bankroll()` no longer returns `self._config.initial_bankroll_usdc` — it delegates to `BankrollSyncProvider.fetch_balance()`.
9. No bypass of `LLMEvaluationResponse` terminal Gatekeeper.
10. No queue topology changes; preserve async pipeline order.

---

## Required Test Matrix

At minimum, WI-18 tests must prove:
1. `fetch_balance()` with `dry_run=True` returns `Decimal` mock balance, no Web3 instantiation, no RPC call.
2. `fetch_balance()` with `dry_run=False` issues `balanceOf` call and returns `Decimal`-converted result.
3. uint256 → Decimal conversion correctness for known fixtures (e.g., `1000000` → `Decimal("1")`, `1500000000` → `Decimal("1500")`).
4. RPC timeout (>500ms) raises `BalanceFetchError`, not a fallback value.
5. RPC connection error raises `BalanceFetchError`, not a fallback value.
6. Malformed RPC response raises `BalanceFetchError`, not a fallback value.
7. Zero balance (`uint256 = 0`) returns `Decimal("0")` — valid, not an error.
8. Import-boundary test confirms no signer/polymarket/evaluation/context module dependencies.
9. `BankrollPortfolioTracker.get_total_bankroll()` calls `fetch_balance()` — does NOT return static config.
10. `BankrollPortfolioTracker.compute_position_size()` propagates `BalanceFetchError` — does not catch or swallow it.
11. Existing `test_bankroll_tracker.py` tests pass with zero behavioral regression.
12. No WI-18 code path introduces token approval, transfer, or state-mutating capability.

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
You are the MAAP Checker for WI-18 (Bankroll Sync) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi18.md
2) Phase 5 PRD section (docs/PRD-v5.0.md WI-18 section, or ARCHIVE_PHASE_4.md Next Phase section if PRD-v5.0 is unavailable)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Decimal violations (any float usage in balance/amount money-path logic)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation)
- Business logic drift (deviation from WI-18 read-only scope, dry_run rules, or fail-closed semantics)
- Read-only violations (any approve/transfer/transferFrom or state-mutating on-chain call in bankroll_sync.py)
- Isolation violations (BankrollSyncProvider importing signer, polymarket_client, evaluation, or context modules)

Additional required checks:
- BankrollSyncProvider class exists in src/agents/execution/bankroll_sync.py
- fetch_balance() is async, returns Decimal, and is the only public method
- balanceOf call targets canonical Polygon USDC proxy address (0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359)
- uint256 → Decimal conversion uses Decimal(raw) / Decimal("1e6") only
- asyncio.wait_for wraps RPC call with timeout=0.5
- dry_run=True path returns config mock balance without Web3 instantiation or RPC contact
- Fetch failure raises BalanceFetchError — no fallback to config/cache/stale value
- STATIC CONFIG REPLACEMENT CHECK: BankrollPortfolioTracker.get_total_bankroll() no longer contains `return self._config.initial_bankroll_usdc` — it delegates to BankrollSyncProvider.fetch_balance(). Grep for any remaining direct config.initial_bankroll_usdc reference in bankroll computation paths (the ONLY allowed reference is the dry_run mock return inside BankrollSyncProvider itself).
- No new send/broadcast/approve/transfer capability introduced

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-18/invariants
4) Explicit statement on each MAAP critical category:
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Business logic drift: CLEARED/FLAGGED
   - Read-only violations: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
   - Static config replacement: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
