# P30-WI-30 — Global Portfolio Exposure Limits Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi30-exposure-limits` (branched from current `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/risk-auditor.md`
- `.agents/rules/async-architect.md`
- `.agents/rules/security-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-30 for Phase 10: a cross-market portfolio exposure gate that prevents the sum of all open position sizes from exceeding the configured global risk cap (3% of bankroll) or per-category risk cap (1.5% of bankroll).

Today, `Orchestrator._execution_consumer_loop()` evaluates each market in isolation. The Gatekeeper enforces per-market confidence thresholds and WI-29 enforces per-trade gas viability — but neither guards against the cumulative risk of many open positions simultaneously. WI-30 inserts a synchronous pre-evaluation gate that queries the live DB state, computes aggregate and per-category exposure, and blocks new entries when either cap is breached.

When the gate fires, the trade is short-circuited with `ExecutionResult(action=SKIP, reason="exposure_limit_exceeded")` — no LLM API call is made, no gas check is run, and no order is routed.

---

## Objective & Scope

### In Scope
1. Create `src/agents/execution/exposure_validator.py` — `ExposureValidator` with three public methods.
2. Add `ExposureSummary` frozen Pydantic model to `src/schemas/risk.py`.
3. Add three `AppConfig` fields: `enable_exposure_validator`, `max_exposure_pct`, `max_category_exposure_pct`.
4. Wire `ExposureValidator` into `Orchestrator.__init__()` (conditional) and `_execution_consumer_loop()` (BEFORE gas gate and `ClaudeClient.evaluate()`).
5. structlog audit events: `exposure.validated`, `exposure.limit_exceeded`, `exposure.summary_computed`.

### Out of Scope
1. Dynamic exposure limits based on correlation analysis.
2. Per-position or per-contract exposure limits.
3. DB persistence of `ExposureSummary` snapshots.
4. Modifications to `ExecutionRouter`, `KellySizer`, `LLMEvaluationResponse`, or Gatekeeper internals.
5. Real-time exposure recalculation during position lifecycle.
6. Exit Path gating — `ExposureValidator` NEVER gates `_exit_scan_loop()`.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi30.md`
4. `docs/PRD-v10.0.md` (WI-30 section)
5. `src/db/repositories/position_repository.py` — **primary data source: `get_open_positions()`**
6. `src/db/models.py` — **verify: does `Position` have a `category` column?**
7. `src/schemas/risk.py` — **target: add `ExposureSummary` model**
8. `src/schemas/llm.py` — **context: `MarketCategory` enum (CRYPTO, POLITICS, SPORTS, GENERAL)**
9. `src/core/config.py` — **target: add 3 new AppConfig fields**
10. `src/orchestrator.py` — **target: wire ExposureValidator into `_execution_consumer_loop()`**
11. `src/schemas/execution.py` — **context: `ExecutionResult` and `Action.SKIP` already exist**
12. Existing test files (verify no regression):
    - `tests/unit/test_orchestrator.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`
    - `tests/unit/test_wi29_live_fees.py`
    - `tests/integration/test_wi29_live_fees_integration.py`

**CRITICAL PRE-FLIGHT CHECK:** After reading `src/db/models.py`, determine whether the `Position` model has a `category` column. This determines whether per-category exposure can be computed from DB state or whether it must be derived differently. Document your finding in the first commit message.

Do not proceed if this context is not loaded.

---

## CRITICAL INVARIANT: Strict Decimal Math

Every exposure computation step — position sums, bankroll multiplication, limit comparisons, headroom calculations — must use `Decimal`. No `float` at any step:

```python
_ZERO = Decimal("0")

aggregate = sum(
    (Decimal(str(p.order_size_usdc)) for p in positions),
    _ZERO,
)
global_limit = bankroll_usdc * self.config.max_exposure_pct
passes = (aggregate + proposed_size_usdc) <= global_limit
```

`float` in any of these computations is a bug. Schema validators must reject `float` for `max_exposure_pct` and `max_category_exposure_pct`.

---

## CRITICAL INVARIANT: NOT Fail-Open

Unlike WI-29's gas estimation (which degrades gracefully to a mock price), `ExposureValidator` does NOT catch `PositionRepository` exceptions. A DB failure must propagate:

```python
# WRONG — do not suppress DB errors:
try:
    positions = self._get_positions()
except Exception:
    return (True, ...)  # silently allows trade — NEVER DO THIS

# CORRECT — let DB errors propagate:
positions = self._get_positions()  # raises if DB unavailable
```

