# WI-25 Business Logic — Alert Engine

## Active Agents + Constraints

- `.agents/rules/risk-auditor.md` — All financial comparisons (drawdown, PnL ratios) use `Decimal` arithmetic. No floats in computation or threshold comparison. `Decimal(str(value))` for any non-Decimal inputs.
- `.agents/rules/db-engineer.md` — Zero DB reads, zero DB writes. `AlertEngine` operates exclusively on pre-computed `PortfolioSnapshot` and `LifecycleReport` objects passed as arguments. No `AsyncSession`, no repository calls.
- `.agents/rules/security-auditor.md` — `dry_run` flag propagated from upstream snapshot/report into `AlertEvent` output for audit context. No credentials, private keys, or write paths touched.
- `.agents/rules/async-architect.md` — `AlertEngine` is stateless and synchronous. It is invoked within `_portfolio_aggregation_loop()` after both `compute_snapshot()` and `generate_report()` complete. No new `asyncio.create_task()` or queue introduced. The `evaluate()` method is plain (not async) because it performs zero I/O.
- `.agents/rules/test-engineer.md` — WI-25 requires unit + integration coverage for each alert rule, threshold edge cases, division-by-zero guards, multi-rule firing, and orchestrator wiring. Full suite remains >= 80%.

## 1. Objective

Introduce `AlertEngine`, a stateless, read-only, rule-based monitoring component that evaluates pre-computed portfolio and lifecycle metrics against configurable risk thresholds and emits a list of typed `AlertEvent` objects.

`AlertEngine` owns:
- Evaluation of portfolio health via configurable threshold rules
- Typed `AlertEvent` emission with severity classification, threshold/actual value pairs, and human-readable messages
- Division-by-zero guards for all ratio-based rules
- `dry_run` flag propagation from upstream data into alert output

`AlertEngine` does NOT own:
- Portfolio snapshot computation (upstream: `PortfolioAggregator`, WI-23)
- Lifecycle report generation (upstream: `PositionLifecycleReporter`, WI-24)
- Position lifecycle management (upstream: `PositionTracker`, WI-17)
- Exit evaluation or exit routing (upstream: `ExitStrategyEngine` WI-19, `ExitOrderRouter` WI-20)
- PnL settlement or DB writes (upstream: `PnLCalculator`, WI-21)
- Order execution, signing, or broadcasting
- Alert persistence, notification delivery, or external webhook dispatch
- Halting or pausing execution — alerts are observational only
- Auto-remediation or position modification in response to alerts

## 2. Scope Boundaries

### In Scope

1. New `AlertEngine` class in `src/agents/execution/alert_engine.py`.
2. New `AlertEvent` frozen Pydantic model in `src/schemas/risk.py` (extends existing file from WI-23/WI-24).
3. New `AlertSeverity` enum (`INFO`, `WARNING`, `CRITICAL`) in `src/schemas/risk.py`.
4. `evaluate(snapshot: PortfolioSnapshot, report: LifecycleReport) -> list[AlertEvent]` as the sole public method.
5. Four configurable alert rules evaluated in deterministic order:
   - **Drawdown alert** — fires when `total_unrealized_pnl` exceeds a negative threshold.
   - **Stale-price alert** — fires when the ratio of stale-price positions exceeds a threshold.
   - **Position-count alert** — fires when the number of open positions exceeds a threshold.
   - **Losing-streak alert** — fires when the loss rate among settled positions exceeds a threshold.
6. Four new `AppConfig` threshold fields with conservative defaults.
7. Orchestrator wiring: constructed in `__init__()`, invoked within `_portfolio_aggregation_loop()` after both `compute_snapshot()` and `generate_report()` complete.
8. structlog audit events for alert evaluation results.

### Out of Scope

1. New database tables, migrations, or DB writes — this component emits in-memory events only.
2. Alert persistence to database or file.
3. Notification delivery (Slack, email, PagerDuty, webhook).
4. Execution halting, circuit-breaking, or position modification.
5. Historical alert deduplication or cooldown/suppression windows.
6. Dynamic threshold adjustment at runtime.
7. Custom or user-defined alert rules beyond the four specified.
8. Async I/O of any kind — `AlertEngine.evaluate()` is synchronous.
9. Modifications to `PortfolioAggregator`, `PositionLifecycleReporter`, `PnLCalculator`, `ExitStrategyEngine`, `ExitOrderRouter`, or `ExecutionRouter` internals.

## 3. Target Component Architecture + Data Contracts

### 3.1 AlertEngine Component (New Class)

