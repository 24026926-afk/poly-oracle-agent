# Orchestrator Fix Plan — 2026-05-18

**Author:** Claude Code (planning, no modifications)
**Companion document:** `2026-05-18-orchestrator-dry-run-session-run4.md` (observation report)
**Status:** PLAN ONLY — no code or config changes have been applied as part of this document.
**Target branch (not yet created):** `feat/runtime-stabilization-run4-2026-05-18`
**Scope discipline:** every change in this plan is intended to fix a *root cause observed in this session's logs.* No speculative refactors, no feature additions.

---

## 0. Why a separate planning document

The observation report (`2026-05-18-orchestrator-dry-run-session-run4.md`) catalogued 7 findings (1 HIGH, 3 MEDIUM, 3 LOW/OBSERVATION). This document converts the actionable subset into a sequenced execution plan with file-level scope, code sketches, test approach, MAAP requirements, risk ranking, and validation criteria — **without applying any of it.** It exists so a human reviewer (and a future Checker agent under MAAP) can approve the *plan* before any source-tree state changes.

The orchestrator (PID 54133) is **left running** during this planning step. It is in stable steady-state at ~1.15 evaluations/min with queue coalescing.

---

## 1. Newly Observed Signal Since Report Was Written

**WS disconnects continuing past the observation window.** After the T+60 snapshot at 17:14:01, a fourth disconnect was observed (noted in the observations report at T+62+). The `consecutive_failures` counter escalated to 4+ without visible reset. This means the exponential backoff will continue (8s, 16s, ...) if the orchestrator continues running, potentially causing evaluation gaps.

Additionally, the **queue coalescing accelerated from 0 to 33 across the 60-minute window**, suggesting the snapshot ingestion pipeline is gradually saturating. While coalescing is by design, this rate (33 coalesces/hour in the 3-market case) implies that at higher market-activation counts, the queue would fully saturate much faster.

---

## 2. Goals of This Plan

In priority order:

1. **Unlock market access.** Today 3 of 100 markets activated. Target: ≥20 markets activated to get signal diversity and enable the bot to find positive EV.
2. **Stabilize WS connectivity.** Today 4 disconnects in 60 min. Target: 0 disconnects in a 60-min window, or capped backoff that prevents runaway escalation.
3. **Add visibility into market spread landscape.** Today the operator has no idea what the other 75 markets look like. Target: spread distribution report at startup.
4. **Add snapshot persistence throttle.** Today 257K snapshots in one session (654/min). Target: ≤100/min by skipping micro-ticks.
5. **Add Python-side EV validation** to reduce wasted reflection calls when primary model arithmetic is wrong.
6. **Keep every change MAAP-clean** with explicit test coverage and a documented rollback for each fix.

What is **explicitly out of scope**:
- No `DRY_RUN=false` changes.
- No live signing, broadcasting, or order placement.
- No Gatekeeper threshold relaxation.
- No new LLM providers or model selection changes.
- No database migration.
- No new Work Items. Every change here is a calibration fix or configuration hardening of existing WIs.
- No prompt-strategy redesign.

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
- Session end appends summary to `03_Daily/2026-05-18.md`.

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

### F1 — Increase preflight candidate pool and add spread distribution report

**Severity:** HIGH (without this, the bot cannot access enough markets)
**MAAP:** Yes (touches `src/agents/ingestion/market_discovery.py`)
**Blast radius:** Startup path only. No effect on runtime evaluation loop.

**Why.** `market_discovery_max_preflight_candidates=25` means 75 of 100 active markets are never checked for order books. Combined with the spread threshold, only 3 markets activate. The bot needs ≥10 markets across multiple categories to find positive EV.

**What.**
1. In `.env`: set `MARKET_DISCOVERY_MAX_PREFLIGHT_CANDIDATES=75`
2. In `src/agents/ingestion/market_discovery.py`: after preflight loop, add a `structlog` summary of spread distribution across all preflighted markets (count by status, min/max/median spread for ELIGIBLE and SPREAD_TOO_WIDE).
3. In `.env`: set `PREFLIGHT_MAX_SPREAD_PCT=1.0` — effectively disable spread gating by threshold. Markets with crossed books (bid ≥ ask, line 424) will still be filtered.

