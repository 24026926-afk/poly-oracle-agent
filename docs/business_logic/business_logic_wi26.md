# WI-26 Business Logic — Telegram Telemetry Sink

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — `TelegramNotifier` is a fully async component. All Telegram API calls use `httpx.AsyncClient.post()` with a hard timeout. No new `asyncio.create_task()` or queue introduced. The notifier is invoked inline within existing Orchestrator loops (`_portfolio_aggregation_loop`, `_execution_consumer_loop`, `_exit_scan_loop`) and must never block or delay those loops.
- `.agents/rules/risk-auditor.md` — `TelegramNotifier` performs zero financial calculations. No `Decimal` math beyond string-formatting values already computed by upstream components. It is a read-only notification sink.
- `.agents/rules/db-engineer.md` — Zero DB reads, zero DB writes. `TelegramNotifier` is a one-way HTTP sink to the Telegram Bot API. No `AsyncSession`, no repository calls, no ORM imports.
- `.agents/rules/security-auditor.md` — The `telegram_bot_token` field is typed as `SecretStr` and must never be logged, serialized to plain text, or included in structlog event payloads. The token is only exposed when constructing the Telegram API URL. `dry_run` flag is propagated into message prefixes for operator awareness.
- `.agents/rules/test-engineer.md` — WI-26 requires unit tests for message formatting, dry-run prefix injection, error swallowing (timeout, HTTP 4xx/5xx, network failure), config-gated disablement, and orchestrator wiring. Integration tests must verify the notifier does not raise into calling loops. Full suite remains >= 80% coverage.

## 1. Objective

Introduce `TelegramNotifier`, a non-blocking, fail-open, async notification sink that delivers typed alert events (from `AlertEngine`, WI-25) and critical execution confirmations (BUY/SELL routed) to the operator's mobile device via the Telegram Bot API.

`TelegramNotifier` owns:
- Formatting `AlertEvent` objects into human-readable Telegram messages
- Formatting execution event summaries (BUY routed, SELL routed) into human-readable Telegram messages
- Delivering formatted text via `httpx.AsyncClient.post()` to `https://api.telegram.org/bot<token>/sendMessage`
- Injecting `[DRY RUN]` prefix when `dry_run=True` — sending is NOT suppressed
- Swallowing ALL delivery exceptions (timeout, HTTP error, network failure, invalid credentials) and logging them via structlog without re-raising
- Managing its own `httpx.AsyncClient` lifecycle (closed during `Orchestrator.shutdown()`)

`TelegramNotifier` does NOT own:
- Alert evaluation or threshold computation (upstream: `AlertEngine`, WI-25)
- Portfolio snapshot computation (upstream: `PortfolioAggregator`, WI-23)
- Lifecycle report generation (upstream: `PositionLifecycleReporter`, WI-24)
- Position lifecycle management, exit routing, or PnL settlement
- Order execution, signing, or broadcasting
- Interactive Telegram bot commands, webhooks, or polling loops
- Message queuing, batching, rate-limiting, deduplication, or cooldown logic
- Alert persistence to database
- Rich media (photos, charts, inline keyboards) — text-only `sendMessage`

## 2. Scope Boundaries

### In Scope

1. New `TelegramNotifier` class in `src/agents/execution/telegram_notifier.py`.
2. Two public async methods:
   - `async def send_alert(self, alert: AlertEvent) -> None`
   - `async def send_execution_event(self, summary: str, dry_run: bool) -> None`
3. Four new `AppConfig` fields in `src/core/config.py`:
   - `enable_telegram_notifier: bool` (default `False`)
   - `telegram_bot_token: SecretStr` (default empty `SecretStr("")`)
   - `telegram_chat_id: str` (default `""`)
   - `telegram_send_timeout_sec: Decimal` (default `Decimal("5")`)
4. Config-gated construction in `Orchestrator.__init__()`.
5. Orchestrator wiring into three existing loops.
6. `httpx.AsyncClient` lifecycle management in `Orchestrator.shutdown()`.
7. structlog audit events for message delivery outcomes.

### Out of Scope

1. Interactive Telegram bot commands (no command handler, webhook, or polling loop).
2. Message queuing, batching, or rate-limiting — one-shot per event.
3. Message deduplication or cooldown logic.
4. Rich media (photos, charts, inline keyboards) — text-only `sendMessage`.
5. Alert persistence to database — alerts remain structlog events.
6. New database tables, migrations, or DB writes of any kind.
7. Modifications to `AlertEngine`, `PortfolioAggregator`, `PositionLifecycleReporter`, `ExecutionRouter`, `ExitOrderRouter`, or any upstream component internals.
8. Retry logic — a single failed send is logged and discarded. No exponential backoff or retry queue.