- **Module:** `src/agents/execution/alert_engine.py`
- **Class Name:** `AlertEngine` (exact)
- **Responsibility:** Accept a `PortfolioSnapshot` and `LifecycleReport`, evaluate each configured alert rule against their respective thresholds, and return a list of `AlertEvent` objects for any rules that fire.

Isolation rules:
- `AlertEngine` must not import LLM prompt construction, context-building, evaluation, or ingestion modules.
- `AlertEngine` must not mutate position status or write to the database.
- `AlertEngine` must not influence routing, exit decisions, or any upstream component.
- `AlertEngine` does not call `TransactionSigner`, `OrderBroadcaster`, `BankrollSyncProvider`, `PolymarketClient`, or any repository.
- `AlertEngine` does not perform async I/O. The `evaluate()` method is synchronous (`def`, not `async def`).

### 3.2 Data Contracts

#### 3.2.1 `AlertSeverity` enum (New)

Location: `src/schemas/risk.py` (existing file, add above `AlertEvent`)

```python
from enum import Enum

class AlertSeverity(str, Enum):
    """Severity classification for alert events."""
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
```

#### 3.2.2 `AlertEvent` model (New)

Location: `src/schemas/risk.py` (existing file, add after `AlertSeverity`)

```python
class AlertEvent(BaseModel):
    """Typed immutable alert event emitted by AlertEngine."""

    alert_at_utc: datetime
    severity: AlertSeverity
    rule_name: str
    message: str
    threshold_value: Decimal
    actual_value: Decimal
    dry_run: bool

    @field_validator(
        "threshold_value",
        "actual_value",
        mode="before",
    )
    @classmethod
    def _reject_float_financials(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float financial values are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    model_config = {"frozen": True}
```

Hard rules:
- Both financial fields (`threshold_value`, `actual_value`) are `Decimal`. Float is rejected at Pydantic boundary.
- Model is frozen (immutable after construction).
- `severity` is typed to `AlertSeverity` enum.
- `rule_name` is a machine-readable identifier (e.g., `"drawdown"`, `"stale_price"`, `"max_positions"`, `"loss_rate"`).
- `message` is a human-readable summary string.
- `dry_run` reflects the snapshot's `dry_run` state for downstream audit consumers.

Field definitions:

| Field | Type | Description |
|-------|------|-------------|
| `alert_at_utc` | `datetime` | UTC timestamp when the alert was evaluated |
| `severity` | `AlertSeverity` | Alert severity: `INFO`, `WARNING`, or `CRITICAL` |
| `rule_name` | `str` | Machine-readable rule identifier |
| `message` | `str` | Human-readable alert description |
| `threshold_value` | `Decimal` | The configured threshold that was breached |
| `actual_value` | `Decimal` | The actual computed value that breached the threshold |
| `dry_run` | `bool` | Whether the system is in dry-run mode |

### 3.3 New AppConfig Threshold Fields (Required)

Four new fields must be added to `AppConfig` in `src/core/config.py`:

```python
# --- Alert Engine (WI-25) ---
alert_drawdown_usdc: Decimal = Field(
    default=Decimal("100"),
    description="USDC drawdown threshold for CRITICAL alert (fires when total_unrealized_pnl < -threshold)",
)
alert_stale_price_pct: Decimal = Field(
    default=Decimal("0.50"),
    description="Stale-price ratio threshold for WARNING alert (fires when stale/total > threshold)",
)
alert_max_open_positions: int = Field(
    default=20,
    description="Maximum open positions before WARNING alert fires",
)
alert_loss_rate_pct: Decimal = Field(
    default=Decimal("0.60"),
    description="Loss rate threshold for WARNING alert (fires when losing/settled > threshold)",
)
```

Hard constraints:
1. `alert_drawdown_usdc` is `Decimal`, default `Decimal("100")`. Represents a positive USDC amount — the rule fires when `total_unrealized_pnl < -alert_drawdown_usdc`.
2. `alert_stale_price_pct` is `Decimal`, default `Decimal("0.50")` (50%). The rule fires when `positions_with_stale_price / position_count > alert_stale_price_pct`.
3. `alert_max_open_positions` is `int`, default `20`. The rule fires when `position_count > alert_max_open_positions`.
4. `alert_loss_rate_pct` is `Decimal`, default `Decimal("0.60")` (60%). The rule fires when `losing_count / total_settled_count > alert_loss_rate_pct`.
5. All threshold fields are read once at `AlertEngine` construction time. Not dynamically adjustable at runtime.

