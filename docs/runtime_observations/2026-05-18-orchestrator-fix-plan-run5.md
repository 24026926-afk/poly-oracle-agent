# Orchestrator Fix Plan — 2026-05-18

**Author:** Claude Code (planning, no modifications)
**Companion document:** `2026-05-18-orchestrator-dry-run-session-run5.md` (observation report)
**Status:** PLAN ONLY — no code or config changes have been applied as part of this document.
**Target branch (not yet created):** `feat/runtime-stabilization-run5`
**Scope discipline:** every change in this plan is intended to fix a *root cause observed in this session's logs.* No speculative refactors, no feature additions.

---

## 0. Why a Separate Planning Document

The observation report (`2026-05-18-orchestrator-dry-run-session-run5.md`) catalogued 9 findings (3 MEDIUM, 6 LOW). This document converts the 3 MEDIUM findings into a sequenced execution plan with file-level scope, code sketches, test approach, MAAP requirements, risk ranking, and validation criteria — **without applying any of it.** It exists so a human reviewer (and a future Checker agent under MAAP) can approve the *plan* before any source-tree state changes.

The orchestrator (PID 56906) is **left running** during this planning step.

---

## 1. Newly Observed Signals Since Report Was Written

**Per-market hourly cap fires with precise regularity.** The 4 `llm_budget_blocked` events correspond to 3 distinct pause cycles at evals 21, 41, and 66. The gap sizes (290s, 240s, 300s) are consistent with a 300-second sliding window refresh. This confirms the cap is the dominant throttle, not incidental.

**CULTURE market midpoint-anchoring is systematic.** All 60 CULTURE evaluations produce either EV=0.0 (neutral midpoint) or EV=-0.84/-0.90 (spread-adjusted negative). The reflection layer correctly identifies `narrative_anchoring_on_midpoint` in the majority of cases. Without Grok sentiment, CULTURE evaluations are structurally unable to diverge from p_true=0.5.

---

## 2. Goals (Prioritized)

1. **G1:** Reduce evaluation-idle ratio from ~17% active / 83% idle to ≥50% active by adjusting per-market budget caps.
2. **G2:** Increase the probability that activated markets can produce positive EV by tightening the preflight spread gate from 0.99 to a calibrated value.
3. **G3:** Reduce wasted evaluations on zero-signal markets by either expanding Grok coverage or deprioritizing CULTURE in market activation.

---

## 3. Constraints

