# WI-14 Business Logic — Polymarket Market Data Client (CLOB Read Path)

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — all market data calls remain async; queue order remains `market_queue -> prompt_queue -> execution_queue`.
- `.agents/rules/ingestion-specialist.md` — no hardcoded market identifiers; token selection must come from discovered market metadata.
- `.agents/rules/risk-auditor.md` — midpoint/spread math is Decimal-safe; no float precision loss on trading-critical prices.
- `.agents/rules/test-engineer.md` — new WI-14 behavior must be test-covered; full suite remains >= 80% coverage.
- `.agents/rules/security-auditor.md` — WI-14 is read-only market data only; no private-key signing surface is added.

## 1. Objective

Introduce a production-safe, read-only Polymarket market data adapter that can fetch live order book quotes for a specific YES `token_id`, compute a deterministic midpoint probability using `Decimal`, and inject that fresh price context into the evaluation pipeline before Claude reasoning.

This WI is a data-quality and pre-trade integrity upgrade. It does not add order placement.

## 2. Scope Boundaries

### In Scope

1. New async `PolymarketClient` component.
2. Official `pyclob` SDK integration in read-only mode.
3. Async order book fetch by YES `token_id`.
4. Decimal-only midpoint probability calculation.
5. Pipeline injection so Claude sees fresh bid/ask/spread before evaluation.

### Out of Scope

1. Private-key auth for CLOB trading.
2. Signing, order posting, cancel flows, or settlement logic.
3. Any change to Gatekeeper thresholds/formulas in `LLMEvaluationResponse`.
4. Any bypass of `dry_run` controls in Layer 4.

## 3. Target Component Architecture

### 3.1 New Client

- **Module:** `src/agents/execution/polymarket_client.py`
- **Class Name:** `PolymarketClient` (exact)
- **Responsibility:** provide canonical read-only order book snapshots for one token.

### 3.2 External Dependency

- Integrate the official Polymarket Python CLOB SDK (`pyclob` / `py-clob-client`) as the primary transport.
- WI-14 must not implement raw handcrafted CLOB REST request logic as the primary path.
- Client initialization in WI-14 is strictly public/read-only (host + chain/network context only).

### 3.3 Data Contract (Required)

`PolymarketClient` must expose a typed quote snapshot contract (schema location is implementation choice, but must be Pydantic-validated at boundary) containing at minimum:

1. `token_id` (YES asset id)
2. `best_bid` (`Decimal`)
3. `best_ask` (`Decimal`)
4. `midpoint_probability` (`Decimal`)
5. `spread` (`Decimal`)
6. `fetched_at_utc` (timestamp)
7. `source` (e.g., `clob_orderbook`)

## 4. Core Method Contracts

### 4.1 Async Order Book Fetch

Required public method:

- `fetch_order_book(token_id: str)` (async)

Behavior requirements:

1. Accepts a single YES `token_id`.
2. Fetches order book via official SDK call.
3. Extracts top-of-book bid/ask.
4. Converts all numeric price fields with `Decimal(str(value))`.
5. Rejects invalid books (`best_ask < best_bid`, missing side, non-positive values).
6. Returns validated snapshot object or explicit non-tradable result (no silent fallback to float defaults).

### 4.2 Market Probability Calculation (Decimal-only)

Required method (exact name may vary, behavior may not):

- Compute midpoint implied probability:
  - `market_probability = (best_bid + best_ask) / Decimal("2")`

Hard constraints:

1. Use `Decimal` exclusively for bid/ask/midpoint/spread math.
2. Never use `float`, `round(float, ...)`, or implicit float division.
3. Enforce deterministic precision policy (implementation may quantize, but must remain Decimal-native end-to-end).

## 5. Pipeline Injection Design (WI-14 Integration Point)

WI-14 injection is mandatory before Claude primary reasoning:

1. Layer 2 still emits candidate market context to `prompt_queue`.
2. Layer 3 (`ClaudeClient._process_evaluation`) receives the item.
3. **Before** `PromptFactory.build_evaluation_prompt(...)`, ClaudeClient fetches fresh order book data from `PolymarketClient` using YES `token_id`.
4. ClaudeClient overwrites/augments `market_state` with fetched `best_bid`, `best_ask`, `midpoint_probability`, and `spread`.
5. PromptFactory receives this enriched state so Claude reasons over live spread-aware pricing.

### 5.1 Token ID Supply Contract

`token_id` must be injected from market discovery metadata path, not inferred from `condition_id` and never hardcoded.

Minimum requirement for WI-14 wiring:

1. Active market payload passed into evaluation contains `yes_token_id`.
2. Missing `yes_token_id` is treated as non-tradable input and logged.

### 5.2 Failure Semantics (Conservative)

If fresh order book fetch fails (timeout/SDK error/empty book/invalid spread):

1. No BUY-eligible evaluation path is allowed on stale or incomplete market data.
2. Item is dropped to conservative non-trading behavior (skip evaluation or forced HOLD path; implementation must be deterministic and logged).
3. No enqueue to `execution_queue` from that failed fetch path.

## 6. Invariants Preserved

1. `LLMEvaluationResponse` remains the terminal Gatekeeper.
2. WI-14 does not alter EV/Kelly formulas or risk thresholds.
3. `dry_run=True` enforcement remains unchanged in Layer 4.
4. No direct DB session usage introduced; repository pattern remains intact.
5. Async runtime and queue architecture remain unchanged.

## 7. Strict Acceptance Criteria (Maker Agent)

1. `src/agents/execution/polymarket_client.py` exists with class `PolymarketClient`.
2. Official `pyclob` SDK is wired as the read path dependency for order book retrieval.
3. WI-14 implementation is read-only; no private-key credential requirement in `PolymarketClient`.
4. `PolymarketClient` provides an async order book fetch method that accepts YES `token_id`.
5. Returned best bid/ask/midpoint/spread are `Decimal`-typed at business-logic boundary.
6. Midpoint probability is computed as bid/ask midpoint with Decimal-only arithmetic.
7. Invalid/missing top-of-book data is handled as non-tradable with explicit reason logging.
8. `ClaudeClient` fetches market data before prompt construction and uses the fetched values in prompt context.
9. Evaluation path cannot produce execution-eligible output when WI-14 market data fetch is invalid/unavailable.
10. `LLMEvaluationResponse.model_validate_json(...)` remains the single final execution gate.
11. Full regression remains green (`pytest --asyncio-mode=auto tests/`) and coverage remains >= 80%.
12. WI-14 adds dedicated unit/integration tests for:
    - order book parse and Decimal conversion
    - midpoint Decimal correctness
    - missing bid/ask conservative behavior
    - pre-prompt fetch ordering in `ClaudeClient`

## 8. Verification Checklist

1. Unit tests for `PolymarketClient` pass:
   - valid book -> Decimal snapshot
   - missing bid or ask -> conservative non-tradable outcome
   - `best_ask < best_bid` -> rejected snapshot
   - midpoint precision assertions with Decimal fixtures
2. Integration test verifies `ClaudeClient` calls `PolymarketClient` before `PromptFactory.build_evaluation_prompt`.
3. Integration test verifies prompt context contains refreshed spread/midpoint from WI-14 snapshot.
4. Integration test verifies market-data failure path never enqueues execution candidate.
5. Full suite:
   - `pytest --asyncio-mode=auto tests/`
   - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m`