## 4. Core Method Contracts (typed)

### 4.1 Constructor

```python
class AlertEngine:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
```

Dependencies:
1. `config: AppConfig` — provides `dry_run` flag and all four alert threshold fields.

No database session factory, no `PolymarketClient`, no `TransactionSigner`, no `OrderBroadcaster` — this component takes pre-computed data as arguments and performs pure computation.

### 4.2 Evaluate Entry Point

```python
def evaluate(
    self,
    snapshot: PortfolioSnapshot,
    report: LifecycleReport,
) -> list[AlertEvent]:
```

This is the sole public method. It is **synchronous** (not `async def`) because it performs zero I/O. It returns an empty list when no rules fire. It evaluates all four rules in deterministic order and collects every fired alert — a single call may return 0 to 4 alerts.

### 4.3 Alert Rules (Deterministic Evaluation Order)

All four rules are evaluated on every call to `evaluate()`, regardless of whether earlier rules fired. A single evaluation may return multiple alerts. The evaluation order is fixed:

#### Rule 1: Drawdown Alert

```
IF snapshot.total_unrealized_pnl < -(config.alert_drawdown_usdc):
    EMIT AlertEvent(
        severity=CRITICAL,
        rule_name="drawdown",
        message="Portfolio drawdown exceeds {alert_drawdown_usdc} USDC: unrealized PnL is {total_unrealized_pnl} USDC",
        threshold_value=config.alert_drawdown_usdc,
        actual_value=snapshot.total_unrealized_pnl,
    )
```

- **Severity:** `CRITICAL` — drawdown is the most severe portfolio health signal.
- **Math:** Pure `Decimal` comparison: `snapshot.total_unrealized_pnl < Decimal("0") - self._config.alert_drawdown_usdc`.
- **No division** — no division-by-zero risk.

#### Rule 2: Stale-Price Alert

```
IF snapshot.position_count > 0:
    stale_ratio = Decimal(snapshot.positions_with_stale_price) / Decimal(snapshot.position_count)
    IF stale_ratio > config.alert_stale_price_pct:
        EMIT AlertEvent(
            severity=WARNING,
            rule_name="stale_price",
            message="Stale price ratio {stale_ratio} exceeds threshold {alert_stale_price_pct}",
            threshold_value=config.alert_stale_price_pct,
            actual_value=stale_ratio,
        )
```

- **Severity:** `WARNING` — stale prices degrade snapshot accuracy but don't indicate direct loss.
- **Division-by-zero guard:** Rule is skipped entirely when `snapshot.position_count == 0`. No alert is emitted for an empty portfolio.
- **Math:** `Decimal(str(positions_with_stale_price)) / Decimal(str(position_count))`. Both operands are converted to `Decimal` via `str()` to prevent any implicit float path.

#### Rule 3: Position-Count Alert

```
IF snapshot.position_count > config.alert_max_open_positions:
    EMIT AlertEvent(
        severity=WARNING,
        rule_name="max_positions",
        message="Open position count {position_count} exceeds limit {alert_max_open_positions}",
        threshold_value=Decimal(str(config.alert_max_open_positions)),
        actual_value=Decimal(str(snapshot.position_count)),
    )
```

- **Severity:** `WARNING` — concentration risk signal, not an immediate loss.
- **No division** — integer comparison only.
- **`threshold_value` and `actual_value`** are stored as `Decimal` for schema consistency, even though the underlying values are `int`.

#### Rule 4: Losing-Streak Alert

```
IF report.total_settled_count > 0:
    loss_rate = Decimal(report.losing_count) / Decimal(report.total_settled_count)
    IF loss_rate > config.alert_loss_rate_pct:
        EMIT AlertEvent(
            severity=WARNING,
            rule_name="loss_rate",
            message="Loss rate {loss_rate} exceeds threshold {alert_loss_rate_pct}",
            threshold_value=config.alert_loss_rate_pct,
            actual_value=loss_rate,
        )
```

- **Severity:** `WARNING` — historical loss rate is a strategy health signal, not an immediate emergency.
- **Division-by-zero guard:** Rule is skipped entirely when `report.total_settled_count == 0`. No alert is emitted when there are no settled positions.
- **Math:** `Decimal(str(losing_count)) / Decimal(str(total_settled_count))`. Both operands converted via `str()`.

### 4.4 Pseudocode (Complete)