A decision made without knowing current exposure state could silently push the portfolio over the 3% bankroll cap. Propagation is the safe behavior.

---

## CRITICAL INVARIANT: Synchronous Validator

`ExposureValidator` contains NO `async def` methods. It is synchronous by design. The Orchestrator manages the async/sync boundary. See Section 4 (Step 2.4) for the exact wiring pattern.

---

## CRITICAL INVARIANT: Exit Path Independence

The exposure gate runs ONLY in `_execution_consumer_loop()`. The `_exit_scan_loop()` is NEVER touched by WI-30. An over-exposed portfolio can always reduce exposure via exits.

---

## CRITICAL INVARIANT: Gate Order in Consumer Loop

When both WI-29 and WI-30 are enabled, the gate order in `_execution_consumer_loop()` MUST be:

```
Kelly sizing → ExposureValidator (WI-30) → GasEstimator gate (WI-29) → ClaudeClient.evaluate()
```

Rationale: portfolio rejection (local DB read) is cheaper than an RPC call. Short-circuit DB-rejected trades before any network I/O.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code. No implementation code can be written until the failing tests are committed and verified.

---

## Phase 1: Test Suite (RED Phase)

Create two new test files. All tests MUST fail (RED) before any production code is modified.

### Step 1.1 — Create `tests/unit/test_wi30_exposure_limits.py`

Write unit tests covering the following behaviors:

**A. `ExposureValidator._compute_aggregate_exposure()` — position summing:**

1. **Empty positions:** mock `get_open_positions()` returning `[]` — assert returns `Decimal("0")`.
2. **Single position:** mock returns `[Position(order_size_usdc=Decimal("10"))]` — assert returns `Decimal("10")`.
3. **Multiple positions:** mock returns 3 positions with sizes `Decimal("5")`, `Decimal("12.50")`, `Decimal("7.25")` — assert returns `Decimal("24.75")`.
4. **Return type:** assert `isinstance(result, Decimal)` in all cases.
5. **No float introduced:** assert no `float` in intermediate computations (verify by patching `float` builtin or checking formula directly).

**B. `ExposureValidator._compute_category_exposure()` — per-category filtering:**

6. **Filters by category:** mock positions with mix of CRYPTO and SPORTS; call `_compute_category_exposure(MarketCategory.CRYPTO)` — assert returns only CRYPTO sum.
7. **No matching positions:** mock positions all SPORTS; call `_compute_category_exposure(MarketCategory.CRYPTO)` — assert returns `Decimal("0")`.
8. **All categories:** verify each of the four `MarketCategory` values filters correctly.
9. **Return type:** assert `isinstance(result, Decimal)`.

**C. `ExposureValidator.validate_entry()` — gate logic:**

10. **Both checks pass — under both limits:** bankroll `Decimal("1000")`, max_exposure_pct `Decimal("0.03")`, max_category_exposure_pct `Decimal("0.015")`; open positions `Decimal("10")` aggregate; proposed `Decimal("5")` CRYPTO; category exposure `Decimal("5")` — assert `(True, summary)`.
11. **Global cap breach:** aggregate `Decimal("28")`, proposed `Decimal("5")` — `33 > 30` — assert `(False, summary)`.
12. **Category cap breach:** aggregate `Decimal("10")`, category (CRYPTO) `Decimal("14")`, proposed `Decimal("2")` CRYPTO — `16 > 15` — assert `(False, summary)`.
13. **At global limit boundary (equal):** aggregate + proposed == 30 exactly — assert `(True, summary)` (at-limit is allowed, `<=` not `<`).
14. **One cent over global limit:** aggregate + proposed == `Decimal("30.01")` — assert `(False, summary)`.
15. **Zero open positions — small proposed:** bankroll `Decimal("1000")`; all positions empty; proposed `Decimal("5")` — assert `(True, summary)`.
16. **Zero proposed size:** proposed `Decimal("0")` — always passes regardless of exposure state.
17. **Always returns `ExposureSummary`:** assert return value is `(bool, ExposureSummary)` for both pass and fail paths.
18. **`ExposureSummary.validation_passed` matches bool return:** assert `summary.validation_passed == passed`.
19. **`ExposureSummary.available_headroom_usdc` non-negative:** assert `summary.available_headroom_usdc >= Decimal("0")` even when over-limit.
20. **`ExposureSummary.aggregate_check_passed` and `category_check_passed` set correctly:** verify both flags independently.

**D. `ExposureSummary` schema:**

