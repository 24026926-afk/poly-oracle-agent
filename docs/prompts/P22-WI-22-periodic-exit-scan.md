# P22-WI-22 — Periodic Exit Scan Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-4.1 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi22-periodic-exit-scan` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/test-engineer.md`
- `.agents/rules/security-auditor.md`

## Role

You are implementing WI-22 for Phase 7: promoting `ExitStrategyEngine.scan_open_positions()` from its current inline call site inside `_execution_consumer_loop()` (Mode A) to a standalone periodic async task `_exit_scan_loop()` (Mode B) in the Orchestrator.

This WI is **wiring-only**. It must decouple exit evaluation cadence from the execution consumer loop without introducing any new exit logic, modifying `ExitStrategyEngine` internals, altering exit criteria or thresholds, or changing any schema. `ExitStrategyEngine`, all exit/position schemas, `PositionTracker`, `PositionRepository`, and `ExecutionRouter` are frozen.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi22.md`
4. `docs/PRD-v7.0.md` (Phase 7 / WI-22 section)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/orchestrator.py` — **primary integration target; `_execution_consumer_loop()` currently calls `scan_open_positions()` inline**
9. `src/agents/execution/exit_strategy_engine.py` (context boundary: frozen, no modifications)
10. `src/core/config.py`
11. Existing tests:
    - `tests/unit/test_exit_strategy_engine.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-22 test files first:
   - `tests/unit/test_exit_scan_loop.py`
   - `tests/integration/test_exit_scan_integration.py`
2. Write failing tests for all required behaviors:
   - `AppConfig` accepts `exit_scan_interval_seconds` as `Decimal` with default `Decimal("60")`.
   - `AppConfig` accepts `exit_scan_interval_seconds` overridden via environment variable (e.g., `EXIT_SCAN_INTERVAL_SECONDS=120`).
   - `_exit_scan_loop()` calls `asyncio.sleep()` with `float(config.exit_scan_interval_seconds)` as the **first** operation inside the loop body (sleep-first pattern).
   - `_exit_scan_loop()` calls `self.exit_strategy_engine.scan_open_positions()` after the sleep.
   - `_exit_scan_loop()` catches `Exception` from `scan_open_positions()` and does NOT re-raise or terminate the loop.
   - `_exit_scan_loop()` catches `ExitEvaluationError` from `scan_open_positions()` and does NOT re-raise or terminate the loop.
   - `_exit_scan_loop()` emits `exit_scan_loop.completed` structlog event at `info` level with fields: `total`, `exits`, `holds`, `interval_seconds`.
   - `_exit_scan_loop()` emits `exit_scan_loop.error` structlog event at `error` level when `scan_open_positions()` raises.
   - `_exit_scan_loop()` continues looping after a failed scan (mock: first call raises, second call succeeds — verify both calls occur).
   - `_execution_consumer_loop()` contains zero references to `exit_strategy_engine` or `scan_open_positions` (grep / AST-level check).
   - `Orchestrator.start()` creates exactly 6 tasks with names `IngestionTask`, `ContextTask`, `EvaluationTask`, `ExecutionTask`, `DiscoveryTask`, `ExitScanTask`.
   - `ExitScanTask` is cancelled cleanly during `Orchestrator.shutdown()` without raising.
3. Run RED tests:
   - `pytest tests/unit/test_exit_scan_loop.py -v`
   - `pytest tests/integration/test_exit_scan_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add `exit_scan_interval_seconds` to AppConfig

Target:
- `src/core/config.py`

Requirements:
1. Add `exit_scan_interval_seconds: Decimal` field with default `Decimal("60")`.
2. Type is `Decimal`, consistent with all other exit-related config fields.
3. Converted to `float` only at the `asyncio.sleep()` call boundary inside `_exit_scan_loop()`.
4. The field is read once per sleep cycle. The interval is not dynamically adjustable at runtime.

### Step 2 — Implement `_exit_scan_loop()` Method

Target:
- `src/orchestrator.py`

Requirements:
1. Add `async def _exit_scan_loop(self) -> None` as a private method on `Orchestrator`.
2. **Sleep first.** `await asyncio.sleep(float(self.config.exit_scan_interval_seconds))` is the first statement inside the `while True` loop body. This ensures the first scan does not fire at startup — the system has time to discover markets and record initial positions.
3. **Call scan.** `results = await self.exit_strategy_engine.scan_open_positions()`.
4. **Log summary.** Emit `exit_scan_loop.completed` structured log at `info` level with:
   - `total`: `len(results)`
   - `exits`: count of `ExitResult` where `should_exit=True`
   - `holds`: `total - exits`
   - `interval_seconds`: `str(self.config.exit_scan_interval_seconds)`
