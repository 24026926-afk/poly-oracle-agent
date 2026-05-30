# Orchestrator Fix Plan — 2026-05-23

**Author:** Claude Code (planning, no modifications)
**Companion document:** `2026-05-23-orchestrator-dry-run-session.md` (observation report)
**Status:** PLAN ONLY — no code or config changes have been applied as part of this document.
**Target branch (not yet created):** `feat/runtime-stabilization-2026-05-23`
**Scope discipline:** every change in this plan is intended to fix a *root cause observed in this session's logs.* No speculative refactors, no feature additions.

---

## 0. Why a Separate Planning Document

The 2026-05-23 observation report catalogued 1 NEW MEDIUM finding (cognitive cooldown 65% block rate), 2 carry-over MEDIUM-to-LOW findings whose 2026-05-17 fixes were planned but never landed (per-market activation log dedup, WS snapshot throttle), 2 new LOW findings (degenerate-quote skip flood, RSS growth), and 1 carry-over LOW (skip_last_trade_no_book noise). This document converts the actionable subset into a sequenced execution plan with file-level scope, code sketches, test approach, MAAP requirements, and rollback — **without applying any of it.**

The orchestrator (PID 23542) is **left running** during this planning step. It is in cooldown-loop steady-state and will keep running until the executor explicitly stops it in F0.

---

## 1. Newly Observed Signal Since Report Was Written

None. Monitor was stopped before this plan began. The observation report is the complete signal set.

---

## 2. Goals of This Plan

In priority order:

1. **Unblock multi-day unattended dry-run.** Today the DB grows ~140 MB/hr; a week's dry-run hits 25 GB. Target: <30 MB/hr DB growth so the Droplet can run unattended for ≥7 days.
2. **Make cooldown saturation a first-class metric.** Today 65% of LLM-eval attempts are short-circuited by cooldown, but this is only visible by counting WARNING lines. Target: a `cognitive_cooldown.block_rate` field surfaced in WI-61 runtime audit and WI-60 daily digest.
3. **Reduce log volume to where the real story is visible.** Today >4,600 of every ~5,400 log lines per 15 min are noise (`market_activated` re-emit + `skip_no_token_non_positive_yes_quote`). Target: drop these two to ≤200 lines / 15 min combined.
4. **Reconcile config drift.** `PREFLIGHT_MAX_SPREAD_PCT=0.99` (live) vs `0.90` (STATE.md). One of them is wrong.
5. **Keep every change MAAP-clean** with explicit test coverage and a documented rollback per fix.

What is **explicitly out of scope** for this plan:
- No `DRY_RUN=false` changes.
- No live signing / broadcasting.
- No Gatekeeper threshold changes (`min_confidence=0.75` stays). The shadow-Gatekeeper question is its own future WI (see §10 of this plan).
- No PostgreSQL migration.
- No prompt-strategy or reflection-strategy redesign.
- No cooldown threshold tuning yet — we surface the metric first, tune in a follow-up WI once we have data.
- No heap snapshot tracing in this plan — that is a one-off debugging task in the next dry-run, not a code change.

---

## 3. Constraints and Non-Negotiables

Per `CLAUDE.md`:

