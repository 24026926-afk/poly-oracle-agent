# P31-WI-31 — Live Wallet Balance Checks Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi31-live-balances` (branched from current `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/risk-auditor.md`
- `.agents/rules/async-architect.md`
- `.agents/rules/security-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-31 for Phase 10: a live, fail-open wallet balance gate that checks the operator's on-chain MATIC and USDC balances before any LLM evaluation is attempted. If the live balances are confirmed insufficient (and this is not due to a fallback/timeout), the Orchestrator short-circuits with a SKIP.

Today, `Orchestrator._execution_consumer_loop()` has no visibility into actual on-chain wallet state. `BankrollSyncProvider` (WI-18) syncs the USDC bankroll for Kelly sizing but is fail-closed and not designed as a per-evaluation gate. WI-31 inserts a new async `WalletBalanceProvider` that runs two Polygon JSON-RPC calls in parallel, evaluates results against configured thresholds, and returns a typed `BalanceCheckResult`. The gate is fail-open: RPC timeouts or errors never block an evaluation.

When the gate fires on confirmed insufficient balance, the trade is short-circuited with `ExecutionResult(action=SKIP, reason="insufficient_wallet_balance")` — no LLM API call is made, no gas check is run, and no order is routed.

---

## Objective & Scope

### In Scope
1. Create `src/agents/execution/wallet_balance_provider.py` — `WalletBalanceProvider` with three public async methods.
2. Add `BalanceCheckResult` frozen Pydantic model to `src/schemas/web3.py`.
3. Add three `AppConfig` fields: `enable_wallet_balance_check`, `min_matic_balance_wei`, `min_usdc_balance_usdc`.
4. Wire `WalletBalanceProvider` into `Orchestrator.__init__()` (conditional) and `_execution_consumer_loop()` (AFTER ExposureValidator WI-30 and BEFORE GasEstimator WI-29).
5. structlog audit events: `wallet.balance_checked`, `wallet.balance_insufficient`, `wallet.balance_fallback_used`.

### Out of Scope
1. Replacement or modification of `BankrollSyncProvider` (WI-18) — independent components.
2. MATIC-to-USDC price conversion for threshold comparison — MATIC threshold is compared directly in WEI.
3. Persistent caching of balance results between loop iterations.
4. WebSocket subscriptions for real-time balance updates.
5. Exit Path gating — `WalletBalanceProvider` NEVER gates `_exit_scan_loop()`.
6. Modifications to `KellySizer`, `ExposureValidator`, `GasEstimator`, or `ClaudeClient` internals.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi31.md`
4. `docs/PRD-v10.0.md` (WI-31 section)
5. `src/agents/execution/gas_estimator.py` — **primary httpx RPC pattern: `eth_gasPrice` via `httpx.AsyncClient`**
6. `src/agents/execution/bankroll_sync.py` — **context: `POLYGON_USDC_PROXY`, `BALANCE_OF_SELECTOR`, and `_USDC_SCALE` constants; understand why WI-31 does NOT use this class**
7. `src/agents/execution/exposure_validator.py` — **context: WI-30 gate wiring in `_execution_consumer_loop()` — WI-31 inserts AFTER this**
8. `src/schemas/web3.py` — **target: add `BalanceCheckResult` model alongside `GasPrice`**
9. `src/core/config.py` — **target: add 3 new AppConfig fields**
10. `src/orchestrator.py` — **target: wire WalletBalanceProvider into `_execution_consumer_loop()` AFTER WI-30 and BEFORE WI-29**
11. `src/schemas/execution.py` — **context: `ExecutionResult` and `Action.SKIP` already exist**
12. Existing test files (verify no regression):
    - `tests/unit/test_orchestrator.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`
    - `tests/unit/test_wi29_live_fees.py`
    - `tests/unit/test_wi30_exposure_limits.py`
    - `tests/integration/test_wi30_exposure_limits_integration.py`