5. **Catch all exceptions.** The `scan_open_positions()` call and summary logging are wrapped in `try/except Exception`:
   - Emit `exit_scan_loop.error` at `error` level with the exception message.
   - Do NOT re-raise. Do NOT terminate the loop.
   - Continue to the next sleep cycle.
6. `asyncio.CancelledError` propagates naturally — the loop exits via standard asyncio cancellation during `shutdown()`.
7. Zero new imports added to `src/orchestrator.py` — the module already imports `ExitStrategyEngine` and `asyncio`.

### Step 3 — Register ExitScanTask in `start()`

Target:
- `src/orchestrator.py`

Requirements:
1. Append `asyncio.create_task(self._exit_scan_loop(), name="ExitScanTask")` to `self._tasks` in `Orchestrator.start()`.
2. After this change, `self._tasks` must contain exactly 6 entries:
   - `IngestionTask`, `ContextTask`, `EvaluationTask`, `ExecutionTask`, `DiscoveryTask`, `ExitScanTask`
3. No other changes to `start()`.

### Step 4 — Remove Inline Scan from `_execution_consumer_loop()`

Target:
- `src/orchestrator.py`

Requirements:
1. **Delete** the following block from `_execution_consumer_loop()`:
   ```python
   try:
       await self.exit_strategy_engine.scan_open_positions()
   except Exception as exc:
       logger.error(
           "execution.exit_scan_error",
           error=str(exc),
       )
   ```
2. After deletion, `_execution_consumer_loop()` must contain zero references to `exit_strategy_engine` or `scan_open_positions`.
3. The removed `execution.exit_scan_error` structlog event no longer appears anywhere in the codebase.
4. All remaining execution consumer loop logic (dequeue, extract, route, track, broadcast) is unchanged.

### Step 5 — GREEN Validation

