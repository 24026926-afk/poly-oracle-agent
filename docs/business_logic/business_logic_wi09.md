# WI-09 Business Logic — Full Repository Pattern Wiring

## Active Agents + Constraints

- `.agents/rules/db-engineer.md` — ALL persistence MUST route exclusively through the three repositories; zero direct AsyncSession calls outside `src/db/repositories/`.
- `.agents/rules/async-architect.md` — Session lifecycle strictly per-task; no cross-task reuse or leakage.
- `.agents/rules/risk-auditor.md` — R-04 HIGH: Bankroll and exposure paths depend on repository queries; migration may alter filters or numeric handling.

## 1. Acceptance Criteria

### 1.1 Repository Compliance
All runtime reads and writes for `market_snapshots`, `agent_decision_logs`, and `execution_txs` are performed through:
- `MarketRepository`
- `DecisionRepository`
- `ExecutionRepository`

No runtime module outside `src/db/repositories/` performs ad hoc persistence for those three domains using direct SQLAlchemy session calls such as `add`, `execute`, `scalar`, `scalars`, `flush`, `refresh`, or inline ORM queries.

### 1.2 Injection Pattern
Runtime modules that persist or query the three domains receive repository instances through constructor or function injection rather than constructing query logic inline.

Pattern: `Callable[[AsyncSession], RepositoryClass] = RepositoryClass` with defaults for production use.

**All four agent clients** implement this pattern:
- `CLOBWebSocketClient`: accepts `market_repo_factory: Callable[[AsyncSession], MarketRepository] = MarketRepository`
- `ClaudeClient`: accepts `decision_repo_factory: Callable[[AsyncSession], DecisionRepository] = DecisionRepository`
- `OrderBroadcaster`: accepts `execution_repo_factory: Callable[[AsyncSession], ExecutionRepository] = ExecutionRepository`
- `BankrollPortfolioTracker`: accepts `execution_repo_factory: Callable[[AsyncSession], ExecutionRepository] = ExecutionRepository`

### 1.3 Behavior Preservation
- Snapshots are still persisted before downstream queue handoff
- Decision logs are still persisted before execution routing
- Execution attempts and receipts are still persisted with the same status model
- Bankroll exposure calculations continue to use persisted `PENDING` and `CONFIRMED` executions
- All flush/commit timing remains explicit and happens before queue routing

### 1.4 Test Coverage
- Existing unit and integration suites pass (92 tests)
- Coverage remains at or above 80%
- Search-based verification shows zero direct SQLAlchemy persistence/query logic for the three domains outside `src/db/repositories/`, excluding session factory setup in `src/db/engine.py`

## 2. Section 6 — Decimal Math for Exposure Calculation

### 2.1 Rule: Exposure Aggregation via ExecutionRepository
`BankrollPortfolioTracker.get_exposure(condition_id)` MUST use ONLY `ExecutionRepository.get_aggregate_exposure(condition_id)`.

```python
# CORRECT:
async with self._db_factory() as session:
    repo = self._execution_repo_factory(session)
    exposure = await repo.get_aggregate_exposure(condition_id)
```

### 2.2 Aggregate Exposure Query Contract
`ExecutionRepository.get_aggregate_exposure(condition_id) → Decimal`

Must:
1. Sum `ExecutionTx.size_usdc` for rows where `condition_id` matches AND `status` IN (`PENDING`, `CONFIRMED`)
2. Return `Decimal("0")` when no matching rows exist
3. Cast the raw float result from SQLite/Postgres through `str()` before converting to Decimal to avoid float precision contamination

```python
stmt = (
    select(func.sum(ExecutionTx.size_usdc))
    .where(ExecutionTx.condition_id == condition_id)
    .where(ExecutionTx.status.in_([TxStatus.PENDING, TxStatus.CONFIRMED]))
)
result = await self._session.execute(stmt)
raw = result.scalar_one_or_none()

if raw is None:
    return Decimal("0")

# CRITICAL: Cast via str() to avoid float → Decimal precision loss
return Decimal(str(raw))
```

### 2.3 Exposure State Machine
- **PENDING** executions count toward current exposure (unfilled orders waiting on chain)
- **CONFIRMED** executions count toward current exposure (filled orders on-chain)
- **REVERTED** executions do NOT count (reverted transactions)
- **FAILED** executions do NOT count (never submitted)

### 2.4 Position Sizing with Exposure
`BankrollPortfolioTracker.compute_position_size(kelly_fraction_raw, condition_id) → Decimal`

Must:
1. Call `get_total_bankroll()` → returns `config.initial_bankroll_usdc` (Decimal)
2. Call `get_exposure(condition_id)` → returns Decimal via `ExecutionRepository.get_aggregate_exposure()`
3. Apply Quarter-Kelly: `f_quarter = 0.25 × f*` where `f*` is the raw Kelly fraction from EV calc
4. Apply 3% exposure cap: `exposure_cap = 0.03 × bankroll`
5. Final size: `min(kelly_size, exposure_cap)` where `kelly_size = f_quarter × bankroll`
6. Floor at zero: `max(final_size, Decimal("0"))`

```python
bankroll = await self.get_total_bankroll()  # Decimal
kelly_frac = Decimal(str(self._config.kelly_fraction))  # 0.25 (Quarter-Kelly)
kelly_size = kelly_frac * kelly_fraction_raw * bankroll
exposure_cap = Decimal(str(self._config.max_exposure_pct)) * bankroll  # 0.03 × bankroll
position_size = min(kelly_size, exposure_cap)
position_size = max(position_size, Decimal("0"))
return position_size
```

### 2.5 Type Safety Invariant
**All math uses `Decimal`. Never `float`.**

