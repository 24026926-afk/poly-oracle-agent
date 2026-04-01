# P27-WI-27 — Global Circuit Breaker Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-4.1 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi27-circuit-breaker` (branched from current `feat/wi26-telegram-sink`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/security-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-27 for Phase 9: a stateful, synchronous, in-memory Global Circuit Breaker (`CircuitBreaker`) that gates the Entry Path (BUY routing) when the `AlertEngine` (WI-25) fires a CRITICAL drawdown alert. The breaker sits BEFORE `ExecutionRouter.route()` in `_execution_consumer_loop()` and short-circuits BUY orders with a typed `ExecutionResult(action=SKIP, reason="circuit_breaker_open")` when tripped.

This WI introduces one new stateful gate component. It is strictly synchronous, in-memory, and performs zero I/O. The breaker is a trip-and-hold latch — NOT a classical circuit breaker with half-open states or auto-recovery. Once tripped, the breaker remains OPEN until explicit human intervention: either toggling `circuit_breaker_override_closed=True` in `.env` or invoking `reset()` programmatically.

The Exit Path (`_exit_scan_loop()`: exit evaluation, SELL routing, PnL settlement) is NEVER gated by the circuit breaker. When the breaker is tripped, the bot stops buying but continues selling to protect the remaining bankroll. This is the core defensive posture.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi27.md`
4. `docs/PRD-v9.0.md` (Phase 9 / WI-27 section)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `src/orchestrator.py` — **integration target; `CircuitBreaker` config-gated in `__init__()`, `evaluate_alerts()` invoked in `_portfolio_aggregation_loop()`, `check_entry_allowed()` invoked in `_execution_consumer_loop()`**
8. `src/schemas/risk.py` (context: `AlertEvent`, `AlertSeverity` from WI-25 — consumed, NOT modified)
9. `src/schemas/execution.py` (context: `ExecutionResult`, `ExecutionAction` — consumed, NOT modified)
10. `src/core/config.py` (target: add 2 new circuit breaker fields)
11. `src/agents/execution/alert_engine.py` (context: upstream WI-25 component — NOT modified)
12. `src/agents/execution/telegram_notifier.py` (context: downstream WI-26 component — NOT modified)
13. Existing tests:
    - `tests/unit/test_alert_engine.py`
    - `tests/unit/test_telegram_notifier.py`
    - `tests/unit/test_exit_scan_loop.py`
    - `tests/integration/test_alert_engine_integration.py`
    - `tests/integration/test_telegram_notifier_integration.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-27 test files first:
   - `tests/unit/test_circuit_breaker.py`
   - `tests/integration/test_circuit_breaker_integration.py`
