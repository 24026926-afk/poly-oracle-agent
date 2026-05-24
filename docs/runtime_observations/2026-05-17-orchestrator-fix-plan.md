# Orchestrator Fix Plan — 2026-05-17

**Author:** Claude Code (planning, no modifications)
**Companion document:** `2026-05-17-orchestrator-dry-run-session.md` (observation report)
**Status:** PLAN ONLY — no code or config changes have been applied as part of this document.
**Target branch (not yet created):** `feat/runtime-stabilization-post-2026-05-17`
**Scope discipline:** every change in this plan is intended to fix a *root cause observed in this session's logs.* No speculative refactors, no feature additions.

---

## 0. Why a separate planning document

The observation report (`2026-05-17-orchestrator-dry-run-session.md`) catalogued 9 findings and 14 recommendations. This document converts the actionable subset into a sequenced execution plan with file-level scope, code sketches, test approach, MAAP requirements, risk ranking, and validation criteria — **without applying any of it.** It exists so a human reviewer (and a future Checker agent under MAAP) can approve the *plan* before any source-tree state changes.

The orchestrator (PID 26469) is **left running** during this planning step. It is in budget-exhausted steady-state and will keep running until the executor explicitly stops it in F0.

---

## 1. Newly Observed Signal Since Report Was Written

**HTTP 429 from xAI** starting at **21:21:38 UTC** (~13 min after restart, ~8 min after LLM budget cap). 5 occurrences captured so far. This is xAI's own server-side rate limit, separate from our internal `LLMBudgetGuard`. It happened because:

- After our internal LLM cost guard exhausted (21:13:54), the bot stopped calling DeepSeek primary/reflection but **kept calling Grok** for every incoming snapshot (Grok is invoked before the LLM budget check in the pipeline).
- ~8 minutes of unthrottled Grok calls on a drained snapshot pipeline (Grok runs on every snapshot, fast — no LLM blocking it) saturated whatever per-minute or per-day quota the xAI free / low-tier API permits.

This finding **promotes "Skip Grok when LLM budget is exhausted" from a low-priority optimization to a Tier-1 fix.** It is now F4 below. Without it, we are paying for (and being rate-limited by) Grok calls whose sentiment will never be used because the downstream LLM cannot consume them.

---

## 2. Goals of This Plan

In priority order:

1. **Unblock real evaluation throughput.** Today the bot can produce ≤30 evaluations per hour. Target: ≥120/hour, capacity for ≥1 APPROVED decision in a 30-min observation window.
2. **Make every WI-56→60 observability subsystem actually fire in dry-run.** Today they default off; today's run is invisible to all of them.
3. **Stop wasting external-API calls** (xAI 429) and internal calls (reflection on guaranteed-HOLD primaries).
4. **Reduce write/log volume** so a 1-hour run does not create 160 MB of DB or 40 MB of log.
5. **Keep every change MAAP-clean** with explicit test coverage and a documented rollback for each fix.

What is **explicitly out of scope** for this plan:
- No `DRY_RUN=false` changes.
- No live signing / broadcasting.
- No Gatekeeper threshold changes (`min_confidence=0.75` stays; we discuss shadow Gatekeeper for a *separate* future plan).
- No PostgreSQL migration.
- No new WIs. Every change here is a fix or hardening of existing WIs.
- No prompt-strategy or reflection-strategy redesign.
- No new LLM providers.

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
1. Send SIGTERM to current orchestrator (PID 26469). Wait for clean exit.
2. Stop active Monitor (task `b0djvhkig`).
3. Archive current logs:
   - `logs/orchestrator-run.log` → `logs/orchestrator-run-2026-05-17-post-grok-fix.log`
   - `logs/orchestrator-run-pre-grok-fix.log` stays as-is.
4. Snapshot DB sizes / row counts to `docs/runtime_observations/2026-05-17-pre-fix-snapshot.txt` for before/after diff.
5. Create branch `feat/runtime-stabilization-post-2026-05-17` off `develop`.
6. Verify `git status` shows only the in-flight `.env` + `grok_client.py` changes from the mid-session hotfix. Stash them; we will re-apply as part of F3 commits.

**Tests.** None.

**Risk.** Low. Reversible by `git stash pop` and `git branch -D`.

**Validation.** `git status` clean on the new branch except for the stashed hotfix.

---

### F1 — Enable observability subsystems (`.env` only)

**Severity:** HIGH (observability gap)
**MAAP:** No (config-only, no code touched)
**Blast radius:** Runtime behavior only. Tests do not consume `.env`.

**Why.** Per Finding 4.4 of the observation report: all four `ENABLE_*` flags default `False`. The current run is invisible to WI-56 (event ledger), WI-57 (narratives), WI-58 (incident replay), WI-59 (dashboard activity feed), WI-60 (daily digest), WI-50 (operational alert bridge), WI-27 (circuit breaker), WI-26 (Telegram).