- **MAAP:** Every change touching `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, or `src/backtest_runner.py` requires MAAP review before commit.
- **Atomicity:** One logical change per commit. No bundled fixes.
- **Decimal integrity:** No `float` in money/EV/pricing paths.
- **No `dry_run` weakening:** Execution guards must never be bypassed.
- **No Gatekeeper bypass:** `LLMEvaluationResponse` must remain the terminal decision schema.
- **Coverage:** New code must not decrease coverage below 80%.
- **Backward compatibility:** Changes to budget logic must not break existing tests.

---

## 4. Fix Inventory

### F1: Increase `LLM_MARKET_HOURLY_CALL_LIMIT` (config-only)

| Field | Value |
|---|---|
| Severity | MEDIUM (M1) |
| MAAP Required | No (`.env` config change only) |
| Blast Radius | Low — affects only the per-market budget throttle |
| Why | 60 calls/hour with primary+reflection double-counting = 30 effective evaluations/market/hour. With 3 CULTURE markets, the cap triggers in ~10 min and leaves ~50 min idle. |
| What | Increase `LLM_MARKET_HOURLY_CALL_LIMIT` from 60 to 120 in `.env`. Also update `.env.example` to match. |
| Files | `.env:48`, `.env.example` |
| Code Sketch | N/A (config values only) |
| Tests | Verify `test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py` passes with new limit. Add assertion that per_market window with 120 calls allows ≥60 evaluations/hour with primary+reflection. |
| Risk | Low. The global hourly limit (240) and daily limit (10M tokens) still provide cost ceiling. 120 calls/market × 4 markets = 480 max calls/hour, under the 480 global cap (240 primary + 240 reflection). |
| Validation | Run 10-minute dry-run. Confirm 0 `per_market_hourly_limit_exhausted` blocks. |

### F2: Recalibrate `PREFLIGHT_MAX_SPREAD_PCT` to 0.90

| Field | Value |
|---|---|
| Severity | MEDIUM (M2) |
| MAAP Required | No (`.env` config change only) |
| Blast Radius | Medium — changes which markets activate. May reduce activated market count. |
| Why | `PREFLIGHT_MAX_SPREAD_PCT=0.99` allows markets with 96-98% spreads to pass preflight. These markets are structurally unactionable — spreads are 65× the 1.5% Gatekeeper threshold. The Run 3 calibration at 0.80 blocked all markets; 0.90 is a midpoint that allows reasonable-spread markets while excluding the worst. |
| What | Change `PREFLIGHT_MAX_SPREAD_PCT` from 0.99 to 0.90 in `.env`. |
| Files | `.env:55` |
| Code Sketch | N/A (config value only) |
| Tests | No new tests needed — existing preflight tests in `test_WI-53-*` already validate spread gate behavior. |
| Risk | Medium. At 0.90, markets with spread > 90% of ask will be rejected. Today's IRAN market (bid=0.01, ask=0.99, spread/ask=0.99) would fail. Activation count may drop to 0 — same as Run 3's 0.80. Mitigation: if 0.90 blocks all, fall back to 0.95 and test. |
| Validation | Launch orchestrator, confirm activated markets have spread/ask < 0.90. |

### F3: Extend `GROK_ELIGIBLE_CATEGORIES` to include CULTURE

| Field | Value |
|---|---|
| Severity | MEDIUM (M3) |
| MAAP Required | Yes — touches `src/schemas/llm.py` |
| Blast Radius | Medium — changes Grok API call volume. CULTURE is 3 of 4 activated markets, so Grok call volume could increase significantly. |
| Why | CULTURE markets (3 of 4 activated) receive zero sentiment signal. All CULTURE evaluations are rejected by reflection for midpoint anchoring. Including CULTURE in Grok eligibility would provide sentiment input, reducing the reflection rejection rate and potentially surfacing real signals. |
| What | Add `MarketCategory.CULTURE` to the `GROK_ELIGIBLE_CATEGORIES` frozenset at `src/schemas/llm.py:91-102`. Also verify that the Grok prompt template in `src/agents/evaluation/grok_client.py` handles cultural/entertainment prediction markets appropriately. |
| Files | `src/schemas/llm.py:91-102`, `src/agents/evaluation/grok_client.py` (prompt review only) |
| Code Sketch | ```python
GROK_ELIGIBLE_CATEGORIES: frozenset["MarketCategory"] = frozenset(
    {
        MarketCategory.CRYPTO,
        MarketCategory.POLITICS,
        MarketCategory.ELECTIONS,
        MarketCategory.GEOPOLITICS,
        MarketCategory.FINANCE,
        MarketCategory.TECH,
        MarketCategory.IRAN,
        MarketCategory.ECONOMY,
        MarketCategory.CULTURE,  # F3: added for Run 5 calibration
    }
)
```
|
| Tests | Add unit test verifying `CULTURE in GROK_ELIGIBLE_CATEGORIES` is True. Update any negative tests that assert CULTURE exclusion. Run `test_WI-53-*` to confirm no Grok eligibility regressions. |
| Risk | Medium-High. CULTURE markets have different signal characteristics than political/financial categories. The Grok prompt may need tuning for cultural context (awards shows, celebrity bets, entertainment outcomes). Also, xAI rate limits: more eligible categories = more Grok calls. Mitigation: monitor Grok HTTP 429 rate after applying F3. |
| Validation | Dry-run 10 minutes. Confirm CULTURE markets receive `grok_sentiment status=SUCCESS` (not `SKIPPED_CATEGORY`). |

---

## 5. Execution Sequence

Atomic commits, dependency-ordered:

```
Commit 1: F1 — config: bump LLM_MARKET_HOURLY_CALL_LIMIT 60→120
  ├─ .env:48
  └─ .env.example (corresponding line)
  [NO MAAP required — config only]

Commit 2: F2 — config: recalibrate PREFLIGHT_MAX_SPREAD_PCT 0.99→0.90
  └─ .env:55
  [NO MAAP required — config only]
  [DEPENDS ON: Commit 1 validated first]

Commit 3: F3 — feat: add CULTURE to GROK_ELIGIBLE_CATEGORIES
  ├─ src/schemas/llm.py:91-102
  └─ tests/unit/test_WI-53-* (eligibility assertion)
  [MAAP-REQUIRED — touches src/schemas/]
  [DEPENDS ON: Commit 2 validated first, to avoid Grok spam on markets that will be rejected by spread gate]
