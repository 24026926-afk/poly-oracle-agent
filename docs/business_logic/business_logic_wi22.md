# WI-22 Business Logic — Periodic Exit Scan (Mode B Promotion)

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — `_exit_scan_loop()` is a standalone async task in `Orchestrator`. It follows the same lifecycle pattern as `_discovery_loop()`: `asyncio.create_task()` in `start()`, cancellation in `shutdown()`. Queue topology (`market_queue -> prompt_queue -> execution_queue`) is unchanged. No new queue is introduced.
- `.agents/rules/db-engineer.md` — WI-22 introduces no new DB access. All DB reads and writes remain delegated to `ExitStrategyEngine` internals via `PositionRepository`. Zero repository changes.
- `.agents/rules/risk-auditor.md` — No financial arithmetic is introduced or modified. Exit thresholds, Decimal comparisons, and `ExitResult` contracts remain exactly as WI-19 delivered them.
- `.agents/rules/security-auditor.md` — `dry_run=True` enforcement is unchanged. The periodic scan inherits `ExitStrategyEngine`'s internal write gate. No credentials, private keys, or broadcast capability are touched.
- `.agents/rules/test-engineer.md` — WI-22 wiring changes require unit + integration coverage for the new loop lifecycle, the config field, and the removal of the inline call. Full suite remains >= 80%.

## 1. Objective

Promote `ExitStrategyEngine.scan_open_positions()` from its current inline call site inside `_execution_consumer_loop()` (Mode A) to a standalone periodic async task (Mode B) in the Orchestrator.

After WI-19, exit evaluation runs at step 5 of the execution consumer loop — every time a new item is dequeued and routed, `scan_open_positions()` is called synchronously before the broadcast path. This has two problems:

1. **Latency coupling.** Each dequeue cycle pays the full cost of scanning all open positions against fresh market data, even when no positions have changed. With N open positions, this adds N order-book fetches to every execution cycle.
2. **Cadence coupling.** Exit evaluation only runs when new items arrive in `execution_queue`. If no new markets are evaluated, open positions are never re-scanned — the opposite of the intended lifecycle behavior.

WI-22 solves both problems by moving the `scan_open_positions()` call to an independent `_exit_scan_loop()` async task that sleeps for a configurable interval, scans, logs, and repeats. The execution consumer loop no longer calls `scan_open_positions()` at all.

WI-22 does **not** introduce any new exit logic, modify `ExitStrategyEngine` internals, alter exit criteria or thresholds, change `ExitResult`/`ExitSignal`/`ExitReason` schemas, or touch `PositionTracker`/`PositionRepository`/`ExecutionRouter`. It is strictly a wiring change.

## 2. Scope Boundaries

### In Scope

1. New `Orchestrator._exit_scan_loop()` async method — the standalone periodic scan task.
2. New `AppConfig` field: `exit_scan_interval_seconds: Decimal` (default: `Decimal("60")`).
3. Registration of `_exit_scan_loop()` as `asyncio.create_task(..., name="ExitScanTask")` in `Orchestrator.start()`.
4. Removal of the inline `scan_open_positions()` call (and its surrounding `try/except`) from `_execution_consumer_loop()`.
5. Fire-and-forget error handling within the loop body: a failed scan iteration logs and continues.
6. Graceful shutdown: `ExitScanTask` is cancelled alongside all other tasks in `Orchestrator.shutdown()`.
7. structlog audit events for loop lifecycle and scan iterations.

### Out of Scope

1. New exit evaluation logic, criteria, or thresholds — `ExitStrategyEngine` internals are frozen.
2. Modifications to `ExitResult`, `ExitSignal`, `ExitReason`, `PositionRecord`, or `PositionStatus` schemas.
3. Modifications to `PositionTracker`, `PositionRepository`, or `ExecutionRouter`.
4. Queue topology changes — no new queue is introduced.
5. Exit order routing or PnL accounting — deferred to WI-20 and WI-21.
6. Retry logic with backoff for failed scans — simple sleep-and-retry is sufficient.
7. Dynamic interval adjustment — the interval is read once from `AppConfig` at construction time.

## 3. Target Component Architecture

### 3.1 Orchestrator._exit_scan_loop() (New Method)

- **Module:** `src/orchestrator.py`
- **Method:** `Orchestrator._exit_scan_loop(self) -> None` (async)
- **Task Name:** `ExitScanTask`
- **Responsibility:** Call `self.exit_strategy_engine.scan_open_positions()` on a recurring interval, log the results summary, and continue on failure.

