# P26-WI-26 — Telegram Telemetry Sink Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-4.1 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi26-telegram-sink` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/security-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-26 for Phase 9: a non-blocking, fail-open async notification component (`TelegramNotifier`) that delivers typed `AlertEvent` objects (from WI-25 `AlertEngine`) and critical execution confirmations (BUY/SELL routed) to the operator's mobile device via the Telegram Bot API.

This WI introduces one new outbound HTTP component. It is strictly non-blocking and fail-open: if the Telegram API is unreachable, times out, returns a 4xx/5xx, or raises any exception, the notifier logs the failure and returns cleanly. It MUST NEVER crash the caller, block the Orchestrator, or influence any upstream component. Telegram is a best-effort notification sink — the bot's core pipeline must remain fully operational regardless of Telegram API availability.

When `dry_run=True`, messages are prefixed with `[DRY RUN] `. Sending is NOT suppressed — the operator should receive telemetry during simulation runs.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi26.md`
4. `docs/PRD-v9.0.md` (Phase 9 / WI-26 section)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `src/orchestrator.py` — **integration target; `TelegramNotifier` config-gated in `__init__()`, invoked within `_portfolio_aggregation_loop()`, `_execution_consumer_loop()`, and `_exit_scan_loop()`**
8. `src/schemas/risk.py` (context: `AlertEvent`, `AlertSeverity` from WI-25 — consumed, NOT modified)
9. `src/core/config.py` (target: add 4 new Telegram fields)
10. `src/agents/execution/alert_engine.py` (context: upstream WI-25 component — NOT modified)
11. Existing tests:
    - `tests/unit/test_alert_engine.py`
    - `tests/integration/test_alert_engine_integration.py`
    - `tests/unit/test_exit_scan_loop.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create WI-26 test files first:
   - `tests/unit/test_telegram_notifier.py`
   - `tests/integration/test_telegram_notifier_integration.py`
2. Write failing tests for all required behaviors:

   **Message formatting — Alert messages:**
   - `send_alert()` formats a `CRITICAL` alert into a message containing the `"🚨"` emoji, `"ALERT: drawdown"`, the threshold value, the actual value, and the ISO timestamp.
   - `send_alert()` formats a `WARNING` alert into a message containing the `"⚠️"` emoji.
   - `send_alert()` formats an `INFO` alert into a message containing the `"ℹ️"` emoji.
   - `send_alert()` with `alert.dry_run=True` produces a message starting with `"[DRY RUN] "`.
   - `send_alert()` with `alert.dry_run=False` produces a message that does NOT contain `"[DRY RUN]"`.

   **Message formatting — Execution event messages:**
   - `send_execution_event()` with `dry_run=True` produces a message starting with `"[DRY RUN] "`.
   - `send_execution_event()` with `dry_run=False` passes the summary through without prefix.

   **HTTP call correctness:**
   - `_send()` POSTs to `https://api.telegram.org/bot<token>/sendMessage` (verify URL contains bot token and path).
   - `_send()` sends JSON payload with `chat_id`, `text`, and `parse_mode` keys.
   - `_send()` calls `httpx.AsyncClient.post()` with `timeout` matching the configured `telegram_send_timeout_sec`.

   **Fail-open error swallowing:**
   - `send_alert()` swallows `httpx.TimeoutException` — returns `None`, no exception propagated to caller.
   - `send_alert()` swallows HTTP 403 (`httpx.HTTPStatusError` from `raise_for_status()`) — returns `None`, no exception propagated.
   - `send_alert()` swallows HTTP 500 — returns `None`, no exception propagated.
   - `send_alert()` swallows `httpx.ConnectError` — returns `None`, no exception propagated.
   - `send_alert()` swallows generic `RuntimeError` — returns `None`, no exception propagated.
   - `send_execution_event()` swallows `httpx.TimeoutException` — same contract.

   **Secret protection:**
   - After a failed `_send()`, the raw bot token string does NOT appear in any captured structlog event fields.

   **Config gating:**
   - `AppConfig.enable_telegram_notifier` is `bool` with default `False`.
   - `AppConfig.telegram_bot_token` is `SecretStr` with default `SecretStr("")`.
   - `AppConfig.telegram_chat_id` is `str` with default `""`.
   - `AppConfig.telegram_send_timeout_sec` is `Decimal` with default `Decimal("5")`.

   **Orchestrator integration:**
   - `TelegramNotifier` module has no dependency on prompt/context/evaluation/ingestion/database modules (import boundary check).
   - When `enable_telegram_notifier=False`, `Orchestrator` sets `self.telegram_notifier = None`.
   - When `enable_telegram_notifier=True` but `telegram_bot_token` is empty, `Orchestrator` sets `self.telegram_notifier = None`.
   - When `enable_telegram_notifier=True` but `telegram_chat_id` is empty, `Orchestrator` sets `self.telegram_notifier = None`.
   - When all three config gates are satisfied, `Orchestrator` sets `self.telegram_notifier` to a `TelegramNotifier` instance.
   - `_portfolio_aggregation_loop()` calls `send_alert()` for each fired `AlertEvent` when `self.telegram_notifier is not None`.
   - `_portfolio_aggregation_loop()` continues to next cycle after a Telegram send failure — loop does not crash.
   - `_execution_consumer_loop()` calls `send_execution_event()` after a BUY is routed (`EXECUTED` or `DRY_RUN` action) when `self.telegram_notifier is not None`.
   - `_execution_consumer_loop()` continues processing next queue item after a Telegram send failure.
   - `_exit_scan_loop()` calls `send_execution_event()` after a SELL is routed (`SELL_ROUTED` or `DRY_RUN` action) when `self.telegram_notifier is not None`.
   - `_exit_scan_loop()` continues evaluating next position after a Telegram send failure.
   - `Orchestrator.shutdown()` closes `self._telegram_client` via `aclose()` and sets it to `None`.