```python
def evaluate(
    self,
    snapshot: PortfolioSnapshot,
    report: LifecycleReport,
) -> list[AlertEvent]:
    """Evaluate all alert rules against current portfolio state."""
    alerts: list[AlertEvent] = []
    now = datetime.now(timezone.utc)
    dry_run = snapshot.dry_run

    # Rule 1: Drawdown
    neg_threshold = _ZERO - self._config.alert_drawdown_usdc
    if snapshot.total_unrealized_pnl < neg_threshold:
        alerts.append(
            AlertEvent(
                alert_at_utc=now,
                severity=AlertSeverity.CRITICAL,
                rule_name="drawdown",
                message=(
                    f"Portfolio drawdown exceeds "
                    f"{self._config.alert_drawdown_usdc} USDC: "
                    f"unrealized PnL is {snapshot.total_unrealized_pnl} USDC"
                ),
                threshold_value=self._config.alert_drawdown_usdc,
                actual_value=snapshot.total_unrealized_pnl,
                dry_run=dry_run,
            )
        )

    # Rule 2: Stale price ratio
    if snapshot.position_count > 0:
        stale_ratio = Decimal(str(snapshot.positions_with_stale_price)) / Decimal(
            str(snapshot.position_count)
        )
        if stale_ratio > self._config.alert_stale_price_pct:
            alerts.append(
                AlertEvent(
                    alert_at_utc=now,
                    severity=AlertSeverity.WARNING,
                    rule_name="stale_price",
                    message=(
                        f"Stale price ratio {stale_ratio} exceeds "
                        f"threshold {self._config.alert_stale_price_pct}"
                    ),
                    threshold_value=self._config.alert_stale_price_pct,
                    actual_value=stale_ratio,
                    dry_run=dry_run,
                )
            )

    # Rule 3: Position count
    if snapshot.position_count > self._config.alert_max_open_positions:
        alerts.append(
            AlertEvent(
                alert_at_utc=now,
                severity=AlertSeverity.WARNING,
                rule_name="max_positions",
                message=(
                    f"Open position count {snapshot.position_count} "
                    f"exceeds limit {self._config.alert_max_open_positions}"
                ),
                threshold_value=Decimal(str(self._config.alert_max_open_positions)),
                actual_value=Decimal(str(snapshot.position_count)),
                dry_run=dry_run,
            )
        )

    # Rule 4: Loss rate
    if report.total_settled_count > 0:
        loss_rate = Decimal(str(report.losing_count)) / Decimal(
            str(report.total_settled_count)
        )
        if loss_rate > self._config.alert_loss_rate_pct:
            alerts.append(
                AlertEvent(
                    alert_at_utc=now,
                    severity=AlertSeverity.WARNING,
                    rule_name="loss_rate",
                    message=(
                        f"Loss rate {loss_rate} exceeds "
                        f"threshold {self._config.alert_loss_rate_pct}"
                    ),
                    threshold_value=self._config.alert_loss_rate_pct,
                    actual_value=loss_rate,
                    dry_run=dry_run,
                )
            )

    return alerts
```

## 5. Pipeline Integration Design

### 5.1 Orchestrator Wiring

#### Construction (in `Orchestrator.__init__()`):

```python
self.alert_engine = AlertEngine(config=self.config)
```

Placement: after `self.lifecycle_reporter` construction.

Dependencies: only `config`. No session factory, no client — `AlertEngine` is pure computation.

#### Invocation (in `Orchestrator._portfolio_aggregation_loop()`):

The existing loop body currently calls `compute_snapshot()` and `generate_report()` in independent try/except blocks. WI-25 adds a third block that invokes `AlertEngine.evaluate()` with both upstream results.

The updated loop body becomes:

```python
async def _portfolio_aggregation_loop(self) -> None:
    """Periodic portfolio snapshot, lifecycle report, and alert evaluation (WI-23/24/25)."""
    while True:
        await asyncio.sleep(
            float(self.config.portfolio_aggregation_interval_sec)
        )
        snapshot: PortfolioSnapshot | None = None
        report: LifecycleReport | None = None

        try:
            snapshot = await self.portfolio_aggregator.compute_snapshot()
        except Exception as exc:
            logger.error(
                "portfolio_aggregation_loop.error",
                error=str(exc),
            )

        try:
            report = await self.lifecycle_reporter.generate_report()
        except Exception as exc:
            logger.error(
                "lifecycle_report_loop.error",
                error=str(exc),
            )

        if snapshot is not None and report is not None:
            try:
                alerts = self.alert_engine.evaluate(snapshot, report)
                if alerts:
                    logger.warning(
                        "alert_engine.alerts_fired",
                        alert_count=len(alerts),
                        rules=[a.rule_name for a in alerts],
                        severities=[a.severity.value for a in alerts],
                        dry_run=snapshot.dry_run,
                    )
                else:
                    logger.info(
                        "alert_engine.all_clear",
                        dry_run=snapshot.dry_run,
                    )
            except Exception as exc:
                logger.error(
                    "alert_engine.error",
                    error=str(exc),
                )
```