2. Write failing tests for all required behaviors:

   **State initialization:**
   - Newly constructed `CircuitBreaker` has `state == CircuitBreakerState.CLOSED`.
   - `check_entry_allowed()` returns `True` on a freshly constructed breaker.

   **State transitions — tripping:**
   - `evaluate_alerts([critical_drawdown_alert])` transitions state from `CLOSED` to `OPEN`.
   - `check_entry_allowed()` returns `False` after the breaker is tripped.
   - Tripping an already-OPEN breaker is idempotent — state remains `OPEN`, no duplicate `circuit_breaker.tripped` structlog event emitted.

   **Alert filtering — only CRITICAL drawdown trips:**
   - `evaluate_alerts([warning_drawdown_alert])` does NOT trip — state remains `CLOSED`.
   - `evaluate_alerts([critical_stale_price_alert])` does NOT trip — state remains `CLOSED` (CRITICAL but wrong `rule_name`).
   - `evaluate_alerts([warning_max_positions_alert])` does NOT trip — state remains `CLOSED`.
   - `evaluate_alerts([])` (empty list) does NOT change state from `CLOSED`.
   - Mixed alert list `[WARNING, CRITICAL drawdown, INFO]` → trips on the CRITICAL drawdown.

   **State transitions — reset:**
   - `reset()` transitions state from `OPEN` to `CLOSED`.
   - `reset()` on an already-CLOSED breaker is idempotent — no error, logs `circuit_breaker.reset`.

   **Override flag:**
   - `circuit_breaker_override_closed=True` + call `evaluate_alerts()` → state becomes `CLOSED`.
   - After override is processed, `config.circuit_breaker_override_closed` is `False` (auto-reset in memory).
   - Override + CRITICAL drawdown alert in same call → override wins, state is `CLOSED` (alert not processed in same cycle).
   - Override on an already-CLOSED breaker → logs `circuit_breaker.override_applied`, state stays `CLOSED`.

   **structlog audit events:**
   - `circuit_breaker.tripped` event emitted on CLOSED→OPEN transition with `rule_name`, `severity`, `alert_message` fields.
   - `circuit_breaker.reset` event emitted on `reset()` call.
   - `circuit_breaker.override_applied` event emitted when override flag forces CLOSED.

   **State property:**
   - `state` property returns current `CircuitBreakerState` value.

   **Config gating:**
   - `AppConfig.enable_circuit_breaker` is `bool` with default `False`.
   - `AppConfig.circuit_breaker_override_closed` is `bool` with default `False`.

   **Orchestrator integration — config gating:**
   - When `enable_circuit_breaker=False`, `Orchestrator` sets `self.circuit_breaker = None`.
   - When `enable_circuit_breaker=True`, `Orchestrator` sets `self.circuit_breaker` to a `CircuitBreaker` instance with initial state `CLOSED`.

   **Orchestrator integration — entry gate (BUY blocking):**
   - When the breaker is tripped (OPEN), `_execution_consumer_loop()` skips the item with `ExecutionResult(action=ExecutionAction.SKIP, reason="circuit_breaker_open")` — `ExecutionRouter.route()` is NOT called.
   - When the breaker is CLOSED, `_execution_consumer_loop()` routes normally — `ExecutionRouter.route()` IS called.
   - Position tracking still records the SKIP result when the circuit breaker blocks an entry.
   - `circuit_breaker.entry_blocked` structlog event emitted with `condition_id` when a BUY is blocked.

   **Orchestrator integration — exit path passthrough:**
   - When the breaker is tripped (OPEN), `_exit_scan_loop()` still executes — `ExitStrategyEngine.scan_open_positions()` is called and SELL orders are routed. The circuit breaker does NOT gate exits.

   **Orchestrator integration — aggregation loop wiring:**
   - `_portfolio_aggregation_loop()` calls `circuit_breaker.evaluate_alerts(alerts)` after `AlertEngine.evaluate()` returns alerts.
   - `_portfolio_aggregation_loop()` calls `circuit_breaker.evaluate_alerts([])` even when no alerts fire (to process override flag).
   - When the breaker trips during aggregation, `TelegramNotifier.send_execution_event()` is called with a message containing `"CIRCUIT BREAKER TRIPPED"`.

   **Orchestrator integration — breaker=None safety:**
   - When `enable_circuit_breaker=False` and `self.circuit_breaker is None`, `_execution_consumer_loop()` routes directly to `ExecutionRouter` with no `AttributeError`.

   **Module isolation:**
   - `CircuitBreaker` module has no dependency on prompt/context/evaluation/ingestion/database modules (import boundary check).

3. Run RED tests:
   - `pytest tests/unit/test_circuit_breaker.py -v`
   - `pytest tests/integration/test_circuit_breaker_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add Circuit Breaker Config Fields to `src/core/config.py`

Target:
- `src/core/config.py`

Requirements:
1. Add the following fields to `AppConfig` after the `# --- Telegram Notifier (WI-26) ---` block:
   ```python
   # --- Circuit Breaker (WI-27) ---
   enable_circuit_breaker: bool = Field(
       default=False,
       description="Enable global circuit breaker to halt BUY routing on CRITICAL drawdown alerts",
   )
   circuit_breaker_override_closed: bool = Field(
       default=False,
       description="Force circuit breaker to CLOSED state on next evaluate_alerts() call (one-shot override)",
   )
   ```
2. Do NOT modify any existing `AppConfig` fields.

Run targeted tests after this step:
```bash
pytest tests/unit/test_circuit_breaker.py -k "config or AppConfig" -v
```

### Step 2 — Create `CircuitBreaker` Module

Target:
- `src/agents/execution/circuit_breaker.py` (new)

Requirements:
1. New enum `CircuitBreakerState`:
   ```python
   from enum import Enum

   class CircuitBreakerState(str, Enum):
       CLOSED = "CLOSED"   # Normal operation — BUY routing allowed
       OPEN = "OPEN"       # Tripped — BUY routing forbidden
   ```

