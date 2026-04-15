# WI-31 Business Logic — Live Wallet Balance Checks

## Active Agents + Constraints

- `.agents/rules/risk-auditor.md` — All balance arithmetic is `Decimal`-only. No `float` in wei-to-MATIC conversion, USDC raw-to-decimal conversion, threshold comparisons, or balance margin calculations. Schema validators on any monetary field must reject `float` and coerce via `Decimal(str(value))`.
- `.agents/rules/async-architect.md` — `WalletBalanceProvider` is **fully async** — all RPC calls use `httpx.AsyncClient` with a hard timeout. No `asyncio.run()`, no thread executor wrappers, no `asyncio.wait_for` on top of sync web3 calls. The provider is a pure async component that slots into the `_execution_consumer_loop()` await chain.
- `.agents/rules/security-auditor.md` — `dry_run=True` must return mock balances that satisfy the configured thresholds. No live RPC calls under dry-run. `WalletBalanceProvider` has zero write access to any chain state — read-only eth_getBalance and eth_call only.
- `.agents/rules/test-engineer.md` — WI-31 requires unit + integration coverage for MATIC RPC path, USDC RPC path, timeout fallback path, threshold evaluation, dry-run mock path, Orchestrator SKIP wiring, and Orchestrator PASS wiring.

## 1. Objective

Introduce `WalletBalanceProvider`, an async pre-evaluation gate that queries the operator's Polygon wallet for live MATIC and USDC balances before invoking the Evaluation Node. If the live balances fall below configured minimum thresholds, the Orchestrator skips the evaluation for that queue item.

Today, the Orchestrator has no visibility into actual on-chain wallet state during the evaluation loop. `BankrollSyncProvider` (WI-18) syncs the USDC bankroll for Kelly sizing but is fail-closed and uses web3.py running in a thread executor — it is not designed as a per-evaluation gate. The Gatekeeper (WI-29 gas gate, WI-30 exposure gate) enforces economic and portfolio-level criteria but cannot detect an empty wallet.

WI-31 inserts a lightweight, fail-open RPC gate:

```
BEFORE ClaudeClient.evaluate():

  matic_wei  = eth_getBalance(wallet_address)
  usdc_units = eth_call(balanceOf(wallet_address), usdc_contract)

  IF matic_wei  < min_matic_balance_wei  AND fallback NOT used:
      SKIP with reason "insufficient_wallet_balance"
  IF usdc_units < min_usdc_balance_usdc  AND fallback NOT used:
      SKIP with reason "insufficient_wallet_balance"
  ELSE:
      proceed to GasEstimator gate (WI-29) → ClaudeClient.evaluate()
```

**Critical behavioral distinction from `BankrollSyncProvider` (WI-18):**

| Dimension | WI-18 `BankrollSyncProvider` | WI-31 `WalletBalanceProvider` |
|---|---|---|
| Purpose | Syncs bankroll for Kelly sizing | Pre-evaluation fund availability gate |
| Transport | web3.py + `run_in_executor` (sync) | httpx JSON-RPC (async) |
| Failure mode | Fail-closed: raises `BalanceFetchError` | Fail-open: returns fallback result, gate passes |
| Checks | USDC only | MATIC + USDC |
| Gate location | Orchestrator `__init__` / background task | `_execution_consumer_loop()` before Claude |
| Write access | None | None |

WI-31 does NOT replace or modify `BankrollSyncProvider`. They are independent components serving different purposes.

## 2. Scope Boundaries

### In Scope

1. New `WalletBalanceProvider` class in `src/agents/execution/wallet_balance_provider.py`.
2. Three public interface methods:
   - `async check_balances() -> BalanceCheckResult` — orchestrates both RPC calls, evaluates thresholds, returns typed result
   - `async get_matic_balance_wei(address: str) -> Decimal` — calls `eth_getBalance` via httpx JSON-RPC
   - `async get_usdc_balance_usdc(address: str) -> Decimal` — calls `eth_call` with `balanceOf(address)` on USDC contract via httpx JSON-RPC
3. New `BalanceCheckResult` frozen Pydantic schema in `src/schemas/web3.py` (alongside existing `GasPrice`).
4. Two new `AppConfig` fields in `src/core/config.py`:
   - `enable_wallet_balance_check: bool` (default `False`)
   - `min_matic_balance_wei: Decimal` (default `Decimal("100000000000000000")` — 0.1 MATIC)
   - `min_usdc_balance_usdc: Decimal` (default `Decimal("10")` — 10 USDC)