Critical design decisions:
1. **Both inputs required:** `evaluate()` is only called when both `snapshot` and `report` are non-None. If either upstream computation fails, alert evaluation is skipped for that cycle.
2. **Fail-open:** Alert evaluation failure does NOT kill the loop. The `except Exception` block logs and continues.
3. **No new task or queue:** Alert evaluation happens synchronously within the existing `_portfolio_aggregation_loop()`.
4. **Snapshot capture:** The loop must capture the return values of `compute_snapshot()` and `generate_report()` into local variables (currently the return values are discarded).

### 5.2 No New Task Registration

`AlertEngine` does not introduce a new `asyncio.create_task()`. It is invoked synchronously within `_portfolio_aggregation_loop()`. The task list remains unchanged.

### 5.3 Orchestrator.shutdown() — No Change Required

No new task is created. Shutdown behavior is unmodified.

## 6. Failure Semantics (Fail-Open, Never Kill the Loop)

| Failure scenario | Behavior | Rationale |
|---|---|---|
| `compute_snapshot()` raises | `snapshot = None`. Alert evaluation skipped for this cycle. | Cannot evaluate portfolio rules without a snapshot. Next cycle will retry. |
| `generate_report()` raises | `report = None`. Alert evaluation skipped for this cycle. | Cannot evaluate lifecycle rules without a report. Next cycle will retry. |
| `evaluate()` raises unexpected `Exception` | Caught by `except Exception` in loop body. `alert_engine.error` logged. Loop continues. | Defensive catch-all. Alert failure must never kill the monitoring loop. |
| `snapshot.position_count == 0` | Stale-price rule skipped (division guard). Position-count rule: `0 > N` is always `False`. No alert. | Empty portfolio is healthy — no alerts needed. |
| `report.total_settled_count == 0` | Loss-rate rule skipped (division guard). No alert. | No history to evaluate — no alert needed. |
| All rules fire simultaneously | 4 `AlertEvent` objects returned. All logged via `alert_engine.alerts_fired`. | Rules are independent. Multiple concurrent alerts are valid. |
| No rules fire | Empty list returned. `alert_engine.all_clear` logged at INFO. | Normal healthy state. |

Critical rule: **`_portfolio_aggregation_loop()` must never re-raise an exception from `evaluate()`.** The `except Exception` block is not optional — it is a hard requirement.

## 7. dry_run Behavior

WI-25 introduces **no new dry_run gate**. The alert engine is inherently read-only — it emits in-memory `AlertEvent` objects and performs zero DB writes regardless of `dry_run`. The `dry_run` flag is read from `snapshot.dry_run` and propagated into each `AlertEvent` for downstream audit consumers only.

| Phase | dry_run=True | dry_run=False |
|---|---|---|
| `evaluate()` computation | Full rule evaluation | Full rule evaluation |
| `AlertEvent` construction | Includes `dry_run=True` field | Includes `dry_run=False` field |
| DB writes | **Zero** (component is read-only) | **Zero** (component is read-only) |
| Alert log event | Emitted | Emitted |

## 8. structlog Audit Events

### 8.1 New Events (WI-25)

| Event Key | Level | When | Key Fields |
|---|---|---|---|
| `alert_engine.alerts_fired` | `WARNING` | At least one alert rule fired | `alert_count`, `rules`, `severities`, `dry_run` |
| `alert_engine.all_clear` | `INFO` | Zero alert rules fired | `dry_run` |
| `alert_engine.error` | `ERROR` | `evaluate()` raised an unexpected exception | `error` |

### 8.2 Preserved Events (Unchanged)

All existing events from WI-23 (`portfolio.snapshot_computed`, `portfolio.price_fetch_failed`, `portfolio_aggregation_loop.error`), WI-24 (`lifecycle.report_generated`, `lifecycle.report_empty`, `lifecycle_report_loop.error`), and all other components are unaffected by WI-25. No events are removed.

## 9. Module Isolation Rules

### 9.1 AlertEngine Import Boundary

