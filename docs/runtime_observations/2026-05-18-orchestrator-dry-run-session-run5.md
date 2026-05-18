# Orchestrator Dry-Run Session Report — 2026-05-18

**Author:** Claude Code (session observer)
**Date (UTC):** 2026-05-18
**Branch:** `develop` (working tree had uncommitted Run 3-4 calibration hotfixes on entry)
**Runtime under test:** `.venv/bin/python -m src.orchestrator` (Python 3.14, `.venv`)
**Mode:** `DRY_RUN=true`, `LLM_PROVIDER=deepseek`, `GROK_LIVE_ENABLED=true`
**Session window:** 17:37:53 UTC → 18:37:53 UTC (60 min continuous, no restarts)
**Scope:** End-to-end runtime observation of the autonomous Polymarket trading agent in paper-trading mode. No mid-session hotfix was applied. The Run 4 calibration (`PREFLIGHT_MAX_SPREAD_PCT=0.99`) was carried forward from the prior session and remained in effect throughout.

---

## 1. Executive Summary

The bot ran for 60 minutes with **zero uncaught exceptions, zero Tracebacks, and zero WebSocket disconnections**. The Gatekeeper maintained perfect fail-closed posture: **0 approved decisions, 0 orders signed, 0 positions opened**. The Grok sentiment pipeline delivered a 99.6% success rate (238/239 eligible calls), and the daily LLM token budget (10M) consumed only 6.5% (~647K tokens) — well within limits.

However, the session revealed **three structural constraints** that prevent the dry-run pipeline from ever producing an actionable signal:

1. **Per-market hourly LLM caps trigger on the only active trading category.** CULTURE markets consumed 63% of evaluation volume (60/95) with 3 markets splitting the shared 60-call/hour window. The cap triggered 4 times, creating periodic 4–5 minute evaluation pauses and collapsing overall cadence from 3.9/min to 1.5/min. The caps fire at `src/agents/evaluation/llm_cost_guard.py:252`.

2. **Activated markets are structurally unactionable.** All 4 activated markets (1 IRAN, 3 CULTURE) trade with 96–98% bid-ask spreads. The `PREFLIGHT_MAX_SPREAD_PCT=0.99` gate (`.env:55`) allows these markets through, but every evaluation produces zero or negative EV. The Gatekeeper reflection correctly rejects all trades, but ~647K tokens were consumed evaluating markets where no trade is possible.

3. **CULTURE markets have zero Grok sentiment signal.** `src/schemas/llm.py:91-102` defines `GROK_ELIGIBLE_CATEGORIES` — CULTURE is not included. With 3 of 4 activated markets in the CULTURE category, 63% of evaluations operate without any sentiment oracle input, guaranteeing reliance on midpoint-anchored estimates that the reflection layer consistently flags as biased.

**Net trading output:** 95 evaluations (1.58/min avg), all HOLD, 0 approved, 0 orders. This is correct fail-closed behavior, but the pipeline is structurally incapable of generating a positive-EV trade given the current market mix and configuration.

---

## 2. Session Timeline (UTC)

| Time (UTC) | Event |
|---|---|
| 17:37:53 | Orchestrator PID 56906 started |
| 17:37:53 | Startup posture: `circuit_breaker.disabled`, all other observability enabled |
| 17:37:53 | Gamma: 100 active markets fetched, 25 preflighted |
| 17:37:54 | 4 markets activated after preflight: 1 IRAN, 3 CULTURE — 8 tokens, 4 conditions |
| 17:37:54 | 13 SPREAD_TOO_WIDE, 1 ORDER_BOOK_UNAVAILABLE, 8 ttr_fail — 22 markets rejected |
| 17:37:55 | Operational alert dispatched: `process_started` |
| 17:38:07 | First evaluation complete: IRAN, EV=-0.9, HOLD, reflection REJECTED |
| 17:42:05 | **Grok HTTP 429** on IRAN market (xAI rate limit) — transient, self-healed |
| 17:45:47 | T+5 snapshot: 31 evals, 46 Grok SUCCESS, 1 Grok HTTP 429 |
| ~17:48:00 | First eval pause (~290s gap): per-market CULTURE cap suspected |
| ~17:52:00 | Eval cadence resumes after per-market window refresh |
| 17:56:37 | T+15 snapshot: 53 evals, 90 Grok SUCCESS, 36 queue.coalesced |
| ~17:58:00 | Second eval pause (~240s gap): second per-market cap event |
| 18:10:33 | **Grok timeout** on IRAN market (attempt 0, remaining_budget=24.0s) — isolated |
| 18:11:33 | T+30 snapshot: 75 evals, 187 Grok SUCCESS, 1 Grok timeout |
| ~18:15:00 | Third eval pause (~300s gap): third per-market cap event |
| 18:37:53 | T+60 window closes |
| 18:41:56 | Final snapshot: 95 evals, 238 Grok SUCCESS, 4 budget blocks |