3. Run RED tests:
   - `pytest tests/unit/test_telegram_notifier.py -v`
   - `pytest tests/integration/test_telegram_notifier_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add Telegram Config Fields to `src/core/config.py`

Target:
- `src/core/config.py`

Requirements:
1. Add `SecretStr` to the existing `from pydantic import ...` import line (it should already be imported for `wallet_private_key`).
2. Add the following fields to `AppConfig` after the `# --- Alert Engine (WI-25) ---` block:
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
3. Do NOT modify any existing `AppConfig` fields.

Run targeted tests after this step:
```bash
pytest tests/unit/test_telegram_notifier.py -k "config or AppConfig" -v
```

### Step 2 — Create `TelegramNotifier` Module

Target:
- `src/agents/execution/telegram_notifier.py` (new)

Requirements:
1. New class `TelegramNotifier` with constructor:
   ```python
   def __init__(self, config: AppConfig, http_client: httpx.AsyncClient) -> None:
   ```
2. Constructor extracts from config:
   - `self._bot_token: str` via `config.telegram_bot_token.get_secret_value()`
   - `self._chat_id: str` from `config.telegram_chat_id`
   - `self._timeout: float` via `float(config.telegram_send_timeout_sec)`
   - `self._client: httpx.AsyncClient` — the injected client
   - `self._log` via `structlog.get_logger(__name__)`
3. Two public async methods:
   - `async def send_alert(self, alert: AlertEvent) -> None`
   - `async def send_execution_event(self, summary: str, dry_run: bool) -> None`
4. One private async helper:
   - `async def _send(self, text: str) -> None`

**`send_alert` implementation:**
   1. Build severity emoji map: `CRITICAL` → `"🚨"`, `WARNING` → `"⚠️"`, `INFO` → `"ℹ️"`.
   2. Format the `AlertEvent` into a multi-line message:
      ```
      [emoji] ALERT: [rule_name]

      [message]

      Threshold: [threshold_value]
      Actual: [actual_value]
      Time: [alert_at_utc ISO format]
      ```
   3. If `alert.dry_run is True`, prepend `"[DRY RUN] "` to the formatted text.
   4. Call `await self._send(text)`.
   5. On success, log `telegram.message_sent` with `rule_name=alert.rule_name` and `severity=alert.severity.value`.