**Must NOT import:**
- `src/agents/context/` (prompt construction, context-building, `DataAggregator`)
- `src/agents/evaluation/` (`ClaudeClient`, `GrokClient`)
- `src/agents/ingestion/` (`CLOBWebSocketClient`, `GammaRESTClient`, `MarketDiscoveryEngine`)
- `src/schemas/llm.py` (`LLMEvaluationResponse`, `MarketContext`)
- `src/agents/execution/portfolio_aggregator.py` (`PortfolioAggregator`)
- `src/agents/execution/lifecycle_reporter.py` (`PositionLifecycleReporter`)
- `src/agents/execution/exit_strategy_engine.py` (`ExitStrategyEngine`)
- `src/agents/execution/exit_order_router.py` (`ExitOrderRouter`)
- `src/agents/execution/pnl_calculator.py` (`PnLCalculator`)
- `src/agents/execution/execution_router.py` (`ExecutionRouter`)
- `src/agents/execution/order_broadcaster.py` (`OrderBroadcaster`)
- `src/agents/execution/signer.py` (`TransactionSigner`)
- `src/agents/execution/polymarket_client.py` (`PolymarketClient`)
- `src/db/` (any repository or model)
- `sqlalchemy` (any module)

**Allowed imports:**
- `src/core/config` → `AppConfig`
- `src/schemas/risk` → `PortfolioSnapshot`, `LifecycleReport`, `AlertEvent`, `AlertSeverity`
- `structlog`, `decimal.Decimal`, `datetime`

### 9.2 risk.py Schema Additions

`AlertSeverity` and `AlertEvent` are added to `src/schemas/risk.py`. The existing import boundary for `risk.py` is unchanged — it remains a leaf schema module importing only `pydantic`, `decimal`, `datetime`, `typing`, and now `enum`.

## 10. Invariants Preserved

1. **Gatekeeper authority** — `LLMEvaluationResponse` remains the terminal pre-execution gate. `AlertEngine` operates downstream and is observational only. No bypass.
2. **Decimal financial integrity** — all threshold comparisons and ratio computations are `Decimal`. Float is rejected at Pydantic boundary. No float intermediary in any arithmetic step.
3. **Quarter-Kelly policy** — `AlertEngine` does not perform Kelly sizing. It reads pre-computed aggregate metrics.
4. **`dry_run=True` blocks DB writes** — `AlertEngine` performs zero DB writes regardless of `dry_run`. The flag is included in `AlertEvent` for audit logging only.
5. **Repository pattern** — `AlertEngine` does not access any repository or database session. It operates exclusively on in-memory Pydantic models.
6. **Read-only semantics** — `AlertEngine` never mutates any upstream state. It emits new `AlertEvent` objects and returns them. Zero side effects beyond structured logging in the orchestrator.
7. **No execution influence** — alerts are observational only. They do NOT halt execution, modify positions, trigger exits, or alter routing decisions.
8. **Async pipeline** — `AlertEngine.evaluate()` is synchronous but called from within the existing async `_portfolio_aggregation_loop()`. No new tasks, queues, or async primitives introduced.
9. **Entry-path routing** — `ExecutionRouter` internals are unmodified.
10. **Exit-path routing** — `ExitOrderRouter` and `ExitStrategyEngine` internals are unmodified.
11. **PnL settlement** — `PnLCalculator` internals are unmodified.
12. **Module isolation** — zero imports from prompt, context, evaluation, ingestion, or database modules.
13. **Shutdown sequence** — no new task to cancel. Shutdown behavior is unchanged.
14. **Queue topology unchanged** — `market_queue -> prompt_queue -> execution_queue`. No new queue.
15. **Division-by-zero safety** — stale-price and loss-rate rules are guarded by `> 0` denominator checks. Rule is skipped (not errored) when denominator is zero.

## 11. Strict Acceptance Criteria (Maker Agent)

