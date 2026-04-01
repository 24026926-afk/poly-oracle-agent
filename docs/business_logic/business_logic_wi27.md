# WI-27 Business Logic — Global Circuit Breaker

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — `CircuitBreaker` is a fully synchronous, in-memory component. It performs zero I/O, zero async calls, and zero awaits. All methods (`check_entry_allowed`, `evaluate_alerts`, `reset`) are synchronous and return immediately. The breaker is invoked inline within existing Orchestrator loops and must never block, delay, or introduce async overhead. No new `asyncio.create_task()` or queue introduced.
- `.agents/rules/risk-auditor.md` — `CircuitBreaker` performs zero financial calculations. No `Decimal` math. It reads `AlertEvent.severity` and `AlertEvent.rule_name` fields (string comparisons only) to determine whether to trip. It does not compute drawdown, PnL, thresholds, or any monetary values.
- `.agents/rules/db-engineer.md` — Zero DB reads, zero DB writes. `CircuitBreaker` is a pure in-memory state machine. No `AsyncSession`, no repository calls, no ORM imports. State is a single `CircuitBreakerState` enum attribute.
- `.agents/rules/security-auditor.md` — `CircuitBreaker` is fail-secure by design. If the breaker state is unknown or the evaluate/check path raises unexpectedly, the Orchestrator must default to blocking BUY execution (fail-closed behavior at the gate point). The `circuit_breaker_override_closed` flag requires an intentional `.env` change — it cannot be toggled at runtime through any external API or Telegram command.
- `.agents/rules/test-engineer.md` — WI-27 requires unit tests for state transitions (CLOSED→OPEN, OPEN→CLOSED), alert filtering (only CRITICAL drawdown trips), override flag behavior, idempotency (tripping an already-OPEN breaker), and `check_entry_allowed` correctness. Integration tests must verify orchestrator wiring: BUY blocking when tripped, SELL passthrough when tripped, and config-gated disablement. Full suite remains >= 80% coverage.

## 1. Objective

Introduce `CircuitBreaker`, a stateful, synchronous, in-memory protection gate that sits BEFORE `ExecutionRouter.route()` on the Entry Path. When the `AlertEngine` (WI-25) fires a CRITICAL drawdown alert, the circuit breaker trips to OPEN state, halting all new BUY order routing. The Exit Path remains fully operational — the bot can still evaluate exits and route SELL orders to unwind the portfolio and protect the remaining bankroll.

`CircuitBreaker` owns:
- Maintaining a `CircuitBreakerState` enum attribute (`CLOSED` or `OPEN`)
- Scanning `AlertEvent` lists for CRITICAL drawdown alerts and tripping when found
- Exposing a synchronous `check_entry_allowed() -> bool` gate for the execution consumer loop
- Supporting explicit manual reset (`reset()`) and config-driven override (`circuit_breaker_override_closed`)
- Emitting structlog audit events for all state transitions

`CircuitBreaker` does NOT own:
- Alert evaluation or threshold computation (upstream: `AlertEngine`, WI-25)
- Portfolio snapshot computation (upstream: `PortfolioAggregator`, WI-23)
- Lifecycle report generation (upstream: `PositionLifecycleReporter`, WI-24)
- Order execution, signing, routing, or broadcasting
- Exit evaluation, SELL routing, or PnL settlement
- Telegram notification of breaker state changes (downstream: `TelegramNotifier`, WI-26 — see Section 5 for wiring)
- Automatic recovery, cooldown timers, or half-open states
- Per-market or per-position gating — this is a global, portfolio-level gate
- Database persistence of any kind
- Any async I/O

## 2. Scope Boundaries

### In Scope

1. New `CircuitBreaker` class in `src/agents/execution/circuit_breaker.py`.
2. New `CircuitBreakerState` enum in `src/agents/execution/circuit_breaker.py`.
3. Three public synchronous methods + one property:
   - `check_entry_allowed() -> bool`
   - `evaluate_alerts(alerts: list[AlertEvent]) -> None`
   - `reset() -> None`
   - `state` property → `CircuitBreakerState`
