# Implementation Prompt - WI-61 Periodic Runtime Audit

## Session Context

You are working in `poly-oracle-agent` after Phase 16 completion and post-Phase 16 runtime stabilization. WI-61 is a standalone operational-hardening Work Item for paper-trading safety evidence.

Current baseline:

- Phase 16 is complete and introduced the operational event ledger, deterministic narratives, incident replay CLI, dashboard activity feed, and daily operations digest.
- WI-56 introduced the SQLite-backed append-only operational event ledger, `OperationalEventRepository`, `OperationalEventBus`, operational event schemas/enums in `src/schemas/ops.py`, representative runtime hooks, and `docs/runbooks/operational-event-ledger.md`.
- WI-57 introduced deterministic, secret-safe narrative rendering in `src/observability/operational_narratives.py` with presentation schemas in `src/schemas/ops.py`.
- WI-58 introduced a read-only incident replay service in `src/observability/incident_replay.py`, replay schemas in `src/schemas/ops.py`, `scripts/ops/replay.py`, and `docs/runbooks/incident-replay.md`.
- WI-59 introduced `src/observability/dashboard_activity_feed.py`, dashboard feed/current-state schemas in `src/schemas/ops.py`, and a read-only Streamlit runtime timeline.
- WI-60 introduced deterministic daily operations digest generation via `src/observability/daily_ops_digest.py`, `scripts/ops/generate_daily_ops_digest.py`, digest schemas in `src/schemas/ops.py`, and `docs/runbooks/daily-operations-digest.md`.
- WI-61 adds a periodic out-of-process runtime auditor that produces JSON + markdown safety evidence, alerts through Telegram on degradation, and optionally runs a separate advisory LLM reviewer against the finalized artifact.
- `DRY_RUN=false` remains out of scope. Live signing, live broadcasting, and execution paths that bypass `LLMEvaluationResponse` remain forbidden.
- `LLMEvaluationResponse` remains the terminal Gatekeeper before execution and must not receive presentation fields.
- The auditor is read-only for runtime state. It must not append, mutate, delete, backfill, repair, or acknowledge operational events.
- The optional LLM reviewer is advisory only, disabled by default, and must use direct `httpx` against Moonshot/Kimi. It must not introduce OpenCode, Hermes, OpenClaw, or any new LLM framework dependency.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v16.0.md`
- `docs/system_architecture.md`
- `01_Brief Context/WI-61-periodic-runtime-audit.md`
- `docs/deliverables/business_logic/business_logic_WI-61-periodic-runtime-audit.md`
- `docs/deliverables/business_logic/business_logic_WI-60-daily-operations-digest.md`
- `docs/deliverables/implementation_prompts/prompt_WI-60-daily-operations-digest.md`
- `docs/deliverables/business_logic/business_logic_WI-59-dashboard-activity-feed.md`
- `docs/deliverables/implementation_prompts/prompt_WI-59-dashboard-activity-feed.md`
- `docs/deliverables/business_logic/business_logic_WI-58-incident-replay-cli.md`
- `docs/deliverables/implementation_prompts/prompt_WI-58-incident-replay-cli.md`
- `docs/deliverables/business_logic/business_logic_WI-57-deterministic-human-narratives.md`
- `docs/deliverables/implementation_prompts/prompt_WI-57-deterministic-human-narratives.md`
- `docs/deliverables/business_logic/business_logic_WI-56-operational-event-ledger.md`
- `docs/deliverables/implementation_prompts/prompt_WI-56-operational-event-ledger.md`
- `src/schemas/ops.py`
- `src/db/repositories/operational_event_repository.py`
- `src/db/repositories/decision_repository.py`
- `src/db/repositories/market_repository.py`
- `src/db/repositories/position_repository.py`
- `src/db/repositories/execution_repository.py`
- `src/observability/health.py`
- `src/observability/health_server.py`
- `src/observability/metrics.py`
- `src/observability/operational_narratives.py`
- `src/observability/incident_replay.py`
- `src/observability/dashboard_activity_feed.py`
- `src/observability/daily_ops_digest.py`
- `src/agents/execution/telegram_notifier.py`
- `src/core/config.py`
- `scripts/ops/replay.py`
- `scripts/ops/generate_daily_ops_digest.py`
- `tests/unit/test_WI-58-incident-replay-cli.py`
- `tests/unit/test_WI-59-dashboard-activity-feed.py`
- `tests/unit/test_WI-60-daily-operations-digest.py`
- `tests/integration/test_WI-58-incident-replay-cli.py`
- `tests/integration/test_WI-59-dashboard-activity-feed.py`
- `tests/integration/test_WI-60-daily-operations-digest.py`

## Objective

Build a deterministic, read-only periodic runtime auditor that probes the running paper-trading deployment, summarizes safety posture through typed Pydantic schemas, writes secret-safe JSON and markdown evidence artifacts, exits with stable typed codes, and optionally alerts the operator through Telegram. Add a separate optional advisory LLM reviewer that reads a finalized audit artifact and writes an opinion markdown only when explicitly enabled.

## Inputs

- `HealthServer` `/healthz` endpoint.
- `HealthServer` `/readyz` endpoint, including dry-run posture evidence.
- `MetricsServer` `/metrics` endpoint in Prometheus text exposition format.
- Existing `OperationalEventRepository` bounded read-window behavior.
- Existing `DecisionRepository` bounded read behavior for recent decision counts/freshness.
- Existing `MarketRepository` bounded read behavior for recent market snapshot freshness.
- Existing `PositionRepository` read behavior for open/settled position summaries.
- Existing `ExecutionRepository` read behavior for dry-run/live execution evidence.
- Existing deterministic narrative renderer from WI-57.
- Existing daily digest and incident replay aggregation patterns where reusable.
- Existing `TelegramNotifier` and Telegram timeout behavior.
- Existing `AppConfig` settings and secret-handling conventions.
- Project-root-constrained output directories under `docs/operations/`.
- SQLite database file path for read-only existence, freshness, and bounded growth checks.
- Docker Compose service status and restart-count output from a bounded read-only probe.
- Application log tail metadata from a bounded byte/line cap.
- Optional `MOONSHOT_API_KEY` for the advisory reviewer, represented as `SecretStr` and never printed.
- No new Python package dependencies. Use existing `httpx` for HTTP probes and Moonshot reviewer calls.

## Outputs

- `src/schemas/runtime_audit.py` containing typed runtime-audit schemas and enums.
- `src/observability/runtime_audit.py` containing the deterministic auditor service.
- `scripts/ops/periodic_runtime_audit.py` CLI entrypoint.
- Optional reviewer implementation using direct `httpx` and no new framework dependency.
- `docs/operations/runtime_audits/runtime-audit-YYYYMMDDTHHMMSSZ.json`.
- `docs/operations/runtime_audits/runtime-audit-YYYYMMDDTHHMMSSZ.md`.
- `docs/operations/runtime_audits/latest.json` and `docs/operations/runtime_audits/latest.md` updated by atomic replacement.
- `docs/operations/runtime_reviews/runtime-review-YYYYMMDDTHHMMSSZ.md` or an equivalent advisory review path documented by the WI.
- Config fields for auditor thresholds, output directories, Telegram alert enablement, and reviewer enablement/key/model settings.
- `.env.example` entries for new non-secret runtime audit and reviewer configuration names.
- `deploy/systemd/poly-oracle-runtime-audit.service`.
- `deploy/systemd/poly-oracle-runtime-audit.timer`.
- `deploy/systemd/poly-oracle-runtime-review.service`.
- `deploy/systemd/poly-oracle-runtime-review.timer`, disabled by default.
- `docs/runbooks/periodic-runtime-audit.md`.
- `tests/unit/test_WI-61-periodic-runtime-audit.py`.
- `tests/integration/test_WI-61-periodic-runtime-audit.py`.

## Acceptance Criteria

1. CLI entrypoint `scripts/ops/periodic_runtime_audit.py` exists.
2. CLI exits only with typed codes: `0`, `1`, `2`, or `3`.
3. Exit code `0` means all mandatory probes and safety gates are healthy.
4. Exit code `1` means degraded or warning findings exist without mandatory safety-gate failure.
5. Exit code `2` means a mandatory safety gate failed, including `dry_run=false` or unsafe metrics labels.
6. Exit code `3` means config, artifact, repository, parse, timeout, or probe error prevented a trustworthy audit.
7. Tests cover all four exit codes.
8. `RuntimeAuditReport` and related schemas live in `src/schemas/runtime_audit.py`.
9. Runtime audit schemas are Pydantic V2 and frozen where appropriate.
10. Any spend, PnL, rate, freshness, growth, or threshold math surfaced in the report uses `Decimal` end to end.
11. Runtime audit schemas reject raw Python `float` for precision-sensitive numeric fields.
12. Auditor service lives in `src/observability/runtime_audit.py`.
13. Auditor is deterministic for the same input probes, repository state, request, and config.
14. Auditor is read-only for runtime state.
15. Auditor does not append, update, delete, backfill, repair, or acknowledge operational events.
16. All operational-event reads route through `OperationalEventRepository` or a repository-backed service.
17. All decision reads route through `DecisionRepository` or a repository-backed service.
18. All market snapshot reads route through `MarketRepository` or a repository-backed service.
19. All position reads route through `PositionRepository` or a repository-backed service.
20. All execution reads route through `ExecutionRepository` or a repository-backed service.
21. Audit business logic does not use raw SQL.
22. Audit business logic does not perform direct `AsyncSession` queries outside repository boundaries.
23. SQLite file probes use read-only URI semantics.
24. SQLite file probes do not create the database file when it is absent.
25. Health probe uses explicit timeout.
26. Readiness probe uses explicit timeout.
27. Readiness probe verifies `dry_run=true` as a mandatory safety gate.
28. Missing dry-run evidence fails closed as a safety-gate failure or typed probe error.
29. Metrics probe uses explicit timeout.
30. Metrics parser accepts valid Prometheus text exposition.
31. Metrics parser rejects or fails closed on malformed exposition.
32. Metrics parser scans label names and values for forbidden content.
33. Metrics containing high-cardinality labels such as `condition_id`, `token_id`, `wallet_address`, `prompt_text`, `reasoning_text`, or `secret` produce exit code `2`.
34. Operational-event ledger summary counts recent errors, warnings, WS reconnects, budget blocks, provider failures, market quarantines, readiness changes, alerts, and recoveries.
35. Repository summaries are bounded by lookback windows and row limits.
36. Application log-tail probe uses a bounded byte cap and does not persist raw lines.
37. Docker Compose probe is read-only and bounded by explicit timeout.
38. Docker probe does not call stop, start, restart, exec, or any mutating Docker command.
39. JSON audit artifact is written under `docs/operations/runtime_audits/`.
40. Markdown audit artifact is written under `docs/operations/runtime_audits/`.
41. Artifact output paths are constrained to the project root.
42. Path traversal, symlink escape, and unsafe absolute output paths fail closed.
43. `latest.json` and `latest.md` are updated only after timestamped artifacts validate successfully.
44. `latest.json` and `latest.md` updates use atomic replacement.
45. Failed artifact writes do not leave `latest.*` pointing at invalid content.
46. Markdown artifact is deterministic and generated from typed fields only.
47. Markdown artifact may use WI-57 deterministic narratives.
48. Auditor never uses an LLM to generate audit text.
49. JSON artifact is the canonical typed report serialization.
50. JSON and markdown artifacts pass forbidden-pattern scans before being finalized.
51. Forbidden scans reject API keys, wallet keys, Telegram tokens, wallet addresses, condition IDs, token IDs, raw prompts, private reasoning, raw provider responses, raw exception text, connection strings, SQL text, and high-cardinality identifiers.
52. Telegram alerting is optional and disabled unless `enable_runtime_audit_alerts=True` and credentials are available.
53. Telegram alert sends only for exit code `>= 1`.
54. Telegram alert payload is bounded, deterministic, timeout-protected, and secret-safe.
55. Telegram alert uses existing `TelegramNotifier` patterns.
56. Telegram failure returns a typed alert result and does not corrupt or delete artifacts.
57. Optional LLM reviewer is disabled by default.
58. Auditor process never calls the optional LLM reviewer before finalizing the audit artifact.
59. Reviewer, when enabled, runs separately after a successful audit artifact exists.
60. Reviewer uses direct `httpx` against `https://api.moonshot.ai/v1/chat/completions`.
61. Reviewer uses `MOONSHOT_API_KEY` through secret-safe config and never prints it.
62. Reviewer introduces no OpenCode, Hermes, OpenClaw, OpenAI SDK, or new LLM framework dependency.
63. Reviewer has no write authority except its own advisory markdown artifact under `docs/operations/runtime_reviews/`.
64. Reviewer has no shell, Docker, git, environment, repository-write, signing, broadcasting, order-routing, or trading-authorization authority.
65. Reviewer output is advisory only and never gates trading.
66. Reviewer output is scanned for forbidden content before writing.
67. Reviewer-disabled default behavior is test-covered.
68. `.env.example` documents new non-secret config names and leaves secret values blank.
69. No `.env` secret value is committed.
70. systemd audit service and timer files exist.
71. systemd audit timer defaults to 15-minute cadence.
72. systemd reviewer service and timer files exist.
73. systemd reviewer timer is disabled by default or documented as not enabled during install unless explicitly chosen.
74. systemd unit files include working directory, user/environment-file expectations, and hardening constraints.
75. `ProtectSystem=strict` and constrained `ReadWritePaths=` behavior is documented.
76. Auditor creates no Alembic migration.
77. Auditor does not call `Base.metadata.create_all()`.
78. Auditor does not import or invoke execution routing, transaction signing, order broadcasting, order placement, or live wallet mutation paths.
79. Auditor does not modify `LLMEvaluationResponse`.
80. Auditor does not add presentation fields to cognitive, financial, or Gatekeeper schemas.
81. No live trading, signing, broadcasting, or `DRY_RUN=false` behavior is added or changed.
82. Logs and metrics from audit code, if any, use low-cardinality labels only.
83. Missing optional probes produce typed unavailable/degraded findings without crashing valid audit generation.
84. Missing mandatory probes produce typed probe errors or safety-gate failures according to the contract.
85. Runbook `docs/runbooks/periodic-runtime-audit.md` documents cadence, exit codes, probe inputs, thresholds, artifacts, Telegram opt-in, reviewer opt-in, systemd installation, troubleshooting, and the advisory-only reviewer guarantee.
86. Targeted WI-61 unit and integration tests pass.
87. Full regression remains compatible with the documented baseline and coverage stays at or above 80%.
88. Runtime audit module line coverage reaches at least 90%.
89. MAAP is run before commit because this WI touches `src/schemas/` and `src/observability/`.

