# PRD v9.0 - Poly-Oracle-Agent Phase 9

Source inputs: `docs/PRD-v8.0.md`, `STATE.md`, `docs/archive/ARCHIVE_PHASE_8.md`, `src/agents/execution/alert_engine.py`, `src/agents/execution/execution_router.py`, `src/agents/execution/pnl_calculator.py`, `src/orchestrator.py`, `src/core/config.py`, `src/schemas/risk.py`, `src/schemas/execution.py`, `src/db/models.py`, `AGENTS.md`.

## 1. Executive Summary

Phase 8 completed the portfolio analytics layer: open-position exposure is aggregated in real-time snapshots, closed-position performance is surfaced through lifecycle reports, and a rule-based alert engine emits typed `AlertEvent` records when risk thresholds are breached. But alerts exist only as structlog events — no human receives them in real time. And when the `AlertEngine` fires a CRITICAL drawdown alert, the bot continues routing new BUY orders without hesitation. The system observes degradation but cannot act on it.

Phase 9 closes that gap with three capabilities executed in strict dependency order:

1. **WI-26 — Telegram Telemetry Sink** introduces `TelegramNotifier`, an async notification channel that consumes `AlertEvent` objects (from WI-25) and critical execution events (BUY/SELL routed) and delivers them to the operator's mobile device via the Telegram Bot API. Notification is non-blocking and fail-open: if the Telegram API is unreachable, the bot logs the delivery failure and continues. It never blocks the Orchestrator or any upstream loop.

2. **WI-27 — Global Circuit Breaker** introduces `CircuitBreaker`, a stateful gate that sits BEFORE the `ExecutionRouter` on the Entry Path. When the `AlertEngine` fires a CRITICAL drawdown alert, the circuit breaker trips to an OPEN state, halting all new BUY order routing. The Exit Path remains fully operational — the bot can still evaluate exits and route SELL orders to protect the remaining bankroll. Restoring BUY capability requires explicit manual intervention (a reset command or `.env` flag toggle), ensuring no automated recovery bypasses human oversight.

3. **WI-28 — Net PnL & Fee Accounting (Architecture Prep)** extends the `PnLCalculator` (WI-21) to account for transaction gas costs (Polygon MATIC/POL) and CLOB taker/maker fees. A new Alembic migration adds `gas_cost_usdc` and `fees_usdc` columns to the `positions` table, enabling true Net PnL reporting through the existing `LifecycleReport` pipeline.

Phase 9 preserves the four-layer async architecture and the terminal authority of `LLMEvaluationResponse`. It introduces one write-capable component (`TelegramNotifier` writes to an external API) and one stateful gate (`CircuitBreaker` maintains a trip state), both of which are fail-safe by design. All Decimal math invariants, repository isolation, and `dry_run` execution blocking remain in force.

## 2. Core Pillars

### 2.1 Real-Time Operator Awareness

Structlog events are only useful when someone is watching the terminal. Phase 9 bridges the observability gap by routing critical alerts and execution events to the operator's mobile device via Telegram. The operator can monitor portfolio health, drawdown alerts, and execution confirmations from anywhere — without SSH access or log-tailing infrastructure.

### 2.2 Automated Capital Preservation

Observation without action is insufficient for risk management. Phase 9 introduces a hard circuit breaker that automatically halts new capital deployment when portfolio drawdown exceeds critical thresholds. The system protects the remaining bankroll by refusing new BUY orders while preserving the ability to exit existing positions — the optimal defensive posture during drawdown events.

### 2.3 True Net PnL Transparency

Realized PnL (WI-21) currently reflects gross entry/exit price differences. Phase 9 extends the PnL pipeline to account for real transaction costs — CLOB trading fees and Polygon gas — so the operator sees accurate net profitability. This is foundational for any future strategy optimization or Sharpe ratio computation.

## 3. Work Items

### WI-26: Telegram Telemetry Sink

**Objective**
Introduce `TelegramNotifier`, an async notification component that delivers typed alert events and critical execution confirmations to the operator via the Telegram Bot API. The notifier consumes `AlertEvent` objects emitted by `AlertEngine` (WI-25) and execution-event summaries (BUY routed, SELL routed) from the Orchestrator's existing loops, formats them as human-readable Telegram messages, and sends them using non-blocking `httpx` async POST calls.

The notifier is fail-open by design. A failed Telegram API call (timeout, HTTP error, invalid token) logs the delivery failure via structlog and returns without raising. It must NEVER block the Orchestrator, the execution consumer loop, the exit scan loop, or the portfolio aggregation loop. Telegram is a best-effort notification channel — the bot's core pipeline must remain fully operational regardless of Telegram API availability.

When `dry_run=True`, the notifier sends messages tagged with a `[DRY RUN]` prefix so the operator can distinguish simulation events from live execution events. Sending is NOT suppressed in dry-run mode — the operator should still receive telemetry during simulation runs.

**Scope Boundaries**

