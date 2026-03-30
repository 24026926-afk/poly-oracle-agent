# STATE.md — Poly-Oracle-Agent Project State

**Last Updated:** 2026-03-30
**Version:** 0.8.0
**Status:** Phase 7 Complete — WI-22 + WI-20 + WI-21 Sealed
**Active WI:** Phase 8 Planning — WI-23 TBD

---

## Historical Context & Invariants

See `docs/archive/ARCHIVE_PHASES_1_TO_3.md` for:
- Core architectural invariants (4-layer pipeline, Decimal math, Repository Pattern, Pydantic Gatekeeper)
- Completed infrastructure inventory
- WI-01 through WI-10 achievement index

---

## Current Metrics

| Metric | Value |
|---|---|
| Total tests | 362 |
| Coverage | 93% (target ≥ 80%) |
| Framework | `pytest` + `pytest-asyncio` |
| DB | `poly_oracle.db` (SQLite, 4 tables, Alembic-managed) |

---

## Phase 4: Cognitive Architecture

### Work Items

- [x] **WI-11 — Market Router** (completed 2026-03-26)
  - `MarketCategory` enum (`CRYPTO | POLITICS | SPORTS | GENERAL`) in `src/schemas/llm.py`
  - `ClaudeClient._route_market()` — async keyword/pattern classification, no extra LLM call
  - `PromptFactory.build_evaluation_prompt(category=...)` — injects domain-specific persona preamble
  - Gatekeeper (`LLMEvaluationResponse`) remains final validation gate regardless of route
  - Key files: `src/schemas/llm.py`, `src/agents/context/prompt_factory.py`, `src/agents/evaluation/claude_client.py`

- [x] **WI-12 — Chained Prompt Factory** (completed 2026-03-26)
  - `SentimentResponse` schema with `Decimal` sentiment_score, int tweet_volume_delta, str top_narrative_summary
  - `GrokClient` async interface (mock-first, 2.0s timeout, httpx-ready, fallback on all failures)
  - `PromptFactory` injects `### SENTIMENT ORACLE (LAST 60 MIN)` block with sentiment values
  - `ClaudeClient._fetch_sentiment()` — category-gated Grok calls (CRYPTO/POLITICS only)
  - Normalized audit logging: `{status, reason, sentiment_score, tweet_volume_delta, top_narrative_summary}`
  - Gatekeeper (`LLMEvaluationResponse`) remains terminal gate; sentiment is upstream cognitive signal only
  - 8 integration tests (RED→GREEN), 115 total tests pass, zero regression
  - Key files: `src/schemas/llm.py`, `src/agents/evaluation/grok_client.py`, `src/agents/context/prompt_factory.py`, `src/agents/evaluation/claude_client.py`, `src/core/config.py`

- [x] **WI-13 — Reflection Auditor** (completed 2026-03-26)
  - Mandatory reflection pass after Stage B and before Gatekeeper validation
  - Enforces conservative HOLD path on bias/contradiction/timeout; ADJUSTED path is single-pass
  - Reflection artifacts persisted in decision audit log envelope; 119 tests passing

### Phase 4 Completion Gate

- [x] WI-12 implemented, tests pass (115 passed), no coverage regression ✅
- [x] WI-13 implemented, tests pass (119 passed), no coverage regression
- [x] `STATE.md` updated: version `0.4.0`, status `Phase 4 Complete`
- [ ] PRs merged to `develop` ✅, then `develop → main`

---

## Phase 5: Market Data Integration

### Work Items

- [x] **WI-14 — Polymarket Market Data Client** (completed 2026-03-26)
  - `PolymarketClient` read-only async client in `src/agents/execution/polymarket_client.py`
  - `MarketSnapshot` Pydantic model with Decimal-typed bid/ask/midpoint/spread
  - `fetch_order_book(token_id)` async method via official `pyclob` SDK (500ms timeout)
  - Decimal-only midpoint: `(best_bid + best_ask) / Decimal("2")`, no float in money path
  - Non-positive prices (≤ 0), crossed books, missing/malformed fields → `None` (non-tradable)
  - `ClaudeClient._process_evaluation` fetches fresh market data before `PromptFactory.build_evaluation_prompt`
  - Missing `yes_token_id` or fetch failure → conservative skip, no execution enqueue
  - `LLMEvaluationResponse` Gatekeeper remains terminal gate, unchanged
  - 34 new tests (24 unit + 6 integration + 4 MAAP fixes), 153 total, 91% coverage
  - Key files: `src/agents/execution/polymarket_client.py`, `src/agents/evaluation/claude_client.py`, `pyproject.toml`