**What.** Append to `.env`:
```
# --- Observability (WI-50, WI-56) ---
ENABLE_TELEGRAM_NOTIFIER=true
ENABLE_STARTUP_ALERT=true
ENABLE_OPERATIONAL_ALERTS=true
ENABLE_OPERATIONAL_EVENT_LEDGER=true
# Circuit breaker stays off explicitly for dry-run (no live drawdown to halt)
ENABLE_CIRCUIT_BREAKER=false
```

**Files.** `.env` (uncommitted).

**Tests.** None required (config defaults are unchanged in tests). Optional: a regression test in `tests/unit/test_orchestrator_startup.py` that asserts startup logs `operational_event_ledger.enabled` when env flag is set. Out of scope for this fix.

**Risk.** Telegram start-up alert will fire to chat 8840799632 within seconds of restart. Confirm with the user that this is acceptable before applying.

**Validation in next run.**
- Startup logs show `operational_event_ledger.enabled`, `operational_alerts.enabled` instead of `*.disabled`.
- `operational_events` table row count > 0 after first minute.
- Telegram chat receives `process_started` notification within 5s of restart.
- `dashboard activity feed` panel populated when `streamlit run src/ui/dashboard.py` is opened.

---

### F2 — Separate primary and reflection LLM budget counters

**Severity:** HIGH (structural throughput cap)
**MAAP:** YES — touches `src/agents/evaluation/llm_cost_guard.py`, `src/core/config.py`, `src/agents/evaluation/claude_client.py`.
**Blast radius:** All paths that consult `LLMBudgetGuard`. Per-market and per-window counters.

**Why.** Per Finding 4.1: the current `_global_window.hourly_calls` is incremented on every registered call regardless of `call_type`. Setting `llm_hourly_call_limit=60` therefore caps the system at 30 *full* evaluations per hour (1 primary + 1 reflection each). At the bot's natural cadence (~6-8 evals/min), the cap is hit in 5 minutes.

The economic intent of the gate is to bound trading-action volume, not to ration the reflection quality layer. Reflection is what *makes* the bot safe; budgeting it against the trading cap inverts the incentives.

**What.** Add a parallel counter for reflection calls, and have the budget guard accept a `call_type` and consult the right counter.

**Files.**
- `src/core/config.py` (~5 lines added)
- `src/schemas/llm.py` (extend `LLMBudgetState` / similar schema if it tracks counters)
- `src/agents/evaluation/llm_cost_guard.py` (largest change, ~30 lines)
- `tests/unit/test_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.py` (new regressions, ~5 cases)

**Code sketch (pseudocode, NOT final):**
```python
# config.py
llm_hourly_call_limit: int = Field(default=60, ge=0,
    description="Max PRIMARY LLM calls per rolling hour")
llm_reflection_hourly_call_limit: int = Field(default=120, ge=0,
    description="Max REFLECTION LLM calls per rolling hour (separate from primary)")

# llm_cost_guard.py
@dataclass
class CallWindow:
    primary_calls: list[float]      # timestamps
    reflection_calls: list[float]
    market_calls: dict[str, list[float]]   # unchanged

def check(self, call_type: str, market_key: str) -> BudgetDecision:
    now = time.monotonic()
    self._prune(now)
    if call_type == "primary":
        if len(self._w.primary_calls) >= self._cfg.llm_hourly_call_limit:
            return BudgetDecision.blocked(reason="hourly_call_limit_exhausted")
    elif call_type == "reflection":
        if len(self._w.reflection_calls) >= self._cfg.llm_reflection_hourly_call_limit:
            return BudgetDecision.blocked(reason="reflection_hourly_limit_exhausted")
    # per-market cap still applies to both
    if len(self._w.market_calls[market_key]) >= self._cfg.llm_market_hourly_call_limit:
        return BudgetDecision.blocked(reason="market_hourly_limit_exhausted")
    return BudgetDecision.allowed()

def record(self, call_type: str, market_key: str, now: float):
    if call_type == "primary":
        self._w.primary_calls.append(now)
    else:
        self._w.reflection_calls.append(now)
    self._w.market_calls[market_key].append(now)
```

**Tests.**
1. `test_primary_and_reflection_have_separate_caps`: set primary=2, reflection=10; record 2 primary and 3 reflection; assert primary blocks at 3rd call but reflection still allowed.
2. `test_reflection_block_reason_distinct`: assert that exhausting reflection produces `reason="reflection_hourly_limit_exhausted"`, not `"hourly_call_limit_exhausted"`.
3. `test_per_market_cap_still_applies_across_both_call_types`: a single market hitting `llm_market_hourly_call_limit` blocks regardless of call_type.
4. `test_pruning_independent_per_counter`: pruning one window does not affect the other.
5. Regression: every existing `test_WI-52-*` case must still pass; bump existing tests to provide `call_type="primary"` explicitly where they relied on the default.

**Risk.**
- If any caller forgets to pass `call_type`, it could default to the wrong counter. Mitigation: make `call_type` a required positional arg in the budget guard's public API; let the type system catch missing calls.
- Existing `agent_decision_logs` rows reference the old reason code `hourly_call_limit_exhausted`. New rows will sometimes use `reflection_hourly_limit_exhausted`. No schema change; just a new value in the existing string column. Document in the WI-52 runbook.