5. Orchestrator integration: `WalletBalanceProvider` constructed conditionally in `Orchestrator.__init__()`; `check_balances()` wired into `_execution_consumer_loop()` AFTER ExposureValidator (WI-30) and BEFORE GasEstimator (WI-29) and `ClaudeClient.evaluate()`.
6. structlog audit events: `wallet.balance_checked`, `wallet.balance_insufficient`, `wallet.balance_fallback_used`.

### Out of Scope

1. Replacement or modification of `BankrollSyncProvider` — these are independent components.
2. Native MATIC price conversion to USDC for threshold comparisons — MATIC threshold is in WEI, compared directly.
3. Multi-wallet or multi-token checks beyond MATIC + USDC.
4. Persistent caching of balance results between loop iterations — each evaluation triggers a fresh RPC call.
5. WebSocket subscription for real-time balance updates.
6. On-chain state mutations of any kind.
7. Modifications to `KellySizer`, `ExposureValidator`, `GasEstimator`, or `ClaudeClient` internals.
8. Exit Path gating — `WalletBalanceProvider` NEVER gates `_exit_scan_loop()`.

## 3. Target Components + Data Contracts

### 3.1 `WalletBalanceProvider` — `src/agents/execution/wallet_balance_provider.py`

`WalletBalanceProvider` is the canonical pre-evaluation fund availability gate for WI-31. It is a fully async class that performs two Polygon JSON-RPC calls on each invocation and evaluates results against configured minimums.

```python
class WalletBalanceProvider:
    """
    Async pre-evaluation gate: checks on-chain MATIC and USDC balances
    before any LLM evaluation is attempted.

    Fail-open by design — RPC timeouts or errors return a fallback result
    that allows the evaluation to proceed. Only confirmed insufficient
    balances cause an Orchestrator SKIP.

    Read-only: zero chain state mutations. No approvals, transfers, or
    ERC-20 allowance changes under any code path.
    """

    def __init__(
        self,
        config: AppConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        ...

    async def check_balances(self) -> BalanceCheckResult:
        """
        Query live MATIC and USDC balances; evaluate against configured thresholds.

        Returns BalanceCheckResult with check_passed=True when:
          - both balances meet or exceed configured minimums, OR
          - fallback_used=True (RPC error/timeout — fail-open)

        Returns BalanceCheckResult with check_passed=False only when:
          - at least one balance is confirmed below the minimum threshold, AND
          - fallback_used=False (live RPC data was obtained)
        """

    async def get_matic_balance_wei(self, address: str) -> Decimal:
        """
        Return wallet MATIC balance in WEI via eth_getBalance JSON-RPC.
        Returns Decimal("0") on any RPC failure (fail-open helper).
        Raises on timeout when called from check_balances — handled there.
        """

    async def get_usdc_balance_usdc(self, address: str) -> Decimal:
        """
        Return wallet USDC balance in USDC (human-readable) via eth_call
        with balanceOf(address) on the POLYGON_USDC_PROXY contract.
        Raw uint256 result is divided by 10^6 (USDC decimals).
        Returns Decimal("0") on any RPC failure (fail-open helper).
        """
```

Required behavior:

1. `get_matic_balance_wei(address)` sends `eth_getBalance` JSON-RPC to `config.polygon_rpc_url` via `httpx.AsyncClient`. The hex response is parsed and converted to `Decimal` integer WEI.
2. `get_usdc_balance_usdc(address)` sends `eth_call` JSON-RPC with `balanceOf` selector and zero-padded address to the `POLYGON_USDC_PROXY` contract. The hex response is parsed and divided by `Decimal("1000000")` (USDC has 6 decimals).
3. `check_balances()` calls both methods with a single `asyncio.gather` for minimal latency. If any call raises (`httpx.TimeoutException`, `httpx.HTTPStatusError`, or any RPC-level error), `check_balances()` catches it, logs `wallet.balance_fallback_used`, and returns a `BalanceCheckResult` with `fallback_used=True` and `check_passed=True`.
4. When both RPC calls succeed, `check_balances()` evaluates:
   - `matic_sufficient = matic_wei >= config.min_matic_balance_wei`
   - `usdc_sufficient = usdc_usdc >= config.min_usdc_balance_usdc`
   - `check_passed = matic_sufficient and usdc_sufficient`