- [x] **WI-15 — Wallet Signer** (completed 2026-03-27)
  - `TransactionSigner` is the single canonical WI-15 signer in `src/agents/execution/signer.py`
  - `KeyProvider` protocol: vault or encrypted keystore only — no `os.environ`, no `.env`
  - `SignRequest` Pydantic model: chain_id=137 enforcement, Decimal-only amounts, float rejected at boundary
  - `SignedArtifact` typed output: signature, owner, signed_at_utc, key_source_type
  - `sign_order_secure()` async WI-15 entry point, fail-closed, no transmission/broadcast capability
  - Source type enforcement: rejects all key sources except `vault` and `encrypted_keystore`
  - Address mismatch guard: derived key must match expected_address
  - Module isolation: zero imports from evaluation, context, or market-data modules
  - Orchestrator dry_run gate: `TransactionSigner` not constructed when `dry_run=True`
  - 46 WI-15 tests (31 unit + 15 integration) + 29 async fixture fixes, 200 total, zero regression
  - Key files: `src/agents/execution/signer.py`, `src/orchestrator.py`

- [x] **WI-16 — Execution Router** (completed 2026-03-27)
  - `ExecutionRouter` is the canonical WI-16 execution orchestrator in `src/agents/execution/execution_router.py`
  - `ExecutionResult` / `ExecutionAction` typed routing contract added in `src/schemas/execution.py`
  - Entry gate skips non-BUY and low-confidence decisions before any upstream order-book, bankroll, or signer call
  - Decimal-only Kelly sizing: `edge = midpoint - threshold`, `odds = (1 - midpoint) / midpoint`, `kelly_scaled = (edge / odds) * config.kelly_fraction`
  - Slippage guard rejects when `best_ask > midpoint_probability + max_slippage_tolerance`
  - Order size capped at `min(kelly_fraction * bankroll, max_order_usdc)` with `maker_amount = int(order_size * Decimal("1e6"))`
  - `dry_run=True` returns a typed `DRY_RUN` result with a full `OrderData` payload and never calls `sign_order()`
  - `signer=None` is tolerated in dry run and returns `FAILED(reason="signer_unavailable")` when live routing is attempted without a signer
  - New config: `max_order_usdc=Decimal("50")`, `max_slippage_tolerance=Decimal("0.02")`
  - 19 new WI-16 tests (4 unit + 15 integration), 230 total, 92% coverage, full regression green
  - Key files: `src/agents/execution/execution_router.py`, `src/schemas/execution.py`, `src/core/config.py`, `src/core/exceptions.py`, `src/orchestrator.py`

- [x] **WI-18 — Bankroll Sync** (completed 2026-03-27)
  - `BankrollSyncProvider` is the canonical WI-18 balance reader in `src/agents/execution/bankroll_sync.py`
  - Read-only Polygon USDC `balanceOf` call only; no `approve`, `transfer`, `transferFrom`, or state mutation
  - Typed `BalanceReadRequest` / `BalanceReadResult` contracts enforce chain_id `137`, canonical USDC proxy, and Decimal-only balance fields
  - `asyncio.wait_for(..., timeout=0.5)` wraps the live RPC read; timeout and RPC failures raise `BalanceFetchError`
  - `dry_run=True` returns `AppConfig.initial_bankroll_usdc` as a mock balance before any `Web3` construction or RPC contact
  - `BankrollPortfolioTracker.get_total_bankroll()` now delegates to `BankrollSyncProvider.fetch_balance()` for live Kelly bankroll
  - `Orchestrator` wires `BankrollSyncProvider` into `BankrollPortfolioTracker` at startup; queue topology unchanged
  - 11 new WI-18 tests (8 unit + 3 integration), 211 total, 91% coverage, full regression green
  - Key files: `src/agents/execution/bankroll_sync.py`, `src/agents/execution/bankroll_tracker.py`, `src/orchestrator.py`, `src/core/exceptions.py`

