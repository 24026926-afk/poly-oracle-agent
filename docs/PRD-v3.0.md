# PRD v3.0 - Poly-Oracle-Agent Phase 3

Source inputs: `PRD-v2.0.md`, `STATE.md`, and `README.md`.

## 1. Executive Summary

`poly-oracle-agent` is an autonomous AI trading agent for Polymarket. The system ingests live CLOB WebSocket market data, enriches market context, evaluates opportunities with Claude under a Pydantic gatekeeper, signs EIP-712 orders, and executes on Polygon PoS through a fully async four-layer pipeline.

Current maturity at the end of Phase 2:
- 4-layer async pipeline implemented end to end
- Phase 2 work items WI-01 through WI-08 delivered
- 92 automated tests passing
- 91% total coverage
- Async database layer, repositories, market discovery, bankroll tracking, dry-run enforcement, Alembic, and integration coverage are present

Phase 3 goal:
- Remove the final pre-production blockers without adding new product features
- Standardize all runtime persistence access behind the repository layer
- Deliver release-grade operator and developer documentation in `README.md`

Phase 3 is complete when:
- No agent runtime path bypasses the repository layer for the three persisted domains
- `README.md` is sufficient for a new engineer to install, configure, migrate, run, validate, and troubleshoot the system without reading source code first
- The existing automated test baseline continues to pass with no reduction in overall coverage below 80%

## 2. Phase 3 Objectives

Phase 3 scope is limited to the two known gaps below and does not expand the trading feature set.

### Objective 1 — Repository Pattern Completion
Finish wiring the repository pattern across all agent runtime code so persistence logic for `market_snapshots`, `agent_decision_logs`, and `execution_txs` is accessed only through `MarketRepository`, `DecisionRepository`, and `ExecutionRepository`.

### Objective 2 — Production README
Bring `README.md` to release-ready quality so it functions as the canonical onboarding and operations document. It must cover architecture, prerequisites, environment setup, migrations, run modes, test execution, and operational safety guidance.

## 3. Work Items

### WI-09: Full Repository Pattern Wiring Across All Agent Code

**Title**  
Full repository pattern wiring across runtime agent code

**Goal**  
Eliminate direct SQLAlchemy session usage for application persistence logic outside `src/db/repositories/` and make repositories the only runtime access path for the three persisted domains.

**Problem Statement**  
Phase 2 implemented repository classes, but the current state still identifies repository wiring as incomplete. Some agent code continues to use direct SQLAlchemy session access instead of repository methods. This leaves persistence behavior split across two patterns and increases the risk of inconsistent queries, duplicated logic, and regression in transaction handling.

**Acceptance Criteria**
1. All runtime reads and writes for `market_snapshots`, `agent_decision_logs`, and `execution_txs` are performed through:
   - `MarketRepository`
   - `DecisionRepository`
   - `ExecutionRepository`
2. No runtime module outside `src/db/repositories/` performs ad hoc persistence for those three domains using direct SQLAlchemy session calls such as `add`, `execute`, `scalar`, `scalars`, `flush`, `refresh`, or inline ORM queries.
3. Runtime modules that persist or query the three domains receive repository instances through constructor or function injection rather than constructing query logic inline.
4. Existing behavior is preserved:
   - snapshots are still persisted before downstream queue handoff
   - decision logs are still persisted before execution routing
   - execution attempts and receipts are still persisted with the same status model
   - bankroll exposure calculations continue to use persisted `PENDING` and `CONFIRMED` executions
5. Automated tests prove repository wiring behavior:
   - existing unit and integration suites pass
   - at least one targeted regression test fails if a runtime persistence path bypasses repositories
   - coverage remains at or above 80%
6. Search-based verification across `src/` shows no direct SQLAlchemy persistence/query logic for the three domains outside `src/db/repositories/`, excluding session factory setup in `src/db/engine.py`.

**Affected Files**
- `src/db/repositories/market_repo.py`
- `src/db/repositories/decision_repo.py`
- `src/db/repositories/execution_repo.py`
- `src/db/repositories/__init__.py`
- `src/agents/ingestion/ws_client.py`
- `src/agents/evaluation/claude_client.py`
- `src/agents/execution/broadcaster.py`
- `src/agents/execution/bankroll_tracker.py`
- `src/orchestrator.py`
- `tests/unit/test_repositories.py`
- `tests/integration/test_pipeline_e2e.py`
- `tests/integration/test_orchestrator.py`
- any additional runtime modules under `src/agents/` that currently access `AsyncSession` directly for the three persisted domains

### WI-10: README.md — Onboarding, Architecture Overview, Environment Setup, and Run Instructions

**Title**  
Release-grade README for onboarding and operations

**Goal**  
Make `README.md` the canonical setup and operations document for engineers running or validating the service.

**Problem Statement**  
Project documentation remains a pre-production blocker. Even where baseline README content exists, Phase 3 requires one authoritative document with verified setup steps, architecture summary, environment requirements, migration commands, run instructions, test commands, and safety guidance. Live trading should not depend on institutional knowledge or source-code inspection.