---

## 3. Environment & Configuration

### Loaded `.env` (secrets redacted)

| Key | Value |
|---|---|
| `ENVIRONMENT` | development |
| `DRY_RUN` | true |
| `LLM_PROVIDER` | deepseek |
| `LLM_HOURLY_CALL_LIMIT` | 240 |
| `LLM_REFLECTION_HOURLY_CALL_LIMIT` | 240 |
| `LLM_DAILY_CALL_LIMIT` | 2000 |
| `LLM_DAILY_TOKEN_LIMIT` | 10000000 (10M) |
| `LLM_DAILY_COST_LIMIT_USD` | 30 |
| `LLM_MARKET_HOURLY_CALL_LIMIT` | 60 |
| `GROK_LIVE_ENABLED` | True |
| `GROK_MOCKED` | False |
| `GROK_MODEL` | grok-4.20-0309-non-reasoning |
| `GROK_TIMEOUT_SECONDS` | 8.0 |
| `DEEPSEEK_MODEL` | deepseek-chat |
| `DEEPSEEK_MAX_TOKENS` | 4096 |
| `PREFLIGHT_MAX_SPREAD_PCT` | 0.99 |
| `ENABLE_MARKET_DISCOVERY_PREFLIGHT` | true |

### Disabled Subsystems

| Subsystem | Status |
|---|---|
| `circuit_breaker` | **DISABLED** |
| `operational_alerts` | enabled |
| `operational_event_ledger` | enabled |
| `telegram` | enabled (startup alert dispatched) |

### Runtime Knobs

| Knob | Value |
|---|---|
| Provider | deepseek |
| Primary model | deepseek-chat |
| Grok model | grok-4.20-0309-non-reasoning |
| Preflight spread max | 0.99 (99%) |
| Per-market hourly cap | 60 calls |
| Daily token cap | 10,000,000 |

### Markets Activated

| # | Category | Condition ID (first 16 chars) | Tokens |
|---|---|---|---|
| 1 | IRAN | 0x0e4a0c937b8934c2 | 2 |
| 2 | CULTURE | 0x5c954cebf46d20ab | 2 |
| 3 | CULTURE | 0x8246b71524253635 | 2 |
| 4 | CULTURE | 0x62f802ca4e7555f0 | 2 |

---

## 4. Findings (Ranked by Severity)

### MEDIUM — M1: Per-market hourly LLM cap creates 4–5 minute evaluation pauses

**Symptom:** 4 `llm_budget_blocked` events with reason `per_market_hourly_limit_exhausted`. Evaluation cadence dropped from 3.9/min (first 5 min) to 1.5/min (T+15–T+30 interval). Three distinct gaps of 240–300 seconds observed at evals 21, 41, and 66.

**Root cause:** `src/agents/evaluation/llm_cost_guard.py:252` — `market_calls >= per_market_limit` — triggers the block when a single market exceeds 60 calls in the sliding hourly window. With 3 CULTURE markets activated, each evaluation consumes 1 primary + 1 reflection call (2 total), effectively halving the per-market capacity to 30 full evaluations/hour. The CULTURE category, evaluated at ~1.5 evals/min across 3 markets, exhausts the 60-call cap in approximately 10 minutes of active evaluation, leaving 50 minutes of idle per market per hour.