4. Two new `AppConfig` fields in `src/core/config.py`:
   - `enable_circuit_breaker: bool` (default `False`)
   - `circuit_breaker_override_closed: bool` (default `False`)
5. Config-gated construction in `Orchestrator.__init__()`.
6. Orchestrator wiring into `_portfolio_aggregation_loop()` and `_execution_consumer_loop()`.
7. Telegram notification of breaker trip events (wired through existing `TelegramNotifier`).
8. structlog audit events for state transitions and blocked entries.

### Out of Scope

1. Database persistence of breaker state — state is in-memory only.
2. Automatic recovery, cooldown timers, or half-open states — this is a trip-and-hold latch, not a classical circuit breaker pattern.
3. Per-market or per-position circuit breakers — this is a single global gate.
4. Modifications to `ExecutionRouter`, `ExitOrderRouter`, `ExitStrategyEngine`, `AlertEngine`, `PnLCalculator`, or any upstream component internals.
5. CLI reset command or API endpoint — reset is via `.env` flag or programmatic `reset()` call.
6. New database tables, migrations, or DB writes of any kind.
7. Retry logic, exponential backoff, or gradual recovery.

## 3. Target Component Architecture + Data Contracts

### 3.1 CircuitBreakerState Enum

- **Module:** `src/agents/execution/circuit_breaker.py`
- **Enum Name:** `CircuitBreakerState` (exact)

```python
class CircuitBreakerState(str, Enum):
    CLOSED = "CLOSED"   # Normal operation — BUY routing allowed (electricity flows)
    OPEN = "OPEN"       # Tripped — BUY routing forbidden (electricity blocked)
```

**Design rationale:** The naming follows electrical circuit semantics — CLOSED means current flows (orders proceed), OPEN means the circuit is broken (orders blocked). This is intentionally counterintuitive to software engineers who might expect "open" to mean "available." The names are chosen for consistency with the established circuit breaker pattern in systems engineering and the PRD v9.0 specification.

### 3.2 CircuitBreaker Component (New Class)

- **Module:** `src/agents/execution/circuit_breaker.py`
- **Class Name:** `CircuitBreaker` (exact)
- **Responsibility:** Accept `AlertEvent` lists from the Orchestrator, scan for CRITICAL drawdown alerts, maintain a binary trip state, and expose a synchronous gate for BUY order routing.

Isolation rules:
- `CircuitBreaker` must not import LLM prompt construction, context-building, evaluation, or ingestion modules.
- `CircuitBreaker` must not import any repository, ORM model, or `AsyncSession`.
- `CircuitBreaker` must not import `TransactionSigner`, `OrderBroadcaster`, `BankrollSyncProvider`, `PolymarketClient`, `ExecutionRouter`, `ExitOrderRouter`, `PnLCalculator`, or `TelegramNotifier`.
- `CircuitBreaker` must not write to the database.
- `CircuitBreaker` must not perform any I/O (network, file, database).
- `CircuitBreaker` must not define any `async` methods.
- `CircuitBreaker` may import from `src/schemas/risk.py` (for `AlertEvent`, `AlertSeverity`) and `src/core/config.py` (for `AppConfig`).

### 3.3 Constructor Signature

```python
class CircuitBreaker:
    def __init__(self, config: AppConfig) -> None:
```

The constructor must:
- Store a reference to `config` for reading `circuit_breaker_override_closed` during `evaluate_alerts()`
- Initialize internal state to `CircuitBreakerState.CLOSED` (safe default — BUY allowed on startup)
- Bind a `structlog` logger: `self._log = structlog.get_logger(__name__)`

Internal attributes:
- `self._config: AppConfig`
- `self._state: CircuitBreakerState` — initialized to `CLOSED`
- `self._log` — structlog logger

