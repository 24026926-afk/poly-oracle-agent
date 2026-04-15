# P29-WI-29 — Live Fee Injection Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi29-live-fees` (branched from current `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/risk-auditor.md`
- `.agents/rules/async-architect.md`
- `.agents/rules/security-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-29 for Phase 10: a pre-evaluation gas cost gate and live settlement cost injection that closes the loop between Polygon network fees and Layer 3 evaluation decisions.

Today, `Orchestrator._execution_consumer_loop()` routes every candidate trade directly to `ClaudeClient.evaluate()`. No check exists for whether the Polygon transaction cost makes the trade economically viable. A trade with positive EV but high gas fees could still be net-negative.

WI-29 inserts a pre-evaluation gas gate: before calling `ClaudeClient.evaluate()`, the Orchestrator queries the Polygon RPC for the current `eth_gasPrice`, converts it to a USDC cost via the MATIC/USDC exchange rate, and checks whether the trade's expected value exceeds that cost by the configured buffer margin. If not, the trade is short-circuited with `ExecutionResult(action=SKIP, reason="gas_cost_exceeds_ev")`.

WI-29 also injects live gas cost into `PnLCalculator.settle()` during `_exit_scan_loop()`, completing the WI-28 fee-aware accounting loop with actual on-chain values instead of manually-injected test parameters.

**Note on existing stub:** `src/agents/execution/gas_estimator.py` already exists from Phase 5 but has a completely different API (EIP-1559, web3, `GasPrice` schema). WI-29 replaces this stub in its entirety with the `httpx`-based `eth_gasPrice` implementation defined in this prompt. The Phase 5 schema (`GasPrice`, `GasEstimatorError`) and the `web3` dependency are not used by WI-29.

---

## Objective & Scope

### In Scope
1. Rewrite `src/agents/execution/gas_estimator.py` — three public methods: `estimate_gas_price_wei()`, `estimate_gas_cost_usdc()`, `pre_evaluate_gas_check()`.
2. Create `src/agents/execution/matic_price_provider.py` — `MaticPriceProvider` with `get_matic_usdc()` and static fallback.
3. Add four `AppConfig` fields: `gas_check_enabled`, `dry_run_gas_price_wei`, `gas_ev_buffer_pct`, `matic_usdc_price`.
4. Wire `GasEstimator` into `Orchestrator._execution_consumer_loop()` BEFORE `ClaudeClient.evaluate()`.
5. Wire `GasEstimator` into `Orchestrator._exit_scan_loop()` to pass live gas cost to `PnLCalculator.settle()`.
6. structlog audit events: `gas.estimated`, `gas.check_passed`, `gas.check_failed`, `gas.rpc_failed`, `gas.settlement_computed`, `matic_price.fetch_failed`.

### Out of Scope
1. Gas unit estimation per transaction type — `gas_units=21000` is hardcoded (configurable in future phase).
2. EIP-1559 fee market — Polygon uses legacy `eth_gasPrice`.
3. Modifications to `ClaudeClient`, `PromptFactory`, `LLMEvaluationResponse`, or Gatekeeper internals.
4. MATIC price persistence or historical oracle queries.
5. New alert thresholds or circuit breaker policy changes based on gas-adjusted PnL.
6. Exit Path gating — the gas check ONLY gates `_execution_consumer_loop()`, never `_exit_scan_loop()`.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi29.md`
4. `docs/PRD-v10.0.md` (WI-29 section)
5. `src/agents/execution/gas_estimator.py` — **primary target: full rewrite**
6. `src/core/config.py` — **target: add 4 new AppConfig fields**
7. `src/orchestrator.py` — **target: wire gas gate into _execution_consumer_loop() and gas cost into _exit_scan_loop()**
8. `src/agents/execution/pnl_calculator.py` — **context: WI-28 settle() signature expects optional gas_cost_usdc**
9. `src/schemas/execution.py` — **context: ExecutionResult and Action enum for SKIP**
10. `src/core/exceptions.py` — **context: existing GasEstimatorError (not used in WI-29 — GasEstimator is fail-open)**
11. Existing test files (verify no regression):
    - `tests/unit/test_orchestrator.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`
    - `tests/unit/test_pnl_calculator.py`
    - `tests/integration/test_pnl_settlement_integration.py`

Do not proceed if this context is not loaded.