- All work on `develop`, no direct commits to `main`.
- **MAAP** required for any change under `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, `src/backtest_runner.py`.
- Tests must stay ≥80% coverage.
- One logical change per commit (atomic).
- `Decimal()` for any money/EV path; no `float` regressions.
- No `dry_run` weakening.
- No execution path that bypasses `LLMEvaluationResponse`.
- No raw SQL in agent code; repository pattern only.
- Run end appends a session summary to `03_Daily/2026-05-23.md`.

---

## 4. Fix Inventory — Ordered by Execution Sequence

Each fix below has the same structure: severity, MAAP requirement, blast radius, why, what, files, code sketch (pseudocode), tests, risk, validation.

---

### F0 — Pre-flight (housekeeping, no code changes)

**Severity:** N/A (operational)
**MAAP:** No
**Blast radius:** Local working directory only.

**Why.** Need a clean baseline for the next run, and need to preserve evidence from this run.

**What.**
1. Send SIGTERM to current orchestrator (PID 23542). Wait for clean exit via `until ! kill -0 23542; do sleep 2; done`.
2. Archive current logs: `logs/orchestrator-run.log` → `logs/orchestrator-run-2026-05-23-15min.log`.
3. Snapshot DB sizes / row counts to `docs/runtime_observations/2026-05-23-pre-fix-snapshot.txt`.
4. Create branch `feat/runtime-stabilization-2026-05-23` off `develop`.
5. Verify `git status` shows the expected pre-existing untracked files (`docs/runtime_observations/2026-05-19-*.md`, `scripts/auto_snapshots.sh`, plus this run's new reports/snapshots/logs) and nothing else.

**Tests.** None.
**Risk.** Low. Reversible by `git branch -D`.
**Validation.** `git status` clean on the new branch except for expected untracked files.

---

### F1 — Reconcile `PREFLIGHT_MAX_SPREAD_PCT` drift (`.env` / STATE.md only)

**Severity:** LOW (operational hygiene)
**MAAP:** No (config / docs only)
**Blast radius:** None at runtime if `.env` value is preserved; documentation alignment only.

**Why.** STATE.md (line 47) records the 2026-05-18 calibration as `PREFLIGHT_MAX_SPREAD_PCT=0.90`. Live `.env` reads `0.99`. One of them is wrong, and the drift itself is a process risk: future operators reading STATE.md will believe the calibration is one thing while the bot acts on another.

**What.** Operator decision — choose one:
- **Option A:** the documented `0.90` is the intended calibration; restore in `.env`.
- **Option B:** the live `0.99` is the intended calibration; update STATE.md to match and note the date of the change.

Either way, both files must agree at the end.

**Files.** `.env` (uncommitted, operator-local) OR `STATE.md` (committed).
**Tests.** None.
**Risk.** Option A might re-introduce the cadence-suppression symptom the original 0.99 was set to escape. Option B leaves the bot running on a value not documented anywhere. Operator must decide.
**Validation.** `grep -h PREFLIGHT_MAX_SPREAD_PCT .env STATE.md` returns matching values.

---

### F2 — Surface `cognitive_cooldown.block_rate` as a first-class metric

**Severity:** MEDIUM (observability of the new headline finding)
**MAAP:** YES — touches `src/schemas/runtime_audit.py`, `src/observability/runtime_audit.py`, `src/schemas/ops.py`.
**Blast radius:** Runtime audit (WI-61) output schema, optionally daily digest (WI-60) summary line. No effect on evaluation path.

**Why.** Per Finding 4.1: 138 `COOLDOWN_BLOCK severity=WARNING` events vs 75 `Evaluation complete` events in 15 min = 65% block rate. Today this is only inferable by counting WARNING lines in the ledger. Target: surface as a typed metric in `RuntimeAuditSummary` so it is alertable.

**What.** Extend `RuntimeAuditSummary` with a `cognitive_cooldown_block_rate` field (Decimal, 0.0–1.0 range, `_reject_float` validator per WI-61 invariants). Populate it via the existing `OperationalEventRepository.get_recent_*` cursor by counting `COOLDOWN_BLOCK` events over the audit window and dividing by `Evaluation complete` count from `DecisionRepository`.

**Files.**
- `src/schemas/runtime_audit.py` (+1 field, +1 `_reject_float` validator)
- `src/observability/runtime_audit.py` (+~25 LoC in the summary aggregation path)
- `tests/unit/test_WI-61-periodic-runtime-audit.py` (+3 cases)
- `tests/integration/test_WI-61-periodic-runtime-audit.py` (+1 case)

**Code sketch (pseudocode, NOT final):**
```python
# src/schemas/runtime_audit.py
class RuntimeAuditSummary(BaseModel):
    ...
    cognitive_cooldown_block_rate: Decimal = Field(
        ...,
        ge=Decimal("0"),
        le=Decimal("1"),
        description="Fraction of LLM evaluation attempts blocked by cognitive cooldown in the audit window.",
    )

    @field_validator("cognitive_cooldown_block_rate", mode="before")
    @classmethod
    def _reject_float(cls, v):
        if isinstance(v, float):
            raise ValueError("cognitive_cooldown_block_rate must be Decimal, not float")
        return v

