# Orchestrator Fix Plan — 2026-05-18

**Author:** Claude Code (planning, no modifications)
**Companion document:** `2026-05-18-orchestrator-dry-run-session-run3.md` (observation report)
**Status:** UPDATED FOR CODEX REVIEW — implementation in progress on local `develop`.
**Target branch:** local `develop` per AGENTS.md. Current local branch is ahead of `origin/develop`; no direct `main` work.
**Scope discipline:** every change in this plan is intended to fix a *root cause observed in this session's logs.* No speculative refactors, no feature additions.

---

## 0. Why a separate planning document

The observation report (`2026-05-18-orchestrator-dry-run-session-run3.md`) catalogued 7 findings and 8 recommendations. This document converts the actionable subset into a sequenced execution plan with file-level scope, code sketches, test approach, MAAP requirements, risk ranking, and validation criteria — **without applying any of it.** It exists so a human reviewer (and a future Checker agent under MAAP) can approve the *plan* before any source-tree state changes.

The orchestrator (PID 43793) is **left running** during this planning step. It is in `daily_token_limit_exhausted` state and will not produce new evaluations until the next UTC day or a restart.

This is a **calibration pass**, not a stabilization pass. The five fixes below are config-tuning or policy-adjustment changes; none touch the Gatekeeper, execution path, live signing/broadcasting, or DRY_RUN posture.

### Code Review Revisions Applied Before Execution

Codex review found that the first draft had four implementation-shape problems:

1. Raising only `LLM_DAILY_TOKEN_LIMIT` would not produce a 9-hour run because `LLM_DAILY_COST_LIMIT_USD` would bind first. F1 now raises both caps together.
2. `MarketMetadata` has no `midpoint`/`spread` fields, so the activation-code sketch would not compile. F2 now uses the existing bounded discovery preflight path and its `spread / best_ask` semantics.
3. Removing `COOLDOWN_BLOCK` / `LLM_CALL_STARTED` would violate Phase 16 event-type coverage and break dashboard/replay/digest counts. F4 now throttles durable high-frequency diagnostic events while preserving event types and metrics.
4. The token metric must be set from the budget guard's rolling daily state, not incremented independently from usage events. F5 now adds prefixed Prometheus gauges sourced from `LLMBudgetGuard`.

---

## 1. Newly Observed Signal Since Report Was Written

No additional signals observed. The orchestrator remained in budget-exhausted state from T+54min through T+60min. The three snapshots (T+15, T+30, T+60) provide consistent data points.

One pattern worth noting for the operator: after `daily_token_limit_exhausted` kicked in at T+54min, the system entered a "graveyard" state where the WS ingestion continues (snapshots still inserting at ~112/min), the bounded queue coalesces (1,288 events in 60 min), and the operational event ledger continues to emit events — but zero evaluations fire. This silent idle is indistinguishable from a healthy idle at the process level (PID alive, WS connected, CPU ~1%), but the operator needs a dashboard signal or Telegram alert to differentiate "budget-exhausted idle" from "normal low-trading-activity idle."

---

## 2. Goals of This Plan

1. **Extend the daily token ceiling.** Current 1M tokens caps the bot at ~54 minutes/day. Target: 10M tokens for ~9 hours of continuous evaluation.
2. **Reduce wasted LLM budget on non-tradable markets.** Use existing order-book preflight to reject extreme bound-quote markets before activation.
3. **Align evaluation cadence with market signal availability.** Markets with no sentiment oracle should consume proportionally less evaluation budget without reordering the prompt queue.

What is **explicitly out of scope:**
- No `DRY_RUN=false` changes.
- No live signing / broadcasting.
- No Gatekeeper threshold changes.
- No PostgreSQL migration.
- No new WIs.
- No prompt-strategy or reflection-strategy redesign.
- No changes to the Run 2 hotfix code (all nine fixes verified working).
- No `circuit_breaker` enablement (stays `false` for dry-run).

---

## 3. Constraints and Non-Negotiables

Per `CLAUDE.md`:

- All work on `develop`, no direct commits to `main`.
- **MAAP** required for any change under `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, `src/backtest_runner.py`.
- Tests must stay ≥ 80% coverage.
- One logical change per commit (atomic).
- `Decimal()` for any money/EV path; no `float` regressions.
- No `dry_run` weakening.
- No execution path that bypasses `LLMEvaluationResponse`.
- Run end appends session summary to `03_Daily/2026-05-18.md`.

Plan execution order respects these by grouping config-only changes (no MAAP needed) before code changes (MAAP needed).

---

## 4. Fix Inventory — Ordered by Execution Sequence

---

### F0 — Pre-flight (housekeeping, no code changes)

**Severity:** N/A (operational)
**MAAP:** No
**Blast radius:** Local working directory only.

**Why.** Need a clean baseline for the next run, and need to preserve evidence from this run before logs roll over.

**What.**
1. Send SIGTERM to current orchestrator (PID 43793). Wait for clean exit.
2. Archive current log: `cp logs/orchestrator-run.log logs/orchestrator-run-2026-05-18-session.log`
3. Snapshot DB sizes / row counts to `docs/runtime_observations/2026-05-18-pre-fix-snapshot.txt`.
4. Stay on local `develop` per AGENTS.md; note that local `develop` is ahead of `origin/develop` and Run 3 docs are untracked.

**Tests.** None.

**Risk.** Low. Reversible by `git branch -D`.

**Validation.** `git status` clean on the new branch.

---

### F1 — Bump daily token limit (`llm_daily_token_limit`)

**Severity:** HIGH (structural throughput cap)
**MAAP:** No (config-only via `.env`)
**Blast radius:** Runtime behavior only. Tests do not consume `.env`.

**Why.** Per Finding 4.1: `llm_daily_token_limit=1,000,000` (`src/core/config.py:427`) is exhausted after 993,018 tokens (~54 minutes of operation). The default was set during the Run 2 hotfix when the hourly cap was the dominant constraint; it is now the binding constraint.

**What.** Add to `.env`:
```
LLM_DAILY_TOKEN_LIMIT=10000000
LLM_DAILY_COST_LIMIT_USD=30
```
This increases the rolling daily token ceiling 10× and raises the matching cost cap so the cost guard does not bind first. At observed Run 3 economics (~$2.57 per 1M tokens), 10M tokens costs roughly $25.70; `$30` gives a small paper-trading buffer while preserving a hard daily spend guard.

**Files.** `.env` (ignored local runtime config), `.env.example`, `README.md`, `docs/runbooks/llm-cost-guard.md`.

**Tests.** None required. Optional: verify `AppConfig` loads the env override correctly by checking startup logs for `llm_daily_token_limit=10000000` in the `provider_selected` or config bootstrap line. Out of scope for this fix.

**Risk.** DeepSeek pricing: at 1M tokens ≈ $2.57 observed in Run 3, 10M tokens ≈ $25.70/day. The cost cap remains the authoritative spend guard.

**Validation in next run.**
- 60-min observation window produces 0 `llm_budget_blocked reason=daily_token_limit_exhausted`.
- Evaluation cadence sustained for full 60 minutes.

---

### F2 — Spread-based discovery preflight filter

**Severity:** HIGH (budget efficiency)
**MAAP:** No for runtime `.env` enablement; tests/docs only if documenting the calibrated setting. Existing source path was implemented in WI-53.
**Blast radius:** Market discovery path only. No change to evaluation, execution, or WS ingestion.

**Why.** Per Finding 4.6: all 15 activated markets have 99.8% bid-ask spreads. The `MAX_SPREAD_PCT=0.015` Gatekeeper filter correctly blocks every evaluation, but the bot still evaluates these markets, consuming LLM budget to confirm "no edge" 228 times. Filtering at activation time avoids the budget spend entirely.

**What.** Enable the existing bounded market-discovery preflight and set a calibration threshold that rejects bound-quote markets without rejecting plausible 0.25/0.75 books:

```
ENABLE_MARKET_DISCOVERY_PREFLIGHT=true
MARKET_DISCOVERY_MAX_PREFLIGHT_CANDIDATES=25
PREFLIGHT_MAX_SPREAD_PCT=0.80
```

This uses the existing `MarketDiscoveryEngine._run_preflight()` check, which computes `spread / best_ask` with `Decimal`. A `0.001/0.999` book fails; a `0.25/0.75` book passes.

**Files.**
- `.env`
- `.env.example`
- `README.md`
- `docs/runbooks/llm-cost-guard.md`
- `tests/unit/test_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.py`

**Code sketch:**
```python
# Runtime configuration; no new activation fields are required.
ENABLE_MARKET_DISCOVERY_PREFLIGHT=true
MARKET_DISCOVERY_MAX_PREFLIGHT_CANDIDATES=25
PREFLIGHT_MAX_SPREAD_PCT=0.80
```

**Tests.**
1. `test_preflight_run3_extreme_spread_threshold_skips_bounds_quotes`: 0.001/0.999, threshold=0.80 → reject before activation.
2. `test_preflight_run3_threshold_allows_plausible_wide_market`: 0.25/0.75, threshold=0.80 → pass.

**Risk.**
- Preflight adds bounded CLOB REST calls. `MARKET_DISCOVERY_MAX_PREFLIGHT_CANDIDATES=25` limits blast radius.
- Order-book data can still change after activation. The Gatekeeper spread filter remains the final fail-closed check downstream.

**Validation in next run.**
- Activated market count drops from 15 to ~3 (only CRYPTO, IRAN, TECH if their spreads are below threshold).
- 60-min evaluation count on non-extreme-spread markets maintains ≥ 3.8/min cadence.
- No `activation_skip_extreme_spread` log for markets that are legitimately tight-spread.

---

### F3 — Category-aware evaluation cadence scaling

**Severity:** MEDIUM (budget allocation efficiency)
**MAAP:** YES — touches `src/agents/context/aggregator.py`, `src/orchestrator.py`, and config.
**Blast radius:** Context emission cadence only. No prompt queue reordering and no LLM budget counter changes.

**Why.** Per Finding 4.3: 74% of evaluations are CULTURE markets with zero Grok signal and EV=0.0. These consume budget without any edge. Scaling the evaluation cadence by category availability gives more budget to signal-rich markets (CRYPTO, IRAN) where non-zero EV is possible.

**What.** Add category-aware minimum emit intervals at the `DataAggregator` boundary:

```
ENABLE_CATEGORY_EVALUATION_CADENCE=true
GROK_ELIGIBLE_EVALUATION_INTERVAL_SEC=30
NON_GROK_EVALUATION_INTERVAL_SEC=120
```

This gives Grok-eligible markets the normal cadence and evaluates non-Grok categories at one quarter of that cadence. It does not reorder the bounded prompt queue, so task accounting and starvation behavior stay simple.

**Files.**
- `src/core/config.py`
- `src/agents/context/aggregator.py`
- `src/orchestrator.py`
- `tests/unit/test_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.py`

**Code sketch:**
```python
# aggregator.py
if category not in GROK_ELIGIBLE_CATEGORIES and elapsed < non_grok_interval:
    return  # no prompt queue insertion