**Validation in next run.**
- `Evaluation complete` cadence sustained for > 10 minutes without `llm_budget_blocked`.
- If the new `reflection_hourly_limit_exhausted` does fire, it should fire later than `hourly_call_limit_exhausted` would have (proving the split is doing work).
- Daily digest (WI-60) should show ≥ 60 evaluations across a 1-hour window.

---

### F3 — Grok narrative truncate-and-recover

**Severity:** MEDIUM (21% sentiment-loss rate)
**MAAP:** YES — touches `src/agents/evaluation/grok_client.py`.
**Blast radius:** Grok success path only. No effect on timeout, http-error, or skipped paths.

**Why.** Per Finding 4.3: 6 of 28 eligible-category Grok calls (~21%) returned valid narratives that exceeded the `SentimentResponse.top_narrative_summary` 320-char Pydantic limit. The `ValidationError` is caught and the entire sentiment is replaced with `NEUTRAL_SENTIMENT`, discarding real signal. Consistent on Pahlavi and Iran regime markets.

**What.** Before `SentimentResponse.model_validate(data)` at `grok_client.py:361`, sanitize `top_narrative_summary` to ≤320 chars while preserving readability: prefer a cut at the last sentence boundary within the budget, fall back to a hard cut + ellipsis. Also tighten the user prompt with an explicit length cue. Also re-classify schema errors as *retryable* (one retry with stricter prompt suffix), since the model can usually correct itself.

**Files.**
- `src/agents/evaluation/grok_client.py` (~25 lines, split between the truncation helper, the JSON-parse path, and the retry loop)
- `tests/unit/test_WI-12-grok-sentiment-oracle.py` (or equivalent) — new regressions

**Code sketch:**
```python
_MAX_NARRATIVE_CHARS = 320  # mirrors the SentimentResponse field constraint

_USER_PROMPT_TEMPLATE = """\
...
3. CRITICAL: "top_narrative_summary" MUST be 10-320 characters. Replies over 320 chars are rejected.
"""

def _coerce_narrative_length(text: str) -> str:
    if len(text) <= _MAX_NARRATIVE_CHARS:
        return text
    # Prefer a clean sentence-boundary truncation
    cutoff = _MAX_NARRATIVE_CHARS - 1   # leave room for ellipsis
    candidate = text[:cutoff]
    last_period = candidate.rfind(". ")
    if last_period > _MAX_NARRATIVE_CHARS // 2:
        return candidate[:last_period + 1]
    return candidate.rstrip() + "…"

# In _attempt_live_call after json.loads:
data = json.loads(json_str, parse_float=Decimal)
if isinstance(data.get("top_narrative_summary"), str):
    data["top_narrative_summary"] = _coerce_narrative_length(data["top_narrative_summary"])
return SentimentResponse.model_validate(data)

# In _fetch_live retry loop, change schema-error handling from `break` to one extra retry with
# a stricter suffix:
except (ValidationError, KeyError, json.JSONDecodeError) as exc:
    self.last_failure_reason = GrokFailureReason.SCHEMA_ERROR
    logger.warning("grok_sentiment_schema_error", ...)
    if not schema_retry_used:
        schema_retry_used = True
        request = self._build_request(..., extra_suffix="REMINDER: top_narrative_summary MUST be ≤300 chars.")
        continue
    break
```

**Tests.**
1. `test_narrative_under_320_passthrough`: a 200-char narrative is unchanged.
2. `test_narrative_over_320_cut_at_sentence_boundary`: a 500-char narrative ending with multiple sentences is truncated to the last full sentence ≤320 chars.
3. `test_narrative_over_320_no_sentence_boundary_uses_ellipsis`: a 500-char narrative without periods is hard-cut + ellipsis to exactly 320 chars.
4. `test_schema_error_triggers_one_retry_with_stricter_prompt`: mock httpx so first attempt returns a 500-char narrative, second attempt returns 200-char; assert only 2 attempts and final result is the second response.
5. `test_schema_retry_not_attempted_twice`: two consecutive schema errors → fall back to NEUTRAL_SENTIMENT after the first retry.

**Risk.**
- Truncation could chop the most important clause of the narrative if the model put the conclusion last. Mitigation: the prompt update tells the model to put the most important point first. Add as a step 3 of the prompt instructions.
- The schema-retry path adds one extra API call per schema failure (max +5 calls / window at observed rates). Acceptable cost relative to recovering 21% of sentiment signal.

**Validation in next run.**
- `grok_sentiment_schema_error` rate drops from ~21% of eligible to <5%.
- `grok_sentiment status=SUCCESS` rate rises proportionally.
- Number of `grok_sentiment status=FALLBACK reason=schema_error` ≈ 0.

---

### F4 — Skip Grok when LLM budget is exhausted

**Severity:** HIGH (causes xAI 429s; wastes external API quota)
**MAAP:** YES — touches `src/agents/evaluation/claude_client.py` (caller-side gate).
**Blast radius:** Eligible-category snapshot processing only. CULTURE / non-eligible path is unchanged.

