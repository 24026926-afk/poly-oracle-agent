# Implementation Prompt - WI-50 Telegram Operational Alert Bridge

## Session Context

You are working in `poly-oracle-agent` on Phase 14: DigitalOcean 24/7 Paper-Trading Deployment.

Current baseline:

- WI-26 added `TelegramNotifier` for alert and execution notifications.
- WI-46 added health/readiness state and WebSocket health snapshots.
- WI-47 added metrics export.
- WI-27 added the circuit breaker state machine.
- Phase 14 needs operational alerts for deployed dry-run runtime attention, not trading authorization.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v14.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-50-telegram-operational-alert-bridge.md`
- `src/orchestrator.py`
- `src/agents/execution/telegram_notifier.py`
- `src/observability/health.py`
- `src/agents/ingestion/ws_client.py`
- Circuit breaker implementation and schemas
- `src/core/config.py`

## Objective

Add a typed operational alert bridge that sends deduplicated Telegram notifications for process start or restart, sustained readiness degradation, stale WebSocket health, and circuit breaker open/closed transitions.

## Inputs

- Existing `TelegramNotifier` transport.
- Existing Telegram config fields.
- Existing health and readiness state.
- Existing WebSocket health snapshot.
- Existing circuit breaker typed state.
- Orchestrator lifecycle events.

## Outputs

- `src/schemas/ops.py`
- `src/observability/operational_alerts.py`
- Updated `src/orchestrator.py` wiring where needed.
- Updated `src/core/config.py` only for bounded operational alert gates, thresholds, or cooldown values.
- `docs/runbooks/telegram-operational-alerts.md`
- `tests/unit/test_WI-50-telegram-operational-alert-bridge.py`
- `tests/integration/test_WI-50-telegram-operational-alert-bridge.py`

## Acceptance Criteria

1. Typed schemas reject unknown operational alert types.
2. Typed schemas reject or redact secret-like fields before dispatch.
3. Startup or restart alert can be enabled for deployment but does not fire in tests unless explicitly configured.
4. Sustained `/readyz` unhealthy or degraded state triggers one alert after the configured threshold.
5. Sustained WebSocket disconnected or stale PONG state triggers one alert after the configured threshold.
6. Repeated degraded checks inside cooldown do not send duplicate Telegram messages.
7. Circuit breaker open and closed typed transitions trigger bounded alerts.
8. Telegram disabled or missing credentials does not crash runtime and logs a structured disabled reason.
9. Alert evaluation is read-only and does not mutate trading state.
10. Alert dispatch does not block ingestion, context, evaluation, execution, health, or metrics loops.
11. All HTTP send attempts use explicit timeout and bounded retry behavior consistent with the existing notifier.
12. Tests cover restart, sustained degraded readiness, stale WebSocket, circuit breaker transitions, dedupe cooldown, disabled Telegram, send failure, and secret-free payloads.
13. Targeted WI tests pass.
14. Full regression remains compatible with the documented baseline and coverage stays >= 80%.

## Anti-Patterns

- Do not parse logs to infer circuit breaker state.
- Do not put raw exceptions, prompts, reasoning text, token IDs, condition IDs, wallet addresses, API keys, or Telegram tokens in alert payloads.
- Do not block the main trading queues on Telegram delivery.
- Do not fail the runtime because Telegram is disabled or unavailable.
- Do not authorize or route trades from alert state.
- Do not bypass `LLMEvaluationResponse`.
- Do not weaken `DRY_RUN=true`.
- Do not introduce unbounded retry loops.
- Do not add high-cardinality alert labels.

## Dependencies

- Existing `TelegramNotifier`.
- Existing `AppConfig` Telegram fields.
- Existing `HealthServer` and `RuntimeHealthSnapshot` models.
- Existing `CLOBWebSocketClient` health snapshot behavior.
- Existing circuit breaker typed state.
- Existing `structlog` logging standard.

## Target Layer

Runtime observability and operator alerting layer spanning orchestrator lifecycle, readiness, WebSocket health, and circuit breaker state. This WI must not alter strategy, LLM evaluation, order sizing, signing, or broadcasting.