1. `AlertEngine` exists in `src/agents/execution/alert_engine.py` as the canonical alert evaluation class.
2. `evaluate(snapshot: PortfolioSnapshot, report: LifecycleReport) -> list[AlertEvent]` is the sole public method.
3. `evaluate()` is synchronous (`def`, not `async def`) — it performs zero I/O.
4. `AlertSeverity` enum exists in `src/schemas/risk.py` with values `INFO`, `WARNING`, `CRITICAL`.
5. `AlertEvent` Pydantic model exists in `src/schemas/risk.py`, is frozen, Decimal-validated, with fields: `alert_at_utc`, `severity`, `rule_name`, `message`, `threshold_value`, `actual_value`, `dry_run`.
6. `AlertEvent` rejects `float` in `threshold_value` and `actual_value` at Pydantic boundary.
7. **Drawdown rule:** fires `CRITICAL` alert when `snapshot.total_unrealized_pnl < -(config.alert_drawdown_usdc)`. `rule_name="drawdown"`, `threshold_value=config.alert_drawdown_usdc`, `actual_value=snapshot.total_unrealized_pnl`.
8. **Stale-price rule:** fires `WARNING` alert when `snapshot.position_count > 0` AND `positions_with_stale_price / position_count > config.alert_stale_price_pct`. `rule_name="stale_price"`, `threshold_value=config.alert_stale_price_pct`, `actual_value=stale_ratio`.
9. **Stale-price division guard:** rule is skipped entirely when `snapshot.position_count == 0`. No alert emitted, no exception raised.
10. **Position-count rule:** fires `WARNING` alert when `snapshot.position_count > config.alert_max_open_positions`. `rule_name="max_positions"`, `threshold_value=Decimal(str(alert_max_open_positions))`, `actual_value=Decimal(str(position_count))`.
11. **Loss-rate rule:** fires `WARNING` alert when `report.total_settled_count > 0` AND `losing_count / total_settled_count > config.alert_loss_rate_pct`. `rule_name="loss_rate"`, `threshold_value=config.alert_loss_rate_pct`, `actual_value=loss_rate`.
12. **Loss-rate division guard:** rule is skipped entirely when `report.total_settled_count == 0`. No alert emitted, no exception raised.
13. All ratio arithmetic uses `Decimal(str(...))` conversion — no implicit float path.
14. `AlertEngine` constructor accepts only `config: AppConfig`. No DB session, no clients.
15. `AppConfig.alert_drawdown_usdc` exists as `Decimal` with default `Decimal("100")`.
16. `AppConfig.alert_stale_price_pct` exists as `Decimal` with default `Decimal("0.50")`.
17. `AppConfig.alert_max_open_positions` exists as `int` with default `20`.
18. `AppConfig.alert_loss_rate_pct` exists as `Decimal` with default `Decimal("0.60")`.
19. `AlertEngine` is constructed in `Orchestrator.__init__()` with `config=self.config`.
20. `_portfolio_aggregation_loop()` captures return values of `compute_snapshot()` and `generate_report()` into local variables.
21. `_portfolio_aggregation_loop()` calls `self.alert_engine.evaluate(snapshot, report)` only when both `snapshot` and `report` are non-None.
22. Alert evaluation failure in the loop is caught by `except Exception`, logged via `alert_engine.error`, and does NOT re-raise or terminate the loop.
23. When alerts fire: `alert_engine.alerts_fired` structlog event emitted at `WARNING` level with fields: `alert_count`, `rules`, `severities`, `dry_run`.
24. When no alerts fire: `alert_engine.all_clear` structlog event emitted at `INFO` level with field: `dry_run`.
25. `AlertEngine` has zero imports from prompt, context, evaluation, ingestion, or database modules.
26. `AlertEngine` performs zero DB reads and zero DB writes.
27. `AlertEngine` does not halt, pause, or modify execution in any way — alerts are observational only.
28. No modifications to `PortfolioAggregator`, `PositionLifecycleReporter`, `PnLCalculator`, `ExitStrategyEngine`, `ExitOrderRouter`, or `ExecutionRouter`.
29. No new database tables, migrations, or schema changes.
30. `dry_run` flag in `AlertEvent` is sourced from `snapshot.dry_run`.
31. All four rules are evaluated on every call — one rule firing does not short-circuit others. A single call may return 0 to 4 alerts.
32. Rules are evaluated in deterministic order: drawdown → stale_price → max_positions → loss_rate.
33. Full regression remains green (`pytest --asyncio-mode=auto tests/`) with coverage >= 80%.

## 12. Verification Checklist (Test Matrix)

### Unit Tests — Schema

1. Unit test: `AlertSeverity` enum has exactly three members: `INFO`, `WARNING`, `CRITICAL`.
2. Unit test: `AlertEvent` accepts `Decimal` in `threshold_value` and `actual_value` and is frozen.
3. Unit test: `AlertEvent` rejects `float` in `threshold_value` at Pydantic boundary.
4. Unit test: `AlertEvent` rejects `float` in `actual_value` at Pydantic boundary.
5. Unit test: `AlertEvent` accepts `AlertSeverity` enum values in `severity` field.

### Unit Tests — Drawdown Rule