## 3. Target Component Architecture + Data Contracts

### 3.1 TelegramNotifier Component (New Class)

- **Module:** `src/agents/execution/telegram_notifier.py`
- **Class Name:** `TelegramNotifier` (exact)
- **Responsibility:** Accept typed alert events or free-form execution summaries, format them as human-readable text, and POST them to the Telegram Bot API. Swallow all delivery failures without raising.

Isolation rules:
- `TelegramNotifier` must not import LLM prompt construction, context-building, evaluation, or ingestion modules.
- `TelegramNotifier` must not import any repository, ORM model, or `AsyncSession`.
- `TelegramNotifier` must not import `TransactionSigner`, `OrderBroadcaster`, `BankrollSyncProvider`, `PolymarketClient`, `ExecutionRouter`, `ExitOrderRouter`, or `PnLCalculator`.
- `TelegramNotifier` must not write to the database.
- `TelegramNotifier` must not influence routing, exit decisions, or any upstream component.
- `TelegramNotifier` may import from `src/schemas/risk.py` (for `AlertEvent`, `AlertSeverity`) and `src/core/config.py` (for `AppConfig`).

### 3.2 Constructor Signature

```python
class TelegramNotifier:
    def __init__(
        self,
        config: AppConfig,
        http_client: httpx.AsyncClient,
    ) -> None:
```

**Design rationale:** The `httpx.AsyncClient` is injected rather than internally constructed. This enables:
- The Orchestrator to manage the client's lifecycle (close on shutdown)
- Test code to inject a mock client without monkeypatching
- Consistent with the existing pattern of injecting shared HTTP clients in the Orchestrator (see `self._httpx_client`)

The constructor must extract the following from `config`:
- `self._bot_token: str` — extracted via `config.telegram_bot_token.get_secret_value()`
- `self._chat_id: str` — from `config.telegram_chat_id`
- `self._timeout: float` — `float(config.telegram_send_timeout_sec)` for httpx timeout configuration
- `self._client: httpx.AsyncClient` — the injected async HTTP client

The constructor must also bind a `structlog` logger:
```python
self._log = structlog.get_logger(__name__)
```

### 3.3 Public Method — `send_alert`

```python
async def send_alert(self, alert: AlertEvent) -> None:
```

**Behavior:**
1. Format the `AlertEvent` into a human-readable message string (see Section 3.5 for format spec).
2. If `alert.dry_run is True`, prepend `[DRY RUN] ` to the message text.
3. Call `self._send(text)` (internal helper).
4. On success, log `telegram.message_sent` with `rule_name` and `severity`.
5. On failure, the exception is caught inside `_send()` — the method returns cleanly.

**Invariant:** This method MUST NOT raise any exception to the caller. All exceptions are caught and logged internally.

### 3.4 Public Method — `send_execution_event`

```python
async def send_execution_event(self, summary: str, dry_run: bool) -> None:
```

**Behavior:**
1. If `dry_run is True`, prepend `[DRY RUN] ` to the summary string.
2. Call `self._send(text)`.
3. On success, log `telegram.message_sent` with the event type.
4. On failure, the exception is caught inside `_send()` — the method returns cleanly.

**Invariant:** This method MUST NOT raise any exception to the caller. All exceptions are caught and logged internally.

### 3.5 Internal Method — `_send`

```python
async def _send(self, text: str) -> None:
```

**Behavior:**
1. Construct the Telegram Bot API URL: `https://api.telegram.org/bot{self._bot_token}/sendMessage`
2. Build the JSON payload: `{"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}`
3. Execute: `response = await self._client.post(url, json=payload, timeout=self._timeout)`
4. Check response status: `response.raise_for_status()`
5. Log `telegram.message_sent`.
6. **Catch block:** Wrap the entire body in a `try/except Exception` that catches ALL exceptions (including `httpx.TimeoutException`, `httpx.HTTPStatusError`, `httpx.ConnectError`, and any unexpected error). Log `telegram.send_failed` with the exception string and return `None`. NEVER re-raise.

**Critical safety invariant:** The `_send()` method is the single chokepoint for all outbound HTTP. Its `except Exception` block guarantees that no Telegram API failure can propagate to any Orchestrator loop. This is the core fail-open contract.

### 3.6 Message Formatting

#### Alert Messages

Format `AlertEvent` fields into a structured, human-readable text block:

```
[severity_emoji] ALERT: [rule_name]

[message]

Threshold: [threshold_value]
Actual: [actual_value]
Time: [alert_at_utc ISO format]
```