**CRITICAL PRE-FLIGHT CHECK:** After reading `src/agents/execution/gas_estimator.py`, confirm the exact httpx pattern used for Polygon JSON-RPC (post vs. get, timeout placement, response parsing). WI-31 must use the same pattern. After reading `src/agents/execution/bankroll_sync.py`, record the `POLYGON_USDC_PROXY` constant value — do NOT import it from `bankroll_sync.py` (avoids cross-module coupling); define it independently in `wallet_balance_provider.py` with a comment citing its origin.

Do not proceed if this context is not loaded.

---

## CRITICAL INVARIANT: Fully Async — httpx JSON-RPC Only

`WalletBalanceProvider` uses `httpx.AsyncClient` for ALL RPC calls. No web3.py, no `asyncio.run()`, no `asyncio.wait_for` wrapping a sync web3 call, no `run_in_executor`. The provider is a first-class async component:

```python
# CORRECT — httpx async pattern (mirrors GasEstimator):
payload = {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [address, "latest"], "id": 1}
response = await self._client.post(self._config.polygon_rpc_url, json=payload)

# WRONG — do not use web3.py or run_in_executor:
w3 = Web3(Web3.HTTPProvider(rpc_url))
raw = await loop.run_in_executor(None, w3.eth.get_balance, address)
```

---

## CRITICAL INVARIANT: Fail-Open — RPC Errors Do NOT Block Evaluation

`check_balances()` catches ALL exceptions from `asyncio.gather`:

```python
# CORRECT — any exception returns a fallback, gate passes:
try:
    matic_wei, usdc_usdc = await asyncio.gather(
        self.get_matic_balance_wei(address),
        self.get_usdc_balance_usdc(address),
    )
except Exception as exc:
    self.log.warning("wallet.balance_fallback_used", error=str(exc))
    return self._build_fallback_result()

# WRONG — do not let exceptions propagate to the Orchestrator:
matic_wei, usdc_usdc = await asyncio.gather(...)  # raises → crashes loop
```

`_build_fallback_result()` returns `BalanceCheckResult(check_passed=True, fallback_used=True)`. The Orchestrator proceeds when `check_passed=True` — it does NOT inspect `fallback_used` separately.

---

## CRITICAL INVARIANT: Strict Decimal Math

Every balance computation — hex-to-int parsing, WEI-to-MATIC division, uint256-to-USDC division, threshold comparison — must use `Decimal`:

```python
_WEI_PER_MATIC = Decimal("1000000000000000000")
_USDC_SCALE    = Decimal("1000000")

# Hex → Decimal WEI (no float):
matic_wei = Decimal(str(int(hex_string, 16)))

# WEI → MATIC (exact Decimal division, no float):
matic_matic = matic_wei / _WEI_PER_MATIC

# uint256 → USDC (exact Decimal division):
usdc_usdc = Decimal(str(int(hex_string, 16))) / _USDC_SCALE

# Threshold comparison (Decimal >= Decimal):
matic_sufficient = matic_wei >= self._config.min_matic_balance_wei
```

`float` anywhere in this path is a bug.

---

## CRITICAL INVARIANT: Parallel RPC Calls via `asyncio.gather`

Both balance lookups MUST run concurrently:

```python
# CORRECT — parallel RPC calls:
matic_wei, usdc_usdc = await asyncio.gather(
    self.get_matic_balance_wei(self._config.wallet_address),
    self.get_usdc_balance_usdc(self._config.wallet_address),
)

# WRONG — sequential RPC calls double the latency:
matic_wei = await self.get_matic_balance_wei(self._config.wallet_address)
usdc_usdc = await self.get_usdc_balance_usdc(self._config.wallet_address)
```

---

## CRITICAL INVARIANT: Gate Order in Consumer Loop

When WI-29, WI-30, and WI-31 are all enabled, the order in `_execution_consumer_loop()` MUST be:

```
Kelly sizing → ExposureValidator (WI-30) → WalletBalanceProvider (WI-31) → GasEstimator (WI-29) → ClaudeClient.evaluate()
```

Rationale: DB read (cheapest) → 2 parallel RPC reads → 1 RPC read + arithmetic → LLM API. Each gate short-circuits before the next more expensive operation.

WI-31 inserts AFTER the existing WI-30 block and BEFORE the existing WI-29 block. Do NOT reorder any existing gates.

---

## CRITICAL INVARIANT: Exit Path Independence

The balance gate runs ONLY in `_execution_consumer_loop()`. The `_exit_scan_loop()` is NEVER touched by WI-31. An underfunded wallet can always close open positions.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code. No implementation code can be written until the failing tests are committed and verified.

---

## Phase 1: Test Suite (RED Phase)

Create two new test files. All tests MUST fail (RED) before any production code is modified.

### Step 1.1 — Create `tests/unit/test_wi31_live_balances.py`

Write unit tests covering the following behaviors. Use `unittest.mock.AsyncMock` and `pytest-asyncio` throughout.

**A. `WalletBalanceProvider.get_matic_balance_wei()` — JSON-RPC parsing:**

1. **Successful response:** Mock httpx `post()` to return `{"jsonrpc":"2.0","result":"0x1bc16d674ec80000"}` — assert returns `Decimal("2000000000000000000")` (2 MATIC in WEI).
2. **Zero balance:** Mock response `{"result": "0x0"}` — assert returns `Decimal("0")`.
3. **Return type:** assert `isinstance(result, Decimal)`, NOT `int`, NOT `float`.
4. **Payload verification:** assert `"eth_getBalance"` is in the posted JSON payload `"method"` field.
5. **Address in payload:** assert `config.wallet_address` appears in the `"params"` list.

**B. `WalletBalanceProvider.get_usdc_balance_usdc()` — ERC-20 eth_call parsing:**

6. **Successful response (100 USDC):** Mock `eth_call` response with hex-encoded 100,000,000 (100 USDC, 6 decimals) — assert returns `Decimal("100")`.
7. **Zero balance:** Mock response with 32 zero bytes — assert returns `Decimal("0")`.
8. **Return type:** assert `isinstance(result, Decimal)`.
9. **USDC contract in payload:** assert `"0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"` is in the `"to"` field of the eth_call params (case-insensitive check).
10. **balanceOf selector in payload:** assert `"70a08231"` appears in the `"data"` field of the eth_call params.
11. **Address zero-padding:** construct a wallet address, call `get_usdc_balance_usdc()`, assert the `"data"` field contains the wallet's hex address zero-padded to 64 chars (without `0x` prefix, lowercased).

**C. `WalletBalanceProvider.check_balances()` — gate logic:**

12. **Both above thresholds:** Mock both RPC calls returning sufficient balances — assert `check_passed=True`, `fallback_used=False`, `matic_sufficient=True`, `usdc_sufficient=True`.
13. **MATIC insufficient:** Mock MATIC below `min_matic_balance_wei`, USDC above minimum — assert `check_passed=False`, `matic_sufficient=False`, `usdc_sufficient=True`.
14. **USDC insufficient:** Mock USDC below `min_usdc_balance_usdc`, MATIC above minimum — assert `check_passed=False`, `matic_sufficient=True`, `usdc_sufficient=False`.
15. **Both insufficient:** Mock both below thresholds — assert `check_passed=False`, both `*_sufficient=False`.
16. **At MATIC threshold exactly (equal):** `matic_wei == min_matic_balance_wei` exactly — assert `matic_sufficient=True` (`>=` semantics, at-limit is allowed).
17. **At USDC threshold exactly (equal):** `usdc_usdc == min_usdc_balance_usdc` exactly — assert `usdc_sufficient=True`.
18. **httpx.TimeoutException on MATIC call:** Mock `asyncio.gather` to raise — assert `check_passed=True`, `fallback_used=True`.
19. **httpx.HTTPStatusError on USDC call:** Same as above — assert `check_passed=True`, `fallback_used=True`.
20. **Any generic Exception:** assert `check_passed=True`, `fallback_used=True` (all exceptions caught by outer `except Exception`).
21. **fallback_used=True always means check_passed=True:** Parameterize test with multiple exception types; assert this invariant holds in all cases.
22. **Return type:** assert return is always `BalanceCheckResult` (both pass and fail paths).