**Acceptance Criteria**
1. `README.md` contains, at minimum, the following sections:
   - Project overview
   - Architecture overview of the 4-layer async pipeline
   - Prerequisites
   - Installation
   - Environment setup using `.env.example`
   - Database migration/setup using Alembic
   - Run instructions for the orchestrator
   - Test execution instructions
   - `dry_run` usage and safety expectations
   - Operational notes and troubleshooting basics
2. Every command in `README.md` is validated against the current repo layout and works from repository root in a clean environment, subject to required secrets and network endpoints.
3. `README.md` documents all required environment variables and distinguishes required secrets from tunable runtime settings.
4. `README.md` explicitly states:
   - the system is not live-trading ready until Phase 3 success criteria are met
   - `DRY_RUN=true` is the required default for local development, CI, and validation runs
   - Alembic is the supported schema-management path
5. The README content is internally consistent with:
   - `STATE.md`
   - `.env.example`
   - `pyproject.toml`
   - `migrations/`
   - `src/orchestrator.py`
   - current test commands
6. A clean-room validation is completed by a reviewer or test procedure that follows `README.md` to:
   - create an environment
   - install dependencies
   - apply migrations
   - run the test suite
   - start the orchestrator in `dry_run` mode
   The validation result must be recorded in the Phase 3 completion notes.

**Affected Files**
- `README.md`
- `.env.example`
- `pyproject.toml`
- `migrations/env.py`
- `alembic.ini`
- `src/orchestrator.py`
- `tests/`
- `docs/system_architecture.md`
- `docs/risk_management.md`
- any other file that must be updated solely to keep `README.md` accurate and non-contradictory

## 4. Out of Scope

Phase 3 does **not** include any of the following:
- New trading strategies, signals, or opportunity-selection logic
- Changes to gatekeeper thresholds, Kelly sizing math, or bankroll policy
- New exchanges, wallets, chains, or settlement paths
- New persistence models or schema redesign beyond repository wiring required to preserve existing behavior
- UI, dashboards, monitoring products, or alerting systems
- Performance optimization work not required to complete WI-09
- Additional LLM providers, model changes, or prompt redesign
- New scripts, developer tooling, or seed utilities beyond what is required to document the current system accurately
- Live trading enablement or production rollout execution

## 5. Success Criteria for Phase 3

Phase 3 is done when all of the following are true:

1. **Repository compliance**
   - Runtime code outside `src/db/repositories/` no longer bypasses repositories for `market_snapshots`, `agent_decision_logs`, or `execution_txs`.
   - Persistence behavior remains functionally equivalent to Phase 2.

2. **Documentation completeness**
   - `README.md` is complete, accurate, and validated against the current codebase.
   - A new engineer can bootstrap the project from repository root using only `README.md`, `.env.example`, and standard credentials.

3. **Regression safety**
   - Full automated test suite passes.
   - Coverage remains at or above 80%.
   - Existing end-to-end dry-run integration behavior remains intact.

4. **Pre-live-trading readiness**
   - All known Phase 3 blockers are closed.
   - Operator instructions for installation, migration, configuration, dry-run validation, and startup are explicit and unambiguous.
   - There is no remaining known runtime path where live trading behavior can diverge from documented behavior because of persistence-layer inconsistency.

## 6. Risk Register

The primary implementation risk in Phase 3 is WI-09 repository migration. The table below captures the expected risks and required mitigations.

| Risk ID | Risk | Cause | Impact | Mitigation | Residual Level |
|---|---|---|---|---|---|
| R-01 | Behavior regression during repository refactor | Query logic is moved out of runtime modules and rewritten incorrectly | Missing or incorrect snapshot, decision, or execution persistence; silent trading-state drift | Preserve existing method contracts, add targeted regression tests for each migrated path, compare persisted rows before/after refactor in integration tests | Medium |
| R-02 | Transaction-boundary changes | Repositories may flush or query at different points than direct-session code | Queue handoff may occur before persistence is durable, or downstream code may observe stale state | Keep flush/commit timing explicit, verify persistence before queue routing in tests, avoid hidden commit behavior inside repositories | Medium |
| R-03 | Session lifecycle misuse | Repository injection may introduce invalid session ownership or reuse across async tasks | Leaked sessions, concurrency bugs, or inconsistent reads under load | Keep `AsyncSession` scope explicit, document repository construction rules, ensure per-task/per-request session boundaries remain unchanged | Medium |
| R-04 | Exposure calculation drift | Bankroll and exposure paths depend on repository queries; migration may alter filters or numeric handling | Incorrect position sizing or false exposure-limit decisions | Preserve `PENDING` + `CONFIRMED` exposure semantics, assert `Decimal` handling in tests, keep aggregate-exposure tests green | High |
| R-05 | False completion signal | Code appears repository-compliant but an unreviewed runtime path still issues direct SQLAlchemy queries | Hidden persistence divergence remains in production code | Add search-based verification over `src/`, require reviewer sign-off on all persistence-touching runtime modules, include explicit acceptance criterion for zero bypasses | Medium |
| R-06 | Documentation lags implementation | WI-09 changes runtime construction or setup details without synchronized README updates | Onboarding steps become inaccurate and operators run the service incorrectly | Treat README updates as required completion work for WI-09, validate commands and run modes from a clean environment before closing Phase 3 | Low |
