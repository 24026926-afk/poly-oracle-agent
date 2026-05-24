---
description: "Autonomous 72-hour server runtime review. Aggregates WI-61 audit artifacts via deterministic Python, then produces a 12-section observation report and conditional 14-section fix plan. Runs headlessly via systemd."
---

Autonomous 72-hour server runtime review of the Poly Oracle Agent. Aggregates WI-61 periodic audit artifacts using deterministic Python arithmetic (never LLM computation), reads optional LLM advisory trends, and produces two deliverables in `docs/runtime_observations/` matching the canonical 2026-05-17 templates.

Usage: `/server-runtime-review`
- No arguments. The lookback window is fixed at 72 hours.
- Designed to run headlessly via systemd timer (openclaude -p "/server-runtime-review").

Canonical templates (use as exact structural reference; do NOT diverge from their section ordering or headings):
- Observations report → `docs/runtime_observations/2026-05-17-orchestrator-dry-run-session.md` (12 sections)
- Fix plan → `docs/runtime_observations/2026-05-17-orchestrator-fix-plan.md` (14 sections)

Output deliverables (always timestamped with today's UTC date):
- `docs/runtime_observations/{YYYY-MM-DD}-server-runtime-session.md`
- `docs/runtime_observations/{YYYY-MM-DD}-server-fix-plan.md` (only when `fix_plan_required=true` in aggregator output; otherwise add a one-line note in the observations report explaining no plan was needed)

Steps:

1. **Pre-flight context hydration.** In parallel:
   - Read `STATE.md` (current phase/WI, known gaps).
   - Read today's daily note: `~/documents/integration_task/03_Daily/{today}.md` (and yesterday's if today is empty).
   - Read `.env` to confirm `DRY_RUN` posture. Redact secrets in any user-facing output.
   - Verify `docs/operations/runtime_audits/` directory exists. If absent, ABORT with: "Audit artifacts directory not found. Is WI-61 periodic audit configured on this server?"
   - Verify `docs/runtime_observations/` directory exists. If absent, create it.
   - Count JSON artifacts matching `runtime-audit-*.json` in the audits directory. If zero, ABORT with: "No audit artifacts found. Verify poly-oracle-runtime-audit.timer is active."

2. **Run the deterministic aggregator.** Execute via Bash:
   ```bash
   .venv/bin/python scripts/ops/aggregate_audits.py --hours 72 --project-root .
   ```
   - Capture the JSON output and exit code.
   - If exit code is 1 (no artifacts in window), ABORT with the error JSON detail.
   - If exit code is 2 (config error), ABORT with the error message.
   - If exit code is 0, parse the JSON summary. Store it as the canonical data source for all subsequent steps.
   - **NEVER perform arithmetic on the JSON values yourself.** All numbers are pre-computed by the aggregator using Decimal. You synthesize narrative, not compute.

3. **Read LLM advisory trends (optional).** If `docs/operations/runtime_reviews/latest.md` exists, read it to understand the most recent Moonshot/Kimi advisory sentiment. Summarize the top 3 recurring themes or concerns. If the file does not exist, note "LLM advisory review not available (reviewer disabled or not yet run)."

4. **Generate the observations report.** Write `docs/runtime_observations/{today}-server-runtime-session.md` using the EXACT 12-section structure of `docs/runtime_observations/2026-05-17-orchestrator-dry-run-session.md`:
   1. **Frontmatter block** — Author (openclaude headless) / Date / Branch / Runtime (server) / Mode (DRY_RUN posture from aggregator) / Window (72h lookback, with `window_start_utc` and `window_end_utc` from aggregator) / Scope (autonomous 72h server runtime review).
   2. **Executive Summary** — Top 3 structural observations from the 72h window. Net safety posture (safety gates, error rate, budget blocks). Decision distribution summary.
   3. **Session Timeline (UTC)** — Key events extracted from aggregator data: window start/end, any safety gate failures, error spikes, budget block patterns.
   4. **Environment & Configuration** — `dry_run_posture` from aggregator (document as context, flag only if `dry_run_changed=true`), `lookback_hours`, `scanned_files`.
   5. **Findings (Ranked by Severity)** — HIGH → MEDIUM → LOW. Each finding must cite:
      - **Symptom:** What the aggregator data shows.
      - **Root cause:** Inferred from the data pattern (cite specific aggregator fields).
      - **Why it matters:** Operational impact.
      - **Recommended fix:** Concrete action.
      Findings are derived from:
      - `critical_safety_gates > 0` → HIGH severity safety finding.
      - `total_errors > 50` → MEDIUM severity error rate finding.
      - `budget_blocks > 10` → MEDIUM severity budget pressure finding.
      - `ws_reconnects > 5` → MEDIUM connectivity finding.
      - `provider_failures > 5` → MEDIUM provider reliability finding.
      - `dry_run_changed=true` → HIGH severity posture change finding.
      - `avg_response_time_ms > 500` → LOW performance degradation finding.
      - `db_growth_bytes > 100_000_000` → LOW storage growth finding.
   6. **Mid-Session Hotfix Applied** — "None. This is a read-only autonomous review."
   7. **Numerical Summary** — Full aggregator output as a table: scanned_files, total_errors, total_warnings, budget_blocks, provider_failures, critical_safety_gates, ws_reconnects, cooldown_blocks, market_quarantines, avg_response_time_ms, max_exposure_usdc, db_growth_bytes, decision_distribution.
   8. **Points of View** — Interpretation of the 72h data. Is the bot healthy? Is it trading-paralyzed or over-active? Are safety gates working correctly?
   9. **Recommendations** — Tier 1 (immediate) / Tier 2 (next sprint) / Tier 3 (strategic).
   10. **Open Questions / Ideas not pursued** — Anything the data suggests but cannot confirm.
   11. **Files Modified This Session** — "None. Read-only review."
   12. **Process Notes for the Next Operator + Closing** — How to interpret this report, when to escalate, how to adjust thresholds.

