# P32-WI-32 — Concurrent Multi-Market Tracking Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi32-concurrent-tracking` (branched from current `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/db-engineer.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-32 for Phase 10: a concurrency refactor that transforms the `Orchestrator` and `DataAggregator` from sequential single-market tracking into a concurrent multi-market fan-out pattern via `asyncio.gather`.

Today, the `Orchestrator` tracks one market at a time in a sequential loop — each market is discovered, subscribed to, aggregated, prompted, and evaluated before moving to the next:

```python
# CURRENT (sequential):
for market in markets:
    await self._track_single_market(market)
```

WI-32 introduces `asyncio.gather` for simultaneous WebSocket subscriptions across multiple markets:

```python
# TARGET (concurrent):
tasks = [
    self._data_aggregator.track_market(token_ids)
    for token_ids in token_ids_list
]
results = await asyncio.gather(*tasks, return_exceptions=True)
```

The `CLOBWebSocketClient` is extended with `subscribe_batch()` for multiplexed subscriptions via a single WebSocket connection. A single connection serves all markets — the fan-out is at the `DataAggregator` task level, not the connection level.

This WI is concurrency-focused and intentionally additive to the pipeline topology. The 4-layer architecture, queue topology, and Gatekeeper authority remain unchanged. `LLMEvaluationResponse` is still the terminal gate.

---

## Objective & Scope

### In Scope
1. Refactored `Orchestrator._market_tracking_loop()` — replaces sequential `_track_single_market()` with `asyncio.gather(*[DataAggregator.track_market(m) for m in markets], return_exceptions=True)`.
2. Refactored `DataAggregator.track_market(token_ids: list[str])` — accepts list of token IDs, manages per-market subscription state.
3. `CLOBWebSocketClient.subscribe_batch(assets_ids: list[str])` — multiplexed subscription via single WebSocket.
4. `CLOBWebSocketClient._handle_message()` — routes incoming frames to per-market `DataAggregator` instances via `asset_id` lookup.
5. New `AppConfig` fields: `max_concurrent_markets: int = 5`, `market_tracking_interval_sec: Decimal = Decimal("10")`.
6. `MarketTrackingTask` — new optional asyncio task in `Orchestrator` (config-gated, sleep-first, fail-open).
7. `PerMarketAggregatorState` frozen Pydantic schema in `src/schemas/market.py`.
8. structlog audit events: `market_tracking.fan_out`, `market_tracking.completed`, `market_tracking.gather_error`, `market_tracking.subscribed_batch`.

### Out of Scope
1. Multiple WebSocket connections — single connection handles all markets.
2. Dynamic market priority adjustment.
3. Market-specific prompt strategies (`PromptFactory` unchanged).
4. Load balancing or market selection heuristics.
5. Modifications to `ClaudeClient`, `LLMEvaluationResponse`, or Gatekeeper internals.
6. Changes to `prompt_queue` or `execution_queue` topology.
7. Per-market rate limiting or back-pressure handling.
8. Database schema changes — zero new tables, columns, or migrations.
9. Renaming canonical existing classes.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/prompts/business_logic_wi32.md`
4. `docs/PRD-v10.0.md` (WI-32 section)
5. `src/orchestrator.py` — **primary target: refactor _market_tracking_loop()**
6. `src/agents/context/aggregator.py` — **target: refactor track_market() signature**
7. `src/agents/ingestion/ws_client.py` — **target: add subscribe_batch() method**
8. `src/core/config.py` — **target: add max_concurrent_markets, market_tracking_interval_sec**
9. `src/schemas/market.py` — **NEW: PerMarketAggregatorState schema**
10. `docs/system_architecture.md` — **context: 4-layer pipeline, queue topology**
11. `docs/risk_management.md` — **context: Kelly formula, safety filters (unchanged)**
12. Existing test files (verify no regression):
    - `tests/unit/test_orchestrator.py`
    - `tests/unit/test_data_aggregator.py`
    - `tests/unit/test_ws_client.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL INVARIANT: asyncio.gather with return_exceptions=True

`asyncio.gather` **MUST** be called with `return_exceptions=True`. A single market failure must NOT crash the entire fan-out. Failed markets are logged via structlog and excluded from that cycle's output:

```python
results = await asyncio.gather(*tasks, return_exceptions=True)
for result in results:
    if isinstance(result, Exception):
        self.log.error("market_tracking.gather_error", error=str(result))
    else:
        self._process_market_contexts(result)
```

This is non-negotiable. Omitting `return_exceptions=True` is a bug.

---

## CRITICAL INVARIANT: Single WebSocket Connection

A **single** `CLOBWebSocketClient` connection serves **all** markets via `subscribe_batch()`. No new WebSocket connections are created per market. This is the core concurrency model:

```python
# ONE connection, MANY markets:
await ws_client.subscribe_batch(assets_ids=[token_id_1, token_id_2, ..., token_id_N])
```

Creating additional connections per market is a bug.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code. No implementation code can be written until the failing tests are committed and verified.

---

## Phase 1: Test Suite (RED Phase)

Create two new test files. All tests MUST fail (RED) before any production code is modified.

### Step 1.1 — Create `tests/unit/test_wi32_concurrent_tracking.py`

Write unit tests covering the following behaviors:

**A. `asyncio.gather` fan-out:**

1. **Concurrent tracking:** `asyncio.gather(*[mock_aggregator.track_market([tid]) for tid in ["t1", "t2", "t3"]], return_exceptions=True)` — assert 3 `MarketContext` outputs produced.
2. **Exception isolation:** one task raises `ValueError("market_error")`, other two succeed — assert `return_exceptions=True` prevents crash, failed market logged, 2 contexts processed.
3. **Empty markets:** `asyncio.gather(*[], return_exceptions=True)` — assert no error, zero contexts.
4. **All tasks fail:** all tasks raise exceptions — assert all logged, zero contexts processed.

**B. `subscribe_batch()` multiplexed subscription:**

5. `subscribe_batch(assets_ids=["t1", "t2", "t3"])` — assert single WebSocket send with multiplexed subscription message.
6. `subscribe_batch(assets_ids=[])` — assert empty subscription handled gracefully.
7. Verify subscription message contains correct `assets_ids` list and `event_types`.

**C. Frame routing via `asset_id`:**

8. `_handle_message()` with frame containing `asset_id="t1"` — assert routed to correct per-market aggregator.
9. `_handle_message()` with frame lacking `asset_id` — assert logged as `ws.frame_unrouted`.
10. `_handle_message()` with frame containing unknown `asset_id` — assert logged as `ws.frame_unrouted`.

**D. Market truncation:**

11. `max_concurrent_markets=3`, 5 markets discovered — assert first 3 tracked, 2 logged as `market_tracking.capped`.
12. `max_concurrent_markets=10`, 3 markets discovered — assert all 3 tracked, no capping.

**E. `PerMarketAggregatorState`:**

13. `PerMarketAggregatorState(token_ids=["t1", "t2"])` — assert correct initialization with `subscription_status="pending"`, `frame_count=0`.
14. State frozen: attempt to modify `token_ids` — assert `ValidationError`.

**F. `MarketTrackingTask` pattern:**

15. Config-gated: `enable_market_tracking=False` — assert task NOT created.
16. Config-gated: `enable_market_tracking=True` — assert task created with name `"MarketTrackingTask"`.
17. Sleep-first: verify `await asyncio.sleep()` at top of loop.
18. Fail-open: exception in discovery logged, loop continues.

**G. Decimal safety under concurrency:**

19. Concurrent `track_market()` calls produce `MarketContext` with `Decimal`-typed financial fields — assert no `float` introduced.
20. `PerMarketAggregatorState` serialization/deserialization preserves `Decimal` types.

### Step 1.2 — Create `tests/integration/test_wi32_concurrent_tracking_integration.py`

Write integration tests covering end-to-end concurrent tracking:

1. **Full fan-out cycle:** discover markets → `subscribe_batch()` → ingest frames → aggregate contexts → produce to `prompt_queue` — assert all markets tracked, contexts queued.
2. **Single WebSocket connection:** verify `ws_client` has exactly 1 active connection despite tracking 3 markets concurrently.
3. **Market failure isolation:** crash in one `DataAggregator` (mock raise) does not affect other 2 aggregators — assert 2 contexts produced, 1 error logged.
4. **Frame routing correctness:** send frames for 3 markets concurrently, assert each frame routed to correct aggregator with no cross-contamination.
5. **`dry_run=True` concurrent pipeline:** run full concurrent tracking with `dry_run=True`, assert no live WS connections, mock values used.
6. **Market truncation end-to-end:** discover 7 markets, `max_concurrent_markets=5` — assert 5 tracked, 2 deferred to next cycle.
7. **Concurrent queue production:** 3 aggregators produce to shared `prompt_queue` simultaneously — assert no queue corruption, all contexts consumable in order.

