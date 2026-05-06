# Implementation Prompt - WI-49 Secure Remote Operator Dashboard Access

## Session Context

You are working in `poly-oracle-agent` on Phase 14: DigitalOcean 24/7 Paper-Trading Deployment.

Current baseline:

- Phase 12 added the Streamlit Command Center in `src/ui/dashboard.py`.
- The dashboard currently reads a local SQLite path derived from the repository root.
- Phase 14 requires a private remote dashboard path for the deployed `/data/poly_oracle.db`.
- Public unauthenticated dashboard exposure is prohibited.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v14.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-49-secure-remote-operator-dashboard-access.md`
- `docker-compose.yml`
- `.env.example`
- `src/ui/dashboard.py`
- Existing dashboard tests if present

## Objective

Make the existing Streamlit dashboard usable through a secure SSH tunnel against the deployed SQLite database while preserving read-only behavior and private access by default.

## Inputs

- Existing Streamlit dashboard code.
- Existing Docker Compose file and persistent `/data` volume.
- Deployed SQLite database at `/data/poly_oracle.db`.
- Operator SSH access to the Droplet.

## Outputs

- Updated `docker-compose.yml` with a profile-gated `dashboard` service.
- Updated `.env.example` with secret-free dashboard configuration where needed.
- Updated `src/ui/dashboard.py` for configurable read-only database access.
- `docs/runbooks/streamlit-ssh-tunnel.md`
- `tests/unit/test_WI-49-secure-remote-operator-dashboard-access.py`
- `tests/integration/test_WI-49-secure-remote-operator-dashboard-access.py`

## Acceptance Criteria

1. `docker compose --profile dashboard up -d dashboard` starts only when the dashboard profile is requested.
2. The dashboard service mounts the same persistent `/data` volume as the orchestrator.
3. The deployed dashboard uses `/data/poly_oracle.db` by default inside the container.
4. Local dashboard usage still has a safe local default.
5. The dashboard database path is configurable through an environment variable such as `DASHBOARD_DB_PATH`.
6. SQLite is opened read-only; missing databases must not be created by dashboard reads.
7. `src/ui/` contains no write SQL verbs or migration paths.
8. Streamlit binds to loopback/private access by default.
9. The SSH tunnel runbook includes exact operator-machine command, local browser URL, verification, and shutdown steps.
10. Optional reverse-proxy notes, if included, require TLS, authentication, and IP allowlisting and are clearly non-default.
11. Dashboard output does not expose secrets, wallet private key material, raw prompt text, private reasoning text, or full config dumps.
12. Tests verify DB path configuration, read-only SQLite behavior, Compose profile presence, and forbidden SQL write absence.
13. Targeted WI tests pass.
14. Full regression remains compatible with the documented baseline and coverage stays >= 80%.

## Anti-Patterns

- Do not run the dashboard by default with the orchestrator service.
- Do not expose Streamlit publicly without authentication.
- Do not add write operations to the dashboard.
- Do not create SQLite files from dashboard reads.
- Do not open public inbound dashboard, health, metrics, or SQLite ports.
- Do not display secrets, raw prompt text, private reasoning text, or full environment values.
- Do not turn the dashboard into a trading control surface.
- Do not weaken `DRY_RUN=true`.

## Dependencies

- Existing Streamlit, pandas, and Plotly dependencies.
- Existing SQLite audit database schema.
- Existing Docker Compose volume.
- Operator SSH access configured by WI-48.

## Target Layer

Read-only operator UI and deployment access layer. This WI must not change LLM evaluation, Gatekeeper validation, execution routing, signing, or broadcast behavior.