2. New class `CircuitBreaker` with constructor:
   ```python
   def __init__(self, config: AppConfig) -> None:
   ```

3. Constructor must:
   - Store `self._config = config`
   - Initialize `self._state = CircuitBreakerState.CLOSED`
   - Bind `self._log = structlog.get_logger(__name__)`

4. Three public synchronous methods + one property:

**`check_entry_allowed` implementation:**
   ```python
   def check_entry_allowed(self) -> bool:
       return self._state == CircuitBreakerState.CLOSED
   ```
   - Pure state read. No logging, no side effects, no I/O.

**`evaluate_alerts` implementation:**
   ```python
   def evaluate_alerts(self, alerts: list[AlertEvent]) -> None:
   ```
   1. **Override check (first priority):** If `self._config.circuit_breaker_override_closed is True`:
      - Set `self._state = CircuitBreakerState.CLOSED`.
      - Log `circuit_breaker.override_applied` at INFO level.
      - Set `self._config.circuit_breaker_override_closed = False` (one-shot auto-reset).
      - `return` — do NOT evaluate alerts in the same cycle.
   2. **Alert scan:** Iterate over `alerts`. For each alert, check:
      - `alert.severity == AlertSeverity.CRITICAL` AND `alert.rule_name == "drawdown"`
   3. **Trip:** If a matching alert is found AND `self._state == CircuitBreakerState.CLOSED`:
      - Set `self._state = CircuitBreakerState.OPEN`.
      - Log `circuit_breaker.tripped` at CRITICAL level with fields: `rule_name=alert.rule_name`, `severity=alert.severity.value`, `alert_message=alert.message`.
      - `break` — one trip is sufficient.
   4. **Idempotency:** If a matching alert is found but breaker is already OPEN — do nothing. No duplicate log.
   5. **No match:** If no alert matches the CRITICAL+drawdown filter — do nothing.

**`reset` implementation:**
   ```python
   def reset(self) -> None:
       self._state = CircuitBreakerState.CLOSED
       self._log.info("circuit_breaker.reset")
   ```
   - Idempotent: calling on already-CLOSED breaker logs and returns without error.

**`state` property:**
   ```python
   @property
   def state(self) -> CircuitBreakerState:
       return self._state
   ```

5. **ALL methods are synchronous.** No `async def`, no `await`, no I/O. This is a pure in-memory state machine.

6. **Allowed imports (exhaustive):**
   - `from enum import Enum`
   - `import structlog`
   - `from src.schemas.risk import AlertEvent, AlertSeverity`
   - `from src.core.config import AppConfig`

7. **Zero imports from (enforced in tests):**
   - `src/agents/evaluation/*`
   - `src/agents/context/*`
   - `src/agents/ingestion/*`
   - `src/agents/execution/alert_engine.py`
   - `src/agents/execution/portfolio_aggregator.py`
   - `src/agents/execution/lifecycle_reporter.py`
   - `src/agents/execution/exit_strategy_engine.py`
   - `src/agents/execution/exit_order_router.py`
   - `src/agents/execution/pnl_calculator.py`
   - `src/agents/execution/execution_router.py`
   - `src/agents/execution/telegram_notifier.py`
   - `src/agents/execution/broadcaster.py`
   - `src/agents/execution/signer.py`
   - `src/agents/execution/bankroll_sync.py`
   - `src/agents/execution/polymarket_client.py`
   - `src/db/*` (any repository, model, or session factory)
   - `sqlalchemy` (any module)
   - `asyncio`, `httpx`, `aiohttp`

Run targeted tests after this step:
```bash
pytest tests/unit/test_circuit_breaker.py -v
```

### Step 3 — Integrate into Orchestrator

Target:
- `src/orchestrator.py`

Requirements:

#### 3a — Import and Constructor Wiring

1. **Add imports:**
   ```python
   from src.agents.execution.circuit_breaker import CircuitBreaker, CircuitBreakerState
   ```