## Anti-Patterns

- Do not enable live trading.
- Do not change `DRY_RUN=false` behavior.
- Do not add live signing or broadcasting.
- Do not bypass `LLMEvaluationResponse`.
- Do not add presentation fields to `LLMEvaluationResponse`.
- Do not invoke execution routing, transaction signing, order broadcasting, order placement, or wallet mutation paths.
- Do not write to runtime database tables from auditor code.
- Do not append, update, delete, backfill, repair, or acknowledge operational events.
- Do not add update or delete behavior to repository classes.
- Do not use raw SQL in audit business logic.
- Do not use direct `AsyncSession` reads in audit business logic outside repository boundaries.
- Do not open SQLite in writable mode for file/freshness probes.
- Do not create the SQLite database file when it is missing.
- Do not call `Base.metadata.create_all()`.
- Do not create an Alembic migration unless an explicitly approved design change requires it.
- Do not persist raw log lines, raw exception text, raw prompts, private reasoning, raw provider responses, token IDs, condition IDs, wallet addresses, or secrets.
- Do not expose high-cardinality identifiers in artifacts, Telegram payloads, metrics labels, logs, reviewer inputs, reviewer outputs, tests, or runbooks.
- Do not use raw `float` for money, spend, PnL, EV, Kelly, price, sizing, exposure, token-cost, rate, freshness, or threshold math.
- Do not treat missing data as zero unless repository-backed typed evidence proves zero.
- Do not allow malformed metrics, unsafe labels, missing dry-run posture, or forbidden artifact content to pass as healthy.
- Do not call an LLM from the auditor.
- Do not introduce OpenCode, Hermes, OpenClaw, OpenAI SDK, or any new LLM framework dependency.
- Do not let the optional reviewer mutate code, config, environment, Docker, git, repositories, runtime state, or trading decisions.
- Do not make the reviewer required for audit success.
- Do not enable the reviewer by default.
- Do not run unbounded Docker, log, HTTP, Telegram, or reviewer calls.
- Do not print raw stack traces, SQL text, connection strings, command output containing secrets, or unbounded exception messages to operators.

