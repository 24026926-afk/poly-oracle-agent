# Business Logic - WI-49 Secure Remote Operator Dashboard Access

## Objective

Make the existing Streamlit Command Center available to the operator from a local browser while keeping the dashboard private, read-only, secret-free, and backed by the deployed `/data/poly_oracle.db` audit database.

## Data Models

Pydantic schema names only:

- `DashboardRuntimeConfig`
- `DashboardDatabaseTarget`
- `DashboardAccessMode`
- `DashboardTunnelSpec`
- `DashboardReadOnlyCheck`
- `DashboardExposureCheck`
- `DashboardAccessValidationReport`

## Key Rules

1. Docker Compose must add a profile-gated dashboard service; the dashboard must not start by default with the orchestrator.
2. The dashboard service must mount the same persistent `/data` volume as the orchestrator.
3. The deployed dashboard database path must default to `/data/poly_oracle.db` inside the container.
4. Local development must keep a safe default database path for the existing Streamlit workflow.
5. The dashboard database path must be configurable through an environment variable.
6. Dashboard SQLite access must be read-only, preferably through SQLite URI mode (`mode=ro`) or an equivalent read-only connection guard.
7. `src/ui/` must not introduce `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP`, `ALTER`, `REPLACE`, migration, or state-mutating query paths.
8. The default remote access path is SSH tunneling from the operator machine to the Droplet.
9. Streamlit must bind to loopback or private access by default; public unauthenticated exposure is prohibited.
10. Optional reverse-proxy documentation must require TLS, authentication, and IP allowlisting, and must remain clearly non-default.
11. The dashboard must not expose wallet private keys, API keys, Telegram tokens, raw prompt text, private reasoning text, full environment configuration, or secret-like fields.
12. Dashboard access is observability-only and cannot write to the runtime database or authorize trades.
13. Health, metrics, dashboard, and SQLite ports must not be opened publicly as part of this WI.

## Edge Cases

1. Dashboard profile is not enabled: orchestrator starts normally without dashboard.
2. SQLite file does not exist yet: dashboard renders a controlled empty state rather than creating a database.
3. SQLite file exists but is locked by the orchestrator: dashboard uses bounded reads and shows a controlled unavailable state.
4. Read-only connection cannot be opened: dashboard reports unavailable state without falling back to writable access.
5. Operator uses the wrong SSH tunnel port: runbook gives a deterministic diagnostic path.
6. Dashboard service starts before the orchestrator creates `/data/poly_oracle.db`: dashboard remains read-only and does not create schema.
7. Optional reverse proxy is requested: docs require explicit operator approval and hardened controls.
8. Sensitive fields appear in a database row or config source: dashboard suppresses or redacts them.
9. Local development runs without Docker: existing local dashboard path remains usable.
10. Streamlit tries to cache stale reads: cache TTL must remain bounded and operator refresh must still work.

## Invariants

1. Dashboard access cannot bypass `LLMEvaluationResponse`.
2. Dashboard access cannot sign, broadcast, route, or mutate orders.
3. Dashboard access cannot weaken `dry_run` safety.
4. Dashboard database access remains read-only.
5. Dashboard defaults remain private.
6. No secrets or raw private operational payloads are displayed.
7. The dashboard is an observability surface, not a trading control surface.
8. A working dashboard tunnel is not live-trading approval.
9. Existing dashboard functionality must not regress for local operators.
10. Public unauthenticated dashboard exposure remains prohibited.
