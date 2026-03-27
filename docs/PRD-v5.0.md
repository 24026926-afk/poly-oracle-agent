# PRD v5.0 - Poly-Oracle-Agent Phase 5

Source inputs: `docs/PRD-v4.0.md`, `STATE.md`, `docs/system_architecture.md`, `docs/risk_management.md`, `docs/business_logic.md`, `docs/archive/ARCHIVE_PHASE_5.md`, and the Phase 5 WI business logic specs (`docs/business_logic/business_logic_wi14.md`, `docs/business_logic/business_logic_wi15.md`, `docs/business_logic/business_logic_wi18.md`, `docs/business_logic/business_logic_wi16.md`).

## 1. Executive Summary

Phase 5 extends the Phase 4 cognitive pipeline into an execution-aware trading surface. The goal is not to relax the existing risk boundary, but to ensure any BUY-eligible path is grounded in fresh market pricing, secure signing rules, live bankroll awareness, and a typed routing contract before any downstream execution side effect is possible.

The execution order of the work was deliberate:
- `WI-14` added fresh Polymarket CLOB pricing before model evaluation.
- `WI-15` hardened the wallet-signing boundary with secure key-source rules.
- `WI-18` replaced static bankroll assumptions with a live Polygon USDC balance read.
- `WI-16` composed those capabilities into a typed execution router with Kelly sizing, slippage controls, and dry-run-safe order construction.

Phase 5 preserves the same four-layer async architecture and the same terminal authority of `LLMEvaluationResponse`. It adds execution readiness without weakening the existing safety model: Decimal-only money paths, quarter-Kelly sizing, 3% exposure policy, repository isolation, and `dry_run=True` as a hard stop for signing and broadcast side effects. Phase 5 completion is recorded at 230 passing tests with 92% coverage.

## 2. Core Pillars

### 2.1 Fresh Market Pricing

Execution-sensitive reasoning must use fresh bid/ask data rather than stale context carried forward from ingestion alone. Phase 5 therefore introduced a read-only Polymarket market-data client so evaluation and routing can see current midpoint and spread before any order payload is built.

### 2.2 Secure Signing Boundary

Signing is treated as a separate security boundary, not a convenience method embedded in routing. Phase 5 hardens key custody, chain validation, and typed signing contracts so the system can sign canonical Polymarket orders without expanding broadcast capability or exposing secret material.

### 2.3 Live Bankroll Awareness

Kelly sizing is only defensible when bankroll is current. Phase 5 replaces static bankroll assumptions with a fresh, read-only Polygon USDC balance read so sizing decisions use live capital rather than configuration defaults.

### 2.4 Typed Execution Routing

Execution routing is formalized as an orchestrator that accepts only Gatekeeper-approved BUY decisions, re-checks live market conditions, applies Decimal-safe sizing and slippage rules, and returns a typed execution outcome. This keeps the execution path auditable, conservative, and testable.

## 3. Work Items

### WI-14: Polymarket Market Data Client

**Objective**  
Introduce a production-safe, read-only Polymarket CLOB market-data adapter that fetches live order-book quotes for a discovered YES token, computes midpoint probability with `Decimal`, and injects that fresh price context into evaluation before prompt construction.

**Scope Boundaries**

In scope:
- Async `PolymarketClient` order-book retrieval using the official `pyclob` SDK in read-only mode
- Typed `MarketSnapshot` contract carrying bid, ask, midpoint, spread, timestamp, and source metadata
- Decimal-only midpoint and spread math
- `ClaudeClient` integration so fresh order-book data is fetched before `PromptFactory.build_evaluation_prompt(...)`
- Conservative non-tradable fallback when token metadata is missing or the order book is malformed/unavailable

Out of scope:
- Private-key authentication
- Signing, order submission, cancellation, or settlement behavior
- Any change to Gatekeeper thresholds or EV/Kelly formulas
- Any change to Layer 4 `dry_run` enforcement

**Component Delivered**
- `PolymarketClient` in `src/agents/execution/polymarket_client.py`
- `MarketSnapshot` Pydantic model with Decimal `best_bid`, `best_ask`, `midpoint_probability`, and `spread`
- `fetch_order_book(token_id)` async contract with 500 ms timeout behavior
- `ClaudeClient` pre-prompt market-data fetch and prompt-context enrichment