6. Unit test: `evaluate()` fires `CRITICAL` drawdown alert when `total_unrealized_pnl` is below `-alert_drawdown_usdc`.
7. Unit test: `evaluate()` does NOT fire drawdown alert when `total_unrealized_pnl == -(alert_drawdown_usdc)` (boundary: exactly at threshold is not a breach).
8. Unit test: `evaluate()` does NOT fire drawdown alert when `total_unrealized_pnl > -(alert_drawdown_usdc)`.
9. Unit test: drawdown alert has `rule_name="drawdown"` and `severity=CRITICAL`.

### Unit Tests — Stale-Price Rule

10. Unit test: `evaluate()` fires `WARNING` stale-price alert when `stale_ratio > alert_stale_price_pct`.
11. Unit test: `evaluate()` does NOT fire stale-price alert when `stale_ratio == alert_stale_price_pct` (boundary: exactly at threshold is not a breach).
12. Unit test: `evaluate()` does NOT fire stale-price alert when `position_count == 0` (division guard).
13. Unit test: stale-price alert has `rule_name="stale_price"` and `severity=WARNING`.

### Unit Tests — Position-Count Rule

14. Unit test: `evaluate()` fires `WARNING` max-positions alert when `position_count > alert_max_open_positions`.
15. Unit test: `evaluate()` does NOT fire max-positions alert when `position_count == alert_max_open_positions` (boundary: exactly at threshold is not a breach).
16. Unit test: `evaluate()` does NOT fire max-positions alert when `position_count < alert_max_open_positions`.
17. Unit test: max-positions alert has `rule_name="max_positions"` and `severity=WARNING`.
18. Unit test: max-positions alert `threshold_value` and `actual_value` are `Decimal` (not `int`).

### Unit Tests — Loss-Rate Rule

19. Unit test: `evaluate()` fires `WARNING` loss-rate alert when `losing_count / total_settled_count > alert_loss_rate_pct`.
20. Unit test: `evaluate()` does NOT fire loss-rate alert when `loss_rate == alert_loss_rate_pct` (boundary: exactly at threshold is not a breach).
21. Unit test: `evaluate()` does NOT fire loss-rate alert when `total_settled_count == 0` (division guard).
22. Unit test: loss-rate alert has `rule_name="loss_rate"` and `severity=WARNING`.

### Unit Tests — Multi-Rule and Edge Cases

23. Unit test: `evaluate()` returns empty list when no rules fire (healthy portfolio).
24. Unit test: `evaluate()` returns multiple alerts when multiple rules fire simultaneously.
25. Unit test: `evaluate()` returns all 4 alerts when all thresholds are breached.
26. Unit test: `evaluate()` propagates `dry_run=True` from snapshot into all emitted alerts.
27. Unit test: `evaluate()` propagates `dry_run=False` from snapshot into all emitted alerts.

### Unit Tests — Config

28. Unit test: `AppConfig` accepts `alert_drawdown_usdc` as `Decimal` with default `Decimal("100")`.
29. Unit test: `AppConfig` accepts `alert_stale_price_pct` as `Decimal` with default `Decimal("0.50")`.
30. Unit test: `AppConfig` accepts `alert_max_open_positions` as `int` with default `20`.
31. Unit test: `AppConfig` accepts `alert_loss_rate_pct` as `Decimal` with default `Decimal("0.60")`.

### Integration Tests

32. Integration test: `AlertEngine` module has no dependency on prompt/context/evaluation/ingestion/database modules (import boundary check).
33. Integration test: `AlertEngine` is constructed in `Orchestrator.__init__()` — verify `self.alert_engine` attribute exists.
34. Integration test: `_portfolio_aggregation_loop()` calls `evaluate()` when both snapshot and report succeed — verify `alert_engine.alerts_fired` or `alert_engine.all_clear` log event is emitted.
35. Integration test: `_portfolio_aggregation_loop()` does NOT call `evaluate()` when `compute_snapshot()` fails — verify no alert log events.
36. Integration test: `_portfolio_aggregation_loop()` does NOT call `evaluate()` when `generate_report()` fails — verify no alert log events.
37. Integration test: `_portfolio_aggregation_loop()` catches `Exception` from `evaluate()` and does NOT re-raise — loop continues to next iteration.
38. Integration test: full `evaluate()` with realistic `PortfolioSnapshot` and `LifecycleReport` objects — alerts match expected rules.

### Regression Gate

39. Full suite: `pytest --asyncio-mode=auto tests/ -q` — all tests pass.
40. Coverage: `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` — >= 80%.