### 3.4 Public Method — `check_entry_allowed`

```python
def check_entry_allowed(self) -> bool:
```

**Behavior:**
1. Return `True` if `self._state == CircuitBreakerState.CLOSED` (BUY allowed).
2. Return `False` if `self._state == CircuitBreakerState.OPEN` (BUY forbidden).

**Invariants:**
- This method is synchronous. No I/O, no side effects, no logging on the hot path (logging happens at the call site in the Orchestrator when a BUY is blocked).
- Pure state read — does not modify breaker state.

### 3.5 Public Method — `evaluate_alerts`

```python
def evaluate_alerts(self, alerts: list[AlertEvent]) -> None:
```

**Behavior:**

1. **Override check (first):** If `self._config.circuit_breaker_override_closed is True`:
   - Transition state to `CircuitBreakerState.CLOSED` (regardless of current state).
   - Log `circuit_breaker.override_applied` at INFO level.
   - Set `self._config.circuit_breaker_override_closed = False` (auto-reset the flag in memory so the override is one-shot — the flag only persists in `.env` if the operator does not remove it, but the in-memory reset prevents repeated forced resets on every evaluation cycle).
   - Return early — do not evaluate alerts in the same cycle as an override.

2. **Alert scan:** Iterate over `alerts`. For each alert, check if BOTH conditions are met:
   - `alert.severity == AlertSeverity.CRITICAL`
   - `alert.rule_name == "drawdown"`

3. **Trip logic:** If any matching alert is found AND `self._state == CircuitBreakerState.CLOSED`:
   - Transition `self._state` to `CircuitBreakerState.OPEN`.
   - Log `circuit_breaker.tripped` at CRITICAL level with fields: `rule_name="drawdown"`, `severity="CRITICAL"`, `alert_message=alert.message`.
   - **Stop scanning** — one trip is sufficient, no need to process remaining alerts.

4. **Idempotency:** If a matching alert is found but the breaker is already `OPEN`, do nothing. The breaker is already tripped — no duplicate log events, no state change.

5. **Non-matching alerts:** If no alert matches the CRITICAL+drawdown filter, do nothing. WARNING-level alerts, INFO-level alerts, and non-drawdown CRITICAL alerts (if any exist in the future) do not affect breaker state.

**Invariants:**
- This method is synchronous. No I/O, no async.
- This method never raises exceptions to the caller. If the alert list is empty, it returns immediately (after override check).
- The override check runs BEFORE alert evaluation, so an operator can force-close the breaker even when CRITICAL drawdown alerts are still firing.

### 3.6 Public Method — `reset`

```python
def reset(self) -> None:
```

**Behavior:**
1. Transition `self._state` to `CircuitBreakerState.CLOSED`.
2. Log `circuit_breaker.reset` at INFO level.

**Invariants:**
- Idempotent: calling `reset()` on an already-CLOSED breaker logs the event and returns. No error.
- This method is for programmatic reset (e.g., future CLI integration). For operator-driven reset via `.env`, the `circuit_breaker_override_closed` flag is used instead.

### 3.7 Public Property — `state`

```python
@property
def state(self) -> CircuitBreakerState:
    return self._state
```

Read-only exposure of the current breaker state. Used by tests and future introspection tooling.

## 4. Configuration

### 4.1 New `AppConfig` Fields

Add the following fields to `AppConfig` in `src/core/config.py`, grouped under a `# --- Circuit Breaker (WI-27) ---` comment block, placed after the existing `# --- Telegram Notifier (WI-26) ---` block:

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

### 4.2 Config-Gate Logic

`CircuitBreaker` is constructed in `Orchestrator.__init__()` ONLY when:
1. `config.enable_circuit_breaker is True`

If the condition is false, set `self.circuit_breaker = None` and log `circuit_breaker.disabled`. This ensures zero overhead when the feature is off.