**Key Invariants**
1. WI-14 is strictly read-only market data. No private keys, signer imports, or broadcast behavior are introduced.
2. Bid, ask, midpoint, and spread remain Decimal-native end to end; midpoint is computed as `(best_bid + best_ask) / Decimal("2")`.
3. Missing bid/ask data, crossed books, non-positive prices, malformed responses, timeouts, or missing `yes_token_id` fail closed as non-tradable input.
4. `LLMEvaluationResponse` remains the terminal pre-execution Gatekeeper. WI-14 enriches inputs to evaluation; it does not authorize execution directly.
5. The async queue topology remains unchanged.

**Acceptance Criteria Met**
1. `src/agents/execution/polymarket_client.py` exists with class `PolymarketClient` and a typed `MarketSnapshot` contract.
2. The market-data read path uses the official `pyclob` SDK in public, read-only mode.
3. `fetch_order_book(token_id)` is async and returns a validated snapshot or a conservative `None` result.
4. Decimal conversion is enforced at the business-logic boundary for best bid, best ask, midpoint probability, and spread.
5. `ClaudeClient` fetches fresh market data before prompt construction and uses the fetched values in prompt context.
6. Market-data failure or missing token metadata prevents an execution-eligible downstream path.
7. WI-14 closed with 34 new tests and moved the project to 153 total tests at 91% coverage.

### WI-15: Wallet Signer

**Objective**  
Introduce a hardened Polygon EIP-712 wallet-signing surface that enforces secure key custody, typed request/response contracts, and fail-closed behavior without adding transmission or broadcast capability.

**Scope Boundaries**

In scope:
- Secure key-provider boundary for `TransactionSigner`
- Typed signing contracts for canonical Polymarket order payloads
- Polygon chain validation and Decimal-only amount handling at signer boundary
- `dry_run=True` guard that prevents signer construction and key loading in orchestrator startup
- Structured non-sensitive logging for signer outcomes

Out of scope:
- Order routing logic
- Market-data fetch logic
- Changes to Gatekeeper formulas or thresholds
- Broadcast submission, receipt polling, settlement, or cancellation features
- Plaintext key material in `.env`, `os.environ`, source, logs, or database rows

**Component Delivered**
- Extended canonical `TransactionSigner` in `src/agents/execution/signer.py`
- `KeyProvider` protocol restricted to `vault` and `encrypted_keystore` source types
- `SignRequest` Pydantic model with `chain_id=137`, opaque `key_ref`, and Decimal amount fields
- `SignedArtifact` typed signing result
- `sign_order_secure()` async secure signing entry point
- Orchestrator dry-run instantiation guard for signer construction

**Key Invariants**
1. `TransactionSigner` remains the only canonical signer class; no parallel signer abstraction was introduced.
2. Key custody is vault-or-encrypted-keystore only. No plaintext env-var or `.env` private-key read is allowed in the WI-15 secure path.
3. Polygon chain identity remains fixed at `137`; signing domain addresses remain canonical.
4. Monetary fields at signer boundary remain `Decimal`, and micro-USDC conversion uses `Decimal("1e6")` only.
5. `dry_run=True` blocks signer construction and prevents key loading before any signing attempt.
6. The signer remains isolated from prompt, evaluation, and market-data modules and does not broadcast orders.

**Acceptance Criteria Met**
1. `TransactionSigner` remained the canonical signer class in `src/agents/execution/signer.py`.
2. The secure signer path accepts only approved key-source types (`vault`, `encrypted_keystore`) through `KeyProvider`.
3. `SignRequest` rejects invalid chain IDs and float amount inputs at schema boundary.
4. `sign_order_secure()` performs just-in-time key loading, source-type enforcement, address mismatch validation, and typed artifact return.
5. No WI-15 code path adds order broadcast capability or bypasses the Gatekeeper boundary.
6. `Orchestrator` skips signer construction when `dry_run=True`, preserving the Layer 4 hard stop before key access.
7. WI-15 added 46 signer-focused tests and moved the project to 200 total passing tests with zero regression.

### WI-18: Bankroll Sync

**Objective**  
Replace static bankroll sizing assumptions with a fresh, read-only Polygon USDC balance read so Kelly sizing uses live capital rather than a configuration default.

**Scope Boundaries**