### Phase 5 Completion Gate

- [x] WI-14 implemented and merged into `develop`
- [x] WI-15 implemented and merged into `develop`
- [x] WI-16 implemented and merged into `develop`
- [x] WI-18 implemented and merged into `develop`
- [x] Full regression green: 230 tests passing
- [x] Coverage maintained at 92% (target ≥ 80%)
- [x] `STATE.md`, `README.md`, and `CLAUDE.md` updated for phase completion
- [x] `docs/archive/ARCHIVE_PHASE_5.md` created

---

## Phase 6: Position Lifecycle

### Work Items

- [x] **WI-17 — Position Tracker** (completed 2026-03-29)
  - `PositionTracker` persists execution outcomes as typed `PositionRecord` entries in `positions` table
  - `PositionStatus` enum (`OPEN | CLOSED | FAILED`) and `PositionRecord` Pydantic model in `src/schemas/position.py`, re-exported from `src/schemas/execution.py`
  - `Position` SQLAlchemy ORM model with `Numeric(38,18)` for all 5 financial columns, 3 indexes
  - `PositionRepository` async CRUD in `src/db/repositories/position_repository.py` (5 methods, follows `ExecutionRepository` pattern)
  - Alembic migration `0002_add_positions_table.py` (parent: `0001`)
  - `record_execution(result, condition_id, token_id) -> PositionRecord | None` — sole public async entry point
  - SKIP → `None`, EXECUTED/DRY_RUN → `OPEN`, FAILED → `FAILED` with `Decimal("0")` sentinels for None financials
  - `dry_run=True` logs full record via structlog, zero DB writes, zero session creation
  - Unreachable state guards: `EXECUTED+dry_run` and `DRY_RUN+live` log error and return `None`
  - Orchestrator: constructed in `__init__()`, called in `_execution_consumer_loop()` before dry_run gate
  - MAAP audit caught 2 orchestrator wiring defects (token_id field, dry_run bypass) — both fixed and re-cleared
  - 27 new tests (unit + integration), 257 total, 92% coverage, full regression green
  - Key files: `src/agents/execution/position_tracker.py`, `src/schemas/position.py`, `src/schemas/execution.py`, `src/db/models.py`, `src/db/repositories/position_repository.py`, `migrations/versions/0002_add_open_positions_table.py`, `src/orchestrator.py`

### Phase 6 Completion Gate

- [x] WI-17 implemented and merged into `develop`
- [x] WI-19 implemented and merged into `develop`
- [x] Full regression green: 295 tests passing
- [x] Coverage maintained at 92% (target ≥ 80%)
- [x] `STATE.md`, `README.md`, and `CLAUDE.md` updated for phase completion
- [x] `docs/archive/ARCHIVE_PHASE_6.md` created
- [ ] PRs merged to `develop` ✅, then `develop → main`

---

## Phase 7: Exit Path Decoupling

### Work Items