5. In `dry_run=True` mode, both RPC calls are skipped. Mock balances are returned using `config.initial_bankroll_usdc` for USDC and a synthetic 1 MATIC for MATIC (always passes thresholds in dry-run).
6. Emit structlog events:
   - `wallet.balance_checked` — on every successful invocation with balances attached
   - `wallet.balance_insufficient` — when `check_passed=False`, with which balance is deficient
   - `wallet.balance_fallback_used` — when an RPC error causes the fail-open fallback

### 3.2 `BalanceCheckResult` — `src/schemas/web3.py`

Add `BalanceCheckResult` as a new frozen Pydantic model in the existing `src/schemas/web3.py` file (alongside `GasPrice`).

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
    min_matic_balance_wei: Decimal    # configured threshold (from AppConfig)
    min_usdc_balance_usdc: Decimal    # configured threshold (from AppConfig)
    matic_sufficient: bool
    usdc_sufficient: bool
    check_passed: bool                 # True iff both sufficient OR fallback_used
    fallback_used: bool                # True iff RPC error/timeout triggered fallback
    is_mock: bool                      # True in dry_run mode
    checked_at_utc: datetime
```

All `Decimal` fields must use the `_reject_float_financials` validator pattern consistent with `src/schemas/risk.py`. No `float` defaults. `matic_balance_matic` is derived from `matic_balance_wei` and must not accept `float`.

### 3.3 Config Changes — `src/core/config.py`

Add three new fields to `AppConfig`. Place alongside the WI-29/WI-30 execution-layer config fields:

```python
enable_wallet_balance_check: bool = Field(
    default=False,
    description="Enable live wallet balance gate in execution consumer loop",
)
min_matic_balance_wei: Decimal = Field(
    default=Decimal("100000000000000000"),
    description="Minimum MATIC balance in WEI required before evaluation (default: 0.1 MATIC)",
)
min_usdc_balance_usdc: Decimal = Field(
    default=Decimal("10"),
    description="Minimum USDC balance (human-readable) required before evaluation (default: 10 USDC)",
)
```

Both `min_matic_balance_wei` and `min_usdc_balance_usdc` must be added to any existing float-rejection `@field_validator` in `AppConfig`. These fields must never accept `float`.

**Existing fields already in scope (no changes needed):**
- `polygon_rpc_url` — used as-is for all RPC calls
- `wallet_address` — used as-is for both balance lookups
- `dry_run` — gates mock vs. live RPC path

### 3.4 Orchestrator Integration — `src/orchestrator.py`

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

**`_execution_consumer_loop()` — balance gate AFTER ExposureValidator and BEFORE GasEstimator:**

The full gate order when all three WI-29/30/31 are enabled:

```
Kelly sizing → ExposureValidator (WI-30) → WalletBalanceProvider (WI-31) → GasEstimator (WI-29) → ClaudeClient.evaluate()
```

Rationale: ExposureValidator is cheapest (local DB read) and fires first. WalletBalanceProvider (two RPC calls) fires second. GasEstimator (one RPC call + arithmetic) fires third. All three must pass before any LLM API call.

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
```

**`_exit_scan_loop()` — NOT modified:**

`WalletBalanceProvider` has zero integration with `_exit_scan_loop()`. Position exits always proceed regardless of wallet balance state.

## 4. Core Logic

### 4.1 MATIC Balance via `eth_getBalance`

```python
_WEI_PER_MATIC = Decimal("1000000000000000000")  # 10^18

async def get_matic_balance_wei(self, address: str) -> Decimal:
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getBalance",
        "params": [address, "latest"],
        "id": 1,
    }
    response = await self._client.post(
        self._config.polygon_rpc_url,
        json=payload,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    hex_balance = response.json()["result"]
    return Decimal(str(int(str(hex_balance), 16)))
```

The hex result is a string like `"0x1bc16d674ec80000"`. Converting via `int(hex_string, 16)` to Python `int` and then to `Decimal(str(...))` preserves exact precision without floating-point error.

### 4.2 USDC Balance via `eth_call` + `balanceOf`

