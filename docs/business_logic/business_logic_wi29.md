# WI-29 Business Logic — Live Fee Injection

## Active Agents + Constraints

- `.agents/rules/risk-auditor.md` — All gas and fee arithmetic is `Decimal`-only. No `float` in WEI, USDC, MATIC, EV, or buffer calculations. Schema validators on any new monetary field must reject `float` and coerce via `Decimal(str(value))`.
- `.agents/rules/async-architect.md` — `GasEstimator` is fail-open: RPC failures must NOT propagate into the caller. A failed `eth_gasPrice` call logs via structlog and returns the configured mock value (`dry_run_gas_price_wei`). The pre-evaluation gas check is an additive gate inserted BEFORE `ClaudeClient.evaluate()` — it does not alter queue topology or introduce new background tasks.
- `.agents/rules/security-auditor.md` — `dry_run=True` must use mock gas values (`dry_run_gas_price_wei`) and still execute the full gate pipeline for deterministic testing. No live RPC calls in dry-run. `GasEstimator` performs zero DB writes — it is a read-only pricing oracle.
- `.agents/rules/test-engineer.md` — WI-29 requires unit + integration coverage for RPC call success, RPC fallback behavior, USDC conversion formula, EV gate pass/fail logic, Orchestrator SKIP path, dry-run mock injection, and settlement gas cost propagation into `PnLCalculator.settle()`.

## 1. Objective

Introduce `GasEstimator` and `MaticPriceProvider` so the system can compute real-time Polygon network transaction costs in USDC and use that cost as a pre-evaluation gate before expensive LLM calls.

Today, the `Orchestrator._execution_consumer_loop()` routes every candidate trade directly to `ClaudeClient.evaluate()`. There is no check for whether the trade is economically viable after gas costs — a trade with positive EV but high gas fees could still be net-negative.

WI-29 inserts a pre-evaluation gas cost gate:

```
IF gas_cost_usdc >= expected_value_usdc * (1 + gas_ev_buffer_pct):
    SKIP trade with reason "gas_cost_exceeds_ev"
ELSE:
    proceed to ClaudeClient.evaluate()
```

This closes the loop between Layer 4 on-chain execution costs and Layer 3 evaluation decisions.

WI-29 also wires `GasEstimator` into the settlement path (`_exit_scan_loop()`) so `PnLCalculator.settle()` receives live gas costs for net PnL accounting — completing the WI-28 accounting loop with actual values instead of manual parameter injection.

## 2. Scope Boundaries

### In Scope

1. Rewrite `src/agents/execution/gas_estimator.py` — replace the Phase 5 EIP-1559/web3 stub with the WI-29 `httpx`-based `eth_gasPrice` implementation.
2. Create `src/agents/execution/matic_price_provider.py` — new lightweight async component fetching live MATIC/USDC price with static fallback.
3. Add four new `AppConfig` fields to `src/core/config.py`:
   - `gas_check_enabled: bool` (default `False`)
   - `dry_run_gas_price_wei: Decimal` (default `Decimal("30000000000")` — 30 Gwei)
   - `gas_ev_buffer_pct: Decimal` (default `Decimal("0.10")` — 10% margin)
   - `matic_usdc_price: Decimal` (default `Decimal("0.50")` — static fallback)
4. Wire `GasEstimator` into `Orchestrator._execution_consumer_loop()` BEFORE `ClaudeClient.evaluate()`.
5. Wire `GasEstimator` into `Orchestrator._exit_scan_loop()` to pass live gas cost into `PnLCalculator.settle()`.

### Out of Scope

1. Gas unit estimation per transaction type — `gas_units` defaults to `21000` (standard transfer); per-order-type calibration is deferred.
2. Dynamic gas limit calculation based on CLOB order complexity.
3. EIP-1559 fee market (base fee + priority tip) — Polygon legacy gas model uses `eth_gasPrice`.
4. Modifying `ClaudeClient`, `PromptFactory`, `LLMEvaluationResponse`, or Gatekeeper internals.
5. Gas cost injection during BUY order signing — gas is paid at on-chain settlement, not at order placement.
6. Historical gas price backfilling or caching.
7. MATIC price feed persistence or historical oracle queries.
8. New alert thresholds or drawdown policy changes based on gas-adjusted PnL.

## 3. Target Components + Data Contracts

### 3.1 Primary Target Components

#### A. `src/agents/execution/gas_estimator.py` (rewrite)

`GasEstimator` is the canonical gas pricing oracle for WI-29. It replaces the Phase 5 EIP-1559/web3 stub entirely.

