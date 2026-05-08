# PRD-v14.0 — Phase 14: DigitalOcean 24/7 Paper-Trading Deployment

**Version:** 14.0
**Status:** READY FOR IMPLEMENTATION
**Phase:** 14
**Author:** Staff Architect / Quantitative Systems Engineer
**Date:** 2026-05-06
**Baseline:** Phase 13 complete — 1041 tests, 93% coverage, live Grok/Sonnet dry-run paper trading enabled, health and metrics servers available

---

## 1. Objective

Deploy the agent to a DigitalOcean Droplet for stable 24/7 paper trading while preserving `DRY_RUN=true`, secret hygiene, SQLite audit persistence, and operator visibility.

Phase 14 is an operational hardening phase, not a live-trading phase. The system may run continuously with real upstream APIs and real LLM credentials, but no work item may enable signing, broadcasting, or any path that weakens dry-run enforcement.

---

## 2. Scope Boundaries

**In scope:**
- DigitalOcean Basic Droplet deployment for a single-node paper-trading runtime.
- Docker Compose runtime hardening for the existing `orchestrator` service.
- Host-level setup documentation for SSH-key-only access, firewall rules, Docker installation, swap, log rotation, restart policy, and SQLite persistence.
- External verification of `/healthz`, `/readyz`, and `/metrics` without exposing secrets or raw trading payloads.
- Secure remote access to the Streamlit dashboard, with SSH tunneling as the default path.
- Telegram operational alerts for process restart, sustained readiness/WebSocket degradation, and circuit breaker state changes.
- A 24/7 paper-trading soak test and operator runbook with auditable pass/fail evidence.

**Out of scope:**
- `DRY_RUN=false`, live trading approval, live order signing, or live broadcast.
- PostgreSQL or any managed database migration.
- Kubernetes, managed Prometheus, Grafana, load balancers, or multi-node orchestration.
- Public unauthenticated dashboard, health, readiness, metrics, or database access.
- Committing `.env`, real API keys, wallet private keys, Telegram tokens, server IPs tied to secrets, or screenshots containing secrets.
- Strategy, prompt, Kelly, threshold, or risk-parameter optimization.
- Generating WI business-logic or implementation-prompt deliverables during PRD creation. Those are generated one at a time via `/wi-start`.

---

## 3. Work Items

### WI-48 — DigitalOcean Droplet Deployment Hardening

**Goal:** Prepare a $6/month-class DigitalOcean Ubuntu Droplet to run the existing Docker Compose paper-trading stack continuously with persistent SQLite storage, host hardening, and externally verifiable health.

#### 3.1 File Structure

```
docker-compose.yml
Dockerfile
entrypoint.sh
.env.example

docs/
└── runbooks/
    └── digitalocean-droplet-deployment.md

scripts/
└── ops/
    └── check_deployment.py

tests/
└── integration/
    └── test_WI-48-digitalocean-droplet-deployment-hardening.py
```

#### 3.2 Core Requirements

- Document an Ubuntu 24.04 Droplet deployment path using SSH-key-only access.
- Require a non-root deploy user, UFW or DigitalOcean firewall rules, and no public inbound port except SSH unless explicitly approved by the operator.
- Install Docker Engine and Docker Compose plugin through documented commands; do not require local Python virtualenv setup on the server.
- Keep `DRY_RUN=true` mandatory in deployment instructions and validation scripts.
- Treat the real `.env` as operator-managed secret material outside git; never write real key values into docs, fixtures, logs, or committed config.
- Configure Compose persistence so SQLite lives under the mounted `/data` volume and survives container rebuilds/restarts.
- Add or update healthchecks so container readiness probes the actual HTTP health/readiness surface, not only Python importability.
- Expose `/healthz`, `/readyz`, and `/metrics` only on loopback or firewall-protected interfaces unless a later WI explicitly opens a secured path.
- Add a deployment checker that validates:
  - Docker/Compose service is running.
  - `DRY_RUN=true` is visible through safe config inspection or a redacted environment check.
  - `/healthz` returns liveness.
  - `/readyz` returns ready or degraded with a typed reason.
  - `/metrics` returns Prometheus text without forbidden labels or secrets.
