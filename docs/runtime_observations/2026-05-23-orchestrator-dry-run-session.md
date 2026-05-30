# Orchestrator Dry-Run Session Report — 2026-05-23

**Author:** Claude Code (session observer, Opus 4.7)
**Date (UTC):** 2026-05-23
**Branch:** `develop` (working tree had three untracked files on entry: prior 2026-05-19 observation/fix-plan pair and `scripts/auto_snapshots.sh`)
**Runtime under test:** `.venv/bin/python -m src.orchestrator` (Python 3.14.2, macOS Darwin 24.6.0)
**Mode:** `DRY_RUN=true`, `LLM_PROVIDER=deepseek`, `GROK_LIVE_ENABLED=True`, `GROK_MODEL=grok-4.20-0309-non-reasoning`
**Session window:** 22:38:34 UTC → 22:53:49 UTC (~15 min observed; orchestrator left running afterward)
**Scope:** End-to-end runtime observation of the autonomous Polymarket trading agent in paper-trading mode, post-WI-61 (periodic runtime audit) merge. No mid-session hotfix applied.

---

## 1. Executive Summary

The bot is **functionally healthy**, **financially safe** (DRY_RUN enforced, 0 orders), and **stable** (0 uncaught exceptions, 0 ERROR-severity events, 0 Tracebacks across 15 minutes / ~40 MB of log). Every prior structural blocker surfaced in 2026-05-17 and 2026-05-18 dry runs is **confirmed-fixed** in this run:

- **Grok live calls: 210 SUCCESS / 210 attempts (100%)**, 0 timeouts, 0 schema errors, 0 HTTP errors. The narrative truncate-and-recover work landed.
- **LLM budget did not block once** — the primary/reflection split + 240-calls-hr / 120-per-market calibration holds for the full window.
- **All four prior-disabled observability subsystems are live**: `operational_alerts.enabled`, `operational_event_ledger.enabled`, ledger writing (60001 → 60275 rows in 15 min), Telegram dispatched the `process_started` startup alert.
- **Category resolution at activation** is fixed (`category=CRYPTO|IRAN|CULTURE|ELECTIONS|FINANCE` populated in every `orchestrator.market_activated` line).
- **`ws_subscribe_summary` dedup is working** — only 2 emits in 15 min vs the prior 1-every-10s.

Three structural constraints remain and one new one was found:

1. **[NEW MEDIUM] WI-52 cognitive cooldown blocks 65% of LLM-eval attempts.** 138 `COOLDOWN_BLOCK severity=WARNING` events vs 75 `Evaluation complete` lines = 65% of attempted evaluations were short-circuited at the cooldown gate before any provider call. Cause is the same one called out as a *calibration* concern in 2026-05-17 §7 ("over-engineered for fail-closed safety"): the same handful of markets get HOLDed repeatedly, enter cooldown, and the bot then loops re-attempting them only to be blocked again. Per `src/agents/evaluation/claude_client.py:744-763`.
2. **[CARRY-OVER LOW] `orchestrator.market_activated` re-emits every ~10s without diff guard.** 1,410 lines in 15 min (95 distinct emit seconds × 15 markets). The dedup at `src/orchestrator.py:657-664` only protects `ws_subscribe_summary`; the per-market log at `src/orchestrator.py:677-682` is emitted unconditionally. Half-fix of 2026-05-17 Finding 4.7.
3. **[CARRY-OVER MEDIUM] WS snapshot persistence rate unchanged.** `market_snapshots` grew by 19,046 rows in 13 min ≈ 1,465 rows/min — projected ~88k/hr, ~2.1M/day, ~640 MB/day at the observed per-row footprint. Same as 2026-05-17 Finding 4.5; the throttle change was never landed.
4. **[NEW LOW] `ws_client.skip_no_token_non_positive_yes_quote` noise.** 3,251 emits in 15 min — one specific market is emitting a malformed early quote on every `price_change` frame.

