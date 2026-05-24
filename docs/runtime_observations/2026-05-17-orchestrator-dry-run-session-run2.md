# Orchestrator Dry-Run Session Report — 2026-05-17 (Run 2)

**Author:** Claude Code (session observer)
**Date (UTC):** 2026-05-17 → 2026-05-18 (T+60 crossed midnight UTC)
**Branch:** `develop` at `dab61ce` (`fix(runtime): stabilize llm budget and grok sentiment`)
**Runtime under test:** `nohup .venv/bin/python -m src.orchestrator` (Python 3.14.2, `.venv`)
**Mode:** `DRY_RUN=true`, `LLM_PROVIDER=deepseek`, `GROK_LIVE_ENABLED=true`, `GROK_MODEL=grok-4.20-0309-non-reasoning`, `GROK_TIMEOUT_SECONDS=8.0`
**Session window:** 23:40:23 UTC → 00:40:51 UTC (T+60m28s observed; orchestrator left RUNNING)
**Scope:** Second-pass runtime observation of `poly-oracle-agent` after the runtime-stabilization commit landed. No mid-session hotfix applied this run. Companion to the canonical Run 1 report (`2026-05-17-orchestrator-dry-run-session.md`).

---

## 1. Executive Summary

The runtime-stabilization commit `dab61ce` **closed three of yesterday's four HIGH-severity findings** and one MEDIUM. Specifically, the live observation confirms:

1. **Primary / reflection LLM budgets are now independently counted** (yesterday's 4.1 HIGH — `llm_budget_blocked reason=hourly_call_limit_exhausted` at T+5:29 — did not fire in this 60-minute window; instead the new `per_market_hourly_limit_exhausted` reason owns throttling).
2. **Grok live calls are reliable** (yesterday's 4.2 HIGH — 100% timeout — measured 10/10 SUCCESS, 0 timeouts, 0 schema errors, 0 HTTP errors across the entire session).
3. **Grok is skipped cleanly when the primary LLM budget is exhausted** (yesterday's wasted-API finding from the fix plan — observed 335 clean `grok_sentiment status=SKIPPED reason=PRIMARY_BUDGET_EXHAUSTED` events, **zero** xAI HTTP 429s).
4. **Observability subsystems are enabled** (yesterday's 4.4 MEDIUM — 4 disabled — now only `circuit_breaker.disabled`; `operational_alerts.enabled`, `operational_event_ledger.enabled` fire at startup, `operational_events` table grew from 0 → 18,027 rows).

The HIGH-impact regression that **replaces** the budget-cap finding is structural and operational, not safety:

**[HIGH] The per-market hourly cap (`llm_market_hourly_call_limit=10`, `src/core/config.py:437`) drives the system into a "graveyard state" by T+7 min, in which the agent ingests ~1,400 snapshots/min, produces 0 evaluations for ~40 minutes, and emits ~68 budget-block log lines/min.** Eval count plateaued at 50 at T+15m and only crawled to 52 by T+60m — the rolling-hour reset did *not* materially release the cap in the observed window. Cooldown events (`llm_repeated_hold_threshold=5`) compound the lockout: 184 `COOLDOWN_BLOCK` events were persisted to the ledger, all in the first ~7 min.

**Net trading output: 0 APPROVED, 0 orders signed, 0 positions opened.** This is the *correct* fail-closed posture, but for the second consecutive day the bot did not produce a single executable decision in a 1-hour window. The cause has moved from "Grok is broken" + "global LLM cap" to "per-market gate is so tight it freezes the pipeline before any market accumulates enough evaluations to climb above the reflection-flag threshold."

Two secondary findings are new this run:

5. **[MEDIUM] `MARKET_REJECTED` ledger spam.** The `operational_events` table received 12,689 `MARKET_REJECTED` rows in 60 min (~215/min). Same 41 TTR-fail markets are re-rejected every market-discovery refresh cycle. The WI-56→60 observability stack now reads a ledger dominated by event noise.
6. **[MEDIUM] SQLite read contention surfaced under sustained write load.** Two `database is locked (5)` errors were thrown during attempted ad-hoc reads of `operational_events GROUP BY` while the orchestrator was ingesting snapshots. The application does not yet retry these, so any dashboard / digest / replay query on a busy DB risks intermittent failures.

This report documents the full T+60 timeline, the structural deltas from Run 1, and a Tier-1/2/3 recommendation set focused on **unfreezing the eval pipeline without weakening any safety gate**.

---

## 2. Session Timeline (UTC)

| Time | Event |
|---|---|
| 23:40:23 | Run launched (PID 35832). `nohup .venv/bin/python -m src.orchestrator > logs/orchestrator-run.log 2>&1 &` |
| 23:40:26 | Startup: only `circuit_breaker.disabled` reported (down from 4 disabled subsystems in Run 1) |
| 23:40:26 | `operational_alerts.enabled`, `operational_event_ledger.enabled`, `operational_event_bus.started queue_maxsize=1000` |
| 23:40:26 | Gamma fetch: 100 active markets, **59 eligible** (Run 1 was 54), 41 `ttr_fail`, 15 activated. WS subscribed to 30 tokens |
| 23:40:27 | `operational_alerts.dispatched alert_type=process_started severity=INFO` — the only operational alert of the session |
| 23:40:32 | First LLM call recorded (`deepseek-chat`, ~$0.0061, 1848 in / 555 out tokens) |
| 23:40:59 | First `Evaluation complete` (IRAN, HOLD, EV=-0.7) — T+0:36 |
| 23:41:11 | Eval 2 (CULTURE, HOLD, EV=0.0) |
| 23:41:34 | **First `grok_sentiment status=SUCCESS reason=RECEIVED`** ever in the session (BTC $150k market, sentiment=0.68, 0 retries) — T+1:11 |
| 23:41:42 | First non-zero EV: BTC $150k, EV=+0.36, HOLD (reflection flagged `narrative_anchoring`, `overconfidence_unsupported`) |
| 23:42:37 | Second Grok SUCCESS (Pahlavi/IRAN, sentiment=0.68) |
| 23:45:31 | First Grok PRIMARY_BUDGET_EXHAUSTED skip — T+5:08 (Codex hotfix firing correctly) |
| 23:45:42 | **T+5 snapshot.** 33 evals, 0 approved, 8 Grok SUCCESS, 0 errors, **0 budget blocks**, 0 tracebacks. DB 192 MB, log 11 MB. |
| 23:46:10 | First `Evaluation complete` with empty `reflection_flags=[]` (LLM-variance, not budget-driven; confirmed by counting only 4/52 across full session) |
| 23:47:44 | **First `llm_budget_blocked call_type=primary reason=per_market_hourly_limit_exhausted`** — T+7:21. From this point the per-market cap owns all throttling. |
| 23:47:50→55:42 | Sustained burst of `per_market_hourly_limit_exhausted` blocks; per-market gate is now the dominant log emitter (~68 lines/min) |
| 23:55:31 | **T+15 snapshot.** 50 evals (+17 in 10 min), 0 approved, 10 Grok SUCCESS, 416 per-market blocks, 0 tracebacks. DB 224 MB, log 32 MB. |
| 00:00–00:11 | "Graveyard state": eval count frozen at 50. Per-market blocks climb 416 → 1666. CPU drops 22.4% → 3.1%. |
| 00:11:10 | **T+30 snapshot.** Evals still 50. Per-market blocks 1666. `operational_events` 9027. **`sqlite3 ... database is locked (5)`** thrown on `GROUP BY operational_events` ad-hoc read. |
| 00:11–00:40 | Continued idle. Two more evals trickle through (50 → 52) — sliding-hour window releasing one slot per market individually, not in batch. |
| 00:23:00 | Dashboard launched separately (`.venv/bin/python -m streamlit run src/ui/dashboard.py`, PID 36118) at user request. Health: `ok`. |
| 00:40:51 | **T+60 final snapshot.** 52 evals, 0 approved, 10 Grok SUCCESS, **4089 per-market blocks**, 18027 operational_events, 0 tracebacks. DB 304 MB, log 128 MB. |
| 00:40+ | Orchestrator left RUNNING (per skill default). Filtered monitor stopped. |

---

## 3. Environment & Configuration

### Loaded `.env` (secrets redacted)

```
DRY_RUN=true
DATABASE_URL=sqlite+aiosqlite:////Users/d.s/.../poly-oracle-agent/data/poly_oracle.db
LLM_PROVIDER=deepseek
DEEPSEEK_MODEL=deepseek-chat            (from Codex 19:28 hotfix — was deepseek-v4-pro)
GROK_LIVE_ENABLED=True
GROK_MOCKED=False
GROK_MODEL=grok-4.20-0309-non-reasoning  (Run 1 hotfix, now persisted)
GROK_TIMEOUT_SECONDS=8.0                 (Run 1 hotfix, now persisted)
GROK_API_KEY=***
TELEGRAM_BOT_TOKEN=***
TELEGRAM_CHAT_ID=***
```

### Disabled subsystems at startup

Only **one** disabled in Run 2 (down from 4 in Run 1). Citations from log lines emitted by `src/orchestrator.py`:

| Subsystem | Status | Source |
|---|---|---|
| `enable_telegram_notifier` | (no `telegram.disabled` log) — assumed wired since `TELEGRAM_*` env present, but `telegram.dispatched` count = 0 (see Finding 4.4) | inferred |
| `enable_circuit_breaker` | **disabled** | `circuit_breaker.disabled` at 23:40:26.449 — same as Run 1 |
| `enable_operational_alerts` | enabled | `operational_alerts.enabled` at 23:40:26.450 — **delta from Run 1** |
| `enable_operational_event_ledger` | enabled | `operational_event_ledger.enabled` at 23:40:26.450 — **delta from Run 1** |

### Active runtime knobs that drove behavior

| Setting | Value | File:line |
|---|---|---|
| `llm_hourly_call_limit` (primary) | 60 (Codex split active) | `src/core/config.py` |
| `llm_reflection_hourly_call_limit` | (Codex-added, default unknown without reading config) | `src/core/config.py` |
| `llm_market_hourly_call_limit` | **10** | `src/core/config.py:437` — **dominant throttle this session** |
| `llm_repeated_hold_threshold` | 5 | `src/core/config.py:442` — drives `COOLDOWN_BLOCK` events |
| `min_confidence` (Gatekeeper) | 0.75 | `src/core/config.py:99` (Run 1 reference) |
| `grok_timeout_seconds` | 8.0 | `.env` (persisted from Run 1 hotfix) |
| `grok_max_retries` | 2 | `src/core/config.py` |
| `per_market_hourly_call_limit` (schema field) | default 0 in schema, overridden by config | `src/schemas/llm.py:410` |

### Markets activated

- 100 fetched from Gamma → **59 passed preflight** (41 failed `ttr_fail` = time-to-resolution preflight; Run 1 was 54/46)
- 15 activated, all with `category=None` in the `orchestrator.market_activated` log (Run 1's Finding 4.6 — still not fixed)
- Subscribed to 30 WS tokens (15 markets × 2 YES/NO)
- `ws_subscribe_summary` emitted **372** times in 60 min (~6/min) — see Finding 4.6 for the rotation analysis

---

## 4. Findings (Ranked by Severity)

### 4.1 [HIGH] Per-market hourly cap freezes the eval pipeline within 7 minutes; no recovery in 1-hour window

**Symptom.** At T+7:21 the first `llm_budget_blocked call_type=primary reason=per_market_hourly_limit_exhausted` fires. By T+15 the system has produced 50 evals and emitted 416 per-market blocks. By T+60 it has produced **52** evals (only +2 in 45 min) and **4,089** per-market blocks. CPU drops from 22.4% to 3.1%; the agent is functionally idle.

**Root cause.** `LLMBudgetGuard.check` at `src/agents/evaluation/llm_cost_guard.py:184-198` reads `self._config.llm_market_hourly_call_limit` (default **10**, `src/core/config.py:437`). With 15 active markets and a natural cadence where high-volatility categories (CULTURE, IRAN, CRYPTO) push 3-5 snapshots/minute per market into the bounded queue, each market hits its 10-call/hour ceiling within ~5-7 minutes.

Once a market is per-market-blocked, the bounded queue at `src/agents/context/bounded_queue.py` continues to coalesce its incoming snapshots (the system has no upstream awareness of the downstream gate). Each coalesced snapshot still wakes the consumer, which calls `LLMBudgetGuard.check`, which logs **two lines** — one metric (`llm_budget_blocked call_type=primary reason=…`) and one narrative (`llm_budget_blocked — skipping primary provider call. attempt=1 …`). Result: 4,089 log lines, no work done.

The rolling-hour window *does* technically release slots one-by-one as old timestamps fall off, which explains the trickle of evals 50→52 between T+30 and T+60. But it does not release in batch at any fixed T+60 boundary because the per-market counter is a sliding window keyed off `time.monotonic()` (`llm_cost_guard.py:72-73`), not a wall-clock hour-boundary reset.

**Why it matters.** The agent loses ~88% of its potential evaluation budget within the first 7 minutes. The dashboard, daily digest, and incident-replay tools all read a ledger that records "the bot did 50 things and then thought about nothing for 53 minutes." There is no path to surfacing a profitable signal in this state, regardless of how good the model is.

**Recommended fix.** See Tier-1 in Section 9: either (a) raise `llm_market_hourly_call_limit` to a value consistent with the desired throughput (e.g. 30, giving a ceiling of 450 evals/hr across 15 markets), or (b) — better — add backpressure at the queue layer: when a market trips the per-market cap, pause its snapshot ingestion for the remainder of the window instead of letting it drain through the queue and trigger guard rejections.

---

### 4.2 [HIGH] Per-market budget-block log spam — 68 lines/min, 128 MB log in 60 min

**Symptom.** Log file grew from 11 MB at T+5 to **128 MB at T+60**. After T+7:21 the dominant log driver is paired emissions of `llm_budget_blocked call_type=primary reason=per_market_hourly_limit_exhausted` + `llm_budget_blocked — skipping primary provider call. attempt=1 snapshot_id=…`. Two log lines per blocked snapshot. At ~34 blocked snapshots/min → 68 lines/min → ~125 MB across 53 minutes of throttled steady-state.

**Root cause.** `LLMBudgetGuard._block` (`src/agents/evaluation/llm_cost_guard.py:186-198`) does no de-duplication: every blocked call emits the structured metric line, and the caller in `claude_client.py` emits a second narrative line. Per-market budget exhaustion is a *steady state*, not a transient — but it logs as if every occurrence were novel.

**Why it matters.** Three downstream consequences:
1. Disk usage: 60 min produces 128 MB of mostly-redundant text. A 24-hour soak test would produce ~3 GB.
2. Signal-to-noise: when a real anomaly happens (HTTP error, traceback, WS disconnect), it is buried in budget-block lines. The Run 2 monitor had to be restarted twice with progressively tighter filters because budget-block events drowned signal.
3. Ledger growth: `BUDGET_BLOCK` events are also persisted to `operational_events` (3,648 rows, ~3.5/sec sustained), inflating the table that WI-58/59/60 read.

**Recommended fix.** See Tier-1 in Section 9. De-duplicate budget-block emission: log + persist once per market per window-rollover, summarize subsequent attempts in a counter.

---

### 4.3 [MEDIUM] `MARKET_REJECTED` ledger spam — 12,689 events in 60 min

**Symptom.** `operational_events` table received 12,689 `MARKET_REJECTED` rows in 60 min (sqlite query confirmed: `MIN(created_at_utc)=23:40:26, MAX=00:39:23, COUNT=12689` ≈ 215/min). All from the same 41 TTR-fail markets re-rejected on every market-discovery refresh cycle.

**Root cause.** `market_discovery.eligible_markets_found` fires twice in the first 10 seconds (logs show `23:40:26.757` and `23:40:36.895`), then on a recurring schedule. Each cycle emits a `MARKET_REJECTED` event for every market that fails preflight, *regardless of whether it failed last cycle for the same reason*. The event ledger is being treated as a per-cycle audit log rather than a state-transition log.

**Why it matters.** WI-56 was designed as an *event* ledger — state transitions and operational decisions, not periodic re-emission of stable rejections. WI-58 (incident replay) and WI-59 (dashboard activity feed) and WI-60 (daily digest) all read this table; their UX degrades when 70% of rows are duplicate rejections of the same 41 markets. The daily digest threshold logic and replay severity filters will become harder to tune.

**Recommended fix.** Emit `MARKET_REJECTED` only on state transitions: first rejection of a previously-unknown market, OR transition from accepted → rejected, OR rejection-reason change. Persisting `MARKET_ELIGIBILITY_CYCLE_COMPLETED` with cumulative counts once per cycle is cheaper, more queryable, and matches WI-56's stated intent.

---

### 4.4 [MEDIUM] Telegram dispatch is silent despite operational-alerts being enabled

**Symptom.** `operational_alerts.dispatched` count = **1** (the `process_started` alert at 23:40:27). `telegram.dispatched` count = **0**. `telegram.failed` count = **0**. Across 60 minutes with `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` both set in `.env` and the operational-alerts bridge enabled at startup.

**Root cause hypothesis (not confirmed in this session, marked as such).** Three plausible paths:
1. **Most likely:** `ENABLE_TELEGRAM_NOTIFIER` is not set in `.env` (was not in our grep), so the notifier itself is off even though the alerts bridge is on. Bridge dispatches alerts to *something*, but the Telegram sink is the no-op.
2. The alerts bridge only routes specific severity classes (e.g. WARNING+) and the only alert this session was `severity=INFO`.
3. `enable_startup_alert` may be a separate flag (Run 1's fix plan F1 listed both `ENABLE_TELEGRAM_NOTIFIER=true` and `ENABLE_STARTUP_ALERT=true`).

**Why it matters.** The dashboard activity feed gets its real-time updates from this bridge. The daily digest's "alerts fired today" section will be empty. An operator monitoring the bot via Telegram (the actual deployment pattern per Phase 16 PRD) has no signal. Yesterday's fix plan F1 explicitly anticipated this — the *partial* observability enablement in `dab61ce` may have missed the Telegram sink itself.

**Recommended fix.** Diagnostic-first: verify which env flags resolve to `True` at startup via the `CONFIG_LOADED` operational event payload. If `enable_telegram_notifier=False`, set it in `.env` and restart. No code change required for this finding alone, but the discovery should be documented in `docs/runbooks/llm-cost-guard.md` (or a new observability runbook).

---

### 4.5 [MEDIUM] SQLite read contention under sustained write load

**Symptom.** Two `sqlite3 prepare error: database is locked (5)` errors thrown when running `SELECT event_type, COUNT(*) FROM operational_events GROUP BY event_type` from a separate `sqlite3` CLI process while the orchestrator was actively writing snapshots and events. Retry after ~2s succeeded. Errors occurred at T+30 and T+60 — both during stats-snapshot capture.

**Root cause.** SQLite's default journal mode + concurrent writer-during-read pattern. The orchestrator opens an async aiosqlite connection (`sqlite+aiosqlite:////…`) and writes continuously (snapshots ~1.4k/min + events ~5/sec = ~24 writes/sec). External read connections compete for the same single-writer lock. Default `busy_timeout` is probably too short for the concurrent-writer case.

**Why it matters.** Three downstream consequences:
1. The Streamlit dashboard (PID 36118 in this session, served at `http://localhost:8501`) opens read transactions through the WI-59 `dashboard_activity_feed` cache (`@st.cache_data(ttl=30)`). At 30-second cache misses with the orchestrator under heavy write load, the dashboard's panel reads will intermittently fail.
2. The incident-replay CLI (WI-58) runs ad-hoc filtered queries — same lock contention path.
3. The daily-digest generator (WI-60) reads `operational_events` and `positions` together — if a digest run lands during an ingestion burst, the digest generation could fail with the same error.

**Recommended fix.** Enable WAL mode at connection setup (`PRAGMA journal_mode=WAL`) and raise `busy_timeout` to ~5000ms. WAL is the standard mitigation for SQLite concurrent-writer-vs-reader scenarios and is fully compatible with `aiosqlite`. This is a connection-string / startup-pragma change, not a schema migration.

---

### 4.6 [LOW] WebSocket re-subscription emits 372 `ws_subscribe_summary` lines in 60 min

**Symptom.** `ws_subscribe_summary` log count went from 33 at T+5 to **372** at T+60. Yesterday's Run 1 had approximately ~10 across an 18-min window; this session is ~6/min sustained. Distribution is even — not a startup burst.

**Root cause hypothesis (not fully confirmed).** Codex's WI-53 / Run 1 hotfix landed a `CLOBWebSocketClient` change to "add subscription diffing with unsubscribe/subscribe updates so market rotation does not keep stale server-side assets subscribed" (per yesterday's daily note at 02:46). If the diffing logic re-emits a subscription summary on every market-rotation cycle (even when the diff is empty), this explains the 6/min rate.

**Why it matters.** Low — no functional impact. The WS connection itself was healthy (0 `ws_client.disconnected`, 0 `crossed_book`, 0 stale frames). But the 372 summary lines add noise to log analysis and slightly inflate operational ledger volume if the path also persists.

**Recommended fix.** Investigate `src/agents/ingestion/ws_client.py` subscription-diff path. If the summary is emitted unconditionally, gate it behind "diff non-empty OR rotation occurred." Low priority — defer until the per-market budget issue is fixed.

---

### 4.7 [LOW] `orchestrator.market_activated category=None` still present (Run 1 Finding 4.6 unresolved)

**Symptom.** Every one of the 15 `orchestrator.market_activated` lines in this session carries `category=None`, identical to Run 1. The `market_category_resolved` log line (added in yesterday's Grok-eligibility hotfix) shows the category being resolved correctly later (e.g. `CULTURE`, `IRAN`, `CRYPTO`), but the activation-time log still emits `None`.

**Root cause.** Unchanged from Run 1 Finding 4.6. `MarketMetadata.category` is resolved in a separate code path (`src/agents/evaluation/claude_client.py` per the hotfix) than the activation logger in `src/orchestrator.py`. The two are not connected at activation time.

**Why it matters.** Low — purely a log-readability issue. But it makes log analysis harder (cannot grep `orchestrator.market_activated category=CRYPTO`), and the `category=None` value is misleading.

**Recommended fix.** Same as Run 1 Finding 4.6 (see canonical fix plan F7). Not re-prioritized.

---

### 4.8 [LOW] Empty `reflection_flags=[]` on 4/52 evals is LLM variance, not budget cutoff

**Symptom.** 4 of 52 evals (7.7%) emitted `reflection_flags=[]` instead of the typical 2-4 flag list. Initially suspected the reflection-budget cap was firing per Codex's split, but ledger shows `reflection_hourly_limit_exhausted=0` for the full session.

**Root cause.** Genuine LLM-output variance — DeepSeek occasionally returns a reflection response with no flags raised. Not a defect.

**Why it matters.** None — informational only. Noted here to correct the in-session hypothesis (recorded at ~23:46:38 in the monitor stream).

---

## 5. Mid-Session Hotfix Applied

**None.** No mid-session code or config change was applied in Run 2. The hotfix values from Run 1 (`.env`'s `GROK_MODEL` and `GROK_TIMEOUT_SECONDS`, `grok_client.py` neutral fallback string) were already persisted at HEAD. The Codex stabilization commit `dab61ce` was the runtime under test from T+0.

---

## 6. Numerical Summary

### Run 2 (single run): 23:40:23 UTC → 00:40:51 UTC (60m 28s observed, orchestrator left running)

| Metric | T+5 | T+15 | T+30 | T+60 |
|---|---|---|---|---|
| Evaluations | 33 | 50 | 50 | **52** |
| Approved | 0 | 0 | 0 | **0** |
| Action distribution | 100% HOLD | 100% HOLD | 100% HOLD | 100% HOLD |
| Categories | CULTURE 25 / IRAN 5 / CRYPTO 3 | CULTURE 40 / IRAN 5 / CRYPTO 5 | CULTURE 40 / IRAN 5 / CRYPTO 5 | CULTURE 47 / IRAN 5 / CRYPTO 5 |
| Non-zero EVs | -0.7×5, +0.36×3 | -0.7×5, +0.36×5 | -0.7×5, +0.36×5 | -0.7×5, +0.36×5 |
| Grok SUCCESS | 8 | 10 | 10 | 10 |
| Grok SKIPPED budget | 0 | 54 | 154 | 335 |
| Grok timeout / schema / http | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |
| Per-market budget blocks | 0 | 416 | 1,666 | 4,089 |
| Reflection budget blocks | 0 | 0 | 0 | 0 |
| Global hourly blocks | 0 | 0 | 0 | 0 |
| Cooldown events | n/a | n/a | 194 | 194 |
| Tracebacks / ERRORs / WS issues | 0 | 0 | 0 | 0 |
| `ws_subscribe_summary` | 33 | n/m | n/m | 372 |
| Telegram emits | 0 | 0 | 0 | 0 |
| `operational_alerts.dispatched` | 1 | 1 | 1 | 1 |
| CPU% | 5.6 | 22.4 | 3.1 | 16.6 |
| RSS (MB) | 132 | 158 | 143 | 153 |
| Log size | 11 MB | 32 MB | 62 MB | **128 MB** |

### Database (cumulative, includes prior session state)

| Table | Pre-run | T+60 | Delta |
|---|---|---|---|
| `market_snapshots` | 114,545 | 192,552 | +78,007 (~1,300/min) |
| `agent_decision_logs` | 154 | 212 | +58 (matches eval count ±, plus prior-session settle) |
| `execution_txs` | 0 | 0 | 0 |
| `positions` | 0 | 0 | 0 |
| `operational_events` | 0 | **18,027** | +18,027 (ledger live, dominated by MARKET_REJECTED at 12,689) |
| DB file size | 189 MB | 304 MB | +115 MB |

### `operational_events` distribution (top 10 by count)

| Event type | Rows |
|---|---|
| `MARKET_REJECTED` | 12,631 (70%) |
| `BUDGET_BLOCK` | 3,648 (20%) |
| `LLM_CALL_STARTED` | 1,645 |
| `COOLDOWN_BLOCK` | 184 |
| `DECISION_SKIPPED` | 50 |
| `MARKET_DISCOVERED` | 15 |
| `CONFIG_LOADED` | 1 |
| `READY_STATE_CHANGED` | 1 |
| `START` | 1 |
| `WS_CONNECTED` | 1 |

The top two row types are **operational noise** (Findings 4.2 and 4.3).

---

## 7. Points of View — What I Think Is Going On

**The Codex stabilization commit `dab61ce` shipped exactly the fixes the Run 1 fix plan called for, and they all work.** Grok is rock-solid. The primary/reflection budget split prevents the 5-minute global-cap exhaustion. Grok cleanly skips when the primary is out of budget. The observability subsystems are live. None of yesterday's HIGH findings reproduced.

**The bot's failure mode has cleanly moved one layer down.** Yesterday: "Grok is broken and the global cap is too tight, so the bot can't produce reliable evals at all." Today: "Grok is reliable, the global cap is right-sized, but the *per-market* cap is now too tight for the bot's natural snapshot cadence, so most markets pin themselves shut within 5-7 minutes and stay shut for the rest of the rolling hour."

**This is a tuning problem, not an architecture problem.** The per-market gate (`llm_market_hourly_call_limit=10`) was almost certainly sized for a future world where:
- Snapshots arrive at < 1/min per market (today they arrive at 3-5/min for CULTURE/IRAN markets);
- And/or there is upstream backpressure that limits how often a market enqueues (today the bounded queue coalesces but does not throttle per-market);
- And/or reflection-pass cost was the dominant LLM expense (today it is, but reflection is now on its own counter).

**The "graveyard state" is the new normal under current defaults.** Once it sets in, the bot looks alive (PID, RSS, ingestion) but is operationally dead until the rolling-hour window releases slots one at a time. Between T+30 and T+60 the bot produced 2 evaluations and 1,500 budget-block log lines.

**The MARKET_REJECTED spam is the most interesting "free win."** It is a one-line behavioural change (state-transition gating) that would shrink the operational ledger by 70%, improve dashboard signal-to-noise, and reduce DB growth — all without changing any safety semantics or threshold.

**SQLite WAL is overdue.** With WI-56 now writing 5 events/sec and the dashboard reading on a 30-sec cache, every dashboard refresh races a snapshot write. WAL plus a `busy_timeout=5000` will eliminate the lock errors I caught in this session — and protect against the more dangerous variant where the dashboard or daily-digest job catches the lock at a worse moment.

---

## 8. Recommendations (Prioritized)

### Tier 1 — Before next dry-run

| ID | Fix | Severity addressed |
|---|---|---|
| R1 | Raise `llm_market_hourly_call_limit` from 10 to 30 in `src/core/config.py:437`, OR add `LLM_MARKET_HOURLY_CALL_LIMIT` to `.env` with value 30 (config-only, no code). Re-validate after T+15 / T+30 / T+60 snapshots. | 4.1 HIGH |
| R2 | De-duplicate per-market budget-block log emission in `LLMBudgetGuard._block` (`src/agents/evaluation/llm_cost_guard.py:186-198`). Emit one structured warning per (market, reason) per minute; emit a counter on rollover. | 4.2 HIGH |
| R3 | Audit `enable_telegram_notifier` resolution. If `False`, add to `.env`. Verify `process_started` Telegram delivery on next restart. | 4.4 MEDIUM |

### Tier 2 — Material improvement

| ID | Fix | Severity addressed |
|---|---|---|
| R4 | Suppress repeat-rejection emission in market discovery: only persist `MARKET_REJECTED` on state transitions (first-rejection, accepted→rejected, reason-change). Add periodic `MARKET_ELIGIBILITY_CYCLE_COMPLETED` summary event. | 4.3 MEDIUM |
| R5 | Enable SQLite WAL + `busy_timeout=5000` at orchestrator startup connection-init. Coordinate with Alembic env so migrations still work. | 4.5 MEDIUM |
| R6 | Add upstream backpressure: when a market trips the per-market cap, mark it `QUARANTINED_BUDGET` in `MarketQuarantineManager` until the window resets, instead of letting the bounded queue keep enqueueing snapshots that the LLM guard rejects. | 4.1 / 4.2 (root cause) |

### Tier 3 — Longer-horizon

| ID | Fix | Severity addressed |
|---|---|---|
| R7 | Resolve `MarketMetadata.category` at activation time so `orchestrator.market_activated` logs the real category, not `None`. (Run 1 F7, unresolved.) | 4.7 LOW |
| R8 | Gate `ws_subscribe_summary` log on diff-non-empty in `src/agents/ingestion/ws_client.py`. | 4.6 LOW |
| R9 | Replace SQLite with Postgres for the operational ledger only (snapshots can stay on SQLite) once ledger volume warrants. Out of scope for fix plan; capture as separate WI. | 4.5 (long-term) |
| R10 | "Shadow Gatekeeper" — log what would have been APPROVED at `min_confidence=0.50` even when the live Gatekeeper holds at 0.75. (Same recommendation as Run 1 Section 8 Tier 3 — still unimplemented and still useful for calibration.) | informational |

---

## 9. Open Questions / Ideas Not Pursued

1. **What is the right per-market cap?** 10/hour is too tight; 30/hour is a guess. Need to model: at the bot's natural cadence and the reflection-flag pattern, what is the smallest cap that does not produce graveyard state? Should it vary by category (CRYPTO and IRAN markets seem to enqueue 3-5×/min, CULTURE often higher)?
2. **Should the per-market cap be a *cooldown* instead of a *counter*?** A market that produces 10 evaluations all HOLD with the same reflection-flag set is providing zero new signal per evaluation. Could collapse the per-market cap and the `llm_repeated_hold_threshold` into a single "this market is uninformative, back off for N minutes" policy.
3. **`agent_decision_logs` only has 212 rows vs 18,027 operational events.** Confirms most operational events are noise (rejections, budget blocks) not decisions. Worth a separate audit of what should and should not be persisted.
4. **Why is `ws_subscribe_summary` firing 6×/min in Run 2 when Run 1 saw ~0.5×/min?** Probably the Codex WS hotfix changed the emission gate, not the subscription rotation itself. Quick log diff would confirm.
5. **Telegram silent — is the bridge configured correctly or is `enable_telegram_notifier` defaulted off?** Diagnostic-first; do not change code until the actual config state is known.
6. **The DB grew 115 MB in 60 min entirely on the ingestion path while doing zero useful work.** Worth deciding the snapshot-retention policy *before* the next soak test, not after.
7. **Would a 30-min run on the next attempt give the same data?** With Tier-1 R1+R2 applied, probably yes — and would halve disk/log cost.
8. **Is `LLM_REFLECTION_HOURLY_CALL_LIMIT` set to a sensible default in `dab61ce`?** I did not read that part of `config.py` this session. Should verify before tuning anything else.

---

## 10. Files Modified This Session

**Source code:** none.
**Config:** none.
**Generated artefacts:**

- `logs/orchestrator-run.log` (128 MB, current session, orchestrator still writing)
- `logs/stats-snapshot-T5min.txt`, `…-T15min.txt`, `…-T30min.txt`, `…-T60min.txt` (snapshots persisted)
- `logs/dashboard.log` (Streamlit, healthy)
- `docs/runtime_observations/2026-05-17-orchestrator-dry-run-session-run2.md` (this file)
- `docs/runtime_observations/2026-05-17-orchestrator-fix-plan-run2.md` (companion)
- `03_Daily/2026-05-17.md` (session-end summary, appended)

---

## 11. Process Notes for the Next Operator

1. **Default filter for `Monitor` must exclude `per_market_hourly_limit_exhausted`.** It is the dominant log emitter under current config and will drown all other signal within 8 minutes. I had to re-arm the monitor twice with progressively tighter filters; the canonical filter in `.claude/commands/dry-run-review.md` needs an update.
2. **The Streamlit dashboard CLI is not installed in `.venv/bin`** on this machine — `streamlit` is importable as a module but not on PATH. Launch via `.venv/bin/python -m streamlit run src/ui/dashboard.py`. Worth noting in the README.
3. **The orchestrator is still RUNNING at the time of this report** (PID 35832, ~1h 1m uptime, RSS 153 MB). Stop it explicitly with SIGTERM when ready; do not let it accumulate another 5 GB of log in the background.
4. **Run 1's `2026-05-17-orchestrator-dry-run-session.md` and `2026-05-17-orchestrator-fix-plan.md` should be considered superseded by this report** for the post-Codex-stabilization state. Yesterday's fix plan F2 (budget split), F3 (Grok narrative recovery), F4 (Grok skip on budget) are all implemented in `dab61ce`. F1 (observability flags) is partially implemented — Telegram is still silent. F5 (snapshot persistence throttle), F6 (de-dup market activation logs), F7 (resolve category), F8 (demote noisy events), F9 (log rotation) are still open.
5. **The "graveyard state" finding was only catchable by waiting past T+15.** A 15-minute review window would have shown "system is healthy" and missed the structural throughput cap entirely. Do not shorten the default review window below 30 minutes.

---

## 12. Closing

The runtime is materially safer and more reliable than 24 hours ago. The hard work of yesterday's plan paid off. The remaining structural issue (per-market cap freezing the pipeline) is well-localized, has a clear fix, and does not require touching any safety gate, Decimal path, or Gatekeeper logic. Tier-1 R1 alone (one config integer) is likely sufficient to demonstrate non-graveyard behavior on the next dry-run; R2 makes the next observation pass actually readable.

**Net trading output: 0 APPROVED, 0 orders, 0 positions, 0 safety violations.** Correct posture. The fix plan addresses why.