```python
class GasEstimator:
    """
    Queries Polygon RPC for real-time eth_gasPrice and converts to USDC cost.

    Fail-open by design: a failed RPC call logs the error and returns the
    configured dry_run_gas_price_wei mock instead of raising.
    """

    def __init__(self, config: AppConfig) -> None:
        ...

    async def estimate_gas_price_wei(self) -> Decimal:
        """Query Polygon RPC eth_gasPrice. Returns WEI as Decimal.
        Falls back to config.dry_run_gas_price_wei on any failure."""

    def estimate_gas_cost_usdc(
        self,
        gas_units: int,
        gas_price_wei: Decimal,
        matic_usdc_price: Decimal,
    ) -> Decimal:
        """Convert gas usage into USDC cost. Synchronous, Decimal-only."""

    def pre_evaluate_gas_check(
        self,
        expected_value_usdc: Decimal,
        gas_cost_usdc: Decimal,
    ) -> bool:
        """Return True when EV exceeds gas cost by the configured buffer margin."""
```

Required behavior:

1. `estimate_gas_price_wei()` issues `httpx.AsyncClient.post()` to `config.polygon_rpc_url` with JSON-RPC payload:
   ```json
   {"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1}
   ```
2. Parse the `"result"` hex string via `int(result, 16)` then wrap in `Decimal(str(...))`.
3. On ANY exception (network error, JSON parse failure, non-200 response), log `gas.rpc_failed` via structlog and return `Decimal(str(config.dry_run_gas_price_wei))` — never raise.
4. When `config.dry_run=True`, skip the HTTP call entirely and return `Decimal(str(config.dry_run_gas_price_wei))`.
5. `estimate_gas_cost_usdc()` computes:
   ```python
   Decimal(str(gas_units)) * gas_price_wei / Decimal("1000000000000000000") * matic_usdc_price
   ```
   All operands and the result must be `Decimal`. No `float` at any step.
6. `pre_evaluate_gas_check()` returns `True` when:
   ```python
   expected_value_usdc > gas_cost_usdc * (Decimal("1") + config.gas_ev_buffer_pct)
   ```
   Returns `False` when gas cost equals or exceeds the buffered EV threshold.
7. Emit structlog events: `gas.estimated`, `gas.check_passed`, `gas.check_failed`, `gas.rpc_failed`, `gas.settlement_computed`.

#### B. `src/agents/execution/matic_price_provider.py` (new)

`MaticPriceProvider` is a lightweight fail-open async component that fetches the live MATIC/USDC price from the Gamma REST API or a configured price oracle.

```python
class MaticPriceProvider:
    """
    Fetches live MATIC/USDC price from Gamma REST API.

    Fail-open: if the live fetch fails, returns config.matic_usdc_price static value.
    """

    def __init__(self, config: AppConfig) -> None:
        ...

    async def get_matic_usdc(self) -> Decimal:
        """Return current MATIC/USDC price as Decimal.
        Falls back to config.matic_usdc_price on any failure."""
```

Required behavior:

1. Attempt live price fetch from configured endpoint.
2. On ANY exception, log `matic_price.fetch_failed` via structlog and return `Decimal(str(config.matic_usdc_price))`.
3. Returned value is always `Decimal` — never `float`.
4. When `config.dry_run=True`, skip live fetch and return `Decimal(str(config.matic_usdc_price))` directly.

### 3.2 Supporting Orchestrator Changes

WI-29 wires `GasEstimator` and `MaticPriceProvider` into two loops in `src/orchestrator.py`:

**`_execution_consumer_loop()` — pre-evaluation gas gate:**

```python
if self.config.gas_check_enabled and self._gas_estimator:
    gas_price_wei = await self._gas_estimator.estimate_gas_price_wei()
    matic_price = await self._matic_price_provider.get_matic_usdc()
    gas_cost_usdc = self._gas_estimator.estimate_gas_cost_usdc(
        gas_units=21000,
        gas_price_wei=gas_price_wei,
        matic_usdc_price=matic_price,
    )
    if not self._gas_estimator.pre_evaluate_gas_check(
        expected_value_usdc=item.expected_value_usdc,
        gas_cost_usdc=gas_cost_usdc,
    ):
        self.log.warning("gas.check_failed", gas_cost_usdc=str(gas_cost_usdc))
        # SKIP — do not call ClaudeClient.evaluate()
        result = ExecutionResult(action=Action.SKIP, reason="gas_cost_exceeds_ev")
        await self._handle_execution_result(result, item)
        continue
```

