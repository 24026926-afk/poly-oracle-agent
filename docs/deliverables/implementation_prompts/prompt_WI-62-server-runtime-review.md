# Implementation Prompt - WI-62 Server Runtime Review

## Session Context

You are working in `poly-oracle-agent` after Phase 16 completion, post-Phase 16 runtime stabilization, and WI-61 periodic runtime audit completion. WI-62 is a standalone operational-hardening Work Item that adds an autonomous 72-hour runtime review skill over WI-61 audit artifacts.

Current baseline:

- Phase 16 is complete and introduced the operational event ledger, deterministic narratives, incident replay CLI, dashboard activity feed, and daily operations digest.
- WI-56 introduced the SQLite-backed append-only operational event ledger, `OperationalEventRepository`, `OperationalEventBus`, operational event schemas/enums in `src/schemas/ops.py`, representative runtime hooks, and `docs/runbooks/operational-event-ledger.md`.
- WI-57 introduced deterministic, secret-safe narrative rendering in `src/observability/operational_narratives.py` with presentation schemas in `src/schemas/ops.py`.
- WI-58 introduced a read-only incident replay service in `src/observability/incident_replay.py`, replay schemas in `src/schemas/ops.py`, `scripts/ops/replay.py`, and `docs/runbooks/incident-replay.md`.
- WI-59 introduced `src/observability/dashboard_activity_feed.py`, dashboard feed/current-state schemas in `src/schemas/ops.py`, and a read-only Streamlit runtime timeline.
- WI-60 introduced deterministic daily operations digest generation via `src/observability/daily_ops_digest.py`, `scripts/ops/generate_daily_ops_digest.py`, digest schemas in `src/schemas/ops.py`, and `docs/runbooks/daily-operations-digest.md`.
- WI-61 introduced the periodic runtime auditor at `src/observability/runtime_audit.py`, typed schemas at `src/schemas/runtime_audit.py`, CLI at `scripts/ops/periodic_runtime_audit.py`, optional advisory LLM reviewer, systemd units at `deploy/systemd/poly-oracle-runtime-audit.{service,timer}`, and `docs/runbooks/periodic-runtime-audit.md`. The auditor produces `RuntimeAuditReport` JSON artifacts at `docs/operations/runtime_audits/runtime-audit-*.json` every 15 minutes.
- WI-62 adds a deterministic Python aggregator over WI-61 artifacts and an openclaude skill that invokes the aggregator and generates observation reports and conditional fix plans.
- `DRY_RUN=false` remains out of scope. Live signing, live broadcasting, and execution paths that bypass `LLMEvaluationResponse` remain forbidden.
- `LLMEvaluationResponse` remains the terminal Gatekeeper before execution and must not receive presentation fields.
- The aggregator is read-only for runtime state. It reads only WI-61 JSON artifacts.
- The skill never performs arithmetic or modifies source code.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v16.0.md`
- `docs/system_architecture.md`
- `01_Brief Context/WI-62-server-runtime-review.md`
- `docs/deliverables/business_logic/business_logic_WI-62-server-runtime-review.md`
- `docs/deliverables/business_logic/business_logic_WI-61-periodic-runtime-audit.md`
- `docs/deliverables/implementation_prompts/prompt_WI-61-periodic-runtime-audit.md`
- `src/schemas/runtime_audit.py`
- `src/observability/runtime_audit.py`
- `scripts/ops/periodic_runtime_audit.py`
- `docs/runtime_observations/2026-05-17-orchestrator-dry-run-session.md`
- `docs/runtime_observations/2026-05-17-orchestrator-fix-plan.md`
- `.opencode/commands/dry-run-review.md`
- `deploy/systemd/poly-oracle-runtime-audit.service`
- `deploy/systemd/poly-oracle-runtime-audit.timer`

## Objective

Build a deterministic Python aggregator that summarizes WI-61 runtime audit artifacts over a rolling 72-hour window using Decimal-safe arithmetic, and an openclaude skill that invokes the aggregator, hydrates project context, and generates a 12-section observation report and a conditional 14-section fix plan — all running unattended via systemd timer with no interactive operator present.

## Inputs

- `docs/operations/runtime_audits/runtime-audit-*.json` — typed `RuntimeAuditReport` artifacts produced by WI-61 at 15-minute cadence (~288 artifacts over 72 hours).
- `docs/operations/runtime_reviews/latest.md` — most recent Moonshot/Kimi advisory review (optional, may not exist).
- `STATE.md` — current project state for context hydration.
- `.env` — `DRY_RUN` posture confirmation, provider API keys for openclaude (redacted in output).
- Canonical 12-section observation template: `docs/runtime_observations/2026-05-17-orchestrator-dry-run-session.md`.
- Canonical 14-section fix plan template: `docs/runtime_observations/2026-05-17-orchestrator-fix-plan.md`.
- `_FORBIDDEN_CONTENT_PATTERNS` from `src/observability/runtime_audit.py` for secret scrubbing.
- No new Python package dependencies. Use existing `structlog`, `pydantic`, and standard library only.

## Outputs

- `scripts/ops/aggregate_audits.py` — hardened CLI aggregator with:
  - `--hours N` lookback window (default 72).
  - Zero-artifact detection (exit code 1 + JSON error if no files in window).
  - Explicit Fix Plan thresholds: `critical_safety_gates > 0` OR `total_errors > 50` OR `budget_blocks > 10`.
  - Decision distribution (buy/sell/hold/skip ratios from aggregated decision summaries).
  - DB growth delta (first vs last artifact `file_size_bytes`).
  - Secret scrubbing on all output (wallet addresses, API keys, condition IDs).
  - Streaming/iterative processing (no load-all-into-memory).
  - `structlog` logging (no `print()`).
- `.opencode/commands/server-runtime-review.md` — openclaude skill with:
  - Pre-flight hydration (STATE.md, .env validation, artifact directory existence check).
  - Run aggregator via Bash (never LLM arithmetic).
  - Read LLM review trends from `docs/operations/runtime_reviews/latest.md`.
  - Generate 12-section observation report to `docs/runtime_observations/{YYYY-MM-DD}-server-runtime-session.md`.
  - Conditional 14-section fix plan to `docs/runtime_observations/{YYYY-MM-DD}-server-fix-plan.md` (only when thresholds exceeded).
  - Secret redaction rules matching `_FORBIDDEN_CONTENT_PATTERNS`.
  - Error handling for zero artifacts, malformed JSON, missing directories.
- `deploy/systemd/poly-oracle-server-review.service` — systemd unit:
  - Binary: `openclaude` (not `/usr/local/bin/claude`).
  - `EnvironmentFile=/opt/poly-oracle-agent/.env` (not hardcoded API key).
  - `ProtectSystem=strict`, `ReadWritePaths=` constrained to `docs/runtime_observations/`.
- `deploy/systemd/poly-oracle-server-review.timer` — systemd timer:
  - 24h cadence with 72h lookback (rolling coverage, no blind spots).
  - `Persistent=true`.
- `tests/unit/test_WI-62-server-runtime-review.py` — unit tests.
- `tests/integration/test_WI-62-server-runtime-review.py` — integration tests.

## Acceptance Criteria

1. `aggregate_audits.py --hours 72` exits with code 0 and prints valid JSON when artifacts exist in the window.
2. `aggregate_audits.py --hours 72` exits with code 1 and prints `{"error": "no_artifacts_in_window", ...}` when zero artifacts match the time window.
3. All arithmetic in the aggregator uses `Decimal` — no `float` coercion for exposure, response time, or ratios.
4. Aggregator output JSON contains: `scanned_files`, `total_errors`, `total_warnings`, `budget_blocks`, `provider_failures`, `critical_safety_gates`, `avg_response_time_ms`, `max_exposure_usdc`, `db_growth_bytes_delta`, `dry_run_posture`, `decision_distribution` (buy/sell/hold/skip counts), `fix_plan_required` (boolean based on explicit thresholds).
5. Aggregator output is scrubbed of wallet addresses, API keys, condition IDs, token IDs per `_FORBIDDEN_CONTENT_PATTERNS` in `runtime_audit.py`.
6. `.opencode/commands/server-runtime-review.md` follows the same structural rigor as `dry-run-review.md`: pre-flight checks, error handling, canonical templates, secret redaction, explicit rules.
7. The skill never performs LLM arithmetic — all numeric computation is delegated to the Python aggregator.
8. The skill never modifies source code (`.py` files). Read-only reporting process.
9. systemd service uses `EnvironmentFile` (not hardcoded secrets), `ProtectSystem=strict`, and correct `ReadWritePaths`.
10. systemd timer runs every 24h (not 72h) with `Persistent=true` for rolling 72h coverage.
11. Observation report uses the exact 12-section structure from `2026-05-17-orchestrator-dry-run-session.md`.
12. Fix plan (when generated) uses the exact 14-section structure from `2026-05-17-orchestrator-fix-plan.md`.
13. Fix plan is generated only when `fix_plan_required=true` in aggregator output (explicit thresholds, not subjective).
14. Tests cover: aggregator happy path, zero-artifact path, malformed-artifact skipping, Decimal integrity, secret scrubbing, threshold boundary conditions.
15. Full regression passes with coverage >= 80%.

## Anti-Patterns

- Do not perform arithmetic in the LLM context. The aggregator produces all numbers; the LLM only synthesizes narrative.
- Do not load all 288 artifacts into memory simultaneously. Use iterative/streaming processing with accumulators.
- Do not use `float` for any numeric field. `Decimal` end-to-end.
- Do not hardcode API keys in systemd service files. Use `EnvironmentFile`.
- Do not flag `dry_run=true` as a critical finding. Document it as context; only flag if it changed unexpectedly.
- Do not generate a Fix Plan for subjective reasons. Only when explicit thresholds are exceeded.
- Do not modify source code during the review. This is a read-only reporting process.
- Do not persist secrets, wallet addresses, or high-cardinality identifiers in any output artifact.
- Do not use `print()` in any generated code. Use `structlog` only.
- Do not weaken `DRY_RUN`, bypass `LLMEvaluationResponse`, or skip Gatekeeper in any recommendation.
- Do not enable live trading or change `DRY_RUN=false` behavior.
- Do not add live signing or broadcasting.
- Do not add presentation fields to `LLMEvaluationResponse`.
- Do not invoke execution routing, transaction signing, order broadcasting, or wallet mutation paths.
- Do not write to runtime database tables from aggregator or skill code.
- Do not create an Alembic migration.
- Do not call `Base.metadata.create_all()`.
- Do not treat missing data as zero unless typed evidence proves zero.
- Do not fabricate metrics, ratios, or summaries when artifacts are absent or malformed.

## Dependencies

- WI-61 — Periodic Runtime Audit (produces the JSON artifacts this skill consumes).
- WI-61 brief context at `01_Brief Context/WI-61-periodic-runtime-audit.md`.
- WI-61 business logic at `docs/deliverables/business_logic/business_logic_WI-61-periodic-runtime-audit.md`.
- WI-61 implementation prompt at `docs/deliverables/implementation_prompts/prompt_WI-61-periodic-runtime-audit.md`.
- `src/schemas/runtime_audit.py` — typed `RuntimeAuditReport` schema consumed by the aggregator.
- `src/observability/runtime_audit.py` — `_FORBIDDEN_CONTENT_PATTERNS` reused for secret scrubbing.
- `docs/runtime_observations/2026-05-17-orchestrator-dry-run-session.md` — canonical 12-section observation template.
- `docs/runtime_observations/2026-05-17-orchestrator-fix-plan.md` — canonical 14-section fix plan template.
- `.opencode/commands/dry-run-review.md` — reference for skill structural rigor.
- `deploy/systemd/poly-oracle-runtime-audit.{service,timer}` — WI-61 systemd units for reference.
- openclaude CLI installed on the server with headless mode support (`-p` flag or equivalent).
- Provider API key configured in `/opt/poly-oracle-agent/.env` for openclaude to use.
- Existing `structlog` logging conventions.
- No new Python package dependencies.

## Target Layer

Observability and operations (cross-cutting). The aggregator lives in `scripts/ops/aggregate_audits.py` and is invoked by the openclaude skill via systemd. It reads WI-61 artifacts from `docs/operations/runtime_audits/` and produces reports in `docs/runtime_observations/`. It does not sit inside the 4-layer trading pipeline and never participates in evaluation, gating, signing, or execution.