**D. Dry-run behavior:**

23. **`dry_run=True` → mock result:** assert `is_mock=True`, `check_passed=True`, `fallback_used=False`.
24. **`dry_run=True` → no httpx calls:** assert the mock httpx client's `post()` was never called.
25. **Mock balances are 10x minimums:** assert `matic_balance_wei == min_matic_balance_wei * 10` in mock result.
26. **Mock USDC is 10x minimum:** assert `usdc_balance_usdc == min_usdc_balance_usdc * 10`.

**E. `BalanceCheckResult` schema:**

27. **Frozen:** construct a valid result, then assert `AttributeError` when trying to mutate any field.
28. **Rejects float — matic_balance_wei:** assert `ValidationError` when `matic_balance_wei=0.5` (float).
29. **Rejects float — usdc_balance_usdc:** assert `ValidationError` when `usdc_balance_usdc=10.0` (float).
30. **String coercion works:** assert `BalanceCheckResult(matic_balance_wei="1000000000000000000", ...)` constructs without error.

**F. Orchestrator gate wiring:**

31. **`enable_wallet_balance_check=False`:** assert `_wallet_balance_provider` is `None`; consumer loop routes directly to WI-29 gate without calling `check_balances()`.
32. **`enable_wallet_balance_check=True`, gate passes:** mock `check_balances()` returns `BalanceCheckResult(check_passed=True)` — assert `ClaudeClient.evaluate()` is called.
33. **`enable_wallet_balance_check=True`, gate fails:** mock `check_balances()` returns `BalanceCheckResult(check_passed=False, fallback_used=False)` — assert `ClaudeClient.evaluate()` NOT called; result is `ExecutionResult(action=Action.SKIP, reason="insufficient_wallet_balance")`.
34. **Fallback — gate passes despite insufficient-looking result:** mock `check_balances()` returns `BalanceCheckResult(check_passed=True, fallback_used=True)` — assert `ClaudeClient.evaluate()` IS called (fallback does not block).
35. **Gate order — WI-30 before WI-31:** enable both; mock WI-30 `validate_entry()` to return `(False, summary)` — assert `check_balances()` is NOT called (WI-30 SKIP short-circuits before WI-31).
36. **Gate order — WI-31 before WI-29:** enable both; mock WI-31 to fail (`check_passed=False`) — assert GasEstimator is NOT called (WI-31 SKIP short-circuits before WI-29).
37. **Exit path not gated:** assert `_wallet_balance_provider.check_balances()` is never called from `_exit_scan_loop()`.

### Step 1.2 — Create `tests/integration/test_wi31_live_balances_integration.py`

Write integration tests covering end-to-end balance gate behavior. Use `respx` (httpx mock library) or `unittest.mock.patch` on `httpx.AsyncClient.post` for RPC response injection.

