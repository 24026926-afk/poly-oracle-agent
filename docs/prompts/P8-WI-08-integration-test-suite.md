# P8-WI-08-integration-test-suite.md
**WI:** WI-08  
**Agent:** Test Specialist  
**Depends on:** P1-P7  
**Risk:** HIGH  

## Context
Integration tests are empty stubs. PRD-v2.0.md WI-08 requires end-to-end proof + ≥80% coverage after all prior work items.

## Objective
Implement full integration test suite covering the wired pipeline in dry_run mode.

## Exact Files to Touch
- `tests/conftest.py` — async fixtures (DB, queues, mocked external)
- `tests/integration/test_orchestrator.py` (new)
- `tests/integration/test_ws_client.py`
- `tests/integration/test_claude_client.py`

## Step-by-Step Task
1. Build shared fixtures in conftest.py for mocked Gamma/Anthropic/CLOB/Polygon.
2. Write tests for startup/shutdown, full queue flow, dry_run trade path, market discovery, repository persistence.
3. Run coverage and assert ≥80%.
4. Update README.md with test command.

## Step 5b — Reflection Pass (NEW)
Tool: Codex Chat Panel (Antigravity)
Prompt: "Review the changes made in this session against:
  1. business_logic_wi08.md — did every rule get implemented?
  2. .agents/rules/db-engineer.md — any violations?
  3. PRD-v3.0 acceptance criteria — all met?
List any gaps before I approve the commit."

## Acceptance Criteria (must match PRD exactly)
- [ ] tests/conftest.py provides shared async fixtures for isolated DB, config overrides, mocked services, queue/orchestrator bootstrapping.
- [ ] Integration tests cover orchestrator startup/shutdown, queue handoff, dry_run trade, market discovery, repository assertions.
- [ ] Suite runs without live network and passes with one command.
- [ ] Coverage reporting shows total project coverage ≥80%.

## Hard Constraints
- All external calls mocked — zero live API hits.
- dry_run=True enforced in every execution test.
- P1-P7 must be complete before this WI.

## Verification Command
```
coverage run -m pytest tests/integration/ --asyncio-mode=auto && coverage report -m | grep "TOTAL" | grep -E "80|81|82|83|84|85|86|87|88|89|90|91|92|93|94|95|96|97|98|99|100"
```