21. **Frozen:** assert `ExposureSummary` cannot be mutated after construction.
22. **Rejects `float`:** assert `ValidationError` raised when `aggregate_exposure_usdc=0.01` (float).
23. **All Decimal fields validate via `Decimal(str(value))`:** assert string coercion path works.

**E. Orchestrator gate wiring:**

24. **`enable_exposure_validator=False`:** assert `_exposure_validator` is `None`, consumer loop routes directly to next gate without exposure check.
25. **`enable_exposure_validator=True`, gate passes:** mock `validate_entry()` returns `(True, summary)` — assert `ClaudeClient.evaluate()` is called.
26. **`enable_exposure_validator=True`, gate fails:** mock `validate_entry()` returns `(False, summary)` — assert `ClaudeClient.evaluate()` is NOT called, result is `ExecutionResult(action=Action.SKIP, reason="exposure_limit_exceeded")`.
27. **Gate order when both WI-29 and WI-30 enabled:** exposure check fires before gas check — verify mock call order.
28. **Exit Path not gated:** simulate `_exit_scan_loop()` with over-exposed portfolio — assert exit proceeds, `PnLCalculator.settle()` called normally.

### Step 1.2 — Create `tests/integration/test_wi30_exposure_limits_integration.py`

Write integration tests covering end-to-end exposure gate behavior:

1. **Full pass path:** Seed test DB with 2 OPEN positions totaling `Decimal("20")` USDC; bankroll `Decimal("1000")`; propose `Decimal("5")` CRYPTO; assert `validate_entry()` returns `True` (aggregate: `25 <= 30`, CRYPTO: `5 <= 15`).
2. **Global cap breach path:** Seed DB with positions totaling `Decimal("28")`; propose `Decimal("5")`; assert `validate_entry()` returns `False`, `ExecutionResult(SKIP)` emitted.
3. **Category cap breach path:** Seed DB with `Decimal("14")` CRYPTO positions; global aggregate `Decimal("14")`; propose `Decimal("2")` CRYPTO; assert `False` (CRYPTO: `16 > 15`).
4. **Zero positions — first trade:** Seed DB with no OPEN positions; propose `Decimal("10")`; assert `True`.
5. **Orchestrator consumer loop end-to-end:** Use real or mocked DB; inject over-exposure condition; assert `_execution_consumer_loop()` emits `ExecutionResult(SKIP, "exposure_limit_exceeded")` without calling `ClaudeClient.evaluate()`.
6. **Exit Path independence:** Seed over-exposed portfolio (positions > `max_exposure_pct * bankroll`); call `_exit_scan_loop()`; assert exit proceeds, `PnLCalculator.settle()` is called, exposure check is NOT invoked.
7. **`dry_run=True` full pipeline:** Assert no DB mutations, mock positions flow through full validator, `ExposureSummary` logged.
8. **WI-29 + WI-30 together:** Enable both validators; mock gas gate to pass; seed near-limit exposure; assert exposure gate fires first; mock gas to fail; assert exposure gate still fires first (order invariant).

### Step 1.3 — Run RED gate

```bash
.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi30_exposure_limits.py tests/integration/test_wi30_exposure_limits_integration.py -v
```

**All new tests MUST fail.** Commit the failing test suite:

```
git add tests/unit/test_wi30_exposure_limits.py tests/integration/test_wi30_exposure_limits_integration.py
git commit -m "test(wi30): add RED test suite for portfolio exposure limits and ExposureValidator gate"
```

---

## Phase 2: Implementation (GREEN Phase)

Implement production code to make all RED tests pass. Execute steps in order.

### Step 2.1 — Pre-flight: Verify `Position` Model Category Column

Read `src/db/models.py`. Search for a `category` column on the `Position` model.

**If `category` column EXISTS:**
Proceed with per-category exposure computation as specified. `_compute_category_exposure()` filters by `position.category == category.value`.

**If `category` column DOES NOT EXIST:**
Per-category exposure computation returns `Decimal("0")` for all categories (making the category check always pass). Document this limitation in a code comment. The global aggregate check remains the primary exposure gate. Do NOT add a migration — that is out of scope for WI-30. Note this limitation in the commit message.

### Step 2.2 — Add Config Fields

In `src/core/config.py`, add three new fields to `AppConfig`. Place alongside the WI-29 exposure-layer config fields:

```python
enable_exposure_validator: bool = Field(
    default=False,
    description="Enable portfolio exposure gate in execution consumer loop",
)
max_exposure_pct: Decimal = Field(
    default=Decimal("0.03"),
    description="Global portfolio exposure cap as fraction of bankroll (3% per risk_management.md)",
)
max_category_exposure_pct: Decimal = Field(
    default=Decimal("0.015"),
    description="Per-category exposure cap as fraction of bankroll (1.5% — half the global cap)",
)
```

Add `"max_exposure_pct"` and `"max_category_exposure_pct"` to the existing `@field_validator` or float-rejection validator in `AppConfig`. Both must reject `float`.

### Step 2.3 — Add `ExposureSummary` to `src/schemas/risk.py`

Add `ExposureSummary` as a new frozen Pydantic model. Place after the existing `PositionReport` class. Do not modify any existing classes.

```python
class ExposureSummary(BaseModel):
    """
    Point-in-time snapshot of portfolio exposure state.
    Logged via structlog on every ExposureValidator.validate_entry() call.
    All Decimal fields coerced via Decimal(str(value)) — no float accepted.
    """
    model_config = ConfigDict(frozen=True)

    aggregate_exposure_usdc: Decimal
    category_exposures: dict[str, Decimal]
    proposed_size_usdc: Decimal
    bankroll_usdc: Decimal
    global_limit_usdc: Decimal
    category_limit_usdc: Decimal
    available_headroom_usdc: Decimal
    category_headroom: dict[str, Decimal]
    aggregate_check_passed: bool
    category_check_passed: bool
    validation_passed: bool

    @field_validator(
        "aggregate_exposure_usdc",
        "proposed_size_usdc",
        "bankroll_usdc",
        "global_limit_usdc",
        "category_limit_usdc",
        "available_headroom_usdc",
        mode="before",
    )
    @classmethod
    def _coerce_decimal_fields(cls, v: Any) -> Decimal:
        if isinstance(v, float):
            raise ValueError("float is not allowed — use Decimal(str(value))")
        return Decimal(str(v))
```

### Step 2.4 — Create `ExposureValidator`

Create `src/agents/execution/exposure_validator.py`:

```python
"""
src/agents/execution/exposure_validator.py

WI-30 Portfolio Exposure Validator — synchronous pre-evaluation gate
that enforces global and per-category position exposure caps.

NOT fail-open: PositionRepository errors propagate to the caller.
All arithmetic is Decimal-only. No async methods.
"""
from __future__ import annotations

from decimal import Decimal

import structlog

from src.core.config import AppConfig
from src.db.repositories.position_repository import PositionRepository
from src.schemas.llm import MarketCategory
from src.schemas.risk import ExposureSummary

log = structlog.get_logger(__name__)

_ZERO = Decimal("0")


class ExposureValidator:
    """
    Synchronous portfolio-level gate: validates aggregate and per-category
    exposure against configured bankroll caps before any trade entry.

    Reads via PositionRepository — zero DB writes.
    NOT fail-open: repository errors propagate to the caller.
    """

    def __init__(
        self,
        config: AppConfig,
        position_repo: PositionRepository,
    ) -> None:
        self.config = config
        self._position_repo = position_repo
        self.log = log.bind(component="ExposureValidator")

    def validate_entry(
        self,
        bankroll_usdc: Decimal,
        proposed_size_usdc: Decimal,
        category: MarketCategory,
        open_positions: list,  # list[Position] — pre-fetched by Orchestrator
    ) -> tuple[bool, ExposureSummary]:
        """
        Return (True, summary) when both aggregate and category limits pass.
        Return (False, summary) when either limit is breached.

        The Orchestrator pre-fetches open_positions via await repo.get_open_positions()
        before calling this synchronous method — avoids async/sync boundary inside here.
        """
        aggregate = self._compute_aggregate_exposure(open_positions)
        cat_exposure = self._compute_category_exposure(category, open_positions)

        global_limit = bankroll_usdc * self.config.max_exposure_pct
        cat_limit = bankroll_usdc * self.config.max_category_exposure_pct

        aggregate_check = (aggregate + proposed_size_usdc) <= global_limit
        category_check = (cat_exposure + proposed_size_usdc) <= cat_limit
        passed = aggregate_check and category_check

        all_cat_exposures = {
            c.value: self._compute_category_exposure(c, open_positions)
            for c in MarketCategory
        }
        cat_headroom = {
            c_val: max(_ZERO, cat_limit - exp)
            for c_val, exp in all_cat_exposures.items()
        }

        summary = ExposureSummary(
            aggregate_exposure_usdc=aggregate,
            category_exposures=all_cat_exposures,
            proposed_size_usdc=proposed_size_usdc,
            bankroll_usdc=bankroll_usdc,
            global_limit_usdc=global_limit,
            category_limit_usdc=cat_limit,
            available_headroom_usdc=max(_ZERO, global_limit - aggregate),
            category_headroom=cat_headroom,
            aggregate_check_passed=aggregate_check,
            category_check_passed=category_check,
            validation_passed=passed,
        )

        event = "exposure.validated" if passed else "exposure.limit_exceeded"
        self.log.info(
            event,
            validation_passed=passed,
            aggregate_exposure_usdc=str(aggregate),
            available_headroom_usdc=str(summary.available_headroom_usdc),
        )
        return passed, summary

    def _compute_aggregate_exposure(self, positions: list) -> Decimal:
        """Return SUM(order_size_usdc) for all provided OPEN positions."""
        if not positions:
            return _ZERO
        return sum(
            (Decimal(str(p.order_size_usdc)) for p in positions),
            _ZERO,
        )

    def _compute_category_exposure(
        self,
        category: MarketCategory,
        positions: list,
    ) -> Decimal:
        """Return SUM(order_size_usdc) for positions matching the given category."""
        matching = [
            p for p in positions
            if getattr(p, "category", None) == category.value
        ]
        if not matching:
            return _ZERO
        return sum(
            (Decimal(str(p.order_size_usdc)) for p in matching),
            _ZERO,
        )
```