```python
_USDC_SCALE = Decimal("1000000")  # 10^6 — USDC has 6 decimals
_BALANCE_OF_SELECTOR = "70a08231"  # keccak256("balanceOf(address)")[:4]

async def get_usdc_balance_usdc(self, address: str) -> Decimal:
    # ABI-encode the address: zero-pad to 32 bytes (64 hex chars)
    padded_address = address[2:].lower().zfill(64)
    call_data = f"0x{_BALANCE_OF_SELECTOR}{padded_address}"

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [
            {"to": POLYGON_USDC_PROXY, "data": call_data},
            "latest",
        ],
        "id": 2,
    }
    response = await self._client.post(
        self._config.polygon_rpc_url,
        json=payload,
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    hex_balance = response.json()["result"]
    raw_uint256 = Decimal(str(int(str(hex_balance), 16)))
    return raw_uint256 / _USDC_SCALE
```

`POLYGON_USDC_PROXY = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"` — the canonical native USDC contract on Polygon PoS (same constant as defined in `bankroll_sync.py`). Do NOT redefine it — import it from `bankroll_sync.py` or define a module-level constant in `wallet_balance_provider.py` pointing to the same value.

### 4.3 Parallel RPC Calls via `asyncio.gather`

Both balance lookups run in parallel to minimize latency:

```python
async def check_balances(self) -> BalanceCheckResult:
    if self._config.dry_run:
        return self._build_mock_result()

    try:
        matic_wei, usdc_usdc = await asyncio.gather(
            self.get_matic_balance_wei(self._config.wallet_address),
            self.get_usdc_balance_usdc(self._config.wallet_address),
        )
    except Exception as exc:
        self.log.warning(
            "wallet.balance_fallback_used",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return self._build_fallback_result()

    min_matic = self._config.min_matic_balance_wei
    min_usdc = self._config.min_usdc_balance_usdc
    matic_sufficient = matic_wei >= min_matic
    usdc_sufficient = usdc_usdc >= min_usdc
    check_passed = matic_sufficient and usdc_sufficient

    result = BalanceCheckResult(
        wallet_address=self._config.wallet_address,
        matic_balance_wei=matic_wei,
        matic_balance_matic=matic_wei / _WEI_PER_MATIC,
        usdc_balance_usdc=usdc_usdc,
        min_matic_balance_wei=min_matic,
        min_usdc_balance_usdc=min_usdc,
        matic_sufficient=matic_sufficient,
        usdc_sufficient=usdc_sufficient,
        check_passed=check_passed,
        fallback_used=False,
        is_mock=False,
        checked_at_utc=datetime.now(timezone.utc),
    )

    event = "wallet.balance_checked" if check_passed else "wallet.balance_insufficient"
    self.log.info(
        event,
        matic_balance_wei=str(matic_wei),
        usdc_balance_usdc=str(usdc_usdc),
        matic_sufficient=matic_sufficient,
        usdc_sufficient=usdc_sufficient,
    )
    return result
```

### 4.4 Fallback Result (Fail-Open)

When any exception occurs during the `asyncio.gather`:

```python
def _build_fallback_result(self) -> BalanceCheckResult:
    """
    Return a BalanceCheckResult with check_passed=True and fallback_used=True.
    Used when any RPC call fails — fail-open means gate passes.
    """
    min_matic = self._config.min_matic_balance_wei
    min_usdc = self._config.min_usdc_balance_usdc
    return BalanceCheckResult(
        wallet_address=self._config.wallet_address,
        matic_balance_wei=min_matic,          # treat as meeting minimum
        matic_balance_matic=min_matic / _WEI_PER_MATIC,
        usdc_balance_usdc=min_usdc,           # treat as meeting minimum
        min_matic_balance_wei=min_matic,
        min_usdc_balance_usdc=min_usdc,
        matic_sufficient=True,
        usdc_sufficient=True,
        check_passed=True,
        fallback_used=True,
        is_mock=False,
        checked_at_utc=datetime.now(timezone.utc),
    )
```

### 4.5 Mock Result (Dry-Run)

```python
def _build_mock_result(self) -> BalanceCheckResult:
    """
    Return deterministic mock balances in dry_run mode — always passes thresholds.
    No live RPC calls made. Balances reflect configured minimums * 10 (comfortable margin).
    """
    min_matic = self._config.min_matic_balance_wei
    min_usdc = self._config.min_usdc_balance_usdc
    mock_matic_wei = min_matic * Decimal("10")
    mock_usdc_usdc = min_usdc * Decimal("10")
    return BalanceCheckResult(
        wallet_address=self._config.wallet_address,
        matic_balance_wei=mock_matic_wei,
        matic_balance_matic=mock_matic_wei / _WEI_PER_MATIC,
        usdc_balance_usdc=mock_usdc_usdc,
        min_matic_balance_wei=min_matic,
        min_usdc_balance_usdc=min_usdc,
        matic_sufficient=True,
        usdc_sufficient=True,
        check_passed=True,
        fallback_used=False,
        is_mock=True,
        checked_at_utc=datetime.now(timezone.utc),
    )
```