---

## CRITICAL INVARIANT: Fail-Open Gas Estimation

`GasEstimator.estimate_gas_price_wei()` and `MaticPriceProvider.get_matic_usdc()` MUST be fail-open. The concurrent multi-market tracking pipeline introduced by WI-32 cannot be halted by a degraded Polygon RPC. Every exception path must catch ALL exceptions and return the configured fallback value — never propagate:

```python
try:
    # live fetch
    ...
    return gas_price_wei
except Exception as exc:
    self.log.error("gas.rpc_failed", error=str(exc))
    return Decimal(str(self.config.dry_run_gas_price_wei))
```

Omitting the blanket `except Exception` catch is a bug. The fallback value flows through `pre_evaluate_gas_check()` normally — the gate still fires, but on a known-safe mock price.

---

## CRITICAL INVARIANT: Strict Decimal Math

Every gas computation step — WEI hex parsing, MATIC conversion, USDC output, EV comparison, buffer multiplication — must use `Decimal`. The WEI-to-MATIC conversion uses:

```python
_WEI_PER_MATIC = Decimal("1000000000000000000")  # 1e18
gas_cost_matic = Decimal(str(gas_units)) * gas_price_wei / _WEI_PER_MATIC
gas_cost_usdc = gas_cost_matic * matic_usdc_price
```

`float` at any step is a bug. The `int(result_hex, 16)` parse is permitted for hex-to-int conversion, but must be immediately wrapped in `Decimal(str(...))`.

---

## CRITICAL INVARIANT: Exit Path Independence

The pre-evaluation gas gate runs ONLY in `_execution_consumer_loop()`. The `_exit_scan_loop()` calls `GasEstimator` to obtain live gas cost for `PnLCalculator.settle()` — but exits proceed unconditionally. A position liquidation is NEVER blocked by gas cost exceeding EV.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code. No implementation code can be written until the failing tests are committed and verified.

---

## Phase 1: Test Suite (RED Phase)

Create two new test files. All tests MUST fail (RED) before any production code is modified.

### Step 1.1 — Create `tests/unit/test_wi29_live_fees.py`

Write unit tests covering the following behaviors:

**A. `GasEstimator.estimate_gas_price_wei()` — RPC fetch and fallback:**

1. **Successful RPC call:** mock `httpx.AsyncClient.post()` returning `{"result": "0x6FC23AC00"}` — assert returned `Decimal` equals `Decimal("30000000000")`.
2. **HTTP error fallback:** mock raises `httpx.HTTPError` — assert returns `Decimal(str(config.dry_run_gas_price_wei))`, `gas.rpc_failed` logged.
3. **JSON parse error fallback:** mock returns malformed JSON — assert returns fallback, `gas.rpc_failed` logged.
4. **Non-200 status fallback:** mock returns HTTP 500 — assert returns fallback, `gas.rpc_failed` logged.
5. **`dry_run=True` skips HTTP:** assert no HTTP call made, returns `Decimal(str(config.dry_run_gas_price_wei))` directly.
6. **Return type is always `Decimal`:** assert `isinstance(result, Decimal)` for both success and fallback paths.

**B. `GasEstimator.estimate_gas_cost_usdc()` — USDC conversion formula:**

7. **Standard formula:** `estimate_gas_cost_usdc(gas_units=21000, gas_price_wei=Decimal("30000000000"), matic_usdc_price=Decimal("0.50"))` — assert result equals `Decimal("21000") * Decimal("30000000000") / Decimal("1000000000000000000") * Decimal("0.50")`.
8. **Zero gas price:** `gas_price_wei=Decimal("0")` — assert `gas_cost_usdc == Decimal("0")`.
9. **No float introduced:** assert `isinstance(result, Decimal)` for all inputs.
10. **High gas scenario:** `gas_price_wei=Decimal("100000000000")` (100 Gwei), `matic_usdc_price=Decimal("1.00")` — assert correctly scaled result.

**C. `GasEstimator.pre_evaluate_gas_check()` — EV gate logic:**

