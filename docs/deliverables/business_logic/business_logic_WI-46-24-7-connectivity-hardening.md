# Business Logic - WI-46 24-7 Connectivity Hardening

## Objective

Harden WebSocket reconnect and runtime liveness behavior so long dry-run sessions recover from disconnects and expose health state to the operator.

## Data Models

Pydantic schema names only:

- `WebSocketConnectionState`
- `WebSocketHealthSnapshot`
- `RuntimeHealthSnapshot`
- `ReadinessStatus`
- `HealthEndpointResponse`
- `WebSocketReconnectConfig`
- `MarketLifecycleState`
- `MarketClosedSkipReason`

## Key Rules

1. `CLOBWebSocketClient` remains the canonical WebSocket client.
2. Reconnect behavior must be bounded, observable, and configurable.
3. Backoff policy must include initial backoff, max backoff, jitter, and consecutive-failure tracking.
4. The client must track current connection state, last successful connection timestamp, last heartbeat sent timestamp, last PONG received timestamp, reconnect count, consecutive failure count, last error reason, and active subscribed asset count.
5. Market closed, inactive, expired, or unavailable states must be handled explicitly and must not cause noisy reconnect loops.
6. Health endpoints must be read-only and expose minimal operational state only.
7. `GET /healthz` reports process liveness.
8. `GET /readyz` reports readiness or degraded state based on database, WebSocket, and subscription health.
9. The health server must start and stop through explicit `Orchestrator` lifecycle hooks.
10. No endpoint may expose secrets, wallet address, private keys, prompt text, reasoning text, or raw market payloads.
11. Health checks must not mutate trading state.
12. Health checks must not block evaluation, context, ingestion, or execution queues.

## Edge Cases

1. WebSocket disconnects cleanly: mark disconnected, increment reconnect count, and retry with backoff.
2. WebSocket disconnects with exception: record last error reason without leaking raw sensitive payloads.
3. Heartbeat send fails: mark degraded and allow reconnect path to recover.
4. PONG is stale beyond threshold: mark readiness degraded.
5. No assets are subscribed: readiness should be degraded or not-ready based on config.
6. Consecutive failures exceed threshold: health state becomes degraded while loop continues bounded recovery.
7. Market is closed or expired: emit typed market lifecycle state and avoid treating it as transport failure.
8. Database unavailable during readiness check: `/readyz` returns not-ready without crashing process.
9. Health server port already in use: startup fails clearly and logs structured error.
10. Shutdown while reconnect sleep is pending: task exits cleanly.
11. Shutdown while health request is in flight: complete or cancel cleanly without leaving background tasks.
12. Health snapshot requested before WebSocket first connects: return initialized state, not an exception.

## Invariants

1. Live trading safety gates remain unchanged.
2. `dry_run` protections are not weakened.
3. WebSocket reconnect does not bypass market discovery or token context requirements.
4. No raw `float` is introduced in financial or pricing paths.
5. Health surfaces are read-only.
6. Health output is low-detail and safe for local operator inspection.
7. All network operations use explicit timeout or bounded behavior.
8. Runtime task shutdown remains graceful.
9. Structured logs are used for state transitions and failures.
10. No database schema changes are required for health snapshots.