- `kelly_fraction` (config): Decimal or cast to Decimal via `Decimal(str(...))`
- `initial_bankroll_usdc` (config): Decimal
- `max_exposure_pct` (config): Decimal or cast via `Decimal(str(...))`
- `get_aggregate_exposure()` return: Decimal
- `get_total_bankroll()` return: Decimal
- All intermediate calculations: Decimal
- All returns: Decimal

## 3. Repository Wiring Map

### 3.1 CLOBWebSocketClient (Layer 1)
- **Domain**: `market_snapshots`
- **Methods used**: `MarketRepository.insert_snapshot(snapshot)`
- **Injection**: `market_repo_factory: Callable[[AsyncSession], MarketRepository] = MarketRepository`
- **Flow**: Validate frame → build MarketSnapshot → `repo.insert_snapshot(row)` → commit → enqueue

### 3.2 ClaudeClient (Layer 3)
- **Domain**: `agent_decision_logs`
- **Methods used**: `DecisionRepository.insert_decision(decision)`
- **Injection**: `decision_repo_factory: Callable[[AsyncSession], DecisionRepository] = DecisionRepository`
- **Flow**: Parse Claude response → validate via Gatekeeper → build AgentDecisionLog → `repo.insert_decision(log)` → commit → route

### 3.3 OrderBroadcaster (Layer 4)
- **Domain**: `execution_txs`
- **Methods used**:
  - `ExecutionRepository.insert_execution(execution)`
  - `ExecutionRepository.update_execution_status(...)`
- **Injection**: `execution_repo_factory: Callable[[AsyncSession], ExecutionRepository] = ExecutionRepository`
- **Flow**: Build ExecutionTx (PENDING) → insert → commit → POST CLOB → poll receipt → update status → commit

**Critical: `size_usdc` Decimal Math (Section 2.5 Invariant)**
```python
# In _build_execution_row(): MUST use Decimal, never float
from decimal import Decimal
size_usdc = Decimal(str(order.maker_amount)) / Decimal('1e6')
```
This ensures financial integrity—prevents float precision loss when converting from integer microUSDC to USDC.

### 3.4 BankrollPortfolioTracker (Bankroll queries)
- **Domain**: `execution_txs` (query only, no writes)
- **Methods used**: `ExecutionRepository.get_aggregate_exposure(condition_id)`
- **Injection**: `execution_repo_factory: Callable[[AsyncSession], ExecutionRepository] = ExecutionRepository`
- **Flow**: Get exposure via repository → compute available bankroll → validate trade size

## 4. No Orchestrator Changes

The orchestrator does NOT explicitly pass repo factories to any agent client. All four classes have defaults (`= MarketRepository`, `= DecisionRepository`, `= ExecutionRepository`) that handle production wiring. This keeps orchestrator coupling minimal and makes tests injectable.

```python
# Orchestrator construction — no repo factory params needed
self.ws_client = CLOBWebSocketClient(
    config=self.config,
    queue=self.market_queue,
    db_session_factory=AsyncSessionLocal,
    # market_repo_factory defaults to MarketRepository
)

self.claude_client = ClaudeClient(
    in_queue=self.prompt_queue,
    out_queue=self.execution_queue,
    config=self.config,
    db_session_factory=AsyncSessionLocal,
    # decision_repo_factory defaults to DecisionRepository
)

self.broadcaster = OrderBroadcaster(
    w3=self.w3,
    nonce_manager=self.nonce_manager,
    gas_estimator=self.gas_estimator,
    http_session=self._http_session,
    db_session_factory=AsyncSessionLocal,
    clob_rest_url=self.config.clob_rest_url,
    config=self.config,
    bankroll_tracker=self.bankroll_tracker,
    # execution_repo_factory defaults to ExecutionRepository
)
```

## 5. Verification Checklist

### 5.1 Code Search
- `grep -r "session\.(add|flush|execute|scalar|scalars)" src/agents/` → **zero results**
- `grep -r "MarketRepository(session)\|DecisionRepository(session)\|ExecutionRepository(session)" src/` → **zero results** (except in test setup)

### 5.2 Test Results
- `pytest --asyncio-mode=auto tests/` → **92 tests pass**
- `coverage report -m` → **≥ 80%** (90% achieved)
- No regressions in existing test behavior

### 5.3 Pattern Consistency
All four agent clients follow the same injection pattern:
- Constructor parameter: `{entity}_repo_factory: Callable[[AsyncSession], RepositoryClass] = RepositoryClass`
- Storage: `self._{entity}_repo_factory = {entity}_repo_factory`
- Usage: `repo = self._{entity}_repo_factory(session)` inside `async with self._db_factory() as session:`

### 5.4 Decimal Math Verification
- OrderBroadcaster `_build_execution_row()` uses `Decimal(str(order.maker_amount)) / Decimal('1e6')` for `size_usdc` ✓
- No float division for USDC calculations anywhere in agent code ✓
- All tests in `tests/unit/test_broadcaster.py` pass with Decimal implementation ✓

## 6. Risk Mitigations (from PRD-v3.0 R-04)

**R-04 HIGH: Exposure calculation drift**

Mitigations applied:
- Preserve `PENDING` + `CONFIRMED` exposure semantics exactly (no filter changes)
- Assert `Decimal` handling in tests (test_restart_recovery_from_db, test_all_math_uses_decimal)
- Keep aggregate-exposure tests green (test_aggregate_exposure_sums_pending_confirmed, test_aggregate_exposure_returns_zero_on_empty)
- BankrollPortfolioTracker.get_exposure() delegates to ExecutionRepository.get_aggregate_exposure() without modification
- All position sizing uses Decimal throughout
