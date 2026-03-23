# P9-WI-09-repo-wiring.md

# P9-WI-09 — Full Repository Pattern Wiring

## Execution Target
- Primary: Claude Code (Plan Mode) — code.claude.com
- Review: Codex Chat Panel (Antigravity IDE)
- Git ops: Codex CLI → `feat(db): wire repository pattern across all agent modules`

## Active Agents
- .agents/rules/db-engineer.md
- .agents/rules/async-architect.md

## Agent Constraints (extracted — do not override)
- `.agents/rules/db-engineer.md` — ALL persistence MUST route exclusively through the three repositories; zero direct AsyncSession calls outside `src/db/repositories/`.
- `.agents/rules/async-architect.md` — Session lifecycle strictly per-task; no cross-task reuse or leakage.

## Context
Repositories (MarketRepository, DecisionRepository, ExecutionRepository) are fully implemented in src/db/repositories/ with correct insert and query methods, but runtime modules in the four-layer pipeline still bypass them with direct AsyncSession.add(), flush(), execute(), scalar(), etc. This WI eliminates every bypass for market_snapshots, agent_decision_logs, and execution_txs, enforces constructor injection, and guarantees identical persistence timing and semantics (especially bankroll exposure).

## Pre-Flight Checklist (Claude Code runs this FIRST)
1. `grep -r "AsyncSession" src/agents/ --include="*.py"`
2. `grep -r "session\." src/agents/ --include="*.py" | grep -E "(add|flush|execute|scalar|scalars)"`
3. `grep -r "from sqlalchemy.ext.asyncio" src/agents/`
4. `grep -r "get_aggregate_exposure" src/`
5. `grep -r "session.add" src/orchestrator.py`
6. `grep -r "market_snapshots|agent_decision_logs|execution_txs" src/agents/`
7. Confirm exactly five files contain direct session persistence before any edit.

## Plan Mode Instructions
1. Read the three repository files, src/db/engine.py, src/db/repositories/__init__.py, and the five affected runtime modules.
2. Propose a complete atomic multi-file plan (one change per module) before touching any file.
3. For each module: add required imports, update __init__ or constructor signature, inject repo instances.
4. Replace every direct session call with the exact repository method.
5. Update orchestrator.py to create repos inside per-task async context managers.
6. Preserve exact flush/commit timing before every queue handoff.
7. Output the full plan with file-by-file diffs (no code yet) and wait for confirmation.

## Per-Module Implementation Steps

### ws_client.py
Inject MarketRepository via constructor. Remove all direct AsyncSession usage. Replace snapshot persistence block with single call to repo.insert_snapshot(snapshot). Ensure persistence completes before enqueuing to market_queue.

### claude_client.py
Inject DecisionRepository via constructor. Replace direct decision persistence with repo.insert_decision(decision). Keep retry loop and queue routing exactly as-is.

### broadcaster.py
Inject ExecutionRepository via constructor. Replace all execution persistence (insert + status updates) with repo.insert_execution(execution). Preserve PENDING → CONFIRMED/REVERTED transitions before and after CLOB broadcast.

### bankroll_tracker.py
Inject ExecutionRepository via constructor. Replace any inline exposure query with exact call: `exposure = await repo.get_aggregate_exposure(condition_id)`. Preserve verbatim the Decimal math rule: exposure = ExecutionRepository.get_aggregate_exposure(condition_id) → SUM(PENDING + CONFIRMED) cast to Decimal(str(total or 0)).

### orchestrator.py
Add creation of all three repositories inside each layer task using `async with get_db_session() as session: repo = MarketRepository(session)`. Pass repos via constructor to WSClient, ClaudeClient, OrderBroadcaster, and BankrollPortfolioTracker. Remove any ad-hoc session usage.

## Regression Gate (Claude Code runs this AFTER all edits)
1. Run before/after row comparison in integration tests for snapshots, decisions, and txs (content + timestamps identical).
2. Confirm E2E pipeline test still proves persistence BEFORE every queue handoff.
3. Verify BankrollPortfolioTracker exposure test returns identical Decimal value.
4. Run `grep -r "session\.(add|flush|execute|scalar)" src/agents/ --include="*.py"` and confirm zero matches outside repositories.
5. Full test suite passes, coverage ≥80%, and targeted bypass test fails if any leak is re-introduced.

## Step 5b — Reflection Pass (NEW)
Tool: Codex Chat Panel (Antigravity)
Prompt: "Review the changes made in this session against:
  1. business_logic_wi09.md — did every rule get implemented?
  2. .agents/rules/db-engineer.md — any violations?
  3. PRD-v3.0 acceptance criteria — all met?
List any gaps before I approve the commit."

## Git Commit Sequence (Codex CLI)
1. `feat(repo): wire MarketRepository into ws_client.py`
2. `feat(repo): wire DecisionRepository into claude_client.py`
3. `feat(repo): wire ExecutionRepository into broadcaster.py`
4. `feat(repo): wire ExecutionRepository into bankroll_tracker.py`
5. `feat(repo): inject all three repositories in orchestrator.py`
6. `feat(repo): complete repository pattern wiring across runtime`
Final PR title: `feat(db): wire repository pattern across all agent modules`