**Note:** Unlike `TelegramNotifier` (which requires three config conditions), the circuit breaker has a single config gate. The `circuit_breaker_override_closed` flag is a runtime control, not a construction gate.

## 5. Orchestrator Integration

### 5.1 Construction (`__init__`)

After `self.telegram_notifier` construction block (WI-26), add:

```python
# WI-27: Circuit Breaker (config-gated)
self.circuit_breaker: CircuitBreaker | None = None
if self.config.enable_circuit_breaker:
    self.circuit_breaker = CircuitBreaker(config=self.config)
else:
    logger.info("circuit_breaker.disabled")
```

**Key:** `CircuitBreaker` has no external dependencies (no HTTP client, no DB session). Construction is trivial.

### 5.2 Wiring into `_portfolio_aggregation_loop()`

After the existing `AlertEngine.evaluate()` block and after the Telegram notification loop (WI-26), add the circuit breaker evaluation:

```python
# Inside the `if alerts:` block, after Telegram notification loop:
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
                    summary="CIRCUIT BREAKER TRIPPED: BUY routing halted due to CRITICAL drawdown alert. Manual reset required.",
                    dry_run=self.config.dry_run,
                )
            except Exception:
                pass  # send_execution_event already swallows
    except Exception as exc:
        logger.error("circuit_breaker.evaluate_error", error=str(exc))
```

**Also call `evaluate_alerts` when no alerts are fired** — this is necessary to process the override flag when there are no alerts:

```python
# After the `if alerts:` / `else:` block (in the all_clear path):
# WI-27: Still evaluate override flag even when no alerts fire
if self.circuit_breaker is not None and not alerts:
    try:
        self.circuit_breaker.evaluate_alerts([])
    except Exception as exc:
        logger.error("circuit_breaker.evaluate_error", error=str(exc))
```

**Design rationale:** The override flag must be processable even during an "all clear" cycle. If the operator sets `circuit_breaker_override_closed=True` in `.env` and restarts, the next aggregation cycle (which may have no alerts) must still process the override and reset the breaker.

### 5.3 Wiring into `_execution_consumer_loop()` — The Entry Gate

BEFORE the `ExecutionRouter.route()` call (the current line `execution_result = await self.execution_router.route(...)`), insert the circuit breaker gate:

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

**Critical behavior:**
- When the breaker blocks a BUY, the `ExecutionResult` is a typed SKIP with reason `"circuit_breaker_open"` — never a silent drop.
- The position tracking block that follows STILL executes with this SKIP result, so the audit trail reflects the rejected entry attempt.
- The Telegram notification block (WI-26) that follows will NOT fire for SKIP results (it only fires for `EXECUTED` and `DRY_RUN`), which is correct — blocked entries should not trigger "BUY ROUTED" notifications.

### 5.4 Exit Path — No Changes

`_exit_scan_loop()` is NOT modified. The circuit breaker does not gate exit evaluation, SELL routing, or PnL settlement. When the breaker is tripped, the bot continues to:
1. Scan open positions for exit conditions (`ExitStrategyEngine.scan_open_positions()`)
2. Route SELL orders via `ExitOrderRouter`
3. Settle PnL via `PnLCalculator`
4. Send Telegram notifications for SELL events

This is the core defensive posture: stop buying, keep selling.

### 5.5 Shutdown

No shutdown logic required. `CircuitBreaker` has no external resources (no HTTP client, no DB session, no file handles). In-memory state is discarded on process exit.

## 6. structlog Events

| Event | Level | When | Fields |
|---|---|---|---|
| `circuit_breaker.tripped` | `critical` | Breaker transitions CLOSED → OPEN | `rule_name`, `severity`, `alert_message` |
| `circuit_breaker.entry_blocked` | `warning` | BUY rejected by open breaker | `condition_id` |
| `circuit_breaker.reset` | `info` | Manual reset: OPEN → CLOSED | (none) |
| `circuit_breaker.override_applied` | `info` | `circuit_breaker_override_closed=True` forces CLOSED | (none) |
| `circuit_breaker.evaluate_error` | `error` | Unexpected exception in evaluate_alerts | `error` |
| `circuit_breaker.disabled` | `info` | Config gate prevents construction | (none) |

