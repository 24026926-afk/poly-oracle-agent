# WI-30 Business Logic — Global Portfolio Exposure Limits

## Active Agents + Constraints

- `.agents/rules/risk-auditor.md` — All exposure arithmetic is `Decimal`-only. No `float` in bankroll, exposure sums, limit calculations, or headroom computations. Schema validators on any new monetary field must reject `float` and coerce via `Decimal(str(value))`.
- `.agents/rules/async-architect.md` — `ExposureValidator` is **synchronous** — no async methods, no `await`, no background tasks. It reads from `PositionRepository` via the session provided by the Orchestrator's existing DB session scope. The validator is an additive gate inserted BEFORE `ClaudeClient.evaluate()` — it does not alter queue topology or introduce new concurrency primitives.
- `.agents/rules/security-auditor.md` — `dry_run=True` must use mock bankroll values and still execute the full validation pipeline for deterministic testing. No DB mutations under any code path. `ExposureValidator` has zero write access.
- `.agents/rules/test-engineer.md` — WI-30 requires unit + integration coverage for aggregate exposure queries, per-category exposure queries, global cap pass/fail, category cap pass/fail, Orchestrator SKIP path wiring, dry-run mock execution, and exit path independence.

## 1. Objective

Introduce `ExposureValidator`, a synchronous portfolio-level gate that computes the sum of all open position sizes across every tracked market and blocks new trade entries when that sum would push the portfolio over the configured risk cap.

Today, each market is evaluated independently. The Gatekeeper enforces per-market confidence and EV thresholds, and the gas gate (WI-29) validates per-trade economic viability. Neither of these guards against the cumulative risk of holding many moderately-sized positions simultaneously — a correlated drawdown scenario that is the most dangerous failure mode at portfolio scale.

WI-30 inserts a cross-market exposure gate:

```
BEFORE ClaudeClient.evaluate():

  aggregate = SUM(order_size_usdc) WHERE status = 'OPEN'
  category_sum = SUM(order_size_usdc) WHERE status = 'OPEN' AND category = proposed_category

  IF aggregate + proposed_size_usdc > max_exposure_pct * bankroll_usdc:
      SKIP with reason "exposure_limit_exceeded"
  IF category_sum + proposed_size_usdc > max_category_exposure_pct * bankroll_usdc:
      SKIP with reason "exposure_limit_exceeded"
  ELSE:
      proceed to ClaudeClient.evaluate()
```

This gate is a HARD gate. No per-market Kelly sizing, Gatekeeper confidence score, or EV calculation can override it. The portfolio exposure cap is the final authority before any LLM call is made.

WI-30 also introduces per-category limits (CRYPTO, POLITICS, SPORTS, GENERAL) to prevent over-concentration in correlated segments. Each category is capped at `max_category_exposure_pct` (default 1.5% of bankroll — half the global cap), enforcing diversification across market types.

## 2. Scope Boundaries

### In Scope

1. New `ExposureValidator` class in `src/agents/execution/exposure_validator.py`.
2. Three public interface methods:
   - `validate_entry(bankroll_usdc: Decimal, proposed_size_usdc: Decimal, category: MarketCategory) -> bool`
   - `_compute_aggregate_exposure() -> Decimal` — synchronous, reads all OPEN positions
   - `_compute_category_exposure(category: MarketCategory) -> Decimal` — synchronous, filters by category
3. New `ExposureSummary` frozen Pydantic schema in `src/schemas/risk.py`.
4. Three new `AppConfig` fields in `src/core/config.py`:
   - `enable_exposure_validator: bool` (default `False`)
   - `max_exposure_pct: Decimal` (default `Decimal("0.03")`)
   - `max_category_exposure_pct: Decimal` (default `Decimal("0.015")`)
5. Orchestrator integration: `ExposureValidator` constructed conditionally in `Orchestrator.__init__()`; `validate_entry()` wired into `_execution_consumer_loop()` AFTER Kelly sizing and BEFORE the gas gate (WI-29) and `ClaudeClient.evaluate()`.
6. structlog audit events: `exposure.validated`, `exposure.limit_exceeded`, `exposure.summary_computed`.

### Out of Scope

