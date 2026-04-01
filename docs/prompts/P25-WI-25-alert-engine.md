# P25-WI-25 — Alert Engine Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-4.1 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi25-alert-engine` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/risk-auditor.md`
- `.agents/rules/security-auditor.md`
- `.agents/rules/async-architect.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-25 for Phase 8: a stateless, rule-based monitoring component (`AlertEngine`) that evaluates pre-computed `PortfolioSnapshot` (WI-23) and `LifecycleReport` (WI-24) against configurable risk thresholds and emits typed `AlertEvent` records when limits are breached. The engine evaluates four deterministic rules: portfolio drawdown, stale-price concentration, position-count ceiling, and settled loss rate.

This WI is **100% read-only and observational**. The engine accepts two typed Pydantic models as arguments, performs pure synchronous Decimal computation, and returns a list of typed alert events. It must not read from or write to the database. It must not perform async I/O. It must not halt execution, modify positions, influence routing, or touch any upstream component. Alerts are informational only.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi25.md`
4. `docs/PRD-v8.0.md` (Phase 8 / WI-25 section)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/orchestrator.py` — **integration target; `AlertEngine` constructed in `__init__()`, invoked within `_portfolio_aggregation_loop()` after both `compute_snapshot()` and `generate_report()`**
9. `src/schemas/risk.py` (context: `PortfolioSnapshot` from WI-23, `LifecycleReport` + `PositionLifecycleEntry` from WI-24 — add `AlertSeverity` enum and `AlertEvent` model)
10. `src/core/config.py` (target: add 4 alert threshold fields)
11. `src/agents/execution/portfolio_aggregator.py` (context: upstream WI-23 component — NOT modified)
12. `src/agents/execution/lifecycle_reporter.py` (context: upstream WI-24 component — NOT modified)
13. Existing tests:
    - `tests/unit/test_portfolio_aggregator.py`
    - `tests/integration/test_portfolio_aggregator_integration.py`
    - `tests/unit/test_lifecycle_reporter.py`
    - `tests/integration/test_lifecycle_reporter_integration.py`
    - `tests/unit/test_exit_scan_loop.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-25 test files first:
   - `tests/unit/test_alert_engine.py`
   - `tests/integration/test_alert_engine_integration.py`