**Why.** **New finding observed during planning** (Section 1 above). After the LLM budget capped at 21:13:54, the bot continued issuing Grok calls for ~8 minutes on drained-queue snapshots, hitting xAI's 429 rate-limit at 21:21:38. These Grok calls are pure waste: even if they succeed, the downstream primary LLM cannot consume them (it's budget-blocked). They cost real money against the xAI plan and are counted toward whatever per-minute / per-day quota xAI enforces.

**What.** Add an early check in the snapshot processing path: if `LLMBudgetGuard.check(call_type="primary", market_key=...)` returns blocked, **skip the Grok call** and return the neutral fallback immediately. The downstream `llm_budget_blocked — skipping primary provider call` log still fires (preserving observability of the *real* reason the snapshot was skipped).

**Files.**
- `src/agents/evaluation/claude_client.py` (~15 lines around the Grok-then-LLM sequence in the snapshot handler; ~967 / ~1072 / ~1208 / ~1393 area)
- `tests/unit/test_WI-52-*` — new regressions
- `tests/integration/test_evaluation_pipeline.py` (or equivalent) — integration check that no httpx call to xAI is issued when budget is exhausted

**Code sketch:**
```python
# In snapshot processing, before the existing Grok call:
budget_pre = self._budget_guard.check(call_type="primary", market_key=snapshot.condition_id)
if budget_pre.blocked:
    # Log a single distinct event so this skip is visible
    logger.info(
        "grok_skipped_due_to_llm_budget",
        condition_id=snapshot.condition_id,
        reason=budget_pre.reason,
    )
    grok_result = NEUTRAL_SENTIMENT
    grok_status = "SKIPPED"
    grok_reason = "PRIMARY_BUDGET_EXHAUSTED"
else:
    grok_result = await self._grok_client.fetch_sentiment(...)
    grok_status = ...
    grok_reason = ...
```

Note: this is a check, not a `record`. We are not yet *taking* the slot; the primary call later (or its own budget block) does that.

**Tests.**
1. `test_grok_skipped_when_primary_budget_exhausted`: set primary cap to 0; submit eligible-category snapshot; assert no httpx call to xAI; assert log `grok_skipped_due_to_llm_budget` emitted once.
2. `test_grok_called_when_primary_budget_available`: cap > 0; eligible-category snapshot; assert one httpx call to xAI.
3. `test_skip_applies_to_eligible_only`: non-eligible (CULTURE) snapshot — Grok is already skipped for SKIPPED_CATEGORY reason; this fix should not change that. Assert log says `SKIPPED_CATEGORY`, not `PRIMARY_BUDGET_EXHAUSTED`.
4. `test_grok_skip_does_not_consume_market_budget`: per-market cap is unchanged by the skip.

**Risk.**
- Race condition: budget could change between the pre-check and the actual primary call. Acceptable — worst case we skip Grok and then the primary becomes available, costing us a stale neutral sentiment for one cycle. Logged and recoverable.
- This pre-empts what was previously an implicit safety net (Grok runs no matter what). Mitigation: the per-market budget cap (`llm_market_hourly_call_limit`) still gates the eventual primary call.

**Validation in next run.**
- After LLM primary cap hits, observe `grok_skipped_due_to_llm_budget` lines instead of `grok_sentiment_http_error status_code=429`.
- xAI 429 count = 0 across the run.
- No drop in Grok success rate when budget is available.

---

### F5 — WebSocket snapshot persistence throttle

**Severity:** MEDIUM (DB and disk growth)
**MAAP:** YES — touches `src/agents/ingestion/ws_client.py` and possibly `src/db/repositories/market_repository.py`.
**Blast radius:** All WS-driven persistence. Aggregator and bounded queue paths.

**Why.** Per Finding 4.5: `market_snapshots` grew at ~4,100 rows/min sustained. Projected ~9.6 GB/day. SQLite is responsive but the file outpaces any reasonable retention policy.

**What.** Replace "persist every frame" with "persist when midpoint Δ ≥ X bps OR Δt ≥ N seconds since last persist for this condition." Default X=25 bps, N=2.0s.

**Files.**
- `src/agents/ingestion/ws_client.py` (~20 lines for per-condition throttle state + check)
- `src/core/config.py` (~6 lines: `snapshot_persist_min_bps`, `snapshot_persist_max_interval_sec`)
- `tests/unit/test_ingestion.py` (new regressions)

**Code sketch:**
```python
# config.py
snapshot_persist_min_bps: int = Field(default=25, ge=0)
snapshot_persist_max_interval_sec: float = Field(default=2.0, ge=0.5)

# ws_client.py
class CLOBWebSocketClient:
    def __init__(self, ...):
        ...
        self._last_persist: dict[str, tuple[float, Decimal]] = {}  # condition_id -> (ts, midpoint)

    def _should_persist(self, condition_id: str, midpoint: Decimal, now: float) -> bool:
        prev = self._last_persist.get(condition_id)
        if prev is None:
            return True
        prev_ts, prev_mid = prev
        if now - prev_ts >= self._cfg.snapshot_persist_max_interval_sec:
            return True
        bps_delta = abs(midpoint - prev_mid) * Decimal("10000") / max(prev_mid, Decimal("0.001"))
        return bps_delta >= self._cfg.snapshot_persist_min_bps

    # In snapshot path:
    if self._should_persist(condition_id, midpoint, now):
        await repo.insert_snapshot(snapshot)
        self._last_persist[condition_id] = (now, midpoint)
        logger.debug("market_snapshot_inserted", ...)   # demote from INFO
    else:
        logger.debug("market_snapshot_throttled", ...)
```