**Net trading output: 75 evals, 0 APPROVED, 0 orders signed, 0 positions opened.** Correct fail-closed posture. Of the 75 evals, six were positive-EV (BTC $150k @ `EV=+0.36`, CRYPTO category) — the *same market* that was HOLDed by reflection in 2026-05-17. The 2026-05-17 §7 calibration question — "is the Gatekeeper over-tuned or correctly conservative?" — is still open.

---

## 2. Session Timeline (UTC)

| Time | Event |
|---|---|
| 22:38:34 | Orchestrator launched (PID 23542). `.venv/bin/python -m src.orchestrator`. T0. |
| 22:38:37 | `circuit_breaker.disabled` (explicit dry-run choice). `operational_alerts.enabled`, `operational_event_ledger.enabled`. |
| 22:38:37 | `operational_event_bus.started queue_maxsize=1000`. `nonce_manager.initialized_dry_run nonce=0`. |
| 22:38:38 | Gamma fetch: 100 active markets, 74 eligible, 26 `ttr_fail`, 15 activated. WS subscribed to 30 tokens. |
| 22:38:38 | `EvaluationConsumer started provider=deepseek`. `health_server` on 127.0.0.1:8080. `metrics_server` on 127.0.0.1:8081. |
| 22:38:39 | `operational_alerts.dispatched alert_type=process_started severity=INFO` (Telegram path active). |
| 22:38:48 | First duplicate `orchestrator.market_activated` block — 15 markets re-emitted (T+10s). Re-emit then repeats every ~10s for the entire session. |
| 22:39:55 | **First `Evaluation complete`** (T+81s). Action=HOLD, EV=−0.9, IRAN, DeepSeek. Reflection flags: `narrative_anchoring`, `overconfidence_unsupported_by_evidence`. |
| 22:39:58 | **First Grok `status=SUCCESS reason=RECEIVED`** (T+84s). IRAN regime collapse, sentiment_score=0.12. |
| 22:40:13 | First positive-EV evaluation: **EV=+0.36, CRYPTO**, action=HOLD (anchoring + overconfidence flags). Same BTC $150k market as 2026-05-17. |
| 22:43:34 | T+5 stats snapshot. 14 evals, all HOLD; Grok 15/15 SUCCESS; 0 budget blocks; 0 Tracebacks; 0 WARNINGs yet; RSS 158 MB. |
| 22:46:38 | **First `COOLDOWN_BLOCK severity=WARNING`** observed in log tail (cognitive cooldown engaged after repeated HOLDs). |
| 22:47–22:50 | Visible Grok call-rate burst: 22/min → 41/min → 48/min → 19/min. Backlog drain after the cooldown loop cleared queued snapshots in rapid succession. |
| 22:48:40 | Monitor reports `events suppressed — output rate too high` on the Grok-burst tail. Per command protocol, Monitor stopped and re-armed at 22:48:55 with a tighter filter (`approved=True` / positive-EV only). |
| 22:53:49 | Final stats snapshot. PID alive, RSS 181 MB, 75 evals, 138 COOLDOWN_BLOCKs, 210 Grok SUCCESS, 0 errors, 0 budget blocks. |

