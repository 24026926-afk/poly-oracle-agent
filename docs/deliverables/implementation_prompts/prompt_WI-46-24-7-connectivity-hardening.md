# Implementation Prompt - WI-46 24-7 Connectivity Hardening

## Session Context

You are working in `poly-oracle-agent` on Phase 13: Real-Data Validation & 24/7 Readiness.

Current baseline:

- `CLOBWebSocketClient` exists in `src/agents/ingestion/ws_client.py`.
- Basic reconnect behavior exists but needs typed health state and better long-run observability.
- `Orchestrator` owns runtime task lifecycle.
- Phase 13 requires `/healthz` and `/readyz` local endpoints.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v13.0.md`
- `docs/deliverables/business_logic/business_logic_WI-46-24-7-connectivity-hardening.md`
- `src/agents/ingestion/ws_client.py`
- `src/orchestrator.py`
- `src/core/config.py`
- Existing WebSocket tests

## Objective

Harden WebSocket reconnect behavior and expose typed runtime health snapshots plus local liveness/readiness endpoints.

## Inputs

- Existing `CLOBWebSocketClient` state and events.
- Existing `Orchestrator` lifecycle.
- App configuration for WebSocket URL and new reconnect/health settings.
- Database/session health status.
- Subscribed asset count and market lifecycle state.

## Outputs

- Updated `src/agents/ingestion/ws_client.py`
- `src/observability/__init__.py`
- `src/observability/health.py`
- `src/observability/health_server.py`
- Updated `src/orchestrator.py`
- `tests/unit/test_WI-46-connectivity-hardening.py`
- `tests/integration/test_WI-46-connectivity-hardening.py`

## Acceptance Criteria

1. WebSocket reconnect path has bounded exponential backoff and jitter.
2. Reconnect behavior exposes current state, last successful connection, heartbeat timestamps, reconnect count, consecutive failure count, last error reason, and subscribed asset count.
3. Consecutive failure state is visible through a typed health snapshot.
4. Market closed, inactive, or expired cases are handled explicitly without infinite transport-error churn.
5. `/healthz` and `/readyz` return deterministic HTTP statuses and minimal JSON bodies.
6. Health server starts and stops cleanly through `Orchestrator`.
7. Health endpoints expose no secrets, wallet details, prompt text, reasoning text, private keys, or raw market payloads.
8. Tests simulate disconnect, reconnect, heartbeat loss, market closed state, readiness degradation, and graceful shutdown.
9. Targeted WI tests pass.
10. Full regression remains compatible with the documented baseline and coverage does not fall below 80%.

## Anti-Patterns

- Do not rename `CLOBWebSocketClient`.
- Do not make health endpoints mutate trading state.
- Do not expose secrets, wallet details, prompt text, reasoning text, or raw payloads.
- Do not create unbounded reconnect loops without max backoff.
- Do not treat closed markets as WebSocket transport failures.
- Do not block ingestion, context, evaluation, or execution queues from health checks.
- Do not bypass market discovery or token context requirements during reconnect.
- Do not weaken `dry_run` protections.
- Do not introduce raw `float` into financial or pricing paths.
- Do not leave background health tasks running after shutdown.

## Dependencies

- Existing WebSocket ingestion path.
- Existing `Orchestrator` runtime lifecycle.
- Existing `structlog` logging standard.
- Existing timeout and retry invariants from `AGENTS.md`.
- WI-47 may reuse health server infrastructure if route boundaries stay clear.

## Target Layer

Ingestion and runtime observability layer. This WI hardens Layer 1 connectivity and exposes read-only process health for long-running dry-run operation.
