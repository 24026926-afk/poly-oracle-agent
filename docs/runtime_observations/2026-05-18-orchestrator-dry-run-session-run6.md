# Orchestrator Dry-Run Session — Run 6

## 1. Frontmatter

| Field | Value |
|---|---|
| **Author** | Qwen Code |
| **Date** | 2026-05-18 |
| **Branch** | `develop` @ `10d78b4` |
| **Runtime** | `.venv/bin/python 3.14.2` |
| **Mode** | `DRY_RUN=true`, `LLM_PROVIDER=deepseek`, `GROK_LIVE_ENABLED=True` |
| **Window** | 40 min (T0=23:07:53 UTC → T+40=23:47:54 UTC) |
| **Scope** | Runtime validation of Run 5 calibration with preflight disabled |

## 2. Executive Summary

- **15 unique markets activated** across 4 categories (7 POLITICS, 3 CULTURE, 3 IRAN, 1 CRYPTO) rotating on a ~10s cadence. 82 of 100 Gamma markets eligible.
- **209 evaluations completed** (5.2/min), all `action=HOLD`, `approved=False`. Zero orders, zero positions, zero safety violations.
- **Gatekeeper 100% enforced.** 206/209 reflection REJECTED, 3/209 APPROVED (all CULTURE, EV=0.0, blocked on MAX_SPREAD). DeepSeek EV arithmetic errors caught 100% by reflection.
- **Grok:** 246 SUCCESS (99.6%), 144 SKIPPED_CATEGORY (CULTURE), 1 timeout, 0 schema errors, 0 HTTP errors.
- **0 budget blocks, 0 WS disconnects, 0 tracebacks, 0 SQLite lock errors.** Cleanest runtime to date.

## 3. Session Timeline (UTC)

| Time | Event |
|---|---|
| 23:07:53 | Orchestrator started (PID 71073). `circuit_breaker.disabled` only |
| 23:07:53 | Gamma: 100 active, 82 eligible, 15 activated |
| 23:08:07 | First eval (CRYPTO, EV=+0.36, HOLD, reflection REJECTED) |
| 23:08:54 | First reflection APPROVED (CULTURE, EV=0.0, Gatekeeper blocked) |
| 23:11:23 | Grok timeout (IRAN, 8s limit, remaining_budget=24.0) |
| 23:29:58 | Second reflection APPROVED (CULTURE) |
| 23:39:21 | Third reflection APPROVED (CULTURE) |
| 23:47:54 | Final snapshot (T+40). Orchestrator still running. |

## 4. Environment & Configuration

### Loaded `.env` (secrets redacted)

| Variable | Value |
|---|---|
| `DRY_RUN` | `true` |
| `LLM_PROVIDER` | `deepseek` |
| `DEEPSEEK_MODEL` | `deepseek-chat` |
| `GROK_MODEL` | `grok-4.20-0309-non-reasoning` |
| `GROK_LIVE_ENABLED` | `True` |
| `ENABLE_MARKET_DISCOVERY_PREFLIGHT` | `false` (env override) |
| `PREFLIGHT_MAX_SPREAD_PCT` | `0.99` |
| `LLM_MARKET_HOURLY_CALL_LIMIT` | `120` |
| `LLM_DAILY_TOKEN_LIMIT` | `10000000` |
| `ENABLE_CIRCUIT_BREAKER` | `false` |

### Disabled Subsystems

| Subsystem | Status |
|---|---|
| `circuit_breaker` | disabled |
| `telegram` | enabled |
| `operational_alerts` | enabled |
| `operational_event_ledger` | enabled |

### Markets Activated (15 unique, rotating ~10s)

| Category | Count |
|---|---|
| POLITICS | 7 |
| CULTURE | 3 |
| IRAN | 3 |
| CRYPTO | 1 |

## 5. Findings (Ranked by Severity)

### M1 — CULTURE Markets Consume 24% of Eval Budget with Zero Grok Signal