**`_exit_scan_loop()` — settlement gas cost injection:**

```python
gas_price_wei = await self._gas_estimator.estimate_gas_price_wei()
matic_price = await self._matic_price_provider.get_matic_usdc()
gas_cost_usdc = self._gas_estimator.estimate_gas_cost_usdc(
    gas_units=21000,
    gas_price_wei=gas_price_wei,
    matic_usdc_price=matic_price,
)
pnl_record = await self._pnl_calculator.settle(
    position=position,
    exit_price=exit_price,
    gas_cost_usdc=gas_cost_usdc,
)
```

**`__init__()` — conditional construction:**

```python
if self.config.gas_check_enabled:
    self._gas_estimator = GasEstimator(config=self.config)
    self._matic_price_provider = MaticPriceProvider(config=self.config)
else:
    self._gas_estimator = None
    self._matic_price_provider = None
```

### 3.3 Config Changes

In `src/core/config.py`, add four new fields to `AppConfig`:

```python
gas_check_enabled: bool = Field(
    default=False,
    description="Enable pre-evaluation gas cost gate in execution consumer loop",
)
dry_run_gas_price_wei: Decimal = Field(
    default=Decimal("30000000000"),
    description="Mock gas price (WEI) returned in dry_run mode or on RPC failure",
)
gas_ev_buffer_pct: Decimal = Field(
    default=Decimal("0.10"),
    description="Required EV margin above gas cost (10% = EV must be 1.10x gas cost)",
)
matic_usdc_price: Decimal = Field(
    default=Decimal("0.50"),
    description="Static MATIC/USDC fallback price used when MaticPriceProvider fetch fails",
)
```

All four must use `Decimal` for monetary fields. No `float` defaults.

## 4. Core Logic

### 4.1 Gas Price Fetch

```python
async def estimate_gas_price_wei(self) -> Decimal:
    if self.config.dry_run:
        return Decimal(str(self.config.dry_run_gas_price_wei))

    payload = {"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.config.polygon_rpc_url,
                json=payload,
                timeout=5.0,
            )
            response.raise_for_status()
            result_hex = response.json()["result"]
            gas_price_wei = int(result_hex, 16)
            gas_price = Decimal(str(gas_price_wei))
            self.log.info("gas.estimated", gas_price_wei=str(gas_price))
            return gas_price
    except Exception as exc:
        self.log.error("gas.rpc_failed", error=str(exc))
        return Decimal(str(self.config.dry_run_gas_price_wei))
```

### 4.2 USDC Cost Conversion Formula

```python
def estimate_gas_cost_usdc(
    self,
    gas_units: int,
    gas_price_wei: Decimal,
    matic_usdc_price: Decimal,
) -> Decimal:
    """
    gas_cost_matic = gas_units * gas_price_wei / 1e18  (WEI to MATIC)
    gas_cost_usdc  = gas_cost_matic * matic_usdc_price
    """
    _WEI_PER_MATIC = Decimal("1000000000000000000")
    gas_cost_matic = Decimal(str(gas_units)) * gas_price_wei / _WEI_PER_MATIC
    gas_cost_usdc = gas_cost_matic * matic_usdc_price
    self.log.info(
        "gas.settlement_computed",
        gas_units=gas_units,
        gas_price_wei=str(gas_price_wei),
        matic_usdc_price=str(matic_usdc_price),
        gas_cost_usdc=str(gas_cost_usdc),
    )
    return gas_cost_usdc
```

### 4.3 Pre-Evaluation EV Gate

```python
def pre_evaluate_gas_check(
    self,
    expected_value_usdc: Decimal,
    gas_cost_usdc: Decimal,
) -> bool:
    """
    True  → trade passes gate, proceed to ClaudeClient.evaluate()
    False → trade is uneconomic, SKIP with reason "gas_cost_exceeds_ev"
    """
    buffered_threshold = gas_cost_usdc * (Decimal("1") + self.config.gas_ev_buffer_pct)
    passes = expected_value_usdc > buffered_threshold
    event = "gas.check_passed" if passes else "gas.check_failed"
    self.log.info(
        event,
        expected_value_usdc=str(expected_value_usdc),
        gas_cost_usdc=str(gas_cost_usdc),
        buffered_threshold=str(buffered_threshold),
    )
    return passes
```

### 4.4 Fallback Behavior