In scope:
- New `TelegramNotifier` class in `src/agents/execution/telegram_notifier.py`
- `send_alert(alert: AlertEvent) -> None` — async method that formats and sends an `AlertEvent` as a Telegram message
- `send_execution_event(summary: str, dry_run: bool) -> None` — async method that sends a free-form execution summary string
- Non-blocking `httpx.AsyncClient.post()` to `https://api.telegram.org/bot<token>/sendMessage`
- Configurable timeout: `telegram_send_timeout_sec: Decimal` (default `Decimal("5")`) — hard cap on per-message delivery time
- New `AppConfig` fields:
  - `telegram_bot_token: SecretStr` (default empty string — notifier is disabled when token is empty)
  - `telegram_chat_id: str` (default empty string — notifier is disabled when chat_id is empty)
  - `telegram_send_timeout_sec: Decimal` (default `Decimal("5")`)
  - `enable_telegram_notifier: bool` (default `False`)
- Config-gated construction: `TelegramNotifier` is constructed only when `enable_telegram_notifier=True` AND `telegram_bot_token` AND `telegram_chat_id` are non-empty
- `dry_run=True` sends messages prefixed with `[DRY RUN]` — sending is NOT suppressed
- Orchestrator integration:
  - Constructed in `Orchestrator.__init__()` (conditional)
  - Called in `_portfolio_aggregation_loop()` after `AlertEngine.evaluate()` fires alerts
  - Called in `_execution_consumer_loop()` after a BUY is routed (EXECUTED or DRY_RUN action)
  - Called in `_exit_scan_loop()` after a SELL is routed (SELL_ROUTED or DRY_RUN action)
- structlog audit events: `telegram.message_sent`, `telegram.send_failed`, `telegram.disabled`
- Graceful lifecycle: `TelegramNotifier` receives and closes its own `httpx.AsyncClient` in `Orchestrator.shutdown()`

Out of scope:
- Interactive Telegram bot commands (no command handler, webhook, or polling loop)
- Message queuing, batching, or rate-limiting (one-shot per event)
- Message deduplication or cooldown logic
- Rich media (photos, charts, inline keyboards) — text-only `sendMessage`
- Alert persistence to database — alerts remain structlog events
- Modifications to `AlertEngine`, `PortfolioAggregator`, `PositionLifecycleReporter`, or any upstream component internals

**Components Delivered**

| Component | Location |
|---|---|
| `TelegramNotifier` | `src/agents/execution/telegram_notifier.py` |
| Config: `enable_telegram_notifier` | `src/core/config.py` |
| Config: `telegram_bot_token` | `src/core/config.py` |
| Config: `telegram_chat_id` | `src/core/config.py` |
| Config: `telegram_send_timeout_sec` | `src/core/config.py` |

**Key Invariants Enforced**

1. `TelegramNotifier` is fail-open. A failed `send_alert()` or `send_execution_event()` call catches ALL exceptions, logs the failure, and returns `None`. It never raises into the caller.
2. `TelegramNotifier` never blocks the Orchestrator. All Telegram API calls use `httpx.AsyncClient.post()` with a hard timeout of `telegram_send_timeout_sec` seconds. No unbounded `await`.
3. `TelegramNotifier` performs zero DB writes. It is a one-way notification sink to an external API.
4. `TelegramNotifier` is config-gated. When `enable_telegram_notifier=False` (default) or credentials are empty, no notifier is constructed and zero HTTP calls are made.
5. `TelegramNotifier` has zero imports from prompt, context, evaluation, or ingestion modules.
6. `dry_run=True` does NOT suppress Telegram sends — it tags messages with `[DRY RUN]` for operator awareness.
7. The `httpx.AsyncClient` used by `TelegramNotifier` is managed within the Orchestrator lifecycle and closed during `shutdown()`.

**Acceptance Criteria**

1. `TelegramNotifier` exists in `src/agents/execution/telegram_notifier.py` with `send_alert(alert: AlertEvent) -> None` and `send_execution_event(summary: str, dry_run: bool) -> None` as the two public async methods.
2. `send_alert()` formats the `AlertEvent` fields (severity, rule_name, message, threshold, actual) into a human-readable Telegram message string.
3. `send_execution_event()` sends a free-form summary string as a Telegram message.
4. Both methods use `httpx.AsyncClient.post()` to `https://api.telegram.org/bot<token>/sendMessage` with `chat_id` and `text` payload.
5. Both methods enforce a hard timeout of `telegram_send_timeout_sec` seconds via `httpx` timeout configuration.
6. Both methods catch ALL exceptions (network, timeout, HTTP error, invalid credentials) and log via structlog without re-raising.
7. When `dry_run=True`, messages are prefixed with `[DRY RUN]`. Sending is NOT suppressed.
8. `AppConfig.enable_telegram_notifier` is `bool` with default `False`.
9. `AppConfig.telegram_bot_token` is `SecretStr` with default empty string.
10. `AppConfig.telegram_chat_id` is `str` with default empty string.
11. `AppConfig.telegram_send_timeout_sec` is `Decimal` with default `Decimal("5")`.
12. `TelegramNotifier` is constructed in `Orchestrator.__init__()` only when `enable_telegram_notifier=True` AND `telegram_bot_token` AND `telegram_chat_id` are non-empty.
13. `send_alert()` is invoked in `_portfolio_aggregation_loop()` for each fired `AlertEvent`.
14. `send_execution_event()` is invoked in `_execution_consumer_loop()` after a BUY is routed (EXECUTED or DRY_RUN).
15. `send_execution_event()` is invoked in `_exit_scan_loop()` after a SELL is routed (SELL_ROUTED or DRY_RUN).
16. The `httpx.AsyncClient` is closed during `Orchestrator.shutdown()`.
17. `TelegramNotifier` has zero imports from prompt, context, evaluation, or ingestion modules.
18. `TelegramNotifier` performs zero DB writes.
19. Full regression remains green with coverage >= 80%.