1. **Full pass path:** Mock both RPC calls to return 2 MATIC and 500 USDC; configure `min_matic_balance_wei=Decimal("100000000000000000")`, `min_usdc_balance_usdc=Decimal("10")` — assert `check_balances()` returns `check_passed=True`, `fallback_used=False`.
2. **MATIC insufficient — SKIP emitted:** Mock MATIC at 0.05 MATIC in WEI (`50000000000000000`), USDC at 500 — assert `check_passed=False`, Orchestrator consumer loop emits `ExecutionResult(SKIP, "insufficient_wallet_balance")`.
3. **USDC insufficient — SKIP emitted:** Mock USDC at 1.0 USDC, MATIC sufficient — assert `check_passed=False`, SKIP emitted.
4. **Fallback — RPC timeout — evaluation proceeds:** Patch httpx to raise `httpx.TimeoutException` — assert `check_passed=True`, `fallback_used=True`, `ClaudeClient.evaluate()` called.
5. **Fallback — HTTP 503 error — evaluation proceeds:** Patch httpx to raise `httpx.HTTPStatusError` with status 503 — assert fallback, evaluation proceeds.
6. **`dry_run=True` full pipeline:** `dry_run=True`; assert no httpx calls; `is_mock=True`; gate passes; `ClaudeClient.evaluate()` called.
7. **WI-30 + WI-31 together — WI-30 fires first:** Enable both; configure over-exposure that triggers WI-30 SKIP; assert `check_balances()` never called, WI-30 `exposure.limit_exceeded` logged.
8. **WI-29 + WI-31 together — WI-31 fires before WI-29:** Enable both; mock WI-31 to fail; assert GasEstimator `estimate_gas_price_wei()` never called; SKIP reason is `"insufficient_wallet_balance"`.
9. **WI-29 + WI-31 together — WI-31 passes, WI-29 fails:** Enable both; mock sufficient balances; mock gas gate to fail; assert SKIP reason is from WI-29 (not WI-31).
10. **Exit path independence:** Configure below-threshold balances; call `_exit_scan_loop()` via mock; assert exit proceeds and `PnLCalculator.settle()` is called normally.

### Step 1.3 — Run RED gate

```bash
.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi31_live_balances.py tests/integration/test_wi31_live_balances_integration.py -v
```

**All new tests MUST fail.** Commit the failing test suite:

```
git add tests/unit/test_wi31_live_balances.py tests/integration/test_wi31_live_balances_integration.py
git commit -m "test(wi31): add RED test suite for live wallet balance gate and WalletBalanceProvider"
```

---

## Phase 2: Implementation (GREEN Phase)

Implement production code to make all RED tests pass. Execute steps in order.

### Step 2.1 — Add Config Fields

In `src/core/config.py`, add three new fields to `AppConfig`. Place alongside the WI-29/WI-30 execution-layer config fields:

```python
enable_wallet_balance_check: bool = Field(
    default=False,
    description="Enable live wallet balance gate in execution consumer loop (WI-31)",
)
min_matic_balance_wei: Decimal = Field(
    default=Decimal("100000000000000000"),
    description="Minimum MATIC balance in WEI for evaluation gate (default: 0.1 MATIC)",
)
min_usdc_balance_usdc: Decimal = Field(
    default=Decimal("10"),
    description="Minimum USDC balance (human-readable) for evaluation gate (default: 10 USDC)",
)
```

Add `"min_matic_balance_wei"` and `"min_usdc_balance_usdc"` to the existing float-rejection `@field_validator` in `AppConfig`. Both must reject `float`.

**Verify:** `polygon_rpc_url` and `wallet_address` already exist in `AppConfig` — do NOT add them again.

### Step 2.2 — Add `BalanceCheckResult` to `src/schemas/web3.py`

Add `BalanceCheckResult` as a new frozen Pydantic model. Place after the existing `GasPrice` class. Do not modify `GasPrice` or any existing classes.