11. **Passes gate:** `expected_value_usdc=Decimal("0.10")`, `gas_cost_usdc=Decimal("0.05")`, `gas_ev_buffer_pct=Decimal("0.10")` — assert `True` (0.10 > 0.05 * 1.10 = 0.055).
12. **Fails gate — gas equals buffered threshold:** `expected_value_usdc=Decimal("0.055")`, `gas_cost_usdc=Decimal("0.05")`, buffer `0.10` — assert `False` (not strictly greater).
13. **Fails gate — gas exceeds EV:** `expected_value_usdc=Decimal("0.03")`, `gas_cost_usdc=Decimal("0.05")` — assert `False`.
14. **Zero gas cost always passes:** `gas_cost_usdc=Decimal("0")`, `expected_value_usdc=Decimal("0.01")` — assert `True`.
15. **Custom buffer pct:** `gas_ev_buffer_pct=Decimal("0.20")` — assert gate uses 20% margin correctly.

**D. `MaticPriceProvider.get_matic_usdc()` — live fetch and fallback:**

16. **Successful fetch:** mock HTTP response returns valid MATIC/USDC price — assert returned `Decimal`.
17. **Fetch failure fallback:** mock raises exception — assert returns `Decimal(str(config.matic_usdc_price))`, `matic_price.fetch_failed` logged.
18. **`dry_run=True` skips HTTP:** assert no HTTP call, returns `config.matic_usdc_price` as `Decimal`.
19. **Return type is always `Decimal`:** assert `isinstance(result, Decimal)` for both paths.

**E. Orchestrator gas gate wiring:**

20. **`gas_check_enabled=False`:** assert `_gas_estimator` is `None`, consumer loop routes directly to `ClaudeClient.evaluate()` without gas check.
21. **`gas_check_enabled=True`, gate passes:** mock `pre_evaluate_gas_check()` returns `True` — assert `ClaudeClient.evaluate()` is called.
22. **`gas_check_enabled=True`, gate fails:** mock `pre_evaluate_gas_check()` returns `False` — assert `ClaudeClient.evaluate()` is NOT called, result is `ExecutionResult(action=Action.SKIP, reason="gas_cost_exceeds_ev")`.
23. **Exit Path not gated:** simulate `_exit_scan_loop()` with high gas price — assert exit proceeds, `PnLCalculator.settle()` called with `gas_cost_usdc`.

### Step 1.2 — Create `tests/integration/test_wi29_live_fees_integration.py`

Write integration tests covering end-to-end gas gate behavior:

1. **Full pre-evaluation gas gate — PASS path:** mock RPC returning `"0x6FC23AC00"` (30 Gwei) + `matic_usdc_price=Decimal("0.50")` → `estimate_gas_cost_usdc(21000, ...)` → `pre_evaluate_gas_check(expected_value_usdc=Decimal("5.00"), ...)` → assert gate passes, `ClaudeClient.evaluate()` called.
2. **Full pre-evaluation gas gate — SKIP path:** mock high gas price → assert gate fails, consumer loop emits `ExecutionResult(action=Action.SKIP, reason="gas_cost_exceeds_ev")`, `ClaudeClient.evaluate()` NOT called.
3. **RPC fallback chain:** mock HTTP error in `estimate_gas_price_wei()` → fallback value flows through full gate → gate pass/fail based on fallback value with `gas.rpc_failed` logged.
4. **Settlement gas injection:** mock exit position → mock RPC → verify `gas_cost_usdc` passed to `PnLCalculator.settle()` → `net_realized_pnl` reflects gas deduction (uses WI-28 settlement).
5. **`dry_run=True` full pipeline:** assert no live HTTP calls, mock values flow through full gate, no DB writes from settlement path.
6. **Exit Path independence:** simulate high gas price scenario → assert `_exit_scan_loop()` proceeds regardless, gas cost injected into settle call.

### Step 1.3 — Run RED gate

```bash
.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi29_live_fees.py tests/integration/test_wi29_live_fees_integration.py -v
```

**All new tests MUST fail.** Commit the failing test suite:

```
git add tests/unit/test_wi29_live_fees.py tests/integration/test_wi29_live_fees_integration.py
git commit -m "test(wi29): add RED test suite for live fee injection and gas gate"
```

---

## Phase 2: Implementation (GREEN Phase)

Implement production code to make all RED tests pass. Execute steps in order.

### Step 2.1 — Add Config Fields

In `src/core/config.py`, add four new fields to `AppConfig`. Place alongside existing execution-layer config fields:

```python
gas_check_enabled: bool = Field(
    default=False,
    description="Enable pre-evaluation gas cost gate in execution consumer loop",
)
dry_run_gas_price_wei: Decimal = Field(
    default=Decimal("30000000000"),
    description="Mock gas price (WEI) returned in dry_run mode or on RPC failure (30 Gwei)",
)
gas_ev_buffer_pct: Decimal = Field(
    default=Decimal("0.10"),
    description="Required EV margin above gas cost — EV must exceed gas cost * (1 + buffer)",
)
matic_usdc_price: Decimal = Field(
    default=Decimal("0.50"),
    description="Static MATIC/USDC fallback price when MaticPriceProvider fetch fails",
)
```

Add `"dry_run_gas_price_wei"`, `"gas_ev_buffer_pct"`, and `"matic_usdc_price"` to the existing `@field_validator` or float-rejection validator if one exists in `AppConfig`. All three must reject `float`.

### Step 2.2 — Rewrite `GasEstimator`

Replace the contents of `src/agents/execution/gas_estimator.py` entirely. The new implementation:

```python
"""
src/agents/execution/gas_estimator.py

WI-29 Gas Estimator — queries Polygon RPC for eth_gasPrice and converts
to USDC cost for pre-evaluation trade viability gating.

Fail-open by design: any RPC failure returns the configured fallback
dry_run_gas_price_wei instead of raising.
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import structlog

from src.core.config import AppConfig

log = structlog.get_logger(__name__)

_WEI_PER_MATIC = Decimal("1000000000000000000")


class GasEstimator:
    """
    Queries Polygon RPC eth_gasPrice and converts to USDC transaction cost.

    All three public methods are the canonical WI-29 interface:
    - estimate_gas_price_wei()     → async, fail-open RPC query
    - estimate_gas_cost_usdc()     → sync, Decimal-only conversion
    - pre_evaluate_gas_check()     → sync, EV gate with buffer margin
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.log = log.bind(component="GasEstimator")

    async def estimate_gas_price_wei(self) -> Decimal:
        """
        Return current Polygon gas price in WEI as Decimal.

        In dry_run mode, returns config.dry_run_gas_price_wei without HTTP.
        On any failure, logs gas.rpc_failed and returns dry_run_gas_price_wei.
        """
        if self.config.dry_run:
            return Decimal(str(self.config.dry_run_gas_price_wei))

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_gasPrice",
            "params": [],
            "id": 1,
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(
                    self.config.polygon_rpc_url,
                    json=payload,
                )
                response.raise_for_status()
                result_hex: str = response.json()["result"]
                gas_price_wei = Decimal(str(int(result_hex, 16)))
                self.log.info("gas.estimated", gas_price_wei=str(gas_price_wei))
                return gas_price_wei
        except Exception as exc:
            self.log.error("gas.rpc_failed", error=str(exc))
            return Decimal(str(self.config.dry_run_gas_price_wei))

    def estimate_gas_cost_usdc(
        self,
        gas_units: int,
        gas_price_wei: Decimal,
        matic_usdc_price: Decimal,
    ) -> Decimal:
        """
        Convert gas usage to USDC cost using Decimal-only arithmetic.

        Formula: gas_units * gas_price_wei / 1e18 * matic_usdc_price
        """
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

    def pre_evaluate_gas_check(
        self,
        expected_value_usdc: Decimal,
        gas_cost_usdc: Decimal,
    ) -> bool:
        """
        Return True when expected_value_usdc > gas_cost_usdc * (1 + gas_ev_buffer_pct).

        True  → trade is economically viable, proceed to ClaudeClient.evaluate()
        False → trade fails gas gate, SKIP with reason "gas_cost_exceeds_ev"
        """
        buffered_threshold = gas_cost_usdc * (
            Decimal("1") + self.config.gas_ev_buffer_pct
        )
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

### Step 2.3 — Create `MaticPriceProvider`

Create `src/agents/execution/matic_price_provider.py`:

```python
"""
src/agents/execution/matic_price_provider.py

WI-29 MATIC/USDC Price Provider — fetches live MATIC price from
Gamma REST API with static config fallback.

Fail-open by design: any fetch failure returns config.matic_usdc_price.
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import structlog

from src.core.config import AppConfig

log = structlog.get_logger(__name__)

# Gamma REST API endpoint for MATIC/USDC price
_GAMMA_MATIC_URL = "https://gamma-api.polymarket.com/prices?ids=MATIC"


class MaticPriceProvider:
    """
    Fetches live MATIC/USDC price with static fallback to config.matic_usdc_price.

    Fail-open: any HTTP error or parse failure returns the config static price.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.log = log.bind(component="MaticPriceProvider")

    async def get_matic_usdc(self) -> Decimal:
        """
        Return current MATIC/USDC price as Decimal.

        In dry_run mode, returns config.matic_usdc_price without HTTP.
        On any failure, logs matic_price.fetch_failed and returns config.matic_usdc_price.
        """
        if self.config.dry_run:
            return Decimal(str(self.config.matic_usdc_price))

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(_GAMMA_MATIC_URL)
                response.raise_for_status()
                data = response.json()
                # Gamma API returns {"MATIC": "0.50"} or similar
                matic_price = Decimal(str(data.get("MATIC", self.config.matic_usdc_price)))
                self.log.info("matic_price.fetched", matic_usdc_price=str(matic_price))
                return matic_price
        except Exception as exc:
            self.log.error("matic_price.fetch_failed", error=str(exc))
            return Decimal(str(self.config.matic_usdc_price))