### Step 1.3 — Run RED gate

```bash
.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi32_concurrent_tracking.py tests/integration/test_wi32_concurrent_tracking_integration.py -v
```

**All new tests MUST fail.** Commit the failing test suite:

```
git add tests/unit/test_wi32_concurrent_tracking.py tests/integration/test_wi32_concurrent_tracking_integration.py
git commit -m "test(wi32): add RED test suite for concurrent multi-market tracking"
```

---

## Phase 2: Implementation (GREEN Phase)

Implement production code to make all RED tests pass. Execute steps in order.

### Step 2.1 — Add Config Fields

In `src/core/config.py`, add two new fields to `AppConfig`:

```python
max_concurrent_markets: int = Field(
    default=5,
    description="Maximum number of markets tracked concurrently",
)
market_tracking_interval_sec: Decimal = Field(
    default=Decimal("10"),
    description="Cadence for market discovery refresh",
)
enable_market_tracking: bool = Field(
    default=False,
    description="Enable MarketTrackingTask in Orchestrator",
)
```

### Step 2.2 — Create `PerMarketAggregatorState` Schema

Create `src/schemas/market.py`:

```python
"""Per-market aggregation state tracking."""

from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict


class PerMarketAggregatorState(BaseModel):
    """Tracks per-market subscription status, last-seen timestamp, and frame count."""
    model_config = ConfigDict(frozen=True)
    
    token_ids: list[str]
    subscription_status: str = "pending"
    last_seen_utc: datetime | None = None
    frame_count: int = 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
```

### Step 2.3 — Extend `CLOBWebSocketClient` with `subscribe_batch()`

In `src/agents/ingestion/ws_client.py`, add the `subscribe_batch()` method:

```python
async def subscribe_batch(self, assets_ids: list[str]) -> None:
    """Subscribe to multiple assets via a single WebSocket connection.
    
    Polymarket CLOB supports multiplexed subscriptions — one connection,
    many assets. This is the concurrency primitive for WI-32.
    """
    if not assets_ids:
        self.log.warning("ws.subscribe_batch_empty", message="No assets to subscribe to")
        return
    
    subscription_msg = json.dumps({
        "type": "subscribe",
        "assets_ids": assets_ids,
        "event_types": ["book", "price_change", "last_trade_price"],
    })
    
    await self.ws.send(subscription_msg)
    self.log.info(
        "market_tracking.subscribed_batch",
        asset_count=len(assets_ids),
        assets_ids=assets_ids,
    )
```

Enhance `_handle_message()` with `asset_id`-based frame routing:

```python
def _handle_message(self, message: str) -> None:
    """Route incoming WS frames to per-market aggregators via asset_id."""
    try:
        frame = json.loads(message)
    except json.JSONDecodeError:
        self.log.error("ws.frame_decode_error", message=message[:100])
        return
    
    # Handle PONG from server
    if message == "PONG":
        self.log.debug("ws.pong_received")
        return
    
    asset_id = frame.get("asset_id")
    
    if asset_id and asset_id in self._aggregator_map:
        self._aggregator_map[asset_id].process_frame(frame)
        self._aggregator_map[asset_id].frame_count += 1
        self._aggregator_map[asset_id].last_seen_utc = datetime.now(timezone.utc)
    else:
        self.log.warning(
            "ws.frame_unrouted",
            asset_id=asset_id,
            frame_type=frame.get("type", "unknown"),
        )
```

Add `_aggregator_map` to `__init__()`:

```python
self._aggregator_map: dict[str, DataAggregator] = {}
```

Add `register_aggregator()` method:

```python
def register_aggregator(self, asset_id: str, aggregator: DataAggregator) -> None:
    """Register a DataAggregator for frame routing."""
    self._aggregator_map[asset_id] = aggregator
```

### Step 2.4 — Refactor `DataAggregator.track_market()`

In `src/agents/context/aggregator.py`, refactor `track_market()`:

```python
async def track_market(self, token_ids: list[str]) -> list[MarketContext]:
    """Track a single market (now accepts list of token IDs for WI-32).
    
    Manages per-market subscription state via PerMarketAggregatorState.
    Produces MarketContext to shared prompt_queue.
    """
    state = PerMarketAggregatorState(token_ids=token_ids)
    
    # Register with WS client for frame routing
    for token_id in token_ids:
        self.ws_client.register_aggregator(token_id, self)
    
    # Subscribe via multiplexed batch
    await self.ws_client.subscribe_batch(token_ids)
    
    # Ingest frames and build context (existing logic, now concurrent-safe)
    market_contexts = []
    try:
        # ... existing frame ingestion logic ...
        context = self._build_market_context(token_ids, state)
        market_contexts.append(context)
        
        # Produce to shared prompt_queue
        await self.prompt_queue.put(context)
    except Exception as exc:
        self.log.error("aggregator.track_market_error", error=str(exc))
        raise
    
    return market_contexts
```

### Step 2.5 — Refactor `Orchestrator._market_tracking_loop()`

In `src/orchestrator.py`, replace sequential tracking with concurrent fan-out:

```python
async def _market_tracking_loop(self) -> None:
    """Concurrent multi-market tracking via asyncio.gather (WI-32)."""
    while self._running:
        await asyncio.sleep(float(self.config.market_tracking_interval_sec))
        try:
            # Discover markets
            snapshots = await self.discovery_engine.discover()
            if not snapshots:
                self.log.debug("market_tracking.no_markets_discovered")
                continue
            
            # Truncate to max_concurrent_markets
            if len(snapshots) > self.config.max_concurrent_markets:
                self.log.info(
                    "market_tracking.capped",
                    discovered=len(snapshots),
                    capped_to=self.config.max_concurrent_markets,
                )
                snapshots = snapshots[:self.config.max_concurrent_markets]
            
            # Group token IDs per market
            token_ids_list = [
                self._extract_token_ids(snapshot) for snapshot in snapshots
            ]
            
            # Fan-out via asyncio.gather
            self.log.info("market_tracking.fan_out", market_count=len(token_ids_list))
            tasks = [
                self._data_aggregator.track_market(token_ids)
                for token_ids in token_ids_list
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results
            success_count = 0
            error_count = 0
            for result in results:
                if isinstance(result, Exception):
                    self.log.error("market_tracking.gather_error", error=str(result))
                    error_count += 1
                else:
                    self._process_market_contexts(result)
                    success_count += 1
            
            self.log.info(
                "market_tracking.completed",
                success=success_count,
                errors=error_count,
                total=len(token_ids_list),
            )
        except Exception as exc:
            self.log.error("market_tracking_loop.error", error=str(exc))
            # Fail-open: loop continues on next interval
```

Add `MarketTrackingTask` registration in `Orchestrator.__init__()`:

```python
if self.config.enable_market_tracking:
    self.market_tracking_task = asyncio.create_task(
        self._market_tracking_loop(), name="MarketTrackingTask"
    )
```

Add task cancellation in `Orchestrator.shutdown()`:

```python
if hasattr(self, "market_tracking_task") and self.market_tracking_task:
    self.market_tracking_task.cancel()
    try:
        await self.market_tracking_task
    except asyncio.CancelledError:
        pass
```

### Step 2.6 — Add structlog Audit Events

Ensure all WI-32 events are logged:

| Event | When Fired | Key Fields |
|---|---|---|
| `market_tracking.fan_out` | Before `asyncio.gather` | `market_count` |
| `market_tracking.completed` | After gather results | `success`, `errors`, `total` |
| `market_tracking.gather_error` | Per-market exception | `error` |
| `market_tracking.subscribed_batch` | After subscribe_batch send | `asset_count`, `assets_ids` |
| `market_tracking.capped` | Excess markets truncated | `discovered`, `capped_to` |
| `ws.frame_unrouted` | Frame with no matching aggregator | `asset_id`, `frame_type` |
| `market_tracking_loop.error` | Top-level exception | `error` |

### Step 2.7 — Run GREEN gate

```bash
.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi32_concurrent_tracking.py tests/integration/test_wi32_concurrent_tracking_integration.py -v
```

**All new WI-32 tests MUST pass.** Commit the implementation:

```
git add src/schemas/market.py src/core/config.py src/agents/ingestion/ws_client.py src/agents/context/aggregator.py src/orchestrator.py
git commit -m "feat(wi32): implement concurrent multi-market tracking via asyncio.gather"
```