The method lives on `Orchestrator` (not a separate class) because it follows the identical pattern of `_discovery_loop()`: a private async method registered as a named task. The Orchestrator already owns the `ExitStrategyEngine` instance — no new dependency injection is needed.

### 3.2 Constructor Dependencies (Already Wired)

`_exit_scan_loop()` accesses the following via `self`:

| Dependency | Source | Wired In |
|---|---|---|
| `self.exit_strategy_engine` | `ExitStrategyEngine` instance | `Orchestrator.__init__()` (WI-19, unchanged) |
| `self.config.exit_scan_interval_seconds` | `Decimal` interval | `AppConfig` (new field) |
| `self.config.dry_run` | `bool` | `AppConfig` (existing, read-only by loop) |

No new constructor parameters are added to `Orchestrator.__init__()`. No new imports are required beyond what the orchestrator already has.

### 3.3 New AppConfig Field (Required)

One new field must be added to `AppConfig` in `src/core/config.py`:

```python
# --- Exit Scan (WI-22) ---
exit_scan_interval_seconds: Decimal = Field(
    default=Decimal("60"),
    description="Seconds between periodic exit-strategy scans of open positions",
)
```

Hard constraints:
1. Type is `Decimal`, consistent with all other exit-related config fields.
2. Default is `Decimal("60")` — one scan per minute. Conservative: frequent enough to catch stop-loss events without overwhelming the order-book endpoint.
3. Converted to `float` only at the `asyncio.sleep()` call boundary: `await asyncio.sleep(float(self.config.exit_scan_interval_seconds))`.
4. The field is read once per sleep cycle. The interval is not dynamically adjustable at runtime.

## 4. Core Method Contract

### 4.1 _exit_scan_loop() — Async Periodic Task

```python
async def _exit_scan_loop(self) -> None:
```

Behavior requirements (in execution order):

1. **Sleep first.** `await asyncio.sleep(float(self.config.exit_scan_interval_seconds))`. Sleep is at the top of the loop, not the bottom. This ensures the first scan does not fire immediately at startup — the system has time to discover markets, wire the pipeline, and record initial positions before the first exit scan.

2. **Call scan.** `results = await self.exit_strategy_engine.scan_open_positions()`.

3. **Log summary.** Emit a structured log with aggregated scan results: total positions scanned, exits triggered, holds, errors. This mirrors the `exit_engine.scan_complete` event already emitted by `ExitStrategyEngine` but provides the orchestrator-level wrapper event.

4. **Catch all exceptions.** The `scan_open_positions()` call and summary logging are wrapped in `try/except Exception`. On failure:
   - Emit `exit_scan_loop.error` at `error` level with the exception message.
   - Do NOT re-raise. Do NOT terminate the loop.
   - Continue to the next sleep cycle.

5. **Repeat.** Loop back to step 1.

6. **CancelledError.** The `while True` loop is exited via `asyncio.CancelledError` when the Orchestrator shuts down. This is handled by the standard `asyncio.gather(..., return_exceptions=True)` in `shutdown()`.

### 4.2 Pseudocode

```python
async def _exit_scan_loop(self) -> None:
    """Periodic exit-strategy scan for open positions (Mode B)."""
    while True:
        await asyncio.sleep(float(self.config.exit_scan_interval_seconds))
        try:
            results = await self.exit_strategy_engine.scan_open_positions()
            exits = sum(1 for r in results if r.should_exit)
            holds = len(results) - exits
            logger.info(
                "exit_scan_loop.completed",
                total=len(results),
                exits=exits,
                holds=holds,
                interval_seconds=str(self.config.exit_scan_interval_seconds),
            )
        except Exception as exc:
            logger.error(
                "exit_scan_loop.error",
                error=str(exc),
            )
```

### 4.3 Why Sleep-First, Not Scan-First

The existing `_discovery_loop()` uses sleep-first:

```python
async def _discovery_loop(self) -> None:
    while True:
        await asyncio.sleep(300)
        # ... discover ...
```

`_exit_scan_loop()` follows this identical pattern for consistency and for a practical reason: at startup, the `positions` table may be empty or the pipeline may not yet have produced any execution results. Scanning immediately would be a wasted cycle. Sleeping first gives the system one full interval to populate positions before the first evaluation.

## 5. Orchestrator Wiring Changes

### 5.1 Orchestrator.start() — Register ExitScanTask

The `_tasks` list in `start()` gains one entry. Current state (5 tasks):

```python
self._tasks = [
    asyncio.create_task(self.ws_client.run(), name="IngestionTask"),
    asyncio.create_task(self.aggregator.start(), name="ContextTask"),
    asyncio.create_task(self.claude_client.start(), name="EvaluationTask"),
    asyncio.create_task(self._execution_consumer_loop(), name="ExecutionTask"),
    asyncio.create_task(self._discovery_loop(), name="DiscoveryTask"),
]
```