**Files.**
- `.env` — `MARKET_DISCOVERY_MAX_PREFLIGHT_CANDIDATES`, `PREFLIGHT_MAX_SPREAD_PCT`
- `src/agents/ingestion/market_discovery.py:220-230` — after preflight loop, add spread summary
- `src/core/config.py:164` — existing max_candidates config field

**Code sketch (market_discovery.py, after line ~228):**
```python
# After preflight loop completes:
spreads = [r.spread for r in preflight_results
           if r.status in (ELIGIBLE, SKIPPED)
           and r.spread is not None and r.spread > 0]
if spreads:
    avg = sum(spreads) / len(spreads)
    await logger.ainfo(
        "market_discovery.preflight_spread_summary",
        eligible_count=len(eligible),
        spread_too_wide_count=len(spread_skipped),
        min_spread=str(min(spreads)),
        max_spread=str(max(spreads)),
        median_spread=str(sorted(spreads)[len(spreads)//2]),
    )
```

**Tests.** Add a test in `tests/unit/test_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.py` that verifies the spread summary log is emitted with correct counts when preflight processes ≥1 market. Mock `MarketDiscovery.preflight_market` to return controlled spread values.

**Risk.** Low. The spread summary is additive — it logs new information without changing the activation path. The `PREFLIGHT_MAX_SPREAD_PCT=1.0` effectively makes preflight informational only. Crossed-book and order-book-unavailable checks remain blocking. If a market with truly garbage books activates, the downstream queue/dedup logic handles it.

**Validation.** Next dry-run startup should show `preflight_spread_summary` with ≥10 markets in each status bucket. Activated count should increase from 3 to ≥15.

---

### F2 — Python-side EV arithmetic validation before reflection

