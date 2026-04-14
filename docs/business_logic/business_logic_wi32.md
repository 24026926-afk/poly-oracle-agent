# WI-32 Business Logic — Concurrent Multi-Market Tracking

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — WI-32 introduces `asyncio.gather` fan-out across multiple `DataAggregator` instances, a single multiplexed WebSocket subscription, and a new optional `MarketTrackingTask` in the `Orchestrator`. All inter-layer communication remains via `asyncio.Queue` — no direct method calls between layers. The `return_exceptions=True` flag on `gather` is non-negotiable for fault isolation.
- `.agents/rules/db-engineer.md` — WI-32 introduces NO new database tables, columns, or migrations. All existing persistence paths (`PositionRepository`, `MarketRepository`) remain unchanged. The `asyncio.Queue` for SQLite serialization (if introduced for concurrent write protection) must follow repository isolation rules — no direct `AsyncSession` calls outside `src/db/repositories/`.
- `.agents/rules/risk-auditor.md` — All per-market aggregation remains `Decimal`-safe. Concurrent execution must NOT introduce `float` via race conditions or shared state mutation. Kelly sizing, EV formulas, and Gatekeeper thresholds are unchanged per market.
- `.agents/rules/test-engineer.md` — WI-32 requires unit + integration coverage for multiplexed WS routing, `asyncio.gather` fan-out behavior, per-market state tracking, and concurrent queue production. Coverage target remains ≥ 80%.

## 1. Objective

Refactor the `Orchestrator` and `DataAggregator` to use `asyncio.gather` for simultaneous WebSocket subscriptions across multiple markets. The current architecture tracks one market at a time in a sequential loop — each market is discovered, subscribed to, aggregated, prompted, and evaluated before moving to the next.

WI-32 introduces a fan-out pattern: the `Orchestrator` discovers all eligible markets via `MarketDiscoveryEngine`, then constructs a `DataAggregator` task per market and runs them concurrently via `asyncio.gather`. Each task independently ingests WebSocket frames, builds context, and queues evaluation prompts. The result is N-fold throughput improvement where N is the number of concurrently tracked markets.

The `CLOBWebSocketClient` is refactored to support multiple `assets_ids` in a single subscription (Polymarket CLOB supports multiplexed subscriptions per WebSocket connection). A single WebSocket connection serves all markets — the fan-out is at the `DataAggregator` task level, not the connection level.

## 2. Scope Boundaries

### In Scope

1. Refactored `Orchestrator._market_tracking_loop()` — replaces sequential `_track_single_market()` with `asyncio.gather(*[DataAggregator.track_market(m) for m in markets], return_exceptions=True)`.
2. Refactored `DataAggregator` — `track_market(token_ids: list[str]) -> list[MarketContext]` now accepts a list of token IDs and manages per-market subscription state.
3. `CLOBWebSocketClient` extended with `subscribe_batch(assets_ids: list[str]) -> None` — multiplexed subscription via single WebSocket.
4. `CLOBWebSocketClient._handle_message()` enhanced to route incoming frames to per-market `DataAggregator` instances via `asset_id` lookup.
5. New `AppConfig` fields:
   - `max_concurrent_markets: int` (default `5`)
   - `market_tracking_interval_sec: Decimal` (default `Decimal("10")`)
6. `MarketTrackingTask` — new optional asyncio task in `Orchestrator` managing the concurrent fan-out loop.
7. `PerMarketAggregatorState` frozen Pydantic schema in `src/schemas/market.py` tracking per-market subscription status, last-seen timestamp, and frame count.
8. structlog audit events: `market_tracking.fan_out`, `market_tracking.completed`, `market_tracking.gather_error`, `market_tracking.subscribed_batch`.

### Out of Scope

1. Multiple WebSocket connections — single connection handles all markets via multiplexed subscriptions.
2. Dynamic market priority adjustment — all discovered markets are treated equally.
3. Market-specific prompt strategies — `PromptFactory` is unchanged per WI-12.
4. Load balancing or market selection heuristics — `MarketDiscoveryEngine` selection logic is unchanged.
5. Modifications to `ClaudeClient`, `LLMEvaluationResponse`, or Gatekeeper internals.
6. Changes to `prompt_queue` or `execution_queue` topology — queues remain single shared channels.
7. Per-market rate limiting or back-pressure handling — deferred to future phase.
8. Database schema changes — WI-32 introduces zero new tables, columns, or migrations.
9. Renaming or repurposing canonical existing classes (`CLOBWebSocketClient`, `DataAggregator`, `Orchestrator`).

