# Business Logic - WI-48 DigitalOcean Droplet Deployment Hardening

## Objective

Prepare a single DigitalOcean Ubuntu Droplet to run the existing Docker Compose paper-trading stack continuously with persistent SQLite audit storage, host hardening, bounded health checks, and a mandatory `DRY_RUN=true` deployment guard.

## Data Models

Pydantic schema names only:

- `DeploymentCheckStatus`
- `DeploymentProbeResult`
- `ComposeServiceStatus`
- `HTTPProbeResult`
- `DryRunGuardResult`
- `MetricsInspectionResult`
- `DeploymentValidationReport`
- `DeploymentFailureReason`

## Key Rules

1. Deployment targets a single-node Ubuntu 24.04 DigitalOcean Droplet running Docker Engine and the Docker Compose plugin.
2. Host setup must require SSH-key-only access, a non-root deploy user, least-privilege sudo usage, and UFW or DigitalOcean firewall rules.
3. No public inbound port may be opened except SSH unless a later approved WI adds a secured access path.
4. `DRY_RUN=true` is mandatory for every deployment, validation, and soak path in Phase 14.
5. A real `.env` file is operator-managed secret material and must never be committed, copied into reports, echoed in docs, or logged by validation tooling.
6. Docker Compose must persist SQLite under `/data/poly_oracle.db` so audit data survives container rebuilds and restarts.
7. Container healthchecks must call the real local HTTP observability surface (`/healthz` or `/readyz`), not a trivial import check.
8. `/healthz`, `/readyz`, and `/metrics` must bind to loopback or remain firewall-protected by default.
9. Deployment validation must use explicit HTTP and subprocess timeouts.
10. Deployment validation must exit non-zero when mandatory checks fail.
11. `/readyz` may be `ready` or `degraded` only when the response includes a typed reason; unreachable or malformed readiness is a failed mandatory check.
12. `/metrics` output must be Prometheus text and must not contain forbidden secret-like fields, prompt text, reasoning text, wallet material, token IDs, raw condition IDs, or high-cardinality labels.
13. The runbook must document minimal 1GB Droplet hardening, including swap, disk checks, log rotation, restart policy, and SQLite backup/export.
14. No Python virtualenv setup is required on the server; the server runtime path is Docker Compose.

## Edge Cases

1. Docker or the Compose plugin is not installed: validation fails with a bounded, typed failure.
2. Compose service exists but is not running: validation fails with service status evidence.
3. Container is restarting repeatedly: validation reports restart count and fails the readiness gate.
4. `.env` is absent: validation fails before probing live services.
5. `.env` exists but `DRY_RUN` is missing or false: validation fails closed.
6. `.env` contains secrets: validation may confirm required keys exist only through redacted presence checks.
7. Health server is disabled or bound to a different port: validation reports connection failure without hanging.
8. Readiness returns degraded due to WebSocket state: validation records the bounded reason and continues only if degraded is explicitly allowed by the checker mode.
9. Metrics endpoint is disabled: validation fails the metrics gate unless the operator explicitly runs a health-only mode.
10. Metrics output contains a forbidden label or secret-like key: validation fails.
11. SQLite file is missing under `/data`: deployment is not considered persistent.
12. Disk is nearly full: runbook recovery path requires operator intervention before soak testing.

## Invariants

1. Deployment hardening cannot authorize `DRY_RUN=false`.
2. Deployment tooling cannot sign, broadcast, or mutate live orders.
3. Deployment validation cannot bypass `LLMEvaluationResponse`.
4. Deployment validation is observability-only and must not mutate trading state.
5. No real API key, wallet private key, Telegram token, Droplet IP tied to secrets, prompt text, or private reasoning payload is committed.
6. All HTTP, subprocess, and service checks are bounded by explicit timeouts.
7. Runtime database schema remains Alembic-managed.
8. Runtime persistence remains repository-based; deployment evidence may inspect files and services only for read-only operational validation.
9. Docker and host instructions must remain compatible with x86_64 Linux droplets.
10. A passing deployment check is not live-trading approval.
