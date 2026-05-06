# Implementation Prompt - WI-51 24-7 Paper-Trading Soak Test and Runbook

## Session Context

You are working in `poly-oracle-agent` on Phase 14: DigitalOcean 24/7 Paper-Trading Deployment.

Current baseline:

- WI-48 should provide DigitalOcean deployment hardening and a deployment checker.
- WI-49 should provide private Streamlit dashboard access.
- WI-50 should provide Telegram operational alerts.
- Phase 14 cannot complete until a real dry-run soak report exists under `docs/operations/`.
- Passing a soak test does not authorize live trading.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v14.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-51-24-7-paper-trading-soak-test-and-runbook.md`
- `docs/runbooks/digitalocean-droplet-deployment.md` if WI-48 is complete
- `docs/runbooks/streamlit-ssh-tunnel.md` if WI-49 is complete
- `docs/runbooks/telegram-operational-alerts.md` if WI-50 is complete
- `scripts/ops/check_deployment.py` if WI-48 is complete
- `docker-compose.yml`

## Objective

Create the paper-trading soak-test runbook and evidence collector that prove the deployed dry-run runtime can operate continuously with durable audit data, observable health, and documented recovery paths.

## Inputs

- Deployed Docker Compose runtime.
- `/healthz`, `/readyz`, and `/metrics` endpoints.
- Persistent SQLite database under `/data`.
- Compose service status and restart counts.
- Telegram operational alert status where enabled.
- Operator-provided soak start and end timestamps.

## Outputs

- `docs/runbooks/paper-trading-soak-test.md`
- `scripts/ops/collect_soak_evidence.py`
- Generated report path support for `docs/operations/phase14-soak-report.md`
- Generated report path support for `docs/operations/phase14-soak-report.json`
- `tests/integration/test_WI-51-paper-trading-soak-test-and-runbook.py`

## Acceptance Criteria

1. The runbook defines setup, start criteria, minimum 24-hour duration, preferred 72-hour duration, evidence collection, pass/fail criteria, and recovery steps.
2. The runbook states that `DRY_RUN=true` is required for the full soak.
3. The runbook states that passing the soak does not authorize `DRY_RUN=false`.
4. The evidence collector writes both markdown and JSON reports under `docs/operations/`.
5. The evidence collector confirms dry-run mode and exits non-zero if dry run is false or missing.
6. Evidence includes Compose status, restart count, health, readiness, metrics, DB file presence, DB growth, recent decision count, recent market snapshot count, Telegram status where enabled, and restart or reboot recovery evidence.
7. Evidence collection uses explicit HTTP, subprocess, and SQLite timeouts or bounded retry behavior.
8. Reports redact secrets, raw prompt text, reasoning text, wallet private keys, API keys, Telegram tokens, raw token IDs, raw condition IDs, and private environment dumps.
9. Missing mandatory evidence produces failed or incomplete verdict, not pass.
10. Recovery steps cover container stopped, readiness degraded, WebSocket stale, disk nearly full, SQLite backup needed, and dashboard tunnel unavailable.
11. Tests verify report generation, redaction, failed-readiness reporting, missing-metrics handling, dry-run-required failure, and output path constraints.
12. Targeted WI tests pass.
13. Full regression remains compatible with the documented baseline and coverage stays >= 80%.

## Anti-Patterns

- Do not generate a passing report for a soak shorter than 24 hours.
- Do not authorize live trading from soak evidence.
- Do not write reports outside `docs/operations/`.
- Do not include secrets, raw prompts, reasoning text, wallet material, API keys, Telegram tokens, token IDs, or condition IDs in reports.
- Do not dump full `.env` contents.
- Do not mutate runtime DB state.
- Do not use `Base.metadata.create_all()`.
- Do not rely on unbounded HTTP, subprocess, or SQLite reads.
- Do not hide missing metrics or degraded readiness behind a passing verdict.

## Dependencies

- WI-48 deployment checker and deployment runbook.
- WI-49 dashboard tunnel runbook.
- WI-50 operational alert runbook.
- Existing health and metrics endpoints.
- Existing SQLite audit schema.
- Existing Docker Compose runtime.

## Target Layer

Operational validation and audit evidence layer. This WI must not alter trading strategy, Gatekeeper validation, execution routing, order signing, or broadcasting.