## 7. Safety Invariants

1. **Entry Path gating ONLY:** The circuit breaker gates ONLY BUY routing in `_execution_consumer_loop()`. The Exit Path (`_exit_scan_loop()`: exit evaluation, SELL routing, PnL settlement) is NEVER gated, regardless of breaker state. This is the most critical invariant.
2. **Typed rejection:** When the breaker blocks a BUY, it produces `ExecutionResult(action=ExecutionAction.SKIP, reason="circuit_breaker_open")` — never a silent drop or untyped rejection. The audit trail always reflects the block.
3. **No auto-recovery:** The breaker does NOT auto-recover. Transition from OPEN → CLOSED requires explicit human intervention: either `circuit_breaker_override_closed=True` in `.env` (processed on next `evaluate_alerts()` call) or programmatic `reset()`. No timers, no cooldowns, no half-open states.
4. **In-memory only:** Breaker state is a single `CircuitBreakerState` attribute. No DB persistence, no file persistence. Process restart resets to CLOSED (safe default).
5. **Narrow trip condition:** The breaker trips ONLY on `AlertSeverity.CRITICAL` alerts with `rule_name == "drawdown"`. No other alert combination affects breaker state. This prevents WARNING-level alerts from halting trading.
6. **Config-gated:** When `enable_circuit_breaker=False` (default), no breaker is constructed and `_execution_consumer_loop()` routes directly to `ExecutionRouter` as before. Zero overhead when disabled.
7. **Synchronous:** All `CircuitBreaker` methods are synchronous. No async, no I/O, no awaits. It is a pure in-memory state machine.
8. **Zero DB writes:** `CircuitBreaker` does not import or interact with any repository, ORM model, or database session.
9. **Module isolation:** Zero imports from `src/agents/ingestion/`, `src/agents/context/`, `src/agents/evaluation/`, or any repository/ORM module.
10. **Fail-secure at gate:** If the `check_entry_allowed()` call or its surrounding logic raises unexpectedly, the Orchestrator's `try/except` in `_execution_consumer_loop()` catches it and the item is not routed — fail-secure by virtue of the existing error handling path.
11. **Gatekeeper authority preserved:** The circuit breaker operates AFTER `LLMEvaluationResponse` Gatekeeper validation and BEFORE execution routing. It is an additional gate, not a replacement for the Gatekeeper.
12. **Override is one-shot:** The `circuit_breaker_override_closed` flag is auto-reset in memory after processing. It does not cause repeated forced resets on every evaluation cycle.

## 8. Test Plan

### 8.1 Unit Tests — `tests/unit/test_circuit_breaker.py`