```python
class BalanceCheckResult(BaseModel):
    """
    Point-in-time snapshot of wallet balance state for WI-31 gate evaluation.
    Logged via structlog on every WalletBalanceProvider.check_balances() call.
    All Decimal fields coerced via Decimal(str(value)) — no float accepted.
    """
    model_config = ConfigDict(frozen=True)

    wallet_address: str
    matic_balance_wei: Decimal        # raw WEI from eth_getBalance
    matic_balance_matic: Decimal      # matic_balance_wei / 10^18
    usdc_balance_usdc: Decimal        # raw uint256 / 10^6 from balanceOf
    min_matic_balance_wei: Decimal    # configured threshold
    min_usdc_balance_usdc: Decimal    # configured threshold
    matic_sufficient: bool
    usdc_sufficient: bool
    check_passed: bool                 # True iff both sufficient OR fallback_used
    fallback_used: bool                # True iff RPC error/timeout triggered fallback
    is_mock: bool                      # True in dry_run mode
    checked_at_utc: datetime

    @field_validator(
        "matic_balance_wei",
        "matic_balance_matic",
        "usdc_balance_usdc",
        "min_matic_balance_wei",
        "min_usdc_balance_usdc",
        mode="before",
    )
    @classmethod
    def _reject_float_balance_fields(cls, v: Any) -> Decimal:
        if isinstance(v, float):
            raise ValueError("Float balance values are forbidden; use Decimal(str(value))")
        return Decimal(str(v))
```

Add the necessary imports at the top of `src/schemas/web3.py`: `from datetime import datetime`, `from decimal import Decimal`, `from typing import Any`, and `from pydantic import ConfigDict` if not already present.

### Step 2.3 — Create `WalletBalanceProvider`

Create `src/agents/execution/wallet_balance_provider.py`. Use `gas_estimator.py` as the primary structural template for the httpx pattern.

```python
"""
src/agents/execution/wallet_balance_provider.py

WI-31 Live Wallet Balance Gate — async pre-evaluation fund availability check.

Checks on-chain MATIC (eth_getBalance) and USDC (eth_call balanceOf) balances
before any LLM evaluation is attempted.

Fail-open: RPC timeouts or errors return a fallback result (check_passed=True).
Only confirmed insufficient balances cause an Orchestrator SKIP.
All arithmetic is Decimal-only. Strictly async — httpx only, no web3.py.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
import structlog

from src.core.config import AppConfig
from src.schemas.web3 import BalanceCheckResult

log = structlog.get_logger(__name__)

# Polygon native USDC contract (same address as bankroll_sync.POLYGON_USDC_PROXY)
_POLYGON_USDC_PROXY = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

# balanceOf(address) function selector: keccak256("balanceOf(address)")[:4]
_BALANCE_OF_SELECTOR = "70a08231"

_WEI_PER_MATIC = Decimal("1000000000000000000")  # 10^18
_USDC_SCALE    = Decimal("1000000")               # 10^6 — USDC has 6 decimals

_REQUEST_TIMEOUT_SECONDS = 3.0


class WalletBalanceProvider:
    """
    Async pre-evaluation gate: checks on-chain MATIC and USDC balances
    before any LLM evaluation is attempted.

    Fail-open: any RPC error returns a fallback result (check_passed=True).
    Read-only: zero on-chain state mutations under any code path.
    """

    def __init__(
        self,
        config: AppConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = http_client
        self.log = log.bind(component="WalletBalanceProvider")
```

**Key implementation notes:**

- When `http_client` is `None`, create a new `httpx.AsyncClient` per call (same pattern as `GasEstimator`).
- `get_matic_balance_wei()` posts `eth_getBalance` JSON-RPC with `params: [address, "latest"]`.
- `get_usdc_balance_usdc()` posts `eth_call` JSON-RPC with:
  - `"to"`: `_POLYGON_USDC_PROXY`
  - `"data"`: `f"0x{_BALANCE_OF_SELECTOR}{address[2:].lower().zfill(64)}"`
- `check_balances()` calls both methods via `asyncio.gather`. Any exception in `gather` is caught by a bare `except Exception` and triggers `_build_fallback_result()`.
- Both `_build_fallback_result()` and `_build_mock_result()` return `BalanceCheckResult` with `check_passed=True`.
- The mock result sets `matic_balance_wei = min_matic_balance_wei * Decimal("10")` and `usdc_balance_usdc = min_usdc_balance_usdc * Decimal("10")`.

**ABI encoding for `balanceOf`:**