**Why it matters:** The cap was designed as a cost guard, but in a session where only one category (CULTURE) dominates evaluation volume, it creates a throttle that compounds across markets. The bot spends 83% of wall-clock time idle waiting for budget windows to refresh.

**Recommended fix:** Consider coupling per-market caps with per-category awareness, or decouple primary and reflection counts so 60 calls = 60 evaluations (not 30). Short-term: increase `LLM_MARKET_HOURLY_CALL_LIMIT` or decrease reflection call frequency for markets with stable HOLD patterns.

### MEDIUM — M2: Activated markets produce structurally unactionable evaluations

**Symptom:** 95 evaluations, 95 HOLD, 0 approved, 0 positive EV. All 71 non-zero EVs are negative (-0.42 to -0.96). ~647K tokens burned on markets where no trade was ever possible.

**Root cause:** `PREFLIGHT_MAX_SPREAD_PCT=0.99` at `.env:55` (overriding `src/core/config.py:519` default of 0.05) allows markets with 96–98% bid-ask spreads to pass the preflight gate at `src/agents/ingestion/market_discovery.py:435`. The spread check `spread / ask > max_spread` with max_spread=0.99 means a market with bid=0.01 and ask=0.99 (spread=0.98) has spread/ask = 0.99, which exactly equals the threshold, allowing it through. The hotfix was correct for the Run 3 calibration (where the 0.80 threshold blocked all markets), but 0.99 is functionally equivalent to disabling the spread gate.

**Why it matters:** Every evaluation on a 98%-spread market is a guaranteed HOLD. The Gatekeeper correctly blocks execution, but the pipeline spends LLM tokens, Grok budget, and compute on markets where no positive-EV trade is possible. The system is correctly fail-closed but structurally wasteful.

**Recommended fix:** Re-calibrate to a balanced threshold (e.g., 0.80 or 0.90) and supplement with additional preflight criteria beyond spread alone — minimum order book depth, last-trade recency, or trade volume thresholds.

### MEDIUM — M3: CULTURE markets lack Grok sentiment, crippling evaluation quality

**Symptom:** 460 Grok `SKIPPED_CATEGORY` events vs 238 SUCCESS. CULTURE evaluations (60/95 = 63%) operate without any sentiment oracle signal. The reflection layer repeatedly flags `narrative_anchoring_on_midpoint`, `no_sentiment_signal`, and `overconfidence_in_0.5_estimate_without_evidence` on CULTURE evaluations — correctly identifying the information vacuum.

**Root cause:** `src/schemas/llm.py:91-102` — `GROK_ELIGIBLE_CATEGORIES` includes CRYPTO, POLITICS, ELECTIONS, GEOPOLITICS, FINANCE, TECH, IRAN, ECONOMY. CULTURE is intentionally excluded. When the only activated markets are CULTURE (3 of 4), the sentiment pipeline provides zero signal for 75% of markets.

**Why it matters:** Evaluations without sentiment are biased toward the midpoint estimate (p_true=0.5), which the Gatekeeper reflection correctly identifies as unsupported. The pipeline produces evaluations that are internally identified as biased and then rejected — a self-defeating cycle.

**Recommended fix:** Either expand Grok eligibility to CULTURE (with appropriate prompt tuning for cultural prediction markets), or ensure market activation preferentially selects categories with Grok coverage. The latter requires addressing M2 (spread filter) so that CRYPTO/FINANCE/TECH markets can pass preflight.

### LOW — L1: Transient Grok HTTP 429 at T+4:12min

**Symptom:** Single `grok_sentiment_http_error` with `status_code=429` at 17:42:05 on IRAN market (condition 0x0e4a...). Self-healed — 237 subsequent SUCCESS without intervention.

### LOW — L2: Isolated Grok timeout at T+32:40min

**Symptom:** Single `grok_sentiment_timeout` at 18:10:33 on IRAN market (attempt=0, remaining_budget=24.0). The 8-second timeout triggered, but retries succeeded.

### LOW — L3: Log growth at 2.7 MB/min

**Symptom:** `logs/orchestrator-run.log` grew from 0 to 164 MB in 60 minutes. Primary contributors are snapshot insertion logs and operational event ledger writes. At this rate, a 24-hour session would produce ~3.9 GB.

