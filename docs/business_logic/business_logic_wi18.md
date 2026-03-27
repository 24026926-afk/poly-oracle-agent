# WI-18 Business Logic — Bankroll Sync (Live USDC Balance from Polygon L2)

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — balance read is async with explicit `asyncio.wait_for` timeout; no blocking I/O.
- `.agents/rules/web3-specialist.md` — read-only `balanceOf` call on Polygon (`chain_id=137`); canonical USDC proxy contract address only.
- `.agents/rules/risk-auditor.md` — all balance values are `Decimal`; uint256 → Decimal conversion uses exact USDC decimals (6) via `Decimal("1e6")`.
- `.agents/rules/security-auditor.md` — strictly read-only; no token approval, no transfer, no state mutation on-chain. `dry_run=True` returns mock balance without RPC contact.
- `.agents/rules/test-engineer.md` — WI-18 balance-read behavior requires unit + integration coverage; full suite remains >= 80%.

## 1. Objective

Replace the static `initial_bankroll_usdc` config lookup in `BankrollPortfolioTracker.get_total_bankroll()` with a live, read-only USDC balance fetch from the Polygon L2 blockchain. The on-chain balance is the single source of truth for the Kelly sizing formula — it must be `Decimal`-exact and always fresh at evaluation time.

WI-18 is a read-only data-feed work item. It adds balance reading only. It does not add token approvals, transfers, or any state-mutating on-chain transactions.

## 2. Scope Boundaries

### In Scope

1. New `BankrollSyncProvider` class: async, read-only USDC `balanceOf` call against Polygon L2.
2. `Decimal`-exact conversion from on-chain `uint256` to human-readable USDC using exact 6-decimal precision.
3. Explicit `asyncio.wait_for` timeout budget on every RPC call (500ms default, consistent with WI-14).
4. Fail-closed semantics: stale or failed balance fetch blocks Kelly sizing — no evaluation proceeds with a last-known or cached balance.
5. `dry_run=True` bypass: return a configurable mock balance (`AppConfig.initial_bankroll_usdc`), never contact the RPC node.
6. Integration into `BankrollPortfolioTracker.get_total_bankroll()` to replace static config lookup.

### Out of Scope

1. Token approvals, transfers, or any ERC-20 state mutation (no `approve`, `transfer`, `transferFrom`).
2. Changes to `TransactionSigner`, `OrderBroadcaster`, or `PolymarketClient` — this is an independent read path.
3. Any modification to `LLMEvaluationResponse` Gatekeeper formulas/thresholds.
4. Gas estimation, nonce management, or transaction submission logic.
5. Multi-token or multi-chain balance reads (USDC on Polygon only).
6. Balance caching or staleness windows — every evaluation cycle fetches fresh.

## 3. Target Component Architecture + Data Contracts

### 3.1 Balance Read Component (New Class)

- **Module:** `src/agents/execution/bankroll_sync.py`
- **Class Name:** `BankrollSyncProvider` (exact)
- **Responsibility:** read the wallet's USDC balance from Polygon L2 via a single `balanceOf` call.

Isolation rule:
- `BankrollSyncProvider` must remain execution-layer only. It must not depend on `TransactionSigner`, `PolymarketClient`, market-data ingestion, prompt logic, routing logic, or evaluation logic modules.
- `BankrollSyncProvider` does not import from or share state with `TransactionSigner`. They are independent read vs. write surfaces.

### 3.2 On-Chain Read Boundary (Required)

WI-18 introduces a strict read-only on-chain boundary:

1. Allowed operations:
   - `balanceOf(address)` on the canonical Polygon USDC proxy contract (read-only, no gas)
2. Forbidden operations:
   - `approve`, `transfer`, `transferFrom`, or any other state-mutating ERC-20 call
   - Any transaction that requires gas or nonce management
   - Any call to contracts other than the canonical USDC proxy
3. RPC interaction lifecycle:
   - `Web3` provider instantiated with `polygon_rpc_url` from `AppConfig`
   - Single `eth_call` per balance read — no batch, no subscription, no polling loop
   - Connection is stateless; no persistent WebSocket or long-lived session

### 3.3 Data Contracts (Required)

Balance-read boundary must use typed contracts (Pydantic at boundary is required). Minimum contracts:

1. `BalanceReadRequest`
   - `wallet_address`: checksummed EIP-55 address (from `AppConfig.wallet_address`)
   - `token_contract`: checksummed USDC proxy address on Polygon
   - `chain_id`: must equal `137`
   - `timeout_ms`: `int`, default `500`
   - `dry_run`: `bool`
2. `BalanceReadResult`
   - `balance_usdc`: `Decimal` (human-readable, 6-decimal converted)
   - `raw_balance_uint256`: `int` (original on-chain value)
   - `wallet_address`: checksummed address queried
   - `block_number`: `int | None` (block at which balance was read; `None` in dry_run)
   - `fetched_at_utc`: `datetime`
   - `is_mock`: `bool` (True when dry_run produced the value)

Hard rules:
- On-chain `uint256` to USDC conversion is `Decimal(raw_uint256) / Decimal("1e6")` only. No `float` intermediary.
- `float` inputs in balance fields are rejected at schema boundary.

## 4. Core Method Contracts (async, typed)

### 4.1 Async Balance Read Entry Point

Required public method:

- `fetch_balance(request: BalanceReadRequest) -> BalanceReadResult` (async)

Behavior requirements:

1. First check is `dry_run`; when True, return `BalanceReadResult` with `balance_usdc=AppConfig.initial_bankroll_usdc`, `is_mock=True`, and exit immediately. No RPC connection is established.
2. `dry_run=True` path MUST NOT instantiate a `Web3` provider or issue any network call.
3. Construct a minimal ERC-20 `balanceOf` call using the USDC ABI fragment (function selector `0x70a08231`).
4. Wrap the RPC call in `asyncio.wait_for(coro, timeout=request.timeout_ms / 1000)`.
5. On `asyncio.TimeoutError`: raise a typed `BalanceFetchError` — do not return stale data.
6. On any RPC/network error: raise a typed `BalanceFetchError` — do not fall back to config or cache.
7. Convert raw `uint256` result to `Decimal` via `Decimal(raw) / Decimal("1e6")`.
8. Validate converted balance is non-negative; zero is valid (empty wallet), negative is a bug.
9. Return fully populated, typed `BalanceReadResult`.

### 4.2 USDC Contract Constants (Required)

Required constants (name may vary, values may not):

- `POLYGON_USDC_PROXY`: `"0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"` (native USDC on Polygon PoS)
- `USDC_DECIMALS`: `6`
- `BALANCE_OF_SELECTOR`: `"0x70a08231"` (or equivalent minimal ABI fragment)

Hard constraints:
1. Contract address is a constant, not configurable via env var (prevents accidental reads against wrong token).
2. Only the `balanceOf` function is encoded — no other ABI methods are included.

### 4.3 Decimal Balance Integrity

1. All balance values entering or leaving the provider boundary are `Decimal`.
2. `float` inputs in balance fields are rejected at schema boundary.
3. uint256 → USDC conversion uses `Decimal(raw) / Decimal("1e6")` only.
4. Zero balance is valid and propagates normally (Kelly sizing will produce zero position).
5. Negative balance after conversion is a fatal assertion error (impossible on-chain state).

## 5. Pipeline Integration Design

WI-18 integration point is `BankrollPortfolioTracker.get_total_bankroll()`:

1. `BankrollPortfolioTracker` receives a `BankrollSyncProvider` instance at construction.
2. `get_total_bankroll()` calls `provider.fetch_balance(...)` instead of returning `config.initial_bankroll_usdc`.
3. If `dry_run=True`: provider returns mock balance from config — behavior is identical to current static lookup.
4. If `dry_run=False`: provider issues live RPC read and returns on-chain balance.
5. If `fetch_balance()` raises `BalanceFetchError`: the exception propagates up. `compute_position_size()` and `validate_trade()` must not catch it — evaluation cycle is blocked.

### 5.1 Provider Isolation Rule

The `BankrollSyncProvider` module must not:

1. import or call `TransactionSigner`,
2. import or call `PolymarketClient`,
3. import or call LLM/evaluation code,
4. perform any token approval or transfer,
5. write to the database.

Provider input is a typed `BalanceReadRequest` containing only the wallet address and contract address. Provider output is a typed `BalanceReadResult` containing only the balance and metadata.

### 5.2 Failure Semantics (Fail Closed)

On RPC timeout, connection error, malformed response, or conversion error:

1. emit structured error log (include `wallet_address`, `block_number` if available, latency; never RPC URL credentials),
2. raise typed `BalanceFetchError` — no fallback to cached/stale/config balance,
3. calling code (`BankrollPortfolioTracker`) does not catch the error — evaluation cycle is aborted for this market.

### 5.3 Evaluation Cycle Interaction

The balance fetch occurs at the start of Kelly sizing, before any position-size computation:

```
evaluation_cycle:
  1. fetch market data (WI-14)
  2. build prompt + evaluate (WI-11/12/13)
  3. Gatekeeper validation (LLMEvaluationResponse)
  4. fetch live USDC balance  ← WI-18 (here)
  5. compute_position_size() using live balance
  6. validate_trade() using live balance
  7. sign order (WI-15) → broadcast (WI-16)
```

## 6. Invariants Preserved

1. `LLMEvaluationResponse` remains the terminal pre-execution Gatekeeper.
2. Kelly formula and safety filters remain unchanged (`KELLY_FRACTION=0.25`, 3% cap still intact).
3. Decimal financial-integrity rules remain mandatory for all balance and transaction amount paths.
4. Async 4-layer queue topology remains unchanged.
5. `dry_run=True` continues to block all Layer 4 side effects; balance read returns mock value.
6. Repository pattern and DB boundaries remain unchanged.
7. `TransactionSigner` and `PolymarketClient` are unmodified — zero coupling to balance reader.
8. No PRD-v5.0 section changes; WI-18 is promoted from Phase 6 into Phase 5.

## 7. Strict Acceptance Criteria (Maker Agent)

1. `BankrollSyncProvider` is the canonical balance-read class in `src/agents/execution/bankroll_sync.py`.
2. Balance read is strictly read-only: no `approve`, `transfer`, `transferFrom`, or any state mutation.
3. On-chain call targets the canonical Polygon USDC proxy contract address only.
4. `fetch_balance(...)` entry point is async and typed (`BalanceReadRequest` → `BalanceReadResult`).
5. RPC call is wrapped in `asyncio.wait_for` with 500ms default timeout.
6. `dry_run=True` bypass occurs before Web3 provider instantiation and before any RPC call.
7. `dry_run=True` returns `AppConfig.initial_bankroll_usdc` as mock balance.
8. Timeout or RPC failure raises typed `BalanceFetchError` — no fallback to cached/stale/config balance.
9. uint256 → USDC conversion uses `Decimal(raw) / Decimal("1e6")` only; no float path.
10. `float` balance inputs are rejected at Pydantic schema boundary.
11. `BankrollPortfolioTracker.get_total_bankroll()` delegates to `BankrollSyncProvider.fetch_balance()`.
12. `BankrollSyncProvider` has zero imports from `TransactionSigner`, `PolymarketClient`, evaluation, or context modules.
13. Full regression remains green (`pytest --asyncio-mode=auto tests/`) with coverage >= 80%.

## 8. Verification Checklist

1. Unit test: `dry_run=True` path does not instantiate Web3 provider and does not issue RPC call.
2. Unit test: `dry_run=True` returns `BalanceReadResult` with `is_mock=True` and `balance_usdc == AppConfig.initial_bankroll_usdc`.
3. Unit test: successful RPC read returns `BalanceReadResult` with correct Decimal conversion from uint256.
4. Unit test: RPC timeout raises `BalanceFetchError`, not a fallback value.
5. Unit test: RPC connection error raises `BalanceFetchError`, not a fallback value.
6. Unit test: malformed RPC response (non-integer, missing data) raises `BalanceFetchError`.
7. Unit test: float input in `BalanceReadRequest` balance-related fields is rejected at schema boundary.
8. Unit test: uint256 → Decimal conversion uses `Decimal("1e6")` and produces expected 6-decimal output.
9. Unit test: zero balance (empty wallet) is valid and propagates as `Decimal("0")`.
10. Integration test: `BankrollPortfolioTracker.get_total_bankroll()` calls `BankrollSyncProvider.fetch_balance()` and returns the on-chain balance.
11. Integration test: `BankrollPortfolioTracker.compute_position_size()` with failed balance fetch does not produce a position size (error propagates).
12. Integration test: `BankrollSyncProvider` module has no dependency on signer/polymarket/evaluation modules (import boundary check).
13. Integration test: end-to-end with `dry_run=True` — position sizing uses mock balance, no RPC call issued.
14. Full suite:
    - `pytest --asyncio-mode=auto tests/`
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m`
