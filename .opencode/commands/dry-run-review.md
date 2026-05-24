---
description: "Launch python3 -m src.orchestrator, monitor the live dry-run, then produce an observation report + fix plan in the exact format used on 2026-05-17."
---

Runs a structured live dry-run review of the orchestrator using the same workflow as the 2026-05-17 session. Launches `python3 -m src.orchestrator` under `.venv`, monitors runtime events, diagnoses anomalies (out-of-band when possible), surfaces optional mid-session hotfixes for explicit user approval, and produces two deliverables in `docs/runtime_observations/` matching the canonical 2026-05-17 templates.

Usage: `/dry-run-review [duration_minutes]`
- `duration_minutes` (optional, default 60) — wall-clock observation window after orchestrator startup.

Canonical templates (use as exact structural reference; do NOT diverge from their section ordering or headings):
- Observations report → `docs/runtime_observations/2026-05-17-orchestrator-dry-run-session.md` (12 sections)
- Fix plan → `docs/runtime_observations/2026-05-17-orchestrator-fix-plan.md` (14 sections)

Output deliverables (always timestamped with today's UTC date):
- `docs/runtime_observations/{YYYY-MM-DD}-orchestrator-dry-run-session.md`
- `docs/runtime_observations/{YYYY-MM-DD}-orchestrator-fix-plan.md` (only when ≥1 HIGH or MEDIUM finding surfaces; otherwise add a one-line note in the observations report explaining no plan was needed)

Steps:

1. **Pre-flight context hydration.** In parallel:
   - Read `STATE.md` (current phase/WI, known gaps).
   - Read today's daily note: `~/documents/integration_task/03_Daily/{today}.md` (and yesterday's if today is empty).
   - Read `.env` to confirm `DRY_RUN=true`, `LLM_PROVIDER`, `GROK_LIVE_ENABLED`, `GROK_MOCKED`, `DATABASE_URL`. Redact secrets in any user-facing output.
   - Check for an already-running orchestrator: `pgrep -f "python -m src.orchestrator"`. If found, ABORT with a message asking the user whether to stop it or attach to its log; do not kill silently.
   - Verify `.venv/bin/python --version` exists (Python 3.12+).
   - Verify `data/poly_oracle.db` exists. If absent, confirm with user then run `.venv/bin/python -m alembic upgrade head` and only proceed if migrations succeed.
   - Confirm `logs/` directory exists; create it if not.
   - If `DRY_RUN` is anything other than `true`, ABORT with: "Refusing to run review under DRY_RUN=false. This command is for paper-trading observation only."
   - If `duration_minutes < 5`, ABORT with: "Minimum review window is 5 minutes to allow at least one LLM budget cycle to be observed."

2. **Archive any pre-existing live log** before overwriting:
   - If `logs/orchestrator-run.log` exists, move it to `logs/orchestrator-run-{ISO timestamp}.log`.
   - Report its size and the new archive path to the user.

3. **Launch orchestrator in background, detached.** Use this exact command shape:
   ```bash
   nohup .venv/bin/python -m src.orchestrator > logs/orchestrator-run.log 2>&1 &
   ```
   (the canonical command is `python3 -m src.orchestrator`; under `.venv` this resolves to `.venv/bin/python -m src.orchestrator`).
   Capture PID. Wait for one of these startup markers via a Monitor `until` loop (NOT a leading sleep): `ws_subscribe_summary`, `gamma.active_markets_fetched`, `orchestrator.market_activated`, `Traceback`, `ERROR`. If a `Traceback` or `ERROR` appears in startup, capture the trace and ABORT with it shown to the user.

4. **Report startup posture** in one short message to the user:
   - PID, start time (UTC), `duration_minutes` window.
   - Which observability subsystems are DISABLED (`telegram.disabled`, `circuit_breaker.disabled`, `operational_alerts.disabled`, `operational_event_ledger.disabled`). Each disabled subsystem is a candidate finding.
   - Gamma fetch totals (`active`, `eligible`, `activated`, `ttr_fail`).
   - LLM provider and model in use.

