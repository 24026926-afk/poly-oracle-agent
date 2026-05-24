# Business Logic - WI-61 Periodic Runtime Audit

## Objective

Add a deterministic, read-only, repository-backed runtime auditor that runs out-of-process on a fixed cadence and produces audit evidence for the paper-trading deployment before an operator notices degradation in the dashboard.

This WI extends the Phase 16 observability surface after WI-56 through WI-60. It consumes health/readiness endpoints, Prometheus metrics, the operational event ledger, existing repositories, Docker/service probes, bounded log-tail summaries, and typed runtime configuration. It writes only audit artifacts and optional advisory review output. It must not mutate runtime tables, control Docker, alter environment state, weaken `dry_run`, change trading behavior, approve trades, replace `LLMEvaluationResponse`, or introduce OpenCode/Hermes/OpenClaw as a dependency.

## Data Models

Pydantic schema names only:

- `RuntimeAuditStatus`
- `RuntimeAuditExitCode`
- `RuntimeAuditFailureReason`
- `RuntimeAuditSeverity`
- `RuntimeAuditFindingType`
- `RuntimeAuditFinding`
- `RuntimeAuditProbeStatus`
- `RuntimeAuditProbeResult`
- `RuntimeAuditHealthProbe`
- `RuntimeAuditReadinessProbe`
- `RuntimeAuditMetricsProbe`
- `RuntimeAuditMetricSample`
- `RuntimeAuditLedgerSummary`
- `RuntimeAuditDecisionSummary`
- `RuntimeAuditMarketSummary`
- `RuntimeAuditPositionSummary`
- `RuntimeAuditExecutionSummary`
- `RuntimeAuditDatabaseProbe`
- `RuntimeAuditDockerProbe`
- `RuntimeAuditLogTailSummary`
- `RuntimeAuditDryRunPosture`
- `RuntimeAuditForbiddenContentCheck`
- `RuntimeAuditArtifactWriteResult`
- `RuntimeAuditTelegramAlert`
- `RuntimeAuditTelegramResult`
- `RuntimeAuditLLMReviewStatus`
- `RuntimeAuditLLMReviewRequest`
- `RuntimeAuditLLMReviewResult`
- `RuntimeAuditReport`
- `OperationalEventRecord`
- `OperationalEventReadWindow`
- `OperationalEventQuery`
- `OperationalEventType`
- `OperationalEventSeverity`
- `OperationalEventSource`
- `OperationalEventReasonCode`
- `OperationalNarrative`
- `RuntimeNarrative`
- `NarrativeRenderResult`
- `NarrativeRenderStatus`
- `HealthStatus`
- `ReadinessStatus`
- `TelegramSendResult`

## Key Rules

