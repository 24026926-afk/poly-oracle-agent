# Implementation Prompt - WI-47 Prometheus Metrics Export

## Session Context

You are working in `poly-oracle-agent` on Phase 13: Real-Data Validation & 24/7 Readiness.

Current baseline:

- Phase 12 added a Streamlit dashboard, but the runtime has no Prometheus scrape endpoint.
- WI-46 may add shared health server infrastructure.
- Metrics must be low-cardinality, read-only, and safe for dry-run 24/7 operation.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v13.0.md`
- `docs/deliverables/business_logic/business_logic_WI-47-prometheus-metrics-export.md`
- `src/orchestrator.py`
- `src/agents/ingestion/ws_client.py`
- `src/agents/evaluation/claude_client.py`
- `src/agents/context/aggregator.py`
- `src/agents/execution/execution_router.py`
- WI-46 health server implementation if present

## Objective

Expose a Prometheus-compatible `/metrics` endpoint with low-cardinality counters and gauges for dry-run operational visibility.

## Inputs

- Decision events from evaluation.
- Execution routing results.
- Layer latency measurements.
- WebSocket health and reconnect state.
- Heartbeat timestamps.
- Active subscribed market count.
- Latest WI-44 backtest live-readiness verdict when available.

## Outputs

- `src/observability/metrics.py`
- `src/observability/metrics_server.py`
- Updated runtime wiring where needed.
- `tests/unit/test_WI-47-prometheus-metrics.py`
- `tests/integration/test_WI-47-prometheus-metrics.py`

## Acceptance Criteria

1. `GET /metrics` returns valid Prometheus text exposition format.
2. Required counters and gauges are emitted with low-cardinality labels.
3. Metrics include decisions per hour, BUY/HOLD/SKIP counts, execution result counts by `ExecutionAction`, evaluation latency, context-build latency, execution-routing latency, WebSocket reconnect count, WebSocket error count, last heartbeat age seconds, active subscribed market count, and latest backtest live-readiness verdict.
4. No secrets, wallet details, prompt text, reasoning text, private keys, raw `condition_id`, token ID, or high-cardinality market IDs appear in metrics.
5. Metrics updates are non-blocking and safe under concurrent queue activity.
6. Health and metrics endpoints may share a server only if route ownership stays clear.
7. Tests validate exposition format, stable labels, counter increments, heartbeat age, concurrency snapshot behavior, and absence of forbidden sensitive fields.
8. Targeted WI tests pass.
9. Full regression remains compatible with the documented baseline and coverage does not fall below 80%.

## Anti-Patterns

- Do not add high-cardinality labels.
- Do not label metrics by raw market ID, condition ID, token ID, wallet address, prompt text, reasoning text, or exception message.
- Do not expose secrets or raw payloads.
- Do not mutate trading state from metrics collection.
- Do not block runtime queues while recording metrics.
- Do not authorize or route trades from metrics state.
- Do not bypass `LLMEvaluationResponse`.
- Do not weaken `dry_run` protections.
- Do not introduce a heavyweight web framework unless explicitly justified.
- Do not write metrics to the runtime database.

## Dependencies

- WI-46 health or observability infrastructure, if already implemented.
- Existing `ExecutionAction` schema.
- Existing evaluation and execution loops.
- Existing WebSocket health state.
- Existing `structlog` logging standard.

## Target Layer

Runtime observability layer spanning ingestion, context, evaluation, execution, and backtest validation verdict reporting.