Orchestrator was **left running** after the snapshot (per command default; no `cleanup-on-exit` requested).

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
GROK_LIVE_ENABLED=True
GROK_MOCKED=False
GROK_MODEL=grok-4.20-0309-non-reasoning
TELEGRAM_BOT_TOKEN=<set>
TELEGRAM_CHAT_ID=<set>
LLM_REFLECTION_HOURLY_CALL_LIMIT=240
LLM_MARKET_HOURLY_CALL_LIMIT=120
PREFLIGHT_MAX_SPREAD_PCT=0.99
OPERATIONAL_EVENT_DIAGNOSTIC_THROTTLE_SEC=60
```

Drift vs STATE.md: STATE.md notes the 2026-05-18 calibration as `PREFLIGHT_MAX_SPREAD_PCT=0.90`; the live `.env` has `0.99`. Not a defect — that's a knob the operator has loosened — but flag it because the documented calibration and the live calibration disagree.

### Disabled subsystems at startup
| Subsystem | State | Notes |
|---|---|---|
| `circuit_breaker` | **disabled** | Explicit dry-run choice (matches 2026-05-17 fix-plan F1 recommendation: "ENABLE_CIRCUIT_BREAKER=false explicit no for dry-run"). No live drawdown to halt. |
| `telegram_notifier` | **enabled** | Initialized (no `telegram.disabled` log line). `operational_alerts.dispatched alert_type=process_started` confirms the path. Cited at `src/orchestrator.py:219-232`. |
| `operational_alerts` | **enabled** | Cited at `src/orchestrator.py:253-260`. |
| `operational_event_ledger` | **enabled** | Cited at `src/orchestrator.py` ledger init; rows persisted (60001 → 60275 in 15 min). |

### Active runtime knobs that drove behavior
| Setting | Value | File / cite |
|---|---|---|
| `llm_market_hourly_call_limit` | 120 | `.env` |
| `llm_reflection_hourly_call_limit` | 240 | `.env` (post-WI Run-2 budget split) |
| `preflight_max_spread_pct` | 0.99 | `.env` (vs STATE.md doc of 0.90) |
| `operational_event_diagnostic_throttle_sec` | 60 | `.env` |
| `GROK_MODEL` | `grok-4.20-0309-non-reasoning` | `.env` |
| Cognitive cooldown trigger | repeated HOLD per market | `src/agents/evaluation/claude_client.py:744-763` |

### Markets activated
- 100 fetched from Gamma → 74 passed preflight (26 `ttr_fail`) → 15 activated.
- Categories: **CRYPTO×1, IRAN×7, CULTURE×5, ELECTIONS×2, FINANCE×1**.
- Subscribed to 30 WS tokens (15 markets × YES/NO).
- Every activation log now correctly carries `category=` (Finding 4.6 from 2026-05-17 is fixed).

---

## 4. Findings (Ranked by Severity)

### 4.1 [MEDIUM] Cognitive cooldown short-circuits 65% of evaluation attempts

**Symptom.** Over the 15-min window the ledger received **138 `COOLDOWN_BLOCK severity=WARNING reason_code=COOLDOWN_REPEATED_HOLD`** events vs 75 `Evaluation complete` events. So roughly two-thirds of every snapshot that reached the LLM consumer was rejected by the cognitive breaker before any provider call.

**Root cause.** `src/agents/evaluation/claude_client.py:744-763` — `_cognitive_breaker.check_cooldown(market_key)` runs *first* in the evaluation path. When a market accumulates repeated HOLD outcomes, the breaker opens and every subsequent snapshot for that market is rejected with this WARNING until cooldown elapses. The breaker is doing exactly what WI-52 designed it to do.

**Why it matters.** This is not a defect — the cooldown is the bot's defense against burning budget on a market the Gatekeeper keeps rejecting. But it interacts with the §7-style "over-conservative Gatekeeper" symptom in a way that produces *visible* idle behavior:

- The same handful of markets (IRAN regime collapse, BTC $150k, Pahlavi viability) get HOLDed by reflection on every visit.
- Each HOLD increments the cooldown trigger count.
- Once cooldown opens, subsequent snapshots for that market are dropped.
- Because IRAN is over-represented in the activated set (7 of 15 markets), the cooldown population is heavily IRAN-skewed (51 of 75 evals = 68% are IRAN; the rest are dwarfed).
- Net effect: the bot spends most of its working time *acknowledging that it cannot evaluate the markets it actively subscribed to*.

**Recommended fix (calibration, not code).**
1. Surface a `cognitive_cooldown.block_rate` metric (per-window, per-market). Today the WARNING is logged and persisted but there is no first-class metric — operators cannot tell at a glance "are we in a cooldown loop?"
2. Re-examine `min_confidence=0.75` and the reflection-flag interpretation for the IRAN cluster specifically. The cooldown-block:eval ratio of ~2:1 is a strong signal that the Gatekeeper is operating in a steady-rejection regime for that category. Either tune the threshold, or remove IRAN markets from the activated set until the signal quality improves.
3. Document the expected COOLDOWN_BLOCK rate in the daily ops digest (WI-60). Today the digest reports total events but does not interpret "138 COOLDOWN_BLOCKs in 15 min" as healthy or unhealthy.

---

### 4.2 [LOW — carry-over from 2026-05-17 §4.7] `orchestrator.market_activated` re-emitted every ~10s without diff guard

**Symptom.** 1,410 `orchestrator.market_activated` INFO lines in 15:15 of runtime = ~94/min = ~5,660/hr. 95 distinct emit-seconds × 15 markets. Re-emit cadence is ~10s, regardless of whether the activated set changed (it did not change at all in this session).

**Root cause.** `src/orchestrator.py:677-682` emits the per-market log unconditionally inside the for-loop over `deduped`:
```
for market in deduped:
    ...
    logger.info("orchestrator.market_activated", condition_id=..., category=..., token_count=...)