1. Dynamic category limit adjustment based on correlation analysis — limits are static config.
2. Per-position or per-contract exposure limits — only aggregate and per-category.
3. DB persistence of `ExposureSummary` snapshots — logged only, not stored.
4. Modifications to `ExecutionRouter`, `KellySizer`, `LLMEvaluationResponse`, or Gatekeeper internals.
5. Real-time exposure recalculation during position lifecycle — computed only at entry validation time.
6. Cross-portfolio PnL impact on exposure limits — uses `order_size_usdc` of OPEN positions only (not mark-to-market).
7. Exit Path gating — `ExposureValidator` NEVER gates `_exit_scan_loop()`.

## 3. Target Components + Data Contracts

### 3.1 `ExposureValidator` — `src/agents/execution/exposure_validator.py`

`ExposureValidator` is the canonical portfolio risk gate for WI-30. It is a purely synchronous class that holds a reference to `PositionRepository` and reads the live DB state on each invocation.

```python
class ExposureValidator:
    """
    Portfolio-level gate: validates aggregate and per-category exposure
    against configured bankroll caps before any new trade is queued.

    Synchronous by design — no async methods, no background tasks.
    Read-only: zero DB writes under any code path.
    """

    def __init__(
        self,
        config: AppConfig,
        position_repo: PositionRepository,
    ) -> None:
        ...

    def validate_entry(
        self,
        bankroll_usdc: Decimal,
        proposed_size_usdc: Decimal,
        category: MarketCategory,
    ) -> tuple[bool, ExposureSummary]:
        """
        Return (True, summary) when both aggregate and category limits pass.
        Return (False, summary) when either limit is breached.

        Always returns a typed ExposureSummary for structlog audit.
        """

    def _compute_aggregate_exposure(self) -> Decimal:
        """
        Return SUM(order_size_usdc) for all OPEN positions.
        Returns Decimal("0") when no open positions exist.
        """

    def _compute_category_exposure(self, category: MarketCategory) -> Decimal:
        """
        Return SUM(order_size_usdc) for OPEN positions in the given category.
        Returns Decimal("0") when no open positions match.
        """
```

Required behavior:

1. `_compute_aggregate_exposure()` calls `PositionRepository.get_open_positions()` and sums `position.order_size_usdc` for all returned records using `Decimal` arithmetic.
2. `_compute_category_exposure(category)` calls `PositionRepository.get_open_positions()` and filters by `position.category == category.value` before summing `order_size_usdc`.
3. Both helpers return `Decimal("0")` when the query returns an empty list.
4. `validate_entry()` runs both checks:
   - `aggregate_check`: `aggregate_exposure + proposed_size_usdc <= max_exposure_pct * bankroll_usdc`
   - `category_check`: `category_exposure + proposed_size_usdc <= max_category_exposure_pct * bankroll_usdc`
5. Both checks must pass for `validate_entry()` to return `True`. Either failure returns `False`.
6. `validate_entry()` always constructs and returns an `ExposureSummary` regardless of pass/fail outcome.
7. Emit structlog events:
   - `exposure.summary_computed` — on every invocation with the full summary attached
   - `exposure.validated` — when both checks pass
   - `exposure.limit_exceeded` — when either check fails, with `breach_type` field (`"aggregate"` or `"category"`)
8. `ExposureValidator` is fully synchronous. The `PositionRepository` call inside it must be made via a synchronous session wrapper or the repository must be invoked in a way that does not require `await`. **Design note:** If `PositionRepository.get_open_positions()` is async, the Orchestrator must run it in the event loop before passing results to the validator, or the validator must accept a pre-fetched `list[Position]` — the implementation prompt will specify the exact wiring.

### 3.2 `ExposureSummary` — `src/schemas/risk.py`

Add `ExposureSummary` as a new frozen Pydantic model in the existing `src/schemas/risk.py` file.