2. Write failing tests for all required behaviors:

   **Schema validation:**
   - `AlertSeverity` enum exists in `src/schemas/risk.py` with exactly three members: `INFO`, `WARNING`, `CRITICAL`.
   - `AlertEvent` Pydantic model exists in `src/schemas/risk.py`, is frozen, with Decimal-validated fields.
   - `AlertEvent` rejects `float` in `threshold_value` at Pydantic boundary.
   - `AlertEvent` rejects `float` in `actual_value` at Pydantic boundary.
   - `AlertEvent` accepts `AlertSeverity` enum values in `severity` field.

   **Drawdown rule:**
   - `evaluate()` fires `CRITICAL` drawdown alert when `total_unrealized_pnl < -(alert_drawdown_usdc)`.
   - `evaluate()` does NOT fire drawdown alert when `total_unrealized_pnl == -(alert_drawdown_usdc)` (boundary: exactly at threshold is not a breach).
   - `evaluate()` does NOT fire drawdown alert when `total_unrealized_pnl > -(alert_drawdown_usdc)`.
   - Drawdown alert has `rule_name="drawdown"` and `severity=CRITICAL`.

   **Stale-price rule:**
   - `evaluate()` fires `WARNING` stale-price alert when `stale_ratio > alert_stale_price_pct`.
   - `evaluate()` does NOT fire stale-price alert when `stale_ratio == alert_stale_price_pct` (boundary: exactly at threshold is not a breach).
   - `evaluate()` does NOT fire stale-price alert when `position_count == 0` (division-by-zero guard — rule skipped entirely).
   - Stale-price alert has `rule_name="stale_price"` and `severity=WARNING`.

   **Position-count rule:**
   - `evaluate()` fires `WARNING` max-positions alert when `position_count > alert_max_open_positions`.
   - `evaluate()` does NOT fire max-positions alert when `position_count == alert_max_open_positions` (boundary: exactly at threshold is not a breach).
   - Max-positions alert has `rule_name="max_positions"` and `severity=WARNING`.
   - Max-positions alert `threshold_value` and `actual_value` are `Decimal` (not `int`).

   **Loss-rate rule:**
   - `evaluate()` fires `WARNING` loss-rate alert when `losing_count / total_settled_count > alert_loss_rate_pct`.
   - `evaluate()` does NOT fire loss-rate alert when `loss_rate == alert_loss_rate_pct` (boundary: exactly at threshold is not a breach).
   - `evaluate()` does NOT fire loss-rate alert when `total_settled_count == 0` (division-by-zero guard — rule skipped entirely).
   - Loss-rate alert has `rule_name="loss_rate"` and `severity=WARNING`.

   **Multi-rule and edge cases:**
   - `evaluate()` returns empty list when no rules fire (healthy portfolio).
   - `evaluate()` returns multiple alerts when multiple rules fire simultaneously.
   - `evaluate()` returns all 4 alerts when all thresholds are breached.
   - `evaluate()` propagates `dry_run=True` from snapshot into all emitted alerts.
   - `evaluate()` propagates `dry_run=False` from snapshot into all emitted alerts.

   **Config:**
   - `AppConfig` accepts `alert_drawdown_usdc` as `Decimal` with default `Decimal("100")`.
   - `AppConfig` accepts `alert_stale_price_pct` as `Decimal` with default `Decimal("0.50")`.
   - `AppConfig` accepts `alert_max_open_positions` as `int` with default `20`.
   - `AppConfig` accepts `alert_loss_rate_pct` as `Decimal` with default `Decimal("0.60")`.

   **Orchestrator integration:**
   - `AlertEngine` module has no dependency on prompt/context/evaluation/ingestion/database modules (import boundary check).
   - `AlertEngine` is constructed in `Orchestrator.__init__()` — verify `self.alert_engine` attribute exists.
   - `_portfolio_aggregation_loop()` calls `evaluate()` when both snapshot and report succeed.
   - `_portfolio_aggregation_loop()` does NOT call `evaluate()` when `compute_snapshot()` fails.
   - `_portfolio_aggregation_loop()` does NOT call `evaluate()` when `generate_report()` fails.
   - `_portfolio_aggregation_loop()` catches `Exception` from `evaluate()` and does NOT re-raise — loop continues.