**`send_execution_event` implementation:**
   1. If `dry_run is True`, prepend `"[DRY RUN] "` to the summary.
   2. Call `await self._send(text)`.
   3. On success, log `telegram.message_sent`.

**`_send` implementation — THE CRITICAL FAIL-OPEN CHOKEPOINT:**
   ```python
   async def _send(self, text: str) -> None:
       url = f"https://api.telegram.org/bot{self._bot_token}/sendMessage"
       payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
       try:
           response = await self._client.post(url, json=payload, timeout=self._timeout)
           response.raise_for_status()
       except Exception as exc:
           self._log.error("telegram.send_failed", error=str(exc))
           return
   ```
   - The `try/except Exception` is intentionally broad. It catches:
     - `httpx.TimeoutException` (API timeout)
     - `httpx.HTTPStatusError` (4xx/5xx from `raise_for_status()`)
     - `httpx.ConnectError` (DNS failure, connection refused)
     - Any other unexpected exception
   - The `except` block logs `telegram.send_failed` with `error=str(exc)`. It MUST NOT include `self._bot_token` or the URL in the log fields.
   - The method returns `None` after logging. It NEVER re-raises.

5. **Module-level constants:** Define the emoji map as a module-level dict:
   ```python
   from src.schemas.risk import AlertEvent, AlertSeverity

   _SEVERITY_EMOJI: dict[AlertSeverity, str] = {
       AlertSeverity.CRITICAL: "🚨",
       AlertSeverity.WARNING: "⚠️",
       AlertSeverity.INFO: "ℹ️",
   }
   ```

6. **Zero imports from:**
   - `src/agents/evaluation/*`
   - `src/agents/context/*`
   - `src/agents/ingestion/*`
   - `src/agents/execution/alert_engine.py`
   - `src/agents/execution/portfolio_aggregator.py`
   - `src/agents/execution/lifecycle_reporter.py`
   - `src/agents/execution/exit_strategy_engine.py`
   - `src/agents/execution/exit_order_router.py`
   - `src/agents/execution/pnl_calculator.py`
   - `src/agents/execution/execution_router.py`
   - `src/agents/execution/broadcaster.py`
   - `src/agents/execution/signer.py`
   - `src/agents/execution/bankroll_sync.py`
   - `src/agents/execution/polymarket_client.py`
   - `src/db/*` (any repository, model, or session factory)
   - `sqlalchemy` (any module)

Run targeted tests after this step:
```bash
pytest tests/unit/test_telegram_notifier.py -v
```

### Step 3 — Integrate into Orchestrator

Target:
- `src/orchestrator.py`

Requirements:

#### 3a — Import and Constructor Wiring

1. **Add import:**
   ```python
   from src.agents.execution.telegram_notifier import TelegramNotifier
   ```
2. **Constructor wiring:** After `self.alert_engine = AlertEngine(config=self.config)` (currently the last component constructed before WebSocket/PromptFactory), add:
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
3. **Key design decision:** `TelegramNotifier` gets its own dedicated `httpx.AsyncClient` (`self._telegram_client`), separate from `self._httpx_client` (used by `GammaRESTClient`). This ensures the Telegram timeout configuration does not interfere with market data HTTP calls.

#### 3b — Wire into `_portfolio_aggregation_loop()`

After the existing `if alerts:` block that logs `alert_engine.alerts_fired`, add Telegram dispatch for each alert:

```python
# Inside the existing `if snapshot is not None and report is not None:` block,
# inside the existing `try:` block, after alert evaluation and logging:
if alerts:
    logger.warning(
        "alert_engine.alerts_fired",
        alert_count=len(alerts),
        rules=[alert.rule_name for alert in alerts],
        severities=[alert.severity.value for alert in alerts],
        dry_run=snapshot.dry_run,
    )
    # WI-26: Telegram notification for each fired alert
    if self.telegram_notifier is not None:
        for alert in alerts:
            try:
                await self.telegram_notifier.send_alert(alert)
            except Exception:
                pass  # send_alert already swallows; belt-and-suspenders
else:
    logger.info(
        "alert_engine.all_clear",
        dry_run=snapshot.dry_run,
    )
```