---

### WI-27: Global Circuit Breaker

**Objective**
Introduce `CircuitBreaker`, a stateful protection gate that sits BEFORE the `ExecutionRouter` on the Entry Path. The circuit breaker monitors `AlertEvent` outputs from the `AlertEngine` (WI-25) and trips to an OPEN state when a CRITICAL drawdown alert is fired. When tripped, the circuit breaker enforces a hard ban on new BUY order routing — the `ExecutionRouter.route()` call is gated and short-circuited with a typed `SKIP` result and reason `"circuit_breaker_open"`. The Exit Path (exit scan, SELL routing, PnL settlement) remains fully operational, allowing the bot to liquidate open positions and preserve the remaining bankroll.

The circuit breaker does NOT auto-recover. Restoring BUY capabilities requires explicit manual intervention: either toggling a `circuit_breaker_override_closed: bool` flag in `.env` (which forces the breaker to CLOSED on next evaluation cycle) or invoking a programmatic `reset()` method. This ensures that no automated logic can silently re-enable capital deployment after a critical drawdown event — human judgment is required to resume trading.

The circuit breaker state is in-memory only. A process restart resets the breaker to CLOSED (default safe state). This is intentional: if the operator restarts the bot, they are implicitly acknowledging the situation and authorizing continued operation.

**Scope Boundaries**

In scope:
- New `CircuitBreaker` class in `src/agents/execution/circuit_breaker.py`
- `CircuitBreakerState` enum (`CLOSED | OPEN`) — `CLOSED` means normal operation (BUY allowed), `OPEN` means tripped (BUY forbidden)
- `check_entry_allowed() -> bool` — synchronous method; returns `True` when CLOSED, `False` when OPEN
- `evaluate_alerts(alerts: list[AlertEvent]) -> None` — synchronous method; scans for CRITICAL drawdown alerts and trips the breaker if found
- `reset() -> None` — manual reset method; transitions OPEN -> CLOSED with structlog audit event
- `state` property exposing current `CircuitBreakerState`
- In-memory state: no DB persistence, no file persistence. Process restart resets to CLOSED.
- New `AppConfig` fields:
  - `enable_circuit_breaker: bool` (default `False`)
  - `circuit_breaker_override_closed: bool` (default `False`) — when `True`, forces the breaker CLOSED on next `evaluate_alerts()` call, then auto-resets the flag in memory
- Orchestrator integration:
  - Constructed in `Orchestrator.__init__()` (conditional on `enable_circuit_breaker`)
  - `evaluate_alerts()` called in `_portfolio_aggregation_loop()` after `AlertEngine.evaluate()` returns
  - `check_entry_allowed()` called in `_execution_consumer_loop()` BEFORE `ExecutionRouter.route()` — if breaker is OPEN, the item is skipped with a typed `ExecutionResult(action=SKIP, reason="circuit_breaker_open")`
  - Position tracking still records the SKIP result so the audit trail reflects the rejected entry
- structlog audit events:
  - `circuit_breaker.tripped` (severity CRITICAL) — fired when breaker transitions CLOSED -> OPEN
  - `circuit_breaker.entry_blocked` — fired each time a BUY is rejected by the open breaker
  - `circuit_breaker.reset` — fired when breaker transitions OPEN -> CLOSED (manual reset)
  - `circuit_breaker.override_applied` — fired when `circuit_breaker_override_closed` forces a reset
- Exit Path impact: NONE. `_exit_scan_loop()` is NOT gated by the circuit breaker. The bot can always evaluate exits, route SELL orders, and settle PnL regardless of breaker state.

Out of scope:
- Database persistence of breaker state — state is in-memory only
- Automatic recovery, cooldown timers, or half-open states (no classical circuit breaker pattern — this is a trip-and-hold latch)
- Per-market or per-position circuit breakers — this is a global, portfolio-level gate
- Modifications to `ExecutionRouter`, `ExitOrderRouter`, `ExitStrategyEngine`, `AlertEngine`, or `PnLCalculator` internals
- Telegram notification of breaker state changes (can be added by the operator via existing `send_execution_event()` from WI-26, but is not automatically wired)
- CLI reset command or API endpoint — reset is via `.env` flag or programmatic `reset()` call

**Components Delivered**

| Component | Location |
|---|---|
| `CircuitBreaker` | `src/agents/execution/circuit_breaker.py` |
| `CircuitBreakerState` enum | `src/agents/execution/circuit_breaker.py` |
| Config: `enable_circuit_breaker` | `src/core/config.py` |
| Config: `circuit_breaker_override_closed` | `src/core/config.py` |