- [x] **WI-22 — Periodic Exit Scan** (completed 2026-03-30)
  - Added `AppConfig.exit_scan_interval_seconds: Decimal = Decimal("60")`
  - Added `Orchestrator._exit_scan_loop()` with sleep-first cadence:
    `await asyncio.sleep(float(self.config.exit_scan_interval_seconds))`
  - Added orchestrator task registration:
    `asyncio.create_task(self._exit_scan_loop(), name="ExitScanTask")`
  - Removed inline `scan_open_positions()` call from `_execution_consumer_loop()`
  - New structlog events:
    - `exit_scan_loop.completed` (`total`, `exits`, `holds`, `interval_seconds`)
    - `exit_scan_loop.error` (`error`)
  - Preserved invariants:
    - `ExitStrategyEngine`, `ExecutionRouter`, `PositionTracker`, and schemas unchanged
    - Queue topology unchanged (`market_queue -> prompt_queue -> execution_queue`)
    - `dry_run` write gate remains inside `ExitStrategyEngine` internals
  - Test additions:
    - `tests/unit/test_exit_scan_loop.py` (8 tests)
    - `tests/integration/test_exit_scan_integration.py` (5 tests)
  - Regression:
    - `pytest --asyncio-mode=auto tests/ -q` → 308 passed
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` → 93%

- [x] **WI-20 — Exit Order Router** (completed 2026-03-30)
  - Added `ExitOrderRouter` in `src/agents/execution/exit_order_router.py`
  - Added `ExitOrderAction` (`SELL_ROUTED | DRY_RUN | FAILED | SKIP`) and frozen `ExitOrderResult` with float-rejecting Decimal validators
  - Added `ExitRoutingError` to exception taxonomy in `src/core/exceptions.py`
  - Added `AppConfig.exit_min_bid_tolerance: Decimal = Decimal("0.01")`
  - Implemented SELL-only exit routing path:
    - Entry gate skip for `should_exit=False` and `exit_reason=ERROR`
    - Fresh `fetch_order_book(position.token_id)` lookup (token_id, never condition_id)
    - Exit bid floor guard (`best_bid < exit_min_bid_tolerance` fails closed)
    - Decimal-only sizing from position metadata:
      - `token_quantity = order_size_usdc / entry_price`
      - `maker_amount = int(token_quantity * Decimal("1e6"))`
      - `taker_amount = int((token_quantity * best_bid) * Decimal("1e6"))`
    - `dry_run=True` returns full payload without signing
    - `signer=None` live guard and signing-exception fail-closed handling
  - Orchestrator wiring:
    - `ExitOrderRouter` constructed in `Orchestrator.__init__()`
    - `_exit_scan_loop()` now routes actionable exits, catches per-exit routing errors, and continues (fail-open)
    - Exit broadcast attempted only when `SELL_ROUTED`, `signed_order` exists, `dry_run=False`, and broadcaster is present
  - Test additions:
    - `tests/unit/test_exit_order_router.py` (14 tests)
    - `tests/integration/test_exit_order_router_integration.py` (9 tests)
  - Regression:
    - `pytest --asyncio-mode=auto tests/ -q` → 331 passed
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` → 93%