## 3. Target Components + Data Contracts

### 3.1 Primary Target Components

#### A. `src/orchestrator.py` — Market Tracking Loop

The `Orchestrator` gains a new `_market_tracking_loop()` method that replaces the sequential per-market tracking pattern:

```python
async def _market_tracking_loop(self) -> None:
    while self._running:
        await asyncio.sleep(float(self.config.market_tracking_interval_sec))
        try:
            snapshots = await self.discovery_engine.discover()
            markets = self._truncate_markets(snapshots, self.config.max_concurrent_markets)
            token_ids_list = self._group_token_ids(markets)
            
            tasks = [
                self._data_aggregator.track_market(token_ids)
                for token_ids in token_ids_list
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self._process_gather_results(results)
        except Exception as exc:
            self.log.error("market_tracking_loop.error", error=str(exc))
```

Required behavior:

1. Sleep-first cadence: `await asyncio.sleep(...)` at top of loop.
2. Market discovery via `MarketDiscoveryEngine.discover()` each cycle.
3. Truncate to `max_concurrent_markets` — excess logged as `market_tracking.capped`.
4. `asyncio.gather(*tasks, return_exceptions=True)` — single market failure does not crash others.
5. Failed markets logged via `market_tracking.gather_error`, excluded from output.
6. `MarketTrackingTask` follows optional task pattern: config-gated construction, sleep-first, fail-open.

#### B. `src/agents/context/aggregator.py` — DataAggregator

`DataAggregator` is refactored so `track_market()` accepts a list of token IDs and manages per-market subscription state:

```python
async def track_market(self, token_ids: list[str]) -> list[MarketContext]:
    state = PerMarketAggregatorState(token_ids=token_ids)
    await self.ws_client.subscribe_batch(token_ids)
    # Ingest frames, build context, produce to prompt_queue
    ...
    return market_contexts
```

Required behavior:

1. `track_market(token_ids: list[str])` accepts multiple token IDs per market.
2. Per-market state tracked via `PerMarketAggregatorState`.
3. Output remains `MarketContext` — unchanged schema from WI-3.
4. All per-market aggregation is `Decimal`-safe — no float introduced by concurrent execution.
5. Produces to shared `prompt_queue` — no per-market queues.

#### C. `src/agents/ingestion/ws_client.py` — CLOBWebSocketClient

`CLOBWebSocketClient` gains a `subscribe_batch()` method for multiplexed subscriptions:

```python
async def subscribe_batch(self, assets_ids: list[str]) -> None:
    """Subscribe to multiple assets via a single WebSocket connection."""
    subscription_msg = self._build_multiplexed_subscription(assets_ids)
    await self.ws.send(subscription_msg)
    self.log.info("market_tracking.subscribed_batch", asset_count=len(assets_ids))
```

Required behavior:

1. Single WebSocket connection serves all markets — no new connections per market.
2. `subscribe_batch(assets_ids: list[str])` sends multiplexed subscription message.
3. `_handle_message()` routes incoming frames to per-market aggregators via `asset_id` lookup.
4. Frames without matching `asset_id` logged as `ws.frame_unrouted` and discarded.
5. `CLOBWebSocketClient` class name is preserved (per AGENTS.md mandatory class reference).

### 3.2 Supporting Model/Schema Changes

The following supporting changes are required for the target components above to function correctly:

1. `src/schemas/market.py` (NEW)
   - Add `PerMarketAggregatorState` frozen Pydantic schema:
     ```python
     class PerMarketAggregatorState(BaseModel):
         model_config = ConfigDict(frozen=True)
         token_ids: list[str]
         subscription_status: str = "pending"
         last_seen_utc: datetime | None = None
         frame_count: int = 0
     ```

2. `src/core/config.py`
   - Add `max_concurrent_markets: int = 5`
   - Add `market_tracking_interval_sec: Decimal = Decimal("10")`

