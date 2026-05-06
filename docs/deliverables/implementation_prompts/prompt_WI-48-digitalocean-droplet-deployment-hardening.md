# Implementation Prompt - WI-48 DigitalOcean Droplet Deployment Hardening

## Session Context

You are working in `poly-oracle-agent` on Phase 14: DigitalOcean 24/7 Paper-Trading Deployment.

Current baseline:

- Phase 13 completed with 1041 tests and 93% coverage.
- The runtime already has local `/healthz`, `/readyz`, and `/metrics` observability servers.
- `docker-compose.yml` exists but the orchestrator healthcheck is currently an import check.
- `DRY_RUN=true` remains mandatory; Phase 14 is not live-trading approval.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v14.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-48-digitalocean-droplet-deployment-hardening.md`
- `docker-compose.yml`
- `Dockerfile`
- `entrypoint.sh`
- `.env.example`
- `src/core/config.py`
- `src/observability/health_server.py`
- `src/observability/metrics_server.py`

## Objective

Harden the DigitalOcean single-Droplet deployment path so the existing Docker Compose paper-trading stack can run continuously with persistent SQLite storage, private observability surfaces, bounded deployment validation, and a mandatory dry-run guard.

## Inputs

- Existing Docker Compose orchestrator service.
- Existing Dockerfile and entrypoint.
- Existing health and metrics HTTP endpoints.
- Operator-managed `.env` file on the Droplet.
- Persistent `/data` Docker volume.

## Outputs

- Updated `docker-compose.yml` where needed.
- Updated `Dockerfile` or `entrypoint.sh` only if needed for the real healthcheck.
- Updated `.env.example` with secret-free deployment defaults or comments where needed.
- `docs/runbooks/digitalocean-droplet-deployment.md`
- `scripts/ops/check_deployment.py`
- `tests/integration/test_WI-48-digitalocean-droplet-deployment-hardening.py`

## Acceptance Criteria

1. The runbook gives a complete ordered path from fresh Ubuntu 24.04 Droplet to running Docker Compose service.
2. The runbook requires SSH-key-only access, a non-root deploy user, firewall hardening, Docker Engine, Compose plugin, swap, log rotation, and SQLite backup/export steps.
3. Compose persists SQLite at `/data/poly_oracle.db`.
4. Compose or Docker healthcheck probes `/healthz` or `/readyz`; it must not rely on a trivial Python import check.
5. Health, readiness, and metrics bind to loopback or firewall-protected interfaces by default.
6. `scripts/ops/check_deployment.py` validates Compose service status, dry-run guard, `/healthz`, `/readyz`, `/metrics`, and forbidden metrics labels.
7. The deployment checker uses explicit HTTP and subprocess timeouts.
8. The deployment checker exits non-zero when a mandatory check fails.
9. Tests cover success, readiness failure, metrics secret rejection, and dry-run-required failure.
10. No real secret, Droplet IP tied to secrets, wallet key, API key, Telegram token, prompt text, or reasoning text is committed.
11. Targeted WI tests pass.
12. Full regression remains compatible with the Phase 13 baseline and coverage stays >= 80%.

## Anti-Patterns

- Do not enable `DRY_RUN=false`.
- Do not add live signing or broadcasting paths.
- Do not commit `.env` or real operator values.
- Do not expose health, readiness, metrics, dashboard, or SQLite publicly.
- Do not use an import-only container healthcheck.
- Do not require a Python virtualenv on the Droplet.
- Do not introduce a new Python dependency unless explicitly justified and approved.
- Do not log or report raw environment values.
- Do not use unbounded subprocess or HTTP calls.
- Do not mark degraded readiness as healthy unless the response has a bounded reason and the checker mode explicitly allows degraded.

## Dependencies

- Existing `HealthServer` and `MetricsServer`.
- Existing `AppConfig` health and metrics host/port fields.
- Existing Alembic runtime migration path in `entrypoint.sh`.
- Docker and Docker Compose plugin installed on the target Droplet.

## Target Layer

Deployment and operations layer. This WI must not change trading strategy, Gatekeeper validation, order routing, signing, or broadcast behavior.
