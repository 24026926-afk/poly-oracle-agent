# Orchestrator Fix Plan — 2026-05-17 (Run 2)

**Author:** Claude Code (planning, no modifications)
**Companion document:** `2026-05-17-orchestrator-dry-run-session-run2.md` (Run 2 observation report)
**Supersedes:** the unactioned portions of `2026-05-17-orchestrator-fix-plan.md` (Run 1). F2 (budget split), F3 (Grok narrative recovery), F4 (Grok skip on budget) are confirmed implemented by `dab61ce` and are not re-planned here. F1 (observability flags) is partially implemented — Telegram sink still needs verification.
**Status:** PLAN ONLY — no code or config changes have been applied as part of this document.
**Target branch (not yet created):** `feat/runtime-stabilization-post-2026-05-17-run2`
**Scope discipline:** every change in this plan is intended to fix a *root cause observed in this Run 2 session's logs.* No speculative refactors, no feature additions.

---

## 0. Why a separate planning document

The Run 2 observation report catalogued 8 findings (2 HIGH, 3 MEDIUM, 3 LOW) and 10 recommendations. This document converts the actionable subset into a sequenced execution plan with file-level scope, code sketches, test approach, MAAP requirements, risk ranking, and validation criteria — without applying any of it. It exists so a human reviewer (and a future Checker agent under MAAP) can approve the *plan* before any source-tree state changes.

The orchestrator (PID 35832) is **left running** during this planning step. It is in graveyard-state steady-state and will keep running until the executor explicitly stops it in F0.

---

## 1. Newly Observed Signal Since Report Was Written

None at planning time. The orchestrator continues to emit `per_market_hourly_limit_exhausted` log lines at the same ~68/min rate, and `MARKET_REJECTED` ledger writes at the same ~215/min rate. No new error class, no WS event, no traceback has surfaced between the T+60 snapshot and the writing of this plan.

If the orchestrator is allowed to keep running past the canonical 1-hour budget window, we *may* observe the per-market sliding window beginning to release evaluations in batches as old timestamps prune. That data would refine R1's recommended cap value but does not change the structure of the plan.

---

## 2. Goals of This Plan

In priority order:

1. **Unfreeze the per-market evaluation pipeline.** Today the bot freezes at ~50 evals within 7 min, and the rolling-hour reset does not release in batch. Target: ≥120 evals in a 30-min observation window without graveyard state.
2. **Reduce log + ledger noise produced by the per-market gate and market-rejection refresh.** Today a 1-hour run produces 128 MB of log dominated by budget-block spam and 12.6k duplicate-rejection events. Target: ≤30 MB log and ≤2k operational events in a 1-hour run.
3. **Make the Telegram alert path actually deliver.** Today: `telegram.dispatched=0` despite the operational-alerts bridge being enabled. Target: `process_started` Telegram notification within 5 s of next restart.
4. **Make read-side DB access safe under sustained write load.** Today: two `database is locked (5)` errors caught during ad-hoc reads while orchestrator was writing. Target: zero lock errors during normal dashboard / digest / replay use.
5. **Keep every change MAAP-clean** with explicit test coverage and a documented rollback for each fix.

What is **explicitly out of scope** for this plan:

- No `DRY_RUN=false` changes.
- No live signing / broadcasting.
- No Gatekeeper threshold changes (`min_confidence=0.75` stays).
- No PostgreSQL migration.
- No new WIs. Every change here is a fix or hardening of existing WIs.
- No prompt-strategy or reflection-strategy redesign.
- No new LLM providers.
- No re-planning of fixes already implemented in `dab61ce` (Run 1 F2/F3/F4).

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
- `structlog` only — no `print()` in any code introduced by this plan.
- Run end appends a session summary to `03_Daily/2026-05-17.md`.

Plan execution order respects these by:

- Grouping config-only changes (no MAAP needed) before code changes (MAAP needed).
- Splitting code changes into one atomic commit per fix.
- Defining test additions per fix before the code change is applied.

---

## 4. Fix Inventory — Ordered by Execution Sequence

Each fix below has the same structure:

> **Title, severity, MAAP req, blast radius**
> **Why** — root cause observed
> **What** — change to make
> **Files** — exact paths
> **Code sketch** — pseudocode, not final
> **Tests** — what to add / modify
> **Risk** — what could go wrong
> **Validation** — what to look for in the next run

---

### F0 — Pre-flight (housekeeping, no code changes)

**Severity:** N/A (operational)
**MAAP:** No
**Blast radius:** Local working directory only.

**Why.** Need a clean baseline for the next run, and need to preserve evidence from this run before logs roll over.

**What.**
1. Send SIGTERM to current orchestrator (PID 35832). Wait for clean exit (`until ! kill -0 35832 2>/dev/null; do sleep 1; done`).
2. Send SIGTERM to current Streamlit dashboard (PID 36118) if it should not stay running across the fix work.
3. Archive current logs:
   - `logs/orchestrator-run.log` → `logs/orchestrator-run-2026-05-17-post-stabilization-run2.log`
   - `logs/dashboard.log` → `logs/dashboard-2026-05-17-run2.log`
4. Snapshot DB sizes / row counts to `docs/runtime_observations/2026-05-17-post-stabilization-snapshot.txt` for before/after diff (the four `logs/stats-snapshot-T*min.txt` files captured during Run 2 already contain this in narrative form — copy them in).
5. Create branch `feat/runtime-stabilization-post-2026-05-17-run2` off `develop` HEAD (currently `dab61ce`).
6. Verify `git status` shows only the unrelated `.qwen/commands/SKILL.md` untracked file; no other working-tree drift.

**Tests.** None.

**Risk.** Low. Reversible by `git branch -D feat/runtime-stabilization-post-2026-05-17-run2`.

**Validation.** `git status` clean on the new branch except for the pre-existing `.qwen/commands/SKILL.md`.