**Symptom:** 52/209 evals (24%) allocated to CULTURE. All 144 Grok SKIPPED_CATEGORY map to CULTURE. CULTURE markets produce EV=0.0 (neutral fallback, extreme spreads ~98-99.8%).

**Root cause:** `GROK_ELIGIBLE_CATEGORIES` frozenset excludes CULTURE (`src/schemas/llm.py`). Orchestrator activates CULTURE markets but they receive no directional signal.

**Why it matters:** 24% of LLM budget wasted on markets that structurally cannot produce actionable signals. ~1.2 evals/min burned on CULTURE.

**Recommended fix:** Exclude CULTURE from activation via category-aware evaluation cadence (`ENABLE_CATEGORY_EVALUATION_CADENCE=true` already set; increase CULTURE interval to 300s).

**File citations:** `src/schemas/llm.py:GROK_ELIGIBLE_CATEGORIES`, `src/agents/context/aggregator.py`

### M2 — Market Rotation Every ~10s Causes Redundant Re-Activations

**Symptom:** 3,630 `orchestrator.market_activated` events in 40 min = 90/min. Same 15 markets re-activated every ~10s.

**Root cause:** Orchestrator rotation loop re-activates all eligible markets each cycle, faster than evaluation cadence (30s Grok, 120s non-Grok).

**Why it matters:** 3,630 activation events inflate operational event ledger (54,153 total events in 40 min). ~67 events/min of low-signal data.

**Recommended fix:** Suppress re-activation logging for already-active markets. Tie rotation interval to evaluation cadence.

**File citations:** `src/orchestrator.py` (market rotation loop)

### L1 — Single Grok Timeout on IRAN Market

**Symptom:** One `grok_sentiment_timeout` at T+3:30 on IRAN market. `attempt=0`, `remaining_budget=24.0`.

**Root cause:** xAI API latency exceeded `GROK_TIMEOUT_SECONDS=8.0`. Single occurrence.

**Why it matters:** Minor. Handled gracefully with neutral fallback.

### L2 — 3 Reflection APPROVED but Gatekeeper Still Blocked

**Symptom:** 3 CULTURE evals received `reflection_verdict=APPROVED` but `action=HOLD`. Gatekeeper blocked on MAX_SPREAD.

**Root cause:** Reflection assesses reasoning quality; Gatekeeper assesses tradability. Two-layer defense working correctly.

### L3 — DeepSeek 100% EV Arithmetic Error Rate

**Symptom:** All 209 evals show reflection flags for EV/Kelly/spread errors. 100% reflection rejection rate.

**Root cause:** DeepSeek-chat (V3) not optimized for arithmetic reasoning. Reflection catches all errors.

**Why it matters:** No false-positive approvals. But DeepSeek primary evals are never directly actionable.

## 6. Mid-Session Hotfix Applied

**None.** No code or config changes applied.

Note: `ENABLE_MARKET_DISCOVERY_PREFLIGHT=false` was set via env var override because shell env had `true` from prior session. `.env` file already contains `false`.

## 7. Numerical Summary

### Run 6 (T+40)

| Metric | Value |
|---|---|
| Evaluations | 209 |
| Eval rate | 5.2/min |
| HOLD | 209 (100%) |
| APPROVED | 0 |
| Non-zero EV | 161/209 (77%) |
| Grok SUCCESS | 246 (99.6%) |
| Grok SKIPPED | 144 (CULTURE) |
| Grok timeout | 1 |
| Budget blocks | 0 |
| WS disconnects | 0 |
| Tracebacks | 0 |
| Errors | 0 |
| Operational events | 54,153 |
| DB market_snapshots | 540,655 |
| DB agent_decision_logs | 1,164 |
| DB positions | 0 |

### Cross-Run Comparison