**Tests.**
1. `test_first_snapshot_always_persisted`: first frame per condition is persisted regardless of delta.
2. `test_subsequent_within_window_throttled`: same midpoint, 0.5s later — not persisted.
3. `test_midpoint_change_above_bps_persisted`: midpoint Δ=50 bps within 1s — persisted.
4. `test_time_window_forces_persist`: midpoint unchanged, 3s later — persisted.
5. `test_zero_midpoint_does_not_divide_by_zero`: edge case where `prev_mid` is very small.

**Risk.**
- Some downstream analytics may assume 1:1 WS frame to row. Verify by grepping for `market_snapshots` table joins in `src/`, `scripts/`, `docs/`. If any analytic does the 1:1 assumption, document the change in WI-XX migration note.
- Aggregator behavior is unchanged (aggregator consumes in-memory, not from DB).

**Validation in next run.**
- `market_snapshots` row growth drops by ≥10× (target: ~400 rows/min, was ~4,100).
- DB size after 1 hour < 30 MB (was ~165 MB).
- No regression in evaluation cadence (eval consumes from in-memory queue, not DB).

---

### F6 — De-duplicate market re-activation logs

**Severity:** LOW (log noise)
**MAAP:** YES — touches `src/orchestrator.py`.
**Blast radius:** Logging only. No behavioral change.

**Why.** Per Finding 4.7: the market discovery loop emits `ws_subscribe_summary` + 15 × `orchestrator.market_activated` every ~10 s, regardless of whether the active set changed. 720+ redundant log lines per 7 minutes.

**What.** Track the previously-activated set; only emit the summary + per-market lines when the set diff is non-empty.

**Files.** `src/orchestrator.py` (~10 lines in `_activate_markets` and its caller).

**Code sketch:**
```python
# orchestrator.py
async def _activate_markets(self, deduped: list[MarketMetadata]) -> None:
    new_condition_ids = {m.condition_id for m in deduped}
    added = new_condition_ids - self._last_activated_condition_ids
    removed = self._last_activated_condition_ids - new_condition_ids
    if not added and not removed:
        logger.debug("market_activation_unchanged", count=len(new_condition_ids))
        # still re-emit ws_subscribe_summary at DEBUG for ops visibility
        return
    self._last_activated_condition_ids = new_condition_ids
    # existing code path; emit logs at INFO only on real diff
    ...
```

**Tests.**
1. `test_no_diff_emits_debug_only`: call `_activate_markets` twice with same list; assert second call emits no INFO `orchestrator.market_activated` lines.
2. `test_diff_emits_info_for_added_only`: second call with one new market; assert INFO line only for that one.
3. `test_diff_emits_info_for_removed`: second call missing one market; assert removal log fires.

**Risk.**
- Operators may rely on the periodic re-emit as a "still alive" signal. Mitigation: the DEBUG `market_activation_unchanged` line preserves the heartbeat for log-tailing operators.

**Validation.**
- 1-hour run produces ≤ 5 `orchestrator.market_activated` INFO lines per market (only on actual changes), not ~360.

---

### F7 — Resolve `MarketMetadata.category` at activation time

**Severity:** LOW (logging completeness)
**MAAP:** YES (touches schemas + orchestrator).
**Blast radius:** Market metadata loader. Downstream eval code already resolves category separately, so no behavioral change there.

**Why.** Per Finding 4.6: every `orchestrator.market_activated` log shows `category=None`. The category is later resolved by `PromptFactory` / `ClaudeClient` from `tags`. The activation log is misleading.

**What.** In the Gamma loader (or wherever `MarketMetadata` is constructed), populate `.category` from the same `tags` heuristic used downstream. Avoid duplicating the resolution logic by extracting it into `src/schemas/market_eligibility.py` or `src/agents/context/category_resolver.py`.

**Files.**
- `src/agents/ingestion/market_discovery.py` or wherever `MarketMetadata` is built (~5 lines call to a shared resolver)
- New file or extracted function: `src/agents/context/category_resolver.py` (~30 lines, single function `resolve_category(tags: list[str]) -> MarketCategory | None`)
- Whatever currently resolves category in `claude_client.py` / `prompt_factory.py` — refactor to call the shared resolver.
- Tests: `tests/unit/test_category_resolver.py`

**Tests.**
1. `test_resolver_returns_crypto_for_bitcoin_tag`
2. `test_resolver_returns_culture_for_oscar_tag`
3. `test_resolver_returns_none_for_empty_tags`
4. `test_orchestrator_activation_log_shows_resolved_category`