Run:
```bash
pytest tests/unit/test_exit_scan_loop.py -v
pytest tests/integration/test_exit_scan_integration.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. **Wiring only.** Zero new exit logic, exit criteria, exit thresholds, or financial arithmetic introduced.
2. **ExitStrategyEngine is frozen.** Constructor signature, public methods (`evaluate_position`, `scan_open_positions`), internal logic, and `dry_run` write gate are byte-identical before and after WI-22.
3. **Schemas are frozen.** `ExitResult`, `ExitSignal`, `ExitReason`, `PositionRecord`, `PositionStatus` are byte-identical before and after WI-22.
4. **PositionTracker, PositionRepository, ExecutionRouter are frozen.** Zero modifications.
5. **Queue topology is unchanged.** `market_queue -> prompt_queue -> execution_queue`. No new queue.
6. **No bypass of `LLMEvaluationResponse` terminal Gatekeeper.**
7. **No Decimal financial arithmetic introduced.** The only `Decimal` field added is `exit_scan_interval_seconds`, a timing config, not a financial value.
8. **Async pipeline is preserved.** WI-22 adds one async task following the existing `create_task` + `gather` pattern. No blocking calls.
9. **Shutdown is preserved.** `ExitScanTask` is cancelled and awaited via the existing `self._tasks` lifecycle in `shutdown()`. No additional shutdown code.
10. **`dry_run` behavior is unchanged.** The periodic scan loop runs identically regardless of `dry_run`. Write-gate enforcement is delegated to `ExitStrategyEngine` internals.
11. **No new imports** added to `src/orchestrator.py` or `src/agents/execution/exit_strategy_engine.py`.
12. **Fail-open semantics.** A failed `scan_open_positions()` call is caught, logged, and retried next interval. The loop never terminates on a single failure.
13. **Sleep-first pattern.** `asyncio.sleep()` is the first statement inside the loop body, not the last. Consistent with `_discovery_loop()`.
14. **`self._tasks` has exactly 6 entries** after `start()` completes.
15. **Inline scan removed.** `_execution_consumer_loop()` contains zero references to `exit_strategy_engine` or `scan_open_positions` after WI-22.

---

## Required Test Matrix

At minimum, WI-22 tests must prove:

### Unit Tests
1. `AppConfig` accepts `exit_scan_interval_seconds` as `Decimal` with default `Decimal("60")`.
2. `AppConfig` accepts `exit_scan_interval_seconds` overridden via environment variable.
3. `_exit_scan_loop()` calls `scan_open_positions()` after sleeping — verify call order via mock timing.
4. `_exit_scan_loop()` catches `Exception` from `scan_open_positions()` and does NOT re-raise.
5. `_exit_scan_loop()` catches `ExitEvaluationError` from `scan_open_positions()` and does NOT re-raise.
6. `_exit_scan_loop()` emits `exit_scan_loop.completed` with correct `total`, `exits`, `holds` fields after a successful scan.
7. `_exit_scan_loop()` emits `exit_scan_loop.error` with exception message when `scan_open_positions()` raises.
8. `_exit_scan_loop()` continues looping after a failed scan iteration (mock: first call raises, second call succeeds — verify both calls occur).
9. `_exit_scan_loop()` calls `asyncio.sleep()` with `float(config.exit_scan_interval_seconds)` — verify sleep duration matches config.
10. `_execution_consumer_loop()` does NOT call `scan_open_positions()` or reference `exit_strategy_engine` (grep / AST-level check).

### Integration Tests
11. `Orchestrator.start()` creates exactly 6 tasks with names `IngestionTask`, `ContextTask`, `EvaluationTask`, `ExecutionTask`, `DiscoveryTask`, `ExitScanTask`.
12. `ExitScanTask` is cancelled cleanly during `Orchestrator.shutdown()` without raising.
13. Full orchestrator boot with mocked dependencies — `_exit_scan_loop()` fires after the configured interval and calls `scan_open_positions()`.
14. `_exit_scan_loop()` with `dry_run=True` — `scan_open_positions()` reads open positions from in-memory SQLite but produces zero DB writes.
15. `ExitStrategyEngine` module import boundary — verify zero imports from `src/agents/context/`, `src/agents/evaluation/`, `src/agents/ingestion/` (unchanged from WI-19 but re-verified).

### Regression Gate
16. Full suite: `pytest --asyncio-mode=auto tests/ -q` — all tests pass.
17. Coverage: `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` — >= 80%.

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
You are the MAAP Checker for WI-22 (Periodic Exit Scan) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi22.md
2) docs/PRD-v7.0.md (WI-22 section)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Wiring-only violation (any new exit logic, threshold, criteria, or financial arithmetic introduced by WI-22)
- Async violations (blocking calls, wrong task lifecycle, missing CancelledError propagation)
- Loop safety (scan_open_positions() exception escaping the try/except, loop terminating on failure)
- Sleep-first violation (asyncio.sleep is NOT the first statement in the loop body)
- Inline call not removed (_execution_consumer_loop still references exit_strategy_engine or scan_open_positions)
- Task count (self._tasks does NOT contain exactly 6 entries after start())
- Decimal violation (any float usage in money-path logic; note: float(exit_scan_interval_seconds) at the asyncio.sleep boundary is permitted)
- structlog events (exit_scan_loop.completed missing required fields; exit_scan_loop.error missing; removed execution.exit_scan_error still emitted)
- Regression (any modification to ExitStrategyEngine, ExitResult, ExitSignal, ExitReason, PositionRecord, PositionStatus, PositionTracker, PositionRepository, or ExecutionRouter)

Additional required checks:
- _exit_scan_loop() exists as an async method on Orchestrator
- _exit_scan_loop() calls self.exit_strategy_engine.scan_open_positions() inside a while True loop
- asyncio.sleep(float(self.config.exit_scan_interval_seconds)) is the first statement in the loop body
- AppConfig.exit_scan_interval_seconds exists as Decimal with default Decimal("60")
- ExitScanTask is registered via asyncio.create_task(..., name="ExitScanTask") in start()
- ExitScanTask is cancelled during shutdown() via existing self._tasks lifecycle
- Zero new imports added to src/orchestrator.py
- Zero new imports added to src/agents/execution/exit_strategy_engine.py
- _execution_consumer_loop() contains zero references to exit_strategy_engine or scan_open_positions
- execution.exit_scan_error structlog event no longer appears in the codebase

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-22/invariants
4) Explicit statement on each MAAP critical category:
   - Wiring-only violation: CLEARED/FLAGGED
   - Async violations: CLEARED/FLAGGED
   - Loop safety: CLEARED/FLAGGED
   - Sleep-first violation: CLEARED/FLAGGED
   - Inline call not removed: CLEARED/FLAGGED
   - Task count: CLEARED/FLAGGED
   - Decimal violation: CLEARED/FLAGGED
   - structlog events: CLEARED/FLAGGED
   - Regression: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