**Design note on async/sync boundary:** The Orchestrator pre-fetches open positions via `await self._position_repo.get_open_positions()` before calling `validate_entry()`. This keeps `ExposureValidator` synchronous while respecting the async Orchestrator event loop. The signature receives `open_positions: list` as a parameter — this avoids `asyncio.run()` inside an already-running loop.

### Step 2.5 — Wire into Orchestrator

In `src/orchestrator.py`, make the following changes:

**Imports (add to existing imports):**
```python
from src.agents.execution.exposure_validator import ExposureValidator
from src.schemas.risk import ExposureSummary
```

**`__init__()` — conditional construction (after WI-29 gas estimator init):**
```python
if self.config.enable_exposure_validator:
    self._exposure_validator: ExposureValidator | None = ExposureValidator(
        config=self.config,
        position_repo=self._position_repo,
    )
else:
    self._exposure_validator = None
```

**`_execution_consumer_loop()` — exposure gate BEFORE gas gate and ClaudeClient:**

Locate the existing gas gate block (WI-29). Insert the exposure gate IMMEDIATELY BEFORE it:

```python
# WI-30: Portfolio exposure gate (fires before WI-29 gas gate)
if self.config.enable_exposure_validator and self._exposure_validator:
    open_positions = await self._position_repo.get_open_positions()
    passed, summary = self._exposure_validator.validate_entry(
        bankroll_usdc=self.config.bankroll_usdc,
        proposed_size_usdc=item.proposed_size_usdc,
        category=item.category,
        open_positions=open_positions,
    )
    self.log.info("exposure.summary_computed", **summary.model_dump())
    if not passed:
        self.log.warning(
            "exposure.limit_exceeded",
            condition_id=str(item.condition_id),
            aggregate_exposure_usdc=str(summary.aggregate_exposure_usdc),
            available_headroom_usdc=str(summary.available_headroom_usdc),
        )
        result = ExecutionResult(action=Action.SKIP, reason="exposure_limit_exceeded")
        await self._handle_execution_result(result, item)
        continue

# WI-29: Pre-evaluation gas cost gate
if self.config.gas_check_enabled and self._gas_estimator and self._matic_price_provider:
    ...  # existing WI-29 code unchanged
```

**Verify `item.category` field:** Confirm that the `ExecutionItem` (or equivalent queue item schema) carries a `category: MarketCategory` field. If it does not, determine how the Orchestrator currently passes category context and use that instead. Do NOT add a `category` field to schemas not controlled by WI-30.

**Verify `item.proposed_size_usdc` field:** Confirm this field exists on the queue item after Kelly sizing. If named differently (e.g., `order_size_usdc`), use the correct attribute name.

### Step 2.6 — Run GREEN gate

```bash
.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi30_exposure_limits.py tests/integration/test_wi30_exposure_limits_integration.py -v
```

**All new WI-30 tests MUST pass.** Commit the implementation:

```
git add src/agents/execution/exposure_validator.py src/schemas/risk.py src/core/config.py src/orchestrator.py
git commit -m "feat(wi30): implement ExposureValidator, ExposureSummary, and portfolio exposure gate"
```