### 4.6 Failure Modes and Fallback Behavior

| Failure Mode | Behavior |
|---|---|
| `httpx.TimeoutException` on either RPC call | Fail-open: return fallback result, `check_passed=True`, `fallback_used=True`, log `wallet.balance_fallback_used` |
| `httpx.HTTPStatusError` (non-200 from RPC node) | Fail-open: same as timeout |
| Malformed JSON response (missing `"result"` key) | `KeyError` caught by outer except: fail-open |
| RPC node returns error object `{"error": {...}}` | Parsed as hex "0x0" or exception — fail-open |
| `enable_wallet_balance_check=False` | Provider not constructed; loop routes directly to next gate |
| `dry_run=True` | Mock result returned; no RPC call made |
| `matic_balance_wei < min_matic_balance_wei` | `check_passed=False`, `fallback_used=False`, log `wallet.balance_insufficient`, SKIP emitted |
| `usdc_balance_usdc < min_usdc_balance_usdc` | `check_passed=False`, `fallback_used=False`, log `wallet.balance_insufficient`, SKIP emitted |
| Both thresholds met | `check_passed=True`, `fallback_used=False`, evaluation proceeds |

**Critical design invariant:** Only confirmed, live-data insufficient balances trigger a SKIP. Uncertainty (RPC error, timeout, node down) never blocks evaluation. This matches WI-29's GasEstimator fail-open semantics and is the correct trade-off: a transient RPC outage should not halt the bot.

## 5. RPC Payload Reference

### `eth_getBalance` — MATIC native balance

```json
{
  "jsonrpc": "2.0",
  "method": "eth_getBalance",
  "params": ["0xYourWalletAddress", "latest"],
  "id": 1
}
```

**Response:**
```json
{"jsonrpc": "2.0", "id": 1, "result": "0x1bc16d674ec80000"}
```

Parsing: `Decimal(str(int("0x1bc16d674ec80000", 16)))` → WEI as exact `Decimal`.

### `eth_call` + `balanceOf` — USDC ERC-20 balance

```json
{
  "jsonrpc": "2.0",
  "method": "eth_call",
  "params": [
    {
      "to": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
      "data": "0x70a08231000000000000000000000000YOUR_WALLET_ADDRESS_WITHOUT_0X"
    },
    "latest"
  ],
  "id": 2
}
```

**ABI encoding rule:** Strip `0x` prefix from wallet address, lowercase, zero-pad to 64 hex characters:
```python
padded_address = wallet_address[2:].lower().zfill(64)
call_data = f"0x70a08231{padded_address}"
```

**Response:**
```json
{"jsonrpc": "2.0", "id": 2, "result": "0x0000000000000000000000000000000000000000000000000000000005f5e100"}
```

Parsing: `Decimal(str(int(result, 16))) / Decimal("1000000")` → USDC as exact `Decimal`.

## 6. Invariants

1. **Strictly async — httpx JSON-RPC only**
   `WalletBalanceProvider` uses `httpx.AsyncClient` for all RPC calls. No web3.py, no `asyncio.run()`, no `run_in_executor`. The provider is a first-class async component that integrates cleanly into the Orchestrator event loop.

2. **Fail-open — RPC errors do NOT block evaluation**
   Any exception during RPC calls (`httpx.TimeoutException`, `httpx.HTTPStatusError`, `KeyError`, `ValueError`, `JSONDecodeError`) is caught at the `check_balances()` level. The fallback result has `check_passed=True` and `fallback_used=True`. The Orchestrator always proceeds when `fallback_used=True`.

3. **Only confirmed insufficient balances cause SKIP**
   `ExecutionResult(action=SKIP, reason="insufficient_wallet_balance")` is emitted ONLY when `check_passed=False AND fallback_used=False`. Uncertainty is never punished.

4. **Strict `Decimal` math only**
   Every balance computation — hex-to-int conversion, WEI-to-MATIC division, uint256-to-USDC division, threshold comparison — uses `Decimal`. `float` anywhere in this path is a bug. `int(hex_str, 16)` is used for hex parsing and immediately coerced to `Decimal(str(...))`.