| # | Test Name | Assertion |
|---|---|---|
| 1 | `test_initial_state_is_closed` | Newly constructed `CircuitBreaker` has `state == CLOSED` |
| 2 | `test_check_entry_allowed_when_closed` | `check_entry_allowed()` returns `True` when state is `CLOSED` |
| 3 | `test_check_entry_allowed_when_open` | `check_entry_allowed()` returns `False` when state is `OPEN` |
| 4 | `test_evaluate_alerts_trips_on_critical_drawdown` | `evaluate_alerts([critical_drawdown_alert])` transitions state to `OPEN` |
| 5 | `test_evaluate_alerts_ignores_warning_drawdown` | `evaluate_alerts([warning_drawdown_alert])` does NOT trip — state remains `CLOSED` |
| 6 | `test_evaluate_alerts_ignores_critical_non_drawdown` | `evaluate_alerts([critical_stale_price_alert])` does NOT trip — state remains `CLOSED` |
| 7 | `test_evaluate_alerts_ignores_warning_non_drawdown` | `evaluate_alerts([warning_max_positions_alert])` does NOT trip — state remains `CLOSED` |
| 8 | `test_evaluate_alerts_idempotent_when_already_open` | Call `evaluate_alerts` twice with CRITICAL drawdown — second call does not re-log `tripped` event |
| 9 | `test_evaluate_alerts_empty_list_no_change` | `evaluate_alerts([])` does not change state from `CLOSED` |
| 10 | `test_evaluate_alerts_mixed_alerts_trips_on_critical_drawdown` | List with [WARNING, CRITICAL drawdown, INFO] → trips on the CRITICAL drawdown |
| 11 | `test_reset_transitions_open_to_closed` | After tripping, `reset()` transitions state back to `CLOSED` |
| 12 | `test_reset_idempotent_when_already_closed` | `reset()` on CLOSED breaker does not error — logs and returns |
| 13 | `test_override_flag_forces_closed` | Set `circuit_breaker_override_closed=True`, call `evaluate_alerts()` → state becomes `CLOSED` |
| 14 | `test_override_flag_auto_resets_in_memory` | After override is processed, `config.circuit_breaker_override_closed` is `False` |
| 15 | `test_override_skips_alert_evaluation` | Set override + pass CRITICAL drawdown alert → override wins, state is `CLOSED` (alert not processed in same cycle) |
| 16 | `test_override_on_already_closed_breaker` | Override flag with CLOSED breaker → logs `override_applied`, state stays `CLOSED` |
| 17 | `test_tripped_event_logged_with_correct_fields` | Capture structlog output → `circuit_breaker.tripped` event contains `rule_name`, `severity`, `alert_message` |
| 18 | `test_reset_event_logged` | Capture structlog output → `circuit_breaker.reset` event emitted on reset |
| 19 | `test_override_event_logged` | Capture structlog output → `circuit_breaker.override_applied` event emitted |
| 20 | `test_state_property_returns_current_state` | `state` property reflects current internal `_state` value |

### 8.2 Integration Tests — `tests/integration/test_circuit_breaker_integration.py`

| # | Test Name | Assertion |
|---|---|---|
| 1 | `test_breaker_disabled_when_config_flag_false` | `enable_circuit_breaker=False` → `circuit_breaker is None` in Orchestrator |
| 2 | `test_breaker_constructed_when_enabled` | `enable_circuit_breaker=True` → `circuit_breaker is not None`, initial state `CLOSED` |
| 3 | `test_execution_consumer_blocks_buy_when_tripped` | Trip the breaker → enqueue an item → verify `ExecutionResult.action == SKIP` and `reason == "circuit_breaker_open"` |
| 4 | `test_execution_consumer_routes_normally_when_closed` | Breaker CLOSED → enqueue an item → verify `ExecutionRouter.route()` is called |
| 5 | `test_exit_scan_unaffected_when_tripped` | Trip the breaker → run exit scan → verify `ExitStrategyEngine.scan_open_positions()` still executes and SELL orders are routed |
| 6 | `test_position_tracking_records_skip_when_blocked` | Trip breaker → enqueue item → verify position tracking records the SKIP result in audit trail |
| 7 | `test_aggregation_loop_trips_breaker_on_critical_drawdown` | Mock `AlertEngine.evaluate()` to return CRITICAL drawdown alert → verify breaker state transitions to OPEN |
| 8 | `test_aggregation_loop_sends_telegram_on_trip` | Mock `AlertEngine.evaluate()` to return CRITICAL drawdown → verify `TelegramNotifier.send_execution_event()` called with "CIRCUIT BREAKER TRIPPED" message |
| 9 | `test_override_processed_in_aggregation_loop` | Set `circuit_breaker_override_closed=True` → run aggregation loop → verify breaker resets to CLOSED |
| 10 | `test_breaker_none_does_not_block_execution` | `enable_circuit_breaker=False` → enqueue item → verify routing proceeds normally (no `AttributeError` on `None`) |