| Failure Mode | Behavior |
|---|---|
| `eth_gasPrice` RPC timeout | Return `dry_run_gas_price_wei`, log `gas.rpc_failed` |
| `eth_gasPrice` non-200 HTTP | Return `dry_run_gas_price_wei`, log `gas.rpc_failed` |
| `eth_gasPrice` JSON parse error | Return `dry_run_gas_price_wei`, log `gas.rpc_failed` |
| MATIC price fetch fails | Return `config.matic_usdc_price`, log `matic_price.fetch_failed` |
| `dry_run=True` | Return mock values for both, skip all live HTTP calls |

**Critical:** The fallback values still flow through `pre_evaluate_gas_check()`. The gate fires on mock values in dry-run and on fallback values when RPC is degraded. The pipeline is never halted.

### 4.5 Exit Path Independence

The gas check ONLY gates the Entry Path (`_execution_consumer_loop()`). The Exit Path (`_exit_scan_loop()`) calls `estimate_gas_price_wei()` and `estimate_gas_cost_usdc()` to inject live gas cost into `PnLCalculator.settle()` for net PnL accounting — but the exit proceeds regardless of whether gas cost exceeds EV. You cannot block a position liquidation based on gas price.

## 5. Invariants

1. **Strict `Decimal` math only**
   Every gas computation step — WEI, MATIC conversion, USDC output, EV comparison, buffer multiplication — uses `Decimal`. `float` anywhere in this path is a bug.

2. **Fail-open, never raise**
   `GasEstimator.estimate_gas_price_wei()` and `MaticPriceProvider.get_matic_usdc()` must catch ALL exceptions, log via structlog, and return configured fallback values. Neither method may raise into the caller.

3. **Pre-evaluation gate fires before LLM call**
   The gas check in `_execution_consumer_loop()` runs BEFORE `ClaudeClient.evaluate()`. No LLM API call is made for a trade that fails the gas gate. This preserves API budget for viable trades.

4. **Exit Path is never gated**
   `_exit_scan_loop()` calls `GasEstimator` for settlement accounting only. Gas cost exceeding EV does NOT block an exit. The exit path is always permitted to proceed.

5. **`dry_run=True` uses mock values throughout**
   In dry-run mode, `GasEstimator` returns `dry_run_gas_price_wei` and `MaticPriceProvider` returns `matic_usdc_price`. No live HTTP calls are made. The full gate pipeline still executes for deterministic testing.

6. **Config-gated construction**
   `GasEstimator` and `MaticPriceProvider` are only constructed in `Orchestrator.__init__()` when `gas_check_enabled=True`. When `gas_check_enabled=False` (default), the execution consumer loop routes directly to `ClaudeClient.evaluate()` as before — zero behavior change.

7. **Zero DB writes**
   `GasEstimator` and `MaticPriceProvider` are read-only oracle components. They read from external HTTP endpoints and configuration only. No DB session access, no `PositionRepository` calls, no direct SQL.

8. **Zero imports from prompt/context/evaluation/ingestion modules**
   `GasEstimator` and `MaticPriceProvider` have no dependency on `PromptFactory`, `DataAggregator`, `ClaudeClient`, `CLOBWebSocketClient`, or any Layer 1/2/3 module. They are pure Layer 4 oracle utilities.

9. **Kelly sizing and Gatekeeper authority unchanged**
   WI-29 inserts a pre-Gatekeeper gas gate. The Kelly fraction (0.25 Quarter-Kelly), EV formula, LLM confidence threshold, and all Gatekeeper decision logic remain unchanged.

10. **EV buffer prevents marginal trades**
    The `gas_ev_buffer_pct` (default `0.10`) requires EV to *exceed* gas cost by 10%, not merely equal it. A trade with `EV == gas_cost` fails the gate.

## 6. Acceptance Criteria