5. **Read-only — zero chain state mutations**
   `WalletBalanceProvider` calls `eth_getBalance` (view) and `eth_call` with a read-only function selector. No `eth_sendTransaction`, no approvals, no allowance changes. Read-only invariant is non-negotiable.

6. **Dry-run returns mock without RPC call**
   When `config.dry_run=True`, `check_balances()` immediately returns a mock result with `is_mock=True`, `check_passed=True`, and both balances set to `10x` the configured minimum. No live Polygon RPC call is made in dry-run mode under any circumstance.

7. **Config-gated construction**
   `WalletBalanceProvider` is only constructed when `enable_wallet_balance_check=True`. When disabled (default), `_execution_consumer_loop()` routes directly to the next gate as before — zero behavior change.

8. **Gate order is fixed — WI-30 before WI-31 before WI-29**
   When all three gates are enabled:
   ```
   Kelly → ExposureValidator (WI-30) → WalletBalanceProvider (WI-31) → GasEstimator (WI-29) → Claude
   ```
   Rationale: DB read (cheapest) fires first. Two parallel RPC calls (WI-31) fire second. One RPC call + arithmetic (WI-29) fires third. All must pass before any LLM API call.

9. **Exit Path is never gated**
   `_exit_scan_loop()` has zero integration with `WalletBalanceProvider`. Position exits proceed unconditionally regardless of wallet balance state. An underfunded wallet can always close open positions.

10. **Parallel RPC calls via `asyncio.gather`**
    `get_matic_balance_wei()` and `get_usdc_balance_usdc()` run concurrently. Total latency is bounded by the slower of the two calls, not their sum. Both calls share the same timeout budget.

11. **`BalanceCheckResult` logged on every cycle**
    Whether the check passes or fails, a full `BalanceCheckResult` is emitted to structlog. This provides an audit trail of wallet state at every evaluation decision point.

12. **Does NOT replace `BankrollSyncProvider`**
    `WalletBalanceProvider` and `BankrollSyncProvider` are independent. `BankrollSyncProvider` (WI-18) manages Kelly sizing bankroll sync — it is fail-closed and targets USDC only. `WalletBalanceProvider` (WI-31) is a pre-evaluation gate that is fail-open and checks both MATIC and USDC. No imports between the two. No shared state.

13. **Zero imports from prompt, context, evaluation, or ingestion modules**
    `WalletBalanceProvider` imports from `src/core/config`, `src/schemas/web3`, `httpx`, `asyncio`, `structlog`, and `decimal` only. It has no dependency on `PromptFactory`, `DataAggregator`, `ClaudeClient`, `CLOBWebSocketClient`, `ExposureValidator`, or `GasEstimator`.

14. **`min_matic_balance_wei` and `min_usdc_balance_usdc` reject `float`**
    Both `AppConfig` fields are `Decimal` with `@field_validator` float rejection. Threshold comparisons are `Decimal >= Decimal` — no coercion to `float` at any step.

## 7. Acceptance Criteria

1. `WalletBalanceProvider` exists in `src/agents/execution/wallet_balance_provider.py` with three public methods: `check_balances() -> BalanceCheckResult`, `get_matic_balance_wei(address) -> Decimal`, `get_usdc_balance_usdc(address) -> Decimal`.
2. `BalanceCheckResult` exists in `src/schemas/web3.py` as a frozen Pydantic model with all fields defined in Section 3.2.
3. `get_matic_balance_wei()` sends `eth_getBalance` JSON-RPC and parses hex result as `Decimal` WEI.
4. `get_usdc_balance_usdc()` sends `eth_call` with `balanceOf` selector + zero-padded address to `POLYGON_USDC_PROXY` and divides result by `Decimal("1000000")`.
5. `check_balances()` uses `asyncio.gather` to run both RPC calls concurrently.
6. `check_balances()` returns `BalanceCheckResult(check_passed=True, fallback_used=True)` on any exception.
7. `check_balances()` returns `BalanceCheckResult(check_passed=False)` when live data confirms MATIC below threshold.
8. `check_balances()` returns `BalanceCheckResult(check_passed=False)` when live data confirms USDC below threshold.
9. When `check_passed=False`, `_execution_consumer_loop()` emits `ExecutionResult(action=SKIP, reason="insufficient_wallet_balance")`.
10. `_exit_scan_loop()` is NOT gated — exits proceed unconditionally.
11. `BalanceCheckResult` is logged via structlog on each invocation.
12. `dry_run=True` returns mock result with `is_mock=True` and `check_passed=True` — no live RPC call.
13. `AppConfig.enable_wallet_balance_check` is `bool` with default `False`.
14. `AppConfig.min_matic_balance_wei` is `Decimal` with default `Decimal("100000000000000000")`.
15. `AppConfig.min_usdc_balance_usdc` is `Decimal` with default `Decimal("10")`.
16. `WalletBalanceProvider` is constructed in `Orchestrator.__init__()` only when `enable_wallet_balance_check=True`.
17. All balance math is `Decimal`-only — no `float` at any computation step.
18. `WalletBalanceProvider` contains no imports from prompt, context, evaluation, or ingestion modules.
19. `WalletBalanceProvider` performs zero on-chain state mutations.
20. Full regression remains green with coverage >= 94%.

