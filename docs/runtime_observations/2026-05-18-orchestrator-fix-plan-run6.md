# Orchestrator Fix Plan — Run 6

## 1. Why a Separate Planning Document

The observations report (`2026-05-18-orchestrator-dry-run-session-run6.md`) documents what happened. This plan defines what to change, in what order, and how to validate — without committing any code.

## 2. Newly Observed Signals Since Last Report

- Run 6 (40 min) confirmed Run 5 calibration stability: 0 budget blocks, 0 WS disconnects, 0 errors.
- CULTURE market eval budget waste quantified: 24% (52/209 evals), zero Grok signal, zero EV.
- Market rotation redundancy quantified: 3,630 activations in 40 min = 90/min for 15 markets.
- DeepSeek 100% EV arithmetic error rate confirmed across 209 evals.

## 3. Goals (Prioritized)

1. **G1:** Reduce CULTURE eval budget waste from 24% to <5% (config-only).
2. **G2:** Reduce market rotation event volume by 80% (config-only).
3. **G3:** Improve DeepSeek primary eval accuracy (code change, MAAP-gated).

## 4. Constraints

- MAAP required for any change under `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`.
- Atomic commits only. No "WIP" on `develop`.
- No `dry_run` weakening, no Gatekeeper bypass, no `float` for money.
- All changes must preserve `DRY_RUN=true` behavior.

## 5. Fix Inventory

### F1 — Increase CULTURE Evaluation Interval (MEDIUM → Config)

- **Severity:** MEDIUM
- **MAAP:** No (config-only)
- **Blast radius:** `.env`, `.env.example`, `README.md`, runbook
- **Why:** CULTURE consumes 24% of eval budget with zero Grok signal and structural EV=0.0.
- **What:** Add `CULTURE_EVALUATION_INTERVAL_SEC=300` to `.env` and wire into category cadence config.
- **Files:** `.env`, `.env.example`, `src/core/config.py`, `docs/runbooks/llm-cost-guard.md`
- **Code sketch:** Add `culture_evaluation_interval_sec: int = Field(default=300)` to `AppConfig`. Aggregator already reads category-specific intervals.
- **Tests:** Verify `AppConfig().culture_evaluation_interval_sec == 300` with env isolation.
- **Risk:** Low. CULTURE evals still happen, just less frequently.
- **Validation:** CULTURE eval count drops from ~24% to <5% in next dry-run.

### F2 — Suppress Redundant Market Activation Logging (MEDIUM → Code)

- **Severity:** MEDIUM
- **MAAP:** Yes (`src/orchestrator.py`)
- **Blast radius:** Operational event volume, log readability
- **Why:** 3,630 activation events in 40 min = 90/min. Same 15 markets re-logged every ~10s.
- **What:** Track active market set; only log `orchestrator.market_activated` on first activation per cycle.
- **Files:** `src/orchestrator.py`
- **Code sketch:**
  ```python
  # Before rotation loop
  self._active_market_ids: set[str] = set()

  # In activation loop
  if condition_id not in self._active_market_ids:
      log.info("orchestrator.market_activated", ...)
      self._active_market_ids.add(condition_id)
  # On deactivation
  self._active_market_ids.discard(condition_id)
  ```
- **Tests:** Verify activation log count drops from 90/min to 15 per cycle.
- **Risk:** Low. Only affects logging, not activation logic.
- **Validation:** `orchestrator.market_activated` count drops from 3,630/40min to ~90/40min (15 markets × 6 cycles).

## 6. Execution Sequence

1. **F1** (config-only, no MAAP) — `.env`, `.env.example`, config field, runbook update.
2. **F2** (MAAP-required) — `src/orchestrator.py` activation logging suppression + tests.

## 7. Test Strategy

- **F1:** 3 new tests for `culture_evaluation_interval_sec` field default + env override + monkeypatch isolation.
- **F2:** 5 new tests for activation dedup: first activation logs, subsequent skips, deactivation clears, empty set on startup, rotation cycle reset.
- **Cumulative:** Full regression (2329 tests), coverage ≥80%.
- **MAAP:** Checker review of F2 against orchestrator rotation logic.

## 8. Post-Implementation Validation

| Metric | Target | Was (Run 6) |
|---|---|---|
| CULTURE eval % | <5% | 24% |
| Activation events/min | <3 | 90 |
| Operational events/40min | <20,000 | 54,153 |
| Total evals/40min | ≥200 | 209 |
| Budget blocks | 0 | 0 |
| Errors | 0 | 0 |

## 9. Rollback Strategy

- **F1:** Revert `.env` to remove `CULTURE_EVALUATION_INTERVAL_SEC`. Default falls back to `NON_GROK_EVALUATION_INTERVAL_SEC=120`.
- **F2:** Revert `src/orchestrator.py` to restore unconditional activation logging. No data loss.

## 10. Open Questions for User Sign-off

1. Should CULTURE be excluded entirely from activation (not just throttled)?
2. Should the rotation interval be made configurable via `.env`?
3. Is 300s the right CULTURE interval, or should it be 600s?

## 11. Timeline Estimate

- F1: 30 min (config + tests)
- F2: 1h (code + tests + MAAP)
- Validation: 30 min (focused + full regression)
- **Total: ~2h**

## 12. What Could Go Wrong

- F1: If category cadence doesn't support per-category intervals, additional aggregator changes needed.
- F2: If activation dedup interferes with market rotation logic (e.g., markets not re-evaluated after deactivation), could cause eval gaps.

## 13. Definition of Done

- [ ] F1 committed with passing tests
- [ ] F2 committed with MAAP clearance and passing tests
- [ ] Full regression: 2329 passed, coverage ≥80%
- [ ] Fresh dry-run confirms CULTURE eval % <5% and activation events/min <3

## 14. Files-Touched Matrix

| File | Change | LOC Est. | MAAP |
|---|---|---|---|
| `.env` | Add `CULTURE_EVALUATION_INTERVAL_SEC=300` | +1 | No |
| `.env.example` | Add field documentation | +3 | No |
| `src/core/config.py` | Add config field | +8 | No |
| `src/orchestrator.py` | Activation dedup logic | +15 | Yes |
| `tests/unit/test_config.py` | F1 tests | +15 | No |
| `tests/unit/test_orchestrator.py` | F2 tests | +30 | No |
| `docs/runbooks/llm-cost-guard.md` | Update calibration table | +5 | No |
| **Total** | | **~77 LOC** | |