## Dependencies

- WI-61 brief context at `01_Brief Context/WI-61-periodic-runtime-audit.md`.
- Phase 16 PRD (`docs/PRD-v16.0.md`) for auditability and secret-safety constraints.
- WI-56 operational event ledger deliverables and implementation.
- WI-57 deterministic narrative deliverables and implementation.
- WI-58 incident replay deliverables and implementation.
- WI-59 dashboard activity feed deliverables and implementation.
- WI-60 daily operations digest deliverables and implementation.
- Existing `OperationalEventRepository` read-window contract.
- Existing `DecisionRepository`, `MarketRepository`, `PositionRepository`, and `ExecutionRepository` read contracts.
- Existing health/readiness server response contracts from WI-46.
- Existing metrics server and low-cardinality metrics contract from WI-47.
- Existing `TelegramNotifier` timeout and failure-handling behavior from WI-50.
- Existing operational narrative helpers from WI-57.
- Existing digest/replay path-validation, redaction, and CLI patterns where reusable.
- Existing async SQLAlchemy engine/session setup.
- Existing `httpx` dependency.
- Existing `structlog` logging conventions.
- Existing low-cardinality and secret/high-cardinality scan constraints.
- No new Python package dependencies.

## Target Layer

Observability and operations safety layer. This WI adds an out-of-process runtime auditor and optional advisory reviewer over existing health, metrics, repository, ledger, Telegram, and artifact surfaces. It must not change ingestion, context aggregation, prompt construction, LLM evaluation semantics, Gatekeeper authority, execution routing, live-trading authorization, signing, broadcasting, or runtime database write semantics.