# src/observability/runtime_audit.py
async def _compute_cooldown_block_rate(
    *,
    op_repo: OperationalEventRepository,
    decision_repo: DecisionRepository,
    cutoff: datetime,
    limit: int,
) -> Decimal:
    cooldowns = await op_repo.get_recent_by_type(
        event_type=OperationalEventType.COOLDOWN_BLOCK,
        cutoff=cutoff,
        limit=limit,
    )
    evals = await decision_repo.get_recent_decisions(cutoff=cutoff, limit=limit)
    denom = Decimal(len(cooldowns) + len(evals))
    if denom == 0:
        return Decimal("0")
    return (Decimal(len(cooldowns)) / denom).quantize(Decimal("0.0001"))
```

**Tests.**
1. `test_cooldown_block_rate_zero_when_no_events`: empty window → returns `Decimal("0")`.
2. `test_cooldown_block_rate_typical`: 138 cooldowns, 75 evals → returns `Decimal("0.6479")`.
3. `test_cooldown_block_rate_only_evals`: 0 cooldowns, 75 evals → returns `Decimal("0")`.
4. `test_cooldown_block_rate_rejects_float_in_construction`: passing a `float` raises validation error.
5. Integration: full audit run with seeded cooldowns and decisions returns the correct rate in `RuntimeAuditReport`.

**Risk.**
- Existing WI-61 audit JSON schema is widened. Downstream consumers (dashboard, daily digest) must tolerate the new field. Mitigation: field is purely additive; old consumers ignore unknown fields.
- Counting cooldowns and evals separately from two repositories opens a small race window (audit could run between cooldown insert and decision insert for the same logical attempt). Acceptable — rate is a percentage, not a balance, and the next audit cycle corrects.

**Validation in next run.**
- WI-61 audit JSON includes `cognitive_cooldown_block_rate=0.6479` (or similar) for a 15-min window that reproduces the 138:75 ratio.
- Daily digest (WI-60) optionally surfaces the metric as a bullet line.

---

### F3 — De-duplicate `orchestrator.market_activated` per-market log

**Severity:** LOW (log noise, but largest single source)
**MAAP:** YES — touches `src/orchestrator.py`.
**Blast radius:** Logging only. No behavioral change.

**Why.** Per Finding 4.2: 1,410 INFO lines in 15 min for an unchanged activated set. The dedup guard at `src/orchestrator.py:657-664` only protects `ws_subscribe_summary`; the per-market loop at `src/orchestrator.py:677-682` re-emits unconditionally. This is the unfinished half of 2026-05-17 fix-plan F6.

**What.** Track the previously-activated condition_id set; only emit `orchestrator.market_activated` INFO when the set diff is non-empty. Keep a DEBUG heartbeat for log-tailing operators.

**Files.** `src/orchestrator.py` (~10 LoC additions in `_activate_markets`).

**Code sketch:**
```python
# src/orchestrator.py near line 666
new_activated = {market.condition_id for market in deduped}
added = new_activated - self._last_activated_condition_ids
removed = self._last_activated_condition_ids - new_activated

if not added and not removed:
    logger.debug(
        "market_activation_unchanged",
        active_count=len(new_activated),
    )
else:
    for market in deduped:
        if market.condition_id not in added:
            continue
        resolved_category = resolved_categories_by_condition.get(market.condition_id)
        if resolved_category is None:
            resolved_category = resolve_market_category(...)
        logger.info(
            "orchestrator.market_activated",
            condition_id=market.condition_id,
            category=resolved_category.value,
            token_count=len(market.token_ids),
        )
    for removed_cid in removed:
        logger.info("orchestrator.market_deactivated", condition_id=removed_cid)

