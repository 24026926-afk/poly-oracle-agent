# Business Logic - WI-62 Server Runtime Review

## Objective

Build a headless, autonomous 72-hour runtime review skill that aggregates WI-61 periodic runtime audit artifacts via deterministic Python, then feeds the structured summary to openclaude (headless CLI) for narrative report generation — running unattended on the server via systemd timer with no interactive operator present.

This WI extends the WI-61 periodic runtime audit surface. It consumes typed `RuntimeAuditReport` JSON artifacts produced at 15-minute cadence, aggregates them over a rolling 72-hour window using Decimal-safe arithmetic, and produces deterministic observation reports and conditional fix plans. It must not modify source code, perform LLM arithmetic, mutate runtime state, weaken `dry_run`, change trading behavior, or bypass `LLMEvaluationResponse`.

## Data Models

Pydantic schema names only:

- `AuditAggregationStatus`
- `AuditAggregationFailureReason`
- `AuditAggregationRequest`
- `AuditAggregationSummary`
- `AuditAggregationDecisionDistribution`
- `AuditAggregationProviderSummary`
- `AuditAggregationSafetyGateSummary`
- `AuditAggregationResult`
- `RuntimeAuditReport` (consumed from WI-61, not redefined)
- `RuntimeAuditExitCode` (consumed from WI-61, not redefined)
- `RuntimeAuditStatus` (consumed from WI-61, not redefined)

## Key Rules

1. The aggregator is a standalone CLI script at `scripts/ops/aggregate_audits.py`. It is invoked by the openclaude skill or manually by an operator.
2. The aggregator accepts `--hours N` to define the lookback window (default 72).
3. The aggregator reads only `RuntimeAuditReport` JSON artifacts from `docs/operations/runtime_audits/`.
4. The aggregator must use iterative/streaming processing. It must not load all artifacts into memory simultaneously.
5. All arithmetic in the aggregator must use `Decimal` end to end. No `float` coercion for exposure, response time, ratios, growth deltas, or counts.
6. The aggregator must detect zero-artifact windows and exit with code `1` and a typed JSON error `{"error": "no_artifacts_in_window", ...}`.
7. The aggregator must exit with code `0` and print valid JSON to stdout when artifacts exist in the window.
8. Malformed JSON artifacts must be skipped with a counted warning, not crash the aggregation.
9. The aggregator output JSON must contain: `scanned_files`, `total_errors`, `total_warnings`, `budget_blocks`, `provider_failures`, `critical_safety_gates`, `avg_response_time_ms`, `max_exposure_usdc`, `db_growth_bytes_delta`, `dry_run_posture`, `decision_distribution` (buy/sell/hold/skip counts), `fix_plan_required` (boolean based on explicit thresholds).
10. Fix Plan thresholds are explicit and deterministic: `critical_safety_gates > 0` OR `total_errors > 50` OR `budget_blocks > 10`.
11. `decision_distribution` must aggregate buy/sell/hold/skip counts from per-artifact decision summaries.
12. `db_growth_bytes_delta` must compute first vs last artifact `file_size_bytes` using `Decimal`.
13. `avg_response_time_ms` must use `Decimal` division and round to a configured precision.
14. `max_exposure_usdc` must use `Decimal` comparison across all artifact exposure fields.
15. All aggregator output must be scrubbed of wallet addresses, API keys, condition IDs, token IDs per `_FORBIDDEN_CONTENT_PATTERNS` in `src/observability/runtime_audit.py`.
16. The aggregator must not persist secrets, wallet addresses, or high-cardinality identifiers in any output.
17. The aggregator must not use `print()` for logging. Use `structlog` only.
18. The openclaude skill at `.opencode/commands/server-runtime-review.md` must perform pre-flight hydration: read `STATE.md`, validate `.env` exists, confirm artifact directory exists.
19. The skill must run the aggregator via Bash tool (never LLM arithmetic).
20. The skill must read LLM review trends from `docs/operations/runtime_reviews/latest.md` when available.
21. The skill must generate a 12-section observation report to `docs/runtime_observations/{YYYY-MM-DD}-server-runtime-session.md` following the canonical template from `2026-05-17-orchestrator-dry-run-session.md`.
22. The skill must conditionally generate a 14-section fix plan to `docs/runtime_observations/{YYYY-MM-DD}-server-fix-plan.md` following the canonical template from `2026-05-17-orchestrator-fix-plan.md` only when `fix_plan_required=true` in aggregator output.
23. The skill must apply secret redaction rules matching `_FORBIDDEN_CONTENT_PATTERNS` in `src/observability/runtime_audit.py`.
24. The skill must handle errors for zero artifacts, malformed JSON, and missing directories.
25. The skill must never modify source code (`.py` files). This is a read-only reporting process.
26. The skill must never perform LLM arithmetic. All numeric computation is delegated to the Python aggregator.
27. The skill must not flag `dry_run=true` as a critical finding. Document it as context; only flag if it changed unexpectedly.
28. The skill must not generate a Fix Plan for subjective reasons. Only when explicit thresholds are exceeded.
29. The skill must not weaken `DRY_RUN`, bypass `LLMEvaluationResponse`, or skip Gatekeeper in any recommendation.
30. systemd service `deploy/systemd/poly-oracle-server-review.service` must use `EnvironmentFile=/opt/poly-oracle-agent/.env` (not hardcoded API keys).
31. systemd service must use `ProtectSystem=strict` and `ReadWritePaths=` constrained to `docs/runtime_observations/`.
32. systemd service binary must be `openclaude` (not `/usr/local/bin/claude`).
33. systemd timer `deploy/systemd/poly-oracle-server-review.timer` must run every 24h (not 72h) with `Persistent=true` for rolling 72h coverage.
34. No new Python package dependencies. Use existing `structlog`, `pydantic`, and standard library only.
35. No Alembic migration or runtime schema mutation is introduced by this WI.
36. No `Base.metadata.create_all()` may be introduced in the aggregator, skill, or tests.
37. No execution, signing, broadcasting, order routing, wallet mutation, or Gatekeeper bypass path may be imported or invoked.
38. `LLMEvaluationResponse` must not be modified.
39. Tests must cover: aggregator happy path, zero-artifact path, malformed-artifact skipping, Decimal integrity, secret scrubbing, threshold boundary conditions, fix-plan-required boolean logic, and streaming processing behavior.

