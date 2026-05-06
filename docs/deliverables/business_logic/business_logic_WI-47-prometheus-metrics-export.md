# Business Logic - WI-47 Prometheus Metrics Export

## Objective

Expose a Prometheus-compatible `/metrics` endpoint with low-cardinality counters and gauges needed to operate dry-run sessions without blindness.

## Data Models

Pydantic schema names only:

- `MetricType`
- `MetricSample`
- `MetricLabelSet`
- `MetricsSnapshot`
- `MetricsEndpointResponse`
- `DecisionMetricEvent`
- `ExecutionMetricEvent`
- `LatencyMetricEvent`
- `BacktestReadinessMetric`

## Key Rules

1. `/metrics` must return Prometheus text exposition format.
2. Metrics export is read-only and must not mutate trading state.
3. Metrics collection must be non-blocking for ingestion, context, evaluation, and execution queues.
4. Labels must be low-cardinality.
5. Labels must not include raw `condition_id`, token ID, wallet address, prompt text, reasoning text, exception message, or private identifier.
6. Required metrics include decisions per hour, BUY/HOLD/SKIP decision counts, execution result counts by `ExecutionAction`, evaluation latency, context-build latency, execution-routing latency, WebSocket reconnect count, WebSocket error count, last heartbeat age seconds, active subscribed market count, and latest backtest live-readiness verdict.
7. Metrics endpoint and health endpoint may share a lightweight server only if route ownership remains clear.
8. Prefer standard-library `asyncio` networking unless a future implementation decision justifies a small dependency.
9. Metrics should be cumulative counters or current gauges with stable names.
10. No secrets or raw operational payloads may appear in output.
11. Metric formatting must be deterministic for tests.
12. Metrics must remain useful in dry-run and local development.

## Edge Cases

1. No decisions have occurred: counters render as zero or are absent consistently according to registry rules.
2. No heartbeat has occurred: heartbeat age renders as a safe sentinel or omitted gauge with documented behavior.
3. WebSocket never connected: reconnect and error metrics remain initialized safely.
4. Backtest verdict unavailable: readiness verdict metric reports unknown/not_available with stable label.
5. Unknown decision action: normalize to bounded label set or reject from metric update.
6. Unknown execution action: normalize to bounded label set or reject from metric update.
7. High-cardinality label input is attempted: reject or strip before export.
8. Concurrent metric updates during scrape: produce a consistent snapshot without blocking runtime loops.
9. Metrics server startup fails because port is occupied: log structured error and fail startup clearly.
10. Metrics scrape occurs during shutdown: return current snapshot or close gracefully.
11. Latency value is negative or malformed: reject with typed metric validation error.
12. Formatting error occurs: endpoint returns controlled error rather than exposing internals.

## Invariants

1. Metrics export cannot authorize or route trades.
2. Metrics export cannot bypass `LLMEvaluationResponse`.
3. Metrics export cannot weaken `dry_run` safety.
4. No raw `float` is used in money, price, EV, Kelly, PnL, or sizing paths.
5. Metric labels remain low-cardinality and secret-free.
6. Metrics collection is read-only.
7. Metrics output is deterministic and testable.
8. Runtime queues must not block on metrics updates.
9. Health and metrics surfaces are local observability surfaces only.
10. No database writes are introduced by metrics export.