**Invariant:** The Telegram dispatch is inside the existing `try/except` that wraps alert evaluation. Belt-and-suspenders `except Exception: pass` ensures a leaked exception cannot crash the aggregation loop.

#### 3c — Wire into `_execution_consumer_loop()`

After the position tracking block (after the `except Exception` for `position_tracker.record_execution`) and BEFORE the `if self.config.dry_run:` skip gate, add:

```python
# WI-26: Telegram notification for BUY-routed events
if (
    self.telegram_notifier is not None
    and execution_result.action in (
        ExecutionAction.EXECUTED,
        ExecutionAction.DRY_RUN,
    )
):
    _buy_summary = (
        f"BUY ROUTED: {condition_id}"
        f" | {execution_result.order_size_usdc} USDC"
        f" | action={execution_result.action.value}"
    )
    try:
        await self.telegram_notifier.send_execution_event(
            summary=_buy_summary,
            dry_run=self.config.dry_run,
        )
    except Exception:
        pass  # send_execution_event already swallows
```

**Placement rationale:** This fires for BOTH `EXECUTED` and `DRY_RUN` actions. It fires BEFORE the `dry_run` skip gate so that dry-run BUY events are still notified. The local variable `_buy_summary` avoids polluting the loop scope.

#### 3d — Wire into `_exit_scan_loop()`

After the PnL settlement block (after the `except Exception` for `pnl_calculator.settle`) and BEFORE the broadcast block (the `if exit_order_result.action == ExitOrderAction.SELL_ROUTED and ...` guard), add:

```python
# WI-26: Telegram notification for SELL-routed events
if (
    self.telegram_notifier is not None
    and exit_order_result.action in (
        ExitOrderAction.SELL_ROUTED,
        ExitOrderAction.DRY_RUN,
    )
):
    _sell_summary = (
        f"SELL ROUTED: {exit_result.position_id}"
        f" | exit_price={exit_order_result.exit_price}"
        f" | action={exit_order_result.action.value}"
    )
    try:
        await self.telegram_notifier.send_execution_event(
            summary=_sell_summary,
            dry_run=self.config.dry_run,
        )
    except Exception:
        pass  # send_execution_event already swallows
```

#### 3e — Shutdown Cleanup

In `Orchestrator.shutdown()`, after the existing `self._httpx_client` cleanup block, add:

```python
if self._telegram_client is not None:
    await self._telegram_client.aclose()
    self._telegram_client = None
```

4. **No new `asyncio.create_task()`.** `TelegramNotifier` is invoked inline within existing loops. No new periodic task, no new queue.
5. **No new config gate on loop registration.** `TelegramNotifier` calls are guarded by `if self.telegram_notifier is not None` inside each loop. The loop registration logic (task creation) is unchanged.
6. **Task count unchanged:** 7 when `enable_portfolio_aggregator=True`, 6 when `False`.

Run targeted tests after this step:
```bash
pytest tests/integration/test_telegram_notifier_integration.py -v
pytest tests/integration/test_orchestrator.py -v
```

### Step 4 — GREEN Validation

