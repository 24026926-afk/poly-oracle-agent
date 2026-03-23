# P7-WI-03-market-discovery-engine.md
**WI:** WI-03  
**Agent:** Market Discovery Specialist  
**Depends on:** P5, P6  
**Risk:** HIGH  

## Context
Orchestrator still uses single hardcoded condition_id. PRD-v2.0.md WI-03 requires autonomous discovery using Gamma + repository exposure checks.

## Objective
Implement MarketDiscoveryEngine that selects eligible markets and feeds them to orchestrator.

## Exact Files to Touch
- `src/agents/ingestion/market_discovery.py` (new)
- `src/orchestrator.py` — replace hardcoded ID with discovery loop

## Step-by-Step Task
1. Create `MarketDiscoveryEngine` using `GammaRESTClient.get_active_markets()` + filters (active, TTR >= MIN_TTR_HOURS, exposure < MAX_EXPOSURE).
2. Subscribe ingestion layer to discovered condition_ids dynamically.
3. Log and skip if no eligible markets.
4. Add tests with mocked Gamma data.

## Acceptance Criteria (must match PRD exactly)
- [ ] Market discovery uses GammaRESTClient.get_active_markets() with no hardcoded condition_id.
- [ ] Candidate selection applies active status, metadata presence, hours_to_resolution, and exposure limits.
- [ ] Orchestrator subscribes to discovered markets; falls back only with explicit log if none eligible.
- [ ] Tests verify deterministic filtering.

## Hard Constraints
- Never hardcode any condition_id in runtime code after this WI.
- Respect MIN_TTR_HOURS=4.0 from risk_management.md.

## Verification Command
```
python -m pytest tests/unit/test_ingestion.py::TestMarketDiscovery -q --tb=no
```