```

**Tests.**
1. `test_non_grok_category_is_throttled_without_queue_reordering`.
2. `test_grok_eligible_category_uses_shorter_interval`.

**Risk.**
- Non-Grok categories are intentionally throttled but still get a guaranteed interval-based cadence.
- Category resolution remains the existing activation-time resolver; unresolved categories fall into the conservative non-Grok cadence.

**Validation in next run.**
- CRYPTO/IRAN evaluation rate ≥ CULTURE evaluation rate (was: roughly equal).
- No snapshot sits in queue > 30 seconds without evaluation.

---

### F4 — Throttle high-frequency diagnostic events in durable ledger

**Severity:** LOW (operational event volume)
**MAAP:** YES — touches the event publish path, likely `src/orchestrator.py` or the `OperationalEventBus`.
**Blast radius:** Event ledger only. No change to evaluation, execution, or WS ingestion.

**Why.** Per Finding 4.4: `COOLDOWN_BLOCK` events fire at 6.3/min and account for 31% of operational event growth (379 / 1,215 in 60 min). These are useful but high-frequency diagnostics. Phase 16 requires the event types to exist, so removal is not acceptable.

**What.** Add an orchestrator-side durable-publish throttle for `COOLDOWN_BLOCK` and `LLM_CALL_STARTED`, defaulting to one persisted event per `(event_type, reason_code)` per 60 seconds. Metrics still count every occurrence.

**Files.**
- `src/core/config.py`
- `src/orchestrator.py`
- `tests/unit/test_WI-56-operational-event-ledger.py`

**Code sketch:**
```python
if event_type in {COOLDOWN_BLOCK, LLM_CALL_STARTED} and within_throttle_window:
    return None
