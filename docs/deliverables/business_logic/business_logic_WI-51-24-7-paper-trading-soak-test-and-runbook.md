# Business Logic - WI-51 24-7 Paper-Trading Soak Test and Runbook

## Objective

Define and collect auditable evidence that the deployed dry-run system can run continuously on the DigitalOcean Droplet with durable SQLite data, observable health, restart recovery, and clear operator recovery procedures.

## Data Models

Pydantic schema names only:

- `SoakVerdict`
- `SoakProbeStatus`
- `SoakProbeResult`
- `SoakServiceStatus`
- `SoakHealthEvidence`
- `SoakMetricsEvidence`
- `SoakDatabaseEvidence`
- `SoakRecoveryEvidence`
- `SoakEvidenceReport`

## Key Rules

1. The minimum soak duration is 24 hours; 72 hours is preferred before any later live-readiness discussion.
2. `DRY_RUN=true` is mandatory for the full soak duration.
3. The soak report is an audit artifact and cannot authorize `DRY_RUN=false`.
4. Evidence collection must include Compose service status and restart count.
5. Evidence collection must probe `/healthz`, `/readyz`, and `/metrics` with explicit timeouts.
6. Evidence collection must confirm SQLite file presence and growth under `/data`.
7. Evidence collection must summarize recent decision count and market snapshot count without exposing raw prompt, reasoning, token ID, or condition ID values.
8. Evidence collection should include Telegram alert delivery status when Telegram is enabled.
9. Evidence collection must document host reboot or container restart recovery status.
10. `collect_soak_evidence.py` must write both markdown and JSON reports under `docs/operations/`.
11. Reports must redact secrets and secret-like fields, including API keys, wallet private keys, Telegram tokens, RPC URLs with embedded credentials, prompt text, reasoning text, and full environment dumps.
12. Report schemas must use typed statuses and bounded reason codes.
13. The runbook must include recovery paths for container stopped, readiness degraded, stale WebSocket, disk nearly full, SQLite backup needed, and dashboard tunnel unavailable.
14. Missing evidence must produce a failed or incomplete verdict, not a silent pass.

## Edge Cases

1. Soak duration is shorter than 24 hours: report verdict is failed or incomplete.
2. `DRY_RUN` is false or missing: evidence script exits non-zero and does not emit a passing report.
3. Health endpoint is reachable but readiness is degraded: report captures degraded reason and marks pass/fail according to criteria.
4. Metrics endpoint is missing: report captures missing metrics and fails the metrics evidence gate.
5. SQLite file exists but does not grow: report flags missing persistence activity.
6. SQLite is locked during read: evidence script uses bounded retry or records unavailable status.
7. Container restart count is non-zero: report includes restart evidence and requires operator interpretation.
8. Host reboot recovery was not tested: report marks recovery evidence incomplete.
9. Telegram is disabled: report records not_applicable rather than failure.
10. Secret-like text appears in evidence: redaction must remove it before writing markdown or JSON.
11. Output directory is missing: script creates `docs/operations/` inside the project only.
12. Evidence collection is run from a developer laptop against localhost: report makes target host explicit without committing private IPs.

## Invariants

1. Soak testing cannot authorize live trading.
2. Soak evidence cannot bypass `LLMEvaluationResponse`.
3. Soak evidence cannot sign, broadcast, route, or mutate live orders.
4. `DRY_RUN=true` remains mandatory.
5. Reports are secret-free and low-cardinality.
6. Evidence collection uses bounded timeouts and exits non-zero on mandatory gate failure.
7. SQLite audit persistence remains the source of local paper-trading evidence.
8. Runtime schema remains Alembic-managed.
9. No raw prompt text, reasoning text, wallet material, API keys, or Telegram tokens are written to reports.
10. Phase 14 is not complete until a real soak report exists under `docs/operations/`.