**Risk.**
- If the existing in-line resolution differs subtly from the extracted version, downstream eligibility checks could shift. Mitigation: extract by copy-and-replace with an equivalence test; run the full regression suite.

**Validation.**
- `orchestrator.market_activated category=CRYPTO|CULTURE|IRAN|...` for all activated markets.
- `Evaluation complete market_category=…` matches what activation logged.

---

### F8 — Demote noisy DEBUG-grade events

**Severity:** LOW (log volume)
**MAAP:** YES (touches `src/agents/ingestion/ws_client.py`, `src/agents/context/bounded_queue.py`).
**Blast radius:** Logging only.

**Why.** Per Findings 4.8 and 4.9: `ws_client.skip_last_trade_no_book` (141 in 7 min) and `queue.coalesced` (346 in 7 min) drown out real events.

**What.**
- `queue.coalesced` per-event log → DEBUG. Add a single INFO `queue.coalesce.burst` line when ≥10 coalesces happen in <5s.
- `ws_client.skip_last_trade_no_book` per-event log → DEBUG. Add a single INFO `ws_client.book_warmup_complete` line per condition when the first book arrives, with `pre_book_trades_suppressed=N`.

**Files.**
- `src/agents/context/bounded_queue.py` (~10 lines)
- `src/agents/ingestion/ws_client.py` (~15 lines)
- Tests: existing tests should still pass; add 2 cases for the new "burst" log.

**Tests.**
1. `test_individual_coalesce_does_not_emit_info`: single coalesce — INFO log absent.
2. `test_coalesce_burst_emits_info`: 10 coalesces in <5s — exactly one INFO `queue.coalesce.burst` line.
3. `test_book_warmup_complete_logged_once_per_condition`: 5 `last_trade_price` then `book` — one INFO `ws_client.book_warmup_complete pre_book_trades_suppressed=5`.

**Risk.**
- Loss of per-event detail at INFO. Mitigation: events still exist at DEBUG; metrics already capture rate.

**Validation.**
- 1-hour log size drops by ≥30% at unchanged LOG_LEVEL=INFO.

---

### F9 — Log rotation

**Severity:** LOW (operational hygiene)
**MAAP:** No (logging config / scripts only).
**Blast radius:** Operator-facing.

**Why.** Even after F8, a 1-hour run produces ~50-100 MB of log. Without rotation, the disk grows monotonically across multi-day runs.

**What.** Add a `logging.handlers.RotatingFileHandler` in the structlog setup (or wrap `python -m src.orchestrator` in `logrotate` semantics via the entrypoint script). Default: rotate at 100 MB, keep 5 archives.

**Files.**
- `src/core/logging_config.py` (or equivalent) — likely where structlog is wired
- `entrypoint.sh` — possibly switch to `python ... | tee >(rotatelogs ...)` or rely on docker driver
- Tests: rotation is hard to unit test cleanly; document config in `docs/runbooks/operating-the-orchestrator.md` (or similar).

**Tests.** None unit-testable; integration test could be a 10-min run that writes >100 MB and checks rotation occurred.

**Risk.** Negligible.

**Validation.** Log files in `logs/` rotate after first 100 MB chunk; no single file > 100 MB.

---

### F10 — Optional: shadow Gatekeeper (deferred; capture as a separate WI)

**Severity:** Not a fix; a measurement enhancement.
**MAAP:** Yes when implemented.
**Blast radius:** New table, new code path; *never* touches the real execution path.

**Why.** This session showed 0 APPROVED decisions even with real signal (EV=+0.36). It's currently impossible to tell whether the Gatekeeper is over-tuned (rejecting profitable signals) or correctly conservative (rejecting noise). A shadow Gatekeeper run in parallel with `min_confidence=0.65` and looser reflection-flag interpretation would persist its "would-have-traded" decisions to a `shadow_decisions` table for offline analysis.

**Why deferred.** This is a feature, not a fix. It belongs in a future Phase (e.g., a Phase 17 calibration WI), not in this stabilization plan. Capture in `STATE.md` as a future candidate.

---

## 5. Execution Sequence (sequenced commits)

Each fix is one atomic commit on `feat/runtime-stabilization-post-2026-05-17`. Order matters because some fixes depend on others.

| Order | Fix | Commit message | Depends on | MAAP needed |
|---|---|---|---|---|
| 1 | F0 | (no commit) | — | — |
| 2 | F1 | `chore(env): enable observability subsystems for dry-run` | F0 | No |
| 3 | F2 | `feat(llm-budget): separate primary and reflection hourly caps (WI-52)` | F0 | **Yes** |
| 4 | F4 | `fix(claude-client): skip grok when llm primary budget exhausted` | F2 | **Yes** |
| 5 | F3 | `fix(grok-client): truncate-and-recover oversized sentiment narratives (WI-12)` | F0 | **Yes** |
| 6 | F5 | `feat(ws-client): throttle market snapshot persistence by bps/time` | F0 | **Yes** |
| 7 | F6 | `chore(orchestrator): only log market_activated on activation diff` | F0 | **Yes** |
| 8 | F7 | `refactor(category): resolve MarketMetadata.category at activation time` | F0 | **Yes** |
| 9 | F8 | `chore(logging): demote per-event noise to DEBUG, add burst markers` | F0 | **Yes** |
| 10 | F9 | `chore(ops): add 100MB rotating log handler` | F0 | No |