self._last_activated_condition_ids = new_activated
```

And in `Orchestrator.__init__`:
```python
self._last_activated_condition_ids: frozenset[str] = frozenset()
```

**Tests.**
1. `test_no_diff_emits_debug_only`: call `_activate_markets` twice with same list; assert second call emits no INFO `orchestrator.market_activated` lines (use caplog).
2. `test_diff_emits_info_for_added_only`: second call with one new market; assert exactly one INFO line for that market.
3. `test_diff_emits_info_for_removed`: second call missing one market; assert `orchestrator.market_deactivated` fires for the removed condition.

**Risk.**
- Operators relying on the periodic re-emit as a "still alive" signal lose that signal. Mitigation: DEBUG `market_activation_unchanged` line preserves it for `LOG_LEVEL=DEBUG`.
- Aggregator/WS subscription is re-applied every cycle regardless; this fix only changes the log, not the call. No behavioral effect.

**Validation in next run.**
- 1-hour run produces ≤ 15 `orchestrator.market_activated` INFO lines total (one per actual activation), not ~5,600.
- DEBUG `market_activation_unchanged` lines appear every ~10s if `LOG_LEVEL=DEBUG`.

---

### F4 — Throttle WS snapshot persistence by bps + time

**Severity:** MEDIUM (DB growth blocks multi-day runs)
**MAAP:** YES — touches `src/agents/ingestion/ws_client.py`, `src/core/config.py`.
**Blast radius:** All WS-driven persistence. Aggregator behavior unchanged (aggregator consumes in-memory, not from DB).

**Why.** Per Finding 4.3: `market_snapshots` grew at ~1,465 rows/min in this run. Projection ~140 MB/hr DB growth, ~25 GB/week. Direct restatement of 2026-05-17 Finding 4.5 / fix-plan F5 — the fix was approved but never landed.

**What.** Persist a row only when (a) midpoint changes by ≥ `snapshot_persist_min_bps` bps for the condition, OR (b) ≥ `snapshot_persist_max_interval_sec` seconds have elapsed since the last persist for that condition. Defaults: 25 bps and 2.0 s.

**Files.**
- `src/core/config.py` (+2 fields, +2 default constants)
- `src/agents/ingestion/ws_client.py` (~25 LoC: per-condition state dict + `_should_persist` helper + call site change)
- `tests/unit/test_ingestion.py` (+5 cases)

**Code sketch:**
```python
# src/core/config.py
snapshot_persist_min_bps: int = Field(default=25, ge=0,
    description="Persist a snapshot only when midpoint changes by >= this many bps for the condition.")
snapshot_persist_max_interval_sec: Decimal = Field(default=Decimal("2.0"), ge=Decimal("0.5"),
    description="Always persist a snapshot if this many seconds have passed since the last persist for the condition.")

# src/agents/ingestion/ws_client.py
class CLOBWebSocketClient:
    def __init__(self, ...):
        ...
        self._last_persist: dict[str, tuple[Decimal, Decimal]] = {}  # condition_id -> (monotonic_ts, midpoint)

    def _should_persist(self, condition_id: str, midpoint: Decimal, now: Decimal) -> bool:
        prev = self._last_persist.get(condition_id)
        if prev is None:
            return True
        prev_ts, prev_mid = prev
        if now - prev_ts >= self._cfg.snapshot_persist_max_interval_sec:
            return True
        if prev_mid <= Decimal("0"):
            return True  # cannot compute bps delta; persist to be safe
        bps_delta = abs(midpoint - prev_mid) * Decimal("10000") / prev_mid
        return bps_delta >= Decimal(self._cfg.snapshot_persist_min_bps)

    # In snapshot insertion path:
    now = Decimal(str(time.monotonic()))
    if self._should_persist(condition_id, midpoint, now):
        await repo.insert_snapshot(snapshot)
        self._last_persist[condition_id] = (now, midpoint)
        logger.debug("market_snapshot_inserted", ...)   # demote from INFO
    else:
        logger.debug("market_snapshot_throttled", ...)