### 8.3 Regression Gate

```bash
pytest --asyncio-mode=auto tests/ -q
# Expected: all existing tests + new WI-27 tests pass (0 failures)

.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m
# Expected: coverage >= 80% (maintained from 94%)
```

## 9. Acceptance Criteria (Strict)

1. `CircuitBreaker` exists in `src/agents/execution/circuit_breaker.py` with `check_entry_allowed() -> bool`, `evaluate_alerts(alerts: list[AlertEvent]) -> None`, `reset() -> None`, and `state` property returning `CircuitBreakerState`.
2. `CircuitBreakerState` enum exists in `src/agents/execution/circuit_breaker.py` with values `CLOSED` and `OPEN`.
3. Default state on construction is `CLOSED` (BUY allowed).
4. `evaluate_alerts()` transitions the breaker from CLOSED to OPEN when any `AlertEvent` has `severity == AlertSeverity.CRITICAL` AND `rule_name == "drawdown"`.
5. `evaluate_alerts()` does NOT trip on WARNING-level alerts, INFO-level alerts, or non-drawdown CRITICAL alerts.
6. `evaluate_alerts()` is idempotent — calling it when already OPEN with a matching alert does not re-log the `tripped` event.
7. `check_entry_allowed()` returns `True` when CLOSED, `False` when OPEN.
8. When `check_entry_allowed()` returns `False`, `_execution_consumer_loop()` skips the item with `ExecutionResult(action=ExecutionAction.SKIP, reason="circuit_breaker_open")`.
9. Position tracking still records the SKIP result when the circuit breaker blocks an entry.
10. `_exit_scan_loop()` is NOT gated by the circuit breaker — exits proceed regardless of breaker state.
11. `reset()` transitions the breaker from OPEN to CLOSED with a `circuit_breaker.reset` structlog event.
12. `reset()` is idempotent — calling it on an already-CLOSED breaker does not error.
13. `circuit_breaker_override_closed=True` in `AppConfig` forces a CLOSED transition on next `evaluate_alerts()` call and logs `circuit_breaker.override_applied`.
14. The override flag is auto-reset in memory after processing (`config.circuit_breaker_override_closed = False`).
15. When the override flag is set, alert evaluation is skipped in the same cycle (override takes priority).
16. The breaker does NOT auto-recover. Absent manual intervention, an OPEN breaker remains OPEN indefinitely.
17. A process restart resets the breaker to CLOSED (in-memory state only).
18. `AppConfig.enable_circuit_breaker` is `bool` with default `False`.
19. `AppConfig.circuit_breaker_override_closed` is `bool` with default `False`.
20. `CircuitBreaker` is constructed in `Orchestrator.__init__()` only when `enable_circuit_breaker=True`.
21. `evaluate_alerts()` is called in `_portfolio_aggregation_loop()` after `AlertEngine.evaluate()` and after the Telegram notification loop.
22. `evaluate_alerts([])` is also called when no alerts fire (to process override flag).
23. `check_entry_allowed()` is called in `_execution_consumer_loop()` BEFORE `ExecutionRouter.route()`.
24. When the breaker trips, a Telegram notification is sent via `TelegramNotifier.send_execution_event()` with the message `"CIRCUIT BREAKER TRIPPED: BUY routing halted due to CRITICAL drawdown alert. Manual reset required."`.
25. `CircuitBreaker` is synchronous — no async methods, no I/O.
26. `CircuitBreaker` has zero imports from `src/agents/ingestion/`, `src/agents/context/`, `src/agents/evaluation/`, or any repository/ORM module.
27. `CircuitBreaker` performs zero DB writes.
28. All unit tests (Section 8.1) and integration tests (Section 8.2) pass.
29. Full regression remains green with coverage >= 80%.