| Metric | Run 1 | Run 2 | Run 4 | Run 5 | Run 6 |
|---|---|---|---|---|---|
| Duration | 60m | 60m | 60m | 64m | 40m |
| Evaluations | 228 | 69 | 95 | ~100 | 209 |
| APPROVED | 0 | 0 | 0 | 0 | 0 |
| Grok success | 99.3% | 100% | 99.6% | 99.6% | 99.6% |
| Budget blocks | 48 | 0 | 2 | 4 | 0 |
| WS disconnects | 0 | 4 | 0 | 0 | 0 |
| Errors | 0 | 0 | 0 | 0 | 0 |

## 8. Points of View

1. **Run 6 is the cleanest runtime to date.** Zero budget blocks, zero WS disconnects, zero errors, zero tracebacks. Run 5 calibration holds under extended observation.

2. **System is structurally blocked from trading, not broken.** All 209 evals correctly HOLD: spreads 98-99.8% (far above 1.5% MAX_SPREAD), DeepSeek EV unreliable (100% error rate), no positive EV survives reflection + Gatekeeper. Correct fail-closed behavior.

3. **CULTURE markets are dead weight.** 24% of eval budget, zero Grok signal, zero EV. Should be deprioritized.

4. **Market rotation too aggressive.** 90 activations/min for 15 markets is 6x redundant. Should tie to evaluation cadence.

5. **DeepSeek is a research tool, not a trading tool.** 100% reflection rejection rate means every eval is corrected. Fine for paper-trading validation, unacceptable for live trading latency.

## 9. Recommendations

### Tier 1 (Config-only, no MAAP)

- **R1:** Increase CULTURE evaluation interval to 300s via category cadence. Reduces wasted eval budget by ~20%.
- **R2:** Reduce market rotation interval from ~10s to match evaluation cadence (30s/120s). Reduces operational event volume by ~80%.

### Tier 2 (Code change, MAAP-required)

- **R3:** Add CULTURE to `GROK_ELIGIBLE_CATEGORIES` if sentiment data exists. Test single market first.
- **R4:** Add pre-reflection arithmetic validator for DeepSeek to catch EV/Kelly errors before reflection call.

### Tier 3 (Strategic)

- **R5:** Test Claude Sonnet 4 as primary provider. Compare EV accuracy vs DeepSeek.
- **R6:** Implement spread-based activation pre-filter: only activate markets with spread < 50%.

## 10. Open Questions / Ideas Not Pursued

- Should preflight be re-enabled with a higher spread threshold (0.95) to filter extreme-spread markets at ingestion rather than at evaluation?
- Is the 10s market rotation interval configurable, or hardcoded in the orchestrator loop?
- Would disabling CULTURE entirely from activation improve eval throughput for Grok-eligible markets?
- Should the daily ops digest be generated to compare against manual daily notes?

## 11. Files Modified This Session

| File | Action |
|---|---|
| `docs/runtime_observations/2026-05-18-orchestrator-dry-run-session-run6.md` | Created |
| `logs/stats-snapshot-T5min.txt` | Created |
| `logs/stats-snapshot-T15min.txt` | Created |
| `logs/stats-snapshot-T30min.txt` | Created |
| `logs/orchestrator-run.log` | Live (63 MB) |
| `03_Daily/2026-05-18.md` | Appended |

## 12. Process Notes for the Next Operator + Closing

- **Orchestrator PID 71073 is still running** at session end. SIGTERM before applying any fixes.
- **No code or config changes applied.** This was observation-only.
- **Preflight was disabled via env var override** (`ENABLE_MARKET_DISCOVERY_PREFLIGHT=false` in shell). The `.env` file already has `false`. Shell env had stale `true` from prior session.
- **Next step:** Review findings M1 (CULTURE budget waste) and M2 (rotation redundancy). Apply R1+R2 (config-only) first, then consider R3+R4 (MAAP-gated).
- **Key insight:** The system is stable and safe. The constraint is market liquidity (extreme spreads), not system correctness. All safety gates function correctly.