```

**Tests.**
1. `test_first_snapshot_always_persisted`: first frame per condition is persisted regardless of delta.
2. `test_subsequent_within_window_throttled`: same midpoint, 0.5s later → not persisted.
3. `test_midpoint_change_above_bps_persisted`: midpoint Δ=50 bps within 1s → persisted.
4. `test_time_window_forces_persist`: midpoint unchanged, 3s later → persisted.
5. `test_zero_prev_midpoint_defaults_to_persist`: edge case; never divide by zero.

**Risk.**
- Dashboard timeline panel may assume 1:1 WS-frame:row. Verify by grepping `src/ui/dashboard.py` and `src/observability/dashboard_activity_feed.py` for `market_snapshots` queries. Behavior under throttle is "the timeline is sparser but still monotonic and chronological."
- Aggregator behavior is unchanged because aggregator consumes from the WS callback queue, not from `market_snapshots`.
- The new `snapshot_persist_max_interval_sec` is `Decimal`, not `float`, per Decimal-integrity rule. If existing config-loading uses `float` for `Decimal` fields, this could trigger a validation error — verify in F0.

**Validation in next run.**
- `market_snapshots` row growth drops by ≥10× (target ≤150 rows/min, was ~1,465 / min).
- DB size after 1 hour < 30 MB delta (was ~140 MB delta).
- No regression in `Evaluation complete` cadence (aggregator does not depend on DB).
- Dashboard timeline panel still renders with the lower density.

---

### F5 — Demote `ws_client.skip_no_token_non_positive_yes_quote` to DEBUG + per-condition burst log

**Severity:** LOW (log noise, new finding)
**MAAP:** YES — touches `src/agents/ingestion/ws_client.py`.
**Blast radius:** Logging only.

**Why.** Per Finding 4.4: 3,251 emits in 15 min, all from one ELECTIONS market with a crossed/degenerate quote (`best_bid=0 best_ask=0.001`). The skip is correct behavior; emitting 217 lines/min is not.

**What.** Demote the per-event log to DEBUG. Track per-condition first-detection; emit one INFO `ws_client.degenerate_quote_first_detected` per condition when the symptom first appears. Optionally emit one INFO `ws_client.degenerate_quote_cleared` when the condition's quotes return to a valid state.

**Files.** `src/agents/ingestion/ws_client.py` (~15 LoC).

**Code sketch:**
```python
self._degenerate_quote_conditions: set[str] = set()

def _handle_skip_degenerate(self, condition_id: str, best_bid: Decimal, best_ask: Decimal) -> None:
    if condition_id not in self._degenerate_quote_conditions:
        logger.info(
            "ws_client.degenerate_quote_first_detected",
            condition_id=condition_id,
            best_bid=str(best_bid),
            best_ask=str(best_ask),
        )
        self._degenerate_quote_conditions.add(condition_id)
    logger.debug(
        "ws_client.skip_no_token_non_positive_yes_quote",
        ...,
    )