Severity emoji mapping:
- `CRITICAL` → `"🚨"`
- `WARNING` → `"⚠️"`
- `INFO` → `"ℹ️"`

Example output (dry run):
```
[DRY RUN] 🚨 ALERT: drawdown

Portfolio drawdown exceeds 100 USDC: unrealized PnL is -142.50 USDC

Threshold: 100
Actual: -142.50
Time: 2026-04-01T14:30:00+00:00
```

#### Execution Event Messages

The `summary` string is passed through directly (with optional `[DRY RUN] ` prefix). The caller (Orchestrator) is responsible for constructing the summary content. Recommended format in Orchestrator integration (see Section 5):

```
BUY ROUTED: [condition_id] | [order_size_usdc] USDC | action=[action]
```

```
SELL ROUTED: [position_id] | exit_price=[exit_price] | action=[action]
```

## 4. Configuration

### 4.1 New `AppConfig` Fields

Add the following fields to `AppConfig` in `src/core/config.py`, grouped under a `# --- Telegram Notifier (WI-26) ---` comment block, placed after the existing `# --- Alert Engine (WI-25) ---` block:

```python
# --- Telegram Notifier (WI-26) ---
enable_telegram_notifier: bool = Field(
    default=False,
    description="Enable Telegram notification delivery for alerts and execution events",
)
telegram_bot_token: SecretStr = Field(
    default=SecretStr(""),
    description="Telegram Bot API token (from @BotFather)",
)
telegram_chat_id: str = Field(
    default="",
    description="Telegram chat ID for notification delivery",
)
telegram_send_timeout_sec: Decimal = Field(
    default=Decimal("5"),
    description="Hard timeout in seconds for each Telegram sendMessage call",
)
```

### 4.2 Config-Gate Logic

`TelegramNotifier` is constructed in `Orchestrator.__init__()` ONLY when ALL of the following conditions are met:
1. `config.enable_telegram_notifier is True`
2. `config.telegram_bot_token.get_secret_value() != ""`
3. `config.telegram_chat_id != ""`

If any condition is false, set `self.telegram_notifier = None` and log `telegram.disabled` with the reason. This ensures zero HTTP client construction and zero outbound calls when the feature is off.

## 5. Orchestrator Integration

### 5.1 Construction (`__init__`)

After `self.alert_engine = AlertEngine(config=self.config)` (line 136), add:

```python
# WI-26: Telegram Notifier (config-gated)
self._telegram_client: httpx.AsyncClient | None = None
self.telegram_notifier: TelegramNotifier | None = None
if (
    self.config.enable_telegram_notifier
    and self.config.telegram_bot_token.get_secret_value()
    and self.config.telegram_chat_id
):
    self._telegram_client = httpx.AsyncClient()
    self.telegram_notifier = TelegramNotifier(
        config=self.config,
        http_client=self._telegram_client,
    )
else:
    logger.info("telegram.disabled")
```

**Key:** `TelegramNotifier` gets its own dedicated `httpx.AsyncClient` (`self._telegram_client`) separate from `self._httpx_client` (used by `GammaRESTClient`). This ensures the Telegram timeout configuration does not interfere with market data HTTP calls.

### 5.2 Wiring into `_portfolio_aggregation_loop()`

After the existing `AlertEngine.evaluate()` block that logs `alert_engine.alerts_fired`, iterate over the fired alerts and send each one:

```python
# Inside the `if alerts:` block, after the existing structlog warning:
if self.telegram_notifier is not None:
    for alert in alerts:
        try:
            await self.telegram_notifier.send_alert(alert)
        except Exception:
            pass  # send_alert already swallows; belt-and-suspenders
```

**Invariant:** The `for alert in alerts` loop is inside the existing `try/except` that wraps alert evaluation. A Telegram failure (even if `send_alert` somehow leaks) cannot crash the aggregation loop.

### 5.3 Wiring into `_execution_consumer_loop()`

After the position tracking block and before the `dry_run` skip gate (after line 276 in the current orchestrator), add a Telegram notification for BUY-routed events:

```python
# After position tracking, before dry_run skip gate:
if (
    self.telegram_notifier is not None
    and execution_result.action in (ExecutionAction.EXECUTED, ExecutionAction.DRY_RUN)
):
    summary = (
        f"BUY ROUTED: {condition_id}"
        f" | {execution_result.order_size_usdc} USDC"
        f" | action={execution_result.action.value}"
    )
    try:
        await self.telegram_notifier.send_execution_event(
            summary=summary,
            dry_run=self.config.dry_run,
        )
    except Exception:
        pass  # send_execution_event already swallows
```

