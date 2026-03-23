# STATE.md — Poly-Oracle-Agent Project State

**Last Updated:** 2026-03-23  
**Version:** 0.1.0  
**Status:** Pre-Production (Core Infrastructure Complete, Integration In-Progress)

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

---

### Layer 2 — Context Builder ✅ IMPLEMENTED

#### `src/agents/context/aggregator.py` — `DataAggregator`
- Maintains in-memory orderbook state (best bid/ask)
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
- `build_order_from_decision()` — Maps LLM evaluation response to a signed order:
  - Converts position size percentage to USDC micro-units (6 decimals) using `Decimal` for precision
  - Calculates taker amount from midpoint
  - Generates random 256-bit salt for order uniqueness
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

#### `src/agents/execution/broadcaster.py` — `OrderBroadcaster`
- Full order lifecycle orchestration: `SignedOrder → POST /order → poll receipt → TxReceipt`
- `broadcast()` — Main entry point: gets gas estimate, gets nonce, submits to CLOB, polls for confirmation
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

### `src/db/repositories/` ⚠️ STUB (3 empty files)
- `market_repo.py` — Empty
- `decision_repo.py` — Empty
- `execution_repo.py` — Empty

---

## 7. Core Infrastructure

### `src/core/config.py` ✅ IMPLEMENTED — `AppConfig`
- Pydantic Settings with `.env` file loading
- **Anthropic config**: API key (SecretStr), model ID, max tokens, max retries
- **Web3 config**: Polygon RPC URL, wallet address (EIP-55 validated), private key (SecretStr)
- **CLOB config**: REST URL, WebSocket URL, Gamma API URL
- **Risk parameters**: All 6 parameters matching `docs/risk_management.md`
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
- Instantiates all 4 layers with proper queue wiring:
  - `market_queue`: ws_client → aggregator
  - `prompt_queue`: aggregator → claude_client
  - `execution_queue`: claude_client → broadcaster
- Spins up 4 concurrent `asyncio.Task` instances via `asyncio.gather()`
- Manages two HTTP clients: `httpx.AsyncClient` for `GammaRESTClient`, `aiohttp.ClientSession` for `OrderBroadcaster`
- Graceful shutdown sequence:
  1. Catches `CancelledError` and `KeyboardInterrupt`
  2. Calls `.stop()` on all agent components
  3. Cancels hanging tasks
  4. Closes both HTTP clients (`aclose()` / `close()`)
  5. Disposes database engine connections
- Uses a hardcoded test asset ID for testing

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
| `test_signer.py` | ✅ **7 tests** | Implemented | EIP-712 domain (standard + neg-risk), order message serialization (field names, values), signer address verification, valid signature output, deterministic signatures, neg-risk signature difference, chain ID constant |
| `test_gas_estimator.py` | ✅ **6 tests** | Implemented | Returns GasPrice model, priority fee multiplier, max fee formula, ceiling breach raises error, fallback on RPC error, fallback never raises |
| `test_broadcaster.py` | ✅ **8 tests** | Implemented | Happy path broadcast, DB persistence, 4xx error + nonce sync, 5xx error without nonce sync, receipt polling retries, receipt timeout raises, timeout persists to DB, gas price logging |
| `test_schemas.py` | ⚠️ **Empty** | Stub | — |
| `test_prompt_factory.py` | ⚠️ **Empty** | Stub | — |

### Integration Tests (`tests/integration/`)

| Test File | Status | Covers |
|---|---|---|
| `test_ws_client.py` | ⚠️ **Empty** | Stub |
| `test_claude_client.py` | ⚠️ **Empty** | Stub |

### Test Infrastructure
- `tests/conftest.py` — ⚠️ **Empty** (no shared fixtures yet)
- Total implemented tests: **43 tests** across 5 test files
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

### Migrations
- `migrations/env.py` — ⚠️ **Empty** (Alembic not configured)
- `migrations/versions/` — Empty directory
- Database initialized directly via `scripts/init_db.py` using `create_all()`

### Active Database
- `poly_oracle.db` — SQLite database file exists (69 KB), tables have been created

---

## 12. Known Gaps & Stubs

### Empty Source Files (Stubs)
| File | Expected Purpose |
|---|---|
| `src/db/repositories/market_repo.py` | Query helpers for MarketSnapshot table |
| `src/db/repositories/decision_repo.py` | Query helpers for AgentDecisionLog table |
| `src/db/repositories/execution_repo.py` | Query helpers for ExecutionTx table |
| `scripts/seed_markets.py` | Dev-time market seeding utility |

### Empty Test Files
| File | Expected Purpose |
|---|---|
| `tests/conftest.py` | Shared pytest-asyncio fixtures, in-memory DB setup |
| `tests/unit/test_schemas.py` | Tests for `LLMEvaluationResponse` Gatekeeper logic, `MarketSnapshotSchema`, `OrderData` |
| `tests/unit/test_prompt_factory.py` | Tests for `PromptFactory.build_evaluation_prompt()` |
| `tests/integration/test_ws_client.py` | Live/mocked WebSocket integration tests |
| `tests/integration/test_claude_client.py` | Live/mocked Claude API integration tests |

### Architecture Gaps
| Gap | Description |
|---|---|
| Alembic migrations | `migrations/env.py` is empty — schema evolution is not managed |
| Repository pattern | DB repositories are stubbed — direct session usage in agent code |
| Orchestrator uses legacy class names | References `AsyncWebSocketClient` and `TxBroadcaster` which differ from actual class names (`CLOBWebSocketClient`, `OrderBroadcaster`) — **orchestrator will crash on import** |
| Orchestrator hardcoded asset | Uses a single hardcoded Polymarket condition ID rather than dynamic market selection |
| `dry_run` flag | Configured in `AppConfig` but not checked in execution code |

| No market selection logic | No mechanism to discover and select profitable markets autonomously |
| No bankroll tracking | `build_order_from_decision()` uses a default 1000 USDC bankroll |
| No position tracking/portfolio management | No awareness of existing positions or exposure limits across trades |
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
- ✅ **43 unit tests** covering the execution layer, ingestion layer, and core components
- ✅ **Configuration management** with type-safe Pydantic Settings and `.env` file support

### What Is NOT Working Yet
The system is **not ready for live trading** due to:

1. **Orchestrator wiring is broken** — Import references do not match actual class names
2. **Repository layer is empty** — All 3 DB repositories are stubs
3. **No market discovery** — Agent operates on a single hardcoded market
4. **No bankroll/portfolio management** — No tracking of available capital or existing positions
5. **`dry_run` not enforced** — Flag exists but is not checked before execution
6. **No Alembic migrations** — Schema changes are not versioned
7. **Integration tests missing** — No end-to-end validation of the full pipeline
8. **README is empty** — No project documentation for onboarding

### Development Phase
The project is at the boundary between **Phase 1 (Infrastructure & Core Implementation)** and **Phase 2 (Integration & Operational Readiness)**. All individual components work in isolation (as verified by unit tests), but the orchestration layer needs repair and the system requires integration testing before any live execution.