```

**Tests.**
1. `test_degenerate_first_detection_emits_info_once`: 5 calls for same condition → exactly one INFO line.
2. `test_degenerate_distinct_conditions_each_get_info_once`: 2 conditions, 3 calls each → exactly 2 INFO lines.
3. `test_normal_quote_unaffected`: valid quote does not interact with this path.

**Risk.** Very low. Loss of per-event detail at INFO; events still in DEBUG.
**Validation.** 1-hour run with the same degenerate market produces 1 INFO `degenerate_quote_first_detected`, not ~13,000 skip lines.

---

### F6 — (carry-over) Demote `ws_client.skip_last_trade_no_book` + add `book_warmup_complete` per condition

**Severity:** LOW (log noise, identical to 2026-05-17 Finding 4.8)
**MAAP:** YES — touches `src/agents/ingestion/ws_client.py`.
**Blast radius:** Logging only.

**Why.** Per Finding 4.5: 854 emits in 15 min. Identical pattern, identical fix as 2026-05-17 fix-plan F8 (first half). Has been carried over twice without being landed.

**What.** Same shape as F5 above, applied to `last_trade_no_book` skip path: demote per-event to DEBUG, emit one INFO `ws_client.book_warmup_complete` per condition once the first book arrives with the count of pre-book trades suppressed.

**Files.** `src/agents/ingestion/ws_client.py` (~15 LoC).

**Code sketch.** Mirror of F5 with `_pre_book_trades_by_condition: dict[str, int]` and the INFO event fired when the first book snapshot for the condition is received.

**Tests.**
1. `test_pre_book_trades_counted_per_condition`.
2. `test_book_warmup_complete_emits_once_per_condition`.
3. `test_warmup_count_resets_on_disconnect`.

**Risk.** Low.
**Validation.** 1-hour run produces N INFO lines (one per condition) instead of ~3,400.

---

### F7 — (deferred — not in this plan) Cooldown threshold tuning

**Severity:** Not a fix; a calibration enhancement.
**MAAP:** Would be required when implemented.
**Blast radius:** All evaluation paths.

**Why deferred.** This plan deliberately *surfaces* the cooldown:eval ratio (F2) before *tuning* it. Tuning without the metric in place is shooting in the dark. After F2 lands and we observe the metric across multiple dry-runs, the operator can decide whether to:
- Loosen the cooldown trigger threshold,
- Add a per-category override (loosen for IRAN where reflection is reactive),
- Or accept the ratio as healthy (the bot correctly refusing to burn budget on a market that has shown no signal).

Capture this as a future WI candidate in STATE.md.

---

### F8 — (deferred — not in this plan) Shadow Gatekeeper

**Severity:** Not a fix; a measurement enhancement.
**MAAP:** Would be required when implemented.
**Blast radius:** New table, new code path; never touches real execution.

**Why deferred.** This is the same F10 from 2026-05-17 fix-plan. Three runs have now HOLDed BTC $150k @ EV=+0.36 — the shadow path is overdue but it is a multi-day WI, not a hygiene fix. Promote out of "deferred" in a Phase-17 planning conversation, not in this plan.

---

## 5. Execution Sequence (sequenced commits)

Each fix below is one atomic commit on `feat/runtime-stabilization-2026-05-23`. Order matters where indicated.

| Order | Fix | Commit message | Depends on | MAAP needed |
|---|---|---|---|---|
| 1 | F0 | (no commit) | — | — |
| 2 | F1 | `chore(config): reconcile PREFLIGHT_MAX_SPREAD_PCT between .env and STATE.md` | F0 | No |
| 3 | F2 | `feat(wi-61): surface cognitive_cooldown.block_rate in runtime audit summary` | F0 | **Yes** |
| 4 | F4 | `feat(ws-client): throttle market snapshot persistence by bps/time (WI-XX)` | F0 | **Yes** |
| 5 | F3 | `chore(orchestrator): dedupe market_activated INFO log on diff` | F0 | **Yes** |
| 6 | F5 | `chore(logging): demote degenerate-quote skip to DEBUG + per-condition burst marker` | F0 | **Yes** |
| 7 | F6 | `chore(logging): demote skip_last_trade_no_book + add book_warmup_complete marker` | F0 | **Yes** |

Total: 5 MAAP-gated commits, 1 config-only commit. F0 is pre-flight, no commit.

After all 5 MAAP commits land on `develop`, open PR `develop → main` for the runtime-stabilization release.

---

## 6. Test Strategy (cumulative)

For each MAAP-gated commit:

1. Author runs targeted tests for the file(s) touched.
2. Author runs full suite: `.venv/bin/python -m pytest --asyncio-mode=auto tests/`
3. Author runs coverage check: `.venv/bin/python -m coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/python -m coverage report -m`
4. Coverage must remain ≥ 80% (per CLAUDE.md).
5. `ruff check .` + `ruff format --check .` pass.
6. Author posts `git diff` for Checker MAAP review per `.opencode/commands/maap.md` / CLAUDE.md MAAP protocol.

Cumulative regression run after all commits land: full suite + 30-min orchestrator dry-run validation (see Section 7).

---

## 7. Post-Implementation Validation (the next dry-run)

After all 5 commits land, run `.venv/bin/python -m src.orchestrator` for 60 minutes and assert:

| Metric | Target | Was (2026-05-23) |
|---|---|---|
| `cognitive_cooldown_block_rate` in runtime audit JSON | present, numeric | absent |
| `orchestrator.market_activated` INFO lines / 15 min | ≤ 15 | 1,410 |
| `ws_client.skip_no_token_non_positive_yes_quote` INFO lines / 15 min | ≤ 5 (one per affected condition) | 3,251 |
| `ws_client.skip_last_trade_no_book` INFO lines / 15 min | ≤ 15 | 854 |
| `market_snapshots` rows / hour | < 9,000 | ~88,000 |
| DB file size growth / hour | < 30 MB | ~140 MB |
| Log file size after 1 hour | < 25 MB | ~40 MB observed in 15 min (projected ~160 MB/hr) |
| Evaluations / hour | ≥ 200 (matches current cadence; not blocked by fix) | ~300 implied |
| Approved decisions | not affected (orthogonal) | 0 |
| Errors / Tracebacks | 0 | 0 (preserve) |
| Process RSS growth rate | < 1 MB/min (verify F2's surfacing implies F-future for leak) | +2.9 MB/min |

If any target is missed, that's a finding for the next iteration; document and decide whether to revert / patch / defer.

---

## 8. Rollback Strategy

Each commit is small and isolated. Rollback options, fastest first:

- **Single fix regressed:** `git revert <sha>` on `develop`. Open targeted PR.
- **Multiple fixes interacting badly:** `git revert -m 1 <merge-sha>` of the `develop → main` PR.
- **Critical failure in production:** `git revert` is the only sanctioned path. `git reset --hard` is NOT permitted on `develop` per CLAUDE.md.

Each `.env` change rolls back by removing the added lines and restarting; no migration impact.

The DB has no schema changes in this plan — `market_snapshots`, `agent_decision_logs`, `operational_events` are all unchanged. No Alembic migration. Rollback is purely a code/config revert.

---

## 9. Open Questions to Resolve Before Execution

The executor should answer these (or get user sign-off) before starting F1:

1. **PREFLIGHT_MAX_SPREAD_PCT — which value is right?** F1 cannot be applied without operator decision: revert `.env` to 0.90 (matches docs, risks re-introducing the symptom the 0.99 escape was for) OR update STATE.md to record 0.99 as the new canonical value.
2. **Should F2's metric also feed Telegram alerts?** F2 surfaces `cognitive_cooldown_block_rate` in the audit JSON. Should WI-26 push a Telegram alert if the rate exceeds e.g. 0.50 sustained over 30 min? Decision: defer to F2's MAAP review; the metric is orthogonal to the alerting policy.
3. **F4 default thresholds — 25 bps / 2.0 s — are they right for this market mix?** The 2026-05-17 fix-plan chose them by intuition. With 15 markets activated and observed midpoints in the 0.035–0.964 range, 25 bps Δ is well above noise on the 0.5-midpoint markets but might be aggressive on the 0.035-midpoint markets. Verify by sampling a real `market_snapshots` slice and computing the percentile of frame-to-frame midpoint Δ — pick a threshold that drops 90% of writes without dropping any genuine price-discovery move. Out of scope for this plan; F4 can ship with the intuitive defaults and re-tune later.
4. **Branch name and Phase numbering.** Acceptable to use `feat/runtime-stabilization-2026-05-23` as a standalone hardening branch outside any phase? Or should we open a Phase 17 first and bind to it?
5. **Does the WI-61 audit need to gain `RSS_GROWTH_RATE_HIGH` as a typed failure reason now that we have evidence the RSS leak exists?** Out of scope for this plan but worth flagging — the audit framework is the natural home, and adding it would make Finding 4.6 self-detecting.

---

## 10. Timeline Estimate

Assuming one engineer working sequentially, no surprises, MAAP turnaround ≤ 30 min per commit:

| Phase | Time | Notes |
|---|---|---|
| F0 pre-flight | 15 min | Branch + archive logs + DB snapshot |
| F1 config drift reconciliation | 10 min | Plus user decision on which value is correct |
| F2 cooldown metric | 90 min | Schema field + audit aggregation + 5 tests; touches WI-61 |
| F4 snapshot throttle | 75 min | Largest functional change; 5 tests; config additions |
| F3 dedupe activation log | 30 min | 3 tests; small change |
| F5 degenerate-quote demote | 30 min | 3 tests; small change |
| F6 last_trade_no_book demote | 30 min | 3 tests; small change |
| Cumulative regression run | 30 min | Full suite + coverage |
| 60-min orchestrator validation | 75 min | Per Section 7 |
| **Total** | **~6.5 hours** | Assuming everything passes first try |

Realistic add-on for MAAP iterations + one debug cycle: **~9 hours total.** Single session feasible.

---

## 11. What Could Go Wrong

- **F2 adds a Decimal field to the WI-61 schema; legacy consumers fail-closed on unknown fields.** Mitigation: Pydantic v2 default is `extra="ignore"` — verify in `runtime_audit.py` model config. If `extra="forbid"`, downstream code that parses old `RuntimeAuditReport` JSON files will break; gate behind a schema-version bump.
- **F4 reduces `market_snapshots` density and breaks dashboard timeline rendering.** Mitigation: check `src/ui/dashboard.py` for assumptions about row density before merging; render gracefully under throttle.
- **F4 changes the meaning of "snapshot persisted" — analytics queries assuming 1:1 with WS frames must change.** Mitigation: `grep -rn "market_snapshots" src/ scripts/ docs/` and audit each hit before merge.
- **F3 changes the operator's "still alive" heartbeat.** Mitigation: DEBUG `market_activation_unchanged` line preserves it for `LOG_LEVEL=DEBUG`; document in `docs/runbooks/operating-the-orchestrator.md` (or equivalent) that the periodic activation INFO is gone by design.
- **F5/F6 demote events some external log-aggregation system was filtering on.** Mitigation: search `docs/`, `deploy/`, and any CI for log-pattern matchers before merge.
- **F1 reverts spread tolerance to 0.90 and the bot suddenly cannot find any market that clears preflight.** Mitigation: this is the symptom the original loosening was meant to escape; if F1 is "Option A" (revert), the operator must accept this risk or confirm the symptom is gone now (likely true since PREFLIGHT_MAX_SPREAD bypass for material moves landed in the 2026-05-18 hotfix per STATE.md line 45).

---

## 12. Definition of Done for This Plan

This plan is "done" (i.e., ready to execute) when:

- [ ] User has answered Section 9 Q1 (PREFLIGHT_MAX_SPREAD_PCT direction).
- [ ] User confirms branch name (Q4).
- [ ] No new findings have arrived from the still-running orchestrator (PID 23542) that would change priorities.
- [ ] (Optional) A second agent / Checker reviews this plan, no Tier 1 finding contradicted.

Once executed (i.e., all 5 MAAP commits merged + 60-min validation passes Section 7 targets), this plan is "delivered" and should be archived into `04_Archive/poly-oracle-agent/runtime_observations/` alongside the observation report.

---

## 13. Files Touched by This Plan (none yet — planning only)

| File | Fix | Change kind | LoC est. |
|---|---|---|---|
| `.env` OR `STATE.md` | F1 | Reconcile drift | ±1 |
| `src/schemas/runtime_audit.py` | F2 | Add field + validator | +12 |
| `src/observability/runtime_audit.py` | F2 | Aggregate cooldowns into summary | +25 |
| `src/core/config.py` | F4 | Add 2 fields | +8 |
| `src/agents/ingestion/ws_client.py` | F4, F5, F6 | Throttle + demote + bursts | +50 / −10 |
| `src/orchestrator.py` | F3 | Dedup activation log | +12 / −5 |
| Tests (multiple) | F2–F6 | New + updates | ~120 / −0 |
| `STATE.md` | F0/F2 | Note new branch + WI-61 summary extension | +8 |
| **Total** | | | **~240 / −15** |

This is a **small-to-medium PR**. Splitting into per-fix PRs is recommended for review hygiene; the table above already maps each fix to its commit boundary.

---

## 14. Closing

This plan is conservative and surgical. It does not change any business logic, prompt, model strategy, Gatekeeper threshold, cooldown threshold, or DRY_RUN posture. It addresses 4 actionable findings observed in this session's logs (1 new MEDIUM, 2 carry-over MEDIUM/LOW, 1 new LOW + 1 carry-over LOW), in dependency order, with explicit tests and rollback for each.

The single most impactful fix is **F4 (WS snapshot persistence throttle)** — it is the only blocker before this can run unattended for >24h on the Droplet. It also has the largest 2026-05-17 fix-plan provenance and is the most-discussed-least-landed change in the entire arc.

The second most impactful is **F2 (cognitive cooldown block rate metric)** — it converts the new headline finding (65% block rate) from an inferred observation into an alertable, monitorable metric, which is the precondition for any later calibration work on cooldown threshold or activated-set rotation.

The remaining three fixes (F3, F5, F6) are log-hygiene that collectively drops log volume ~90% without behavioral change.

**Next step:** answer Section 9 Q1 (PREFLIGHT_MAX_SPREAD_PCT) and Q4 (branch name), then sequence into execution.