```python
class ExposureSummary(BaseModel):
    """
    Snapshot of portfolio exposure state at a single validation instant.
    Logged via structlog on every ExposureValidator.validate_entry() call.
    """
    model_config = ConfigDict(frozen=True)

    aggregate_exposure_usdc: Decimal
    category_exposures: dict[str, Decimal]          # keyed by MarketCategory.value
    proposed_size_usdc: Decimal
    bankroll_usdc: Decimal
    global_limit_usdc: Decimal                       # max_exposure_pct * bankroll_usdc
    category_limit_usdc: Decimal                     # max_category_exposure_pct * bankroll_usdc
    available_headroom_usdc: Decimal                 # global_limit - aggregate_exposure
    category_headroom: dict[str, Decimal]            # per-category remaining capacity
    aggregate_check_passed: bool
    category_check_passed: bool
    validation_passed: bool                          # True iff both checks passed
```

All `Decimal` fields must use the existing `_coerce_decimal` validator pattern from `PositionRecord`. No `float` defaults.

### 3.3 Config Changes — `src/core/config.py`

Add three new fields to `AppConfig`:

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

Both `max_exposure_pct` and `max_category_exposure_pct` must be added to any existing float-rejection `@field_validator` in `AppConfig`. These fields must never accept `float`.

### 3.4 Orchestrator Integration — `src/orchestrator.py`

**`__init__()` — conditional construction:**

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

The gate runs AFTER Kelly sizing (so `proposed_size_usdc` is known) and BEFORE the WI-29 gas gate and `ClaudeClient.evaluate()`. If both WI-29 and WI-30 are enabled, the order is:

```
Kelly sizing → ExposureValidator (WI-30) → GasEstimator gate (WI-29) → ClaudeClient.evaluate()
```

Rationale: exposure validation is a DB read that is cheaper than an RPC call. Portfolio-level rejections should short-circuit before any network I/O.

```python
# WI-30: Portfolio exposure gate
if self.config.enable_exposure_validator and self._exposure_validator:
    passed, summary = self._exposure_validator.validate_entry(
        bankroll_usdc=self.config.bankroll_usdc,
        proposed_size_usdc=item.proposed_size_usdc,
        category=item.category,
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
```

**`_exit_scan_loop()` — NOT modified:**

`ExposureValidator` has zero integration with `_exit_scan_loop()`. Position exits always proceed regardless of portfolio exposure state.

## 4. Core Logic

### 4.1 Aggregate Exposure Computation

```python
def _compute_aggregate_exposure(self) -> Decimal:
    positions = self._position_repo.get_open_positions_sync()
    if not positions:
        return Decimal("0")
    return sum(
        (Decimal(str(p.order_size_usdc)) for p in positions),
        Decimal("0"),
    )
```

**Note on sync/async boundary:** `PositionRepository.get_open_positions()` is an `async` method. The implementation prompt defines the exact pattern for invoking it from a synchronous context. One valid approach is for the Orchestrator to fetch positions once per loop iteration and pass the result to the validator; another is a synchronous session variant. The implementation must NOT use `asyncio.run()` inside an already-running event loop.

### 4.2 Category Exposure Computation

```python
def _compute_category_exposure(self, category: MarketCategory) -> Decimal:
    positions = self._position_repo.get_open_positions_sync()
    category_positions = [
        p for p in positions
        if getattr(p, "category", None) == category.value
    ]
    if not category_positions:
        return Decimal("0")
    return sum(
        (Decimal(str(p.order_size_usdc)) for p in category_positions),
        Decimal("0"),
    )
```

### 4.3 Validation Gate Logic

```python
def validate_entry(
    self,
    bankroll_usdc: Decimal,
    proposed_size_usdc: Decimal,
    category: MarketCategory,
) -> tuple[bool, ExposureSummary]:
    aggregate = self._compute_aggregate_exposure()
    cat_exposure = self._compute_category_exposure(category)

    global_limit = bankroll_usdc * self.config.max_exposure_pct
    cat_limit = bankroll_usdc * self.config.max_category_exposure_pct

    aggregate_check = (aggregate + proposed_size_usdc) <= global_limit
    category_check = (cat_exposure + proposed_size_usdc) <= cat_limit
    passed = aggregate_check and category_check

    # Build category exposure snapshot for all categories
    all_categories = list(MarketCategory)
    cat_exposures = {
        c.value: self._compute_category_exposure(c)
        for c in all_categories
    }
    cat_headroom = {
        c.value: max(Decimal("0"), cat_limit - cat_exposures[c.value])
        for c in all_categories
    }

    summary = ExposureSummary(
        aggregate_exposure_usdc=aggregate,
        category_exposures=cat_exposures,
        proposed_size_usdc=proposed_size_usdc,
        bankroll_usdc=bankroll_usdc,
        global_limit_usdc=global_limit,
        category_limit_usdc=cat_limit,
        available_headroom_usdc=max(Decimal("0"), global_limit - aggregate),
        category_headroom=cat_headroom,
        aggregate_check_passed=aggregate_check,
        category_check_passed=category_check,
        validation_passed=passed,
    )

    event = "exposure.validated" if passed else "exposure.limit_exceeded"
    self.log.info(event, validation_passed=passed)
    return passed, summary
```