```python
padded_address = address[2:].lower().zfill(64)
call_data = f"0x{_BALANCE_OF_SELECTOR}{padded_address}"
```

This produces the correct ABI-encoded `balanceOf(address)` call. The selector is 4 bytes (8 hex chars), and the address is zero-padded to 32 bytes (64 hex chars) per the Ethereum ABI spec.

**Hex response parsing:**

```python
# Both eth_getBalance and eth_call return a hex string in "result":
hex_str = response.json()["result"]
raw_int = int(str(hex_str), 16)
value = Decimal(str(raw_int))
```

Never cast `raw_int` to `float`. Go directly from `int` → `str` → `Decimal`.

### Step 2.4 — Wire into Orchestrator

In `src/orchestrator.py`, make the following changes:

**Imports (add to existing imports):**
```python
from src.agents.execution.wallet_balance_provider import WalletBalanceProvider
from src.schemas.web3 import BalanceCheckResult
```

**`__init__()` — conditional construction (after WI-30 ExposureValidator init):**
```python
if self.config.enable_wallet_balance_check:
    self._wallet_balance_provider: WalletBalanceProvider | None = WalletBalanceProvider(
        config=self.config,
        http_client=self._httpx_client,
    )
else:
    self._wallet_balance_provider = None
```

**`_execution_consumer_loop()` — balance gate AFTER WI-30 and BEFORE WI-29:**

Locate the existing WI-30 block (ExposureValidator gate). Insert the WI-31 block IMMEDIATELY AFTER it and BEFORE the WI-29 gas gate block:

```python
# WI-31: Live wallet balance gate (fires after WI-30, before WI-29)
if self.config.enable_wallet_balance_check and self._wallet_balance_provider:
    balance_result = await self._wallet_balance_provider.check_balances()
    self.log.info("wallet.balance_checked", **balance_result.model_dump())
    if not balance_result.check_passed:
        self.log.warning(
            "wallet.balance_insufficient",
            condition_id=str(item.condition_id),
            matic_balance_wei=str(balance_result.matic_balance_wei),
            usdc_balance_usdc=str(balance_result.usdc_balance_usdc),
            matic_sufficient=balance_result.matic_sufficient,
            usdc_sufficient=balance_result.usdc_sufficient,
        )
        result = ExecutionResult(action=Action.SKIP, reason="insufficient_wallet_balance")
        await self._handle_execution_result(result, item)
        continue

# WI-29: Pre-evaluation gas cost gate
if self.config.gas_check_enabled and self._gas_estimator and self._matic_price_provider:
    ...  # existing WI-29 code unchanged
```

**Do NOT touch `_exit_scan_loop()`.**

### Step 2.5 — Run GREEN gate

```bash
.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi31_live_balances.py tests/integration/test_wi31_live_balances_integration.py -v
```

**All new WI-31 tests MUST pass.** Commit the implementation:

```
git add src/agents/execution/wallet_balance_provider.py src/schemas/web3.py src/core/config.py src/orchestrator.py
git commit -m "feat(wi31): implement WalletBalanceProvider and live wallet balance gate"
```

---

## Phase 3: Refactor & Regression

### Step 3.1 — Full regression

```bash
.venv/bin/pytest --asyncio-mode=auto tests/ -q
```

**ALL tests must pass** (target: 644+ existing tests + new WI-31 tests). Fix any regressions before proceeding. Do not suppress or skip pre-existing tests.

The most likely regression sources:
- Existing `test_orchestrator.py` tests that construct `Orchestrator` without mocking the new `WalletBalanceProvider` init path — patch `enable_wallet_balance_check=False` in their config fixtures.
- Existing WI-29 and WI-30 tests that check gate order assertions — update mock call order if WI-31 is now between them and the WI-29 mock call is no longer the second gate invocation.
- Existing `test_pipeline_e2e.py` tests that construct `Orchestrator` — ensure `enable_wallet_balance_check=False` in test configs.

### Step 3.2 — Coverage verification

