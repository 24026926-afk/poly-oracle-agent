---
description: "Autonomous 72-hour runtime review: aggregates WI-61 audit artifacts via deterministic Python, generates observation report and conditional fix plan. Runs unattended via systemd timer."
---

Autonomous 72-hour server runtime review. Aggregates WI-61 periodic runtime audit artifacts using the deterministic Python aggregator, hydrates project context, and generates a 12-section observation report and conditional 14-section fix plan. Designed to run unattended via systemd timer with no interactive operator present.

Usage: `/server-runtime-review`

Canonical templates (use as exact structural reference):
- Observations report → `docs/runtime_observations/2026-05-17-orchestrator-dry-run-session.md` (12 sections)
- Fix plan → `docs/runtime_observations/2026-05-17-orchestrator-fix-plan.md` (14 sections)

Output deliverables (always timestamped with today's UTC date):
- `docs/runtime_observations/{YYYY-MM-DD}-server-runtime-session.md`
- `docs/runtime_observations/{YYYY-MM-DD}-server-fix-plan.md` (only when aggregator reports `fix_plan_required=true`)

Steps:

1. **Pre-flight context hydration.** In parallel:
   - Read `STATE.md` (current phase/WI, known gaps, test coverage).
   - Read `.env` to confirm `DRY_RUN=true`. Redact all secrets (API keys, wallet addresses, Telegram tokens) in any output.
   - Verify `docs/operations/runtime_audits/` directory exists and contains `runtime-audit-*.json` files.
   - Verify `.venv/bin/python` exists (Python 3.12+).
   - If `DRY_RUN` is anything other than `true`, document as context but do NOT flag as a finding.
   - If artifact directory is missing or empty, generate a minimal observation report noting "no audit artifacts available" and exit without generating a fix plan.

2. **Run the deterministic aggregator.** Execute via Bash (NEVER perform arithmetic in LLM context):
   ```bash
   .venv/bin/python scripts/ops/aggregate_audits.py --hours 72 --artifact-dir docs/operations/runtime_audits
   ```
   Capture the JSON output. If exit code is 1 and error is `no_artifacts_in_window`, generate a minimal observation report noting "no artifacts in 72-hour window" and exit.
   If exit code is non-zero for other reasons, document the error and exit.

3. **Read LLM review trends (optional).** If `docs/operations/runtime_reviews/latest.md` exists, read it and extract key findings. Apply secret redaction matching `_FORBIDDEN_CONTENT_PATTERNS` from `src/observability/runtime_audit.py`. If the file does not exist, proceed without LLM review trend section.

4. **Generate the 12-section observation report.** Write `docs/runtime_observations/{YYYY-MM-DD}-server-runtime-session.md` using the EXACT structure from `2026-05-17-orchestrator-dry-run-session.md`:
   1. Frontmatter block (Author: openclaude autonomous reviewer / Date / Branch / Mode: DRY_RUN=true / Window: 72h rolling / Scope: WI-61 audit artifact aggregation)
   2. Executive Summary — top 3 findings from aggregator output (critical_safety_gates, total_errors, budget_blocks, provider_failures). Net operational posture.
   3. Session Timeline (UTC) — key events from aggregated artifacts (first/last artifact timestamps, any dry_run inconsistencies, error spikes).
   4. Environment & Configuration — `DRY_RUN` posture, LLM provider, budget blocks, provider failures. All secrets redacted.
   5. Findings (Ranked by Severity) — HIGH (critical_safety_gates > 0) → MEDIUM (total_errors > 50 OR budget_blocks > 10) → LOW (warnings, provider failures). Each with: Symptom / Root cause (from artifact findings) / Why it matters / Recommended investigation.
   6. Aggregator Numerical Summary — scanned_files, skipped_artifacts, total_errors, total_warnings, budget_blocks, provider_failures, critical_safety_gates, avg_response_time_ms, max_exposure_usdc, db_growth_bytes_delta, decision_distribution.
   7. Decision Distribution Analysis — buy/sell/hold/skip counts. If all zeros, document as "no decisions in window" (do not fabricate ratios).
   8. DB Growth Trend — db_growth_bytes_delta. If positive and >10MB, flag as potential bloat. If negative, document as VACUUM or cleanup.
   9. LLM Review Trends (if available) — summary from `latest.md`, redacted.
   10. Recommendations — Tier 1 (critical safety gates) / Tier 2 (error/budget thresholds) / Tier 3 (warnings, optimizations).
   11. Open Questions — artifacts with missing fields, timezone inconsistencies, dry_run inconsistencies.
   12. Process Notes for the Next Operator — how to interpret this report, when to escalate, how to run the aggregator manually.
   
   Every finding MUST be derived from aggregator output or artifact content. NEVER fabricate metrics or speculate without evidence.

5. **Generate the 14-section fix plan (CONDITIONAL).** If aggregator output has `fix_plan_required=true`, also write `docs/runtime_observations/{YYYY-MM-DD}-server-fix-plan.md` using the EXACT structure from `2026-05-17-orchestrator-fix-plan.md`:
   1. Why a separate planning document
   2. Newly observed signals since report was written (if any)
   3. Goals (prioritized: safety gates first, then error reduction, then budget optimization)
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
   
   The plan MUST NOT include any committed code. Every change touching `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, or `src/backtest_runner.py` MUST be marked MAAP-required.
   
   If `fix_plan_required=false`, add a one-line note in the observations report Section 10 explaining no plan was needed (thresholds not exceeded).

6. **Secret redaction enforcement.** Before writing any output file, scan all content for forbidden patterns matching `_FORBIDDEN_CONTENT_PATTERNS` in `src/observability/runtime_audit.py`:
   - Private keys (64-char hex, 0x-prefixed)
   - Telegram tokens (8-10 digit prefix)
   - Wallet addresses (0x + 40 hex chars)
   - API keys (sk-*, pk-*)
   - Condition IDs (0x + 60+ hex chars)
   - Token IDs (15+ digit strings)
   
   Replace all matches with `[REDACTED:{pattern_name}]`. If redaction fails or patterns are found in output, log an error and exit with code 1.

7. **Append a session summary to today's daily note** at `~/documents/integration_task/03_Daily/{YYYY-MM-DD}.md`:
   ```
   ## [HH:MM] Autonomous Server Runtime Review
   - Agent: openclaude (autonomous, systemd-triggered)
   - Active WI: WI-62
   - Actions taken: aggregated {N} WI-61 audit artifacts over 72h window
   - Files created: docs/runtime_observations/{YYYY-MM-DD}-server-runtime-session.md, docs/runtime_observations/{YYYY-MM-DD}-server-fix-plan.md (if generated)
   - Key findings: {critical_safety_gates} critical, {total_errors} errors, {budget_blocks} budget blocks
   - Fix plan required: {true/false}
   - Next: {manual review recommended if fix_plan_required=true, otherwise continue monitoring}
   ```

Rules:
- NEVER perform arithmetic in LLM context. All numbers come from the aggregator output.
- NEVER modify source code (`.py` files). This is a read-only reporting process.
- NEVER flag `dry_run=true` as a critical finding. Document as context; only flag if it changed unexpectedly (dry_run_inconsistent=true).
- NEVER generate a Fix Plan for subjective reasons. Only when `fix_plan_required=true` in aggregator output.
- NEVER persist secrets, wallet addresses, or high-cardinality identifiers in any output artifact.
- NEVER use `print()` in any generated code sketch — use `structlog` only.
- NEVER use raw `float` in any money/EV/Decimal path of a code sketch.
- NEVER suggest a change that weakens `DRY_RUN`, bypasses `LLMEvaluationResponse`, or skips Gatekeeper.
- NEVER fabricate metrics, ratios, or summaries when artifacts are absent or malformed. Missing data remains "unavailable", not zero.
- The observations report and fix plan ARE the deliverables. Do not skip them or substitute an inline summary.
- The next operator should be able to understand the 72-hour runtime posture by reading the observation report alone.

Error handling:
- If aggregator exits with code 1 and `error=no_artifacts_in_window`, generate a minimal observation report noting the gap and exit successfully.
- If aggregator exits with code 1 and `error=artifact_directory_not_found`, log an error and exit with code 1.
- If aggregator exits with code 2 (configuration error), log the error and exit with code 2.
- If aggregator output JSON is malformed or missing required fields, log an error and exit with code 3.
- If secret redaction fails, log an error and exit with code 4.