3. `src/orchestrator.py`
   - Add `_market_tracking_loop()` method
   - Add `MarketTrackingTask` registration:
     ```python
     if self.config.enable_market_tracking:
         self.market_tracking_task = asyncio.create_task(
             self._market_tracking_loop(), name="MarketTrackingTask"
         )
     ```

4. `src/agents/context/aggregator.py`
   - Refactor `track_market()` to accept `list[str]` instead of single token ID
   - Add per-market state management via `PerMarketAggregatorState`

5. `src/agents/ingestion/ws_client.py`
   - Add `subscribe_batch(assets_ids: list[str])` method
   - Enhance `_handle_message()` with `asset_id`-based frame routing

## 4. Core Logic

### 4.1 Canonical Fan-Out Pattern

WI-32 replaces sequential market tracking with concurrent fan-out:

```python
# BEFORE (sequential):
for market in markets:
    await self._track_single_market(market)

# AFTER (concurrent):
tasks = [
    self._data_aggregator.track_market(token_ids)
    for token_ids in token_ids_list
]
results = await asyncio.gather(*tasks, return_exceptions=True)
for result in results:
    if isinstance(result, Exception):
        self.log.error("market_tracking.gather_error", error=str(result))
    else:
        self._process_market_contexts(result)
```

### 4.2 Multiplexed WebSocket Subscription

Polymarket CLOB supports subscribing to multiple assets via a single WebSocket connection:

```python
def _build_multiplexed_subscription(self, assets_ids: list[str]) -> str:
    return json.dumps({
        "type": "subscribe",
        "assets_ids": assets_ids,
        "event_types": ["book", "price_change", "last_trade_price"]
    })
```

### 4.3 Frame Routing

Incoming WebSocket frames are routed to per-market aggregators via `asset_id`:

```python
def _handle_message(self, message: str) -> None:
    frame = json.loads(message)
    asset_id = frame.get("asset_id")
    if asset_id in self._aggregator_map:
        self._aggregator_map[asset_id].process_frame(frame)
    else:
        self.log.warning("ws.frame_unrouted", asset_id=asset_id)
```

### 4.4 Market Truncation

When `MarketDiscoveryEngine` returns more markets than `max_concurrent_markets`:

```python
def _truncate_markets(self, snapshots: list[MarketSnapshot], max_markets: int) -> list[MarketSnapshot]:
    if len(snapshots) > max_markets:
        self.log.info("market_tracking.capped", 
                      discovered=len(snapshots), 
                      capped_to=max_markets)
    return snapshots[:max_markets]
```

## 5. Invariants

1. **`asyncio.gather` with `return_exceptions=True`**
   A single market failure does not crash the entire fan-out. Failed markets are logged via structlog and excluded from that cycle's output.

2. **Single WebSocket connection**
   A single `CLOBWebSocketClient` connection serves all markets via multiplexed `subscribe_batch()`. No new WebSocket connections are created per market.

3. **Shared prompt queue**
   The `prompt_queue` remains a single shared channel — all `DataAggregator` instances produce into the same queue. No per-market queues are introduced.

4. **Market cap enforcement**
   `max_concurrent_markets` caps the number of simultaneous markets. Excess markets from `MarketDiscoveryEngine` are logged as `market_tracking.capped` and deferred to next discovery cycle.

5. **Decimal safety under concurrency**
   All per-market aggregation is `Decimal`-safe — no float introduced by concurrent execution.

6. **Unchanged MarketContext output**
   `DataAggregator` remains the canonical context builder — `track_market()` output is `MarketContext`, unchanged from WI-3.

7. **Class name preservation**
   `CLOBWebSocketClient` class name is preserved (per AGENTS.md mandatory class reference). Only `subscribe_batch()` is added.

8. **Optional task pattern**
   `MarketTrackingTask` follows the same optional task pattern as `PortfolioAggregatorTask` (WI-23) — config-gated construction, sleep-first cadence, fail-open loop.

9. **Frame routing correctness**
   Frame routing in `_handle_message()` uses `asset_id` to dispatch to the correct per-market aggregator. Frames without a matching `asset_id` are logged as `ws.frame_unrouted` and discarded.

10. **Gatekeeper authority unchanged**
    `LLMEvaluationResponse` Gatekeeper authority is unaffected. The fan-out accelerates context production but does not alter Gatekeeper validation.