After WI-22 (6 tasks):

```python
self._tasks = [
    asyncio.create_task(self.ws_client.run(), name="IngestionTask"),
    asyncio.create_task(self.aggregator.start(), name="ContextTask"),
    asyncio.create_task(self.claude_client.start(), name="EvaluationTask"),
    asyncio.create_task(self._execution_consumer_loop(), name="ExecutionTask"),
    asyncio.create_task(self._discovery_loop(), name="DiscoveryTask"),
    asyncio.create_task(self._exit_scan_loop(), name="ExitScanTask"),
]
```

No other changes to `start()`.

### 5.2 _execution_consumer_loop() — Remove Inline Scan

The following block must be **deleted** from `_execution_consumer_loop()`:

```python
# REMOVE — lines 233-239 of current orchestrator.py
try:
    await self.exit_strategy_engine.scan_open_positions()
except Exception as exc:
    logger.error(
        "execution.exit_scan_error",
        error=str(exc),
    )
```

After removal, the execution consumer loop flow becomes:

```text
_execution_consumer_loop (after WI-22):
  1. Dequeue item from execution_queue
  2. Extract LLMEvaluationResponse + MarketContext from item
  3. ExecutionRouter.route(response, market_context) -> ExecutionResult   [WI-16]
  4. PositionTracker.record_execution(result, condition_id, token_id)     [WI-17]
  5. (exit scan removed — now in ExitScanTask)                           [WI-22]
  6. If EXECUTED and not dry_run, proceed to broadcast                   [existing]
```

Step 5 is now a no-op in the consumer loop. The exit scan runs independently in `ExitScanTask`.

### 5.3 Orchestrator.shutdown() — No Change Required

`shutdown()` already cancels all tasks in `self._tasks` and gathers with `return_exceptions=True`. Because `ExitScanTask` is appended to the same `_tasks` list, it is automatically cancelled and awaited during shutdown. No shutdown code changes are required.

### 5.4 Orchestrator.__init__() — No Change Required

`self.exit_strategy_engine` is already constructed in `__init__()` (WI-19). No new instance variables or constructor changes are needed.

## 6. Failure Semantics (Fail-Open, Never Kill the Loop)

| Failure scenario | Behavior | Rationale |
|---|---|---|
| `scan_open_positions()` raises `ExitEvaluationError` | Caught by `except Exception`. `exit_scan_loop.error` logged. Loop sleeps and retries. | DB read failure is transient. Next cycle may succeed. |
| `scan_open_positions()` raises unexpected `Exception` | Same as above. | Defensive catch-all. No exception should kill the loop. |
| `asyncio.CancelledError` raised (shutdown) | Propagates naturally. Loop exits. `gather()` collects it. | Standard asyncio shutdown pattern. |
| `asyncio.sleep()` raises `CancelledError` | Same as above. | Sleep is the most common cancellation point. |
| `ExitStrategyEngine` internal failure (per-position) | Already handled by `ExitStrategyEngine.scan_open_positions()` — individual position errors are caught internally, logged, and an `ERROR` result is produced. The loop only sees the returned `list[ExitResult]`. | WI-19 design: per-position failures don't propagate. |
| Config value `exit_scan_interval_seconds` is zero or negative | `asyncio.sleep(0)` or `asyncio.sleep(negative)` — both return immediately, creating a tight loop. **Not guarded by WI-22.** A Pydantic validator could enforce `> 0`, but is out of scope unless the Maker adds one. | Misconfiguration. Document as a known edge case. |

Critical rule: **`_exit_scan_loop()` must never re-raise an exception from `scan_open_positions()`.** The `except Exception` block is not optional — it is a hard requirement.

## 7. dry_run Behavior

WI-22 introduces **no new dry_run gate**. The periodic scan loop runs identically regardless of `dry_run`:

| Phase | dry_run=True | dry_run=False |
|---|---|---|
| `asyncio.sleep(interval)` | Sleeps normally | Sleeps normally |
| `scan_open_positions()` — DB read | Reads `OPEN` positions (read-path permitted) | Reads `OPEN` positions |
| `scan_open_positions()` — per-position evaluation | Computes full `ExitResult` | Computes full `ExitResult` |
| `scan_open_positions()` — state mutation | **Zero DB writes** (early-return guard in `ExitStrategyEngine.evaluate_position()`) | Calls `PositionRepository.update_status()` for exits |
| Loop summary log | Emitted | Emitted |