3. Run RED tests:
   - `pytest tests/unit/test_alert_engine.py -v`
   - `pytest tests/integration/test_alert_engine_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add `AlertSeverity` and `AlertEvent` to `src/schemas/risk.py`

Target:
- `src/schemas/risk.py` (existing file — append below `LifecycleReport`)

Requirements:
1. Add `from enum import Enum` to imports.
2. `AlertSeverity` is a `str, Enum` with members: `INFO = "INFO"`, `WARNING = "WARNING"`, `CRITICAL = "CRITICAL"`.
3. `AlertEvent` is a frozen Pydantic `BaseModel` with fields:
   - `alert_at_utc: datetime`
   - `severity: AlertSeverity`
   - `rule_name: str`
   - `message: str`
   - `threshold_value: Decimal`
   - `actual_value: Decimal`
   - `dry_run: bool`
4. Add `_reject_float_financials` validator for `threshold_value`, `actual_value` — same pattern as `PortfolioSnapshot`.
5. `model_config = {"frozen": True}`.
6. Do NOT modify `PortfolioSnapshot`, `PositionLifecycleEntry`, or `LifecycleReport`.

Run targeted tests after this step:
```bash
pytest tests/unit/test_alert_engine.py -k "AlertSeverity or AlertEvent or schema" -v
```

### Step 2 — Add Alert Threshold Fields to `src/core/config.py`

Target:
- `src/core/config.py`

Requirements:
1. Add the following fields to `AppConfig` (after the Portfolio Aggregator section):
   ```python
   # --- Alert Engine (WI-25) ---
   alert_drawdown_usdc: Decimal = Field(
       default=Decimal("100"),
       description="USDC drawdown threshold for CRITICAL alert (fires when total_unrealized_pnl < -threshold)",
   )
   alert_stale_price_pct: Decimal = Field(
       default=Decimal("0.50"),
       description="Stale-price ratio threshold for WARNING alert (fires when stale/total > threshold)",
   )
   alert_max_open_positions: int = Field(
       default=20,
       description="Maximum open positions before WARNING alert fires",
   )
   alert_loss_rate_pct: Decimal = Field(
       default=Decimal("0.60"),
       description="Loss rate threshold for WARNING alert (fires when losing/settled > threshold)",
   )
   ```
2. Do NOT modify any existing `AppConfig` fields.

Run targeted tests after this step:
```bash
pytest tests/unit/test_alert_engine.py -k "config or AppConfig" -v
```

### Step 3 — Create `AlertEngine` Module

Target:
- `src/agents/execution/alert_engine.py` (new)

Requirements:
1. New class `AlertEngine` with constructor accepting only `config: AppConfig`.
2. No `db_session_factory`, no `PolymarketClient`, no `TransactionSigner` — this is a pure computation component.
3. Single public method: `def evaluate(self, snapshot: PortfolioSnapshot, report: LifecycleReport) -> list[AlertEvent]`.
4. The method is **synchronous** (`def`, not `async def`) — it performs zero I/O.
5. Compute `now = datetime.now(timezone.utc)` once at the start — shared by all alerts.
6. Read `dry_run = snapshot.dry_run` — used in all emitted `AlertEvent` objects.
7. Evaluate all four rules in **deterministic order**, collecting results into a single list:

   **Rule 1 — Drawdown (CRITICAL):**
   ```python
   _ZERO = Decimal("0")
   neg_threshold = _ZERO - self._config.alert_drawdown_usdc
   if snapshot.total_unrealized_pnl < neg_threshold:
       alerts.append(AlertEvent(
           alert_at_utc=now,
           severity=AlertSeverity.CRITICAL,
           rule_name="drawdown",
           message=f"Portfolio drawdown exceeds {self._config.alert_drawdown_usdc} USDC: unrealized PnL is {snapshot.total_unrealized_pnl} USDC",
           threshold_value=self._config.alert_drawdown_usdc,
           actual_value=snapshot.total_unrealized_pnl,
           dry_run=dry_run,
       ))
   ```
   - No division. Pure Decimal comparison.

   **Rule 2 — Stale Price (WARNING):**
   ```python
   if snapshot.position_count > 0:
       stale_ratio = Decimal(str(snapshot.positions_with_stale_price)) / Decimal(str(snapshot.position_count))
       if stale_ratio > self._config.alert_stale_price_pct:
           alerts.append(AlertEvent(
               alert_at_utc=now,
               severity=AlertSeverity.WARNING,
               rule_name="stale_price",
               message=f"Stale price ratio {stale_ratio} exceeds threshold {self._config.alert_stale_price_pct}",
               threshold_value=self._config.alert_stale_price_pct,
               actual_value=stale_ratio,
               dry_run=dry_run,
           ))
   ```
   - **Division-by-zero guard:** `position_count > 0` gate. Rule skipped entirely when denominator is zero.
   - Ratio computed via `Decimal(str(...))` — no implicit float path.

   **Rule 3 — Position Count (WARNING):**
   ```python
   if snapshot.position_count > self._config.alert_max_open_positions:
       alerts.append(AlertEvent(
           alert_at_utc=now,
           severity=AlertSeverity.WARNING,
           rule_name="max_positions",
           message=f"Open position count {snapshot.position_count} exceeds limit {self._config.alert_max_open_positions}",
           threshold_value=Decimal(str(self._config.alert_max_open_positions)),
           actual_value=Decimal(str(snapshot.position_count)),
           dry_run=dry_run,
       ))
   ```
   - No division. Integer comparison. `threshold_value` and `actual_value` stored as `Decimal` for schema consistency.

   **Rule 4 — Loss Rate (WARNING):**
   ```python
   if report.total_settled_count > 0:
       loss_rate = Decimal(str(report.losing_count)) / Decimal(str(report.total_settled_count))
       if loss_rate > self._config.alert_loss_rate_pct:
           alerts.append(AlertEvent(
               alert_at_utc=now,
               severity=AlertSeverity.WARNING,
               rule_name="loss_rate",
               message=f"Loss rate {loss_rate} exceeds threshold {self._config.alert_loss_rate_pct}",
               threshold_value=self._config.alert_loss_rate_pct,
               actual_value=loss_rate,
               dry_run=dry_run,
           ))
   ```
   - **Division-by-zero guard:** `total_settled_count > 0` gate. Rule skipped entirely when denominator is zero.
   - Ratio computed via `Decimal(str(...))` — no implicit float path.

8. Return the collected `alerts` list (may be empty).
9. All arithmetic is `Decimal`. No `float()` conversion at any step.
10. Structured logging via `structlog` only — no `print()`. Note: the `AlertEngine` itself does NOT log. Logging of alert results is done in the Orchestrator (Step 4). The engine is a pure function — input in, output out.
11. **Zero imports from:**
    - `src/agents/evaluation/*`
    - `src/agents/context/*`
    - `src/agents/ingestion/*`
    - `src/agents/execution/portfolio_aggregator.py`
    - `src/agents/execution/lifecycle_reporter.py`
    - `src/agents/execution/exit_strategy_engine.py`
    - `src/agents/execution/exit_order_router.py`
    - `src/agents/execution/pnl_calculator.py`
    - `src/agents/execution/execution_router.py`
    - `src/agents/execution/order_broadcaster.py`
    - `src/agents/execution/signer.py`
    - `src/agents/execution/bankroll_sync.py`
    - `src/agents/execution/polymarket_client.py`
    - `src/db/*` (any repository, model, or session factory)
    - `sqlalchemy` (any module)

Run targeted tests after this step:
```bash
pytest tests/unit/test_alert_engine.py -v
```

### Step 4 — Integrate into Orchestrator

Target:
- `src/orchestrator.py`

Requirements:
1. **Add import:** `from src/agents/execution/alert_engine import AlertEngine`
2. **Constructor wiring:** Construct `AlertEngine(config=self.config)` in `Orchestrator.__init__()`, after `self.lifecycle_reporter` construction. Assign to `self.alert_engine`.
3. **Modify `_portfolio_aggregation_loop()` to capture return values and invoke AlertEngine.**

   The existing loop body discards the return values of `compute_snapshot()` and `generate_report()`. WI-25 requires capturing them into local variables so they can be passed to `evaluate()`.

   Updated loop body:
   ```python
   async def _portfolio_aggregation_loop(self) -> None:
       """Periodic portfolio snapshot, lifecycle report, and alert evaluation (WI-23/24/25)."""
       while True:
           await asyncio.sleep(
               float(self.config.portfolio_aggregation_interval_sec)
           )
           snapshot: PortfolioSnapshot | None = None
           report: LifecycleReport | None = None

           try:
               snapshot = await self.portfolio_aggregator.compute_snapshot()
           except Exception as exc:
               logger.error(
                   "portfolio_aggregation_loop.error",
                   error=str(exc),
               )

           try:
               report = await self.lifecycle_reporter.generate_report()
           except Exception as exc:
               logger.error(
                   "lifecycle_report_loop.error",
                   error=str(exc),
               )

           if snapshot is not None and report is not None:
               try:
                   alerts = self.alert_engine.evaluate(snapshot, report)
                   if alerts:
                       logger.warning(
                           "alert_engine.alerts_fired",
                           alert_count=len(alerts),
                           rules=[a.rule_name for a in alerts],
                           severities=[a.severity.value for a in alerts],
                           dry_run=snapshot.dry_run,
                       )
                   else:
                       logger.info(
                           "alert_engine.all_clear",
                           dry_run=snapshot.dry_run,
                       )
               except Exception as exc:
                   logger.error(
                       "alert_engine.error",
                       error=str(exc),
                   )
   ```

4. **Critical design constraints for the loop modification:**
   - `snapshot` and `report` are declared as `Optional` (`None` initial) before the try blocks.
   - `compute_snapshot()` and `generate_report()` return values are captured (previously discarded).
   - `evaluate()` is called ONLY when both `snapshot is not None` and `report is not None`.
   - If either upstream call fails, alert evaluation is **skipped for that cycle** — not errored.
   - Alert evaluation failure is caught independently in its own `try/except` block.
   - The `except Exception` block for `evaluate()` logs via `alert_engine.error` and does NOT re-raise.
   - When alerts fire: `alert_engine.alerts_fired` emitted at `WARNING` level with `alert_count`, `rules`, `severities`, `dry_run`.
   - When no alerts fire: `alert_engine.all_clear` emitted at `INFO` level with `dry_run`.
5. **Add necessary type imports** at the top of `orchestrator.py`:
   - `from src.schemas.risk import PortfolioSnapshot, LifecycleReport` (for type annotations in the loop)
6. **No new `asyncio.create_task()`.** `AlertEngine` is invoked inline within the existing loop.
7. **No new config gate.** `AlertEngine` runs whenever `_portfolio_aggregation_loop()` runs (i.e., when `enable_portfolio_aggregator=True`).
8. **Shutdown:** No changes needed. `AlertEngine` is invoked inline (not a separate task), so it terminates when the loop's task is cancelled.
9. **Task count unchanged:** When `enable_portfolio_aggregator=True`, `self._tasks` still contains 7 entries (same as after WI-23/24). When `False`, 6 entries.

Run targeted tests after this step:
```bash
pytest tests/integration/test_alert_engine_integration.py -v
pytest tests/integration/test_orchestrator.py -v
```

### Step 5 — GREEN Validation

Run:
```bash
pytest tests/unit/test_alert_engine.py -v
pytest tests/integration/test_alert_engine_integration.py -v
pytest tests/unit/test_portfolio_aggregator.py -v
pytest tests/unit/test_lifecycle_reporter.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. **Read-only analytics / zero I/O.** `AlertEngine` performs zero DB reads, zero DB writes, and zero network calls. It accepts typed Pydantic models and returns typed Pydantic models. No `AsyncSession`, no `PolymarketClient`, no `httpx`, no `aiohttp`. The `evaluate()` method is synchronous.
2. **Decimal financial integrity.** All threshold comparisons (drawdown) and ratio computations (stale-price, loss-rate) are `Decimal`-only. Float is rejected at Pydantic boundary via `AlertEvent` validators. No float intermediary in any arithmetic step. Integer-to-Decimal conversion uses `Decimal(str(...))`.
3. **Division-by-zero guards.** Stale-price rule is skipped when `snapshot.position_count == 0`. Loss-rate rule is skipped when `report.total_settled_count == 0`. No `ZeroDivisionError` is possible under any input combination.
4. **No bypass of `LLMEvaluationResponse` terminal Gatekeeper.** `AlertEngine` operates far downstream: it is a passive observer of pre-computed analytics snapshots. It has no path to execution.
5. **Observational only — no execution influence.** Alerts do NOT halt execution, pause the pipeline, trigger exits, modify positions, adjust thresholds, or influence routing decisions. They are informational structlog events only.
6. **Module isolation.** Zero imports from prompt, context, evaluation, ingestion, or database modules. Zero imports from `PortfolioAggregator`, `PositionLifecycleReporter`, `ExitStrategyEngine`, `ExitOrderRouter`, `PnLCalculator`, `ExecutionRouter`, `OrderBroadcaster`, `TransactionSigner`, `BankrollSyncProvider`, `PolymarketClient`, any repository, any ORM model, or `sqlalchemy`.
7. **Fail-open loop semantics.** A failed `evaluate()` call within the orchestrator loop is caught, logged via `alert_engine.error`, and does NOT re-raise or terminate the loop. Independent of `compute_snapshot()` and `generate_report()` error handling.
8. **Both-inputs-required guard.** `evaluate()` is only called when both `snapshot` and `report` are non-None. A failed upstream computation causes alert evaluation to be skipped (not errored) for that cycle.
9. **No new periodic task.** `AlertEngine` is invoked inline within `_portfolio_aggregation_loop()`, not as a separate `asyncio.create_task()`. Task count is unchanged from WI-23/24.
10. **Shutdown preserved.** No additional shutdown code needed. The engine terminates when its parent task is cancelled.
11. **No queue topology changes.** `market_queue -> prompt_queue -> execution_queue`. No new queue introduced.
12. **Synchronous evaluate method.** `evaluate()` is `def`, not `async def`. It performs no I/O and returns immediately. It is safe to call from within an async context without blocking.
13. **Deterministic rule evaluation order.** Rules are always evaluated in the order: drawdown -> stale_price -> max_positions -> loss_rate. One rule firing does not short-circuit others. A single call may return 0 to 4 alerts.
14. **No database schema changes.** Zero new tables, zero new columns, zero Alembic migrations. `AlertEngine` does not touch the database at all.
15. **Frozen upstream components.** `PortfolioAggregator`, `PositionLifecycleReporter`, `PnLCalculator`, `ExitStrategyEngine`, `ExitOrderRouter`, `ExecutionRouter`, `PolymarketClient`, `OrderBroadcaster`, `PositionTracker`, `PositionRepository`, and all schemas in `src/schemas/execution.py`, `src/schemas/position.py` are byte-identical before and after WI-25. The only modified schemas file is `src/schemas/risk.py` (additive: `AlertSeverity`, `AlertEvent`).
16. **`dry_run` behavior is passthrough.** The `dry_run` flag is read from `snapshot.dry_run` and included in each `AlertEvent` for audit context. No new `dry_run` gate is needed because the component is inherently read-only and I/O-free.

---

## Required Test Matrix

At minimum, WI-25 tests must prove:

### Unit Tests — Schema Validation
1. `AlertSeverity` enum has exactly three members: `INFO`, `WARNING`, `CRITICAL`.
2. `AlertEvent` accepts valid `Decimal` values in `threshold_value` and `actual_value`, and is frozen (immutable after construction).
3. `AlertEvent` rejects `float` in `threshold_value` at Pydantic boundary.
4. `AlertEvent` rejects `float` in `actual_value` at Pydantic boundary.
5. `AlertEvent` accepts `AlertSeverity` enum values in `severity` field.

### Unit Tests — Drawdown Rule
6. `evaluate()` fires `CRITICAL` drawdown alert when `total_unrealized_pnl` is below `-(alert_drawdown_usdc)`.
7. `evaluate()` does NOT fire drawdown alert when `total_unrealized_pnl == -(alert_drawdown_usdc)` (boundary: exactly at threshold is not a breach).
8. `evaluate()` does NOT fire drawdown alert when `total_unrealized_pnl > -(alert_drawdown_usdc)`.
9. Drawdown alert has `rule_name="drawdown"` and `severity=CRITICAL`.

### Unit Tests — Stale-Price Rule
10. `evaluate()` fires `WARNING` stale-price alert when `stale_ratio > alert_stale_price_pct`.
11. `evaluate()` does NOT fire stale-price alert when `stale_ratio == alert_stale_price_pct` (boundary: exactly at threshold is not a breach).
12. `evaluate()` does NOT fire stale-price alert when `position_count == 0` (division-by-zero guard — rule skipped, no error).
13. Stale-price alert has `rule_name="stale_price"` and `severity=WARNING`.

### Unit Tests — Position-Count Rule
14. `evaluate()` fires `WARNING` max-positions alert when `position_count > alert_max_open_positions`.
15. `evaluate()` does NOT fire max-positions alert when `position_count == alert_max_open_positions` (boundary: exactly at threshold is not a breach).
16. `evaluate()` does NOT fire max-positions alert when `position_count < alert_max_open_positions`.
17. Max-positions alert has `rule_name="max_positions"` and `severity=WARNING`.
18. Max-positions alert `threshold_value` and `actual_value` are `Decimal` (not `int`).

### Unit Tests — Loss-Rate Rule
19. `evaluate()` fires `WARNING` loss-rate alert when `losing_count / total_settled_count > alert_loss_rate_pct`.
20. `evaluate()` does NOT fire loss-rate alert when `loss_rate == alert_loss_rate_pct` (boundary: exactly at threshold is not a breach).
21. `evaluate()` does NOT fire loss-rate alert when `total_settled_count == 0` (division-by-zero guard — rule skipped, no error).
22. Loss-rate alert has `rule_name="loss_rate"` and `severity=WARNING`.

### Unit Tests — Multi-Rule and Edge Cases
23. `evaluate()` returns empty list when no rules fire (healthy portfolio state).
24. `evaluate()` returns multiple alerts when multiple rules fire simultaneously.
25. `evaluate()` returns all 4 alerts when all thresholds are breached.
26. `evaluate()` propagates `dry_run=True` from snapshot into all emitted alerts.
27. `evaluate()` propagates `dry_run=False` from snapshot into all emitted alerts.

### Unit Tests — Config
28. `AppConfig` accepts `alert_drawdown_usdc` as `Decimal` with default `Decimal("100")`.
29. `AppConfig` accepts `alert_stale_price_pct` as `Decimal` with default `Decimal("0.50")`.
30. `AppConfig` accepts `alert_max_open_positions` as `int` with default `20`.
31. `AppConfig` accepts `alert_loss_rate_pct` as `Decimal` with default `Decimal("0.60")`.

### Integration Tests
32. `AlertEngine` module has no dependency on prompt/context/evaluation/ingestion/database modules (import boundary check).
33. `AlertEngine` is constructed in `Orchestrator.__init__()` — verify `self.alert_engine` attribute exists.
34. `_portfolio_aggregation_loop()` calls `evaluate()` when both snapshot and report succeed — verify `alert_engine.alerts_fired` or `alert_engine.all_clear` log event is emitted.
35. `_portfolio_aggregation_loop()` does NOT call `evaluate()` when `compute_snapshot()` fails — verify no alert log events.
36. `_portfolio_aggregation_loop()` does NOT call `evaluate()` when `generate_report()` fails — verify no alert log events.
37. `_portfolio_aggregation_loop()` catches `Exception` from `evaluate()` and does NOT re-raise — loop continues to next iteration.
38. Full `evaluate()` with realistic `PortfolioSnapshot` and `LifecycleReport` objects — alerts match expected rules end-to-end.

### Regression Gate
39. Full suite: `pytest --asyncio-mode=auto tests/ -q` — all tests pass.
40. Coverage: `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` — >= 80%.

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
You are the MAAP Checker for WI-25 (Alert Engine) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi25.md
2) docs/PRD-v8.0.md (WI-25 section)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Read-only violation (any DB read, DB write, session creation, repository call, or network I/O within AlertEngine)
- Decimal violations (any float usage in threshold comparison, ratio computation, or AlertEvent construction)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation)
- Division-by-zero (position_count == 0 or total_settled_count == 0 causing ZeroDivisionError instead of rule skip)
- Execution influence (AlertEngine halting execution, modifying positions, triggering exits, adjusting thresholds, or influencing routing)
- Isolation violations (AlertEngine importing prompt, context, evaluation, ingestion, database, PortfolioAggregator, PositionLifecycleReporter, ExitStrategyEngine, ExitOrderRouter, PnLCalculator, ExecutionRouter, OrderBroadcaster, TransactionSigner, BankrollSyncProvider, PolymarketClient, or sqlalchemy modules)
- Loop safety (evaluate() failure killing the orchestrator loop; evaluate() called when snapshot or report is None)
- Synchronous contract (evaluate() declared as async def instead of def)
- Rule order (rules not evaluated in deterministic order: drawdown -> stale_price -> max_positions -> loss_rate)
- Short-circuit (one rule firing preventing subsequent rules from being evaluated)
- Task count regression (new asyncio.create_task for AlertEngine — should be inline only)
- Upstream mutation (any modification to PortfolioAggregator, PositionLifecycleReporter, PnLCalculator, ExitStrategyEngine, ExitOrderRouter, ExecutionRouter, PolymarketClient, PositionTracker, PositionRepository, OrderBroadcaster, or existing schemas in execution.py/position.py)
- Regression (any modification to existing tests or coverage < 80%)

Additional required checks:
- AlertEngine class exists in src/agents/execution/alert_engine.py
- evaluate(snapshot: PortfolioSnapshot, report: LifecycleReport) -> list[AlertEvent] is the sole public method
- evaluate() is synchronous (def, not async def)
- AlertSeverity enum exists in src/schemas/risk.py with INFO, WARNING, CRITICAL
- AlertEvent Pydantic model exists in src/schemas/risk.py, is frozen, with float-rejecting validators for threshold_value and actual_value
- AlertEngine constructor accepts only config: AppConfig — no db_session_factory, no clients
- Drawdown rule: CRITICAL severity, rule_name="drawdown", fires when total_unrealized_pnl < -(alert_drawdown_usdc)
- Stale-price rule: WARNING severity, rule_name="stale_price", division guard on position_count == 0
- Position-count rule: WARNING severity, rule_name="max_positions", threshold_value and actual_value as Decimal
- Loss-rate rule: WARNING severity, rule_name="loss_rate", division guard on total_settled_count == 0
- Boundary tests: exactly-at-threshold does NOT fire (strict inequality, not <=/>= )
- AppConfig.alert_drawdown_usdc: Decimal, default Decimal("100")
- AppConfig.alert_stale_price_pct: Decimal, default Decimal("0.50")
- AppConfig.alert_max_open_positions: int, default 20
- AppConfig.alert_loss_rate_pct: Decimal, default Decimal("0.60")
- AlertEngine constructed in Orchestrator.__init__() with config only
- _portfolio_aggregation_loop() captures return values of compute_snapshot() and generate_report()
- evaluate() called only when both snapshot and report are non-None
- evaluate() failure handled independently from compute_snapshot() and generate_report() failures
- alert_engine.alerts_fired structlog event at WARNING level with alert_count, rules, severities, dry_run
- alert_engine.all_clear structlog event at INFO level with dry_run
- alert_engine.error structlog event at ERROR level with error
- No new asyncio.create_task — AlertEngine is inline within existing loop
- Task count unchanged: 7 when enable_portfolio_aggregator=True, 6 when False
- Zero new database tables, columns, or Alembic migrations
- Zero new queues
- Queue topology unchanged: market_queue -> prompt_queue -> execution_queue
- dry_run flag in AlertEvent sourced from snapshot.dry_run
- All four rules evaluated on every call — no short-circuit on first fire
- Deterministic order: drawdown -> stale_price -> max_positions -> loss_rate

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-25/invariants
4) Explicit statement on each MAAP critical category:
   - Read-only violation: CLEARED/FLAGGED
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Division-by-zero: CLEARED/FLAGGED
   - Execution influence: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
   - Loop safety: CLEARED/FLAGGED
   - Synchronous contract: CLEARED/FLAGGED
   - Rule order: CLEARED/FLAGGED
   - Short-circuit: CLEARED/FLAGGED
   - Task count regression: CLEARED/FLAGGED
   - Upstream mutation: CLEARED/FLAGGED
   - Regression: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