11. **No database changes**
    WI-32 introduces zero new tables, columns, or migrations. All existing persistence paths remain unchanged.

12. **No queue topology changes**
    `market_queue → prompt_queue → execution_queue` remains unchanged. Concurrent execution produces into existing queues.

## 6. Acceptance Criteria

1. `Orchestrator._market_tracking_loop()` uses `asyncio.gather(*[DataAggregator.track_market(m) for m in markets], return_exceptions=True)`.
2. `DataAggregator.track_market(token_ids: list[str])` manages per-market subscription state and produces `MarketContext`.
3. `CLOBWebSocketClient.subscribe_batch(assets_ids: list[str])` sends a multiplexed subscription for all assets in a single WS call.
4. `CLOBWebSocketClient._handle_message()` routes frames to per-market aggregators via `asset_id` lookup.
5. `max_concurrent_markets` caps simultaneous markets at configured limit (default 5).
6. Excess markets are logged as `market_tracking.capped` and deferred.
7. Single market failure within `asyncio.gather` does not crash other markets — failed market logged via `market_tracking.gather_error`.
8. `prompt_queue` remains a single shared channel — no per-market queues.
9. `PerMarketAggregatorState` tracks per-market subscription status, last-seen timestamp, and frame count.
10. `AppConfig.max_concurrent_markets` is `int` with default `5`.
11. `AppConfig.market_tracking_interval_sec` is `Decimal` with default `Decimal("10")`.
12. `MarketTrackingTask` follows optional task pattern (config-gated, sleep-first, fail-open).
13. `CLOBWebSocketClient` class name is preserved — only `subscribe_batch()` method added.
14. `asyncio.gather` uses `return_exceptions=True`.
15. Full regression remains green with coverage >= 80%.
16. Zero database migrations introduced.
17. Zero new queue topologies introduced.

## 7. Test Plan

### Unit Tests

1. `asyncio.gather` fan-out: concurrent `track_market()` calls produce correct number of `MarketContext` outputs.
2. `return_exceptions=True`: single market failure does not crash other markets in gather.
3. `subscribe_batch()` sends correct multiplexed subscription message.
4. `_handle_message()` routes frames to correct per-market aggregator via `asset_id`.
5. Unroutable frames logged as `ws.frame_unrouted`.
6. `max_concurrent_markets` truncation: excess markets logged and deferred.
7. `PerMarketAggregatorState` tracks subscription status, last-seen, frame count correctly.
8. `prompt_queue` receives contexts from all concurrent aggregators.
9. `Decimal` safety: concurrent execution does not introduce `float` in money paths.
10. `MarketTrackingTask` follows sleep-first, fail-open pattern.
11. Config-gated task construction: task not created when `enable_market_tracking=False`.
12. Empty market discovery: loop handles zero markets without error.

### Integration Tests

1. Full fan-out cycle: discover → subscribe → ingest → aggregate → produce to `prompt_queue`.
2. Multiple markets tracked concurrently: verify N-fold throughput improvement.
3. Single WebSocket connection serves all markets (verify no additional connections).
4. Market failure isolation: crash in one aggregator does not affect others.
5. Frame routing correctness: frames routed to correct aggregators, no cross-contamination.
6. `dry_run=True` runs full concurrent pipeline without live WS connections.
7. Regression: full test suite passes without WI-32-induced failures.

## 8. Non-Negotiable Design Decision

WI-32 uses a **single WebSocket connection with multiplexed subscriptions** to serve all concurrently tracked markets. The fan-out is at the `DataAggregator` task level via `asyncio.gather`, not at the connection level. This is the core business rule:

```python
# ONE connection, MANY markets:
await ws_client.subscribe_batch(assets_ids=[token_id_1, token_id_2, ..., token_id_N])
results = await asyncio.gather(*[aggregator.track_market(token_ids) for token_ids in token_ids_list], return_exceptions=True)
```

The `prompt_queue` remains the **single shared channel** — all aggregators produce into it. No per-market queues are introduced. The Gatekeeper consumes from the same queue, unaware of the fan-out upstream.

This design preserves the 4-layer pipeline semantics while delivering N-fold throughput improvement for concurrent market tracking.