---

### F1 — Raise `llm_market_hourly_call_limit` (config-only)

**Severity:** HIGH (Finding 4.1 — graveyard state within 7 min)
**MAAP:** No (config default in `src/core/config.py`, but no code logic change — debatable; treat as MAAP-gated to be safe because `src/core/config.py` *is* under `src/`)
**Blast radius:** All callers of `LLMBudgetGuard.check` that consult the per-market counter.

**Why.** Per Finding 4.1: with the default `llm_market_hourly_call_limit=10` (`src/core/config.py:437`), every high-volatility market trips its per-market cap within 5-7 min of startup. The bot then ingests ~1,400 snapshots/min and produces zero evaluations until the rolling-hour window releases slots one-by-one.

The original cap value (10) was almost certainly chosen for an architecture in which reflection ran on its own counter (which it now does, per `dab61ce`). With the new split, raising the per-market cap no longer risks blowing past the global hourly cap — the global cap remains the safety ceiling.

**What.** Raise default from 10 to 30 in `src/core/config.py:437`, OR provide override via `.env` (no code change required — Pydantic V2 picks up the env var automatically from the field name).

Recommended: keep code default at 10 (conservative) and add `LLM_MARKET_HOURLY_CALL_LIMIT=30` to `.env`. This is reversible without a deploy and is the smallest possible change.

**Files.**

- `.env` (uncommitted) — add one line.
- Optional follow-up: `src/core/config.py:437` (change default to a more defensible value; MAAP-gated if changed).

**Code sketch.** None (config only).

```
# add to .env
LLM_MARKET_HOURLY_CALL_LIMIT=30
```

**Tests.** Optional regression in `tests/unit/test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py`: assert per-market gate fires at the 31st call when limit=30. The existing test for the gate firing at 11th call (assuming one exists) should be parametrized rather than copied.

**Risk.**

- If raised too high, the per-market cap stops being a meaningful brake on a runaway evaluation loop in a single market. Mitigation: 30 is still 3× lower than the global `llm_hourly_call_limit=60`, so a single market cannot consume the entire global budget on primary alone. Reflection split protects further.
- If the `.env` value is not picked up (Pydantic V2 with case-sensitive env), the change is silently no-op. Mitigation: verify after restart by checking the `CONFIG_LOADED` operational event payload for the resolved value.

**Validation in next run.**

- `llm_budget_blocked … per_market_hourly_limit_exhausted` first occurrence happens at T+>15 min instead of T+7 min.
- `Evaluation complete` count at T+30 ≥ 120 (today: 50).
- The global `llm_budget_blocked … hourly_call_limit_exhausted` (the *primary* global cap) becomes the dominant throttle, not the per-market.

---

### F2 — De-duplicate per-market budget-block log emission

**Severity:** HIGH (Finding 4.2 — 68 spam lines/min, 128 MB log in 60 min)
**MAAP:** YES — touches `src/agents/evaluation/llm_cost_guard.py` and `src/agents/evaluation/claude_client.py`.
**Blast radius:** Logging path of `LLMBudgetGuard._block` and its callers. No effect on safety semantics or returned `BudgetDecision`.

**Why.** Per Finding 4.2: each blocked call emits two log lines (the metric + a narrative). Per-market budget exhaustion is a steady state, so emitting per-occurrence is pure noise. After T+7 the log file grows ~2 MB/min, almost entirely from these two lines, and 3,648 `BUDGET_BLOCK` events land in `operational_events`.

**What.** Add a small per-(market, reason) emission throttle inside the guard. First occurrence in a window: emit at WARNING and persist to ledger. Subsequent occurrences within the same window: increment a counter, do not emit. On window rollover: emit a single summary line + ledger event with the accumulated count.

**Files.**