5. **Arm a persistent Monitor** on `logs/orchestrator-run.log` filtered to state-transition events only. Filter MUST exclude (high-volume, low-signal): `ws_client.raw_message`, `snapshot_route_debug`, `market_snapshot_inserted`, `ws_client.snapshot_enqueued`, `bankroll.exposure_queried`, `queue.coalesced`, `skip_last_trade_no_book`, `orchestrator.market_activated`, `ws_subscribe_summary`, `market_category_resolved`, `SKIPPED_CATEGORY`, `grok_sentiment_timeout`. Filter MUST include: `Evaluation complete`, `grok_sentiment ` (success/fallback variants), `grok_sentiment_http_error`, `grok_sentiment_schema_error`, `llm_budget_blocked`, `circuit_breaker.*`, `decision.approved|rejected|persisted`, `execution.*`, `dry_run.broadcast`, `order_*`, `ws_client.(disconnected|reconnect|stale|crossed_book)`, `orchestrator.(stopped|shutdown|halted|panic)`, `Traceback`, `ERROR\b`.
   If the monitor reports "events suppressed — output rate too high", STOP and re-arm with a tighter filter; never let the user be drowned in low-signal lines.

6. **Periodic stats snapshots.** At T+5, T+15, T+30 minutes and at T+`{duration_minutes}` (final), pull a structured snapshot using an ANSI-strip helper plus `grep`/`awk` (do not rely on `print` — use `structlog`-readable log lines). Capture at minimum:
   - process PID / uptime / RSS / CPU,
   - `Evaluation complete` total count,
   - action distribution,
   - market_category distribution,
   - expected_value distribution (call out non-zeros),
   - Grok status histogram + reason histogram,
   - grok_sentiment_timeout, grok_sentiment_schema_error, grok_sentiment_http_error counts,
   - llm_budget_blocked count,
   - queue.coalesced count,
   - ws_subscribe_summary count,
   - Tracebacks/ERROR count,
   - log file size,
   - DB file size + row counts for `market_snapshots`, `agent_decision_logs`, `execution_txs`, `positions`, `operational_events`.
   Print each snapshot as a short table to the user; persist the raw text to `logs/stats-snapshot-T{Tn}min.txt`.

7. **React only to MATERIAL state transitions** as the Monitor emits them. "Material" = worth interrupting the user for:
   - First Grok `status=SUCCESS` after a run of failures.
   - First non-zero EV in the session.
   - First `approved=True`.
   - First `llm_budget_blocked` (record exact T+ delta from startup).
   - Any `grok_sentiment_http_error` (especially 429 / 5xx).
   - Any `ws_client.disconnected` / `reconnect` / `stale`.
   - Any `Traceback` or `ERROR`.
   - Schema validation failures producing fallbacks at a sustained >5% rate.
   For each: `grep -B2 -A2` the surrounding lines, show the user, and append a candidate finding to a running scratch list. Do NOT modify code or config in this step.

8. **Out-of-band diagnostics when warranted.** If a finding likely has a root cause testable without stopping the orchestrator (e.g., suspected API latency, bad key, wrong endpoint, model unavailable), run a one-off `curl` directly against the external service. Templates (verified by the 2026-05-17 session):
   - xAI Grok ping: `curl -sS --max-time 10 -X POST https://api.x.ai/v1/chat/completions -H "Authorization: Bearer $GROK_KEY" -H "Content-Type: application/json" -d '{"model":"<model>","messages":[{"role":"system","content":"Reply OK only"},{"role":"user","content":"ping"}],"temperature":0.0,"max_tokens":4}'`
   - xAI available models: `curl -sS --max-time 10 https://api.x.ai/v1/models -H "Authorization: Bearer $GROK_KEY"`
   - DeepSeek probe: equivalent against `https://api.deepseek.com/anthropic/v1/messages`.
   - Polymarket Gamma probe if WS issues suspected.
   Report latency, status codes, and response shape back to the user. Diagnosis findings go into the report; they do NOT trigger any source-tree change automatically.

