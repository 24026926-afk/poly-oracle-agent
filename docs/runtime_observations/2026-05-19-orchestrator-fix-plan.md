# Orchestrator Fix Plan — Run 7

## 1. Why a Separate Planning Document

The observations report (`2026-05-19-orchestrator-dry-run-session.md`) documents what happened. This plan defines what to change, in what order, and how to validate — without committing any code.

## 2. Newly Observed Signals Since Last Report

- Run 7 (16 min) confirmed Run 5/6 calibration stability: 0 budget blocks, 0 WS disconnects, 0 errors.
- Grok 100% success rate (65/65) — best across all runs.
- CULTURE eval budget waste confirmed at 16% (9/57), consistent with prior runs.
- Orchestrator exited without shutdown log due to background shell termination (infrastructure artifact, not code crash).
- Session too short (16 min) to observe per-market hourly cap exhaustion.

## 3. Goals (Prioritized)

1. **G1:** Reduce CULTURE eval budget waste from 16% to <5% (config-only).
2. **G2:** Add clean shutdown logging to orchestrator (code change, MAAP-gated).
3. **G3:** Run full 60-minute dry-run to observe complete budget cycle.

## 4. Constraints

- MAAP required for any change under `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`.
- Atomic commits only. No "WIP" on `develop`.
- No `dry_run` weakening, no Gatekeeper bypass, no `float` for money.
- All changes must preserve `DRY_RUN=true` behavior.

## 5. Fix Inventory

### F1 — Increase CULTURE Evaluation Interval (MEDIUM → Config)

- **Severity:** MEDIUM
- **MAAP:** No (config-only)
- **Blast radius:** `.env`, `.env.example`, `src/core/config.py`
- **Why:** CULTURE consumes 16% of eval budget with zero Grok signal and structural EV=0.0.
- **What:** Add `CULTURE_EVALUATION_INTERVAL_SEC=300` to `.env` and wire into category cadence config.
- **Files:** `.env`, `.env.example`, `src/core/config.py`
- **Code sketch:** Add `culture_evaluation_interval_sec: int = Field(default=300)` to `AppConfig`. Aggregator already reads category-specific intervals.
- **Tests:** Verify `AppConfig().culture_evaluation_interval_sec == 300` with env isolation.
- **Risk:** Low. CULTURE evals still happen, just less frequently.
- **Validation:** CULTURE eval count drops from ~16% to <5% in next dry-run.

### F2 — Add SIGTERM/SIGINT Shutdown Handler (MEDIUM → Code)

- **Severity:** MEDIUM
- **MAAP:** Yes (`src/orchestrator.py`)
- **Blast radius:** Shutdown logging only, no execution path changes
- **Why:** Orchestrator exited without shutdown log. Cannot distinguish graceful exit from crash in production.
- **What:** Register signal handlers for SIGTERM/SIGINT that log `orchestrator.shutdown` before calling `sys.exit(0)`.
- **Files:** `src/orchestrator.py`
- **Code sketch:**
  ```python
  import signal
  import sys
  import structlog

  log = structlog.get_logger()

  def _shutdown_handler(signum, frame):
      log.info("orchestrator.shutdown", signal=signum)
      sys.exit(0)

  signal.signal(signal.SIGTERM, _shutdown_handler)
  signal.signal(signal.SIGINT, _shutdown_handler)
  ```
- **Tests:** Verify signal handler logs shutdown message. Mock signal delivery in unit test.
- **Risk:** Low. Only affects shutdown path, no runtime behavior change.
- **Validation:** Next dry-run shows `orchestrator.shutdown` log on SIGTERM.

## 6. Execution Sequence

1. **F1** (config-only, no MAAP) — `.env`, `.env.example`, config field.
2. **F2** (MAAP-required) — `src/orchestrator.py` signal handler + tests.

## 7. Test Strategy

- **F1:** 3 new tests for `culture_evaluation_interval_sec` field default + env override + monkeypatch isolation.
- **F2:** 3 new tests for signal handler: SIGTERM logs shutdown, SIGINT logs shutdown, handler does not mutate state.
- **Cumulative:** Full regression (2329 tests), coverage ≥80%.
- **MAAP:** Checker review of F2 against orchestrator signal handling.

## 8. Post-Implementation Validation

| Metric | Target | Was (Run 7) |
|---|---|---|
| CULTURE eval % | <5% | 16% |
| Shutdown log present | Yes | No |
| Total evals/60min | ≥100 | 57 (16 min) |
| Budget blocks | 0 | 0 |
| Errors | 0 | 0 |

## 9. Rollback Strategy

- **F1:** Revert `.env` to remove `CULTURE_EVALUATION_INTERVAL_SEC`. Default falls back to `NON_GROK_EVALUATION_INTERVAL_SEC=120`.
- **F2:** Revert `src/orchestrator.py` to remove signal handlers. No data loss.

## 10. Open Questions for User Sign-off

1. Should CULTURE be excluded entirely from activation (not just throttled)?
2. Is 300s the right CULTURE interval, or should it be 600s?
3. Should the next dry-run use `nohup` instead of background shell to prevent premature termination?

## 11. Timeline Estimate

- F1: 30 min (config + tests)
- F2: 45 min (code + tests + MAAP)
- Validation: 60 min (full dry-run window)
- **Total: ~2.25h**

## 12. What Could Go Wrong

- F1: If category cadence doesn't support per-category intervals, additional aggregator changes needed.
- F2: Signal handler could interfere with existing asyncio event loop cleanup. Must ensure handler is async-safe.

## 13. Definition of Done

- [ ] F1 committed with passing tests
- [ ] F2 committed with MAAP clearance and passing tests
- [ ] Full regression: 2329 passed, coverage ≥80%
- [ ] Fresh 60-minute dry-run confirms CULTURE eval % <5% and `orchestrator.shutdown` log on exit

## 14. Files-Touched Matrix

| File | Change | LOC Est. | MAAP |
|---|---|---|---|
| `.env` | Add `CULTURE_EVALUATION_INTERVAL_SEC=300` | +1 | No |
| `.env.example` | Add field documentation | +3 | No |
| `src/core/config.py` | Add config field | +8 | No |
| `src/orchestrator.py` | Signal handler | +12 | Yes |
| `tests/unit/test_config.py` | F1 tests | +15 | No |
| `tests/unit/test_orchestrator.py` | F2 tests | +20 | No |
| **Total** | | **~59 LOC** | |