**Placement rationale:** This fires for BOTH `EXECUTED` and `DRY_RUN` actions so the operator sees all routed decisions during paper trading. It fires BEFORE the `dry_run` skip gate so that dry-run BUY events are still notified.

### 5.4 Wiring into `_exit_scan_loop()`

After the PnL settlement block and before the broadcast block (after line 393 in the current orchestrator), add a Telegram notification for SELL-routed events:

```python
# After PnL settlement, before broadcast:
if (
    self.telegram_notifier is not None
    and exit_order_result.action in (
        ExitOrderAction.SELL_ROUTED,
        ExitOrderAction.DRY_RUN,
    )
):
    summary = (
        f"SELL ROUTED: {exit_result.position_id}"
        f" | exit_price={exit_order_result.exit_price}"
        f" | action={exit_order_result.action.value}"
    )
    try:
        await self.telegram_notifier.send_execution_event(
            summary=summary,
            dry_run=self.config.dry_run,
        )
    except Exception:
        pass  # send_execution_event already swallows
```

### 5.5 Shutdown (`shutdown()`)

Add cleanup for `self._telegram_client` in `Orchestrator.shutdown()`, after the existing `self._httpx_client` cleanup:

```python
if self._telegram_client is not None:
    await self._telegram_client.aclose()
    self._telegram_client = None
```

## 6. structlog Events

| Event | Level | When | Fields |
|---|---|---|---|
| `telegram.message_sent` | `info` | Successful delivery to Telegram API | `rule_name` (if alert), `severity` (if alert) |
| `telegram.send_failed` | `error` | Any exception in `_send()` | `error` (str) |
| `telegram.disabled` | `info` | Config gate prevents construction | (none or reason string) |

## 7. Safety Invariants

1. **Fail-open:** `_send()` catches `Exception` at the broadest level. No Telegram API failure — timeout, 4xx, 5xx, DNS resolution failure, connection refused — can propagate to any Orchestrator loop. This is the single most important invariant.
2. **Non-blocking:** All HTTP calls are bounded by `telegram_send_timeout_sec` (default 5s). No unbounded `await`.
3. **Zero DB writes:** `TelegramNotifier` does not import or interact with any repository, ORM model, or database session.
4. **Zero upstream mutation:** `TelegramNotifier` does not modify, halt, or influence any upstream component (AlertEngine, ExecutionRouter, ExitOrderRouter, PnLCalculator, etc.).
5. **Config-gated:** When `enable_telegram_notifier=False` or credentials are empty, no `TelegramNotifier` or `httpx.AsyncClient` is constructed. Zero runtime overhead.
6. **Secret protection:** `telegram_bot_token` is `SecretStr`. The raw token value must NEVER appear in structlog events, error messages, or exception strings. It is only exposed inside `_send()` when constructing the API URL.
7. **Dry-run transparency:** When `dry_run=True`, messages are prefixed with `[DRY RUN] `. Sending is NOT suppressed — the operator should see all events during paper trading.
8. **Module isolation:** Zero imports from `src/agents/ingestion/`, `src/agents/context/`, `src/agents/evaluation/`, or any repository/ORM module.

## 8. Test Plan

### 8.1 Unit Tests — `tests/unit/test_telegram_notifier.py`

| # | Test Name | Assertion |
|---|---|---|
| 1 | `test_send_alert_formats_critical_message` | `_send()` called with message containing `"🚨 ALERT: drawdown"` and threshold/actual values |
| 2 | `test_send_alert_formats_warning_message` | `_send()` called with message containing `"⚠️ ALERT:"` |
| 3 | `test_send_alert_formats_info_message` | `_send()` called with message containing `"ℹ️ ALERT:"` |
| 4 | `test_send_alert_dry_run_prefix` | When `alert.dry_run=True`, message text starts with `"[DRY RUN] "` |
| 5 | `test_send_alert_live_no_prefix` | When `alert.dry_run=False`, message text does NOT contain `"[DRY RUN]"` |
| 6 | `test_send_execution_event_dry_run_prefix` | When `dry_run=True`, summary starts with `"[DRY RUN] "` |
| 7 | `test_send_execution_event_live_no_prefix` | When `dry_run=False`, summary passed through without prefix |
| 8 | `test_send_swallows_timeout_exception` | Mock `httpx.AsyncClient.post` raises `httpx.TimeoutException` → method returns `None`, no exception propagated |
| 9 | `test_send_swallows_http_status_error` | Mock returns HTTP 403 → `raise_for_status()` throws → swallowed, returns `None` |
| 10 | `test_send_swallows_connect_error` | Mock raises `httpx.ConnectError` → swallowed |
| 11 | `test_send_swallows_generic_exception` | Mock raises `RuntimeError` → swallowed |
| 12 | `test_send_posts_correct_url` | Verify POST URL is `https://api.telegram.org/bot<token>/sendMessage` |
| 13 | `test_send_posts_correct_payload` | Verify JSON payload contains `chat_id` and `text` fields |
| 14 | `test_send_uses_configured_timeout` | Verify `httpx.post()` called with `timeout` matching config value |
| 15 | `test_bot_token_not_in_log_events` | After a failed send, assert the raw bot token string does not appear in any captured structlog event |