In scope:
- Async `BankrollSyncProvider` for Polygon USDC `balanceOf`
- Typed `BalanceReadRequest` and `BalanceReadResult` contracts
- Exact `uint256 -> Decimal` conversion using 6-decimal USDC precision
- Explicit 500 ms timeout budget on every RPC call
- Fail-closed behavior on timeout, malformed response, or RPC error
- `BankrollPortfolioTracker.get_total_bankroll()` delegation to the live balance provider
- `dry_run=True` mock-balance return path with zero Web3 construction or RPC contact

Out of scope:
- ERC-20 approvals, transfers, or any state-mutating on-chain action
- Broadcast, signing, gas, or nonce changes
- Market-data or prompt/evaluation changes
- Multi-token, multi-chain, or cached bankroll reads
- Any fallback to stale balance data on live-read failure

**Component Delivered**
- `BankrollSyncProvider` in `src/agents/execution/bankroll_sync.py`
- `BalanceReadRequest` and `BalanceReadResult` typed contracts
- Canonical Polygon USDC proxy constant and minimal `balanceOf` ABI boundary
- `BankrollPortfolioTracker` integration so live bankroll reads feed Kelly sizing
- Orchestrator wiring that constructs and injects the provider at startup

**Key Invariants**
1. WI-18 is strictly read-only. No `approve`, `transfer`, `transferFrom`, gas usage, nonce usage, or state mutation is introduced.
2. Chain ID remains `137` and the token contract remains the canonical Polygon USDC proxy only.
3. Balance math remains Decimal-safe; live conversion is `Decimal(raw_uint256) / Decimal("1e6")` only.
4. `dry_run=True` returns `AppConfig.initial_bankroll_usdc` as a mock balance before any Web3 provider is created.
5. Timeout or RPC failure raises `BalanceFetchError`; no stale, cached, or config fallback is allowed on live reads.
6. `BankrollSyncProvider` stays isolated from signer, market-data, context, and evaluation modules.

**Acceptance Criteria Met**
1. `src/agents/execution/bankroll_sync.py` exists with canonical class `BankrollSyncProvider`.
2. `fetch_balance(...)` is async, typed, and wrapped in `asyncio.wait_for(..., timeout=0.5)`.
3. The live read path performs a single read-only `balanceOf` call against the canonical Polygon USDC proxy.
4. `float` balance inputs are rejected at schema boundary and live balance conversion remains Decimal-native.
5. `BankrollPortfolioTracker.get_total_bankroll()` delegates to `BankrollSyncProvider.fetch_balance()` for live bankroll input.
6. `dry_run=True` returns configured mock bankroll with no RPC side effect.
7. Live-read failures raise `BalanceFetchError` and block sizing rather than falling back to stale or static values.
8. WI-18 added 11 tests and moved the project to 211 total passing tests at 91% coverage.

### WI-16: Execution Router

**Objective**  
Introduce `ExecutionRouter` as the Layer 4 orchestrator that converts a validated BUY decision into a typed execution outcome by composing fresh order-book data (WI-14), live bankroll reads (WI-18), and signing delegation (WI-15), while owning Kelly sizing, slippage validation, and order-size capping.

**Scope Boundaries**

In scope:
- `ExecutionRouter` as an async orchestration boundary
- BUY-only routing from validated `LLMEvaluationResponse`
- Confidence gate before any upstream execution dependency call
- Fresh order-book fetch, live bankroll fetch, Decimal Kelly sizing, slippage guard, and order-size cap
- Typed `ExecutionAction` and `ExecutionResult` contracts
- `dry_run=True` result path that builds a full order payload but never signs
- Fail-closed handling for unavailable order books, balance-read failures, signer absence, or signer errors
- New execution config controls for per-order cap and slippage tolerance

Out of scope:
- Order broadcast / CLOB submission
- Database writes or position-tracking persistence
- SELL or HOLD routing behavior beyond typed skip results
- Changes to `PolymarketClient`, `BankrollSyncProvider`, or `TransactionSigner` internals
- Changes to the `LLMEvaluationResponse` schema or Gatekeeper logic

**Component Delivered**
- `ExecutionRouter` in `src/agents/execution/execution_router.py`
- `ExecutionAction` enum and `ExecutionResult` Pydantic model in `src/schemas/execution.py`
- New `AppConfig` execution controls: `max_order_usdc` and `max_slippage_tolerance`
- Typed routing exceptions in `src/core/exceptions.py`
- Orchestrator construction of the router with injected dependencies

