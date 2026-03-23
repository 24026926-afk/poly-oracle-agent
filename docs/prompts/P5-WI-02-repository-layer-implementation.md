# P5-WI-02-repository-layer-implementation.md
**WI:** WI-02  
**Agent:** Repository Specialist  
**Depends on:** P1, P3  
**Risk:** HIGH  

## Context
`src/db/repositories/` three files are empty stubs. PRD-v2.0.md WI-02 requires stable abstraction before bankroll/portfolio and market discovery.

## Objective
Implement async repository classes for all three tables with methods needed by later WIs.

## Exact Files to Touch
- `src/db/repositories/market_repo.py`
- `src/db/repositories/decision_repo.py`
- `src/db/repositories/execution_repo.py`
- `src/db/repositories/__init__.py`

## Step-by-Step Task
1. Create `MarketRepository` with `insert_snapshot`, `get_latest_by_condition_id`.
2. Create `DecisionRepository` with `insert_decision`, `get_recent_by_market`.
3. Create `ExecutionRepository` with `insert_execution`, `get_by_decision_id`, `get_aggregate_exposure`.
4. Update orchestrator and agents to inject repositories instead of raw sessions.
5. Add unit tests against async SQLite.

## Acceptance Criteria (must match PRD exactly)
- [ ] The three repository modules contain async repository implementations.
- [ ] Repository methods cover snapshot insert/lookup, decision insert/recent lookup, execution insert/update, and aggregate exposure queries.
- [ ] Runtime code outside `src/db/` uses repository methods only.
- [ ] Repository unit tests run against an async SQLite test database.

## Hard Constraints
- All DB access via repositories — never direct sessions in agent code.
- Use `Decimal` for exposure calculations.

## Verification Command
```
python -m pytest tests/unit/test_ingestion.py tests/unit/test_broadcaster.py -q --tb=no
```
