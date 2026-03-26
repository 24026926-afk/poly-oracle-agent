# ARCHIVE — Poly-Oracle-Agent Phases 1–3

> Compressed reference. Source files (PRD-v2.0.md, PRD-v3.0.md, WI-01 through WI-10) are retained in version control but are NOT required reading for Phase 4 work.

---

## 1. Core Architectural Invariants

These constraints are PERMANENT and must never be violated in Phase 4+:

### Pipeline Structure
- **4-layer async pipeline**: Ingestion → Context → Evaluation → Execution, connected by `asyncio.Queue` bridges
- Each layer runs as a concurrent `asyncio.Task` inside a single event loop
- Inter-layer data types: `MarketSnapshot` (L1→L2), prompt+state dict (L2→L3), `SignedDecision` (L3→L4)

### Financial Math
- **All monetary calculations MUST use `Decimal`** — never `float`
- USDC micro-unit conversion: `Decimal(str(maker_amount)) / Decimal('1e6')` (never float division)
- Exposure aggregation: cast SQLite float results via `Decimal(str(raw))` before arithmetic
- Kelly sizing: Quarter-Kelly (`0.25 × f*`) with 3% exposure cap enforced at all times

### Repository Pattern
- **All persistence for the three domains routes exclusively through repositories** — no direct `session.add/execute/flush` in agent code
- `market_snapshots` → `MarketRepository` only
- `agent_decision_logs` → `DecisionRepository` only
- `execution_txs` → `ExecutionRepository` only
- Injection pattern: `Callable[[AsyncSession], RepoClass] = RepoClass` with production defaults on all four agent clients
- Bypass is a bug; a regression test exists to prove it fails when a direct session call is injected

### Pydantic Gatekeeper
- `LLMEvaluationResponse` is the ONLY validation gate between LLM output and execution
- 5 mandatory safety filters (evaluated in order): EV > 0, EV ≥ 2%, Confidence ≥ 75%, Spread ≤ 1.5%, TTR ≥ 4h
- Gatekeeper overrides any LLM `decision_boolean=True` if any filter fails — no exceptions
- `_validate_final_consistency` performs assertion-level invariant checks (e.g., `decision_boolean=True` + `action=HOLD` is a BUG)
- Risk constants: `KELLY_FRACTION=0.25`, `MIN_CONFIDENCE=0.75`, `MAX_SPREAD_PCT=0.015`, `MAX_EXPOSURE_PCT=0.03`, `MIN_EV_THRESHOLD=0.02`, `MIN_TTR_HOURS=4.0`

### EIP-712 / Execution Safety
- EIP-712 signing from first principles — no `py-order-utils` dependency
- Chain ID 137 (Polygon PoS); domain separator is hardcoded and matches CTF Exchange
- `dry_run=True` blocks all CLOB broadcast calls; enforced in `OrderBroadcaster`
- Gas safety ceiling: 500 Gwei hard cap; fallback: 50 Gwei fixed when RPC unreachable

### Market Discovery
- No hardcoded `condition_id` in runtime code — ever
- `MarketDiscoveryEngine.discover()` applies: metadata presence → TTR ≥ 4h → exposure < 3% bankroll
- Re-runs every 5 minutes via `_discovery_loop()` in orchestrator; rotates aggregator on new best market

---

## 2. Completed Infrastructure

| Component | Status | Notes |
|---|---|---|
| SQLite + SQLAlchemy 2.0 Async | ✅ Complete | `aiosqlite`, `AsyncSessionLocal`, `expire_on_commit=False` |
| Alembic migrations | ✅ Complete | `migrations/versions/0001_initial_schema.py` baseline; `alembic upgrade head` is the only schema path |
| 3 normalized DB tables | ✅ Complete | `market_snapshots`, `agent_decision_logs`, `execution_txs` |
| 3 Repository classes | ✅ Complete | Fully wired with injectable factories; bypass regression test in place |
| CLOB WebSocket ingestion | ✅ Complete | `CLOBWebSocketClient`; exponential backoff; validates via `MarketSnapshotSchema` |
| Gamma REST client | ✅ Complete | `GammaRESTClient` (httpx); 60s TTL cache; graceful stale fallback |
| Market Discovery Engine | ✅ Complete | `MarketDiscoveryEngine`; TTR + exposure filters; no hardcoded IDs |
| Bankroll / Portfolio Tracker | ✅ Complete | `BankrollPortfolioTracker`; Quarter-Kelly; 3% cap; DB-backed exposure |
| Pydantic Gatekeeper | ✅ Complete | `LLMEvaluationResponse` 4-stage validator; 5 safety filters |
| EIP-712 Signer | ✅ Complete | `TransactionSigner`; standard + neg-risk exchange support |
| Nonce Manager | ✅ Complete | `NonceManager`; `asyncio.Lock`; monotonic counter; `pending` block tag |
| Gas Estimator | ✅ Complete | EIP-1559; 15% priority buffer; 500 Gwei ceiling; 50 Gwei fallback |
| Order Broadcaster | ✅ Complete | `OrderBroadcaster`; CLOB POST; receipt polling (30×2s); status machine |
| Structured Logging | ✅ Complete | `structlog`; JSON in prod; console in DEBUG; bridges stdlib |
| Pydantic Settings config | ✅ Complete | `AppConfig`; EIP-55 address validation; SecretStr for keys; 20 env vars |
| Custom exception hierarchy | ✅ Complete | `PolyOracleError` base; `NonceManagerError`, `GasEstimatorError`, `BroadcastError`, `ExposureLimitError`, `WebSocketError`, `RESTClientError` |
| Test suite | ✅ Complete | 92 tests (76 unit + 16 integration); 90% coverage; deterministic with mocked services |
| README | ✅ Complete | Operator onboarding: install → migrate → run → test; Command Validation Checklist |

---

## 3. Work Item Index (WI-01 through WI-10)

| WI | Phase | Title | Core Achievement |
|---|---|---|---|
| WI-01 | P2 | Orchestrator Class Name Fix | Fixed import-crash blocker (`AsyncWebSocketClient` → `CLOBWebSocketClient`, `TxBroadcaster` → `OrderBroadcaster`) |
| WI-02 | P2 | Repository Layer Implementation | Implemented `MarketRepository`, `DecisionRepository`, `ExecutionRepository` with async session injection |
| WI-03 | P2 | Market Discovery Engine | `MarketDiscoveryEngine` with metadata/TTR/exposure filter chain; eliminated hardcoded `condition_id` |
| WI-04 | P2 | Bankroll & Portfolio Tracker | `BankrollPortfolioTracker` with Quarter-Kelly sizing, 3% cap, and DB-backed exposure via repository |
| WI-05 | P2 | dry_run Flag Enforcement | `dry_run=True` blocks all CLOB broadcast side-effects; enforcement in `OrderBroadcaster` |
| WI-06 | P2 | HTTP Library Migration | Migrated `GammaRESTClient` from `aiohttp` to `httpx`; standardized async HTTP stack |
| WI-07 | P2 | Alembic Migrations Setup | `alembic.ini` + `migrations/env.py` + `0001_initial_schema.py`; Alembic is sole schema path |
| WI-08 | P2 | Integration Test Suite | 16 new integration tests (orchestrator, WS client, Claude client, E2E pipeline); 92 total, 90% coverage |
| WI-09 | P3 | Full Repository Pattern Wiring | Eliminated all direct `session.add/execute` in agent code; injected repo factories on all 4 agent clients; bypass regression test added |
| WI-10 | P3 | Production README | README covers install, configure, migrate, run, test, troubleshoot; Command Validation Checklist; operator-complete |