**Key Invariants Enforced**

1. The circuit breaker gates ONLY the Entry Path (BUY routing). The Exit Path (exit scan, SELL routing, PnL settlement) is NEVER gated by the circuit breaker, regardless of breaker state.
2. When tripped (OPEN), the breaker produces a typed `ExecutionResult(action=SKIP, reason="circuit_breaker_open")` — never a silent drop or untyped rejection.
3. The breaker does NOT auto-recover. Transition from OPEN -> CLOSED requires explicit human intervention (`circuit_breaker_override_closed=True` in `.env` or programmatic `reset()` call).
4. Breaker state is in-memory only. A process restart resets the breaker to CLOSED (safe default). No DB persistence, no file persistence.
5. The breaker trips ONLY on `AlertSeverity.CRITICAL` alerts with `rule_name == "drawdown"`. Non-critical alerts and non-drawdown alerts do not affect breaker state.
6. `CircuitBreaker` is config-gated. When `enable_circuit_breaker=False` (default), no breaker is constructed and `_execution_consumer_loop()` routes directly to `ExecutionRouter` as before.
7. `CircuitBreaker` has zero imports from prompt, context, evaluation, or ingestion modules.
8. `CircuitBreaker` performs zero DB writes. State is held in a single `CircuitBreakerState` attribute.
9. `CircuitBreaker` is synchronous — no async methods, no I/O. It is a pure in-memory state gate.
10. `LLMEvaluationResponse` Gatekeeper authority is unaffected. The circuit breaker operates AFTER Gatekeeper validation and BEFORE execution routing — it is an additional gate, not a replacement.

**Acceptance Criteria**

1. `CircuitBreaker` exists in `src/agents/execution/circuit_breaker.py` with `check_entry_allowed() -> bool`, `evaluate_alerts(alerts: list[AlertEvent]) -> None`, `reset() -> None`, and `state` property.
2. `CircuitBreakerState` enum exists with values `CLOSED` and `OPEN`.
3. Default state is `CLOSED` (BUY allowed).
4. `evaluate_alerts()` transitions the breaker from CLOSED to OPEN when any `AlertEvent` has `severity=CRITICAL` and `rule_name="drawdown"`.
5. `check_entry_allowed()` returns `True` when CLOSED, `False` when OPEN.
6. When `check_entry_allowed()` returns `False`, `_execution_consumer_loop()` skips the item with `ExecutionResult(action=SKIP, reason="circuit_breaker_open")`.
7. Position tracking still records the SKIP result when the circuit breaker blocks an entry.
8. `_exit_scan_loop()` is NOT gated by the circuit breaker — exits proceed regardless of breaker state.
9. `reset()` transitions the breaker from OPEN to CLOSED with a `circuit_breaker.reset` structlog event.
10. `circuit_breaker_override_closed=True` in `AppConfig` forces a CLOSED transition on next `evaluate_alerts()` call and logs `circuit_breaker.override_applied`.
11. The breaker does NOT auto-recover. Absent manual intervention, an OPEN breaker remains OPEN indefinitely.
12. A process restart resets the breaker to CLOSED (in-memory state only).
13. `AppConfig.enable_circuit_breaker` is `bool` with default `False`.
14. `AppConfig.circuit_breaker_override_closed` is `bool` with default `False`.
15. `CircuitBreaker` is constructed in `Orchestrator.__init__()` only when `enable_circuit_breaker=True`.
16. `evaluate_alerts()` is called in `_portfolio_aggregation_loop()` after `AlertEngine.evaluate()`.
17. `check_entry_allowed()` is called in `_execution_consumer_loop()` before `ExecutionRouter.route()`.
18. `CircuitBreaker` has zero imports from prompt, context, evaluation, or ingestion modules.
19. `CircuitBreaker` performs zero DB writes.
20. `CircuitBreaker` is synchronous — no async methods.
21. Full regression remains green with coverage >= 80%.

---

### WI-28: Net PnL & Fee Accounting (Architecture Prep)

**Objective**
Extend the `PnLCalculator` (WI-21) to account for real transaction costs — CLOB taker/maker fees and Polygon gas costs — in realized PnL computation. Currently, `PnLCalculator.settle()` computes gross PnL as `(exit_price - entry_price) * position_size_tokens`. This overstates true profitability because it ignores the fees paid to the CLOB exchange and the gas costs paid to the Polygon network for on-chain settlement.

WI-28 adds two new nullable `Numeric(38,18)` columns — `gas_cost_usdc` and `fees_usdc` — to the `positions` table via an Alembic migration. The `PnLCalculator.settle()` method is extended to accept optional `gas_cost_usdc` and `fees_usdc` parameters and compute Net PnL as:

```
net_pnl = gross_pnl - gas_cost_usdc - fees_usdc
```

When fee/gas values are not provided (None), they default to `Decimal("0")` and the computation degrades gracefully to the existing gross PnL behavior — full backward compatibility is preserved.