The `dry_run` enforcement is owned entirely by `ExitStrategyEngine` and is unchanged by WI-22. The periodic loop does not inspect `self.config.dry_run` for gating purposes — it delegates all write-gate decisions to the engine.

This is the correct design: `_exit_scan_loop()` is wiring, not logic. Write gates belong in the component that owns the mutation, not in the caller.

## 8. structlog Audit Events

### 8.1 New Events (WI-22)

| Event Key | Level | When | Key Fields |
|---|---|---|---|
| `exit_scan_loop.completed` | `info` | After a successful `scan_open_positions()` call completes | `total`, `exits`, `holds`, `interval_seconds` |
| `exit_scan_loop.error` | `error` | `scan_open_positions()` raised an exception | `error` |

### 8.2 Preserved Events (WI-19, Unchanged)

All nine `exit_engine.*` events from WI-19 (§5.4 of `business_logic_wi19.md`) continue to fire from within `ExitStrategyEngine` internals. They are unaffected by the call-site change.

### 8.3 Removed Events

| Event Key | Status | Reason |
|---|---|---|
| `execution.exit_scan_error` | **REMOVED** | This was the inline error handler in `_execution_consumer_loop()`. It is replaced by `exit_scan_loop.error` in the new periodic loop. |

## 9. Module Isolation Rules

### 9.1 _exit_scan_loop() Import Boundary

`_exit_scan_loop()` lives inside `src/orchestrator.py`, which already imports `ExitStrategyEngine`. WI-22 adds **zero new imports** to the orchestrator module.

### 9.2 ExitStrategyEngine Import Boundary (Unchanged)

The `ExitStrategyEngine` module import boundary established in WI-19 (§5.5 of `business_logic_wi19.md`) remains enforced:

**Must NOT import:**
- `src/agents/context/` (prompt construction, context-building)
- `src/agents/evaluation/` (`ClaudeClient`, `GrokClient`)
- `src/agents/ingestion/` (`CLOBWebSocketClient`, `GammaRESTClient`, `MarketDiscoveryEngine`)
- `src/schemas/llm.py` (`LLMEvaluationResponse`, `MarketContext`)

**Allowed imports (unchanged):**
- `src/core/config` (`AppConfig`)
- `src/core/exceptions` (`ExitEvaluationError`, `ExitMutationError`)
- `src/schemas/execution` (`ExitSignal`, `ExitResult`, `ExitReason`, `PositionRecord`, `PositionStatus`)
- `src/db/repositories/position_repository` (`PositionRepository`)
- `src/agents/execution/polymarket_client` (`PolymarketClient`)
- `structlog`, `datetime`, `decimal` (stdlib / logging)

WI-22 does not modify `ExitStrategyEngine`'s import set.

## 10. Invariants Preserved

1. **ExitStrategyEngine is frozen.** Constructor signature, public methods (`evaluate_position`, `scan_open_positions`), internal logic, exit criteria, threshold comparisons, and `dry_run` write gate are all unmodified.
2. **ExitResult, ExitSignal, ExitReason schemas are frozen.** No field additions, removals, or validator changes.
3. **PositionTracker, PositionRepository, ExecutionRouter are frozen.** Zero changes to entry-path tracking or routing.
4. **Queue topology is unchanged.** `market_queue -> prompt_queue -> execution_queue`. No new queue.
5. **Gatekeeper authority is unchanged.** `LLMEvaluationResponse` remains the terminal validation boundary before execution routing.
6. **Decimal financial integrity is unchanged.** WI-22 introduces no financial arithmetic. The only Decimal field added is `exit_scan_interval_seconds`, which is a timing configuration, not a financial value.
7. **Async pipeline is preserved.** WI-22 adds one async task following the existing `create_task` + `gather` pattern. No blocking calls introduced.
8. **Shutdown sequence is preserved.** `ExitScanTask` is cancelled and awaited via the existing `self._tasks` lifecycle.
9. **Repository pattern is preserved.** No new DB access is introduced. All repository interactions remain inside `ExitStrategyEngine`.
10. **Fail-open semantics are preserved.** A failed exit scan never blocks the execution consumer loop (they are now entirely independent tasks).

## 11. Strict Acceptance Criteria (Maker Agent)