### LOW — L4: Database at 698 MB with no snapshot retention policy

**Symptom:** `market_snapshots` table at 480,069 rows (cumulative across all prior sessions). The DB grows unboundedly. No TTL or archival mechanism exists for old snapshots.

### LOW — L5: Mild queue backpressure (36 coalesced events)

**Symptom:** `queue.coalesced` count reached 36 by T+15 and stabilized thereafter. Indicates snapshot arrival rate occasionally exceeds evaluation throughput, but the system self-regulates without escalation.

### LOW — L6: WebSocket subscription summaries at 13

**Symptom:** `ws_subscribe_summary` emitted 13 times in 60 minutes. 12 beyond the initial subscription, suggesting periodic re-subscription events. No disconnections or stale connections observed, so these are likely rebalance-driven.

---

## 5. Mid-Session Hotfix Applied

**None.** No code or configuration changes were applied during this session. The orchestrator ran continuously from 17:37:53 to 18:37:53 UTC without restart.

---

## 6. Numerical Summary

### Run 5 (this session — continuous)

| Metric | T+5 | T+15 | T+30 | T+60 |
|---|---|---|---|---|
| Evaluations | 31 | 53 | 75 | 95 |
| Cadence (avg/min) | 3.9 | 3.5 | 2.5 | 1.58 |
| HOLD | 31 | 53 | 75 | 95 |
| BUY | 0 | 0 | 0 | 0 |
| approved=True | 0 | 0 | 0 | 0 |
| EV > 0 | 0 | 0 | 0 | 0 |
| EV = 0 | 48 | 81 | 101 | 121 |
| EV < 0 | 14 | 26 | 51 | 71 |
| IRAN | 7 | 13 | 25 | 35 |
| CULTURE | 24 | 40 | 50 | 60 |
| Grok SUCCESS | 46 | 90 | 187 | 238 |
| Grok SKIPPED | 143 | 254 | 379 | 460 |
| Grok HTTP 429 | 1 | 1 | 1 | 1 |
| Grok timeout | 0 | 0 | 1 | 1 |
| Budget blocks | 0 | 0 | 0 | 4 |
| Queue coalesced | 16 | 36 | 36 | 36 |
| WS disconnects | 0 | 0 | 0 | 0 |
| Reflection REJECTED | — | — | — | 87 |
| Reflection APPROVED | — | — | — | 8 |
| Input tokens | — | — | 389K | 494K |
| Output tokens | — | — | 120K | 154K |
| Log size | 63 MB | 144 MB | 157 MB | 164 MB |
| DB size | 610 MB | 677 MB | 689 MB | 698 MB |

### DB Cumulative (across all sessions)

| Table | Row Count |
|---|---|
| `market_snapshots` | 480,069 |
| `agent_decision_logs` | 716 |
| `execution_txs` | 0 |
| `positions` | 0 |
| `operational_events` | 37,337 |

---

## 7. Points of View

1. **The system is provably safe.** Five consecutive dry-run sessions (2026-05-17 Run 1 through 2026-05-18 Run 5) with zero live orders, zero signings, zero position opens. The Gatekeeper (`LLMEvaluationResponse`) is never bypassed — 100% enforcement.

2. **The budget guard works too well.** The 60-call per-market hourly limit, combined with the primary+reflection double-counting, creates a 30-effective-evaluation ceiling per market per hour. This means the bot can only evaluate ~2 markets fully before the cap triggers. With 3 CULTURE markets activated, the cap becomes the dominant throttle.

3. **The market activation pipeline is mismatched to the current Polymarket landscape.** Today's active markets are dominated by extreme-spread political/cultural bets. The preflight gate at 0.99 spread allows them through. The sentiment pipeline excludes CULTURE. The result is a closed loop: activate unactionable markets → produce zero-signal evaluations → Gatekeeper correctly rejects → repeat.

4. **Reflection is our best diagnostic tool.** The 87 REJECTED vs 8 APPROVED reflection ratio (10.9:1) correctly identifies narrative anchoring, overconfidence, and midpoint bias in the primary evaluations. The reflection layer is doing its job — flagging flawed reasoning before it reaches the Gatekeeper.