1. `GasEstimator` exists in `src/agents/execution/gas_estimator.py` with three public methods: `estimate_gas_price_wei() -> Decimal`, `estimate_gas_cost_usdc(...) -> Decimal`, `pre_evaluate_gas_check(...) -> bool`.
2. `estimate_gas_price_wei()` uses `httpx.AsyncClient.post()` to `config.polygon_rpc_url` with `eth_gasPrice` JSON-RPC payload.
3. `estimate_gas_cost_usdc()` computes `gas_units * gas_price_wei / Decimal("1e18") * matic_usdc_price` using Decimal-only arithmetic.
4. `pre_evaluate_gas_check()` returns `True` when `expected_value_usdc > gas_cost_usdc * (1 + gas_ev_buffer_pct)`.
5. `GasEstimator` is fail-open: any exception in `estimate_gas_price_wei()` logs `gas.rpc_failed` and returns `dry_run_gas_price_wei`.
6. `MaticPriceProvider` exists in `src/agents/execution/matic_price_provider.py` with `get_matic_usdc() -> Decimal` async method.
7. `MaticPriceProvider` is fail-open: any exception logs `matic_price.fetch_failed` and returns `config.matic_usdc_price`.
8. `dry_run=True` returns mock gas/price values without making any live HTTP calls.
9. When gas check fails, `_execution_consumer_loop()` skips the item with `ExecutionResult(action=SKIP, reason="gas_cost_exceeds_ev")`.
10. `_exit_scan_loop()` passes live `gas_cost_usdc` to `PnLCalculator.settle()` for net PnL accounting.
11. `GasEstimator` is constructed in `Orchestrator.__init__()` only when `gas_check_enabled=True`.
12. All four new `AppConfig` fields are `Decimal` or `bool` — no `float` defaults.
13. `GasEstimator` has zero imports from prompt, context, evaluation, or ingestion modules.
14. `GasEstimator` performs zero DB writes.
15. Full regression remains green with coverage >= 94%.

## 7. Test Plan

### Unit Tests

1. `estimate_gas_price_wei()` parses hex RPC response correctly and returns `Decimal`.
2. `estimate_gas_price_wei()` falls back to `dry_run_gas_price_wei` on HTTP error.
3. `estimate_gas_price_wei()` falls back to `dry_run_gas_price_wei` on JSON parse error.
4. `estimate_gas_price_wei()` returns `dry_run_gas_price_wei` when `dry_run=True` without making HTTP call.
5. `estimate_gas_cost_usdc()` applies correct formula with Decimal arithmetic.
6. `estimate_gas_cost_usdc()` uses `_WEI_PER_MATIC = Decimal("1000000000000000000")` — no float division.
7. `pre_evaluate_gas_check()` returns `True` when EV exceeds buffered threshold.
8. `pre_evaluate_gas_check()` returns `False` when EV equals buffered threshold (boundary: not strictly greater).
9. `pre_evaluate_gas_check()` returns `False` when gas cost exceeds EV.
10. `MaticPriceProvider.get_matic_usdc()` returns live price when fetch succeeds.
11. `MaticPriceProvider.get_matic_usdc()` falls back to `config.matic_usdc_price` on any exception.
12. `MaticPriceProvider.get_matic_usdc()` returns `config.matic_usdc_price` when `dry_run=True`.
13. Orchestrator: `gas_check_enabled=False` — `_gas_estimator` is `None`, no gas gate in consumer loop.
14. Orchestrator: `gas_check_enabled=True` — gas gate fires BEFORE `ClaudeClient.evaluate()`.
15. Orchestrator SKIP path: `pre_evaluate_gas_check()` returns `False` → `ExecutionResult(SKIP, "gas_cost_exceeds_ev")`.

### Integration Tests

1. Full pre-evaluation gas gate: mock RPC returning known hex → verify `estimate_gas_price_wei()` → `estimate_gas_cost_usdc()` → `pre_evaluate_gas_check()` → SKIP result in consumer loop.
2. Fallback chain: mock RPC failure → fallback value flows through full gate → SKIP or PASS based on fallback value.
3. Settlement gas injection: mock exit scan → verify `gas_cost_usdc` passed to `PnLCalculator.settle()` → `net_realized_pnl` reflects gas deduction.
4. `dry_run=True` end-to-end: no live HTTP calls, mock values flow through full gate, no DB writes.
5. Exit Path independence: simulate high gas price → exit scan proceeds regardless, gas injected into settlement.

## 8. Non-Negotiable Design Decision

WI-29 is **fail-open by design**. The concurrent multi-market tracking pipeline introduced by WI-32 cannot be halted because a Polygon RPC endpoint is temporarily degraded. Therefore:

```python
# ANY exception in gas estimation → return fallback, never raise:
try:
    gas_price_wei = await self._fetch_from_rpc()
    return gas_price_wei
except Exception as exc:
    self.log.error("gas.rpc_failed", error=str(exc))
    return Decimal(str(self.config.dry_run_gas_price_wei))
```

The fallback value (`dry_run_gas_price_wei` default: `30 Gwei`) is conservative enough that:

- Most trades with genuine positive EV will still pass the gas gate.
- Marginal trades near the EV threshold will correctly fail the gate, preventing fee-negative execution.
- The system never enters an undefined state due to RPC unavailability.

This is the core business rule: **gas estimation failure degrades gracefully to a known-safe mock price — it never stops the pipeline**.