1. The auditor is an out-of-process observability tool. It must never run inside the orchestrator hot path.
2. The auditor is read-only with respect to runtime state. It must not append, update, delete, backfill, repair, or acknowledge operational events.
3. All runtime database reads must route through existing repositories or repository-backed services.
4. The auditor must not use raw SQL, direct `AsyncSession` operations in audit logic, or direct model queries outside repository boundaries.
5. Any SQLite file probe must open the database using read-only URI semantics so writes are impossible at the connection layer.
6. The auditor may write only its own validated JSON and markdown artifacts under `docs/operations/runtime_audits/`.
7. `latest.json` and `latest.md` must be updated by atomic replacement after the timestamped artifact is fully validated.
8. The optional LLM reviewer may write only its own advisory markdown under `docs/operations/runtime_reviews/` after a successful audit artifact exists.
9. The auditor itself must never call Claude, DeepSeek, Grok, Kimi, or any other LLM.
10. The optional LLM reviewer must run as a separate process and use a direct `httpx` POST to `https://api.moonshot.ai/v1/chat/completions`.
11. OpenCode, Hermes, OpenClaw, and any new LLM framework dependency are forbidden for this WI.
12. The reviewer has no authority over code, config, environment, Docker, git, signing, broadcasting, repositories, or trading decisions.
13. `dry_run=true` is a mandatory safety gate. A production audit that detects `dry_run=false` must exit with code `2`.
14. Exit codes are typed and stable: `0` healthy, `1` degraded/warning, `2` failed mandatory safety gate, `3` config/probe error.
15. The report must not collapse mandatory safety-gate failures into generic degraded findings.
16. Health and readiness probes must use explicit HTTP timeouts.
17. Metrics scraping must use explicit HTTP timeouts and parse Prometheus text exposition deterministically.
18. Metrics labels must be scanned for forbidden high-cardinality labels before a report can pass.
19. Forbidden metric labels include `condition_id`, `token_id`, `wallet_address`, `prompt_text`, `reasoning_text`, and `secret`.
20. Operational-event reads must be bounded by a configured lookback window and maximum row count.
21. Decision, market, position, and execution repository reads must be bounded and summary-oriented.
22. Application log inspection must be bounded by bytes and line count, and must persist aggregate counts only.
23. The report must never persist raw log lines, raw exception text, raw prompts, private reasoning, provider responses, secrets, token IDs, condition IDs, wallet addresses, or unbounded market identifiers.
24. Docker probing is read-only and limited to bounded status/restart-count inspection such as `docker compose ps`.
25. The auditor must not call Docker restart, stop, start, exec, logs with unbounded output, or any control-plane mutation.
26. All HTTP, Docker, Telegram, and reviewer paths must have explicit timeout or bounded retry behavior.
27. All spend, PnL, rate, growth, freshness, and threshold math surfaced in the report must use `Decimal` end to end.
28. Raw Python `float` must be rejected at the schema boundary for any numeric report field where precision matters.
29. Missing optional probes must produce typed warning or unavailable findings, not fabricated healthy data.
30. Missing mandatory probes must produce typed probe errors and exit code `3` unless the missing evidence is itself a safety-gate failure.
31. Telegram alerting is optional and disabled unless explicitly configured and credentials are available through existing `TelegramNotifier` patterns.
32. Telegram alerts are sent for exit code `>= 1` only when runtime audit alerts are enabled.
33. Telegram payloads must be bounded, deterministic, timeout-protected, and pass forbidden-content scanning before send.
34. Telegram failures must be reported as typed alert failures but must not delete already-written audit artifacts.
35. Markdown artifacts must be deterministic for the same report input.
36. JSON artifacts must be the canonical typed report serialization.
37. Human-readable markdown may use WI-57 deterministic narratives, but must not use LLM-generated prose.
38. Artifact output paths must be project-root constrained and fail closed on traversal, symlink escape, or absolute-path override.
39. systemd unit files must keep the auditor out of the runtime process and document hardening constraints.
40. The auditor systemd timer defaults to a 15-minute cadence.
41. The reviewer systemd timer is committed disabled by default.
42. `ProtectSystem=strict` and constrained `ReadWritePaths=` must be documented for systemd deployment.
43. `.env.example` may document new variable names, but no secret values may be committed.
44. No Alembic migration or new runtime table is allowed for this WI unless explicitly approved after a design blocker is discovered.
45. No `Base.metadata.create_all()` may be introduced in runtime, CLI, auditor, reviewer, or tests.
46. No execution, signing, broadcasting, order routing, wallet mutation, or Gatekeeper bypass path may be imported or invoked by the auditor.
47. `LLMEvaluationResponse` must not be modified.
48. The auditor reports on safety posture; it does not authorize, override, or substitute for trading gates.
49. Logs and metrics emitted by the auditor, if any, must use low-cardinality labels only.
50. Tests must cover all exit codes, safety gates, repository injection, read-only SQLite behavior, forbidden-content rejection, artifact atomic swaps, Telegram redaction, metrics label rejection, reviewer-disabled defaults, and systemd/runbook deliverables.

## Edge Cases

