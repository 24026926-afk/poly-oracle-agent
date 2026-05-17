# Orchestrator Dry-Run Session Report — 2026-05-17

**Author:** Claude Code (session observer)
**Date (UTC):** 2026-05-17
**Branch:** `develop` (working tree had uncommitted Grok-eligibility hotfix on entry)
**Runtime under test:** `python3 -m src.orchestrator` (Python 3.14, `.venv`)
**Mode:** `DRY_RUN=true`, `LLM_PROVIDER=deepseek`, `GROK_LIVE_ENABLED=true`
**Session window:** 20:50:33 UTC → ongoing (≥ 24 min observed)
**Scope:** End-to-end runtime observation of the autonomous Polymarket trading agent in paper-trading mode. One mid-session hotfix applied (Grok latency / model selection).

---

## 1. Executive Summary

The bot is **functionally healthy**, **financially safe** (DRY_RUN enforced, 0 live orders), and **stable** (0 uncaught exceptions across two consecutive process lifecycles totalling ~24 min and ~178k log lines). However, **its dry-run signal pipeline is structurally constrained by three independent throttles** that compound to produce a near-uniform `HOLD` output even when real positive expected value (EV) is present.

The three structural constraints, in order of impact:

1. **The "60-call hourly cap" is actually a 30-evaluation cap.** `LLMBudgetGuard` counts *primary + reflection* against the same global window, so `LLM_HOURLY_CALL_LIMIT=60` corresponds to ≤ 30 full evaluations per hour. At the bot's natural cadence (~6–8 evaluations / minute), the cap is hit in ~5 minutes flat, after which the bot sits idle for ~55 minutes waiting for the sliding window to release slots. **This was observed twice in the same session, with identical timing.**
2. **Live Grok sentiment was 100% unusable before the fix.** The `grok_timeout_seconds=2.0` default cannot accommodate xAI's real latency on legacy model aliases (which silently re-route to `grok-4.3`, a reasoning model that spends 300–700 reasoning tokens on a 1-token answer). **Every single eligible-category sentiment call timed out in the first 5-minute window**, poisoning the LLM input with neutral fallbacks and producing `expected_value=0.0` for 30 / 30 evaluations.
3. **The reflection layer is doing its job — almost too well.** Once Grok was producing real sentiment, the reflection pass began correctly flagging `narrative_anchoring`, `overconfidence_unsupported`, and `ZERO_CONFIDENCE` on most evaluations. Combined with `min_confidence=0.75`, the Gatekeeper still enforced `HOLD` on the only two markets with non-trivial positive EV (BTC $150k @ EV+0.36, Pahlavi @ EV+0.10).

**Net trading output across the entire session: 0 APPROVED, 0 orders signed, 0 positions opened.** This is the *correct* fail-closed posture given the inputs, but it means the bot would not have traded even if `DRY_RUN` were `false`.

The mid-session Grok hotfix (model swap + 4× timeout bump) **converted a 0/30 success rate into a 22/118 SUCCESS / 6 schema-error / 90 SKIPPED-by-eligibility distribution** — a structural improvement that unlocked real reasoning, surfaced a new low-rate Pydantic schema defect, but did not by itself produce an APPROVED decision in the windows observed.

This report documents the full timeline, all findings, the hotfix application, and a prioritized list of follow-up changes I would make to convert this from a "safely idle" runtime into a runtime that can actually surface profitable signals.

---

## 2. Session Timeline (UTC)