---

## Phase 3: Refactor & Regression

### Step 3.1 — Full regression

```bash
.venv/bin/pytest --asyncio-mode=auto tests/ -q
```

**ALL tests must pass** (target: 593 + new WI-32 tests). Fix any regressions before proceeding. Do not suppress or skip pre-existing tests.

### Step 3.2 — Coverage verification

```bash
.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m
```

Coverage MUST remain at or above **95%**. If coverage drops, add targeted tests for uncovered lines before proceeding.

### Step 3.3 — Regression commit

If any fixes were needed in Phase 3, commit them atomically:

```
git commit -m "fix(wi32): address regression findings from full test suite"
```

---

## Regression Gate Summary

| Gate | Command | Pass Criteria |
|---|---|---|
| RED | `.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi32_concurrent_tracking.py tests/integration/test_wi32_concurrent_tracking_integration.py -v` | All new tests FAIL |
| GREEN | Same command | All new tests PASS |
| Regression | `.venv/bin/pytest --asyncio-mode=auto tests/ -q` | ALL tests pass (593 + WI-32 additions) |
| Coverage | `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` | >= 95% |

---

## Definition of Done

Before declaring WI-32 complete:

1. All new WI-32 unit and integration tests pass GREEN.
2. Full regression suite passes with zero failures.
3. Coverage >= 95%.
4. `STATE.md` updated: test count, coverage, WI-32 marked complete.
5. `CLAUDE.md` updated: active WI status.
6. `README.md` updated if any new environment variables or commands are introduced.
7. Memory Consolidation executed.
8. `docs/prompts/business_logic_wi32.md` created and reviewed.

---

## Files Modified (Summary)

| File | Change |
|---|---|
| `src/schemas/market.py` | **NEW** — `PerMarketAggregatorState` frozen Pydantic schema |
| `src/core/config.py` | Add `max_concurrent_markets`, `market_tracking_interval_sec`, `enable_market_tracking` |
| `src/agents/ingestion/ws_client.py` | Add `subscribe_batch()`, `register_aggregator()`, enhance `_handle_message()` with `asset_id` routing |
| `src/agents/context/aggregator.py` | Refactor `track_market()` to accept `list[str]`, manage per-market state |
| `src/orchestrator.py` | Refactor `_market_tracking_loop()` with `asyncio.gather`, add `MarketTrackingTask` |
| `tests/unit/test_wi32_concurrent_tracking.py` | **NEW** — ~20 unit tests |
| `tests/integration/test_wi32_concurrent_tracking_integration.py` | **NEW** — ~7 integration tests |

## Files NOT Modified

| File | Reason |
|---|---|
| `src/agents/evaluation/claude_client.py` | Gatekeeper evaluation unchanged |
| `src/agents/context/prompt_factory.py` | Prompt strategies unchanged per WI-12 |
| `src/schemas/llm.py` | LLM schemas unchanged |
| `src/schemas/risk.py` | Risk schemas unchanged |
| `src/schemas/execution.py` | Execution schemas unchanged |
| `src/schemas/position.py` | Position schemas unchanged |
| `src/db/models.py` | Zero DB schema changes |
| `src/db/repositories/position_repository.py` | Repository unchanged |
| `src/db/repositories/market_repository.py` | Repository unchanged |
| `migrations/` | Zero migrations introduced |
| `src/agents/execution/execution_router.py` | BUY routing unchanged |
| `src/agents/execution/exit_order_router.py` | SELL routing unchanged |
| `src/agents/execution/pnl_calculator.py` | Settlement unchanged |
| `src/agents/execution/circuit_breaker.py` | Entry gate unchanged |
| `src/agents/execution/alert_engine.py` | Alert thresholds unchanged |
| `src/agents/execution/position_tracker.py` | Position tracking unchanged |
| `src/agents/execution/portfolio_aggregator.py` | Portfolio aggregation unchanged |
| `src/agents/execution/lifecycle_reporter.py` | Lifecycle reporting unchanged |
| `src/agents/execution/telegram_notifier.py` | Telegram unchanged |
| `src/agents/execution/gas_estimator.py` | Gas estimation unchanged |
| `src/agents/execution/exposure_validator.py` | Exposure validation unchanged |