1. `Orchestrator._exit_scan_loop()` exists as an `async` method in `src/orchestrator.py` that calls `self.exit_strategy_engine.scan_open_positions()` inside a `while True` loop.
2. `asyncio.sleep(float(self.config.exit_scan_interval_seconds))` is the first statement inside the loop body (sleep-first pattern).
3. `AppConfig.exit_scan_interval_seconds` exists in `src/core/config.py` as a `Decimal` field with default `Decimal("60")`.
4. `_exit_scan_loop()` is registered as `asyncio.create_task(self._exit_scan_loop(), name="ExitScanTask")` in `Orchestrator.start()`, appended to `self._tasks`.
5. The `Orchestrator._tasks` list contains exactly 6 entries after `start()` completes: `IngestionTask`, `ContextTask`, `EvaluationTask`, `ExecutionTask`, `DiscoveryTask`, `ExitScanTask`.
6. The inline `scan_open_positions()` call and its surrounding `try/except` block are removed from `_execution_consumer_loop()`.
7. After WI-22, `_execution_consumer_loop()` contains zero references to `exit_strategy_engine` or `scan_open_positions`.
8. A `scan_open_positions()` exception inside the loop is caught by `except Exception`, logged via `exit_scan_loop.error`, and does NOT re-raise or terminate the loop.
9. `exit_scan_loop.completed` structlog event is emitted at `info` level after each successful scan with fields: `total`, `exits`, `holds`, `interval_seconds`.
10. `exit_scan_loop.error` structlog event is emitted at `error` level when `scan_open_positions()` raises, with field: `error`.
11. The removed `execution.exit_scan_error` structlog event is no longer emitted anywhere.
12. `ExitScanTask` is cancelled during `Orchestrator.shutdown()` via the existing `self._tasks` cancellation loop — no additional shutdown code required.
13. `ExitStrategyEngine.__init__()`, `evaluate_position()`, `scan_open_positions()`, and all private methods are byte-identical before and after WI-22. Zero modifications.
14. `ExitResult`, `ExitSignal`, `ExitReason`, `PositionRecord`, `PositionStatus` schemas are byte-identical before and after WI-22.
15. `PositionTracker`, `PositionRepository`, `ExecutionRouter` are byte-identical before and after WI-22.
16. No new imports are added to `src/orchestrator.py`.
17. No new imports are added to `src/agents/execution/exit_strategy_engine.py`.
18. Full regression remains green (`pytest --asyncio-mode=auto tests/`) with coverage >= 80%.

## 12. Verification Checklist (Test Matrix)

### Unit Tests

1. Unit test: `AppConfig` accepts `exit_scan_interval_seconds` as `Decimal` with default `Decimal("60")`.
2. Unit test: `AppConfig` accepts `exit_scan_interval_seconds` overridden via environment variable (e.g., `EXIT_SCAN_INTERVAL_SECONDS=120`).
3. Unit test: `_exit_scan_loop()` calls `scan_open_positions()` after sleeping — verify call order via mock timing.
4. Unit test: `_exit_scan_loop()` catches `Exception` from `scan_open_positions()` and does NOT re-raise.
5. Unit test: `_exit_scan_loop()` catches `ExitEvaluationError` from `scan_open_positions()` and does NOT re-raise.
6. Unit test: `_exit_scan_loop()` emits `exit_scan_loop.completed` with correct `total`, `exits`, `holds` fields after a successful scan.
7. Unit test: `_exit_scan_loop()` emits `exit_scan_loop.error` with exception message when `scan_open_positions()` raises.
8. Unit test: `_exit_scan_loop()` continues looping after a failed scan iteration (mock: first call raises, second call succeeds — verify both calls occur).
9. Unit test: `_exit_scan_loop()` calls `asyncio.sleep()` with `float(config.exit_scan_interval_seconds)` — verify the sleep duration matches config.
10. Unit test: `_execution_consumer_loop()` does NOT call `scan_open_positions()` or reference `exit_strategy_engine` (grep / AST-level check).

### Integration Tests

11. Integration test: `Orchestrator.start()` creates exactly 6 tasks with names `IngestionTask`, `ContextTask`, `EvaluationTask`, `ExecutionTask`, `DiscoveryTask`, `ExitScanTask`.
12. Integration test: `ExitScanTask` is cancelled cleanly during `Orchestrator.shutdown()` without raising.
13. Integration test: full orchestrator boot with mocked dependencies — `_exit_scan_loop()` fires after the configured interval and calls `scan_open_positions()`.
14. Integration test: `_exit_scan_loop()` with `dry_run=True` — `scan_open_positions()` reads open positions from in-memory SQLite but produces zero DB writes.
15. Integration test: `ExitStrategyEngine` module import boundary — verify zero imports from `src/agents/context/`, `src/agents/evaluation/`, `src/agents/ingestion/` (unchanged from WI-19 but re-verified).

### Regression Gate

16. Full suite: `pytest --asyncio-mode=auto tests/ -q` — all tests pass.
17. Coverage: `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` — >= 80%.