2. **Constructor wiring:** After the `self.telegram_notifier` construction block (after line 151 — the `logger.info("telegram.disabled")` line), add:
   ```python
   # WI-27: Circuit Breaker (config-gated)
   self.circuit_breaker: CircuitBreaker | None = None
   if self.config.enable_circuit_breaker:
       self.circuit_breaker = CircuitBreaker(config=self.config)
   else:
       logger.info("circuit_breaker.disabled")
   ```
3. **Key design decision:** `CircuitBreaker` has no external dependencies — no HTTP client, no DB session. Construction is trivial. It follows TelegramNotifier in the constructor chain because it logically depends on WI-25 (AlertEngine) outputs and will interact with WI-26 (TelegramNotifier) for trip notifications.

#### 3b — Wire into `_portfolio_aggregation_loop()` — Breaker Evaluation

**Location:** Inside the existing `if snapshot is not None and report is not None:` block, within the `try:` block that wraps `AlertEngine.evaluate()`.

After the existing Telegram notification loop (the `if self.telegram_notifier is not None: for alert in alerts:` block, ending around line 525), add circuit breaker evaluation:

```python
# WI-27: Evaluate alerts for circuit breaker trip
if self.circuit_breaker is not None:
    try:
        previous_state = self.circuit_breaker.state
        self.circuit_breaker.evaluate_alerts(alerts)
        # Send Telegram notification if breaker just tripped
        if (
            previous_state == CircuitBreakerState.CLOSED
            and self.circuit_breaker.state == CircuitBreakerState.OPEN
            and self.telegram_notifier is not None
        ):
            try:
                await self.telegram_notifier.send_execution_event(
                    summary=(
                        "CIRCUIT BREAKER TRIPPED: "
                        "BUY routing halted due to CRITICAL drawdown alert. "
                        "Manual reset required."
                    ),
                    dry_run=self.config.dry_run,
                )
            except Exception:
                pass  # send_execution_event already swallows
    except Exception as exc:
        logger.error(
            "circuit_breaker.evaluate_error",
            error=str(exc),
        )
```

**Also wire the `else` branch (all_clear path).** After the existing `else: logger.info("alert_engine.all_clear", ...)` block, add:

```python
# WI-27: Still evaluate override flag even when no alerts fire
if self.circuit_breaker is not None:
    try:
        self.circuit_breaker.evaluate_alerts([])
    except Exception as exc:
        logger.error(
            "circuit_breaker.evaluate_error",
            error=str(exc),
        )
```

**Design rationale:** The override flag must be processable during all-clear cycles. If the operator sets `circuit_breaker_override_closed=True` and restarts, the next aggregation cycle (which may have no alerts) must still process the override and reset the breaker to CLOSED.

#### 3c — Wire into `_execution_consumer_loop()` — The Entry Gate

**Location:** BEFORE the existing `execution_result = await self.execution_router.route(...)` call (line 265 in the current orchestrator). Replace the direct `route()` call with a circuit breaker gate:

```python
# WI-27: Gate entry path on circuit breaker state
if self.circuit_breaker is not None and not self.circuit_breaker.check_entry_allowed():
    logger.warning(
        "circuit_breaker.entry_blocked",
        condition_id=condition_id,
    )
    execution_result = ExecutionResult(
        action=ExecutionAction.SKIP,
        reason="circuit_breaker_open",
    )
else:
    execution_result = await self.execution_router.route(
        response=eval_resp,
        market_context=eval_resp.market_context,
    )
```

**IMPORTANT — Move `condition_id` extraction ABOVE the gate.** The current code extracts `condition_id` on line 271 (after `route()`). For the circuit breaker gate log to include `condition_id`, move this extraction BEFORE the gate:

```python
# Current order:
#   1. route()       ← line 265
#   2. condition_id  ← line 271
#
# New order:
#   1. condition_id  ← FIRST (needed for gate log)
#   2. gate check    ← circuit breaker
#   3. route() or SKIP
```

So the full revised sequence in `_execution_consumer_loop()` becomes:

```python
eval_resp = item.get("evaluation")
if eval_resp is None:
    logger.error("execution.missing_evaluation")
    continue

condition_id = str(eval_resp.market_context.condition_id)  # Moved up for WI-27

# WI-27: Gate entry path on circuit breaker state
if self.circuit_breaker is not None and not self.circuit_breaker.check_entry_allowed():
    logger.warning(
        "circuit_breaker.entry_blocked",
        condition_id=condition_id,
    )
    execution_result = ExecutionResult(
        action=ExecutionAction.SKIP,
        reason="circuit_breaker_open",
    )
else:
    execution_result = await self.execution_router.route(
        response=eval_resp,
        market_context=eval_resp.market_context,
    )
item["execution_result"] = execution_result

# Position tracking continues below (unchanged) — records SKIP result for audit trail
```