Run:
```bash
pytest tests/unit/test_telegram_notifier.py -v
pytest tests/integration/test_telegram_notifier_integration.py -v
pytest tests/unit/test_alert_engine.py -v
pytest tests/integration/test_alert_engine_integration.py -v
pytest tests/unit/test_exit_scan_loop.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. **Fail-open / never crash caller.** `_send()` catches `Exception` at the broadest level. No Telegram API failure — timeout, 4xx, 5xx, DNS resolution failure, connection refused, invalid credentials, malformed response — can propagate to any Orchestrator loop. This is the single most important invariant of WI-26.
2. **Non-blocking.** All HTTP calls are bounded by `telegram_send_timeout_sec` (default 5s) via the `timeout` parameter on `httpx.AsyncClient.post()`. No unbounded `await`. No `asyncio.sleep` inside the notifier.
3. **Zero DB writes.** `TelegramNotifier` does not import or interact with any repository, ORM model, or database session. It is a one-way HTTP sink to an external API.
4. **Zero upstream mutation.** `TelegramNotifier` does not modify, halt, or influence any upstream component (`AlertEngine`, `ExecutionRouter`, `ExitOrderRouter`, `PnLCalculator`, `PortfolioAggregator`, `PositionLifecycleReporter`, etc.). It is a passive consumer of pre-computed events.
5. **Config-gated construction.** When `enable_telegram_notifier=False` (default) or either credential is empty, no `TelegramNotifier` or `httpx.AsyncClient` is constructed. Zero runtime overhead when the feature is off. Zero HTTP calls.
6. **Secret protection.** `telegram_bot_token` is `SecretStr`. The raw token value MUST NEVER appear in structlog events, error messages, or exception strings. It is only exposed inside `_send()` when constructing the API URL.
7. **Dry-run transparency.** When `dry_run=True`, messages are prefixed with `[DRY RUN] `. Sending is NOT suppressed. The operator should see all events during paper trading — this is a deliberate design choice.
8. **Module isolation.** Zero imports from `src/agents/ingestion/`, `src/agents/context/`, `src/agents/evaluation/`, or any repository/ORM module. Allowed imports: `src/schemas/risk.py` (`AlertEvent`, `AlertSeverity`), `src/core/config.py` (`AppConfig`), `httpx`, `structlog`.
9. **No bypass of `LLMEvaluationResponse` terminal Gatekeeper.** `TelegramNotifier` operates far downstream as a notification sink. It has no path to execution influence.
10. **Dedicated HTTP client.** `TelegramNotifier` uses `self._telegram_client`, NOT `self._httpx_client`. The Telegram timeout configuration must not interfere with market data HTTP calls via `GammaRESTClient`.
11. **Lifecycle management.** `self._telegram_client` is created in `Orchestrator.__init__()` (config-gated) and closed via `aclose()` in `Orchestrator.shutdown()`. No leaked HTTP connections.
12. **No new periodic task.** `TelegramNotifier` is invoked inline within three existing loops, not as a separate `asyncio.create_task()`. Task count is unchanged from WI-25.
13. **No queue topology changes.** `market_queue -> prompt_queue -> execution_queue`. No new queue introduced.
14. **No database schema changes.** Zero new tables, zero new columns, zero Alembic migrations.
15. **Frozen upstream components.** `AlertEngine`, `PortfolioAggregator`, `PositionLifecycleReporter`, `PnLCalculator`, `ExitStrategyEngine`, `ExitOrderRouter`, `ExecutionRouter`, `PolymarketClient`, `OrderBroadcaster`, `PositionTracker`, `PositionRepository`, and all schemas in `src/schemas/execution.py`, `src/schemas/position.py`, `src/schemas/risk.py` are byte-identical before and after WI-26. The only modified existing files are `src/core/config.py` (additive: 4 Telegram fields) and `src/orchestrator.py` (additive: import, constructor wiring, 3 inline call sites, shutdown cleanup).
16. **Belt-and-suspenders exception guards.** Each Telegram call site in the Orchestrator wraps the `await self.telegram_notifier.send_*()` call in its own `try/except Exception: pass` block, even though `send_alert` and `send_execution_event` already swallow exceptions internally. This protects against any future regression in the notifier's error handling.

---

## Required Test Matrix

At minimum, WI-26 tests must prove:

### Unit Tests — Message Formatting (Alert)
1. `send_alert()` formats a `CRITICAL` alert with `"🚨"` emoji, `"ALERT: drawdown"`, threshold, actual, and ISO timestamp.
2. `send_alert()` formats a `WARNING` alert with `"⚠️"` emoji.
3. `send_alert()` formats an `INFO` alert with `"ℹ️"` emoji.
4. `send_alert()` with `alert.dry_run=True` produces message starting with `"[DRY RUN] "`.
5. `send_alert()` with `alert.dry_run=False` produces message without `"[DRY RUN]"` prefix.

### Unit Tests — Message Formatting (Execution Event)
6. `send_execution_event()` with `dry_run=True` produces message starting with `"[DRY RUN] "`.
7. `send_execution_event()` with `dry_run=False` passes summary through without prefix.

### Unit Tests — HTTP Call Correctness
8. `_send()` POSTs to URL matching `https://api.telegram.org/bot<token>/sendMessage`.
9. `_send()` sends JSON payload with `chat_id`, `text`, and `parse_mode` keys.
10. `_send()` calls `httpx.AsyncClient.post()` with `timeout` matching configured value.