9. **Decision gate at any anomaly.** When step 7 or 8 surfaces a real defect, present `AskUserQuestion` with three options:
   - Diagnose without stopping (default, lowest risk)
   - Stop, apply hotfix, restart (requires explicit named change scope)
   - Continue monitoring untouched
   NEVER apply automatic code modification. If the user picks "Stop, apply hotfix, restart":
   - Stop the orchestrator (SIGTERM, then `until ! kill -0 PID` Monitor loop).
   - Stop the Monitor.
   - Apply ONLY the explicitly-named hotfix (smallest possible change). Any change under `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, or `src/backtest_runner.py` is **MAAP-gated**; remind the user that the commit requires MAAP review.
   - Archive the pre-fix log to `logs/orchestrator-run-pre-{slug}-fix.log`.
   - Relaunch the orchestrator (same command shape as step 3), re-arm the Monitor (step 5), and resume observation with a *new* T0. Track both phases independently in the final report.

10. **At the end of the observation window (T+`duration_minutes`):**
    - Stop the Monitor.
    - Capture a final stats snapshot.
    - Leave the orchestrator RUNNING by default. Only stop it if the user explicitly asked for cleanup-on-exit (e.g., "finish everything in N min").

11. **Generate the observations report.** Write `docs/runtime_observations/{today}-orchestrator-dry-run-session.md` using the EXACT 12-section structure of `docs/runtime_observations/2026-05-17-orchestrator-dry-run-session.md`:
    1. Frontmatter block (Author / Date / Branch / Runtime / Mode / Window / Scope)
    2. Executive Summary — top 3 structural constraints + net trading output
    3. Session Timeline (UTC) — every key event with timestamps
    4. Environment & Configuration — loaded `.env` (secrets redacted), disabled subsystems table, active runtime knobs table, markets activated
    5. Findings (Ranked by Severity) — HIGH → MEDIUM → LOW, each with Symptom / Root cause (with `file:line` citations) / Why it matters / Recommended fix
    6. Mid-Session Hotfix Applied (if any) — diff, rationale, what was NOT touched, validation
    7. Numerical Summary — Run 1 / Run 2 / DB cumulative tables
    8. Points of View — interpretation, not just observation
    9. Recommendations — Tier 1 / Tier 2 / Tier 3
    10. Open Questions / Ideas not pursued
    11. Files Modified This Session
    12. Process Notes for the Next Operator + Closing
    Every finding MUST cite real `file:line` for root cause.

12. **Generate the fix plan (CONDITIONAL).** If the observations report has ≥1 HIGH or MEDIUM finding, also write `docs/runtime_observations/{today}-orchestrator-fix-plan.md` using the EXACT 14-section structure of `docs/runtime_observations/2026-05-17-orchestrator-fix-plan.md`:
    1. Why a separate planning document
    2. Newly observed signals since report was written (if any)
    3. Goals (prioritized)
    4. Constraints (MAAP, atomicity, Decimal, no `dry_run` weakening, no Gatekeeper bypass)
    5. Fix inventory — one entry per actionable finding, each with: Severity / MAAP req / Blast radius / Why / What / Files / Code sketch (pseudocode only) / Tests / Risk / Validation
    6. Execution sequence — atomic commits, dependency-ordered, MAAP flagged per commit
    7. Test strategy — per-commit + cumulative + coverage ≥80%
    8. Post-implementation validation — concrete "Metric / Target / Was" table
    9. Rollback strategy
    10. Open questions for user sign-off
    11. Timeline estimate
    12. What could go wrong
    13. Definition of Done
    14. Files-touched matrix with LOC estimates
    The plan MUST NOT include any committed code. Every change touching `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, `src/backtest_runner.py` MUST be marked MAAP-required.

13. **Append a session summary to today's daily note** at `~/documents/integration_task/03_Daily/{today}.md`, using the CLAUDE.md Session End format:
    ```
    ## [HH:MM] Session Summary
    - Agent: Claude Code
    - Active WI: <or "none — runtime review">
    - Actions taken: <bullets>
    - Files created/modified: <bullets with vault-relative paths>
    - Blockers or decisions: <bullets>
    - Next: <single-sentence next-step recommendation>
    ```

14. **Final report to the user.** Print, in a single short message:
    - PID + uptime + RSS of the still-running orchestrator (or "stopped" if cleanup-on-exit was requested).
    - Paths to the two deliverables (observations + fix plan, if generated).
    - Top 3 findings by severity, one line each.
    - Single-sentence recommended next step (e.g., "Review fix plan Section 10 open questions before executing F1").

Rules:
- NEVER apply any source-tree change without explicit user approval (step 9 gate).
- NEVER commit anything as part of this command. The user runs `/maap` and `git` commands separately.
- NEVER kill an in-flight orchestrator without confirming (step 1 check).
- NEVER overwrite `logs/orchestrator-run.log` without archiving (step 2 rule).
- NEVER write findings as "code that doesn't exist" — every recommendation must cite a real `file:line`.
- NEVER use Bash `sleep` with long leading delays; use Monitor `until` loops or `run_in_background` with timeouts ≤ 600s.
- NEVER use `print()` in any generated code sketch — use `structlog` (per CLAUDE.md).
- NEVER use raw `float` in any money/EV/Decimal path of a code sketch.
- NEVER suggest a change that weakens `DRY_RUN`, bypasses `LLMEvaluationResponse`, or skips Gatekeeper.
- The observations report and fix plan ARE the deliverables. Do not skip them or substitute an inline summary.
- The next operator should be able to reproduce the entire workflow by reading the two deliverables; write them with that in mind.
