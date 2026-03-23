# STATE.md — Poly-Oracle-Agent Project State

**Last Updated:** 2026-03-23
**Version:** 0.2.0
**Status:** Phase 2 Complete — All 8 Work Items Delivered (92 tests, 91% coverage)

# ⚙️ Phase 3 Evaluation Gate
**Status:** 🔴 IN PROGRESS — v0.3.0

### WI-09 — Repository Wiring
- [ ] `grep -r "session.add\|session.execute\|session.flush\|session.scalar" src/agents/`
      → zero results outside `src/db/`
- [ ] `pytest --asyncio-mode=auto tests/` → all 92 pass, no regressions
- [ ] `coverage report` → ≥ 80%
- [ ] Bypass regression test EXISTS and FAILS when a direct session call is injected

### WI-10 — README
- [ ] Clean-room validation completed (fresh venv, follow README only)
- [ ] All 6 commands in Command Validation Checklist produce expected output signals
- [ ] README is internally consistent with STATE.md, .env.example, pyproject.toml

### Phase Gate (both WIs required)
- [ ] Both PRs merged to `develop`
- [ ] Final PR `develop → master` approved
- [ ] STATE.md updated: version `0.3.0`, status `Phase 3 Complete`
- [ ] `docs/prompts/` has P9 + P10 archived
---
When the gate is fully green → flip:

text
**Status:** 🟢 COMPLETE — v0.3.0
**Last Updated:** [date]
Execution Order From Here
text
Now:   Codex CLI → create both branches
Then:  Claude Code Session B → WI-10 (README, fast, low risk)
Then:  Claude Code Session A → WI-09 (repo wiring, higher risk)
Then:  Reflection Pass on each (Codex Chat Panel)
Then:  Run Evaluation Gate checklist
Then:  Merge WI-10 → develop → PR
       Merge WI-09 → develop → PR
       develop → master
Then:  STATE.md → v0.3.0, Phase 3 Complete


---


## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Technology Stack](#2-technology-stack)
3. [Architecture Summary](#3-architecture-summary)
4. [Implementation Status by Layer](#4-implementation-status-by-layer)
5. [Schemas (Pydantic V2)](#5-schemas-pydantic-v2)
6. [Database Layer](#6-database-layer)
7. [Core Infrastructure](#7-core-infrastructure)
8. [Documentation](#8-documentation)
9. [Test Coverage](#9-test-coverage)
10. [Scripts & Utilities](#10-scripts--utilities)
11. [Configuration & Environment](#11-configuration--environment)
12. [Known Gaps & Stubs](#12-known-gaps--stubs)
13. [Current State Summary](#13-current-state-summary)

---

## 1. Project Overview

**Poly-Oracle-Agent** is an autonomous AI-powered trading agent for [Polymarket](https://polymarket.com), a prediction market platform. The system streams live orderbook data via WebSocket, aggregates context, evaluates trading opportunities using Claude (Anthropic LLM) with structured Chain-of-Thought reasoning, and executes EIP-712 signed orders on the Polymarket CLOB (Central Limit Order Book) with on-chain settlement on Polygon PoS.

The agent operates as a fully async (`asyncio`) pipeline with four isolated processing layers connected by `asyncio.Queue` bridges, ensuring clean decoupling and concurrent execution.

---

## 2. Technology Stack

| Category | Technology | Version / Notes |
|---|---|---|
| Language | Python | 3.12+ |
| Concurrency | `asyncio` | All I/O is non-blocking |
| Data Validation | Pydantic V2 | `pydantic>=2.5.0`, `pydantic-settings>=2.1.0` |
| Database | SQLAlchemy 2.0 (Async) | `sqlalchemy>=2.0.0` with `aiosqlite>=0.19.0` |
| Blockchain | `web3.py` | `web3>=6.15.0` (Polygon PoS, EIP-712 signing) |
| LLM | Anthropic Claude | `anthropic>=0.19.0` (Claude 3.5 Sonnet) |
| WebSocket | `websockets` | `websockets>=12.0` |
| HTTP Client | `httpx` | `httpx>=0.27.0` (for async REST calls) |
| Logging | `structlog` | `structlog>=24.1.0` (structured JSON/console output) |
| Config | `python-dotenv` | `python-dotenv>=1.0.1` |
| Build | `setuptools` | PEP 621 via `pyproject.toml` |

---

## 3. Architecture Summary

The system follows a **4-layer pipeline architecture**, each layer running as a concurrent `asyncio.Task` inside a single event loop:

```
Layer 1: Ingestion → Layer 2: Context → Layer 3: Evaluation → Layer 4: Execution
     ↓ Queue           ↓ Queue             ↓ Queue
  (MarketSnapshot)  (Prompt+State)     (SignedDecision)
```

### Data Flow

1. **Market Ingestion Engine** — Streams CLOB WebSocket frames + Gamma REST metadata
2. **Context Builder** — Aggregates orderbook state, applies time/volatility triggers, builds CoT prompts
3. **LLM Evaluation Node** — Queries Claude, validates structured JSON output via Pydantic Gatekeeper, persists audit trail
4. **Web3 Execution Node** — Signs EIP-712 orders, manages nonces, estimates gas, broadcasts to CLOB, polls for receipts

All inter-layer communication is via `asyncio.Queue` instances. Every layer persists its output to SQLite via SQLAlchemy Async.

---

## 4. Implementation Status by Layer

### Layer 1 — Market Ingestion Engine ✅ IMPLEMENTED

#### `src/agents/ingestion/ws_client.py` — `CLOBWebSocketClient`
- Connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Subscribes to market channels
- Sends periodic heartbeat pings every 10 seconds
- Validates incoming frames via `MarketSnapshotSchema` (Pydantic V2)
- Filters for valid event types: `book`, `price_change`, `last_trade_price`
- Persists validated snapshots to `market_snapshots` table via async SQLAlchemy session
- Enqueues `MarketSnapshot` ORM objects for downstream consumption
- Implements exponential backoff reconnection (1s → 60s max)
- Handles invalid JSON, validation errors, and connection drops gracefully

#### `src/agents/ingestion/rest_client.py` — `GammaRESTClient`
- Fetches market metadata from Gamma API (`https://gamma-api.polymarket.com`)
- Uses `httpx.AsyncClient` exclusively (migrated from `aiohttp` in WI-06)
- `get_active_markets()` — Returns all active, non-closed markets
- `get_market_by_condition_id()` — Single market lookup with 404 handling
- In-memory cache with 60-second TTL for active markets
- Graceful degradation: returns stale cache on API failure
- Validates responses via `MarketMetadata` Pydantic model
- Custom `RESTClientError` for 5xx server errors

#### `src/agents/ingestion/market_discovery.py` — `MarketDiscoveryEngine` ✅ NEW (WI-03)
- Autonomous market discovery using Gamma API and exposure-based filtering
- `discover()` — Returns eligible `condition_id` list (best candidates first), never hardcoded
- Sequential filter chain applied to `GammaRESTClient.get_active_markets()` output:
  1. **Metadata presence**: `condition_id` non-empty and `token_ids` non-empty
  2. **Time-to-resolution**: `hours_to_resolution >= config.min_ttr_hours` (4.0h); markets with no/unparseable `end_date_iso` excluded
  3. **Exposure limits**: current exposure < `max_exposure_pct × bankroll` (Decimal math via `BankrollPortfolioTracker`)
- Logs filter stats (`total`, `no_metadata`, `ttr_fail`, `exposure_fail`) on every discovery cycle
- Returns `[]` with `logger.warning` when no market is eligible — never falls back to hardcoded ID
- All monetary comparisons use `Decimal` — no `float` for exposure checks

---

### Layer 2 — Context Builder ✅ IMPLEMENTED

#### `src/agents/context/aggregator.py` — `DataAggregator`
- Maintains in-memory orderbook state (best bid/ask)
- **Filters incoming messages by `condition_id`** — silently discards messages for other markets (WI-03)
- Dual-trigger emission system:
  - **Time trigger**: Emits market state every 10 seconds
  - **Volatility trigger**: Emits immediately on >2% midpoint change
- Background timer loop runs independently to enforce time-based triggers even during message silence
- Builds market state dictionary with: `condition_id`, `best_bid`, `best_ask`, `midpoint`, `spread`, `timestamp`
- Generates unique `snapshot_id` (UUID4) per emission
- Passes state through `PromptFactory` before enqueuing

#### `src/agents/context/prompt_factory.py` — `PromptFactory`
- Constructs structured Chain-of-Thought evaluation prompts for Claude
- Injects live market data (condition ID, bid, ask, midpoint, spread, timestamp)
- Embeds the `LLMEvaluationResponse` JSON schema directly into the prompt to enforce strict output format
- Instructs the LLM to:
  1. Analyze market parameters
  2. Estimate true probability
  3. Calculate Expected Value
  4. Apply safety filters (EV > 2%, Spread < 1.5%, Confidence ≥ 75%)
  5. Output reasoning and final decision as raw JSON

---

### Layer 3 — LLM Evaluation Node ✅ IMPLEMENTED

#### `src/agents/evaluation/claude_client.py` — `ClaudeClient`
- Async Anthropic client using `AsyncAnthropic` SDK
- Consumes prompts from input queue, processes evaluations, routes decisions
- **Retry mechanism**: Up to 2 retries on JSON validation failures, re-prompting Claude with specific Pydantic errors
- **JSON extraction**: Handles both raw JSON and markdown-wrapped JSON responses (````json ... ```)
- **Gatekeeper enforcement**: All responses validated through `LLMEvaluationResponse` Pydantic model
- **Persistence**: Full audit trail saved to `agent_decision_logs` table including:
  - Structured fields (confidence, EV, decision boolean, action)
  - Raw Chain-of-Thought reasoning text
  - Token usage (input/output)
  - Model ID and prompt version
- **Routing**: Approved trades (`decision_boolean=True`) forwarded to execution queue; rejected trades logged and dropped
- Temperature set to 0.0 for deterministic outputs
- Exponential backoff on API errors

---

### Layer 4 — Web3 Execution Node ✅ IMPLEMENTED

#### `src/agents/execution/signer.py` — `TransactionSigner`
- **EIP-712 typed data signing** from first principles (no `py-order-utils` dependency)
- Domain separator matches Polymarket CTF Exchange on Polygon PoS (Chain ID 137)
- Supports both standard exchange (`0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E`) and neg-risk exchange (`0xC5d563A36AE78145C45a50134d48A1215220f80a`)
- Order struct fields mirror on-chain `Order` struct exactly: salt, maker, signer, taker, tokenId, makerAmount, takerAmount, expiration, nonce, feeRateBps, side, signatureType
- `sign_order()` — Produces EIP-712 signature with 0x prefix
- `async build_order_from_decision()` — Maps LLM evaluation response to a signed order:
  - **Requires** a `BankrollPortfolioTracker` instance (raises `ValueError` if not provided)
  - Delegates position sizing to `tracker.compute_position_size()` (Quarter-Kelly + 3% cap)
  - Calls `tracker.validate_trade()` to enforce exposure limits before signing
  - Converts USDC to micro-units (6 decimals) using `Decimal('1e6')`
  - Calculates taker amount from midpoint
  - Generates random 256-bit salt for order uniqueness
  - **No hardcoded bankroll** — all sizing comes from the tracker
- Uses `eth_account.Account.sign_typed_data()` for local signing (no RPC required)

#### `src/agents/execution/nonce_manager.py` — `NonceManager`
- Async-safe nonce management under `asyncio.Lock`
- Lifecycle: `initialize()` → `get_next_nonce()` → `sync()` (on errors)
- Fetches initial nonce from Polygon RPC using `pending` block tag
- Monotonically incrementing local counter prevents duplicate/colliding orders
- `sync()` re-fetches from chain after tx reverts or RPC errors
- Raises `RuntimeError` if used before initialization
- Custom `NonceManagerError` with cause chaining

#### `src/agents/execution/gas_estimator.py` — `GasEstimator`
- EIP-1559 gas pricing for Polygon PoS
- Queries latest block for `baseFeePerGas` and `max_priority_fee`
- Applies 15% priority fee buffer (`PRIORITY_FEE_MULTIPLIER = 1.15`)
- Formula: `maxFeePerGas = (2 × baseFee) + bufferedTip`
- **Safety ceiling**: 500 Gwei hard cap — raises `GasEstimatorError` on breach
- **Fallback**: Returns 50 Gwei fixed price when RPC is unreachable
- Never caches (Polygon ~2s blocks = volatile prices)
- Returns `GasPrice` Pydantic model with Wei and Gwei values

#### `src/agents/execution/bankroll_tracker.py` — `BankrollPortfolioTracker` ✅ NEW (WI-04)
- Real-time bankroll awareness and position-size enforcement (replaces hardcoded 1000 USDC)
- `get_total_bankroll()` — Returns `config.initial_bankroll_usdc` (`Decimal`)
- `get_exposure(condition_id)` — Queries `ExecutionRepository.get_aggregate_exposure()` (PENDING + CONFIRMED)
- `get_available_bankroll(condition_id)` — `total - exposure`, floored at `Decimal("0")`
- `compute_position_size(kelly_fraction_raw, condition_id)` — Applies Quarter-Kelly (`0.25 × f*`) and 3% exposure cap: `min(kelly_size, 0.03 × bankroll)`
- `validate_trade(size_usdc, condition_id)` — Raises `ExposureLimitError` if trade exceeds exposure cap or available bankroll
- All math uses `Decimal` — no `float` for money

#### `src/agents/execution/broadcaster.py` — `OrderBroadcaster`
- Full order lifecycle orchestration: `SignedOrder → POST /order → poll receipt → TxReceipt`
- `broadcast()` — Main entry point: gets gas estimate, gets nonce, submits to CLOB, polls for confirmation
- Accepts optional `bankroll_tracker` via constructor for dependency injection through the execution pipeline
- **CLOB submission**: POST to `/order` endpoint with JSON payload
- **Receipt polling**: Queries Polygon RPC up to 30 times with 2-second intervals
- **Error handling**:
  - 4xx errors: Raises `BroadcastError` + triggers nonce sync
  - 5xx errors: Raises `BroadcastError` without nonce sync
  - Receipt timeout: Persists as `PENDING` status, then re-raises
- **DB persistence**: Every broadcast attempt persisted to `execution_txs` table with full gas accounting, order details, and receipt data
- Records `CONFIRMED`, `REVERTED`, `PENDING`, or `FAILED` status

---

## 5. Schemas (Pydantic V2)

### `src/schemas/market.py` ✅ IMPLEMENTED

| Schema | Purpose |
|---|---|
| `CLOBTick` | Single orderbook price level (price + size), frozen, coerced from strings |
| `CLOBMessage` | WebSocket frame structure (event, market, bids, asks), frozen, ignores extras |
| `MarketSnapshotSchema` | Validated snapshot from WS frame. Auto-computes midpoint via `@model_validator`. Never trusts externally-provided midpoints. Bid/ask constrained to [0.0, 1.0] |
| `MarketMetadata` | Gamma REST API response (condition_id, question, token_ids, end_date, volume_24h). Uses field aliases for API compatibility (`conditionId`, `clobTokenIds`, etc.) |

### `src/schemas/llm.py` ✅ IMPLEMENTED (THE GATEKEEPER)

This module **IS** the Gatekeeper — the risk enforcement layer between LLM output and execution. All risk rules are encoded as Pydantic validator logic.

| Schema | Purpose |
|---|---|
| `MarketContext` | Market state envelope: condition_id, outcome, bid/ask/midpoint, end_date. Validates bid ≤ ask. Computes `spread_pct` and `hours_to_resolution` as properties |
| `ProbabilisticEstimate` | LLM's probability assessment: p_true, p_market. Auto-computes via `@model_validator`: net_odds_b, expected_value, kelly_full (f*), kelly_quarter (0.25×f*) |
| `RiskAssessment` | Qualitative risk scores: liquidity_risk, resolution_risk, information_asymmetry_flag, risk_notes |
| `GatekeeperAudit` | Full audit trail of filter evaluation: all_filters_passed, triggered_filter, computed values, override status |
| `LLMEvaluationResponse` | **Primary schema** with 4 chained `@model_validator` stages: |

**Gatekeeper Filter Chain (executed in order):**
1. `_compute_ev_and_kelly` — Copies computed EV from `ProbabilisticEstimate`
2. `_apply_gatekeeper_filters` — Evaluates 5 safety filters:
   - Filter 1: EV > 0 (non-positive → forced HOLD)
   - Filter 2: EV ≥ 2% minimum edge
   - Filter 3: Confidence ≥ 75%
   - Filter 4: Spread ≤ 1.5%
   - Filter 5: Time-to-resolution ≥ 4 hours
   - Information asymmetry halves Kelly allocation
   - Position size capped at 3% of bankroll
3. `_enforce_decision_override` — Overrides LLM decision to HOLD if any filter fails. Prepends `[GATEKEEPER]` audit prefix to reasoning log
4. `_validate_final_consistency` — Assertion-level invariant checks:
   - `decision_boolean=True` + `action=HOLD` → BUG
   - `decision_boolean=False` + `position_size_pct>0` → BUG
   - `decision_boolean=True` + `EV≤0` → BUG

**Risk Constants:**
- `KELLY_FRACTION = 0.25` (Quarter-Kelly)
- `MIN_CONFIDENCE = 0.75`
- `MAX_SPREAD_PCT = 0.015` (1.5%)
- `MAX_EXPOSURE_PCT = 0.03` (3%)
- `MIN_EV_THRESHOLD = 0.02` (2% edge)
- `MIN_TTR_HOURS = 4.0`

### `src/schemas/web3.py` ✅ IMPLEMENTED

| Schema | Purpose |
|---|---|
| `OrderSide` | IntEnum: BUY=0, SELL=1 (matches on-chain encoding) |
| `OrderData` | EIP-712 Order struct: salt, maker, signer, taker, token_id, maker_amount, taker_amount, expiration, nonce, fee_rate_bps, side, signature_type. Frozen |
| `SignedOrder` | Order + hex signature + owner. Includes `to_api_payload()` for CLOB REST serialization (uint256 → string conversion for JavaScript precision safety) |
| `GasPrice` | EIP-1559 gas estimate: base_fee_wei, priority_fee_wei, max_fee_per_gas (Wei + Gwei), is_fallback flag |
| `TxReceiptSchema` | Parsed on-chain receipt: order_id, tx_hash, status, gas_used, block_number |

---

## 6. Database Layer

### `src/db/engine.py` ✅ IMPLEMENTED
- Async SQLAlchemy engine using `create_async_engine` with `aiosqlite`
- Session factory: `async_sessionmaker` with `expire_on_commit=False`, `autoflush=False`
- `get_db_session()` async generator for dependency injection
- Singleton config loading via `get_config()`

### `src/db/models.py` ✅ IMPLEMENTED — 3 Tables

#### Table 1: `market_snapshots`
- Point-in-time orderbook capture per market
- Fields: condition_id (indexed), question, best_bid, best_ask, last_trade_price, midpoint, bid_liquidity_usdc, ask_liquidity_usdc, outcome_token, market_end_date, volume_24h_usdc, raw_ws_payload, captured_at
- Composite index on `(condition_id, captured_at)`
- Relationship: `1:N` → `AgentDecisionLog`

#### Table 2: `agent_decision_logs`
- Full LLM evaluation audit trail
- Fields: snapshot_id (FK → market_snapshots), confidence_score, expected_value, decision_boolean, recommended_action (enum: BUY/SELL/HOLD), implied_probability, reasoning_log (raw CoT text), prompt_version, llm_model_id, input_tokens, output_tokens, evaluated_at
- Relationship: `N:1` → `MarketSnapshot`, `1:1` → `ExecutionTx`

#### Table 3: `execution_txs`
- On-chain transaction record per decision
- Fields: decision_id (FK → agent_decision_logs, unique), tx_hash (unique), status (enum: PENDING/CONFIRMED/FAILED/REVERTED), side, size_usdc, limit_price, condition_id, outcome_token, gas_limit, gas_price_gwei, gas_used, nonce, block_number, error_message, submitted_at, confirmed_at
- Composite index on `(status, submitted_at)`
- Unique constraint on `decision_id` enforces 1-to-1 with `AgentDecisionLog`

### `src/db/repositories/` ✅ IMPLEMENTED — 3 Repository Classes

#### `market_repo.py` — `MarketRepository`
- `insert_snapshot(snapshot) → MarketSnapshot` — Adds + flushes, returns persisted instance
- `get_latest_by_condition_id(condition_id) → MarketSnapshot | None` — Latest snapshot by `captured_at DESC`

#### `decision_repo.py` — `DecisionRepository`
- `insert_decision(decision) → AgentDecisionLog` — Adds + flushes, returns persisted instance
- `get_recent_by_market(condition_id, limit=10) → list[AgentDecisionLog]` — Joins through `MarketSnapshot`, ordered by `evaluated_at DESC`

#### `execution_repo.py` — `ExecutionRepository`
- `insert_execution(execution) → ExecutionTx` — Adds + flushes, returns persisted instance
- `get_by_decision_id(decision_id) → ExecutionTx | None` — Lookup by FK
- `get_aggregate_exposure(condition_id) → Decimal` — Sums `size_usdc` for `PENDING` + `CONFIRMED` rows only; casts to `Decimal` via `str()` to avoid float contamination

All repositories take `AsyncSession` via constructor injection. All methods are `async`. `__init__.py` re-exports all three classes.

---

## 7. Core Infrastructure

### `src/core/config.py` ✅ IMPLEMENTED — `AppConfig`
- Pydantic Settings with `.env` file loading
- **Anthropic config**: API key (SecretStr), model ID, max tokens, max retries
- **Web3 config**: Polygon RPC URL, wallet address (EIP-55 validated), private key (SecretStr)
- **CLOB config**: REST URL, WebSocket URL, Gamma API URL
- **Risk parameters**: All 6 parameters matching `docs/risk_management.md`
- **Bankroll**: `initial_bankroll_usdc: Decimal` (default `Decimal("1000")`, override via `INITIAL_BANKROLL_USDC` env var)
- **Gas config**: Max gas price ceiling (500 Gwei), fallback price (50 Gwei)
- **Database**: SQLite default connection string
- **Operational**: Log level (enum validated), dry_run flag (with warning)
- Field validators:
  - `wallet_address` — Validates and returns checksummed EIP-55 address
  - `log_level` — Validates against allowed set (DEBUG, INFO, WARNING, ERROR)
  - `dry_run` — Emits UserWarning when True
- Singleton pattern via `@lru_cache` on `get_config()`

### `src/core/exceptions.py` ✅ IMPLEMENTED — Exception Hierarchy
- `PolyOracleError` — Base exception
- `NonceManagerError` — RPC/state errors in nonce management (with cause chaining)
- `GasEstimatorError` — Gas price ceiling breaches (with cause chaining)
- `BroadcastError` — CLOB submission failures (with status_code and cause)
- `ExposureLimitError` — Trade exceeds exposure cap or available bankroll (WI-04)
- `WebSocketError` — WS connection failures (with cause chaining)
- `RESTClientError` — Gamma API failures (with status_code and cause)

### `src/core/logging.py` ✅ IMPLEMENTED — Structured Logging
- `configure_logging()` — Full structlog configuration
  - DEBUG mode: Console renderer for human readability
  - Non-DEBUG: JSON renderer for machine parsing
  - Shared processor chain: context vars, log level, logger name, timestamps, stack info, exception formatting
  - Bridges stdlib logging so third-party libraries flow through structlog
  - Binds global context: `app=poly-oracle-agent`
- `get_logger()` — Returns bound structlog logger

### `src/orchestrator.py` ✅ IMPLEMENTED — Main Entry Point
- Loads `.env` and validates `AppConfig`
- **Market discovery at startup** via `MarketDiscoveryEngine.discover()` — no hardcoded `condition_id` (WI-03)
- Instantiates `BankrollPortfolioTracker` and passes it to signer and broadcaster (WI-04)
- Instantiates all 4 layers with proper queue wiring:
  - `market_queue`: ws_client → aggregator
  - `prompt_queue`: aggregator → claude_client
  - `execution_queue`: claude_client → broadcaster
- Spins up 5 concurrent `asyncio.Task` instances via `asyncio.gather()`:
  - IngestionTask, ContextTask, EvaluationTask, ExecutionTask, **DiscoveryTask**
- **`_discovery_loop()`** — Re-runs discovery every 5 minutes; rotates aggregator to new best market if found, resets bid/ask state
- Manages two HTTP clients: `httpx.AsyncClient` for `GammaRESTClient`, `aiohttp.ClientSession` for `OrderBroadcaster`
- Execution consumer calls `await signer.build_order_from_decision()` with `bankroll_tracker`
- Graceful shutdown sequence:
  1. Catches `CancelledError` and `KeyboardInterrupt`
  2. Calls `.stop()` on all agent components (None-safe)
  3. Cancels hanging tasks
  4. Closes both HTTP clients (`aclose()` / `close()`)
  5. Disposes database engine connections

---

## 8. Documentation

### `docs/system_architecture.md` ✅ COMPLETE
- Full Mermaid architecture diagram showing all 4 modules, external services, and persistence layer
- Mermaid sequence diagram showing the async trading loop
- Complete project directory tree
- Key design decisions table (immutability, cross-field validation, 1-to-1 tx guard, raw CoT persistence, async-safe nonce, WebSocket heartbeat, enum sync)

### `docs/risk_management.md` ✅ COMPLETE
- Mental model for binary outcome contracts
- Full Kelly Criterion derivation (binary prediction market form)
- Quarter-Kelly multiplier rationale
- Expected Value formula and activation condition
- All 5 safety filters with mathematical definitions and motivations
- Gatekeeper decision matrix table
- Risk parameter registry with configurability notes
- Audit trail format specification

### `docs/business_logic.md` ✅ COMPLETE
- Buy order rule: EV > 0 mandatory
- Expected Value formula with parameter definitions
- Activation condition: `Action = BUY ⟺ EV > 0`

### `docs/architecture_visual.html` ✅ EXISTS
- HTML visualization of the architecture (6.3 KB)

---

## 9. Test Coverage

### Unit Tests (`tests/unit/`)

| Test File | Status | Tests | Covers |
|---|---|---|---|
| `test_ingestion.py` | ✅ **8 tests** | Implemented | WS message handling (valid frames, unknown events, invalid JSON, validation errors, midpoint computation), Gamma REST via `httpx` mocks (active markets, caching, 404 handling) |
| `test_nonce_manager.py` | ✅ **7 tests** | Implemented | Initialize from RPC, get_next_nonce increment, uninitialized error, sync from chain, concurrent nonce uniqueness, log verification, pending block tag usage |
| `test_signer.py` | ✅ **7 tests** | Implemented | EIP-712 domain (standard + neg-risk), order message serialization (field names, values), signer address verification, valid signature output, deterministic signatures, neg-risk signature difference, dry_run enforcement (async), chain ID constant |
| `test_gas_estimator.py` | ✅ **6 tests** | Implemented | Returns GasPrice model, priority fee multiplier, max fee formula, ceiling breach raises error, fallback on RPC error, fallback never raises |
| `test_broadcaster.py` | ✅ **8 tests** | Implemented | Happy path broadcast, DB persistence, 4xx error + nonce sync, 5xx error without nonce sync, receipt polling retries, receipt timeout raises, timeout persists to DB, gas price logging |
| `test_bankroll_tracker.py` | ✅ **13 tests** | Implemented | Bankroll queries (total, exposure, available), Quarter-Kelly sizing, 3% cap enforcement, negative Kelly floor, trade validation (pass/reject), exposure cap raises, insufficient bankroll raises, Decimal type safety, restart recovery from persisted DB state |
| `test_repositories.py` | ✅ **8 tests** | Implemented | MarketRepository (insert + get latest, None on miss), DecisionRepository (insert + recent ordered, cross-market filtering), ExecutionRepository (insert + get by decision, None on miss, aggregate exposure PENDING+CONFIRMED only, zero on empty). **100% coverage** on all repo modules |
| `test_market_discovery.py` | ✅ **12 tests** | Implemented | Eligible market selection (happy path), empty token_ids exclusion, TTR below minimum, no end_date, past end_date, exposure at/below limit, no eligible markets, empty Gamma response, unparseable end_date, TTR computation accuracy, Decimal exposure math |
| `test_schemas.py` | ⚠️ **Empty** | Stub | — |
| `test_prompt_factory.py` | ⚠️ **Empty** | Stub | — |

### Integration Tests (`tests/integration/`)

| Test File | Status | Covers |
|---|---|---|
| `test_orchestrator.py` | ✅ **5 tests** | Instantiation, early exit when no markets, shutdown disposal, discovery sets `condition_id`, dry_run execution skip |
| `test_ws_client.py` | ✅ **4 tests** | Enqueue + persist frames, filter invalid event types, skip malformed JSON payloads, handle multiple WebSocket frames |
| `test_claude_client.py` | ✅ **4 tests** | Approved decisions enqueue to execution queue, rejected decisions dropped, decision log persistence, retry on validation errors |
| `test_pipeline_e2e.py` | ✅ **3 tests** | Full 4-layer dry_run proof, discovery feeds pipeline, persistence across `market_snapshots`, `agent_decision_logs`, and `execution_txs` tables |

### WI-08 Integration Test Suite — Results

- **Metrics**
  - Total tests increased from 76 to 92 (16 new integration tests) while coverage shifted from 94% to 91%, remaining well above the 80% target.
  - Integration tests grew from the single Alembic suite to 17 deterministic scenarios running without live network access.
- **Files created/modified**
  - `tests/conftest.py` — Added `test_config`, `mock_gamma_markets`, `mock_anthropic_buy_json`, `mock_anthropic_hold_json`, `db_session_factory`, and `pipeline_queues` fixtures plus safe-collection env var setup.
  - `tests/integration/test_orchestrator.py` — 5 tests covering orchestrator lifecycle, discovery wiring, and dry_run skip logic.
  - `tests/integration/test_ws_client.py` — Rewritten to ensure enqueue/persist behavior, filtering, malformed frames, and multi-frame handling.
  - `tests/integration/test_claude_client.py` — Rewritten to assert routing, persistence, and retry behavior.
  - `tests/integration/test_pipeline_e2e.py` — New suite proving the end-to-end queue handoff and persistence across all three tables.
- **Acceptance criteria met**
  - Shared async fixtures for isolated databases, config overrides, mocked services, and queue bootstrapping.
  - Integration coverage for orchestrator startup/shutdown, queue handoff, dry_run trade gating, market discovery, and repository persistence.
  - Suite runs deterministically with mocked services and without external network access.
  - Coverage remains at 91% (target ≥ 80%).

### Test Infrastructure
- `tests/conftest.py` — ✅ **Implemented** with async in-memory SQLite fixtures (`async_engine` + `async_session` with per-test rollback), additional shared fixtures for mocked Gamma, Anthropic, queues, and safe-collection env var overrides
- Total implemented tests: **92 tests** across 12 test files (8 unit + 4 integration)
- Coverage: **91%** (target ≥ 80%)
- Framework: `pytest` with `pytest-asyncio`

---

## 10. Scripts & Utilities

| Script | Status | Purpose |
|---|---|---|
| `scripts/init_db.py` | ✅ Implemented | Creates all 3 tables (`MarketSnapshot`, `AgentDecisionLog`, `ExecutionTx`) using `Base.metadata.create_all` |
| `scripts/test_ws.py` | ✅ Implemented | Manual WebSocket test: connects to CLOB WS, runs mock consumer for 15 seconds, logs ticks |
| `scripts/test_ws_direct.py` | ✅ Implemented | Minimal raw WebSocket test: connects, subscribes, receives 5 messages directly |
| `scripts/seed_markets.py` | ⚠️ **Empty** | Stub (dev-time market seeding utility) |

---

## 11. Configuration & Environment

### `.env.example` ✅ COMPLETE
All 20 environment variables documented:
- Anthropic: `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `ANTHROPIC_MAX_TOKENS`, `ANTHROPIC_MAX_RETRIES`
- Web3: `POLYGON_RPC_URL`, `WALLET_ADDRESS`, `WALLET_PRIVATE_KEY`
- CLOB: `CLOB_REST_URL`, `CLOB_WS_URL`, `GAMMA_API_URL`
- Risk: `KELLY_FRACTION`, `MIN_CONFIDENCE`, `MAX_SPREAD_PCT`, `MAX_EXPOSURE_PCT`, `MIN_EV_THRESHOLD`, `MIN_TTR_HOURS`
- Gas: `MAX_GAS_PRICE_GWEI`, `FALLBACK_GAS_PRICE_GWEI`
- DB: `DATABASE_URL`
- Ops: `LOG_LEVEL`, `DRY_RUN`

### `.gitignore` ✅ CONFIGURED
Covers: `.env`, `venv/`, `__pycache__/`, `*.db`, `.DS_Store`, IDE dirs, build artifacts, pytest cache

### `pyproject.toml` ✅ CONFIGURED
PEP 621 project metadata with all 10 dependencies declared.

### Migrations ✅ CONFIGURED (WI-07)
- `migrations/env.py` — Alembic async environment configured with `run_async_migrations()`
- `migrations/versions/0001_initial_schema.py` — Baseline migration for all 3 tables
- `alembic.ini` — Points to `DATABASE_URL` from config

### Active Database
- `poly_oracle.db` — SQLite database file exists (69 KB), tables have been created

---

## 12. Known Gaps & Stubs

### Empty Source Files (Stubs)
| File | Expected Purpose |
|---|---|
| `scripts/seed_markets.py` | Dev-time market seeding utility |

### Empty Test Files
| File | Expected Purpose |
|---|---|
| `tests/unit/test_schemas.py` | Tests for `LLMEvaluationResponse` Gatekeeper logic, `MarketSnapshotSchema`, `OrderData` |
| `tests/unit/test_prompt_factory.py` | Tests for `PromptFactory.build_evaluation_prompt()` |

### Architecture Gaps
| Gap | Description |
|---|---|
| Repository pattern | DB repositories implemented — agent code should migrate to use them instead of direct sessions |
| README.md | Empty file — no project documentation |

---

## 13. Current State Summary

### What Has Been Built
The core trading pipeline is **structurally complete** from data ingestion to order broadcasting. All four processing layers have functional implementations:

- ✅ **Real-time data streaming** from Polymarket CLOB WebSocket with reconnection logic
- ✅ **Market metadata fetching** from Gamma REST API with caching
- ✅ **Context aggregation** with dual-trigger (time + volatility) emission system
- ✅ **Chain-of-Thought prompt construction** with embedded JSON schema enforcement
- ✅ **Claude LLM integration** with structured output parsing and retry logic
- ✅ **Pydantic Gatekeeper** implementing 5 safety filters + Quarter-Kelly position sizing
- ✅ **EIP-712 order signing** from first principles (no third-party signing libraries)
- ✅ **Async-safe nonce management** with lock-based concurrency guard
- ✅ **EIP-1559 gas estimation** with safety ceiling and fallback pricing
- ✅ **Order broadcasting** with CLOB REST submission and receipt polling
- ✅ **Full audit trail persistence** across 3 normalized database tables
- ✅ **Comprehensive risk management documentation** with mathematical specifications
- ✅ **Bankroll & portfolio tracking** via `BankrollPortfolioTracker` with DB-backed exposure, Quarter-Kelly sizing, and 3% cap enforcement
- ✅ **Autonomous market discovery** via `MarketDiscoveryEngine` with metadata, TTR, and exposure filters — no hardcoded condition_ids
- ✅ **92 automated tests** (76 unit + 16 integration) covering execution, ingestion, repository, bankroll, market discovery, and core components
- ✅ **WI-08 integration suite** proving orchestrator startup/shutdown, queue handoff, dry_run gating, market discovery, and repository persistence
- ✅ **Configuration management** with type-safe Pydantic Settings and `.env` file support

### What Is NOT Working Yet
The system is **not ready for live trading** due to:

1. **Repository layer not fully wired** — Repositories are implemented but some agent code still uses direct sessions
2. **README is empty** — No project documentation for onboarding

### Development Phase
The project is in **Phase 2 (Integration & Operational Readiness)**. Completed WIs: WI-01 (orchestrator fix), WI-02 (repository layer), WI-03 (market discovery), WI-04 (bankroll tracker), WI-05 (dry_run enforcement), WI-06 (httpx migration), WI-07 (Alembic migrations), WI-08 (integration test suite). All four layers now run under 92 tests with 91% coverage, and the integration suite runs deterministically with mocked services and no external network access. Remaining operational work focuses on repository wiring and onboarding documentation before live trading.