Total: 8 MAAP-gated commits, 2 config-only commits. Plus pre-flight (F0) and post-merge validation run.

After all 8 land on `develop`, open PR `develop → main` for Phase 16.5 hotfix release.

---

## 6. Test Strategy (cumulative)

For each MAAP-gated commit:

1. Author runs targeted tests for the file(s) touched.
2. Author runs full suite: `.venv/bin/python -m pytest --asyncio-mode=auto tests/`
3. Author runs coverage check: `.venv/bin/python -m coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/python -m coverage report -m`
4. Coverage must remain ≥ 80% (per CLAUDE.md).
5. `ruff check .` + `ruff format --check .` pass.
6. Author posts `git diff` for Checker MAAP review.

Cumulative regression run after all commits land: full suite + 30-min orchestrator dry-run validation (see Section 7).

---

## 7. Post-Implementation Validation (the next dry-run)

After all 8 commits land, run `python3 -m src.orchestrator` for 60 minutes and assert:

| Metric | Target | Was (pre-fix) |
|---|---|---|
| Evaluations / hour | ≥ 120 | ~30 |
| APPROVED decisions | ≥ 1 (or detailed reasoning why not) | 0 |
| Grok timeouts | 0 | ~169 in 11 min |
| Grok schema errors | < 5% of eligible-call total | ~21% |
| xAI HTTP 429 count | 0 | 5+ |
| `operational_events` rows | > 0 | 0 |
| `market_snapshots` rows / hr | < 30,000 | ~245,000 |
| Telegram startup alert received | yes | no (subsystem disabled) |
| `orchestrator.market_activated` INFO lines / hr | ~15 (one per actual change) | ~720 (cycling) |
| Log file size after 1 hour | < 50 MB | ~155 MB |
| Errors / Tracebacks | 0 | 0 (preserve this) |

If any target is missed, that's a finding for the next iteration; document and decide whether to revert / patch / defer.

---

## 8. Rollback Strategy

Each commit is small and isolated. Rollback options, fastest first:

- **Single fix regressed:** revert that one commit via `git revert <sha>` on `develop`. Open targeted PR.
- **Multiple fixes interacting badly:** revert the whole branch's merge from `develop`. The 8 commits become a single revert.
- **Critical failure in production:** `git reset --hard <pre-merge-sha>` is *not* permitted on `develop` per CLAUDE.md hard constraints. Use `git revert -m 1 <merge-sha>` instead.

Each `.env` change can be rolled back by simply removing the added lines and restarting; no migration impact.

The DB has no schema changes in this plan — `market_snapshots`, `agent_decision_logs`, `operational_events`, etc. are all unchanged. No Alembic migration required. Rollback is purely a code/config revert.

---

## 9. Open Questions to Resolve Before Execution

The executor should answer these (or get user sign-off) before starting F1:

1. **Telegram blast radius.** F1 will cause `process_started` and `process_stopped` alerts to fire to chat `8840799632` every restart. Confirm acceptable.
2. **DeepSeek primary cap intent.** Is the current `llm_hourly_call_limit=60` intended as "60 actions/hr" (in which case F2 fixes the bug) or "60 LLM calls of any type" (in which case F2 is a *behavior change* requiring user signoff)? My read of the runbook (`docs/runbooks/llm-cost-guard.md`) suggests the former; confirm.
3. **Snapshot retention policy.** F5 reduces DB growth but does not retroactively prune. Do we add a one-time cleanup of pre-fix `market_snapshots` rows (~98k rows, ~165 MB) as part of F0? Or leave for a separate WI?
4. **xAI tier.** Is the current xAI account on the free tier? If so, 429s may be inevitable at any non-trivial volume; F4 (skip Grok on budget) is necessary but not sufficient. Upgrading the xAI tier is a billing decision out of scope here but should be flagged.
5. **Should F3 retry the schema error?** Adds ≤1 extra Grok call per failure. At observed 6 errors / 5 minutes, that is 72 extra calls / hour, well inside any reasonable xAI cap if F4 is also in place. Confirm acceptable.
6. **F7 category resolution semantics.** The current `claude_client.py` resolver: is its mapping table the source of truth, or is there a yet-undiscovered alternative in the codebase? Quick `grep` audit needed before extraction.
7. **Branch name.** Is `feat/runtime-stabilization-post-2026-05-17` acceptable, or should this be split into multiple branches (one per fix)?
8. **Phase numbering.** This stabilization is a follow-up to Phase 16 close. Should it be tagged as Phase 16.5 hotfix release, or rolled into Phase 17 planning?

---

## 10. Timeline Estimate

Assuming one engineer working sequentially, no surprises, MAAP turnaround ≤ 30 min per commit:

| Phase | Time | Notes |
|---|---|---|
| F0 pre-flight | 15 min | Branch + stash + archive |
| F1 .env edit | 10 min | Plus user Telegram confirmation |
| F2 budget split | 90 min | Most complex; 5 tests; touches 3 files |
| F4 skip-grok-on-budget | 45 min | Builds on F2; 4 tests |
| F3 narrative truncation | 60 min | 5 tests; touches 1 file plus tests |
| F5 snapshot throttle | 60 min | 5 tests; config additions |
| F6 dedupe activation logs | 30 min | 3 tests; small change |
| F7 category resolver | 60 min | 4 tests; mini-refactor |
| F8 demote noisy logs | 30 min | 2 tests; small change |
| F9 log rotation | 30 min | No unit tests |
| Cumulative regression run | 30 min | Full suite + coverage |
| 60-min orchestrator validation | 75 min | Per Section 7 |
| **Total** | **~9 hours** | Assuming everything passes first try |

Realistic add-on for MAAP review iterations, test surprises, and one debug cycle: **~12 hours total.** Plan for two sessions.

---

## 11. What Could Go Wrong

- **F2 (budget split) breaks an existing test that depended on the unified counter.** Mitigation: search `tests/` for `hourly_call_limit` references first; bump them to pass `call_type` explicitly.
- **F4 (skip-grok) reduces Grok's training-signal capture in dry-run.** If the operator wants Grok signal *for analytics* even when not for trading, this fix is the wrong call. Mitigation: gate F4 behind an env flag `SKIP_GROK_ON_BUDGET=true` (default true).
- **F5 (snapshot throttle) breaks the dashboard's price chart.** Mitigation: check `src/ui/dashboard.py` for `market_snapshots` queries; ensure they handle the lower density.
- **F7 (category extraction) silently changes which markets are Grok-eligible.** Mitigation: run an equivalence test that asserts the new resolver produces the same `MarketCategory` for every entry in the old `_GROK_ELIGIBLE`-style mapping.
- **F1 (Telegram + ledger) reveals more bugs in WI-26 / WI-56.** They are tested in isolation but have not been observed live in this run. Mitigation: keep the first post-fix run to a 30-min observation, not 60, so we can iterate quickly.

---

## 12. Definition of Done for This Plan

This plan is "done" (i.e., ready to execute) when:

- [ ] User has signed off on Section 9 open questions (specifically Q1, Q2, Q4).
- [ ] User confirms branch name and Phase numbering (Q7, Q8).
- [ ] Plan reviewed by a second agent / Checker, no Tier 1 finding contradicted.
- [ ] No new findings have arrived from the still-running orchestrator (PID 26469) that would change priorities.

Once executed (i.e., all 8 commits merged + 60-min validation passes Section 7 targets), this plan is "delivered" and should be archived into `04_Archive/poly-oracle-agent/runtime_observations/` alongside the observation report.

---

## 13. Files Touched by This Plan (none yet — planning only)

| File | Fix | Change kind | LOC est. |
|---|---|---|---|
| `.env` | F1 | Add 5 lines | +5 / -0 |
| `src/core/config.py` | F2, F5 | Add fields | +12 / -0 |
| `src/agents/evaluation/llm_cost_guard.py` | F2 | Refactor counter | +30 / -10 |
| `src/agents/evaluation/claude_client.py` | F4 | Early-skip gate | +15 / -0 |
| `src/agents/evaluation/grok_client.py` | F3 | Truncate + retry | +25 / -5 |
| `src/agents/ingestion/ws_client.py` | F5, F8 | Throttle + demote | +35 / -5 |
| `src/agents/context/bounded_queue.py` | F8 | Demote + burst | +10 / -3 |
| `src/orchestrator.py` | F6 | Dedupe log | +10 / -3 |
| `src/agents/context/category_resolver.py` | F7 | New (extracted) | +30 / -0 |
| `src/agents/ingestion/market_discovery.py` | F7 | Use resolver | +5 / -0 |
| `src/agents/evaluation/claude_client.py` | F7 | Use resolver | +5 / -10 |
| `src/core/logging_config.py` | F9 | Rotation handler | +10 / -0 |
| Tests (multiple) | F2-F8 | New + updates | ~150 / -0 |
| `STATE.md` | F0 | Document new branch | +10 / -0 |
| **Total** | | | **~330 / -36** |

This is a **medium-sized PR**. Splitting into per-fix PRs is recommended for review hygiene.

---

## 14. Closing

This plan is conservative and surgical. It does not change any business logic, prompt, model strategy, Gatekeeper threshold, or DRY_RUN posture. It fixes 9 concrete defects observed in this session's logs, in dependency order, with explicit tests and rollback for each.

The single most impactful fix is **F2 (separate primary/reflection budget)**: it converts the bot from a "30 evals/hr ceiling" system to a "120 evals/hr ceiling" system, which alone should produce visible APPROVED decisions in the next 30-min observation. F4 (skip-grok-on-budget) prevents the new 429 finding from recurring. F1 (enable observability) makes everything else inspectable.

The remaining six fixes are hygiene that prevents the log/DB from outgrowing reasonable bounds and makes future debugging cheaper. None of them change behavior in a way that affects trading decisions.

**Next step:** review Section 9's open questions, then sequence into execution.