## Edge Cases

1. Zero artifacts in the 72-hour window: aggregator exits code `1` with typed JSON error. Skill reports no data available without fabricating metrics.
2. Artifact directory does not exist: aggregator exits with a clear error. Skill reports missing directory without creating it.
3. Artifact directory is empty: treated as zero-artifact window.
4. Single artifact in window: aggregation proceeds with `db_growth_bytes_delta=0` (first == last).
5. Malformed JSON in one artifact: skip that artifact, increment `skipped_artifacts` count, continue processing remaining files.
6. Artifact timestamp is outside the lookback window: exclude from aggregation without error.
7. Artifact timestamp is timezone-naive: treat as UTC per WI-61 convention.
8. Artifact contains forbidden content despite WI-61 validation: aggregator scrubs before output; skill scrubs before report generation.
9. `docs/operations/runtime_reviews/latest.md` does not exist: skill proceeds without LLM review trend section.
10. `docs/operations/runtime_reviews/latest.md` contains forbidden content: skill redacts before including trend summary.
11. All artifacts report `dry_run=true`: document as expected context, not a finding.
12. One artifact reports `dry_run=false` while others report `true`: flag as a safety-gate inconsistency in the observation report.
13. `decision_distribution` has zero decisions across all artifacts: report zero counts, do not fabricate ratios.
14. `max_exposure_usdc` is absent from all artifacts: report as unavailable, not zero.
15. `avg_response_time_ms` has zero valid samples: report as unavailable, not zero.
16. `fix_plan_required` is `false`: skill generates observation report only, no fix plan.
17. `fix_plan_required` is `true`: skill generates both observation report and fix plan.
18. Observation report output directory does not exist: create `docs/runtime_observations/` if it does not exist.
19. Concurrent aggregation runs: timestamped output files remain unique; no file locking required.
20. openclaude CLI is not installed: skill fails with clear error message; systemd service exits non-zero.
21. Provider API key is missing from `.env`: openclaude fails with its own error; systemd service logs the failure.
22. systemd `ReadWritePaths` prevents writing outside `docs/runtime_observations/`: aggregator artifacts remain under `docs/operations/runtime_audits/` (written by WI-61, not this WI).

## Invariants

1. The aggregator is deterministic for the same set of input artifacts and lookback window.
2. All numeric computation uses `Decimal` end to end. No `float` coercion.
3. The aggregator is read-only for runtime state. It reads only WI-61 JSON artifacts.
4. The aggregator never calls an LLM.
5. The skill never performs arithmetic. All numbers come from the aggregator output.
6. The skill never modifies source code.
7. Fix plans are generated only on explicit threshold breach, never on subjective grounds.
8. `dry_run=true` is documented as context, not flagged as a finding.
9. All output artifacts, reports, and payloads are secret-safe and high-cardinality-safe.
10. Missing or unavailable data remains unknown/unavailable; it is never invented.
11. `LLMEvaluationResponse` remains the terminal Gatekeeper and is not modified.
12. No live trading, signing, broadcasting, order routing, wallet mutation, or `DRY_RUN=false` behavior is added or changed.
13. No raw database sessions or raw SQL appear in aggregator or skill logic.
14. No Alembic migration or runtime schema mutation is introduced.
15. systemd units use `EnvironmentFile`, `ProtectSystem=strict`, and constrained `ReadWritePaths`.
16. The timer runs every 24h with `Persistent=true` for rolling 72h coverage.
17. Tests cover happy path, zero-artifact, malformed-artifact, Decimal integrity, secret scrubbing, threshold boundaries, and streaming behavior.