**Critical behaviors after the gate:**
1. `item["execution_result"] = execution_result` — stored regardless of gate outcome.
2. The position tracking block (lines 273–291) still executes, recording the SKIP result. This preserves the audit trail for blocked entries.
3. The Telegram BUY notification block (lines 293–313) fires ONLY for `EXECUTED` and `DRY_RUN` actions — it will NOT fire for `SKIP`, which is correct. Blocked entries should not trigger "BUY ROUTED" notifications.
4. The `dry_run` skip gate (lines 315–327) fires ONLY for dry-run results — it will NOT fire for circuit breaker SKIP results, which is correct.

#### 3d — Exit Path (`_exit_scan_loop()`) — NO CHANGES

`_exit_scan_loop()` (lines 379–485) is NOT modified. The circuit breaker does not gate:
- `ExitStrategyEngine.scan_open_positions()`
- `ExitOrderRouter.route_exit()`
- `PnLCalculator.settle()`
- SELL Telegram notifications
- SELL order broadcasting

This is the most critical invariant: **stop buying, keep selling.**

#### 3e — Shutdown — NO CHANGES

No shutdown logic required. `CircuitBreaker` has no external resources (no HTTP client, no DB session, no file handles). In-memory state is discarded on process exit.

4. **No new `asyncio.create_task()`.** `CircuitBreaker` is invoked inline within existing loops. No new periodic task, no new queue.
5. **No new config gate on loop registration.** `CircuitBreaker` calls are guarded by `if self.circuit_breaker is not None` inside each relevant loop.
6. **Task count unchanged:** 7 when `enable_portfolio_aggregator=True`, 6 when `False`.

Run targeted tests after this step:
```bash
pytest tests/integration/test_circuit_breaker_integration.py -v
pytest tests/integration/test_orchestrator.py -v
```

### Step 4 — GREEN Validation