```

### Step 2.4 — Wire into Orchestrator

In `src/orchestrator.py`, make the following changes:

**Imports (add to existing imports):**
```python
from src.agents.execution.gas_estimator import GasEstimator
from src.agents.execution.matic_price_provider import MaticPriceProvider
```

**`__init__()` — conditional construction (after existing component init):**
```python
if self.config.gas_check_enabled:
    self._gas_estimator: GasEstimator | None = GasEstimator(config=self.config)
    self._matic_price_provider: MaticPriceProvider | None = MaticPriceProvider(config=self.config)
else:
    self._gas_estimator = None
    self._matic_price_provider = None
```

**`_execution_consumer_loop()` — gas gate BEFORE `ClaudeClient.evaluate()`:**

Locate the section of `_execution_consumer_loop()` that calls `ClaudeClient.evaluate()`. Insert the gas gate immediately before that call:

```python
# WI-29: Pre-evaluation gas cost gate
if self.config.gas_check_enabled and self._gas_estimator and self._matic_price_provider:
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
        self.log.warning(
            "gas.check_failed",
            condition_id=str(item.condition_id),
            gas_cost_usdc=str(gas_cost_usdc),
        )
        result = ExecutionResult(action=Action.SKIP, reason="gas_cost_exceeds_ev")
        await self._handle_execution_result(result, item)
        continue
```

**`_exit_scan_loop()` — live gas cost injection into settle:**

In `_exit_scan_loop()`, locate the call to `PnLCalculator.settle()`. Before that call, compute live gas cost and pass it through:

```python
# WI-29: Inject live gas cost for net PnL accounting (exit path always proceeds)
gas_cost_usdc_for_settlement = Decimal("0")
if self._gas_estimator and self._matic_price_provider:
    gas_price_wei = await self._gas_estimator.estimate_gas_price_wei()
    matic_price = await self._matic_price_provider.get_matic_usdc()
    gas_cost_usdc_for_settlement = self._gas_estimator.estimate_gas_cost_usdc(
        gas_units=21000,
        gas_price_wei=gas_price_wei,
        matic_usdc_price=matic_price,
    )