await event_bus.publish(event)
```

**Tests.**
1. `test_orchestrator_throttles_high_frequency_diagnostic_events`.
2. `test_orchestrator_never_throttles_critical_events`.

**Risk.**
- Dashboard, replay, and daily digest counts become sampled durable counts for high-frequency diagnostics. Prometheus metrics remain per-occurrence.

**Validation.**
- 60-min run: `operational_events` delta < 800 rows (down from 1,215).

---

### F5 — Add daily token usage Prometheus metric

**Severity:** LOW (operator visibility)
**MAAP:** YES — touches `src/observability/metrics.py`.
**Blast radius:** Metrics collection layer only.

**Why.** The operator currently has no dashboard visibility into token consumption vs. the daily cap. The `llm_budget_blocked reason=daily_token_limit_exhausted` log line is the first indication that the cap was hit — too late for preventive action.

**What.** Add Prometheus gauges sourced from the budget guard's rolling daily state:

- `poly_agent_llm_daily_token_usage_total{provider=...}`
- `poly_agent_llm_daily_token_limit{provider=...}`
- `poly_agent_llm_daily_token_utilization_ratio{provider=...}`

**Files.**
- `src/observability/metrics.py`
- `src/agents/evaluation/llm_cost_guard.py`
- `tests/unit/test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py`

**Code sketch:**
```python
# metrics.py
await metrics.set_llm_daily_token_usage(
    provider=provider.value,
    total_tokens=self._total_tokens_consumed,
    token_limit=self._config.llm_daily_token_limit,
)
```

**Tests.**
1. `test_metrics_track_daily_token_usage_by_provider`.
2. `test_budget_guard_usage_updates_daily_token_gauge`.

**Risk.** Negligible. Gauge is read-only observation.

**Validation.**
- `GET /metrics` endpoint includes `poly_agent_llm_daily_token_usage_total{provider="deepseek"} <value>`.

---

## 5. Execution Sequence (sequenced commits)

| Order | Fix | Commit message | Depends on | MAAP needed |
|---|---|---|---|---|
| 1 | F0 | (no commit) | — | — |
| 2 | F1 | `chore(config): raise dry-run token and cost caps for sustained evaluation` | F0 | No |
| 3 | F2 | `chore(config): enable bounded spread preflight for dry-run calibration` | F0 | No |
| 4 | F3 | `feat(context): throttle non-grok evaluation cadence before queueing` | F0 | **Yes** |
| 5 | F4 | `feat(events): throttle durable high-frequency diagnostic events` | F0 | **Yes** |
| 6 | F5 | `feat(metrics): add daily token usage Prometheus gauge` | F0 | **Yes** |

Total: 3 MAAP-gated code commits plus local/runtime config and docs. Plus pre-flight (F0).

After all 5 land on `develop`, open PR `develop → main` for Phase 16.6 calibration release.

---

## 6. Test Strategy (cumulative)

For each MAAP-gated commit:

1. Run targeted tests for the file(s) touched.
2. Run full suite: `.venv/bin/python -m pytest --asyncio-mode=auto tests/`
3. Run coverage check: `.venv/bin/python -m coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/python -m coverage report -m`
4. Coverage must remain ≥ 80% (per CLAUDE.md). Current baseline: 93%.
5. `ruff check .` + `ruff format --check .` pass.
6. Author posts `git diff` for Checker MAAP review.

Cumulative regression after all commits land: full suite + 60-min orchestrator dry-run validation (see Section 7).

---

## 7. Post-Implementation Validation (the next dry-run)

After all 5 commits land, run the orchestrator for 60 minutes and assert:

| Metric | Target | Was (this session) |
|---|---|---|
| Evaluations / 60 min | ≥ 200 | 228 |
| `daily_token_limit_exhausted` blocks | 0 | 48 |
| Activated markets not rejected by 0.80 preflight spread/ask threshold | ≥ 1 | 0 (all extremes) |
| Grok-eligible eval fraction | > 30% | 26% (59/228) |
| `operational_events` delta / hr | < 800 | 1,215 |
| `llm_daily_token_usage_total` on `/metrics` | present | absent |
| Errors / Tracebacks | 0 | 0 |
| WS disconnects | 0 | 0 |
| Log file size after 1 hour | < 30 MB | 25 MB |

If any target is missed, document and decide whether to revert / patch / defer.

---

## 8. Rollback Strategy

Each commit is small and isolated:

- **Single fix regressed:** `git revert <sha>` on `develop`. Open targeted PR.
- **Full rollback:** revert the merge commit on `develop` via `git revert -m 1 <merge-sha>`.
- **Config-only F1:** remove the `LLM_DAILY_TOKEN_LIMIT` line from `.env` and restart. Instant revert.

No DB schema changes in this plan. No Alembic migration required. Rollback is purely code/config revert.

---

## 9. Open Questions to Resolve Before Execution

1. **Daily token budget philosophy.** Execution uses `LLM_DAILY_TOKEN_LIMIT=10000000` plus `LLM_DAILY_COST_LIMIT_USD=30`, preserving a hard cost guard above the observed ~$25.70/10M-token rate.

2. **Spread pre-filter threshold.** Execution uses existing preflight semantics (`spread / best_ask`) with `PREFLIGHT_MAX_SPREAD_PCT=0.80`, so 0.001/0.999 bound quotes fail while 0.25/0.75 books pass.

3. **Category preference vs. fairness.** F3 introduces a preference bias toward Grok-eligible markets. Should CULTURE markets still get a guaranteed minimum evaluation cadence (e.g., at least 1 eval per 2 minutes) to avoid complete starvation?

4. **COOLDOWN_BLOCK removal impact.** Resolved by throttling durable diagnostic events instead of removing event types. Dashboard/replay/digest still see typed events.

5. **Branch strategy.** Should each fix be its own PR (per atomic-commit convention), or all 4 MAAP-gated commits in a single branch? Single branch is faster for a calibration pass; split branches are better for review.

6. **Do we stop the current orchestrator before starting F0?** Completed during implementation. PID 43793 was stopped with SIGTERM and no longer appears in `ps`.

---

## 10. Timeline Estimate

| Phase | Time | Notes |
|---|---|---|
| F0 pre-flight | 10 min | Stop orchestrator, archive, branch |
| F1 env edit | 5 min | Single line in `.env` |
| F2 spread filter | 20 min | Runtime config + preflight tests |
| F3 category cadence | 60 min | Context emission code + tests |
| F4 event throttle | 45 min | Orchestrator publish throttle + tests |
| F5 token metric | 30 min | 2 tests; ~11 lines code |
| Cumulative regression | 30 min | Full suite + coverage |
| 60-min validation run | 75 min | Per Section 7 |
| **Total** | **~4.75 hours** | Assuming everything passes |

Realistic with MAAP review iterations: **~6 hours.** Can complete in one session.

---

## 11. What Could Go Wrong

- **F2 (spread filter) rejects too much.** The 0.80 `spread / best_ask` threshold intentionally targets bound-quote books; tighten only after observing at least one real tight market.
- **F3 (category cadence) under-samples CULTURE markets.** Non-Grok categories still receive interval-based evaluation every 120s by default; adjust via `.env` if operator wants a different budget split.
- **F4 (event throttle) changes durable diagnostic counts.** Dashboard/replay/digest continue to work, but high-frequency durable counts become sampled counts. Prometheus remains the per-occurrence source of truth.
- **F1 (token limit) depletes the DeepSeek quota faster than expected.** Verify the DeepSeek account's daily rate limit and per-minute rate limit before bumping the token ceiling. The bot's internal guard should align with, not exceed, the provider's rate limit.

---

## 12. Definition of Done

This plan is "done" (ready to execute) when:

- [x] Section 9 open questions resolved through Code Review revisions.
- [x] Implementation proceeds on local `develop` per AGENTS.md.
- [x] Orchestrator stopped before changes; no new live findings are arriving during implementation.

Once executed (all 5 commits merged + 60-min validation passes Section 7 targets), this plan is "delivered" and should be archived into `04_Archive/poly-oracle-agent/runtime_observations/`.

---

## 13. Files Touched by This Plan (none yet — planning only)

| File | Fix | Change kind | LOC est. |
|---|---|---|---|
| `.env` | F1 | Add 1 line | +1 / -0 |
| `src/core/config.py` | F3, F4 | Add fields | +4 / -0 |
| `src/orchestrator.py` | F3, F4 | Configure cadence + event throttle | +55 / -0 |
| `src/agents/context/aggregator.py` | F3 | Category cadence before queueing | +35 / -0 |
| `src/observability/metrics.py` | F5 | New gauge | +8 / -0 |
| `src/agents/evaluation/llm_cost_guard.py` | F5 | Gauge set from budget state | +10 / -0 |
| `tests/unit/test_WI-53-market-eligibility-*.py` | F3 | New tests | +25 / -0 |
| `tests/unit/test_WI-56-operational-event-ledger.py` | F4 | Regression test | +10 / -0 |
| `tests/unit/test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py` | F5 | New metric tests | +20 / -0 |
| **Total** | | | **~143 / -5** |

This is a **small calibration PR.** All 4 MAAP-gated commits can live on one branch.

---

## 14. Closing

This plan is conservative and surgical. It does not change any business logic, prompt strategy, model selection, Gatekeeper threshold, or DRY_RUN posture. It calibrates three knobs — token budget, market activation policy, and evaluation allocation — based on measured data from the first clean 60-minute post-stabilization observation.

The single most impactful fix is **F1 (daily token limit bump)**: it converts the system from a 54-minute daily ceiling to a 9-hour ceiling with one line in `.env`. **F2 (spread filter)** is the highest-value code change: it would reduce the activated market count from 15 to ~3, reallocating 80%+ of the LLM budget from non-tradable extreme-spread markets to the 2-3 with potential edge.

This is the first time in the project's observation history that all fixes are calibration adjustments rather than defect fixes. That is a milestone worth noting.