Run:
```bash
pytest tests/unit/test_circuit_breaker.py -v
pytest tests/integration/test_circuit_breaker_integration.py -v
pytest tests/unit/test_alert_engine.py -v
pytest tests/unit/test_telegram_notifier.py -v
pytest tests/integration/test_alert_engine_integration.py -v
pytest tests/integration/test_telegram_notifier_integration.py -v
pytest tests/unit/test_exit_scan_loop.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. **Entry Path gating ONLY.** The circuit breaker gates ONLY BUY routing in `_execution_consumer_loop()`. The Exit Path (`_exit_scan_loop()`: exit evaluation, SELL routing, PnL settlement, SELL broadcasting, SELL Telegram notifications) is NEVER gated by the circuit breaker, regardless of breaker state. This is the single most critical invariant of WI-27.
2. **Fail-secure at the gate.** If the `check_entry_allowed()` call or its surrounding logic raises unexpectedly, the Orchestrator's outer `try/except Exception` in `_execution_consumer_loop()` catches it and the item is not routed. The breaker defaults to blocking, not allowing, on unexpected errors.
3. **Typed rejection.** When the breaker blocks a BUY, the result is `ExecutionResult(action=ExecutionAction.SKIP, reason="circuit_breaker_open")` — never a silent drop, untyped rejection, or bare `continue`. The audit trail (position tracking) always records the block.
4. **No auto-recovery.** The breaker does NOT auto-recover. Transition from OPEN → CLOSED requires explicit human intervention: `circuit_breaker_override_closed=True` in `.env` (processed on next `evaluate_alerts()` call) or programmatic `reset()`. No timers, no cooldowns, no half-open states.
5. **Narrow trip condition.** The breaker trips ONLY on `AlertSeverity.CRITICAL` alerts with `rule_name == "drawdown"`. No other alert combination affects breaker state. WARNING-level alerts and non-drawdown CRITICAL alerts do not trip the breaker.
6. **In-memory only.** Breaker state is a single `CircuitBreakerState` attribute. No DB persistence, no file persistence. Process restart resets to CLOSED (safe default). This is intentional: a restart implies operator acknowledgment.
7. **Synchronous.** All `CircuitBreaker` methods are synchronous. No `async def`, no `await`, no I/O. It is a pure in-memory state machine with zero latency impact.
8. **Zero DB writes.** `CircuitBreaker` does not import or interact with any repository, ORM model, or database session.
9. **Module isolation.** Zero imports from `src/agents/ingestion/`, `src/agents/context/`, `src/agents/evaluation/`, or any repository/ORM module. Allowed imports: `enum`, `structlog`, `src/schemas/risk.py` (`AlertEvent`, `AlertSeverity`), `src/core/config.py` (`AppConfig`).
10. **Config-gated construction.** When `enable_circuit_breaker=False` (default), no breaker is constructed and `_execution_consumer_loop()` routes directly to `ExecutionRouter` as before. Zero overhead when disabled.
11. **Override is one-shot.** The `circuit_breaker_override_closed` flag is auto-reset in memory (`config.circuit_breaker_override_closed = False`) after processing. It does not cause repeated forced resets on every evaluation cycle.
12. **Override skips alert evaluation.** When the override flag is set, `evaluate_alerts()` resets the breaker and returns immediately — it does NOT process alerts in the same cycle. This ensures the operator can force-close the breaker even when CRITICAL drawdown alerts are still firing.
13. **Gatekeeper authority preserved.** The circuit breaker operates AFTER `LLMEvaluationResponse` Gatekeeper validation and BEFORE execution routing. It is an additional gate, not a replacement for the Gatekeeper. No path to bypass the Gatekeeper is introduced.
14. **Telegram trip notification.** When the breaker trips (CLOSED→OPEN transition), `TelegramNotifier.send_execution_event()` is called with a message containing `"CIRCUIT BREAKER TRIPPED"`. This notification is best-effort — a Telegram failure does not prevent the trip. The notification call is wrapped in `try/except Exception: pass` (belt-and-suspenders).
15. **No new periodic task.** `CircuitBreaker` is invoked inline within existing loops, not as a separate `asyncio.create_task()`. Task count is unchanged from WI-26.
16. **No queue topology changes.** `market_queue -> prompt_queue -> execution_queue`. No new queue introduced.
17. **No database schema changes.** Zero new tables, zero new columns, zero Alembic migrations.
18. **Frozen upstream components.** `AlertEngine`, `PortfolioAggregator`, `PositionLifecycleReporter`, `PnLCalculator`, `ExitStrategyEngine`, `ExitOrderRouter`, `ExecutionRouter`, `TelegramNotifier`, `PolymarketClient`, `OrderBroadcaster`, `PositionTracker`, `PositionRepository`, and all schemas in `src/schemas/execution.py`, `src/schemas/position.py`, `src/schemas/risk.py` are byte-identical before and after WI-27. The only modified existing files are `src/core/config.py` (additive: 2 circuit breaker fields) and `src/orchestrator.py` (additive: import, constructor wiring, aggregation loop wiring, execution consumer gate).

---

## Required Test Matrix

At minimum, WI-27 tests must prove:

### Unit Tests — State Initialization
1. Newly constructed `CircuitBreaker` has `state == CircuitBreakerState.CLOSED`.
2. `check_entry_allowed()` returns `True` on a freshly constructed breaker.
3. `check_entry_allowed()` returns `False` when state is `OPEN`.

### Unit Tests — Trip Logic
4. `evaluate_alerts([critical_drawdown_alert])` transitions state to `OPEN`.
5. `evaluate_alerts([warning_drawdown_alert])` does NOT trip — state remains `CLOSED`.
6. `evaluate_alerts([critical_non_drawdown_alert])` does NOT trip — state remains `CLOSED`.
7. `evaluate_alerts([warning_non_drawdown_alert])` does NOT trip — state remains `CLOSED`.
8. `evaluate_alerts([])` does NOT change state from `CLOSED`.
9. Mixed alert list `[WARNING, CRITICAL drawdown, INFO]` trips on the CRITICAL drawdown.
10. Tripping an already-OPEN breaker is idempotent — no duplicate `circuit_breaker.tripped` event.

### Unit Tests — Reset
11. `reset()` transitions OPEN → CLOSED.
12. `reset()` on already-CLOSED breaker is idempotent — no error.

### Unit Tests — Override Flag
13. `circuit_breaker_override_closed=True` → `evaluate_alerts()` forces CLOSED.
14. After override, `config.circuit_breaker_override_closed` is `False`.
15. Override + CRITICAL drawdown in same call → override wins, state is CLOSED.
16. Override on already-CLOSED breaker → logs `override_applied`, stays CLOSED.

### Unit Tests — structlog Events
17. `circuit_breaker.tripped` event emitted with `rule_name`, `severity`, `alert_message`.
18. `circuit_breaker.reset` event emitted on reset.
19. `circuit_breaker.override_applied` event emitted on override.

### Unit Tests — Property
20. `state` property returns current `CircuitBreakerState`.

### Integration Tests — Config Gating
21. `enable_circuit_breaker=False` → `self.circuit_breaker is None` in Orchestrator.
22. `enable_circuit_breaker=True` → `self.circuit_breaker is not None`, initial state `CLOSED`.

### Integration Tests — Entry Gate
23. Breaker tripped → enqueue item → `ExecutionResult.action == SKIP` and `reason == "circuit_breaker_open"`. `ExecutionRouter.route()` NOT called.
24. Breaker CLOSED → enqueue item → `ExecutionRouter.route()` IS called.
25. Position tracking records the SKIP result when breaker blocks an entry.
26. `circuit_breaker.entry_blocked` structlog event emitted with `condition_id`.

### Integration Tests — Exit Path Passthrough
27. Breaker tripped → run exit scan → `ExitStrategyEngine.scan_open_positions()` still executes, SELL orders still routed. Circuit breaker does NOT gate exits.

### Integration Tests — Aggregation Loop Wiring
28. `_portfolio_aggregation_loop()` calls `evaluate_alerts()` after `AlertEngine.evaluate()` returns CRITICAL drawdown → breaker state transitions to OPEN.
29. When breaker trips during aggregation, `TelegramNotifier.send_execution_event()` called with `"CIRCUIT BREAKER TRIPPED"`.
30. `evaluate_alerts([])` called in the all-clear path to process override flag.

### Integration Tests — Breaker=None Safety
31. `enable_circuit_breaker=False` → `_execution_consumer_loop()` routes directly to `ExecutionRouter` with no `AttributeError`.

### Integration Tests — Module Isolation
32. `CircuitBreaker` module has no dependency on prompt/context/evaluation/ingestion/database modules.

### Regression Gate
33. Full suite: `pytest --asyncio-mode=auto tests/ -q` — all existing + new tests pass, 0 failures.
34. Coverage: `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` — >= 80%.

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
You are the MAAP Checker for WI-27 (Global Circuit Breaker) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi27.md
2) docs/PRD-v9.0.md (WI-27 section)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:

- Exit Path gating violation (any code path where _exit_scan_loop, ExitStrategyEngine, ExitOrderRouter, PnLCalculator, or SELL broadcasting is gated, blocked, or conditioned on circuit breaker state — this is the MOST CRITICAL check)
- Entry gate bypass (any code path in _execution_consumer_loop where ExecutionRouter.route() is called without checking circuit_breaker.check_entry_allowed() first, when the breaker is enabled)
- Silent drop (BUY blocked by circuit breaker without producing a typed ExecutionResult with action=SKIP and reason="circuit_breaker_open")
- Auto-recovery (any code path that transitions the breaker from OPEN to CLOSED without explicit human intervention — no timers, no cooldowns, no half-open states, no automatic reset after N cycles)
- Incorrect trip condition (breaker tripping on anything other than AlertSeverity.CRITICAL + rule_name=="drawdown" — WARNING/INFO alerts, non-drawdown CRITICAL alerts must NOT trip the breaker)
- Override flag persistence (circuit_breaker_override_closed not auto-reset to False after processing — must be one-shot)
- Override/alert ordering (alerts evaluated in the same cycle as an override — override must return early and skip alert processing)
- Async in CircuitBreaker (any async def, await, asyncio import, or I/O operation inside circuit_breaker.py — all methods must be synchronous)
- DB interaction (any import from src/db/*, any AsyncSession, any repository call, any ORM model reference inside CircuitBreaker)
- Isolation violations (CircuitBreaker importing prompt, context, evaluation, ingestion, AlertEngine, PortfolioAggregator, PositionLifecycleReporter, ExitStrategyEngine, ExitOrderRouter, PnLCalculator, ExecutionRouter, TelegramNotifier, OrderBroadcaster, TransactionSigner, BankrollSyncProvider, PolymarketClient, or sqlalchemy modules)
- Config-gate bypass (CircuitBreaker constructed when enable_circuit_breaker=False)
- Upstream mutation (any modification to AlertEngine, PortfolioAggregator, PositionLifecycleReporter, PnLCalculator, ExitStrategyEngine, ExitOrderRouter, ExecutionRouter, TelegramNotifier, PolymarketClient, PositionTracker, PositionRepository, OrderBroadcaster, or existing schemas in risk.py/execution.py/position.py)
- Position tracking gap (SKIP result from circuit breaker not recorded by position tracking — audit trail must always reflect blocked entries)
- Telegram trip notification missing (breaker trips without sending CIRCUIT BREAKER TRIPPED message via TelegramNotifier.send_execution_event when notifier is available)
- Telegram notification blocking (TelegramNotifier failure preventing or delaying the circuit breaker trip state transition — trip must happen regardless of notification success)
- Task count regression (new asyncio.create_task for CircuitBreaker — should be inline only)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation — circuit breaker is an ADDITIONAL gate, not a Gatekeeper replacement)
- Decimal violations (any float used for monetary calculations)
- Regression (any modification to existing tests or coverage < 80%)

Additional required checks:
- CircuitBreaker class exists in src/agents/execution/circuit_breaker.py
- CircuitBreakerState enum exists with values CLOSED and OPEN
- check_entry_allowed() -> bool is synchronous, returns True when CLOSED, False when OPEN
- evaluate_alerts(alerts: list[AlertEvent]) -> None is synchronous
- reset() -> None is synchronous
- state property returns CircuitBreakerState
- Constructor accepts config: AppConfig, initializes state to CLOSED
- AppConfig.enable_circuit_breaker: bool, default False
- AppConfig.circuit_breaker_override_closed: bool, default False
- CircuitBreaker constructed in Orchestrator.__init__() only when enable_circuit_breaker=True
- _portfolio_aggregation_loop: calls evaluate_alerts() after AlertEngine.evaluate() (both alert and all-clear paths)
- _execution_consumer_loop: calls check_entry_allowed() before ExecutionRouter.route()
- _exit_scan_loop: ZERO references to circuit_breaker — completely unmodified
- circuit_breaker.tripped structlog event on CLOSED→OPEN (with rule_name, severity, alert_message)
- circuit_breaker.entry_blocked structlog event on BUY rejection (with condition_id)
- circuit_breaker.reset structlog event on reset
- circuit_breaker.override_applied structlog event on override
- circuit_breaker.disabled structlog event when config gate prevents construction
- No new asyncio.create_task — inline calls only
- Task count unchanged: 7 when enable_portfolio_aggregator=True, 6 when False
- Zero new database tables, columns, or Alembic migrations
- Zero new queues
- Queue topology unchanged: market_queue -> prompt_queue -> execution_queue
- condition_id extraction moved BEFORE the circuit breaker gate in _execution_consumer_loop

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-27/invariants
4) Explicit statement on each MAAP critical category:
   - Exit Path gating violation: CLEARED/FLAGGED
   - Entry gate bypass: CLEARED/FLAGGED
   - Silent drop: CLEARED/FLAGGED
   - Auto-recovery: CLEARED/FLAGGED
   - Incorrect trip condition: CLEARED/FLAGGED
   - Override flag persistence: CLEARED/FLAGGED
   - Override/alert ordering: CLEARED/FLAGGED
   - Async in CircuitBreaker: CLEARED/FLAGGED
   - DB interaction: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
   - Config-gate bypass: CLEARED/FLAGGED
   - Upstream mutation: CLEARED/FLAGGED
   - Position tracking gap: CLEARED/FLAGGED
   - Telegram trip notification missing: CLEARED/FLAGGED
   - Telegram notification blocking: CLEARED/FLAGGED
   - Task count regression: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Decimal violations: CLEARED/FLAGGED
   - Regression: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