```

Order rationale: Fix the budget throttle (F1) first so we can observe clean evaluation cadence. Then tighten the spread gate (F2) so we only evaluate potentially-actionable markets. Finally, expand Grok coverage (F3) so the markets that survive preflight have sentiment input.

---

## 6. Test Strategy

### Per-Commit Tests

| Commit | Test Command | Expected |
|---|---|---|
| F1 | `.venv/bin/python -m pytest tests/unit/test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py -v` | All pass; new limit accepted |
| F2 | `.venv/bin/python -m pytest tests/unit/test_WI-53-* -v` | All pass; spread gate unchanged |
| F3 | `.venv/bin/python -m pytest tests/unit/test_WI-53-* tests/unit/test_schemas.py -v -k "grok"` | CULTURE eligibility asserted |

### Cumulative Regression

```bash
.venv/bin/python -m pytest tests/ --asyncio-mode=auto
```

### Coverage Gate

```bash
.venv/bin/python -m coverage run -m pytest tests/ --asyncio-mode=auto
.venv/bin/python -m coverage report -m
# Target: ≥80%
```

---

## 7. Post-Implementation Validation

| Metric | Target | Pre-Fix (Run 5) |
|---|---|---|
| `per_market_hourly_limit_exhausted` blocks | 0 in 60 min | 4 |
| Eval cadence (avg/min) | ≥2.0 | 1.58 |
| Activated markets with spread > 90% | 0 | 4 |
| CULTURE Grok SKIPPED_CATEGORY | 0 | 460 |
| CULTURE Grok SUCCESS | >0 | 0 |
| approved=True | Unknown (depends on market) | 0 |
| EV > 0 | Unknown (depends on market) | 0 |
| Gatekeeper bypass | 0 | 0 |
| Tracebacks/ERRORs | 0 | 0 |
| Test coverage | ≥80% | ≥80% |

Validation procedure:
1. Apply commits 1-3 sequentially.
2. Launch orchestrator with 15-minute observation window.
3. Capture T+5, T+10, T+15 snapshots.
4. Compare against pre-fix metrics in the table above.

---

## 8. Rollback Strategy

| Change | Rollback |
|---|---|
| F1 (LLM_MARKET_HOURLY_CALL_LIMIT) | Revert `.env` line to 60. Instant effect on next budget cycle. |
| F2 (PREFLIGHT_MAX_SPREAD_PCT) | Revert `.env` line to 0.99. Requires orchestrator restart (market activation is startup-only). |
| F3 (GROK_ELIGIBLE_CATEGORIES) | `git revert` commit 3. Remove CULTURE from frozenset. Requires orchestrator restart. |

All rollbacks are reversible without data loss. No schema migrations are involved.

---

## 9. Open Questions for User Sign-Off

1. **Q1:** Is 0.90 the right spread threshold? The Run 3 session showed 0.80 blocked all markets. At 0.90, we may still lose the IRAN market (spread/ask = 0.99). Alternative: 0.95 as a more conservative step from 0.99.
2. **Q2:** Is CULTURE the right category to add to Grok eligibility, or should we instead adjust market activation to *prefer* already-eligible categories (CRYPTO, FINANCE, TECH) if they can pass a tighter spread gate?
3. **Q3:** Should primary and reflection calls be tracked in separate per-market budget windows? This is a larger code change but would effectively double the useful evaluation ceiling without increasing token consumption.
4. **Q4:** Should this plan be executed immediately, or should we wait for the next scheduled Work Item?

---

## 10. Timeline Estimate

| Phase | Effort |
|---|---|
| Commit 1 (F1): config change + test | 5 min |
| Commit 2 (F2): config change + test | 5 min |
| Commit 3 (F3): code change + test | 15 min |
| MAAP review (Checker agent) | 10 min |
| Cumulative regression + coverage | 10 min |
| Post-implementation validation dry-run | 15 min |
| **Total estimated** | **~60 min** |

---

## 11. What Could Go Wrong

1. **F2 blocks all markets again.** If 0.90 still rejects all activatable markets (as 0.80 did in Run 3), we have no evaluation surface. Mitigation: prepare 0.95 and 0.97 fallback values.
2. **F3 triggers xAI rate limit cascade.** Adding CULTURE (3 markets, ~1.5 evals/min each) could add ~270 Grok calls/hour. Combined with IRAN's ~35 calls/hour, that's ~305 Grok calls/hour. xAI's free-tier rate limit is unknown. Mitigation: monitor HTTP 429 rate and back off if >5 in first 10 minutes.
3. **F1 + F2 together may still produce zero actionable evals.** Even with relaxed budget and tighter spread, if the remaining activated markets still have 98% spreads, no trade can occur. The root issue is market selection, not evaluation throughput.
4. **Reflection cost increases with more evaluations.** F1 doubles the effective evaluation ceiling, which means more reflection calls. The reflection hourly limit (240) should absorb this, but verify.

---

## 12. Definition of Done

- [ ] F1: `LLM_MARKET_HOURLY_CALL_LIMIT=120` in `.env` and `.env.example`
- [ ] F2: `PREFLIGHT_MAX_SPREAD_PCT=0.90` in `.env`
- [ ] F3: `CULTURE` added to `GROK_ELIGIBLE_CATEGORIES` in `src/schemas/llm.py`
- [ ] All 3 commits pass MAAP review (Commit 3 requires MAAP)
- [ ] Full test suite passes (≥2314 tests, ≥80% coverage)
- [ ] 15-minute validation dry-run shows:
  - 0 `per_market_hourly_limit_exhausted` blocks
  - 0 CULTURE `SKIPPED_CATEGORY` Grok events (if CULTURE markets activate)
  - 0 Gatekeeper bypasses
  - 0 Tracebacks/ERRORs
- [ ] Observability subsystems remain operational (telegram, operational_alerts, operational_event_ledger)
- [ ] `circuit_breaker` remains disabled (no change)

---

## 13. Files-Touched Matrix

| File | F1 | F2 | F3 | LOC Change | MAAP |
|---|---|---|---|---|---|
| `.env` | L48 | L55 | — | 2 line edits | No |
| `.env.example` | L48 | — | — | 1 line edit | No |
| `src/schemas/llm.py` | — | — | L91-102 | +1 line | **Yes** |
| `tests/unit/test_WI-53-*` | — | — | eligibility test | ~5 lines | No |

**Total LOC change:** ~8 lines (6 config, 2 source + test)