The `PnLRecord` schema is extended with `gas_cost_usdc`, `fees_usdc`, and `net_realized_pnl` fields. The existing `realized_pnl` field continues to represent gross PnL for backward compatibility, while `net_realized_pnl` represents the fee-adjusted value. The `LifecycleReport` (WI-24) automatically benefits from persisted net PnL data via existing aggregation logic — no changes to `PositionLifecycleReporter` internals are required.

**Scope Boundaries**

In scope:
- New Alembic migration `0004_add_fee_columns.py` (parent `0003`):
  - `gas_cost_usdc Numeric(38,18) NULLABLE` column on `positions` table
  - `fees_usdc Numeric(38,18) NULLABLE` column on `positions` table
- Extended `Position` ORM model with nullable `gas_cost_usdc` and `fees_usdc` columns
- Extended `PositionRecord` Pydantic schema with optional `gas_cost_usdc: Decimal | None` and `fees_usdc: Decimal | None` fields
- Extended `PnLRecord` Pydantic schema with `gas_cost_usdc: Decimal`, `fees_usdc: Decimal`, and `net_realized_pnl: Decimal` fields
- Extended `PnLCalculator.settle()` signature: `settle(position, exit_price, gas_cost_usdc=None, fees_usdc=None) -> PnLRecord`
- Net PnL computation: `net_realized_pnl = realized_pnl - (gas_cost_usdc or 0) - (fees_usdc or 0)` using Decimal-only arithmetic
- Extended `PositionRepository.record_settlement()` to persist `gas_cost_usdc` and `fees_usdc` alongside existing settlement fields
- Float rejection at Pydantic boundary for all new financial fields
- Backward compatibility: when `gas_cost_usdc` and `fees_usdc` are both `None`, `net_realized_pnl` equals `realized_pnl` (gross PnL)

Out of scope:
- Live gas price fetching from Polygon RPC — gas costs are passed as parameters by the caller (future phase integration with `GasEstimator`)
- Live CLOB fee calculation — fee amounts are passed as parameters by the caller (future phase integration with Polymarket fee schedule)
- Automatic fee injection in `ExecutionRouter` or `ExitOrderRouter` — callers are not modified in this WI; fee parameters remain `None` until a future WI wires live fee sources
- Tax lot accounting, FIFO/LIFO, or multi-leg cost basis tracking
- Modifications to `PortfolioAggregator`, `AlertEngine`, `PositionLifecycleReporter`, or `ExecutionRouter` internals
- Historical backfill of gas/fee data for existing positions
- Fee-adjusted unrealized PnL in `PortfolioSnapshot` (deferred to future phase)

**Components Delivered**

| Component | Location |
|---|---|
| Alembic migration `0004_add_fee_columns.py` | `migrations/versions/0004_add_fee_columns.py` |
| Extended `Position` ORM model | `src/db/models.py` |
| Extended `PositionRecord` schema | `src/schemas/position.py` |
| Extended `PnLRecord` schema | `src/schemas/execution.py` |
| Extended `PnLCalculator.settle()` | `src/agents/execution/pnl_calculator.py` |
| Extended `PositionRepository.record_settlement()` | `src/db/repositories/position_repository.py` |

**Key Invariants Enforced**

1. All new financial fields (`gas_cost_usdc`, `fees_usdc`, `net_realized_pnl`) are `Decimal`-only. Float is rejected at Pydantic boundary.
2. The migration is additive: two new NULLABLE columns. Existing rows remain valid with `NULL` gas/fee values.
3. `PnLCalculator.settle()` signature is backward-compatible: `gas_cost_usdc` and `fees_usdc` default to `None`, preserving existing caller contracts.
4. When both fee parameters are `None`, `net_realized_pnl` equals `realized_pnl` (gross PnL). Existing behavior is perfectly preserved.
5. `record_settlement()` is extended additively — it persists `gas_cost_usdc` and `fees_usdc` alongside existing settlement fields. Existing settlement logic is unchanged.
6. The existing `realized_pnl` column semantics are preserved as gross PnL. No existing column is repurposed or renamed.
7. `PnLCalculator` has zero imports from prompt, context, evaluation, or ingestion modules.
8. The `dry_run` write gate for settlements remains unchanged — `dry_run=True` still blocks DB writes.

**Acceptance Criteria**

1. Alembic migration `0004_add_fee_columns.py` exists with parent `0003`, adding `gas_cost_usdc Numeric(38,18) NULLABLE` and `fees_usdc Numeric(38,18) NULLABLE` to the `positions` table.
2. `Position` ORM model has `gas_cost_usdc: Mapped[Optional[Decimal]]` and `fees_usdc: Mapped[Optional[Decimal]]` columns typed as `Numeric(38,18)`.
3. `PositionRecord` has optional `gas_cost_usdc: Decimal | None = None` and `fees_usdc: Decimal | None = None` fields with float rejection at Pydantic boundary.
4. `PnLRecord` has `gas_cost_usdc: Decimal`, `fees_usdc: Decimal`, and `net_realized_pnl: Decimal` fields with float rejection at Pydantic boundary.
5. `PnLCalculator.settle()` accepts optional `gas_cost_usdc: Decimal | None = None` and `fees_usdc: Decimal | None = None` parameters.
6. Net PnL is computed as `realized_pnl - (gas_cost_usdc or Decimal("0")) - (fees_usdc or Decimal("0"))` using Decimal-only arithmetic.
7. When both fee parameters are `None`, `net_realized_pnl` equals `realized_pnl`.
8. `PositionRepository.record_settlement()` persists `gas_cost_usdc` and `fees_usdc` to the `positions` table.
9. Existing callers of `PnLCalculator.settle()` continue to work without modification (backward-compatible signature).
10. Float is rejected at Pydantic boundary for all new financial fields in `PositionRecord` and `PnLRecord`.
11. All new columns are NULLABLE — existing rows and migrations are unaffected.
12. `PnLCalculator` has zero imports from prompt, context, evaluation, or ingestion modules.
13. Full regression remains green with coverage >= 80%.