### 4.4 Failure Modes and Fallback Behavior

| Failure Mode | Behavior |
|---|---|
| `PositionRepository` raises (DB unavailable) | Propagate exception — do NOT silently allow trade. DB unavailability is a hard error, not a soft fallback. |
| Empty open positions list | `aggregate_exposure = Decimal("0")`, all category exposures `Decimal("0")` — validation passes unless proposed size alone exceeds cap |
| `proposed_size_usdc = Decimal("0")` | Validation passes (zero-size trade cannot breach any cap); downstream components handle zero-size logic |
| `bankroll_usdc = Decimal("0")` | Both limits compute to `Decimal("0")` — any non-zero proposed size fails both checks; this is correct behavior |
| `enable_exposure_validator=False` | Validator not constructed; `_execution_consumer_loop()` routes directly to next gate without any exposure check |

**Critical difference from WI-29:** `ExposureValidator` is NOT fail-open. A DB read failure should bubble up — a decision made without knowing the current exposure state could silently exceed the risk cap. This is the correct trade-off: halt the individual entry attempt rather than risk invisible over-exposure.

## 5. Invariants

1. **Strict `Decimal` math only**
   Every exposure computation step — position sums, bankroll multiplication, limit comparisons, headroom calculations — uses `Decimal`. `float` anywhere in this path is a bug.

2. **NOT fail-open — DB errors propagate**
   Unlike WI-29's gas estimation (which degrades gracefully), `ExposureValidator` cannot safely assume zero exposure when the DB is unavailable. A DB failure causes the trade entry to fail, not to proceed unchecked.

3. **Pre-Gatekeeper gate**
   The exposure check runs BEFORE `ClaudeClient.evaluate()`. No LLM API call is made for a trade that would breach the portfolio cap. This preserves API budget for viable trades.

4. **Exit Path is never gated**
   `_exit_scan_loop()` has zero integration with `ExposureValidator`. Position liquidations proceed unconditionally regardless of exposure state. An over-exposed portfolio can always reduce exposure via exits.

5. **Both checks must pass — AND logic, not OR**
   `validate_entry()` returns `True` only when the aggregate check AND the category check both pass. A trade that is within the global cap but over a category cap is rejected.

6. **`ExposureSummary` logged on every cycle**
   Whether validation passes or fails, a full `ExposureSummary` is emitted to structlog. This provides a complete audit trail of portfolio state at every entry decision point.

7. **Config-gated construction**
   `ExposureValidator` is only constructed when `enable_exposure_validator=True`. When disabled (default), `_execution_consumer_loop()` routes directly to the next gate as before — zero behavior change.

8. **Zero DB writes**
   `ExposureValidator` is a read-only gate. It reads from `PositionRepository` only. No direct session access, no INSERT, no UPDATE.

9. **Zero imports from prompt/context/evaluation/ingestion modules**
   `ExposureValidator` has no dependency on `PromptFactory`, `DataAggregator`, `ClaudeClient`, `CLOBWebSocketClient`, or any Layer 1/2/3 module. It is a pure Layer 4 risk gate that imports from `src/db/repositories`, `src/schemas`, and `src/core/config` only.

10. **Synchronous — no async methods**
    `ExposureValidator` contains no `async def` methods. It is synchronous by design. The Orchestrator is responsible for managing the async/sync boundary when calling into it.