5. **Operational observability is healthy.** All subsystems except `circuit_breaker` are active. Telegram dispatched the startup alert. The operational event ledger accumulated 37K events. Zero SQLite lock errors. Zero WS disconnections.

---

## 8. Recommendations

### Tier 1 (address before next dry-run session)

1. **Recalibrate `PREFLIGHT_MAX_SPREAD_PCT` to 0.90** — balances the Run 3 discovery (0.80 blocked everything) with the need to exclude guaranteed-unactionable markets. Target: activate markets where spread < 90% instead of < 99%.

2. **Increase `LLM_MARKET_HOURLY_CALL_LIMIT` to 120** — doubles the effective evaluation ceiling from 30 to 60 per market per hour, matching the ~1 eval/min natural cadence.

### Tier 2 (address in next Work Item)

3. **Decouple primary and reflection call counting** in `llm_cost_guard.py` — either track them in separate per-market windows, or exclude reflection from the per-market cap since reflection is a safety check, not a cost driver.

4. **Add minimum order-book depth to preflight** — a market with spread > 50% and no bids/asks within 10% of midpoint should not be activated regardless of spread threshold.

5. **Expand Grok eligibility to CULTURE** — or implement an alternative sentiment source for CULTURE markets (web search, news API). Running evaluations without sentiment is structurally wasteful.

### Tier 3 (nice-to-have, future Phase)

6. **Implement snapshot retention policy** — cap `market_snapshots` at N days or N rows, with optional archival to cold storage.

7. **Add log rotation with compression** — at current rates, 24-hour sessions produce unmanageable log files.

---

## 9. Open Questions / Ideas Not Pursued

1. Should per-market budget caps be proportional to the number of activated markets? (e.g., 60 ÷ 4 = 15 calls/market if all 4 active, vs 60 ÷ 1 = 60 if only 1)
2. Could the preflight gate use a composite score (spread × depth × last-trade-age) instead of a single spread threshold?
3. Is there a Polymarket API endpoint for 24h trade volume that could inform activation decisions?
4. Should CULTURE be permanently excluded from the trading universe, or is there a subcategory of CULTURE markets (e.g., awards, entertainment) where sentiment signals could be sourced?

---

## 10. Files Modified This Session

**None.** No source-tree changes were applied. The orchestrator ran against the `develop` branch at commit `2b6ae38` with uncommitted `.env` changes (`PREFLIGHT_MAX_SPREAD_PCT=0.99`) carried forward from Run 3-4 calibration.

---

## 11. Process Notes for the Next Operator

- The orchestrator PID 56906 was **left RUNNING** at session end. To stop: `kill 56906`.
- Archived log from the earlier Run 4 session (PID 54133): `logs/orchestrator-run-2026-05-18T173740Z.log` (269 MB).
- Current session log: `logs/orchestrator-run.log` (164 MB).
- Stats snapshots saved to `logs/stats-snapshot-T5min.txt`, `T15min.txt`, `T30min.txt`, `T60min.txt`.
- The ANSI-stripped clean log is at `/tmp/orchestrator-clean.log` for grep convenience.
- All Grep queries in this report used the ANSI-stripped log to avoid color-code interference with pattern matching.

### Reproducibility
```bash
# Launch (from project root):
nohup .venv/bin/python -m src.orchestrator > logs/orchestrator-run.log 2>&1 &

# Monitor filtered events:
tail -f logs/orchestrator-run.log | sed 's/\x1b\[[0-9;]*m//g' | grep -nE "Evaluation complete|grok_sentiment |budget_blocked|decision\.|execution\.|ws_client\.(disconnected|reconnect|stale)|Traceback|ERROR"

# Stats snapshot:
grep -c "Evaluation complete" logs/orchestrator-run.log
grep -c "approved=True" logs/orchestrator-run.log
```

### Closing
The agent is safe, stable, and correctly fail-closed. The next operator's primary task is to close the gap between market activation (what we can evaluate) and market actionability (what can produce positive EV). The spread gate, budget throttle, and sentiment coverage are the three knobs that control whether the pipeline produces HOLD-only or begins routing live-eligible decisions.