| Time | Event |
|---|---|
| 20:50:33 | Run 1 launched (PID 25461). `.venv/bin/python -m src.orchestrator` |
| 20:50:33 | Startup: 4 disabled subsystems logged (telegram, circuit_breaker, operational_alerts, operational_event_ledger) |
| 20:50:33 | Gamma fetch: 100 active markets, 54 eligible, 15 activated. WS subscribed to 30 tokens |
| 20:50:50 | First LLM evaluation completes (DeepSeek, HOLD, EV=0.0) |
| 20:50–20:55 | 30 evaluations completed at ~6/min cadence. All HOLD, all EV=0.0 |
| 20:51:09 | First `grok_sentiment_timeout` observed (CRYPTO market, all 3 attempts time out within 2s window) |
| 20:55:56 | Last successful LLM call before budget cap |
| 20:56:02 | **First `llm_budget_blocked reason=hourly_call_limit_exhausted`** (T+5:29 from start) |
| 20:56–21:08 | Bot in steady-state idle. WS continues ingesting (~1,500 snapshots/min). 306 LLM blocks, 169 Grok timeouts accumulated |
| 21:01:17 | Status snapshot taken. `market_snapshots` table at 83,933 rows, DB 139 MB |
| 21:07:55 | User asks: keep monitoring or fix Grok? |
| 21:08:00 | Out-of-band diagnostic curl runs against `https://api.x.ai/v1`. Key valid, real latency = 2.85s (`grok-4-1-fast` alias) to 7.65s (`grok-3` alias). Both alias to `grok-4.3` reasoning model |
| 21:08:30 | `.env` edited (`GROK_MODEL=grok-4.20-0309-non-reasoning`, `GROK_TIMEOUT_SECONDS=8.0`), `grok_client.py` neutral fallback string updated, pre-fix log archived |
| 21:08:56 | Run 2 launched (PID 26469) |
| 21:09:04 | First eval (HOLD, EV=0.0 — CULTURE market, Grok correctly SKIPPED) |
| 21:09:24 | **First Grok `status=SUCCESS reason=RECEIVED`** ever in this session — IRAN/regime collapse, sentiment_score=0.12 |
| 21:09:32 | First non-zero EV evaluation ever: action=HOLD, **EV=-0.96**, market_category=IRAN |
| 21:10:19 | First positive EV: BTC $150k, sentiment=0.68, **EV=+0.36**, HOLD (reflection flagged anchoring) |
| 21:10:46 | First `grok_sentiment_schema_error`: response > 320 chars on IRAN narrative |
| 21:13:54 | **Second budget exhaustion** at exactly T+4:58 from restart — identical timing to first run |
| 21:14:48 | Status snapshot. 30 evals, 22 Grok SUCCESS, 6 schema_error, 90 SKIPPED_CATEGORY, 0 timeouts, 70 LLM blocks |
| 21:15+ | Steady-state idle, sliding budget window not yet rolling |

---

## 3. Environment & Configuration

### Loaded `.env` (secrets redacted)
```
ENVIRONMENT=development
LOG_LEVEL=INFO
DRY_RUN=true
DATABASE_URL=sqlite+aiosqlite:////Users/d.s/.../poly-oracle-agent/data/poly_oracle.db
LLM_PROVIDER=deepseek
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic
DEEPSEEK_MAX_TOKENS=4096
DEEPSEEK_MAX_RETRIES=2
GROK_LIVE_ENABLED=True
GROK_MOCKED=False
GROK_MODEL=grok-4.20-0309-non-reasoning      ← added mid-session
GROK_TIMEOUT_SECONDS=8.0                      ← added mid-session
CLAUDE_MODEL=claude-sonnet-4-20250514
TELEGRAM_BOT_TOKEN=<set>
TELEGRAM_CHAT_ID=<set>
POLYMARKET_API_KEY=dummy_key_for_dry_run_only
POLYMARKET_SECRET=dummy_secret_for_dry_run_only
POLYMARKET_PASSPHRASE=dummy_passphrase
POLYGON_RPC_URL=https://polygon-rpc.com
```

### Disabled subsystems at startup (`src/orchestrator.py:216, 222, 244, 267`)
All four require an explicit `ENABLE_*=true` in `.env`, none of which were set:

| Subsystem | Default | Effect of being off |
|---|---|---|
| `enable_telegram_notifier` | False | No Telegram delivery (despite token/chat being configured) |
| `enable_circuit_breaker` | False | No global halt on CRITICAL drawdown alerts |
| `enable_operational_alerts` | False | WI-50 operational alert bridge silent |
| `enable_operational_event_ledger` | False | **WI-56 event bus disabled — `operational_events` table stays at 0 rows. Dashboard timeline and daily digest will be empty.** |

### Active runtime knobs that drove behavior
| Setting | Value | File |
|---|---|---|
| `llm_hourly_call_limit` | 60 | `src/core/config.py:412` |
| `min_confidence` (Gatekeeper) | 0.75 | `src/core/config.py:99` |
| `grok_timeout_seconds` (initial) | 2.0 | `src/core/config.py:316` |
| `grok_max_retries` | 2 | `src/core/config.py:320` |
| `_CHAIN_BUDGET_DRY_RUN` | 60.0 s | `src/agents/evaluation/claude_client.py:58` |
| `_compute_grok_budget` cap | min(timeout × (retries+1), chain × 0.7) | `src/agents/evaluation/claude_client.py:61–74` |
| Bounded queue depth | 50 | `src/agents/context/bounded_queue.py` |
| Mock bankroll | 1000 USDC (no live wallet) | `bankroll_sync.mock_balance_returned` |

### Markets activated
- 100 fetched from Gamma → 54 passed preflight (46 failed `ttr_fail` = time-to-resolution preflight)
- 15 activated, all logged with `category=None` (see Finding 4.6)
- Subscribed to 30 WS tokens (15 markets × 2 YES/NO)
- Re-discovery loop running every ~10s, re-emitting `orchestrator.market_activated` for the same 15 markets

---

## 4. Findings (Ranked by Severity)