11. **Kelly sizing and Gatekeeper authority unchanged**
    WI-30 inserts a pre-Gatekeeper exposure gate. The Kelly fraction (0.25 Quarter-Kelly), EV formula, LLM confidence threshold, and all Gatekeeper decision logic remain unchanged.

12. **`dry_run=True` uses mock bankroll, still runs full gate**
    In dry-run mode, `validate_entry()` receives the configured mock bankroll and still computes full exposure sums from the DB (which contains only test data). No live DB mutations in dry-run. The full gate pipeline executes for deterministic testing.

## 6. Data Flow — Position Category Mapping

The `MarketCategory` enum (`src/schemas/llm.py`) has four values: `CRYPTO`, `POLITICS`, `SPORTS`, `GENERAL`. The `Position` DB model (or its associated `PositionRecord` schema) must carry a `category` field for per-category exposure computation to function.

**WI-30 dependency check:** Before implementation, verify whether the `Position` DB model already has a `category` column. If not, a migration and model change are required. The PRD does not explicitly add a migration for WI-30, so either:
- The `category` is already stored on `Position`, or
- WI-30's per-category exposure computes `Decimal("0")` for all categories when no `category` column exists, and the global aggregate check remains the primary gate.

The implementation prompt must explicitly verify this before writing the category exposure logic.

## 7. Acceptance Criteria

1. `ExposureValidator` exists in `src/agents/execution/exposure_validator.py` with three public methods: `validate_entry(...) -> tuple[bool, ExposureSummary]`, `_compute_aggregate_exposure() -> Decimal`, `_compute_category_exposure(...) -> Decimal`.
2. `ExposureSummary` exists in `src/schemas/risk.py` as a frozen Pydantic model with all fields defined in Section 3.2.
3. `validate_entry()` returns `(True, summary)` when both aggregate and category exposure are within limits.
4. `validate_entry()` returns `(False, summary)` when aggregate exposure exceeds `max_exposure_pct × bankroll`.
5. `validate_entry()` returns `(False, summary)` when category exposure exceeds `max_category_exposure_pct × bankroll`.
6. When validation fails, `_execution_consumer_loop()` skips with `ExecutionResult(action=SKIP, reason="exposure_limit_exceeded")`.
7. `_exit_scan_loop()` is NOT gated by `ExposureValidator` — exits proceed unconditionally.
8. `ExposureSummary` is logged via structlog on each validation cycle.
9. `AppConfig.enable_exposure_validator` is `bool` with default `False`.
10. `AppConfig.max_exposure_pct` is `Decimal` with default `Decimal("0.03")`.
11. `AppConfig.max_category_exposure_pct` is `Decimal` with default `Decimal("0.015")`.
12. `ExposureValidator` is constructed in `Orchestrator.__init__()` only when `enable_exposure_validator=True`.
13. `ExposureValidator` reads via `PositionRepository` — zero direct DB session access.
14. `ExposureValidator` has zero imports from prompt, context, evaluation, or ingestion modules.
15. `ExposureValidator` performs zero DB writes.
16. `ExposureValidator` contains no `async def` methods.
17. All exposure math is `Decimal`-only — no `float` at any computation step.
18. DB errors in `ExposureValidator` propagate to the caller — NOT silently suppressed.
19. Full regression remains green with coverage >= 94%.

## 8. Test Plan

### Unit Tests