```bash
.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m
```

Coverage MUST remain at or above **94%**. If coverage drops, add targeted tests for uncovered lines (particularly the `_build_fallback_result()` exception handling and the `_build_mock_result()` dry-run path) before proceeding.

### Step 3.3 — Regression commit

If any fixes were needed in Phase 3, commit them atomically:

```
git commit -m "fix(wi31): address regression findings from full test suite"
```

---

## Regression Gate Summary

| Gate | Command | Pass Criteria |
|---|---|---|
| RED | `.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi31_live_balances.py tests/integration/test_wi31_live_balances_integration.py -v` | All new tests FAIL |
| GREEN | Same command | All new tests PASS |
| Regression | `.venv/bin/pytest --asyncio-mode=auto tests/ -q` | ALL tests pass (644+ existing + WI-31 additions) |
| Coverage | `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` | >= 94% |

---

## Definition of Done

Before declaring WI-31 complete:

1. All new WI-31 unit and integration tests pass GREEN.
2. Full regression suite passes with zero failures (644+ existing tests intact).
3. Coverage >= 94%.
4. `STATE.md` updated: test count, coverage, WI-31 marked complete.
5. `CLAUDE.md` updated: active WI status.
6. Memory Consolidation executed per CLAUDE.md DoD (update STATE.md, document invariants, print summary).

---

## Files Modified (Summary)

| File | Change |
|---|---|
| `src/agents/execution/wallet_balance_provider.py` | **NEW** — `WalletBalanceProvider` with `check_balances()`, `get_matic_balance_wei()`, `get_usdc_balance_usdc()` |
| `src/schemas/web3.py` | Add `BalanceCheckResult` frozen Pydantic model |
| `src/core/config.py` | Add `enable_wallet_balance_check`, `min_matic_balance_wei`, `min_usdc_balance_usdc` |
| `src/orchestrator.py` | Wire balance gate into `_execution_consumer_loop()` AFTER WI-30 and BEFORE WI-29 |
| `tests/unit/test_wi31_live_balances.py` | **NEW** — ~37 unit tests |
| `tests/integration/test_wi31_live_balances_integration.py` | **NEW** — ~10 integration tests |

## Files NOT Modified

| File | Reason |
|---|---|
| `src/agents/execution/bankroll_sync.py` | WI-31 is independent — no shared state, no imports |
| `src/agents/execution/gas_estimator.py` | WI-29 gate unchanged — WI-31 fires before it |
| `src/agents/execution/exposure_validator.py` | WI-30 gate unchanged — WI-31 fires after it |
| `src/agents/evaluation/claude_client.py` | Evaluation unchanged — balance gate precedes it |
| `src/agents/execution/pnl_calculator.py` | Settlement logic unchanged |
| `src/agents/execution/circuit_breaker.py` | Entry gate unchanged |
| `src/agents/execution/alert_engine.py` | Alert thresholds unchanged |
| `src/agents/execution/position_tracker.py` | Position tracking unchanged |
| `src/agents/execution/execution_router.py` | BUY routing unchanged |
| `src/agents/execution/exit_order_router.py` | SELL routing unchanged |
| `src/schemas/execution.py` | `ExecutionResult` and `Action.SKIP` already exist — no changes |
| `src/schemas/risk.py` | `ExposureSummary` already exists — no changes |
| `src/schemas/llm.py` | `MarketCategory` enum unchanged |
| `src/schemas/position.py` | Position schemas unchanged |
| `src/db/models.py` | Zero DB schema changes |
| `src/db/repositories/position_repository.py` | Repository unchanged |
| `migrations/` | Zero migrations — `WalletBalanceProvider` writes nothing to DB |
| `src/agents/context/aggregator.py` | DataAggregator unchanged |
| `src/agents/ingestion/ws_client.py` | WebSocket client unchanged |
| `src/agents/context/prompt_factory.py` | Prompt strategies unchanged |