## 4. Architecture Impact

### 4.1 Layer 4 Extension

Phase 9 extends Layer 4 (Execution) with a notification sink, a stateful entry gate, and enhanced settlement accounting:

```text
Layer 1: Ingestion
  CLOBWebSocketClient + GammaRESTClient + MarketDiscoveryEngine

Layer 2: Context
  DataAggregator + PromptFactory

Layer 3: Evaluation
  ClaudeClient + LLMEvaluationResponse Gatekeeper

Layer 4: Execution
  ┌─ Entry Path ─────────────────────────────────────────────────────────┐
  │ CircuitBreaker (NEW) -> ExecutionRouter -> PositionTracker           │
  │ -> TransactionSigner -> NonceManager -> GasEstimator                 │
  │ -> OrderBroadcaster                                                  │
  └──────────────────────────────────────────────────────────────────────┘
  ┌─ Exit Path (NOT gated by CircuitBreaker) ────────────────────────────┐
  │ ExitStrategyEngine -> ExitOrderRouter -> PnLCalculator (extended)    │
  │ -> TransactionSigner -> OrderBroadcaster                             │
  └──────────────────────────────────────────────────────────────────────┘
  ┌─ Analytics Path ─────────────────────────────────────────────────────┐
  │ PortfolioAggregator -> PositionLifecycleReporter -> AlertEngine      │
  │ -> CircuitBreaker.evaluate_alerts() (NEW)                            │
  │ -> TelegramNotifier (NEW)                                            │
  └──────────────────────────────────────────────────────────────────────┘
```

Queue topology unchanged: `market_queue -> prompt_queue -> execution_queue`. The circuit breaker is an inline gate in the execution consumer loop, not a new queue or task. The Telegram notifier is a fire-and-forget sink invoked from existing loops.

### 4.2 Orchestrator Task Topology (After Phase 9)

```text
Orchestrator.start():
  Task 1: IngestionTask           — CLOBWebSocketClient.run()
  Task 2: ContextTask             — DataAggregator.start()
  Task 3: EvaluationTask          — ClaudeClient.start()
  Task 4: ExecutionTask           — _execution_consumer_loop()            [WI-27 gate added]
  Task 5: DiscoveryTask           — _discovery_loop()
  Task 6: ExitScanTask            — _exit_scan_loop()                     [WI-26 notify added]
  Task 7: PortfolioAggregatorTask — _portfolio_aggregation_loop()         [WI-26/27 hooks added, conditional]
```

No new async tasks are introduced. Phase 9 adds inline hooks to existing tasks: a circuit breaker gate in Task 4, Telegram notification calls in Tasks 4/6/7, and circuit breaker evaluation in Task 7.

### 4.3 Execution Consumer Flow (After Phase 9)

```text
_execution_consumer_loop:
  1. Dequeue item from execution_queue
  2. Extract evaluation response
  3. CircuitBreaker.check_entry_allowed()                              [WI-27, NEW]
     -> If OPEN: SKIP with reason="circuit_breaker_open", record position, continue
  4. ExecutionRouter.route(response, market_context)
  5. PositionTracker.record_execution(result)
  6. TelegramNotifier.send_execution_event(summary)                    [WI-26, NEW]
  7. dry_run gate / broadcast
```

### 4.4 Portfolio Aggregation Loop Flow (After Phase 9)

```text
_portfolio_aggregation_loop:
  1. Sleep for config.portfolio_aggregation_interval_sec               [WI-23]
  2. PortfolioAggregator.compute_snapshot()                            [WI-23]
     -> PortfolioSnapshot
  3. PositionLifecycleReporter.generate_report()                       [WI-24]
     -> LifecycleReport
  4. AlertEngine.evaluate(snapshot, report)                            [WI-25]
     -> list[AlertEvent]
  5. CircuitBreaker.evaluate_alerts(alerts)                            [WI-27, NEW]
  6. TelegramNotifier.send_alert(alert) for each alert                 [WI-26, NEW]
  7. Log summary and repeat
```

Steps 5-6 are fail-open: if `evaluate_alerts()` or `send_alert()` raises, the loop catches, logs, and continues.

### 4.5 Database Schema Extension

Phase 9 adds one migration (`0004_add_fee_columns.py`) with two new NULLABLE columns on the `positions` table:

| Column | Type | Nullable | Purpose |
|---|---|---|---|
| `gas_cost_usdc` | `Numeric(38,18)` | YES | Polygon gas cost in USDC at settlement time |
| `fees_usdc` | `Numeric(38,18)` | YES | CLOB taker/maker fees in USDC at settlement time |

Existing rows are unaffected — both columns default to `NULL`, which the `PnLCalculator` treats as `Decimal("0")` for net PnL computation.

### 4.6 Preserved Boundaries

Phase 9 does not alter:
- **Gatekeeper authority** — `LLMEvaluationResponse` remains the sole terminal validation boundary before execution-aware routing. The circuit breaker is an ADDITIONAL gate after Gatekeeper validation, not a replacement.
- **Decimal financial integrity** — all fee accounting, PnL computation, and threshold comparisons remain Decimal-native with float rejection at Pydantic boundary.
- **Quarter-Kelly and exposure policy** — `kelly_fraction=0.25` and `min(kelly_size, 0.03 * bankroll)` are unchanged. The circuit breaker prevents entry entirely when tripped — it does not alter sizing.
- **Repository pattern** — `PositionRepository` remains the sole access path for the `positions` table. `record_settlement()` is extended additively.
- **Async pipeline** — Phase 9 adds no new async tasks. All new logic is inline within existing loops.
- **Exit-path independence** — `ExitOrderRouter`, `ExitStrategyEngine`, and `PnLCalculator` internals are unmodified (WI-28 extends `settle()` signature with backward-compatible optional parameters). The Exit Path is explicitly NOT gated by the circuit breaker.

## 5. Risk and Safety Notes

### 5.1 dry_run Behavior

`dry_run=True` remains a hard stop for all Layer 4 side effects. Phase 9 components interact with `dry_run` as follows:

| Component | dry_run=True behavior |
|---|---|
| `TelegramNotifier` | Sends messages prefixed with `[DRY RUN]`. Sending is NOT suppressed — the operator receives telemetry during simulation. The notifier writes to an external API (Telegram), not to the database. |
| `CircuitBreaker` | Evaluates alerts and trips normally. BUY gating applies equally in dry-run mode (a dry-run BUY is still blocked by an open breaker). This ensures dry-run simulations accurately reflect what would happen in live mode. |
| `PnLCalculator` (extended) | `dry_run=True` continues to block `record_settlement()` DB writes. Net PnL computation is performed but not persisted. |

### 5.2 Failure Semantics

| Component | Failure behavior | Rationale |
|---|---|---|
| `TelegramNotifier.send_alert()` | Catches all exceptions, logs `telegram.send_failed`, returns `None`. | Notification failure must never block the pipeline. Best-effort delivery. |
| `TelegramNotifier.send_execution_event()` | Catches all exceptions, logs `telegram.send_failed`, returns `None`. | Same as above. |
| `CircuitBreaker.evaluate_alerts()` | Catches exceptions in `_portfolio_aggregation_loop()` wrapper. | A failed evaluation leaves the breaker in its current state (safe: if CLOSED, stays CLOSED; if OPEN, stays OPEN). |
| `CircuitBreaker.check_entry_allowed()` | Synchronous, no I/O — cannot fail in normal operation. If called on a `None` breaker reference, the Orchestrator skips the check (breaker disabled). | The gate is a simple boolean check on in-memory state. |
| `PnLCalculator.settle()` (extended) | Existing failure semantics unchanged — settlement exceptions are caught in `_exit_scan_loop()`. | Fee parameters are optional; `None` degrades to zero. |

### 5.3 Circuit Breaker Safety Model

The circuit breaker implements a **trip-and-hold latch**, not the classical half-open recovery pattern:

| Property | Behavior |
|---|---|
| Trip trigger | `AlertSeverity.CRITICAL` + `rule_name == "drawdown"` only |
| Auto-recovery | **NONE** — stays OPEN until manual intervention |
| Exit path impact | **NONE** — SELL routing, exit scans, and PnL settlement always proceed |
| Process restart | Resets to CLOSED (in-memory state) — implicit operator acknowledgment |
| Override mechanism | `.env` flag `CIRCUIT_BREAKER_OVERRIDE_CLOSED=true` or `reset()` call |

This design ensures the bot protects capital during drawdown events while preserving the ability to exit positions — the optimal defensive posture. The operator retains full control over when to resume BUY activity.

## 6. Metrics

| Metric | Target |
|---|---|
| Coverage | >= 80% (maintain existing 94%) |
| Regression gate | `pytest --asyncio-mode=auto tests/ -q` green |
| WI-26 tests | Unit + integration for message formatting, send_alert, send_execution_event, timeout handling, fail-open semantics, dry-run tagging, disabled-notifier guard |
| WI-27 tests | Unit + integration for trip-on-critical, check_entry_allowed gating, exit-path independence, manual reset, override flag, non-drawdown alerts ignored |
| WI-28 tests | Unit + integration for net PnL computation, fee parameter handling, None-fee backward compat, migration schema, float rejection, settlement persistence |

## 7. Strict Constraints