- The checker must use explicit HTTP timeouts and exit non-zero on failed mandatory checks.
- Document minimal memory hardening for a 1GB Droplet, including swap configuration and operational caveats.
- Document SQLite backup/export commands from the `/data` volume without requiring PostgreSQL.

#### 3.3 Definition of Done — WI-48

- [ ] `docs/runbooks/digitalocean-droplet-deployment.md` contains a complete, ordered deployment procedure from fresh Droplet to running container.
- [ ] Compose runtime preserves `/data/poly_oracle.db` across container rebuilds and restarts.
- [ ] Container healthcheck uses `/healthz` or `/readyz` rather than a trivial import check.
- [ ] `scripts/ops/check_deployment.py` validates liveness, readiness, metrics, Compose status, and dry-run guard with bounded timeouts.
- [ ] No real secret value, wallet key, API key, Droplet IP, or Telegram token is committed.
- [ ] Integration tests cover deployment-check success, readiness failure, metrics secret rejection, and dry-run-required failure.

---

### WI-49 — Secure Remote Operator Dashboard Access

**Goal:** Make the existing Streamlit Command Center usable from the operator's browser while keeping the dashboard private and read-only.

#### 4.1 File Structure

```
docker-compose.yml
.env.example

src/
└── ui/
    └── dashboard.py

docs/
└── runbooks/
    └── streamlit-ssh-tunnel.md

tests/
├── unit/
│   └── test_WI-49-secure-remote-operator-dashboard-access.py
└── integration/
    └── test_WI-49-secure-remote-operator-dashboard-access.py
```

#### 4.2 Core Requirements

- Add a profile-gated Streamlit dashboard service to Compose rather than running it by default.
- Dashboard service must mount the same persistent `/data` volume as the orchestrator and read the deployed SQLite database.
- Make the dashboard database path configurable by environment variable while preserving the existing local default.
- Dashboard must remain read-only; no `INSERT`, `UPDATE`, `DELETE`, schema migration, or state mutation path may be introduced in `src/ui/`.
- Bind Streamlit to loopback/private access by default. Public dashboard exposure is prohibited unless protected by a later explicitly approved reverse-proxy WI.
- Provide an SSH tunnel runbook from operator machine to Droplet, including local browser URL and shutdown steps.
- If Nginx reverse proxy is documented as an optional alternative, it must require TLS, basic auth or stronger authentication, and IP allowlisting; it must not be the default.
- Do not expose health, metrics, dashboard, or SQLite ports publicly as part of this WI.
- Dashboard must not show secrets, wallet private key material, raw prompts, or full private operational configuration.

#### 4.3 Definition of Done — WI-49

- [ ] `docker compose --profile dashboard up -d dashboard` starts a dashboard service against `/data/poly_oracle.db`.
- [ ] Dashboard DB path can be set through an environment variable and defaults safely for local development.
- [ ] `docs/runbooks/streamlit-ssh-tunnel.md` gives exact SSH tunnel and browser access steps.
- [ ] Dashboard access path does not require public inbound Streamlit/Nginx exposure.
- [ ] Tests verify DB path configuration, read-only SQL behavior, Compose profile presence, and no forbidden SQL writes in `src/ui/`.

---

### WI-50 — Telegram Operational Alert Bridge

**Goal:** Notify the operator when the deployed paper-trading runtime needs attention without requiring SSH polling.

#### 5.1 File Structure

```
src/
├── observability/
│   └── operational_alerts.py
├── schemas/
│   └── ops.py
└── orchestrator.py

docs/
└── runbooks/
    └── telegram-operational-alerts.md

tests/
├── unit/
│   └── test_WI-50-telegram-operational-alert-bridge.py
└── integration/
    └── test_WI-50-telegram-operational-alert-bridge.py
```

#### 5.2 Core Requirements

- Reuse the existing `TelegramNotifier` transport and configuration fields where possible.
- Add typed operational alert schemas for bounded alert types:
  - process started or restarted
  - `/readyz` unhealthy or degraded for more than 5 minutes
  - WebSocket disconnected or PONG stale for more than 5 minutes
  - circuit breaker opened
  - circuit breaker returned to closed