## 8. Test Plan

### Unit Tests

**A. `WalletBalanceProvider.get_matic_balance_wei()` — RPC parsing:**
1. Mock httpx response with `{"result": "0x1bc16d674ec80000"}` — assert returns `Decimal("2000000000000000000")` (2 MATIC).
2. Mock httpx response with `{"result": "0x0"}` — assert returns `Decimal("0")`.
3. Assert return type is `Decimal` (not `int`, not `float`).
4. Mock httpx response that raises `httpx.TimeoutException` — verify exception propagates (caught by `check_balances`, not here).

**B. `WalletBalanceProvider.get_usdc_balance_usdc()` — ERC-20 parsing:**
5. Mock `eth_call` response with 100 USDC (raw: `0x5f5e100 * 10` = `0x5F5E1000`) — assert returns `Decimal("100")`.
6. Mock response with `{"result": "0x0000...0000"}` — assert returns `Decimal("0")`.
7. Assert return type is `Decimal`.
8. Verify `POLYGON_USDC_PROXY` is in the `eth_call` payload `"to"` field.
9. Verify `balanceOf` selector `"70a08231"` is in the `eth_call` payload `"data"` field.
10. Verify address zero-padding: wallet `"0xDeAdBeEf..."` → `"data"` contains `"deadbeef..."` padded to 64 chars.

**C. `WalletBalanceProvider.check_balances()` — gate logic:**
11. Both balances above thresholds → assert `check_passed=True`, `fallback_used=False`.
12. MATIC below threshold, USDC above → assert `check_passed=False`, `matic_sufficient=False`, `usdc_sufficient=True`.
13. USDC below threshold, MATIC above → assert `check_passed=False`, `matic_sufficient=True`, `usdc_sufficient=False`.
14. Both below threshold → assert `check_passed=False`, both sufficient flags `False`.
15. At MATIC threshold exactly (equal) → assert `matic_sufficient=True` (`>=` not `>`).
16. At USDC threshold exactly (equal) → assert `usdc_sufficient=True`.
17. httpx timeout on MATIC call → assert `check_passed=True`, `fallback_used=True`.
18. httpx timeout on USDC call → assert `check_passed=True`, `fallback_used=True`.
19. `asyncio.gather` exception on one leg → assert fallback result (not partial result).
20. `fallback_used=True` → assert `check_passed` is always `True` regardless of threshold values.
21. Return type is always `BalanceCheckResult`.

**D. Dry-run behavior:**
22. `dry_run=True` → assert `is_mock=True`, `check_passed=True`, `fallback_used=False`.
23. `dry_run=True` → assert no httpx calls made (mock client not called).
24. Mock MATIC and USDC are `10x` the configured minimums.

**E. `BalanceCheckResult` schema:**
25. Frozen: assert cannot mutate after construction.
26. Rejects `float`: `ValidationError` when `matic_balance_wei=0.5` (float).
27. `Decimal` fields coerce from string: `matic_balance_wei="1000000000000000000"` works.