### Unit Tests — Fail-Open Error Swallowing
11. `send_alert()` swallows `httpx.TimeoutException` — returns `None`, no propagation.
12. `send_alert()` swallows HTTP 403 (`httpx.HTTPStatusError`) — returns `None`, no propagation.
13. `send_alert()` swallows HTTP 500 — returns `None`, no propagation.
14. `send_alert()` swallows `httpx.ConnectError` — returns `None`, no propagation.
15. `send_alert()` swallows generic `RuntimeError` — returns `None`, no propagation.
16. `send_execution_event()` swallows `httpx.TimeoutException` — same contract.

### Unit Tests — Secret Protection
17. After a failed `_send()`, the raw bot token string does NOT appear in any captured structlog event.

### Unit Tests — Config
18. `AppConfig.enable_telegram_notifier` is `bool` with default `False`.
19. `AppConfig.telegram_bot_token` is `SecretStr` with default `SecretStr("")`.
20. `AppConfig.telegram_chat_id` is `str` with default `""`.
21. `AppConfig.telegram_send_timeout_sec` is `Decimal` with default `Decimal("5")`.

### Integration Tests — Config Gating
22. `enable_telegram_notifier=False` → `self.telegram_notifier is None` in Orchestrator.
23. `enable_telegram_notifier=True` + empty token → `self.telegram_notifier is None`.
24. `enable_telegram_notifier=True` + empty chat_id → `self.telegram_notifier is None`.
25. All three config gates satisfied → `self.telegram_notifier is not None` and is a `TelegramNotifier` instance.

### Integration Tests — Module Isolation
26. `TelegramNotifier` module has no dependency on prompt/context/evaluation/ingestion/database modules (import boundary check).

### Integration Tests — Orchestrator Loop Resilience
27. `_portfolio_aggregation_loop()` calls `send_alert()` for fired alerts when notifier is present — loop continues after Telegram failure.
28. `_execution_consumer_loop()` calls `send_execution_event()` after BUY routed — loop continues after Telegram failure.
29. `_exit_scan_loop()` calls `send_execution_event()` after SELL routed — loop continues after Telegram failure.

### Integration Tests — Shutdown
30. `Orchestrator.shutdown()` calls `aclose()` on `self._telegram_client` and sets it to `None`.

### Regression Gate
31. Full suite: `pytest --asyncio-mode=auto tests/ -q` — all existing + new tests pass, 0 failures.
32. Coverage: `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` — >= 80%.

---

## Deliverables

1. RED-phase failing test summary.
2. GREEN implementation summary by file.
3. Passing targeted test summary + full regression summary.
4. Final staged `git diff` for MAAP checker review.

---

## MAAP Reflection Pass (Checker Prompt for Gemini 2.5 Pro)

Use the following exact prompt with the final staged `git diff`:

```text
You are the MAAP Checker for WI-26 (Telegram Telemetry Sink) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi26.md
2) docs/PRD-v9.0.md (WI-26 section)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:

- Fail-open violation (any code path in TelegramNotifier where an exception can propagate to the caller — _send() must catch Exception unconditionally)
- Blocking I/O (any httpx call without a timeout, any synchronous network call, any unbounded await)
- Secret leakage (telegram_bot_token raw value appearing in structlog events, error messages, exception strings, or any log field)
- DB interaction (any import from src/db/*, any AsyncSession, any repository call, any ORM model reference inside TelegramNotifier)
- Isolation violations (TelegramNotifier importing prompt, context, evaluation, ingestion, AlertEngine, PortfolioAggregator, PositionLifecycleReporter, ExitStrategyEngine, ExitOrderRouter, PnLCalculator, ExecutionRouter, OrderBroadcaster, TransactionSigner, BankrollSyncProvider, PolymarketClient, or sqlalchemy modules)
- Config-gate bypass (TelegramNotifier or httpx.AsyncClient constructed when enable_telegram_notifier=False or credentials are empty)
- Dry-run suppression (Telegram sends suppressed when dry_run=True — they should NOT be suppressed, only prefixed)
- Dry-run prefix missing (messages sent without "[DRY RUN] " prefix when dry_run=True)
- Upstream mutation (any modification to AlertEngine, PortfolioAggregator, PositionLifecycleReporter, PnLCalculator, ExitStrategyEngine, ExitOrderRouter, ExecutionRouter, PolymarketClient, PositionTracker, PositionRepository, OrderBroadcaster, or existing schemas in risk.py/execution.py/position.py)
- HTTP client lifecycle (self._telegram_client not closed in shutdown(), or sharing self._httpx_client instead of dedicated client)
- Task count regression (new asyncio.create_task for TelegramNotifier — should be inline only)
- Loop wiring completeness (send_alert missing from _portfolio_aggregation_loop, send_execution_event missing from _execution_consumer_loop or _exit_scan_loop)
- Belt-and-suspenders guards (Orchestrator call sites missing outer try/except Exception around send_alert/send_execution_event)
- Execution influence (TelegramNotifier affecting routing decisions, halting execution, modifying positions, or influencing any upstream component)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation)
- Decimal violations (any float used for monetary calculations — note: TelegramNotifier itself does no financial math, but verify it does not introduce float conversions of Decimal values)
- Regression (any modification to existing tests or coverage < 80%)

Additional required checks:
- TelegramNotifier class exists in src/agents/execution/telegram_notifier.py
- send_alert(alert: AlertEvent) -> None is async, catches all exceptions internally
- send_execution_event(summary: str, dry_run: bool) -> None is async, catches all exceptions internally
- _send(text: str) -> None wraps httpx.AsyncClient.post() in try/except Exception
- Constructor accepts config: AppConfig and http_client: httpx.AsyncClient
- Emoji mapping: CRITICAL→🚨, WARNING→⚠️, INFO→ℹ️
- AppConfig.enable_telegram_notifier: bool, default False
- AppConfig.telegram_bot_token: SecretStr, default SecretStr("")
- AppConfig.telegram_chat_id: str, default ""
- AppConfig.telegram_send_timeout_sec: Decimal, default Decimal("5")
- TelegramNotifier constructed in Orchestrator.__init__() only when all 3 gates pass
- self._telegram_client is a dedicated httpx.AsyncClient, separate from self._httpx_client
- _portfolio_aggregation_loop: iterates fired alerts, calls send_alert() for each
- _execution_consumer_loop: calls send_execution_event() for EXECUTED and DRY_RUN actions
- _exit_scan_loop: calls send_execution_event() for SELL_ROUTED and DRY_RUN actions
- Orchestrator.shutdown() calls self._telegram_client.aclose()
- telegram.message_sent structlog event on success
- telegram.send_failed structlog event on failure (without bot token in fields)
- telegram.disabled structlog event when config gate prevents construction
- No new asyncio.create_task — inline calls only
- Task count unchanged: 7 when enable_portfolio_aggregator=True, 6 when False
- Zero new database tables, columns, or Alembic migrations
- Zero new queues
- Queue topology unchanged: market_queue -> prompt_queue -> execution_queue

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-26/invariants
4) Explicit statement on each MAAP critical category:
   - Fail-open violation: CLEARED/FLAGGED
   - Blocking I/O: CLEARED/FLAGGED
   - Secret leakage: CLEARED/FLAGGED
   - DB interaction: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
   - Config-gate bypass: CLEARED/FLAGGED
   - Dry-run suppression: CLEARED/FLAGGED
   - Dry-run prefix missing: CLEARED/FLAGGED
   - Upstream mutation: CLEARED/FLAGGED
   - HTTP client lifecycle: CLEARED/FLAGGED
   - Task count regression: CLEARED/FLAGGED
   - Loop wiring completeness: CLEARED/FLAGGED
   - Belt-and-suspenders guards: CLEARED/FLAGGED
   - Execution influence: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Decimal violations: CLEARED/FLAGGED
   - Regression: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