5. **Conditional fix plan.** If `fix_plan_required=true` in the aggregator output, also write `docs/runtime_observations/{today}-server-fix-plan.md` using the EXACT 14-section structure of `docs/runtime_observations/2026-05-17-orchestrator-fix-plan.md`:
   1. Why a separate planning document
   2. Newly observed signals since report was written (if any)
   3. Goals (prioritized)
   4. Constraints (MAAP, atomicity, Decimal, no `dry_run` weakening, no Gatekeeper bypass)
   5. Fix inventory — one entry per triggered threshold, each with: Severity / MAAP req / Blast radius / Why / What / Files / Code sketch (pseudocode only) / Tests / Risk / Validation
   6. Execution sequence — atomic commits, dependency-ordered, MAAP flagged per commit
   7. Test strategy — per-commit + cumulative + coverage ≥80%
   8. Post-implementation validation — concrete "Metric / Target / Was" table
   9. Rollback strategy
   10. Open questions for user sign-off
   11. Timeline estimate
   12. What could go wrong
   13. Definition of Done
   14. Files-touched matrix with LOC estimates

   If `fix_plan_required=false`, add a one-line note in the observations report Section 5: "No fix plan generated. All metrics within acceptable thresholds."

6. **Append a session summary to today's daily note** at `~/documents/integration_task/03_Daily/{today}.md`:
   ```
   ## [HH:MM] Session Summary
   - Agent: openclaude (headless, server-side)
   - Active WI: WI-62 — Server Runtime Review
   - Actions taken: Aggregated {scanned_files} audit artifacts over 72h window
   - Files created/modified: docs/runtime_observations/{today}-server-runtime-session.md[, docs/runtime_observations/{today}-server-fix-plan.md]
   - Blockers or decisions: [any findings or "none"]
   - Next: [single-sentence recommendation based on findings]
   ```

7. **Final output.** Print a short summary to stdout (captured by systemd journal):
   - Scanned files and window.
   - Top 3 findings by severity (one line each), or "No findings."
   - Paths to generated deliverables.
   - Fix plan generated (yes/no).

Rules:
- NEVER perform arithmetic on aggregator output values. All numbers are pre-computed by Python using Decimal. The LLM synthesizes narrative only.
- NEVER modify any source code (`.py` files). This is a read-only reporting process.
- NEVER commit anything. The operator runs `/maap` and `git` commands separately.
- NEVER flag `dry_run=true` as a critical finding. Document it as context. Only flag if `dry_run_changed=true`.
- NEVER generate a Fix Plan for subjective reasons. Only when `fix_plan_required=true` in the aggregator output.
- NEVER persist secrets, wallet addresses, API keys, condition IDs, token IDs, or raw prompts in any output artifact. The aggregator scrubs its output; verify the report does not re-introduce forbidden content.
- NEVER use `print()` in any generated code sketch — use `structlog` (per QWEN.md).
- NEVER use raw `float` in any money/EV/Decimal path of a code sketch.
- NEVER suggest a change that weakens `DRY_RUN`, bypasses `LLMEvaluationResponse`, or skips Gatekeeper.
- The observations report and fix plan ARE the deliverables. Do not skip them or substitute an inline summary.
- The next operator should be able to understand the 72h server health by reading the observation report alone; write it with that in mind.
- If the aggregator returns an error (exit code 1 or 2), do NOT attempt to generate reports. ABORT and print the error to stdout for the systemd journal.