- Operational alerts must be deduplicated with a cooldown so persistent failures do not spam the operator.
- Alert payloads must be concise and secret-free. They may include alert type, severity, first-seen timestamp, duration, service name, and bounded reason code.
- Alert evaluation must be read-only and must not mutate trading state.
- Alert evaluation must not block ingestion, context, evaluation, execution, health, or metrics loops.
- Telegram send attempts must use explicit timeout and bounded retry behavior already established for `TelegramNotifier`.
- If Telegram is disabled or credentials are missing, the runtime must continue normally and log a structured disabled reason.
- Startup/restart alert must not be sent in tests unless explicitly enabled through config.
- Circuit breaker alerts must be based on existing typed circuit breaker state, not string parsing from logs.

#### 5.3 Definition of Done — WI-50

- [ ] Typed operational alert schemas reject unknown alert types and raw secret-like fields.
- [ ] Sustained readiness/WebSocket degradation triggers one Telegram alert after the configured threshold.
- [ ] Repeated degraded checks inside cooldown do not send duplicate Telegram messages.
- [ ] Circuit breaker open/closed transitions trigger bounded operational alerts.
- [ ] Runtime continues without failure when Telegram is disabled or credentials are absent.
- [ ] Tests cover restart, sustained degraded readiness, stale WebSocket, circuit breaker transitions, dedupe cooldown, disabled Telegram, and secret-free payloads.

---

### WI-51 — 24/7 Paper-Trading Soak Test and Runbook

**Goal:** Prove the deployed dry-run system can run continuously on the Droplet with durable audit data, observable health, and clear operator recovery steps.

#### 6.1 File Structure

```
docs/
├── operations/
│   ├── phase14-soak-report.md
│   └── phase14-soak-report.json
└── runbooks/
    └── paper-trading-soak-test.md

scripts/
└── ops/
    └── collect_soak_evidence.py

tests/
└── integration/
    └── test_WI-51-paper-trading-soak-test-and-runbook.py
```

#### 6.2 Core Requirements

- Define a minimum 24-hour paper-trading soak test; 72 hours is preferred before considering any later live-readiness discussion.
- Require `DRY_RUN=true` for the full soak duration.
- Collect evidence from:
  - Compose service status and restart count.
  - `/healthz`, `/readyz`, and `/metrics`.
  - SQLite file presence and growth under `/data`.
  - recent decision count and market snapshot count.
  - Telegram alert delivery status where enabled.
  - host reboot or container restart recovery.
- `collect_soak_evidence.py` must produce both markdown and JSON reports under `docs/operations/`.
- The evidence script must redact secrets and must not include raw prompt text, reasoning text, wallet private keys, API keys, Telegram tokens, or high-cardinality market identifiers.
- The runbook must include operator recovery steps for:
  - container stopped
  - readiness degraded
  - WebSocket stale
  - disk nearly full
  - SQLite backup needed
  - dashboard tunnel unavailable
- The soak report must be an audit artifact, not a source of trading authorization.
- Passing the soak test does not authorize `DRY_RUN=false`.

#### 6.3 Definition of Done — WI-51

- [ ] `docs/runbooks/paper-trading-soak-test.md` defines setup, evidence collection, pass/fail criteria, and recovery steps.
- [ ] `scripts/ops/collect_soak_evidence.py` writes secret-free markdown and JSON reports.
- [ ] Soak evidence includes health, readiness, metrics, restart count, DB persistence, and dry-run confirmation.
- [ ] A host reboot or container restart recovery check is documented and represented in the report schema.
- [ ] Tests verify report generation, redaction, failed-readiness reporting, missing-metrics handling, and dry-run-required failure.
- [ ] Phase 14 cannot be marked complete until a real soak report exists under `docs/operations/`.

---

## 4. Phase 14 Definition of Done

Phase 14 is complete when all WI DoDs pass and the following global gates are satisfied:

1. **Deployment gate:** A DigitalOcean Droplet can run the Docker Compose paper-trading stack continuously with persistent `/data/poly_oracle.db`.
2. **Dry-run gate:** `DRY_RUN=true` is verified by deployment checks and soak evidence; `DRY_RUN=false` remains prohibited.
3. **Secret gate:** No committed file contains real API keys, wallet private keys, Telegram tokens, Droplet-specific secrets, raw prompt text, or private reasoning payloads.
4. **Health gate:** `/healthz`, `/readyz`, and `/metrics` are reachable through secured operator paths and are not publicly exposed without explicit protection.
5. **Dashboard gate:** Streamlit is remotely accessible through an SSH tunnel or stronger private access path and remains read-only.
6. **Alert gate:** Telegram operational alerts cover restart, sustained readiness/WebSocket degradation, and circuit breaker transitions with dedupe cooldown.
7. **Persistence gate:** SQLite audit data survives container restart and rebuild; backup/export instructions are documented.
8. **Soak gate:** A minimum 24-hour dry-run soak report exists under `docs/operations/` with pass/fail evidence.
9. **Trading integrity gate:** No money, pricing, EV, Kelly, PnL, or sizing calculation uses raw `float`.
10. **Gatekeeper gate:** No execution path bypasses `LLMEvaluationResponse`.
11. **Safety gate:** No signing, broadcasting, or state-mutating live execution call can occur in dry run.
12. **Regression gate:** Full test suite passes with coverage >= 80% and no material regression from the Phase 13 baseline without explicit approval.
13. **MAAP gate:** Any core-logic changes under `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, or `src/backtest_runner.py` are MAAP-reviewed before commit.

---

## 5. Constraints & Non-Negotiables

1. All financial, pricing, EV, sizing, calibration, and PnL arithmetic must use `Decimal`.
2. `DRY_RUN=true` is mandatory for Phase 14 deployment, dashboard operation, alerting, and soak testing.
3. `LLMEvaluationResponse` remains the terminal Gatekeeper schema before execution.
4. `PromptFactory` must assemble real market context only; never invent balances, positions, fees, or market metadata.
5. WebSocket, HTTP, RPC, LLM, Telegram, and deployment-check paths must use explicit timeout or bounded retry behavior.
6. Runtime DB access remains repository-based in agent code. Deployment scripts may inspect SQLite only for read-only operational evidence.
7. Alembic remains the only supported schema migration path. Do not use `Base.metadata.create_all()` in runtime or deployment paths.
8. Secrets must never be logged, committed, persisted in reports, or exposed through dashboard, health, readiness, metrics, or Telegram messages.
9. Health, metrics, dashboard, soak evidence, and alerting are observability surfaces only; they cannot authorize trades.
10. No direct commits to `main`; all work remains on `develop` and feature branches.

---

## 6. Dependencies to Add

No new third-party Python dependencies are required at PRD time.

Implementation should first use the existing approved stack: Docker Compose, Python standard library, `httpx`, existing Streamlit dependencies, existing Telegram notifier, and existing observability modules. If an implementation prompt later justifies Nginx or system packages for host setup, those belong in runbook instructions and must not introduce Python runtime dependencies unless explicitly approved.

---

## 7. Deliverables Summary

| WI | Deliverable |
|---|---|
| WI-48 | DigitalOcean deployment runbook, Compose health/persistence hardening, deployment checker |
| WI-49 | Profile-gated dashboard service, configurable dashboard DB path, SSH tunnel runbook |
| WI-50 | Typed operational alert bridge using Telegram, dedupe/cooldown behavior, alert runbook |
| WI-51 | Paper-trading soak-test runbook, evidence collector, markdown/JSON soak report |

PRD generation creates only this file and updates `STATE.md`. Business-logic and implementation-prompt deliverables are intentionally deferred until `/wi-start WI-XX`.

---

## 8. State & Documentation Updates on Phase Completion

On Phase 14 completion:

1. `STATE.md` version is bumped to `0.14.0`, status updated to `Phase 14 COMPLETE`.
2. `README.md` is updated with:
   - DigitalOcean deployment summary
   - Docker Compose runtime commands
   - dashboard SSH tunnel usage
   - Telegram operational alert configuration
   - soak-test evidence commands
3. `docs/system_architecture.md` is updated to reflect the deployed single-node operational topology.
4. `docs/archive/ARCHIVE_PHASE_14.md` is generated with final WI outcomes, test counts, coverage, and operational caveats.
5. `docs/operations/phase14-soak-report.md` and `docs/operations/phase14-soak-report.json` are retained as audit artifacts.
6. `DRY_RUN=false` remains out of scope unless a later phase explicitly approves live-trading readiness.