**Severity:** MEDIUM (reduces wasted reflection calls, but doesn't change decisions)
**MAAP:** Yes (touches `src/agents/evaluation/claude_client.py`)
**Blast radius:** Evaluation pipeline only. Gatekeeper inputs unchanged. Fail-closed (reject on mismatch).

**Why.** DeepSeek-chat consistently produces EV calculations that are arithmetically inconsistent with the p_true, p_market, bid, and ask values it states in the same evaluation. All 23 non-zero-EV evaluations in this session were flagged by reflection for EV arithmetic errors. Adding a pre-reflection validation would:
- Catch arithmetic errors before spending a reflection call
- Surface `ev_arithmetic_mismatch` as a labeled metric
- Allow the system to re-request primary evaluation with explicit EV formula instructions

**What.** In the primary evaluation path (after `LLMEvaluationResponse` is parsed, before reflection is called), compute the expected EV from the response's stated values and compare:
```
stated_ev = Decimal(response.expected_value)  # already Decimal
# EV for buying YES at best_bid:
expected_ev = p_true * (1 - best_bid) - (1 - p_true) * best_bid
```
If `abs(stated_ev - expected_ev) > Decimal("0.02")`, log `ev_arithmetic_mismatch` at WARNING, skip reflection, and HOLD with reason `EV_ARITHMETIC_ERROR`.

**Files.**
- `src/agents/evaluation/claude_client.py` — primary evaluation path, after `LLMEvaluationResponse` parsed
- `src/schemas/llm.py` — add `EV_ARITHMETIC_ERROR` to reflection rejection reasons
- `tests/unit/test_claude_client.py` or equivalent — test EV validation

**Code sketch:**
```python
from decimal import Decimal

EV_TOLERANCE = Decimal("0.02")

def _validate_ev_arithmetic(
    p_true: Decimal,
    p_market: Decimal,
    best_bid: Decimal,
    best_ask: Decimal,
    stated_ev: Decimal,
    action: str,
) -> tuple[bool, Decimal]:
    """Returns (is_valid, computed_ev)."""
    if action == "BUY":
        computed_ev = p_true * (Decimal("1") - best_ask) - (Decimal("1") - p_true) * best_ask
    elif action == "SELL":
        computed_ev = (Decimal("1") - p_true) * best_bid - p_true * (Decimal("1") - best_bid)
    else:  # HOLD
        computed_ev = Decimal("0")
    delta = abs(stated_ev - computed_ev)
    return delta <= EV_TOLERANCE, computed_ev
```

**Tests.** Add parametrized tests for BUY/SELL/HOLD with known EV values. Test that mismatch triggers HOLD with EV_ARITHMETIC_ERROR flag and no reflection call. Test that match allows evaluation to proceed to reflection.

**Risk.** Medium. If the EV formula is correct but the LLM's EV interpretation is valid in a way we don't capture (e.g., the LLM computes risk-adjusted EV differently), we risk false-positive rejections. The 0.02 tolerance is intentionally permissive to handle rounding.

**Validation.** Next dry-run should show `ev_arithmetic_mismatch` log lines for DeepSeek-chat evaluations. When Claude Sonnet 4 is tested as primary, the mismatch rate should be lower.

---

### F3 — WS disconnect backoff cap and reconnect success logging

**Severity:** MEDIUM (4 disconnects in 60 min, backoff escalating)
**MAAP:** Yes (touches `src/agents/ingestion/ws_client.py`)
**Blast radius:** WebSocket lifecycle only. No effect on evaluation pipeline.

**Why.** The current reconnect backoff is unbounded exponential (1s → 2s → 4s → 8s → ...). With 4 disconnects already in this session and `consecutive_failures` escalating, the backoff could reach minutes, causing the bot to lose market data for extended periods. A cap at 30s prevents runaway escalation. Adding `ws_client.reconnect` success logging gives visibility into when the connection recovers.

**What.**
1. In `src/core/config.py`: add `ws_reconnect_max_backoff_seconds: float = Field(default=30.0)`
2. In `src/agents/ingestion/ws_client.py:178-196`: cap the computed backoff at `config.ws_reconnect_max_backoff_seconds`
3. In `src/agents/ingestion/ws_client.py`: after successful reconnect, log `ws_client.reconnected` with `downtime_s` and reset `consecutive_failures` counter

**Files.**
- `src/core/config.py` — new field `ws_reconnect_max_backoff_seconds`
- `src/agents/ingestion/ws_client.py:132` — backoff cap application
- `src/agents/ingestion/ws_client.py:~210` — reconnect success logging

**Code sketch:**
```python
# In reconnect logic:
backoff_s = min(
    config.ws_reconnect_initial_backoff_seconds * (2 ** (consecutive_failures - 1)),
    config.ws_reconnect_max_backoff_seconds,
)

# After successful reconnect:
await logger.ainfo(
    "ws_client.reconnected",
    downtime_s=downtime,
    consecutive_failures=consecutive_failures,
)
consecutive_failures = 0  # reset counter
```

**Tests.** Test that backoff caps at configured max. Test that consecutive_failures resets on successful reconnect. Test that `ws_client.reconnected` log is emitted.

**Risk.** Low. Capping backoff is conservative (limits worst-case, doesn't affect normal operation). The `ws_client.reconnected` log is additive.

**Validation.** Next dry-run should show `ws_client.reconnected` lines after any disconnect. If a disconnect occurs, backoff should not exceed 30s.

---

### F4 — Snapshot persistence throttle (per-market minimum interval)

**Severity:** LOW (functional correctness not impacted; DB size is a long-term concern)
**MAAP:** Yes (touches `src/agents/ingestion/ws_client.py` or snapshot persistence path)
**Blast radius:** Snapshot persistence only. Evaluation pipeline uses the latest snapshot from the queue, not persisted rows.

**Why.** The DB grows at ~650 snapshots/min (10.8/sec) in a 3-market configuration. At 100 activated markets, this extrapolates to ~350 snapshots/sec = ~21,000/min = 30M rows/day. The `market_snapshots` table is the dominant consumer of disk space at 393 MB after cumulative sessions. A simple per-market minimum persistence interval (e.g., 2s) reduces write volume by 10-20× while preserving trading-relevant resolution.

**What.**
1. In `src/core/config.py`: add `snapshot_persist_min_interval_seconds: float = Field(default=2.0)`
2. In the snapshot persistence path (where `market_snapshot_inserted` is logged): maintain a per-condition-id `last_persist_time` dict. Skip persistence if time since last persist < configured interval.

**Files.**
- `src/core/config.py` — new field `snapshot_persist_min_interval_seconds`
- `src/agents/ingestion/ws_client.py` or `src/db/repositories/market_repository.py` — throttle logic

**Code sketch:**
```python
# Per-condition throttle dict
_last_persist: dict[str, float] = {}

async def _should_persist(condition_id: str, now: float, min_interval: float) -> bool:
    last = _last_persist.get(condition_id, 0.0)
    if now - last >= min_interval:
        _last_persist[condition_id] = now
        return True
    return False
```

**Tests.** Test that two snapshots for the same condition within the interval result in only one persist. Test that a snapshot after the interval is persisted.

**Risk.** Low. The evaluation pipeline uses in-memory snapshots from the bounded queue, not persisted rows. The throttle only affects the DB write path. If the throttle is too aggressive, the DB `market_snapshots` table will have sparser time-series data — acceptable for a paper-trading observation DB.

**Validation.** Next dry-run should show `market_snapshots` rows growing at ≤300/min (50% reduction minimum). DB file size growth should slow proportionally.

---

### F5 — Per-market Grok call rate limiter (anti-429)

**Severity:** LOW (self-healed in this session, but indicates xAI quota near limit)
**MAAP:** Yes (touches `src/agents/evaluation/grok_client.py` or `claude_client.py`)
**Blast radius:** Grok sentiment path only. Missed calls default to NEUTRAL_SENTIMENT.

**Why.** 3 HTTP 429s from xAI at session startup, despite only 1 Grok-eligible market active. If more markets are activated (F1), the startup burst will be proportionally larger, potentially causing sustained rate limiting.

**What.**
1. In `src/core/config.py`: add `grok_max_calls_per_minute: int = Field(default=20, ge=1)`
2. In the Grok call path (`claude_client.py` or `grok_client.py`): maintain a sliding-window counter of Grok calls. If calls in the last 60s exceed the limit, skip Grok for this snapshot and return `FALLBACK reason=GROK_RATE_LIMITED`.

**Files.**
- `src/core/config.py` — new field `grok_max_calls_per_minute`
- `src/agents/evaluation/grok_client.py:337-365` — call path with rate limit check

**Code sketch (grok_client.py):**
```python
from collections import deque
from time import monotonic

_grok_call_timestamps: deque[float] = deque()

def _grok_rate_limited(max_per_min: int) -> bool:
    now = monotonic()
    cutoff = now - 60.0
    while _grok_call_timestamps and _grok_call_timestamps[0] < cutoff:
        _grok_call_timestamps.popleft()
    if len(_grok_call_timestamps) >= max_per_min:
        return True
    _grok_call_timestamps.append(now)
    return False
```

**Tests.** Test that calls above the limit are skipped. Test that calls below the limit pass. Test that the sliding window releases slots.

**Risk.** Low. Fallback path is well-tested. NEUTRAL_SENTIMENT is the safe default. The rate limit is configurable and can be adjusted per environment.

**Validation.** Next dry-run startup should show 0 `grok_sentiment_http_error status_code=429`. If the rate limiter engages, `grok_sentiment status=FALLBACK reason=GROK_RATE_LIMITED` should appear instead.

---

## 5. Execution Sequence

Commits are atomic, dependency-ordered, and MAAP-flagged.

| # | Fix | Files | MAAP | Depends on |
|---|---|---|---|---|
| C1 | F1 (preflight config + spread report) | `.env`, `market_discovery.py` | Yes | None |
| C2 | F2 (EV arithmetic validation) | `claude_client.py`, `llm.py` | Yes | None |
| C3 | F3 (WS backoff cap) | `ws_client.py`, `config.py` | Yes | None |
| C4 | F4 (snapshot persist throttle) | `ws_client.py` or `market_repository.py`, `config.py` | Yes | None |
| C5 | F5 (Grok rate limiter) | `grok_client.py`, `config.py` | Yes | None |

**Commit order rationale:**
- C1 first because it unlocks the observation space needed to validate all other fixes.
- C2-C5 are independent of each other and can be sequenced in any order.
- All commits touch different files (no merge conflicts).

**Per commit:**
1. Write/update test
2. Confirm test fails (red phase)
3. Implement fix
4. Confirm test passes + full suite green
5. `git add` + commit with conventional commit message
6. MAAP on C1-C5 (all touch `src/agents/`)

---

## 6. Test Strategy

### Per-commit tests
- **C1**: `test_preflight_spread_summary_emitted` — verifies spread summary log with ≥2 status buckets
- **C2**: `test_ev_arithmetic_valid` / `test_ev_arithmetic_invalid` — parametrized BUY/SELL/HOLD with known values
- **C3**: `test_ws_backoff_capped_at_max` / `test_ws_reconnect_resets_failures`
- **C4**: `test_snapshot_throttle_skips_within_interval` / `test_snapshot_persist_after_interval`
- **C5**: `test_grok_rate_limit_allows_below_limit` / `test_grok_rate_limit_blocks_above_limit`

### Cumulative
- Full test suite: `.venv/bin/python -m pytest --asyncio-mode=auto tests/`
- Coverage: `.venv/bin/python -m coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/python -m coverage report -m`
- Target: ≥80% (current baseline from `STATE.md`)

### Integration
- Launch orchestrator after all commits: `nohup .venv/bin/python -m src.orchestrator > logs/orchestrator-run.log 2>&1 &`
- Verify: all 5 fix log lines appear, 0 regressions

---

## 7. Post-Implementation Validation

| Metric | Target | Current (Run 4) | Validation Method |
|---|---|---|---|
| Markets activated | ≥15 | 3 | `orchestrator.market_activated` count at startup |
| Preflight spread summary | Emitted | Not present | `grep preflight_spread_summary` on log |
| EV arithmetic mismatches | Logged per primary eval | Not logged | `grep ev_arithmetic_mismatch` on log |
| WS disconnects / 60 min | ≤2 | 4 | `grep ws_client.disconnected` on log |
| WS max backoff | ≤30s | Unbounded | `grep reconnect_in` on disconnect lines |
| Snapshot persist rate | ≤300/min | ~654/min | `grep market_snapshot_inserted` count / minutes |
| Grok HTTP 429s | 0 | 3 | `grep grok_sentiment_http_error.*429` on log |
| Grok rate-limited (fallback) | ≤5 calls | N/A (no limiter) | `grep GROK_RATE_LIMITED` on log |
| DB growth / 60 min | ≤200 MB | 393 MB → ~30 MB new | `sqlite3` row count diff |
| Test coverage | ≥80% | Reported in `STATE.md` | `coverage report -m` |

---

## 8. Rollback Strategy

Each fix is independently revertible:

- **C1 (config-only facet)**: change `.env` back to `MARKET_DISCOVERY_MAX_PREFLIGHT_CANDIDATES=25, PREFLIGHT_MAX_SPREAD_PCT=0.99`. The spread summary log is additive — no side effects.
- **C2**: remove EV validation call from primary evaluation path. Gatekeeper path unchanged. Reflection path unchanged.
- **C3**: revert `ws_reconnect_max_backoff_seconds` to not applied (default None → no cap). WS reconnect logic unchanged.
- **C4**: set `snapshot_persist_min_interval_seconds=0` or very large value to effectively disable.
- **C5**: set `grok_max_calls_per_minute=999999` to effectively disable.

All rollbacks are config-only or single-code-line removals. No data migration required.

---

## 9. Open Questions for User Sign-Off

1. **Do you want to increase `max_preflight_candidates` to 75, or remove the cap entirely (0 = unlimited)?** I recommend 75 as a conservative first step; we can go higher in the next session.
2. **Do you want to set `PREFLIGHT_MAX_SPREAD_PCT=1.0` (effectively disabled), or keep a lower threshold (e.g., 0.98) that still filters some?** I recommend 1.0 for the next session to maximize market access and observe what markets are actually available.
3. **Do you want to test Claude Sonnet 4 as the primary model alongside the F2 EV validation?** This is not a code change (just `.env`), but would give us a data point on whether DeepSeek-chat is the right primary model.
4. **Should F4 (snapshot throttle) be implemented now, or deferred?** Snapshot growth at 654/min is a long-term concern but does not affect the current session's trading correctness. I recommend deferring if the operator prefers to minimize code changes.
5. **Should F5 (Grok rate limiter) be implemented now, or deferred?** The 3 HTTP 429s in this session were self-healing and caused minimal impact. With more markets (F1), rate limiting may become necessary, but we could also observe whether it actually escalates before writing code.

---

## 10. Timeline Estimate

| Phase | Work | Estimated Time |
|---|---|---|
| C1 | Config + spread summary | 30 min |
| C2 | EV validation | 40 min |
| C3 | WS backoff cap | 25 min |
| C4 | Snapshot throttle (if approved) | 30 min |
| C5 | Grok rate limiter (if approved) | 25 min |
| Integration test | Full suite + dry-run launch | 20 min |
| **Total (minimal: C1+C2+C3)** | | **~1.5 hours** |
| **Total (all 5 fixes)** | | **~2.5 hours** |

All estimates assume the implementation follows the code sketches above and tests are straightforward additions to existing test files.

---

## 11. What Could Go Wrong

1. **C1 (more markets active = more Grok calls).** Activating 15+ markets could cause sustained xAI 429s rather than the 3-burst seen with 1 eligible market. Mitigation: implement C5 (Grok rate limiter) alongside C1.
2. **C2 (EV validation false positives).** The EV formula may incorrectly reject valid evaluations if the LLM uses a different pricing model (e.g., risk-adjusted probability differ-ing from naive EV). Mitigation: the 0.02 tolerance is generous; decreasing it to 0.001 would make the validator stricter-only when arithmetic is definitively wrong.
3. **C3 (WS backoff cap could cause rapid reconnect cycling).** If the WS is unstable, capping backoff at 30s means more disconnect/reconnect cycles within the same window. Mitigation: the `consecutive_failures` counter still increments; we can add a cooldown (e.g., pause for 60s after 5 consecutive failures) as a follow-up.
4. **C4 (snapshot throttle could hide rapid price movements).** A 2s minimum interval means up to 2s of micro-tick data is lost. Mitigation: the throttle only affects persistence, not the evaluation queue. The bounded queue still receives every snapshot. Decisions are based on the latest in-memory snapshot, not persisted rows.
5. **C5 (Grok rate limiter state is in-process, not persistent).** If the orchestrator restarts, the rate limiter resets. Mitigation: acceptable — a restart means a fresh xAI quota window anyway.

---

## 12. Definition of Done

- [ ] All 3-5 fix commits are on `develop` with conventional commit messages
- [ ] Full test suite passes: `pytest --asyncio-mode=auto tests/` (≥2314 passed, 0 failed)
- [ ] Coverage ≥80%
- [ ] MAAP review completed for all commits touching `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`
- [ ] Dry-run launch succeeds: markets activated ≥15
- [ ] All new log lines visible in `logs/orchestrator-run.log`:
  - `preflight_spread_summary`
  - `ev_arithmetic_mismatch` (if DeepSeek primary still errs)
  - `ws_client.reconnected` (if WS instability continues)
  - `market_snapshot_inserted` rate reduced (if C4 implemented)
  - `GROK_RATE_LIMITED` or 0 `grok_sentiment_http_error 429` (if C5 implemented)
- [ ] Session summary appended to `03_Daily/2026-05-18.md`

---

## 13. Files-Touched Matrix

| File | Fixes | LOC est. | MAAP |
|---|---|---|---|
| `.env` | C1 | 2 lines changed | No |
| `src/core/config.py` | C3, C4, C5 | +4 fields (~12 lines) | No (config fields are declarative) |
| `src/agents/ingestion/market_discovery.py` | C1 | +15 lines (spread summary) | Yes |
| `src/agents/evaluation/claude_client.py` | C2 | +30 lines (EV validation) | Yes |
| `src/agents/ingestion/ws_client.py` | C3, C4 | +20 lines (backoff cap + throttle) | Yes |
| `src/agents/evaluation/grok_client.py` | C5 | +15 lines (rate limiter) | Yes |
| `src/schemas/llm.py` | C2 | +1 enum value | Yes |
| `src/db/repositories/market_repository.py` | C4 (optional) | +10 lines (throttle check) | Yes |
| `tests/unit/test_WI-53-*.py` | C1, C2, C4 | +40 lines (new tests) | No |
| `tests/unit/test_ws_client.py` or similar | C3 | +20 lines | No |
| `tests/unit/test_grok_client.py` or similar | C5 | +20 lines | No |
| **Total** | | **~182 lines** | 6 files MAAP-required |

All estimates are upper bounds. The implementation code sketches above suggest the actual changes are smaller.

---

## 14. Closing Note

This is a light fix plan relative to the 2026-05-17 plan (which had 8 fixes across 6 hours estimated). Most fixes here are calibration adjustments and thin instrumentation layers. The core system — budget guard, Grok client, reflection layer, Gatekeeper, event ledger, WS subscribe dedup, SQLite WAL — is working correctly. The primary action for the next session is to open the market-access aperture and observe what signals become available.