The following constraints are mandatory and non-negotiable for all Phase 9 work:

1. **Gatekeeper remains immutable:**
   `LLMEvaluationResponse` remains the sole terminal validation boundary before execution-aware routing. The circuit breaker is an ADDITIONAL gate positioned after Gatekeeper validation — it does not bypass, replace, or weaken Gatekeeper authority. Telegram notification operates downstream of all decision-making.

2. **Decimal financial integrity remains immutable:**
   All fee accounting (`gas_cost_usdc`, `fees_usdc`), PnL computation (`net_realized_pnl`), and alert threshold comparisons remain Decimal-native. Float is rejected at Pydantic boundary. No float intermediary in any arithmetic step.

3. **Quarter-Kelly and exposure policy remain immutable:**
   Phase 9 does not alter `kelly_fraction=0.25` or the system-wide `min(kelly_size, 0.03 * bankroll)` exposure policy. The circuit breaker prevents entry entirely — it does not influence sizing or probability estimates.

4. **`dry_run=True` remains a hard execution stop:**
   Dry run blocks all order signing, CLOB broadcast, and state-mutating DB writes. `TelegramNotifier` sends tagged `[DRY RUN]` messages (external API write is permitted — it is informational, not financial). `CircuitBreaker` evaluates and trips normally in dry-run mode.

5. **Repository pattern remains the sole DB access path:**
   `PositionRepository` is the only component that touches the `positions` table. `record_settlement()` is extended additively with fee parameters. No raw SQL, no direct session manipulation.

6. **Async pipeline behavior remains immutable:**
   Phase 9 preserves the existing non-blocking, queue-driven four-layer architecture. No new async tasks are introduced. All new logic is inline within existing loops. The `CircuitBreaker` is synchronous. The `TelegramNotifier` uses non-blocking `httpx` with hard timeouts.

7. **Module isolation remains enforced:**
   `TelegramNotifier`, `CircuitBreaker`, and extended `PnLCalculator` have zero imports from prompt, context, evaluation, or ingestion modules. They receive and produce only typed contracts from `src/schemas/`.

8. **Exit Path independence is sacrosanct:**
   The `CircuitBreaker` gates ONLY the Entry Path (BUY routing). The Exit Path (exit scan, SELL routing, PnL settlement) is NEVER gated by the circuit breaker, regardless of breaker state. This is a non-negotiable safety invariant: the bot must always be able to exit positions to preserve capital.

9. **Telegram is fail-open and non-blocking:**
   `TelegramNotifier` must NEVER block the Orchestrator, execution consumer, exit scan, or portfolio aggregation loops. All Telegram API calls must use hard timeouts. All delivery failures must be caught and logged without re-raising.

## 8. Success Criteria For Phase 9

Phase 9 is complete when all of the following are true:

1. `AlertEvent` objects fired by the `AlertEngine` are delivered to the operator's Telegram channel in real time, with `[DRY RUN]` tagging when applicable.
2. Critical execution events (BUY routed, SELL routed) are delivered to Telegram as free-form summaries.
3. Telegram delivery failures are logged and never block any Orchestrator loop.
4. A CRITICAL drawdown alert from the `AlertEngine` trips the circuit breaker, immediately halting all new BUY order routing.
5. The Exit Path (exit scan, SELL routing, PnL settlement) continues to operate normally when the circuit breaker is tripped.
6. The circuit breaker does not auto-recover — manual intervention is required to restore BUY capability.
7. `PnLCalculator.settle()` accepts optional `gas_cost_usdc` and `fees_usdc` parameters and computes `net_realized_pnl` using Decimal-only arithmetic.
8. The `positions` table has `gas_cost_usdc` and `fees_usdc` NULLABLE columns via Alembic migration `0004`.
9. When fee parameters are `None`, net PnL equals gross PnL — existing behavior is perfectly preserved.
10. Full regression remains green and project coverage stays at or above 80%.
11. All prior architectural invariants remain in force: Decimal safety, repository isolation, Gatekeeper authority, no hardcoded market identifiers, `dry_run` execution blocking, async-only pipeline, and Exit Path independence.

## 9. Next Phase

Phase 10 should address strategy intelligence and advanced risk metrics. Potential scope includes:

- **Advanced risk metrics** — Sharpe ratio, maximum drawdown tracking, and rolling-window analytics for quantitative strategy evaluation beyond simple win/loss counts.
- **Live fee injection** — wiring `GasEstimator` and Polymarket fee schedules into `ExecutionRouter` and `ExitOrderRouter` so `gas_cost_usdc` and `fees_usdc` are automatically populated at routing time rather than passed manually.
- **Portfolio-level exposure limits** — enforcing hard cross-position exposure caps (not just per-order 3%) with automatic rejection of new orders that would breach portfolio limits.
- **Strategy backtesting framework** — replaying historical market data through the evaluation pipeline to validate prompt and threshold tuning before live deployment.
- **Multi-market concurrent tracking** — extending the Orchestrator to manage multiple active `condition_id` markets simultaneously instead of the current single-market rotation pattern.

Detailed scope, work items, and acceptance criteria to be finalized in the Phase 10 PRD.