**F. Orchestrator gate wiring:**
28. `enable_wallet_balance_check=False` → `_wallet_balance_provider` is `None`, loop routes directly to WI-29.
29. `enable_wallet_balance_check=True`, gate passes → `ClaudeClient.evaluate()` is called.
30. `enable_wallet_balance_check=True`, gate fails → `ClaudeClient.evaluate()` NOT called, `ExecutionResult(SKIP, "insufficient_wallet_balance")` emitted.
31. `enable_wallet_balance_check=True`, RPC timeout (fallback) → `ClaudeClient.evaluate()` IS called.
32. Gate order: WI-30 fires before WI-31 — verify mock call order when both enabled.
33. Gate order: WI-31 fires before WI-29 — verify mock call order when both enabled.
34. Exit path not gated: `_exit_scan_loop()` proceeds regardless of balance state.

### Integration Tests

1. **Full pass path:** Mock httpx client returning 2 MATIC (above threshold) and 500 USDC (above threshold) — assert `check_balances()` returns `check_passed=True`.
2. **MATIC insufficient:** Mock httpx returning 0.05 MATIC in WEI — assert `check_passed=False`, Orchestrator emits `ExecutionResult(SKIP)`.
3. **USDC insufficient:** Mock httpx returning 1 USDC — assert `check_passed=False`, Orchestrator SKIP emitted.
4. **Both insufficient:** Mock both thresholds breached — assert `check_passed=False` with both `*_sufficient=False`.
5. **Fallback — RPC timeout:** Mock httpx to raise `httpx.TimeoutException` — assert `check_passed=True`, `fallback_used=True`, Orchestrator proceeds to next gate.
6. **Fallback — HTTP error:** Mock httpx to raise `httpx.HTTPStatusError` — assert `check_passed=True`, `fallback_used=True`.
7. **Dry-run end-to-end:** `dry_run=True`; assert no httpx calls, `is_mock=True`, gate passes, evaluation proceeds.
8. **WI-29 + WI-31 together:** Both enabled; mock balance gate pass, mock gas gate fail — assert WI-31 fires before WI-29, WI-29 SKIP emitted (not WI-31 SKIP).
9. **WI-30 + WI-31 together:** Both enabled; mock WI-30 exposure fail — assert WI-30 SKIP emitted, WI-31 NOT invoked.
10. **Exit path independence:** Simulate insufficient wallet → call `_exit_scan_loop()` → assert exit proceeds, `PnLCalculator.settle()` called normally.

## 9. Non-Negotiable Design Decisions

### 9.1 Fail-Open Is Mandatory for RPC Gates

`WalletBalanceProvider` is fail-open because:
- A transient RPC outage (node overloaded, timeout, network hiccup) should not halt active market tracking.
- The alternative — blocking evaluations when the RPC node is unreachable — causes cascading timeouts on every queue item and effectively shuts down the bot.
- The risk of one missed evaluation (when balance is actually insufficient but RPC reports as fallback) is far smaller than the risk of shutting down entirely on every RPC blip.

This is the same rationale as WI-29 GasEstimator. Both gates are fail-open.

Compare to WI-30 ExposureValidator (fail-hard): portfolio exposure is computed from local DB state — DB unavailability is a hard error because we cannot safely ignore unknown exposure. RPC availability is a softer dependency.

### 9.2 httpx JSON-RPC Over web3.py

`WalletBalanceProvider` uses httpx directly rather than web3.py because:
- `httpx.AsyncClient` is already in the project's async toolkit (used by GasEstimator, TelegramNotifier, REST client).
- web3.py's provider system uses blocking sync calls; wrapping it in `run_in_executor` adds thread pool overhead and bypasses Python's event loop scheduler.
- Raw JSON-RPC calls for `eth_getBalance` and `eth_call` with `balanceOf` are simple and don't require web3.py's ABI encoding machinery (the selector and address padding are trivial string operations).
- `BankrollSyncProvider` (WI-18) uses web3.py because it was introduced before the project standardized on httpx — WI-31 does not repeat that pattern.

### 9.3 Both Balances Checked in Parallel

`asyncio.gather` for both RPC calls is mandatory:
- Checking MATIC and USDC sequentially doubles latency on the critical path.
- Both calls are independent reads with no ordering dependency.
- If either call fails, the exception propagates out of `gather`, the outer except catches it, and the fallback result is returned — no partial-state issues.

### 9.4 Gate Position — After WI-30, Before WI-29

ExposureValidator (WI-30) is the cheapest gate (local DB read, no network I/O) and fires first. WalletBalanceProvider (WI-31) makes two parallel RPC calls and fires second. GasEstimator (WI-29) makes one RPC call plus arithmetic and fires third. This ordering ensures the most expensive operation (LLM API call) is protected by three progressively more network-intensive guards.