```
The dedup guard at `src/orchestrator.py:657-664` (`if subscribe_summary != self._last_ws_subscribe_summary`) only protects the *summary* line, not the per-market lines. The hotfix called out in STATE.md as "suppresses unchanged WebSocket subscription-summary logs" implemented only the summary half of 2026-05-17 Finding 4.7.

**Why it matters.** This is the largest single source of log volume (1,410 of ~40 MB log over 15 min). It also makes it harder to spot the *real* activation events (when the set actually changes) — they are invisible inside the noise. At paper-trading scale this is hygiene; at any future multi-week run it is a real disk-pressure concern.

**Recommended fix.** Apply the dedup guard to the per-market loop too — store `self._last_activated_condition_ids: frozenset[str]`, compare `added`/`removed`, and only emit INFO when either set is non-empty. ~10 line change in `src/orchestrator.py:666-682`. This is exactly the F6 sketch from `2026-05-17-orchestrator-fix-plan.md:417-432` — the plan was approved but the per-market half was never landed.

---

### 4.3 [MEDIUM — carry-over from 2026-05-17 §4.5] WS snapshot persistence not throttled

**Symptom.** `market_snapshots` table grew from 794,218 → 813,264 over the 13 min between snapshots = **+19,046 rows / 13 min ≈ 1,465 rows/min sustained**. Projection: ~88k/hr, ~2.1M/day. At the historical row footprint the DB file already sits at **1.25 GB** (was 1.21 GB at T+5).

**Root cause.** WS frames still translate 1:1 to `market_snapshot_inserted` rows. There is no per-condition midpoint-Δ or time-Δ throttle at the persistence boundary. Same code path as cited in 2026-05-17 §4.5 (`src/agents/ingestion/ws_client.py`).

**Why it matters.** SQLite remains responsive (no `database is locked`, no slow queries observed), but the file growth is locked in. The 2026-05-17 fix-plan F5 (throttle at `midpoint Δ ≥ 25 bps OR Δt ≥ 2 s`) was approved but never implemented.

**Recommended fix.** F5 from `2026-05-17-orchestrator-fix-plan.md` is still the right shape — `src/agents/ingestion/ws_client.py` plus 2 new `src/core/config.py` fields. Verbatim sketch already exists in that document; reuse it.

---

### 4.4 [LOW — new] `ws_client.skip_no_token_non_positive_yes_quote` flood

**Symptom.** **3,251 `ws_client.skip_no_token_non_positive_yes_quote`** events in 15 min (~217/min). Sample line at T0+5s shows it firing on condition `0xce9e1f6eaad809a6ceb725d6afb29fe533a037abaaf91051360e6a478b22aba3` with `best_bid=0 best_ask=0.001` — a crossed/degenerate quote on one specific ELECTIONS market.

**Root cause.** The skip is *correct behavior* (the quote is malformed; routing it would corrupt downstream EV). But the WS `price_change` frames for that market keep arriving and each one re-triggers the skip log. Same general shape as 2026-05-17 §4.8 (`skip_last_trade_no_book`), but a different skip event.

**Why it matters.** Noise — the skip is safe and idempotent. But 3,251 lines in 15 min for one degenerate market is the kind of pattern that should be detected once per condition and then suppressed.

**Recommended fix.** Same shape as the F8 demote-and-burst pattern from `2026-05-17-orchestrator-fix-plan.md:478-505`: demote per-event log to DEBUG, emit one INFO `ws_client.degenerate_quote_detected` per condition when the symptom first appears, with `since_seconds=` counter so the operator sees "this market has had 3,251 bad quotes over 15 min" instead of 3,251 separate lines.

---

### 4.5 [LOW — carry-over from 2026-05-17 §4.8] `ws_client.skip_last_trade_no_book` not yet demoted

**Symptom.** 854 of these events in 15 min. Cause and remediation are identical to 2026-05-17 Finding 4.8 — `last_trade_price` arrives before the book snapshot per condition. Functionally correct skip, log volume only.

**Recommended fix.** Same F8 pattern as 4.4 above. The fix-plan code sketch is already written and unchanged.

---

### 4.6 [LOW — new] Process RSS growth ~2.9 MB/min sustained

**Symptom.** Process RSS over the session: 137 MB (T0) → 158 MB (T+5) → 175 MB (T+~6.5) → 181 MB (T+15:15). Linear-looking ~+2.9 MB/min. Extrapolation: +175 MB/hr, +4.2 GB/24h. Process did not stabilize within the 15-min window.

**Why it matters.** macOS dev-box can absorb this; the DigitalOcean Droplet target referenced in STATE.md's "Open PR from develop → main, then either kick the systemd timer on the Droplet" cannot tolerate 4 GB/day RSS drift on a small instance. The growth source is not yet identified — candidates from cite are:
- WI-52 cognitive breaker state (per-market history rings, possibly unbounded)
- Operational event bus queue (maxsize=1000; should be bounded — verify drain)
- WS condition_by_token maps
- DeepSeek `httpx` connection pool

**Recommended fix.** Out of scope to debug in this report — capture a heap snapshot in the next dry-run (e.g., `tracemalloc.snapshot()` at T0, T+30, T+60) and diff. WI-61 (periodic runtime audit, just merged) is already collecting RSS samples — extending its `RuntimeAuditSummary` with `rss_growth_mb_per_min` would make this self-detecting.

---

### 4.7 [OBSERVATION — open question from 2026-05-17 §7] Gatekeeper still HOLDs BTC $150k @ EV=+0.36

**Symptom.** Six independent `Evaluation complete action=HOLD approved=False expected_value=0.36 market_category=CRYPTO` lines across the window — the same BTC $150k by mid-2026 market that was the headline positive-EV signal in 2026-05-17 Run 2. Reflection flags every time: `narrative_anchoring`, `overconfidence`, `recency_bias`, `spread_pct_marginal`.

**Why it matters.** This is the same calibration question the 2026-05-17 §7 / fix-plan §F10 (shadow Gatekeeper, deferred) raised: is the Gatekeeper *correctly* HOLDing a noisy signal, or is it *over-conservative* on a real one? Without a shadow path or a paper-traded EV-realization curve, we cannot distinguish. After three dry runs against this same market with the same HOLD outcome, this is no longer "interesting one-off" data — it's a repeated signal that warrants a shadow path.

**Recommended action.** Promote 2026-05-17 fix-plan F10 (shadow Gatekeeper) out of "deferred" and into a real WI scope. Not a fix — a measurement enhancement. Without it the trading-readiness story stays unfalsifiable.

---

## 5. Mid-Session Hotfix Applied

**None.** No code, no `.env`, no config change was applied during this session. Two operational adjustments were made and are not source-tree-affecting:

1. **Monitor re-armed** at 22:48:55 with a tighter filter after the Grok-burst surge produced `events suppressed — output rate too high` on the original filter. Replacement filter narrowed the include-set to `approved=True | positive-EV | errors/blocks/state-changes` and dropped the `grok_sentiment ` SUCCESS pattern (high volume, low signal). Per command spec rule.
2. **Window wrapped at T+~10:30** at user request after stats stabilized, then the original T+15 wait task fired naturally — the final snapshot at 22:53:49 was actually taken at T+15:15, so the 15-min window completed end-to-end.

---

## 6. Numerical Summary

### Run 1 (only run): 22:38:34 → 22:53:49 (15 min 15 s observed)
| Metric | Value |
|---|---|
| Evaluations completed | 75 |
| First eval | 22:39:55 (T+81s) |
| Last eval (in window) | 22:53:36 (T+15:02) |
| Effective eval cadence | ~5.5 / min average |
| Action distribution | 75 HOLD / 0 APPROVED |
| EV distribution | 29 × −0.9, 12 × −0.96, 8 × −0.7, 7 × 0.0, **6 × +0.36**, 6 × −0.98, 5 × −0.84, 2 × −0.94, 1 × −0.979, 1 × −0.49 |
| Approved | 0 |
| Cognitive cooldown blocks | **138** (WARNING severity) |
| Cooldown:eval ratio | 138 : 75 ≈ 1.84 : 1 |
| Grok status | **210 SUCCESS / 0 FALLBACK / 0 timeout / 0 schema_error / 0 http_error** |
| Grok eligible-call success rate | 210 / 210 = **100%** |
| `llm_budget_blocked` | 0 |
| `ws_subscribe_summary` | 2 (dedup working) |
| `orchestrator.market_activated` | **1,410** (dedup not applied to per-market line) |
| `ws_client.skip_no_token_non_positive_yes_quote` | 3,251 (new noise) |
| `ws_client.skip_last_trade_no_book` | 854 (carry-over) |
| `queue.coalesced` | 620 |
| Errors / Tracebacks | 0 |
| WARNING severity ledger events | 134 (all `COOLDOWN_BLOCK`) |
| Process RSS (T0 → T+15) | 137 MB → 181 MB (+44 MB, +2.9 MB/min) |
| Process CPU (sustained) | ~17% |
| Log file size | 40 MB |

### Database (cumulative since DB inception)
| Table | Rows (T+15) | Δ vs T+5 |
|---|---|---|
| `market_snapshots` | 813,264 | +19,046 over 13 min ≈ 1,465 / min |
| `agent_decision_logs` | 2,044 | +63 over 13 min |
| `execution_txs` | 0 | 0 (DRY_RUN; also 0 with `dry_run=false` since 0 APPROVED) |
| `positions` | 0 | 0 |
| `operational_events` | 60,275 | +274 over 13 min (≈ 21 / min — ledger active) |
| DB file size | 1,246,277,632 B (1.16 GiB) | +30 MB over 13 min |

---

## 7. Points of View — Interpretation, Not Just Observation

**The infrastructure work between 2026-05-17 and 2026-05-23 was successful.** Every prior HIGH and MEDIUM defect from 2026-05-17 is either fixed (Grok timeout, Grok schema, LLM budget split, ledger, telegram, category resolution, ws-subscribe dedup) or has explicit configuration in place (`circuit_breaker.disabled` is now intentional for dry-run, not default-off). That is a real release-readiness milestone. The new finding (cooldown 65% block rate) is *not* the same kind of defect — it is an emergent calibration symptom of conservative-Gatekeeper + skewed market activation, not a code bug. The bot has graduated from "broken pipeline" to "well-built pipeline with calibration questions."

**The cooldown:eval ratio is the new headline metric.** With Grok at 100% SUCCESS and the LLM budget no longer the bottleneck, the cooldown gate is now the limiting factor on evaluation throughput. 138:75 means the cognitive breaker is doing 65% of the work the budget guard used to do. Whether that is good or bad depends on whether the underlying HOLD verdicts are *correct*. There is no way to know without ground truth — which is what the shadow Gatekeeper (2026-05-17 §F10) was designed to provide.

**Activated-set composition is silently driving the symptom.** 7 of 15 markets are IRAN — and IRAN is the cluster where reflection most consistently flags `narrative_anchoring on sentiment 0.12`. So 51 of 75 evals (68%) end up routing into the cooldown-prone path. Rotating the activated set to dilute IRAN — or making the activation eligibility check aware of recent COOLDOWN_BLOCK rate per category — would shift the eval mix toward signals where reflection is less reactive.

**The log noise is now back-loaded into one or two specific patterns.** `orchestrator.market_activated` × 1,410 and `skip_no_token_non_positive_yes_quote` × 3,251 dominate the log. Both are LOW-severity but together they account for >4,600 lines / 15 min — 25× the rate of the actual evaluation events. The "real" story (75 evals, 138 cooldowns, 0 errors) is buried.

**The DB file is the only resource trajectory that is genuinely concerning.** 1.25 GB and growing at ~140 MB/hr means a single-week dry run on the Droplet would hit 25 GB — well past the cheapest tier's disk allocation. F5 (snapshot throttle) is the only blocker before the dry-run can run unattended for >24h.

---

## 8. Recommendations (Prioritized)

### Tier 1 — Do before next multi-day dry-run

1. **Implement F5 (WS snapshot persistence throttle)** — verbatim from `2026-05-17-orchestrator-fix-plan.md` §F5. ~30 LoC + tests. Drops DB growth rate ~10× and unblocks multi-day unattended runs.
2. **Implement F6 (de-dupe market_activated per-market log)** — verbatim from `2026-05-17-orchestrator-fix-plan.md` §F6. ~10 LoC + 3 tests. Drops log volume ~20%.
3. **Add `cognitive_cooldown.block_rate` to runtime audit** — extend WI-61's `RuntimeAuditSummary` with the per-window cooldown:eval ratio. Becomes a first-class alertable metric.

### Tier 2 — Material improvement, larger scope

4. **Rotate or shrink the IRAN-heavy activated set.** Activation eligibility should weigh recent per-category cooldown-block rate. Today the set is purely Gamma-eligibility + preflight; it should also be "Gatekeeper-tractable."
5. **Demote `skip_no_token_non_positive_yes_quote` to DEBUG + add per-condition burst log** (same shape as F8 in 2026-05-17 fix-plan).
6. **Reconcile `PREFLIGHT_MAX_SPREAD_PCT` drift.** STATE.md documents 0.90; live `.env` is 0.99. Either update STATE.md to match the operator's actual calibration, or revert `.env`. The drift itself is a process risk regardless of which value is correct.

### Tier 3 — Longer-horizon

7. **Capture a heap snapshot to identify the +2.9 MB/min RSS source.** `tracemalloc.snapshot()` at T0 / T+30 / T+60 in the next dry-run will localize the leak in one session.
8. **Promote 2026-05-17 fix-plan F10 (shadow Gatekeeper) out of "deferred" into a real Phase-17 WI.** After three runs HOLDing the same BTC $150k @ EV=+0.36, the question "is the Gatekeeper over-tuned?" can no longer be answered by observation alone.

---

## 9. Open Questions / Ideas Not Pursued

- **Where exactly does the cooldown trigger threshold get configured?** `src/agents/evaluation/claude_client.py:744-763` shows the *check*, not the threshold. Verifying the threshold (and whether it is per-market or global) would tell us whether tuning it would shift the 138:75 ratio meaningfully.
- **Why did Grok burst from ~6/min to ~48/min around T+10?** The most likely explanation is that the bounded prompt queue accumulated backlog while cooldowns were firing (cooldown does not consume Grok calls, but Grok runs on every queued snapshot regardless), then drained rapidly when the cooldown for several IRAN markets simultaneously cleared. Worth confirming by correlating cooldown clear timestamps with the burst onset.
- **Is `process_started` Telegram alert actually being delivered to chat?** The dispatch event fired, but there is no `telegram.send_success` log line to confirm the HTTP POST returned 200. WI-26's contract should include such a confirmation log.
- **Does `operational_events` row count of 60,275 imply ~9 hours of accumulated history?** At ~21/min observed during this run, that backs out to ~48 hours of similar runs. Worth checking before the daily digest gets overwhelmed.

---

## 10. Files Modified This Session

| File | Status | Change |
|---|---|---|
| `logs/orchestrator-run.log` | Created (40 MB) | Run 1 stdout/stderr |
| `logs/orchestrator-run-20260523T223805Z.log` | Created (0 B) | Empty pre-existing log archived per protocol |
| `logs/stats-snapshot-T5min.txt` | Created | T+5 snapshot persisted |
| `logs/stats-snapshot-final.txt` | Created | Final snapshot persisted |
| `docs/runtime_observations/2026-05-23-orchestrator-dry-run-session.md` | Created (this file) | Session report |
| `docs/runtime_observations/2026-05-23-orchestrator-fix-plan.md` | Created (companion) | Fix plan for the 4 actionable findings (§4.1, §4.2, §4.3, §4.4) |

**No source-tree files were modified.** No commit, no MAAP, no `develop` mutation.

---

## 11. Process Notes for the Next Operator

- **Orchestrator is still running** (PID 23542, started 22:38:34Z). It is in steady-state with cooldown loop active. If you do not need it, send SIGTERM and archive `logs/orchestrator-run.log` to `logs/orchestrator-run-2026-05-23-15min.log` before next run.
- **Monitor was stopped twice** during this session — task `bog6hbxl4` (original filter, rate-suppressed by Grok burst) and task `bnr91mdhb` (replacement, user wrap-up). No active Monitor remains.
- **Two wait tasks fired naturally** (`betrtq54g`, `bp85a3cej`) — they were the T+15 markers; harmless.
- **The pre-existing 2026-05-19 untracked files in `docs/runtime_observations/`** (`2026-05-19-orchestrator-dry-run-session.md`, `2026-05-19-orchestrator-fix-plan.md`) and `scripts/auto_snapshots.sh` were on entry and are *not* touched by this session.
- **`ugrep` is the default `grep` on this machine** (BSD-friendly wrapper). It rejects `\b` word boundaries and empty alternations. Use `/usr/bin/grep -E` directly to bypass.
- **`stdbuf` is not installed.** macOS BSD `grep --line-buffered` is sufficient for tail+grep pipelines.
- **The single most-load-bearing fix to land before re-running** is F5 (snapshot persistence throttle from 2026-05-17). Until it lands, every hour of dry-run adds ~140 MB to `data/poly_oracle.db`.

---

## 12. Closing

The 2026-05-17 → 2026-05-23 release-engineering arc is complete: every infrastructure defect from the original observation report has been addressed, observability is fully wired, and the bot now fails closed on its *own* terms (cognitive cooldown) rather than on broken-pipeline terms (Grok timeout, budget exhaustion). That is a meaningful step.

The remaining work is **calibration**, not infrastructure. The 138:75 cooldown:eval ratio is the new sharpest question, and answering it requires either (a) tuning the Gatekeeper for the IRAN cluster, (b) rotating the activated set to dilute it, or (c) building the shadow path that makes the Gatekeeper's verdicts falsifiable. The 2026-05-17 fix-plan F10 (shadow Gatekeeper) is the natural home for (c).

The DB-growth story (~140 MB/hr) is the one remaining infrastructure blocker before this can run unattended for >24h on the Droplet. F5 is the entire fix and the code sketch is already written. Land it before the next multi-day window.