- [x] **WI-21 — Realized PnL & Settlement** (completed 2026-03-30)
  - Added `PnLCalculator` in `src/agents/execution/pnl_calculator.py`
  - Added frozen `PnLRecord` schema with float-rejecting Decimal validators in `src/schemas/execution.py`
  - Added `PnLCalculationError` to exception taxonomy in `src/core/exceptions.py`
  - Extended `PositionRecord` with optional settlement fields:
    - `realized_pnl: Decimal | None`
    - `exit_price: Decimal | None`
    - `closed_at_utc: datetime | None`
  - Extended `Position` ORM with nullable settlement columns:
    - `realized_pnl Numeric(38,18)`
    - `exit_price Numeric(38,18)`
    - `closed_at_utc DateTime(timezone=True)`
  - Added Alembic migration `0003_add_pnl_columns.py` (parent `0002`)
  - Added additive `PositionRepository.record_settlement()` with idempotency guard (`position.settlement_already_recorded`)
  - Orchestrator wiring:
    - `PnLCalculator` constructed in `Orchestrator.__init__()`
    - `_exit_scan_loop()` settles PnL after `ExitOrderRouter.route_exit()` when action is `SELL_ROUTED`/`DRY_RUN` with non-null `exit_price`
    - Settlement failures logged as `exit_scan.pnl_settlement_error` and do not block scan/broadcast path
  - Test additions:
    - `tests/unit/test_pnl_calculator.py` (19 tests)
    - `tests/integration/test_pnl_settlement_integration.py` (12 tests)
  - Regression:
    - `pytest --asyncio-mode=auto tests/ -q` → 362 passed
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` → 93%

### Phase 7 Progress Gate

- [x] WI-22 implemented and validated
- [x] WI-20 implemented and validated
- [x] WI-21 implemented and validated
- [x] Full phase regression + archive seal

---

## Active Constraints (always enforced)

1. **Decimal math** — all monetary values; no `float` in financial calculations
2. **Repository pattern** — `market_snapshots`, `agent_decision_logs`, `execution_txs`, `positions` only through their respective repositories
3. **Pydantic Gatekeeper** — `LLMEvaluationResponse` is the final validation gate; no bypass
4. **No hardcoded `condition_id`** — market discovery via `MarketDiscoveryEngine` only
5. **`dry_run=True` blocks execution** — `OrderBroadcaster` enforces; always set in dev/test
6. **Async-only** — no blocking I/O in any agent task; `asyncio.Lock` for shared state
7. **Live bankroll sync** — Kelly sizing uses fresh Polygon USDC balance; `initial_bankroll_usdc` is mock-only when `dry_run=True`

---

## Key File Map (Phase 7)

| File | Purpose |
|---|---|
| `src/agents/execution/bankroll_sync.py` | `BankrollSyncProvider` — read-only Polygon USDC bankroll sync with typed request/result contracts |
| `src/agents/execution/execution_router.py` | `ExecutionRouter` — BUY-only execution routing, Decimal Kelly sizing, slippage guard, dry-run bypass |
| `src/agents/execution/signer.py` | `TransactionSigner` — canonical signer: legacy `sign_order()` + WI-15 `sign_order_secure()` |
| `src/agents/execution/polymarket_client.py` | `PolymarketClient` — read-only CLOB market data + `MarketSnapshot` |
| `src/agents/execution/position_tracker.py` | `PositionTracker` — persists execution outcomes as typed `PositionRecord` entries |
| `src/agents/execution/exit_strategy_engine.py` | `ExitStrategyEngine` — rule-based exit evaluation for open positions |
| `src/agents/execution/exit_order_router.py` | `ExitOrderRouter` — SELL-side exit routing from `ExitResult` + `PositionRecord` to signed/unsigned `OrderData` |
| `src/agents/execution/pnl_calculator.py` | `PnLCalculator` — WI-21 realized PnL computation + settlement persistence orchestration |
| `src/schemas/position.py` | `PositionRecord`, `PositionStatus` — position lifecycle schemas |
| `src/schemas/execution.py` | `ExecutionResult` / `ExecutionAction` / `ExitReason` / `ExitSignal` / `ExitResult` / `ExitOrderAction` / `ExitOrderResult` / `PnLRecord` |
| `src/db/repositories/position_repository.py` | `PositionRepository` — async CRUD for `positions` table |
| `src/db/models.py` | `Position` ORM model with `Numeric(38,18)` financial + WI-21 settlement columns |
| `migrations/versions/0002_add_open_positions_table.py` | Alembic migration adding `positions` table |
| `migrations/versions/0003_add_pnl_columns.py` | Alembic migration adding `realized_pnl`, `exit_price`, `closed_at_utc` |
| `src/schemas/llm.py` | `MarketCategory` enum + `SentimentResponse` + `LLMEvaluationResponse` Gatekeeper |
| `src/agents/context/prompt_factory.py` | `PromptFactory` — domain-aware + sentiment oracle injection |
| `src/agents/evaluation/claude_client.py` | `ClaudeClient` — WI-14 fetch + routing + sentiment + evaluation |
| `src/agents/evaluation/grok_client.py` | `GrokClient` — async sentiment oracle (mock-first, 2.0s timeout) |
| `src/core/config.py` | `AppConfig` — Grok fields, WI-16 order cap/slippage, WI-22 scan interval, WI-20 exit bid floor |
| `src/orchestrator.py` | Main entry point; spins up 6 async tasks, periodic scans, and WI-20 exit routing/broadcast integration |
| `docs/PRD-v4.0.md` | Phase 4 scope and acceptance criteria |
| `docs/archive/ARCHIVE_PHASES_1_TO_3.md` | Historical invariants and completed WI index |
| `AGENTS.md` | Agent rules, class name reference, hard constraints |