pnl_record = await self._pnl_calculator.settle(
    position=position,
    exit_price=exit_price,
    gas_cost_usdc=gas_cost_usdc_for_settlement,
)
```

### Step 2.5 — Run GREEN gate

```bash
.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi29_live_fees.py tests/integration/test_wi29_live_fees_integration.py -v
```

**All new WI-29 tests MUST pass.** Commit the implementation:

```
git add src/agents/execution/gas_estimator.py src/agents/execution/matic_price_provider.py src/core/config.py src/orchestrator.py
git commit -m "feat(wi29): implement GasEstimator, MaticPriceProvider, and pre-evaluation gas gate"
```

---

## Phase 3: Refactor & Regression

### Step 3.1 — Full regression

```bash
.venv/bin/pytest --asyncio-mode=auto tests/ -q
```

**ALL tests must pass** (target: 620 + new WI-29 tests). Fix any regressions before proceeding. Do not suppress or skip pre-existing tests.

The most likely regression sources:
- Existing tests that import from `src/agents/execution/gas_estimator.py` may reference the old Phase 5 API (`AsyncWeb3`, `GasPrice`, `estimate()` method). Update these tests to use the new WI-29 API or mock accordingly.
- Existing `test_orchestrator.py` tests may not mock the new `GasEstimator`/`MaticPriceProvider` init path — patch `gas_check_enabled=False` in their config fixtures.

### Step 3.2 — Coverage verification

```bash
.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m
```

Coverage MUST remain at or above **94%**. If coverage drops, add targeted tests for uncovered lines (particularly the fallback paths in `GasEstimator` and `MaticPriceProvider`) before proceeding.

### Step 3.3 — Regression commit

If any fixes were needed in Phase 3, commit them atomically:

```
git commit -m "fix(wi29): address regression findings from full test suite"
```

---

## Regression Gate Summary

| Gate | Command | Pass Criteria |
|---|---|---|
| RED | `.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi29_live_fees.py tests/integration/test_wi29_live_fees_integration.py -v` | All new tests FAIL |
| GREEN | Same command | All new tests PASS |
| Regression | `.venv/bin/pytest --asyncio-mode=auto tests/ -q` | ALL tests pass (620 + WI-29 additions) |
| Coverage | `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` | >= 94% |

---

## Definition of Done

Before declaring WI-29 complete:

1. All new WI-29 unit and integration tests pass GREEN.
2. Full regression suite passes with zero failures (620+ existing tests intact).
3. Coverage >= 94%.
4. `STATE.md` updated: test count, coverage, WI-29 marked complete.
5. `CLAUDE.md` updated: active WI status.
6. `README.md` updated if any new environment variables are introduced (`POLYGON_RPC_URL` must be documented if not already present).
7. Memory Consolidation executed per CLAUDE.md DoD.

---

## Files Modified (Summary)

| File | Change |
|---|---|
| `src/agents/execution/gas_estimator.py` | **REWRITE** — Replace Phase 5 EIP-1559/web3 stub with WI-29 `httpx`-based `eth_gasPrice` implementation |
| `src/agents/execution/matic_price_provider.py` | **NEW** — `MaticPriceProvider` with `get_matic_usdc()` and static fallback |
| `src/core/config.py` | Add `gas_check_enabled`, `dry_run_gas_price_wei`, `gas_ev_buffer_pct`, `matic_usdc_price` |
| `src/orchestrator.py` | Wire gas gate into `_execution_consumer_loop()` and gas cost injection into `_exit_scan_loop()` |
| `tests/unit/test_wi29_live_fees.py` | **NEW** — ~23 unit tests |
| `tests/integration/test_wi29_live_fees_integration.py` | **NEW** — ~6 integration tests |

## Files NOT Modified

| File | Reason |
|---|---|
| `src/agents/evaluation/claude_client.py` | Gatekeeper evaluation unchanged — gas gate precedes it |
| `src/agents/context/prompt_factory.py` | Prompt strategies unchanged per WI-12 |
| `src/schemas/llm.py` | LLM schemas unchanged |
| `src/schemas/risk.py` | Risk schemas unchanged (WI-28 already handles fee fields) |
| `src/schemas/execution.py` | `ExecutionResult` and `Action.SKIP` already exist — no new schema additions |
| `src/schemas/position.py` | Position schemas unchanged |
| `src/db/models.py` | Zero DB schema changes — GasEstimator is read-only |
| `src/db/repositories/position_repository.py` | Repository unchanged |
| `migrations/` | Zero migrations — GasEstimator writes nothing |
| `src/agents/execution/execution_router.py` | BUY routing unchanged |
| `src/agents/execution/exit_order_router.py` | SELL routing unchanged |
| `src/agents/execution/pnl_calculator.py` | WI-28 `settle()` signature already accepts `gas_cost_usdc` — no further changes |
| `src/agents/execution/circuit_breaker.py` | Entry gate unchanged |
| `src/agents/execution/alert_engine.py` | Alert thresholds unchanged |
| `src/agents/execution/position_tracker.py` | Position tracking unchanged |
| `src/agents/execution/lifecycle_reporter.py` | Lifecycle reporting unchanged |
| `src/agents/context/aggregator.py` | DataAggregator unchanged |
| `src/agents/ingestion/ws_client.py` | WebSocket client unchanged |