1. `/healthz` is unreachable: record a health probe error and return exit code `3` unless other mandatory safety-gate evidence determines exit code `2`.
2. `/readyz` is reachable but reports not ready or degraded: return exit code `1` or `2` depending on whether the failed readiness detail is a mandatory safety gate.
3. `/readyz` reports `dry_run=false`: return exit code `2` and trigger a Telegram alert when alerts are enabled.
4. `/readyz` omits dry-run posture: fail closed as a mandatory safety-gate failure or probe error according to the typed contract.
5. `/metrics` is unreachable: return a typed metrics probe error without fabricating zeros.
6. `/metrics` contains a forbidden high-cardinality label: return exit code `2` because operator-facing telemetry is unsafe.
7. `/metrics` has malformed Prometheus text: return exit code `3` with a bounded parse failure reason.
8. Operational-event table is missing: produce a typed ledger-unavailable finding without trying to create the table.
9. Operational-event window is empty: report no recent events and keep other probes authoritative.
10. Operational-event row has malformed payload JSON: skip payload-derived fields and keep typed event metadata only.
11. Operational-event row contains forbidden content despite earlier validation: redact or fail the human-facing artifact before writing.
12. Repository read fails due to database lock: report a typed repository/database probe error with bounded message text.
13. SQLite database path does not exist: record database unavailable; do not create the file.
14. SQLite file growth exceeds configured threshold: report degraded with Decimal-backed growth calculations.
15. Recent market snapshots are stale: report degraded without inventing market identities.
16. Recent decisions are absent: report no-decision or degraded only according to configured freshness thresholds.
17. Positions table is absent or empty: report position data unavailable or zero open positions only when repository evidence supports it.
18. Execution repository reports live execution while `dry_run=true`: produce a safety finding for operator review.
19. Docker Compose is unavailable on a local developer machine: report optional Docker probe unavailable rather than failing the whole audit when configured optional.
20. Docker restart count exceeds threshold: report degraded with bounded service identifiers only.
21. Log file is missing: report log-tail unavailable without creating the file.
22. Log tail contains raw secrets or IDs: never persist the raw lines; only record forbidden-content detection.
23. Artifact directory does not exist: create only the validated audit artifact directory under project docs, not runtime DB directories.
24. Artifact write partially fails: do not update `latest.*`; return typed artifact failure.
25. `latest.*` atomic replacement fails: keep timestamped artifacts intact and return typed artifact failure.
26. Telegram is disabled: audit still succeeds or fails according to probes; Telegram result is disabled.
27. Telegram credentials are absent: audit records alert unavailable rather than exposing credential details.
28. Telegram send times out: audit records typed alert failure without raw exception text.
29. Reviewer is disabled: no Moonshot HTTP call is made and no review artifact is required.
30. Reviewer is enabled but `MOONSHOT_API_KEY` is missing: audit artifact remains valid; reviewer exits with typed reviewer-disabled/config failure.
31. Reviewer returns unsafe text: reject the review artifact or write a safe failure notice without unsafe content.
32. Reviewer HTTP request times out: reviewer writes no opinion artifact or writes a typed safe failure artifact according to the prompt contract.
33. System clock or timestamp input is timezone-naive: schema validation normalizes or rejects according to the request contract.
34. Multiple audits run concurrently: timestamped artifacts remain unique and `latest.*` replacement is atomic.
35. Local development without running orchestrator: audit returns clear probe errors or degraded/unavailable findings without mutating runtime state.

## Invariants

1. Runtime audit is deterministic for the same probe inputs, repository state, request, and config.
2. Runtime audit is read-only for all runtime state.
3. Runtime database access is repository-backed.
4. SQLite file probing is read-only at the connection layer.
5. The only auditor writes are validated audit artifacts under `docs/operations/runtime_audits/`.
6. The only reviewer writes are advisory review artifacts under `docs/operations/runtime_reviews/`.
7. The auditor never calls an LLM.
8. The reviewer is optional, disabled by default, advisory only, and separate from the auditor process.
9. `dry_run=true` remains a mandatory safety gate.
10. Exit codes remain typed and stable.
11. Operator-facing artifacts, Telegram payloads, logs, metrics, and reviewer input/output are secret-safe and high-cardinality-safe.
12. Missing or unavailable evidence remains unknown/unavailable; it is never invented.
13. `LLMEvaluationResponse` remains the terminal Gatekeeper and is not modified.
14. No live trading, signing, broadcasting, order routing, wallet mutation, or `DRY_RUN=false` behavior is added or changed.
15. No raw database sessions or raw SQL appear in auditor business logic.
16. No Alembic migration or runtime schema mutation is introduced by this WI.
17. All spend, PnL, rate, freshness, threshold, and growth math surfaced in the report is Decimal-native.
18. Tests cover healthy, degraded, safety-failed, and probe-error paths plus artifact, Telegram, reviewer, systemd, redaction, repository, and read-only database behavior.