### 4.1 [HIGH] LLM hourly budget hits in 5 min, not 60 min

**Symptom.** Within 5:29 of process start, `llm_budget_blocked reason=hourly_call_limit_exhausted` fires and all subsequent primary calls are skipped. Bot is idle for ~55 minutes until the sliding window releases slots. Observed twice in this session with identical timing across two independent process lifecycles.

**Root cause.** `LLMBudgetGuard` increments a single counter on every registered LLM call. Both call sites count against it:
- `claude_client.py:1072, 1393` register `call_type="primary"`
- `claude_client.py:967, 1208` register `call_type="reflection"`

The hourly window (`llm_cost_guard.py:100-105, 176`) treats them as identical. So `llm_hourly_call_limit=60` ⇒ ≤ 30 *full evaluations* per hour, not 60.

The bot's natural cadence is ~6–8 evaluations/min when not blocked. So the cap is hit before the rolling window can release a single slot.

**Why it matters.** The cap interacts pathologically with the bounded queue: snapshots continue arriving, get coalesced, then get processed rapidly (sub-150 ms each) as failures, draining the queue. The bot looks active in `htop` but is producing 0 evaluations.

**Recommended fix (any one of these unblocks):**
- (A) Add a separate `llm_reflection_hourly_call_limit` and only count primary against the trading cap. Reflection is a cognitive-quality gate, not a market-action gate; budgeting them together inverts the economics.
- (B) Bump `llm_hourly_call_limit` to 120 to restore the intuitive "60 evals/hour" reading.
- (C) Add a per-process token-cost gate (USD/hr) alongside the call-count gate, and remove the call-count gate as primary. Call counts are a proxy for cost; metering cost directly is more honest and lets you set policy in dollars rather than calls.

My recommendation is **(A) for correctness + (C) for the long term.** (B) is a quick patch that papers over the bug.

---

### 4.2 [HIGH] Grok live calls 100% failed pre-fix — 2.0 s timeout vs 2.85–7.65 s real latency

**Symptom.** In Run 1 (pre-fix), every eligible-category Grok call (`CRYPTO`, `IRAN`, etc.) emitted three `grok_sentiment_timeout` lines and then `grok_sentiment status=FALLBACK reason=GROK_TIMEOUT sentiment_score=0.0`. The downstream LLM saw neutral sentiment, reflection caught `narrative_anchoring_on_neutral_fallback`, and Gatekeeper enforced HOLD with EV=0.0. **Result: 30 / 30 evals had EV=0.0**, the bot had no signal to act on.

**Root cause (diagnosed out-of-band via curl).** Two compounding issues:

1. **Timeout too tight.** `grok_timeout_seconds=2.0` (`config.py:316` default) is shorter than xAI's real per-request latency. Measured with `curl --max-time 10`:
   - `grok-3` (then-current code default): **7.65 s** wall clock
   - `grok-4-1-fast` (per yesterday's daily note): **2.85 s** wall clock
2. **Silent model aliasing.** Both `grok-3` and `grok-4-1-fast` are aliased server-side to `grok-4.3`, a reasoning model. The response includes `reasoning_content` with **739 reasoning tokens** to produce a 1-token answer. The reasoning overhead is the latency.

**Currently available xAI models** (`GET /v1/models`):
- `grok-4.20-0309-non-reasoning` ← right tool for sentiment classification
- `grok-4.20-0309-reasoning`
- `grok-4.20-multi-agent-0309`
- `grok-4.3` (what aliases route to today)
- `grok-imagine-image|-quality|-video` (irrelevant)

**Fix applied mid-session:**
- `.env` ← `GROK_MODEL=grok-4.20-0309-non-reasoning`, `GROK_TIMEOUT_SECONDS=8.0`
- `src/agents/evaluation/grok_client.py:45` ← neutral fallback string changed from `"Sentiment unavailable in 2.0s window; neutral fallback applied."` to `"Sentiment unavailable within configured window; neutral fallback applied."` (the hard-coded 2.0s string was actively misleading once the timeout was bumped)

**Post-fix Grok success rate (Run 2, 22 eligible-category calls):** 22 SUCCESS / 6 schema_error / 0 timeout = ~78% truly useful signal. The remaining 6 are not timeouts; they are schema rejections (Finding 4.3).

**My recommendation going forward:** also bump `grok_max_retries=3` (currently 2) and add an explicit `chain_budget` env override; today the chain budget is hard-coded at 60.0 s for dry-run and 2.0 s for live (`claude_client.py:55-58`) which is an unusually large gap to leave non-configurable.

---

### 4.3 [MEDIUM] Grok SUCCESS but Pydantic rejects on `top_narrative_summary > 320 chars`

**Symptom.** After the Grok fix, two specific IRAN markets (Pahlavi opposition viability; regime collapse by May 31) consistently produced narratives longer than 320 characters. `SentimentResponse.top_narrative_summary` is constrained to ≤320 chars (`src/schemas/llm.py`). The Pydantic `ValidationError` is caught at `grok_client.py:295-304`, immediately `break`s out of the retry loop, and returns `NEUTRAL_SENTIMENT`. **Net effect: a valid, well-reasoned narrative is thrown away** and the LLM sees neutral fallback for that snapshot.

**Frequency in Run 2:** 6 schema errors out of 28 eligible-category calls (22 SUCCESS + 6 schema_error) = **21% loss rate on otherwise-good sentiment data**.

**Sample rejected payloads (truncated):**
- `'Discourse centers on Rez...st-regime Iran by 2026.'`
- `'Most tweets express skep...me change this quickly.'`
- `'Discourse is largely bul...underground popularity.'`

**Three ways to fix, in order of preference:**

1. **Truncate-and-recover at the client boundary.** In `_attempt_live_call` (`grok_client.py:337-365`), before passing `data` to `SentimentResponse.model_validate`, soft-truncate `top_narrative_summary` to 320 chars (e.g., truncate at last full sentence ≤320 chars, append `…`). Preserves the real signal; absorbs model misbehavior gracefully. **This is what I would ship first.**
2. **Bump the schema to 500–600 chars.** Cheap, but loses the original intent (concise summary for prompt-budget reasons).
3. **Add a "≤320 chars" hard instruction to `_USER_PROMPT_TEMPLATE`** (`grok_client.py:62-79`). Will reduce but not eliminate the issue — non-reasoning models are notoriously bad at strict character constraints.

**Pick (1) + (3) together.** Tighten the prompt; recover gracefully when the model still over-shoots.

**Also tighten the retry semantics:** the current `except ValidationError: ... break` (line 304) treats schema errors as unrecoverable. They are not. For schema errors specifically, a single retry with an even stricter prompt suffix (`"Reply MUST be ≤300 chars. Previous reply was rejected for being too long."`) is worth trying.

---

### 4.4 [MEDIUM] Four observability subsystems silently disabled

**Symptom.** Startup logs four `*.disabled` lines:
- `telegram.disabled`
- `circuit_breaker.disabled`
- `operational_alerts.disabled`
- `operational_event_ledger.disabled`

**Cause.** All four feature flags default to `False` in `src/core/config.py:208, 225, 234, 553`. The current `.env` does not set any of them.

**Why it matters.**
- The Telegram bot token, chat ID, and validated send path are all present in `.env`. The bot would deliver runtime alerts if `ENABLE_TELEGRAM_NOTIFIER=true` were added. Today the operator has no real-time visibility.
- **`enable_operational_event_ledger=False` is the most material one.** WI-56 was specifically built for this dry-run use case (durable runtime story for non-technical operators). With it disabled, the dashboard activity feed (WI-59) is empty, the incident replay CLI (WI-58) has nothing to replay, and the daily digest (WI-60) cannot generate. **This run is invisible to every WI-56-through-WI-60 observability tool.**
- The circuit breaker (WI-27) being off is defensible in dry-run, but worth a deliberate decision rather than a default-off accident.

**Recommendation.** Add a `.env` block:
```
ENABLE_TELEGRAM_NOTIFIER=true
ENABLE_STARTUP_ALERT=true
ENABLE_OPERATIONAL_ALERTS=true
ENABLE_OPERATIONAL_EVENT_LEDGER=true
ENABLE_CIRCUIT_BREAKER=false   # explicit no for dry-run
```
And surface this misconfiguration loudly at startup — e.g., log `observability.minimal_mode` with a warning when more than two of these are off, since it's almost certainly unintended for any non-CI run.

---

### 4.5 [MEDIUM] `market_snapshots` write rate vs SQLite

**Symptom.** `market_snapshots` table grew from 0 to 98,690 rows in ~24 minutes wall clock (~4,100 rows/min sustained after the startup burst). DB file size: 161 MB after ~24 min. Disk extrapolation: ~400 MB/hr, ~9.6 GB/day, ~67 GB/week.

**Cause.** Every Polymarket WS `price_changes` frame triggers `market_snapshot_inserted`. There is no rate-limiting, sampling, or change-significance filter at the persistence boundary — every micro-price-tick is a row.

**Why it matters.**
- The data is being captured at fidelity well beyond what any downstream consumer needs. Decisions are made every ~10 s, not every 100 ms.
- SQLite remains responsive at this rate (no errors observed) but the file grows in a way that will quickly outpace any reasonable retention policy.
- Backups, restores, and analytical queries get expensive fast.

**Recommendations, cheapest first:**
1. **Add a per-market write throttle.** Persist only when (a) midpoint changes by ≥X bps, OR (b) ≥N seconds have elapsed since last persist for that condition. Even N=2s would drop write volume 10-20×.
2. **Move the high-frequency stream to an append-only ring buffer** (in-memory or a separate SQLite WAL file with auto-truncate) and only land "significant" snapshots in `market_snapshots`.
3. **Implement a retention job.** Drop or roll-up rows older than 7 days in `market_snapshots`. The decisions table (`agent_decision_logs`) and execution table should be retained indefinitely; raw ticks should not be.
4. **Long-term: PostgreSQL.** Stated as out-of-scope in Phase 16, but raw-tick volume is the canonical reason teams migrate off SQLite for this kind of workload.

---

### 4.6 [LOW] `orchestrator.market_activated category=None` logging gap

**Symptom.** All 15 activation log lines show `category=None`, but the same markets correctly show `market_category=CULTURE|CRYPTO|IRAN` in the downstream `Evaluation complete` line. This is a logging defect, not a categorization defect.

**Cause.** `MarketMetadata.category` is `None` at the time of `_activate_markets` (`src/orchestrator.py:621-625`). Category resolution happens later in `PromptFactory` / `ClaudeClient` from a different signal (likely the `tags` field).

**Recommendation.** Either resolve the category at activation time (so the log is useful for operators triaging market mix) or drop the `category=` key from the log to avoid implying it's known when it isn't. I would resolve it at activation — the `tags` field is already on `MarketMetadata`, so this is a 10-line change in the activation path.

---

### 4.7 [LOW] WS market re-discovery loop fires every ~10 s

**Symptom.** `ws_subscribe_summary` and 15 × `orchestrator.market_activated` lines are emitted every ~10 seconds, even though the active set has not changed. After 7 min there were 45 such cycles = 45 × 16 = 720 redundant log lines.

**Cause.** The market discovery / activation loop has a polling cadence that re-walks the eligible set without checking whether anything changed.

**Why it matters.** This is the largest single source of log volume and obscures real state-change events. It also implies the WS `subscribe_batch` is being called every 10s; this is currently a no-op if the token set is unchanged (per the WI-31 subscription-diffing work in yesterday's hotfix), but the orchestrator-side log noise remains.

**Recommendation.** Add a `dirty` check around the activation logging. Only emit `ws_subscribe_summary` + `orchestrator.market_activated` when the activated set or the subscribed token set actually changes. This is a 5-line change that will reduce log volume by an order of magnitude.

---

### 4.8 [LOW] `ws_client.skip_last_trade_no_book` startup race

**Symptom.** ~141 `skip_last_trade_no_book` warnings in the first 7 minutes. WebSocket emits `last_trade_price` frames before the corresponding `book` snapshot has populated, so the client correctly skips them but logs a warning each time.

**Cause.** Subscription ordering — `last_trade_price` and `book` are independent streams on the same condition.

**Why it matters.** Functionally correct (skipping is safe). Just noise. Easily filtered.

**Recommendation.** Downgrade to DEBUG, or count and emit a single `ws_client.book_warmup_complete` line per condition after the first book arrives, with the count of pre-book trades suppressed.

---

### 4.9 [LOW] Sustained `queue.coalesced` warnings at `queue_depth=50`

**Symptom.** 346 `queue.coalesced` lines in the first 7 minutes of Run 1, then slowed to ~5/min as the eval cadence stabilized.

**Cause.** Bounded queue (depth=50) saturated because the WS produces snapshots faster than the single LLM consumer drains them. This is by design — the WI-53 BoundedPromptQueue exists precisely to coalesce.

**Why it matters.** This is **expected and correct** under load. It's worth noting only because the volume of `queue.coalesced` log lines drowns out real events. The information ("we are coalescing") is captured well by the existing `queue.coalesce.rate` metric; logging every individual coalesce is redundant.

**Recommendation.** Demote per-coalesce log to DEBUG, keep the metric. Add a single `queue.coalesce.burst` log line at INFO when ≥10 coalesces happen in <5s, which is the actually-actionable signal.

---

## 5. Mid-Session Hotfix Applied

### Diff summary
Two files modified, both with low blast radius. Code changes are **not yet committed** and have not gone through MAAP review (required per `CLAUDE.md` for `src/agents/` changes before commit).

#### `.env` (additions)
```diff
 # --- Phase 13 Activation ---
 GROK_LIVE_ENABLED=True
 GROK_MOCKED=False
+GROK_MODEL=grok-4.20-0309-non-reasoning
+GROK_TIMEOUT_SECONDS=8.0
 CLAUDE_MODEL=claude-sonnet-4-20250514
```

Rationale:
- `GROK_MODEL`: pick a model that does not silently route through `grok-4.3` reasoning overhead. `grok-4.20-0309-non-reasoning` is the closest fast classification target in the current `/v1/models` listing.
- `GROK_TIMEOUT_SECONDS=8.0`: gives ~3× headroom over the measured 2.85s worst-case latency for the non-reasoning model. Combined with the unchanged `grok_max_retries=2`, `_compute_grok_budget` derives `min(8.0 × 3, 60.0 × 0.7) = 24.0s` total budget under dry-run.

#### `src/agents/evaluation/grok_client.py:42-46`
```diff
 NEUTRAL_SENTIMENT = SentimentResponse(
     sentiment_score=Decimal("0.0"),
     tweet_volume_delta=0,
-    top_narrative_summary="Sentiment unavailable in 2.0s window; neutral fallback applied.",
+    top_narrative_summary="Sentiment unavailable within configured window; neutral fallback applied.",
 )
```

Rationale: the hardcoded "2.0s window" string is misleading once the timeout is bumped. The new wording is timeout-agnostic. **This single edit is the one that requires MAAP review** before any commit, since it touches `src/agents/`.

### What I did NOT touch (deliberate)
- `GROK_TIMEOUT_SECONDS = 2.0` constant at `grok_client.py:55` — left alone because tests instantiate `GrokClient` with this default. Bumping it could cascade into test fixture expectations. Better addressed in a dedicated WI.
- `_CHAIN_BUDGET` / `_CHAIN_BUDGET_DRY_RUN` at `claude_client.py:55-58` — these are wider-scope budgets covering the full evaluation chain. Out of scope for a Grok-only hotfix.
- `SentimentResponse.top_narrative_summary` schema constraint — would resolve Finding 4.3 but needs schema migration consideration.
- The 4 disabled observability subsystems — operator decision, not a code defect.

### Validation
- Process restart was clean (PID 26469, same banner sequence, no exceptions).
- First Grok `status=SUCCESS` arrived 28 seconds after restart.
- New `top_narrative_summary` string visible in `grok_sentiment status=SKIPPED` lines.
- No regressions in queue, WS routing, snapshot persistence, or DB writes.

---

## 6. Numerical Summary

### Run 1 (pre-fix): 20:50:33 → 21:08:30 (~18 min)
| Metric | Value |
|---|---|
| Evaluations completed | 30 |
| First eval | 20:50:50 |
| Last eval | 20:55:56 |
| Effective eval window | 5 min 6 s |
| Action distribution | 30 HOLD / 0 APPROVED |
| EV distribution | 30 × 0.0 (all neutral fallback) |
| Grok status | 0 SUCCESS / 30 FALLBACK / many SKIPPED_CATEGORY |
| Grok timeouts | ≥ 169 (counted at T+10:46) |
| LLM budget blocks | 306 (at T+10:46) |
| `market_snapshots` rows | 83,933 (at T+10:46) |
| Errors / Tracebacks | 0 |
| Log file size | 40 MB (archived) |

### Run 2 (post-fix): 21:08:56 → ongoing (~6 min observed)
| Metric | Value |
|---|---|
| Evaluations completed | 30 |
| First eval | 21:09:04 |
| Last eval | 21:13:53 (just before budget cap) |
| Effective eval window | 4 min 49 s |
| Action distribution | 30 HOLD / 0 APPROVED |
| EV distribution | 22 × 0.0, +0.36 × 2, +0.10, −0.30, −0.70 × 2, −0.90, −0.96 |
| Grok status | 22 SUCCESS / 6 FALLBACK (schema) / 90 SKIPPED_CATEGORY |
| Grok eligible-call success rate | 22 / 28 = 78.6% |
| Grok timeouts | 0 |
| Grok schema errors | 6 |
| LLM budget blocks | 70 (and counting) |
| `market_snapshots` rows | 98,690 (cumulative) |
| Errors / Tracebacks | 0 |
| Process RSS | 122 MB (stable) |
| Log file size | 11 MB (ongoing) |

### Database (cumulative across both runs)
| Table | Rows |
|---|---|
| `market_snapshots` | 98,690 |
| `agent_decision_logs` | 154 |
| `execution_txs` | 0 (DRY_RUN holds; would also be 0 with `dry_run=false` since 0 APPROVED) |
| `positions` | 0 |
| `operational_events` | 0 (ledger disabled) |

---

## 7. Points of View — What I Think Is Going On

This section is my interpretation, not just observation. Take with appropriate skepticism.

**The bot is over-engineered for fail-closed safety relative to its current ability to surface positive signals.** Every layer (Grok sentiment → DeepSeek primary → reflection pass → Gatekeeper) is correctly conservative in isolation; their composition is so conservative that the bot can plausibly run for hours without taking a single action even when the inputs would justify one. The two strongest positive-EV evaluations in this session (BTC $150k @ EV+0.36; Pahlavi @ EV+0.10) were both HOLDed because the reflection layer flagged them. This is the *right* behavior for production with real money, but it makes evaluating the bot's ability to actually trade *very* hard. For paper-trading evaluation, I would shadow-log what the bot would have done with a slightly relaxed Gatekeeper (e.g., `min_confidence=0.65`) without changing the actual decision path.

**The Grok integration was load-bearing in a way that wasn't obvious until it broke.** When Grok returns neutral fallback, the LLM is structurally incapable of producing a non-zero EV: the entire sentiment column in the prompt collapses to "unavailable." The reflection layer then catches "anchoring on neutral fallback" and the system fail-closes. So Grok being 100% timing out doesn't *look* like a Grok problem in the logs — it looks like 30 consecutive HOLDs with EV=0.0, which is what a safe bot is supposed to do. **The Grok timeout was the silent root cause of an apparent "everything is fine" symptom.** This is a teaching moment: every external dependency's health needs a first-class metric and alert, not just a fail-closed default.

**The LLM cost guard's design conflicts with the system's actual cost model.** Counting reflection calls against the same hourly budget as primary calls means that *quality* (reflection) is rationed against *coverage* (primary evaluations). In practice, reflection is what gives the system its safety properties; primary alone is unsafe. So the current accounting penalizes the very thing that makes the bot trustworthy. I'd separate these.

**The bot is running with three different cadences that don't compose well:**
- WebSocket frames: 1500+/min
- Market re-discovery: every 10 s
- LLM evaluation: ~6-8/min (when not budget-blocked)

The WS-to-eval ratio is ~250:1. The queue coalesces 80%+ of frames before the consumer touches them. This isn't wrong, but it means the system is doing a lot of work upstream that the downstream layer cannot consume. If the goal is "high-quality slow decisions," the WS ingestion should be sampling, not absorbing every tick.

---

## 8. Recommendations (Prioritized)

### Tier 1 — Do before next dry-run

1. **Separate primary and reflection budget counters** (`src/agents/evaluation/llm_cost_guard.py`). Add `llm_reflection_hourly_call_limit` (default 60 or 120) and stop counting reflection against the primary cap. Without this, every dry-run is structurally constrained to 30 evals/hour regardless of how fast the bot wants to run.
2. **Enable WI-56 event ledger and Telegram notifier** for operator visibility:
   ```
   ENABLE_TELEGRAM_NOTIFIER=true
   ENABLE_STARTUP_ALERT=true
   ENABLE_OPERATIONAL_ALERTS=true
   ENABLE_OPERATIONAL_EVENT_LEDGER=true
   ```
   These are what every WI-56→60 was built to surface. Running the dry-run without them defeats half the value.
3. **Implement Grok narrative truncate-and-recover** at `grok_client.py:_attempt_live_call` to convert the 21% schema-error loss into 0%. ~10-line change.
4. **Add a `grok.success_rate` and `llm.budget_window.utilization` metric**, both labeled by category. Today these metrics exist implicitly in logs; promoting them lets the operator see the two failure modes at a glance.

### Tier 2 — Material improvement, larger scope

5. **WS snapshot persistence throttle**: persist a row when (midpoint Δ ≥ 25 bps) OR (Δt ≥ 2 s). Drops DB write volume 10–20×, keeps the trading-relevant resolution.
6. **MAAP-clean the Grok hotfix** (the `grok_client.py:45` string change) and commit on `develop`. Tests should pass since the string is only consumed in fallback paths.
7. **De-dupe market re-activation logs.** Only log `ws_subscribe_summary` + `orchestrator.market_activated` when the active token set actually changes. Drops 720+ redundant lines per 7 minutes.
8. **Resolve `MarketMetadata.category` at activation time** (Finding 4.6). Either populate it from `tags` in the Gamma loader, or drop the `category=` key from the activation log.

### Tier 3 — Longer-horizon ideas

9. **Add a shadow Gatekeeper.** Run a "would-have-traded" path with `min_confidence=0.65` (and looser reflection-flag interpretation) in parallel to the real Gatekeeper. Persist its decisions to a `shadow_decisions` table. This gives you A/B data on whether the production Gatekeeper is over- or under-tuned, without changing real behavior. Especially valuable for paper trading where the goal is calibration.
10. **Promote Grok-eligibility resolution out of `claude_client.py` / `grok_client.py`**. Today it's split across both files (shared via `GROK_ELIGIBLE_CATEGORIES` from `src/schemas/llm.py`, per yesterday's hotfix). Move the eligibility check to a dedicated `SentimentRouter` that decides which oracle (Grok, none, future others) is consulted per market. Makes future per-category routing decisions trivial.
11. **Cost-per-decision metric.** Track DeepSeek token-cost USD per `Evaluation complete`. Today the cost guard is a call-count gate; a USD/hr gate is more honest and lets policy be set in dollars.
12. **Replace `grok-3` / `grok-4-1-fast` references everywhere.** The model name in `.env.example`, daily notes, and tests should reflect what xAI actually exposes today. Audit all of `src/`, `docs/`, `tests/` for stale model strings.
13. **Add WS health watchdog.** Today there's no proactive disconnect detection; the system relies on `websockets`' internal ping/pong. A 30s "no frame received" watchdog that triggers a reconnect-and-resubscribe would be a one-class addition with high MTBF impact.
14. **Bot SLO definition.** What does "the bot is working" mean operationally? Today the only signal is "process is alive + WS connected." A composite SLO of `evals_per_minute ≥ X AND grok_success_rate ≥ Y AND queue_coalesce_rate ≤ Z` would give the operator a single status-light view. Telegram can push that.

---

## 9. Open Questions / Ideas I Did Not Pursue

- **Why is the rolling-window budget counter implemented as a simple `>=` rather than a true sliding window?** If it's a true sliding window, the bot should be releasing one slot every ~3 minutes (180s / call) after the cap. I did not observe a single release during ~10 min of post-cap idle in Run 1. Worth verifying.
- **Does the budget guard count failed calls?** If `llm_budget_blocked` is itself counted, the cap is self-perpetuating. From the code path it doesn't appear to, but I didn't trace it end-to-end.
- **Should reflection be skipped on low-confidence primary outputs?** If primary already produces `confidence < min_confidence`, running the reflection LLM call is wasted budget — the Gatekeeper will HOLD regardless. A cheap "skip reflection if primary fails confidence" optimization could double the effective eval throughput.
- **Are CULTURE markets worth subscribing to at all?** 21 of 30 evaluations were CULTURE markets that the bot has no useful signal on (Grok skips them by design). If the bot won't trade on them, why are we burning LLM budget evaluating them? The eligibility filter at market activation should consider whether the bot has at least one usable signal source for the category.

---

## 10. Files Modified This Session

| File | Status | Change |
|---|---|---|
| `.env` | Modified (uncommitted) | Added `GROK_MODEL`, `GROK_TIMEOUT_SECONDS` |
| `src/agents/evaluation/grok_client.py` | Modified (uncommitted) | Updated `NEUTRAL_SENTIMENT.top_narrative_summary` text |
| `logs/orchestrator-run-pre-grok-fix.log` | Created (40 MB) | Archived Run 1 stdout/stderr |
| `logs/orchestrator-run.log` | Created (ongoing) | Run 2 stdout/stderr |
| `docs/runtime_observations/2026-05-17-orchestrator-dry-run-session.md` | Created (this file) | Session report |

The two source-tree changes (`.env`, `grok_client.py`) **must go through MAAP review before any commit** per `CLAUDE.md` MAAP protocol. The `.env` change is config-only; the `grok_client.py` change is a single-line string edit in a fallback constant. No tests should regress, but the test suite should be run before commit.

---

## 11. Process Notes for the Next Operator

- **Two background sentinels are still running** in this session's tmp directory (10-min markers fired ~21:07 and ~21:23). They are no-ops and will exit naturally.
- **Monitor is currently armed** (task `b0djvhkig`) watching `logs/orchestrator-run.log` for state-transition events only. It will not fire on `llm_budget_blocked`, `grok_sentiment_timeout`, or `queue.coalesced` to keep noise low.
- **Orchestrator is currently running** (PID 26469, started 21:08:56). It is in budget-exhausted steady-state. The next evaluation should fire when the sliding window opens at ~22:08:56 (roughly 1 hour from restart).
- **Log file `logs/orchestrator-run.log` is the live run.** `logs/orchestrator-run-pre-grok-fix.log` is the archived first run.
- If you stop and restart, **archive the current log first** (`mv` to a timestamped name), otherwise the next run will overwrite it.

---

## 12. Closing

The session's biggest single deliverable is **a clear root-cause diagnosis for the "bot looks idle even when conditions warrant action" pattern.** That pattern is the product of three independent throttles compounding (4.1 + 4.2 + 4.3) plus an over-conservative Gatekeeper (Section 7), not a single broken component. Fixing any one of them in isolation only shifts the bottleneck.

The Grok hotfix was the right first move because it converted the system from "structurally unable to produce a non-zero signal" into "structurally able, but currently throttled by the budget counter." That's a much better problem to have.

The session also surfaced one architectural smell worth naming: **observability subsystems that default to disabled.** WI-56 through WI-60 are excellent infrastructure, but they cost nothing to enable and you can't see them work if they're off. The default-off posture is reasonable for tests; it is anti-functional for any human-operated run. Flipping those defaults (or aggressively warning at startup when they're off) would prevent the next session from spending its first 10 minutes confirming what should have been visible all along.