1. `_compute_aggregate_exposure()` with no open positions returns `Decimal("0")`.
2. `_compute_aggregate_exposure()` with N open positions returns correct `Decimal` sum.
3. `_compute_category_exposure(CRYPTO)` returns only CRYPTO position sizes.
4. `_compute_category_exposure(SPORTS)` returns only SPORTS position sizes.
5. `_compute_category_exposure()` returns `Decimal("0")` when no positions match the category.
6. `validate_entry()` returns `True` when aggregate + proposed < global limit and category + proposed < category limit.
7. `validate_entry()` returns `False` when aggregate + proposed > global limit (category within limits).
8. `validate_entry()` returns `False` when category + proposed > category limit (aggregate within limits).
9. `validate_entry()` boundary: aggregate + proposed == global limit → `True` (at-limit is allowed).
10. `validate_entry()` boundary: aggregate + proposed > global limit by `Decimal("0.01")` → `False`.
11. `validate_entry()` always returns an `ExposureSummary` regardless of pass/fail.
12. `ExposureSummary.validation_passed` matches the `bool` return value.
13. `ExposureSummary.available_headroom_usdc` is never negative (floored at `Decimal("0")`).
14. All `Decimal` fields in `ExposureSummary` reject `float` at construction.
15. Orchestrator: `enable_exposure_validator=False` → `_exposure_validator` is `None`, consumer loop skips exposure gate.
16. Orchestrator: `enable_exposure_validator=True`, gate passes → `ClaudeClient.evaluate()` is called.
17. Orchestrator: `enable_exposure_validator=True`, gate fails → `ClaudeClient.evaluate()` is NOT called, `ExecutionResult(SKIP, "exposure_limit_exceeded")` emitted.
18. Orchestrator exit path: `_exit_scan_loop()` proceeds regardless of exposure state.

### Integration Tests

1. **Full pass path:** Mock `PositionRepository` returning 2 OPEN positions totaling `Decimal("20")` USDC; bankroll `Decimal("1000")`; proposed size `Decimal("5")`; assert `validate_entry()` returns `True` (`25 <= 30`).
2. **Global cap breach:** Mock open positions totaling `Decimal("28")`; proposed `Decimal("5")`; assert `validate_entry()` returns `False` (`33 > 30`).
3. **Category cap breach:** Open positions total `Decimal("10")` globally, but `Decimal("14")` in CRYPTO; proposed CRYPTO `Decimal("2")`; assert `validate_entry()` returns `False` (category: `16 > 15`).
4. **Zero open positions — full pass:** Mock empty `get_open_positions()` → all sums `Decimal("0")` → any reasonable proposed size passes.
5. **Orchestrator SKIP path end-to-end:** Mock open positions that breach global cap → assert `ExecutionResult(action=SKIP, reason="exposure_limit_exceeded")` returned, `ClaudeClient.evaluate()` not called.
6. **Exit Path independence:** Simulate over-exposed portfolio → call `_exit_scan_loop()` → assert exit proceeds, `PnLCalculator.settle()` called normally.
7. **`dry_run=True` full pipeline:** Assert no mutations, mock positions flow through full gate, `ExposureSummary` logged.

## 9. Non-Negotiable Design Decisions

### 9.1 Synchronous Validation Is Intentional

`ExposureValidator` is synchronous because:
- The DB read (`get_open_positions()`) is a simple SELECT — not a long-running analytics query.
- The validation logic is pure arithmetic with no I/O beyond the single DB call.
- Keeping the validator synchronous makes it trivially testable without `pytest-asyncio` complexity and avoids introducing new async primitives into the risk gate layer.

The Orchestrator manages the async/sync boundary by either awaiting the repository call before passing results to the validator, or by providing a synchronous session wrapper. The implementation prompt specifies the exact pattern.

### 9.2 DB Errors Are Hard Failures

This is the critical behavioral difference from WI-29's fail-open gas estimation:

- **WI-29 fail-open:** An unavailable RPC returns a known-safe fallback price. The pipeline degrades gracefully because gas estimation is a best-effort pricing oracle.
- **WI-30 fail-hard:** An unavailable DB means we cannot compute current exposure. Proceeding with an unknown exposure state could silently exceed the 3% bankroll cap and expose the portfolio to unquantified risk. Therefore, `ExposureValidator` does NOT catch repository exceptions — they propagate to the Orchestrator and cause the individual entry attempt to fail.

### 9.3 Exposure Gate Position in the Pipeline

The exposure gate fires BEFORE the gas gate (WI-29). The rationale:

1. The exposure check is a DB read (local I/O) — cheaper than an RPC call.
2. If the portfolio is exposure-exhausted, there is no point querying the Polygon RPC.
3. Portfolio-level rejections should be as fast and as early as possible to minimize latency on the hot path.

Both gates must pass before `ClaudeClient.evaluate()` is called. The ordering is:

```
Kelly sizing → WI-30 (ExposureValidator) → WI-29 (GasEstimator) → ClaudeClient.evaluate()
```