**Key Invariants**
1. `ExecutionRouter` accepts only an already-validated `LLMEvaluationResponse`; it cannot bypass Gatekeeper or originate an execution candidate on its own.
2. Entry gating is conservative: non-BUY or low-confidence decisions return `SKIP` before any order-book, bankroll, or signer call.
3. Kelly sizing, slippage checks, midpoint handling, bankroll extraction, and order-size math remain Decimal-safe.
4. Phase 5 does not alter the system-wide Kelly fraction (`0.25`) or the upstream 3% exposure policy; WI-16 adds an additional absolute per-order cap via `max_order_usdc`.
5. `dry_run=True` returns a typed `DRY_RUN` result with full payload context and never calls `sign_order()`.
6. Live routing without a signer fails closed, and the router performs no database writes, no prompt construction, and no direct blockchain RPC or private-key handling.

**Acceptance Criteria Met**
1. `ExecutionRouter` exists in `src/agents/execution/execution_router.py` with a single async `route(...)` entry point.
2. `ExecutionAction` and `ExecutionResult` formalize `SKIP`, `DRY_RUN`, `EXECUTED`, and `FAILED` execution outcomes with Decimal financial fields.
3. Entry gate logic skips non-BUY and low-confidence decisions before any upstream dependency is touched.
4. Slippage rejection occurs when `best_ask > midpoint_probability + max_slippage_tolerance`.
5. Kelly sizing uses Decimal-only math: `edge = midpoint - threshold`, `odds = (1 - midpoint) / midpoint`, `kelly_scaled = (edge / odds) * kelly_fraction`.
6. Order size is capped at `min(kelly_scaled * bankroll, max_order_usdc)`, and `maker_amount` converts with `Decimal("1e6")`.
7. `dry_run=True` returns a payload-bearing `DRY_RUN` result without calling `sign_order()`.
8. `signer=None` is tolerated during dry run and returns `FAILED(reason="signer_unavailable")` for live routing attempts without a signer.
9. WI-16 added 19 tests and completed Phase 5 at 230 total passing tests and 92% coverage.

## 4. Strict Constraints

The following constraints are mandatory and non-negotiable for all Phase 5 work:

1. **Gatekeeper remains immutable:**  
   `LLMEvaluationResponse` remains the sole terminal validation boundary before execution-aware routing. No Phase 5 component bypasses, replaces, or weakens that authority.

2. **Decimal financial integrity remains immutable:**  
   All market-price, bankroll, Kelly, slippage, and order-size calculations remain Decimal-native. USDC micro-unit conversion uses `Decimal("1e6")` only.

3. **Quarter-Kelly and exposure policy remain immutable:**  
   Phase 5 does not alter `kelly_fraction=0.25` or the system-wide `min(kelly_size, 0.03 * bankroll)` exposure policy defined in risk management and business logic. WI-16 adds only an additional absolute per-order cap.

4. **`dry_run=True` remains a hard execution stop:**  
   Dry run blocks signing and broadcast side effects. Phase 5 components may compute, log, and return typed artifacts in dry run, but they may not sign or transmit live orders.

5. **Read-only dependencies remain read-only:**  
   `PolymarketClient` and `BankrollSyncProvider` introduce fresh data inputs only. They must not mutate on-chain state, hold private keys, or expand execution privileges.

6. **Async pipeline behavior remains immutable:**  
   Phase 5 preserves the existing non-blocking, queue-driven four-layer architecture. New components may enrich Layer 4, but they must not introduce synchronous bottlenecks or alternate routing around the existing pipeline.

## 5. Success Criteria For Phase 5

Phase 5 is complete when all of the following are true:

1. Fresh Polymarket order-book data is available before evaluation and conservative failure semantics prevent stale-price trading paths.
2. The signer boundary is secure, typed, fail-closed, and isolated from prompt, evaluation, and market-data concerns.
3. Bankroll sizing uses a fresh read-only Polygon USDC balance rather than a static bankroll assumption in live mode.
4. A typed execution router can convert Gatekeeper-approved BUY decisions into sized, slippage-checked order payloads while preserving dry-run safety.
5. Full regression remains green and project coverage stays at or above 80%.
6. All prior architectural invariants remain in force: Decimal safety, repository isolation, Gatekeeper authority, no hardcoded market identifiers, and `dry_run` execution blocking.