---

## Phase 3: Refactor & Regression

### Step 3.1 — Full regression

```bash
.venv/bin/pytest --asyncio-mode=auto tests/ -q
```

**ALL tests must pass** (target: 639+ existing tests + new WI-30 tests). Fix any regressions before proceeding. Do not suppress or skip pre-existing tests.

The most likely regression sources:
- Existing `test_orchestrator.py` tests that do not mock the new `ExposureValidator` init path — patch `enable_exposure_validator=False` in their config fixtures.
- Existing `test_pipeline_e2e.py` tests that construct `Orchestrator` — ensure `enable_exposure_validator=False` in test configs.
- Existing WI-29 tests that check gate order in `_execution_consumer_loop()` — update mock call order assertions if needed.

### Step 3.2 — Coverage verification

```bash
.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m
```

Coverage MUST remain at or above **94%**. If coverage drops, add targeted tests for uncovered lines (particularly the `_compute_category_exposure()` fallback and the `available_headroom_usdc` floor) before proceeding.

### Step 3.3 — Regression commit

If any fixes were needed in Phase 3, commit them atomically:

```
git commit -m "fix(wi30): address regression findings from full test suite"
```

---

## Regression Gate Summary

| Gate | Command | Pass Criteria |
|---|---|---|
| RED | `.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi30_exposure_limits.py tests/integration/test_wi30_exposure_limits_integration.py -v` | All new tests FAIL |
| GREEN | Same command | All new tests PASS |
| Regression | `.venv/bin/pytest --asyncio-mode=auto tests/ -q` | ALL tests pass (639+ existing + WI-30 additions) |
| Coverage | `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` | >= 94% |

---

## Definition of Done

Before declaring WI-30 complete:

1. All new WI-30 unit and integration tests pass GREEN.
2. Full regression suite passes with zero failures (639+ existing tests intact).
3. Coverage >= 94%.
4. `STATE.md` updated: test count, coverage, WI-30 marked complete.
5. `CLAUDE.md` updated: active WI status.
6. Memory Consolidation executed per CLAUDE.md DoD (update STATE.md, document invariants, print summary).

---

## Files Modified (Summary)

| File | Change |
|---|---|
| `src/agents/execution/exposure_validator.py` | **NEW** — `ExposureValidator` with `validate_entry()`, `_compute_aggregate_exposure()`, `_compute_category_exposure()` |
| `src/schemas/risk.py` | Add `ExposureSummary` frozen Pydantic model |
| `src/core/config.py` | Add `enable_exposure_validator`, `max_exposure_pct`, `max_category_exposure_pct` |
| `src/orchestrator.py` | Wire exposure gate into `_execution_consumer_loop()` BEFORE gas gate |
| `tests/unit/test_wi30_exposure_limits.py` | **NEW** — ~28 unit tests |
| `tests/integration/test_wi30_exposure_limits_integration.py` | **NEW** — ~8 integration tests |

## Files NOT Modified

| File | Reason |
|---|---|
| `src/agents/evaluation/claude_client.py` | Gatekeeper evaluation unchanged — exposure gate precedes it |
| `src/agents/execution/gas_estimator.py` | WI-29 gate unchanged — WI-30 fires before it |
| `src/agents/execution/matic_price_provider.py` | WI-29 component unchanged |
| `src/agents/execution/pnl_calculator.py` | Settlement logic unchanged |
| `src/agents/execution/circuit_breaker.py` | Entry gate unchanged |
| `src/agents/execution/alert_engine.py` | Alert thresholds unchanged |
| `src/agents/execution/position_tracker.py` | Position tracking unchanged |
| `src/agents/execution/execution_router.py` | BUY routing unchanged |
| `src/agents/execution/exit_order_router.py` | SELL routing unchanged |
| `src/schemas/execution.py` | `ExecutionResult` and `Action.SKIP` already exist — no new schema additions |
| `src/schemas/llm.py` | `MarketCategory` enum already exists — no changes |
| `src/schemas/position.py` | Position schemas unchanged |
| `src/db/models.py` | Zero DB schema changes — `ExposureValidator` is read-only |
| `src/db/repositories/position_repository.py` | Repository unchanged — consumed as-is |
| `migrations/` | Zero migrations — `ExposureValidator` writes nothing |
| `src/agents/context/aggregator.py` | DataAggregator unchanged |
| `src/agents/ingestion/ws_client.py` | WebSocket client unchanged |
| `src/agents/context/prompt_factory.py` | Prompt strategies unchanged |