### 8.2 Integration Tests — `tests/integration/test_telegram_notifier_integration.py`

| # | Test Name | Assertion |
|---|---|---|
| 1 | `test_notifier_disabled_when_config_flag_false` | `enable_telegram_notifier=False` → `telegram_notifier is None` in Orchestrator |
| 2 | `test_notifier_disabled_when_token_empty` | `enable_telegram_notifier=True` + empty token → `telegram_notifier is None` |
| 3 | `test_notifier_disabled_when_chat_id_empty` | `enable_telegram_notifier=True` + empty chat_id → `telegram_notifier is None` |
| 4 | `test_notifier_constructed_when_fully_configured` | All three config gates satisfied → `telegram_notifier is not None` |
| 5 | `test_alert_loop_continues_after_telegram_failure` | Mock Telegram API failure → `_portfolio_aggregation_loop` continues to next cycle without crash |
| 6 | `test_execution_loop_continues_after_telegram_failure` | Mock Telegram API failure → `_execution_consumer_loop` continues processing next queue item |
| 7 | `test_exit_scan_continues_after_telegram_failure` | Mock Telegram API failure → `_exit_scan_loop` continues evaluating next position |
| 8 | `test_shutdown_closes_telegram_client` | After `Orchestrator.shutdown()`, verify `_telegram_client` is `None` and `aclose()` was called |

### 8.3 Regression Gate

```bash
pytest --asyncio-mode=auto tests/ -q
# Expected: all existing tests + new WI-26 tests pass (0 failures)

.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m
# Expected: coverage >= 80% (maintained from 94%)
```

## 9. Acceptance Criteria (Strict)

1. `TelegramNotifier` exists in `src/agents/execution/telegram_notifier.py` with exactly two public async methods: `send_alert(alert: AlertEvent) -> None` and `send_execution_event(summary: str, dry_run: bool) -> None`.
2. Both methods catch ALL exceptions internally and never raise to the caller.
3. The internal `_send(text: str)` method POSTs to `https://api.telegram.org/bot<token>/sendMessage` with `{"chat_id": ..., "text": ..., "parse_mode": "HTML"}`.
4. `httpx.AsyncClient.post()` is called with `timeout=float(config.telegram_send_timeout_sec)`.
5. `AppConfig.enable_telegram_notifier` is `bool` with default `False`.
6. `AppConfig.telegram_bot_token` is `SecretStr` with default `SecretStr("")`.
7. `AppConfig.telegram_chat_id` is `str` with default `""`.
8. `AppConfig.telegram_send_timeout_sec` is `Decimal` with default `Decimal("5")`.
9. `TelegramNotifier` is constructed in `Orchestrator.__init__()` only when `enable_telegram_notifier=True` AND `telegram_bot_token` is non-empty AND `telegram_chat_id` is non-empty.
10. `send_alert()` is invoked in `_portfolio_aggregation_loop()` for each fired `AlertEvent`.
11. `send_execution_event()` is invoked in `_execution_consumer_loop()` after a BUY is routed (`EXECUTED` or `DRY_RUN`).
12. `send_execution_event()` is invoked in `_exit_scan_loop()` after a SELL is routed (`SELL_ROUTED` or `DRY_RUN`).
13. When `dry_run=True`, all messages are prefixed with `[DRY RUN] `. Sending is NOT suppressed.
14. The `httpx.AsyncClient` used by `TelegramNotifier` (`self._telegram_client`) is closed during `Orchestrator.shutdown()`.
15. `TelegramNotifier` has zero imports from `src/agents/ingestion/`, `src/agents/context/`, `src/agents/evaluation/`, or any repository/ORM module.
16. `TelegramNotifier` performs zero DB writes.
17. The raw `telegram_bot_token` value never appears in structlog events.
18. All unit tests (Section 8.1) and integration tests (Section 8.2) pass.
19. Full regression remains green with coverage >= 80%.