- `src/agents/evaluation/llm_cost_guard.py` (~40 lines — add throttle state, modify `_block`).
- `src/agents/evaluation/claude_client.py` (~3 lines — remove the redundant narrative log; the guard's structured event already carries the same fields).
- `tests/unit/test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py` (~3 new cases).

**Code sketch (pseudocode, NOT final):**

```python
# llm_cost_guard.py
from collections import defaultdict
from decimal import Decimal

_EMIT_WINDOW_SECONDS = 60  # one structured emission per (market, reason) per minute

class LLMBudgetGuard:
    def __init__(self, config: AppConfig, ...) -> None:
        ...
        self._last_emit: dict[tuple[str, LLMBudgetBlockReason], float] = {}
        self._suppressed_counts: dict[tuple[str, LLMBudgetBlockReason], int] = defaultdict(int)

    def _block(
        self,
        reason: LLMBudgetBlockReason,
        call_type: LLMCallType,
        *,
        emit: bool,
        market_key: str | None,
    ) -> LLMBudgetDecision:
        decision = LLMBudgetDecision(allowed=False, reason=reason, call_type=call_type)
        if not emit:
            return decision
        key = (market_key or "_global_", reason)
        now = time.monotonic()
        last = self._last_emit.get(key, 0.0)
        if now - last >= _EMIT_WINDOW_SECONDS:
            suppressed = self._suppressed_counts.pop(key, 0)
            log.warning(
                "llm_budget_blocked",
                call_type=call_type.value,
                reason=reason.value,
                market_key=market_key,
                suppressed_since_last_emit=suppressed,
            )
            # Persist a single ledger event with the suppressed count
            self._publish_event(reason, call_type, market_key, suppressed)
            self._last_emit[key] = now
        else:
            self._suppressed_counts[key] += 1
        return decision
```

The caller in `claude_client.py` (search for `llm_budget_blocked — skipping primary provider call.`) should be removed: the structured `llm_budget_blocked` line from the guard already carries the snapshot context.

**Tests.**

1. `test_budget_block_throttle_emits_first_then_suppresses`: trigger 100 blocks for the same (market, reason) within 5 s; assert the structured `log.warning` was called exactly once.
2. `test_budget_block_throttle_emits_summary_on_window_rollover`: trigger 50 blocks in window 1, advance clock by 61 s, trigger 1 block in window 2; assert window 2's emission carries `suppressed_since_last_emit=50`.
3. `test_budget_block_throttle_per_market_independent`: blocks for market A do not suppress blocks for market B within the same window.
4. Existing regressions for the guard's `BudgetDecision` return values must still pass — this fix changes only side effects, not return semantics.

**Risk.**

- If the throttle is buggy and over-suppresses, an operator could miss a critical block class (e.g. global `hourly_call_limit_exhausted` if it ever fires). Mitigation: throttle is keyed per-(market, reason), so distinct reasons are emitted independently. The global cap reason is different from per-market reason and would not be suppressed by per-market activity.
- The `_last_emit` / `_suppressed_counts` dicts grow unboundedly across market churn. Mitigation: prune entries older than `_EMIT_WINDOW_SECONDS * 2` on each `_block` call.

**Validation in next run.**

- Log file size at T+60 ≤ 30 MB (today: 128 MB).
- Per-market budget-block log lines ≤ ~30 in a 30-min steady-state idle window (today: ~2000).
- `BUDGET_BLOCK` rows in `operational_events` ≤ ~50 in a 60-min run (today: 3,648).

---

### F3 — Diagnose Telegram dispatch (investigation, no code)

**Severity:** MEDIUM (Finding 4.4)
**MAAP:** No (no code changes in this fix; diagnostic only)
**Blast radius:** Configuration only, conditional on diagnostic outcome.

**Why.** Per Finding 4.4: `telegram.dispatched=0`, `operational_alerts.dispatched=1` (the startup `process_started` only). Several possible causes — we should not patch code until we know which.

**What.**

1. Stop orchestrator (F0).
2. Add `ENABLE_TELEGRAM_NOTIFIER=true` and `ENABLE_STARTUP_ALERT=true` to `.env` (these were called out by Run 1's F1 but only `ENABLE_OPERATIONAL_ALERTS` and `ENABLE_OPERATIONAL_EVENT_LEDGER` appear to have been applied).
3. Restart orchestrator.
4. Observe: within 10 s of startup, `telegram.dispatched` should appear in the log, and the actual Telegram chat should receive the `process_started` message.
5. If `telegram.dispatched` does not fire: read `src/observability/operational_alert_bridge.py` (or wherever the bridge dispatches to the notifier) and verify the severity-routing table includes `INFO`-level alerts. If only `WARNING+` is routed, that explains the gap.
6. If it does fire: no code change. Persist the new env flags as the canonical config.

**Files.**

- `.env` (uncommitted) — add two flags.
- Possibly `src/observability/operational_alert_bridge.py` if severity-routing is the cause (would become a separate MAAP-gated fix; not planned here until the diagnostic confirms).

**Code sketch.** None.

```
# add to .env
ENABLE_TELEGRAM_NOTIFIER=true
ENABLE_STARTUP_ALERT=true
```

**Tests.** None for the env-only path. If F3 escalates to a code fix, the escalated plan must include tests.

**Risk.**

- Telegram start-up alert will fire to chat `8840799632` within seconds of restart. Confirm with the user that this is acceptable before applying (per Run 1 F1 risk note).
- If the bot is restarted multiple times during fix work, the user will receive multiple Telegram pings. Acceptable trade-off but flag in advance.

**Validation in next run.**

- Startup logs show `telegram_notifier.enabled` (or equivalent) instead of any disabled marker.
- Telegram chat `8840799632` receives `process_started` notification within 5 s of restart.
- `telegram.dispatched` count > 0 after first minute of runtime.

---

### F4 — Emit `MARKET_REJECTED` only on state transitions

**Severity:** MEDIUM (Finding 4.3 — 12,689 duplicate-rejection events in 60 min)
**MAAP:** YES — touches `src/agents/ingestion/market_discovery.py` (or wherever the discovery loop emits `MARKET_REJECTED`) and likely `src/agents/ingestion/market_quarantine.py`.
**Blast radius:** Operational event ledger emission for the discovery path. No effect on which markets are activated or evaluated.

**Why.** Per Finding 4.3: every market-discovery refresh cycle re-emits a `MARKET_REJECTED` event for every market still failing preflight. With 41 stable TTR-fail markets and a ~5 s refresh cadence, the ledger receives ~215 rejections/min — 70% of all operational events written in a session.

The semantic intent of WI-56 (per the runbook) is *state transitions*. A market that was rejected last cycle and is still rejected this cycle has no new state to record.

**What.** Maintain a small in-memory `_last_rejection_state: dict[market_key, MarketRejectionReason]` on `MarketDiscoveryService` (or the equivalent). On each discovery cycle:

- If a market is in `_last_rejection_state` with the same reason: do nothing.
- If a market was previously accepted (or unknown) and is now rejected: emit `MARKET_REJECTED` with reason.
- If a market was previously rejected with reason A and is now rejected with reason B: emit `MARKET_REJECTED_REASON_CHANGED` (new event type, or include `previous_reason` in payload).
- If a market was previously rejected and is now accepted: emit `MARKET_ACCEPTED` (or `MARKET_RECOVERED`).

In addition, emit one `MARKET_ELIGIBILITY_CYCLE_COMPLETED` event per cycle with cumulative counts: `{total: 100, eligible: 59, ttr_fail: 41, ...}`. This is queryable and proves the discovery loop is running without per-market spam.

**Files.**

- `src/agents/ingestion/market_discovery.py` (~40 lines — add state map, modify emission)
- `src/schemas/ops.py` or `src/schemas/operational_events.py` (~10 lines — add `MARKET_ELIGIBILITY_CYCLE_COMPLETED` event type and `MARKET_REJECTED_REASON_CHANGED` if accepted)
- `tests/unit/test_WI-56-operational-event-ledger.py` (~6 new cases)

**Code sketch (pseudocode, NOT final):**

```python
# market_discovery.py
class MarketDiscoveryService:
    def __init__(self, ...) -> None:
        ...
        self._last_rejection_state: dict[str, MarketRejectionReason] = {}
        self._last_accepted: set[str] = set()

    async def run_cycle(self) -> None:
        result = await self._fetch_and_classify()
        now_rejected: dict[str, MarketRejectionReason] = {
            m.market_key: r.reason for m, r in result.rejections.items()
        }
        now_accepted: set[str] = {m.market_key for m in result.accepted}

        for key, reason in now_rejected.items():
            prev = self._last_rejection_state.get(key)
            if prev is None and key not in self._last_accepted:
                # first time we have seen this market at all
                await self._event_bus.publish(MarketRejectedEvent(market_key=key, reason=reason))
            elif prev is None and key in self._last_accepted:
                # transition: accepted -> rejected
                await self._event_bus.publish(MarketRejectedEvent(market_key=key, reason=reason))
            elif prev is not None and prev != reason:
                # transition: rejected for reason A -> rejected for reason B
                await self._event_bus.publish(
                    MarketRejectedEvent(market_key=key, reason=reason, previous_reason=prev)
                )
            # else: still rejected for same reason — suppress

        for key in now_accepted - self._last_accepted:
            if key in self._last_rejection_state:
                await self._event_bus.publish(MarketAcceptedEvent(market_key=key))

        await self._event_bus.publish(MarketEligibilityCycleCompletedEvent(
            total=result.total,
            eligible=len(now_accepted),
            ttr_fail=result.ttr_fail,
            ...
        ))

        self._last_rejection_state = now_rejected
        self._last_accepted = now_accepted
```

**Tests.**

1. `test_stable_rejection_emits_once`: same market with same reason across 10 cycles → 1 `MARKET_REJECTED` event, 10 `MARKET_ELIGIBILITY_CYCLE_COMPLETED` events.
2. `test_reason_change_emits_new_event`: market rejected for `TTR_FAIL` then for `LIQUIDITY_FAIL` → 2 `MARKET_REJECTED` events (or 1 + 1 `_REASON_CHANGED`).
3. `test_accepted_after_rejection_emits_recovery`: market rejected then accepted → 1 `MARKET_REJECTED` + 1 `MARKET_ACCEPTED`.
4. `test_first_rejection_emits`: unknown market rejected on first cycle → 1 event.
5. `test_cycle_completed_carries_counts`: payload includes total / eligible / ttr_fail.
6. Regression: existing WI-56 tests must still pass.

**Risk.**

- A state-tracking dict introduces a per-process memory footprint proportional to total known markets. Mitigation: at 100 markets × ~64 bytes/entry the footprint is < 10 KB. Negligible.
- If a market is removed from Gamma between cycles, its entry in `_last_rejection_state` will stay forever. Mitigation: prune entries whose market_key was not in the most-recent classification result. Add to the same cycle.
- WI-58 (incident replay) and WI-59 (dashboard activity feed) consume these events. If they filter or display them in ways that assume per-cycle emission, this change could break their displays. Mitigation: read both modules during implementation and verify the filter logic; add a test that opens an incident replay across the boundary of this change.

**Validation in next run.**

- `operational_events` rows of type `MARKET_REJECTED` ≤ 50 in a 60-min run (today: 12,689).
- New `MARKET_ELIGIBILITY_CYCLE_COMPLETED` rows present, count matches discovery cycle count.
- Dashboard activity feed still shows current rejection state correctly.

---

### F5 — Enable SQLite WAL + `busy_timeout`

**Severity:** MEDIUM (Finding 4.5 — `database is locked (5)` under concurrent read+write)
**MAAP:** YES — touches `src/db/session.py` (or wherever the async engine is created) and possibly `alembic/env.py`.
**Blast radius:** Every DB connection opened by the orchestrator + dashboard + CLI tools. WAL is the standard SQLite production mode but is a behavioural change worth testing.

**Why.** Per Finding 4.5: at ~24 writes/sec sustained (snapshots + events), external reads from the dashboard, daily digest, and incident replay race the writer's lock and intermittently fail. WAL mode allows readers and a single writer to operate concurrently without lock contention.

**What.** On engine creation, register a connection-init hook that sets `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` (5 s). For `aiosqlite`, this is typically done via SQLAlchemy's `@event.listens_for(engine.sync_engine, "connect")` decorator.

**Files.**

- `src/db/session.py` (or wherever `create_async_engine` is called) — ~10 lines.
- `alembic/env.py` — match the same pragma at migration time so the on-disk journal mode is consistent. ~5 lines.
- `tests/integration/test_db_concurrency.py` (new file) — verify concurrent reader does not error during sustained writes.

**Code sketch (pseudocode, NOT final):**

```python
# src/db/session.py
from sqlalchemy import event

def _set_sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA synchronous=NORMAL")  # safe with WAL
    cursor.close()

engine = create_async_engine(settings.database_url, ...)
event.listen(engine.sync_engine, "connect", _set_sqlite_pragmas)
```

**Tests.**

1. `test_wal_mode_active_on_first_connection`: open a connection, `PRAGMA journal_mode;` returns `wal`.
2. `test_busy_timeout_set`: open a connection, `PRAGMA busy_timeout;` returns 5000.
3. `test_concurrent_read_during_sustained_write_does_not_error`: spawn a writer task that inserts 1000 rows into `operational_events` and a reader task that performs `GROUP BY event_type` every 100 ms; assert reader never raises `OperationalError`.

**Risk.**

- WAL creates `<dbfile>-wal` and `<dbfile>-shm` sidecar files. Backup scripts / git-ignore lists must account for these. Mitigation: extend `.gitignore` if needed; document in `docs/runbooks/`.
- A power loss with WAL + `synchronous=NORMAL` can lose the last few seconds of writes. Acceptable for dry-run / observational workloads. Document explicitly.
- Existing tests that open the DB in a different mode (e.g. `:memory:`) are unaffected — the pragma listener still runs but is no-op on memory DBs.

**Validation in next run.**

- Ad-hoc `sqlite3 data/poly_oracle.db "SELECT … GROUP BY event_type;"` while orchestrator is running succeeds without `database is locked (5)`.
- Dashboard refresh during heavy ingestion does not surface SQLAlchemy `OperationalError` in the UI logs.

---

### F6 — Per-market budget quarantine (upstream backpressure)

**Severity:** HIGH root-cause complement to F1 + F2 (Findings 4.1 and 4.2)
**MAAP:** YES — touches `src/agents/ingestion/market_quarantine.py`, `src/agents/context/bounded_queue.py`, `src/orchestrator.py`.
**Blast radius:** Snapshot enqueue path. A market under budget quarantine is held off the queue until its per-market window resets.

**Why.** F1 raises the per-market cap; F2 hides the noise when it does fire. Neither addresses the root architectural mistake: **the bounded queue accepts snapshots from a market that the LLM guard will then reject**. This wastes a queue slot, a consumer wakeup, and (today) two log lines per snapshot.

The correct architecture is: when `LLMBudgetGuard` rejects a snapshot with `per_market_hourly_limit_exhausted`, mark the market as `BUDGET_QUARANTINED` for the remainder of its window. The ingestion layer should not enqueue snapshots for a quarantined market.

**What.** Add a typed `BUDGET_QUARANTINED` state to `MarketQuarantineManager`. When a market enters this state:

- Its window-end timestamp is recorded.
- The bounded queue's `put()` short-circuits for that market until the window-end passes.
- A single `MARKET_BUDGET_QUARANTINED` operational event is persisted on entry; a single `MARKET_BUDGET_QUARANTINE_LIFTED` on exit. No per-snapshot spam.

**Files.**

- `src/agents/ingestion/market_quarantine.py` (~50 lines — new state + entry/exit logic)
- `src/agents/context/bounded_queue.py` (~15 lines — consult quarantine state in `put()`)
- `src/orchestrator.py` (~10 lines — wire `LLMBudgetGuard.check` rejection back into `MarketQuarantineManager.quarantine_for_budget()`)
- `src/schemas/market_eligibility.py` (~5 lines — add `BUDGET_QUARANTINED` to `MarketQuarantineReason` enum)
- `tests/unit/test_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.py` (~5 new cases)

**Code sketch (pseudocode, NOT final):**

```python
# market_quarantine.py
class MarketQuarantineManager:
    def quarantine_for_budget(self, market_key: str, window_end_utc: datetime) -> None:
        if market_key not in self._budget_quarantined:
            self._budget_quarantined[market_key] = window_end_utc
            self._publish(MarketBudgetQuarantinedEvent(market_key=market_key, window_end=window_end_utc))

    def is_quarantined(self, market_key: str) -> bool:
        end = self._budget_quarantined.get(market_key)
        if end is None:
            return False
        if datetime.utcnow() >= end:
            del self._budget_quarantined[market_key]
            self._publish(MarketBudgetQuarantineLiftedEvent(market_key=market_key))
            return False
        return True

# bounded_queue.py — in put()
if self._quarantine.is_quarantined(snapshot.market_key):
    self._metrics.inc_quarantine_skipped(snapshot.market_key)
    return  # drop silently — single ledger event already emitted on entry

# orchestrator.py — when budget guard rejects with per_market_hourly_limit_exhausted
decision = self._budget_guard.check(call_type="primary", market_key=snapshot.market_key)
if not decision.allowed and decision.reason == LLMBudgetBlockReason.PER_MARKET_HOURLY_LIMIT_EXHAUSTED:
    window_end = self._budget_guard.market_window_end(snapshot.market_key)
    self._quarantine.quarantine_for_budget(snapshot.market_key, window_end)
```

**Tests.**

1. `test_quarantine_for_budget_sets_state`.
2. `test_is_quarantined_returns_true_within_window`.
3. `test_is_quarantined_lifts_after_window_end_and_emits_event`.
4. `test_bounded_queue_drops_quarantined_market_snapshots_silently`.
5. `test_orchestrator_quarantines_on_budget_block`.
6. Existing WI-53 quarantine and queue tests must still pass.

**Risk.**

- If the window-end calculation is wrong, a market could stay quarantined too long (lost throughput) or too short (re-quarantine immediately). Mitigation: derive `window_end` from the budget guard's own sliding-window state — the same source the gate uses to decide block/allow.
- Quarantine state is in-memory; an orchestrator restart drops it. Acceptable for dry-run; the budget guard's own state also resets on restart, so they remain consistent.
- F6 changes the **observable behaviour** of the bounded queue (silent drop vs. coalesce-then-reject). Document in `docs/runbooks/market-eligibility-and-backpressure.md`. Dashboard activity feed should still receive `MARKET_BUDGET_QUARANTINED` events, so the operator-visible signal does not disappear, it just consolidates.

**Validation in next run.**

- Per-market budget block events in `operational_events` ≤ ~15 (one per market) in a 60-min run, regardless of snapshot rate.
- `MARKET_BUDGET_QUARANTINED` / `MARKET_BUDGET_QUARANTINE_LIFTED` events present and paired.
- `queue.coalesced` count drops sharply once markets quarantine (no more queue churn from rejected markets).

---

### F7 — Resolve `MarketMetadata.category` at activation time

**Severity:** LOW (Finding 4.7, carried from Run 1 Finding 4.6)
**MAAP:** YES — touches `src/orchestrator.py`.
**Blast radius:** Activation-time log readability only.

**Why.** `orchestrator.market_activated category=None` is emitted for every activation. The category is resolved later in the evaluation pipeline (via the Run 1 hotfix in `claude_client.py`), so the data exists; it is just not connected to the activation logger.

**What.** During activation, resolve `MarketMetadata.category` from the same source the evaluation pipeline uses (the `MarketKeyParser` / category resolver helper). Log the resolved value.

**Files.**

- `src/orchestrator.py` (~5 lines)

**Code sketch.** None — straightforward attribute pass-through.

**Tests.**

- `test_orchestrator_market_activated_logs_resolved_category`: assert log payload includes `category=CRYPTO` (or appropriate value) for an activation whose condition_id maps to a known crypto market.

**Risk.** Negligible.

**Validation in next run.**

- Grep `orchestrator.market_activated category=` shows real categories, not `None`.

---

### F8 — Gate `ws_subscribe_summary` emission on diff non-empty

**Severity:** LOW (Finding 4.6)
**MAAP:** YES — touches `src/agents/ingestion/ws_client.py`.
**Blast radius:** Logging-only path of the WS subscription rotation.

**Why.** 372 emissions in 60 min where the underlying subscription state did not meaningfully change. Codex's Run 1 WS hotfix added subscription diffing; the summary emission appears to be unconditional rather than diff-gated.

**What.** Emit `ws_subscribe_summary` only when the diff between previous and current subscription sets is non-empty (i.e. when at least one token was actually subscribed or unsubscribed in this cycle). Otherwise, increment a counter and emit a single summary line per minute.

**Files.**

- `src/agents/ingestion/ws_client.py` (~15 lines)
- `tests/unit/test_ws_bugs.py` (~2 new cases)

**Code sketch.** None — gating logic only.

**Tests.**

1. `test_ws_subscribe_summary_emitted_when_diff_non_empty`.
2. `test_ws_subscribe_summary_suppressed_when_diff_empty`.

**Risk.** Negligible. Existing WS connection / routing logic untouched.

**Validation in next run.**

- `ws_subscribe_summary` count ≤ ~30 in a 60-min run with no market rotation (today: 372).

---

### F9 — Log rotation

**Severity:** OPERATIONAL (referenced by Finding 4.2 but not a code defect)
**MAAP:** No (operational only; touches `.env` / a logging config file, not source).
**Blast radius:** Log file shape only.

**Why.** Today `logs/orchestrator-run.log` is a single file that grew to 128 MB in 60 min. A 24-hour soak test would produce ~3 GB. Search, tail, archive, and disk usage all suffer.

**What.** Add log rotation via the standard `logging.handlers.RotatingFileHandler` or `TimedRotatingFileHandler` in whatever Python logging config the orchestrator uses (currently it inherits from structlog defaults + `nohup` redirect). Or, simpler: switch the launch command to use `logrotate` or `multilog` externally.

Recommended initial setup: rotate on 50 MB, keep 10 archives. Configurable via `LOG_MAX_BYTES` and `LOG_BACKUP_COUNT` env vars.

**Files.**

- `src/core/logging_config.py` (if exists; otherwise wherever structlog is configured) — ~15 lines.
- `.env.example` — document the new env vars.

**Tests.** Not unit-testable in a meaningful way; smoke test by tailing the log during a 5-min run and verifying rotation happens at the configured size.

**Risk.** Low. Reversible by reverting the env vars to unbounded.

**Validation in next run.**

- After a 30-min run with ~50-100 MB of log output, `logs/orchestrator-run.log` is ≤ 50 MB and one or more `orchestrator-run.log.1` / `.2` / `.gz` files exist.

---

### F10 — Optional: shadow-Gatekeeper at `min_confidence=0.50`

**Severity:** Informational (deferred — captured for completeness from Run 1 Tier 3, still relevant)
**MAAP:** YES — touches `src/agents/evaluation/claude_client.py` (or wherever Gatekeeper runs).
**Blast radius:** Adds a second decision path that *does not* produce executions, only logs.

**Why.** Two consecutive 1-hour observations have produced 0 APPROVED decisions. We do not know whether the Gatekeeper threshold is "too strict" or whether the upstream signal genuinely doesn't merit approval. A shadow run at `min_confidence=0.50` would tell us: of N evaluations that the live Gatekeeper rejected, how many would have been APPROVED at the lower bar?

**Status.** Deferred to a separate WI. Capture as a backlog item; do not implement as part of this plan.

---

## 5. Execution Sequence (sequenced commits)

Each commit is one atomic change. MAAP runs after each `src/` change.

| Order | Commit | Files | MAAP req? |
|---|---|---|---|
| 1 | F0 — pre-flight (no commit, just housekeeping) | — | No |
| 2 | F1 — `.env` adds `LLM_MARKET_HOURLY_CALL_LIMIT=30` (and F3 adds 2 more lines) | `.env` only | No |
| 3 | F2 — budget-block log throttle | `src/agents/evaluation/llm_cost_guard.py`, `src/agents/evaluation/claude_client.py`, `tests/unit/test_WI-52-*.py` | **YES** |
| 4 | F4 — `MARKET_REJECTED` state-transition gating | `src/agents/ingestion/market_discovery.py`, `src/schemas/ops.py`, `tests/unit/test_WI-56-*.py` | **YES** |
| 5 | F5 — SQLite WAL + busy_timeout | `src/db/session.py`, `alembic/env.py`, `tests/integration/test_db_concurrency.py` | **YES** |
| 6 | F6 — per-market budget quarantine | `src/agents/ingestion/market_quarantine.py`, `src/agents/context/bounded_queue.py`, `src/orchestrator.py`, `src/schemas/market_eligibility.py`, `tests/unit/test_WI-53-*.py` | **YES** |
| 7 | F7 — resolve category at activation | `src/orchestrator.py`, `tests/unit/test_orchestrator_market_activation.py` | **YES** |
| 8 | F8 — gate `ws_subscribe_summary` on diff | `src/agents/ingestion/ws_client.py`, `tests/unit/test_ws_bugs.py` | **YES** |
| 9 | F9 — log rotation | logging config + `.env.example` | No |

Total: 1 housekeeping step, 1 config-only PR (`.env` updates from F1+F3+F9), 7 MAAP-gated atomic commits.

After commit 3 (F2), do a 30-min validation run before continuing — F2 alone may unblock the log-noise problem enough to reveal whether F6 is still necessary at the same scope. Re-evaluate the F6 design after that data lands.

---

## 6. Test Strategy (cumulative)

| Layer | Tests added | Existing tests touched |
|---|---|---|
| Unit | ~21 new (F2: 3, F4: 6, F5: 2, F6: 5, F7: 1, F8: 2, F9: smoke only, F10 deferred) | WI-52, WI-53, WI-56 regressions |
| Integration | 1 new (F5: concurrent read during write) | dashboard activity feed re-read |
| Coverage | Must stay ≥ 80% per CLAUDE.md | run `.venv/bin/python -m coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/python -m coverage report -m` after each commit |
| Regression | Full suite (2,296 tests at HEAD `dab61ce`) must pass after each commit | per-commit `.venv/bin/python -m pytest --asyncio-mode=auto tests/` |

---

## 7. Post-Implementation Validation (the next dry-run)

After all commits land, run `/dry-run-review` with a 30-minute window. Expected metrics, derived directly from the Run 2 numbers and the fixes above:

| Metric | Run 2 (today, T+30) | Target (post-fix, T+30) |
|---|---|---|
| Evaluations | 50 | ≥ 120 |
| First per-market budget block | T+7:21 | ≥ T+15 (or absent in window) |
| Per-market block log lines | ~1,700 (at T+30) | ≤ 50 (F2 throttle) |
| `BUDGET_BLOCK` ledger rows | 3,648 (T+60) | ≤ 200 (F2 throttle + F6 quarantine) |
| `MARKET_REJECTED` ledger rows | 12,689 (T+60) | ≤ 50 (F4 transition-only) |
| `database is locked (5)` errors | 2 (during ad-hoc reads) | 0 (F5 WAL) |
| `telegram.dispatched` count | 0 | ≥ 1 within first minute (F3) |
| `ws_subscribe_summary` count | 372 (T+60) | ≤ 30 (F8) |
| Log file size | 62 MB (T+30) | ≤ 20 MB |
| `category=None` in activation log | 15/15 | 0/15 (F7) |
| APPROVED decisions | 0 | informational (not a target; depends on signal quality, not on these fixes) |

---

## 8. Rollback Strategy

Each commit is independently revertible via `git revert`. The plan's commit ordering is dependency-clean:

- F1 (config-only) can be reverted by removing one `.env` line. No DB impact.
- F2 (log throttle) is pure-additive logging behaviour; revert restores per-occurrence emission.
- F4 (`MARKET_REJECTED` gating) requires noting that revert will re-introduce ledger spam but **does not** lose data — the dashboard / replay tools will still function on the old data shape.
- F5 (WAL) revert is safe: SQLite can switch journal modes on the next connection. The `-wal` / `-shm` files will be removed automatically once the DB is opened in rollback (`DELETE`) mode.
- F6 (quarantine) revert is the most consequential: rolling back restores the graveyard-state log spam. Ensure F2 (throttle) stays in place if F6 is rolled back, so the log noise stays bounded.
- F7, F8 (low-severity logging fixes) trivially revertible.
- F9 (log rotation) revertible by removing env vars; existing rotated files stay on disk for cleanup.

If the validation run in Section 7 shows any regression in safety metrics (any `approved=True` order signed during DRY_RUN, any bypass of `LLMEvaluationResponse`, any `float` introduced in money paths), revert **all** commits in this plan and re-open the planning step.

---

## 9. Open Questions to Resolve Before Execution

1. **What is the correct value for `llm_market_hourly_call_limit`?** This plan recommends 30, derived from "10 was clearly too low, 60 (the global cap) would let a single market consume the whole budget." A more principled answer requires modelling the bot's natural per-market snapshot cadence under representative markets. A 5-minute data-collection run with the F2 throttle in place would inform the answer.
2. **Should F3 (Telegram diagnostic) escalate to a code fix?** Depends on whether `ENABLE_TELEGRAM_NOTIFIER=true` alone fixes the gap. If the alerts bridge has a severity-routing filter that drops `INFO` alerts, a one-line code change is needed. Do not plan this until the diagnostic confirms.
3. **Should F4's new event type be `MARKET_ACCEPTED` or `MARKET_RECOVERED`?** Semantic difference: "accepted" describes the new state; "recovered" describes the transition. WI-56 schemas should choose consistently. Defer to whoever owns `src/schemas/ops.py`.
4. **Is `LLM_REFLECTION_HOURLY_CALL_LIMIT` set to a sensible default in `dab61ce`?** This plan assumes yes (it never fired in Run 2). If it is set to a very high value, reflection could become the next runaway cost. Verify in the `CONFIG_LOADED` event payload during F0.
5. **Should F6 (quarantine) be deferred until F1+F2 validation proves it is still needed?** Possibly. If F1+F2 alone bring per-market block events to ~50 in a 60-min run, F6 is incremental, not critical. Re-evaluate after commit 3.
6. **Does the user want Telegram delivery to fire on the next restart?** F3 will trigger one `process_started` ping to chat `8840799632`. Confirm before applying.
7. **Should snapshot retention be addressed in this plan?** Run 2 ingested ~78,000 snapshots in 60 min (DB grew 115 MB). The plan does not include a retention fix because no observation in Run 2 was *caused* by snapshot table size. Capture as a separate WI.
8. **What is the budget for log-rotated archives?** F9 defaults to 10 × 50 MB = 500 MB. Operator-configurable, but a default needs to be picked.

---

## 10. Timeline Estimate

Assuming one engineer pairing with a Checker agent under MAAP:

| Step | Active work | MAAP / review | Test runtime | Total |
|---|---|---|---|---|
| F0 + F1 + F3 (.env only) | 15 min | n/a | n/a | 15 min |
| F2 (throttle) | 60 min | 30 min | 8 min | ~1h 40m |
| F4 (rejection gating) | 90 min | 45 min | 8 min | ~2h 25m |
| F5 (WAL) | 30 min | 20 min | 12 min | ~1h |
| F6 (quarantine) | 2h | 60 min | 10 min | ~3h 10m |
| F7 (category) | 20 min | 15 min | 8 min | ~45 min |
| F8 (ws diff gate) | 30 min | 20 min | 8 min | ~1h |
| F9 (log rotation) | 20 min | n/a | smoke 5 min | 25 min |
| **Total** | | | | **~10–11 hours of focused work** |

Followed by a 30-min validation dry-run via `/dry-run-review 30`.

---

## 11. What Could Go Wrong

1. **F6 (quarantine) interacts badly with F4 (rejection gating).** A market that enters quarantine is technically still "accepted" from the discovery pipeline's POV, so F4's `_last_accepted` set must not be confused by the quarantine state. Mitigation: F4 only reads discovery results, not quarantine state; quarantine is purely a downstream gate.
2. **F5 (WAL) plus a buggy migration could leave the DB in WAL mode with no `-wal` checkpoint on shutdown.** Mitigation: SQLite handles this correctly on restart by replaying the WAL. Document in the runbook.
3. **F2 (log throttle) suppresses a real signal during initial development.** Mitigation: the throttle is keyed per-(market, reason); a new reason code always emits. Tracebacks and ERRORs are not gated by this throttle.
4. **F3 (Telegram) ends up requiring a code change after the diagnostic, blocking the rest of the plan.** Mitigation: F3 is independent of F1/F2/F4/F5/F6/F7/F8/F9. If it escalates, defer to a separate PR; do not block the others.
5. **The per-market window release at the hour boundary turns out not to be the right model.** Mitigation: F6's quarantine carries an explicit window-end timestamp derived from the guard itself, not assumed from clock-hour boundaries. Whatever the guard considers "window end" is what we honour.
6. **Coverage drops below 80% on any commit.** Mitigation: each test addition is paired with the code change in the same commit. Run coverage gate locally before pushing.

---

## 12. Definition of Done for This Plan

The plan is "done" when:

- All 7 MAAP-gated commits are merged to `develop`.
- Full test suite passes (`2,296 + ~21 new = ~2,317 tests`).
- Coverage ≥ 80%.
- A 30-min `/dry-run-review` validation run produces the metrics in Section 7's "Target" column.
- No regression in Run 2's resolved findings (Grok still 100% reliable; primary/reflection budget split still in effect; observability subsystems still enabled).
- `STATE.md` and `03_Daily/2026-05-17.md` updated with the post-Run-2 hotfix description.
- A PR from `develop` to `main` is opened and merged.

---

## 13. Files Touched by This Plan (none yet — planning only)

| File | Fix | Type | MAAP |
|---|---|---|---|
| `.env` | F1, F3, F9 | config | No |
| `.env.example` | F9 | docs | No |
| `src/core/logging_config.py` (or equivalent) | F9 | code | No (not under MAAP scope) |
| `src/db/session.py` | F5 | code | YES |
| `alembic/env.py` | F5 | code | No (test-time only) |
| `src/agents/evaluation/llm_cost_guard.py` | F2 | code | YES |
| `src/agents/evaluation/claude_client.py` | F2 | code | YES |
| `src/agents/ingestion/market_discovery.py` | F4 | code | YES |
| `src/schemas/ops.py` | F4 | code | YES |
| `src/agents/ingestion/market_quarantine.py` | F6 | code | YES |
| `src/agents/context/bounded_queue.py` | F6 | code | YES |
| `src/schemas/market_eligibility.py` | F6 | code | YES |
| `src/orchestrator.py` | F6, F7 | code | YES |
| `src/agents/ingestion/ws_client.py` | F8 | code | YES |
| `tests/unit/test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py` | F2 | tests | n/a |
| `tests/unit/test_WI-56-operational-event-ledger.py` | F4 | tests | n/a |
| `tests/integration/test_db_concurrency.py` (new) | F5 | tests | n/a |
| `tests/unit/test_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.py` | F6 | tests | n/a |
| `tests/unit/test_orchestrator_market_activation.py` | F7 | tests | n/a |
| `tests/unit/test_ws_bugs.py` | F8 | tests | n/a |

Estimated source LOC: ~200 added, ~10 removed (the redundant narrative log in F2).
Estimated test LOC: ~350 added across new and existing files.

---

## 14. Closing

The Run 2 findings are sharper, more localized, and easier to fix than the Run 1 set. The biggest single win in this plan is F6 (quarantine) combined with F2 (throttle): together they convert the current "produce 50 evals then sit in noisy idle for 53 minutes" pattern into "produce evals continuously up to the per-market cap, then idle silently until the window resets." That alone should restore the agent to a state where a single dry-run window can plausibly produce APPROVED decisions when real signal arrives.

None of the planned changes weaken `DRY_RUN`, bypass `LLMEvaluationResponse`, introduce `float` in any money/EV path, or touch the Gatekeeper threshold. Every change cites a real `file:line` for its root cause. The plan is MAAP-clean by construction.

Once the user signs off on the Section 9 open questions (in particular Q1 cap value, Q2 Telegram code-fix needed, Q6 acceptable to ping Telegram chat on restart), F0/F1/F3 can be applied immediately and F2 can begin.
