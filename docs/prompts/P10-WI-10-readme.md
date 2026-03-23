# P10-WI-10-readme.md

# P10-WI-10 — Release-Grade README

## Execution Target
- Primary: Claude Code (Plan Mode) — code.claude.com
- Review: Codex Chat Panel (Antigravity IDE)
- Git ops: Codex CLI → `docs(readme): add onboarding, architecture, and ops guide`

## Active Agents
- .agents/rules/db-engineer.md
- .agents/rules/async-architect.md

## Agent Constraints (extracted)
- `.agents/rules/db-engineer.md` — README must document Alembic as sole schema path.
- `.agents/rules/async-architect.md` — All run commands assume single event-loop orchestrator.

## Context
README.md is currently a pre-production blocker containing only partial content. Phase 3 requires one canonical, verified onboarding document that lets a new engineer install, configure, migrate, test, and run the orchestrator in dry_run mode using only the README, .env.example, and STATE.md.

## Plan Mode Instructions
1. Read STATE.md, PRD-v3.0.md, .env.example, pyproject.toml, alembic.ini, src/orchestrator.py, docs/system_architecture.md, and docs/risk_management.md.
2. Draft each of the 10 required sections one by one.
3. For every CLI command in the README, execute it in a clean environment and capture exact output.
4. Cross-check every section against the Consistency Matrix.
5. Output complete README draft before any file write.

## Section-by-Section Instructions
1. Project Overview — use exact Phase 2 metrics from STATE.md (v0.2.0, 92 tests, 91% coverage); do not invent new status.
2. Prerequisites — list only the secrets and connectivity from .env.example; reference wallet checksum validation.
3. Installation — exact venv + `pip install -e .` commands; mention editable vs non-editable.
4. Database Setup — state “Alembic is the ONLY supported path” and show `alembic upgrade head`.
5. Configuration — copy the full 22-variable table from business_logic_wi10.md; mark DRY_RUN=true as mandatory for local/CI.
6. Running the Agent — exact `python -m src.orchestrator` command + what happens at startup (discovery + 5 tasks).
7. Running Tests — exact pytest and coverage commands with expected 92 tests / ≥80% coverage.
8. Git Workflow — copy exact branching/PR rules from current README + STATE.md.
9. Architecture Overview — embed 4-layer Mermaid from docs/system_architecture.md; add pipeline diagram reference.
10. Operational Notes — mandatory DRY_RUN banner, safety expectations, basic troubleshooting.

## Validation Pass (Claude Code runs this AFTER draft)
Execute every command from the Command Validation Checklist in order and confirm each produces the exact expected output signal.

## Consistency Check (Claude Code runs this LAST)
Diff the generated README against:
- STATE.md (version, test count, coverage)
- pyproject.toml / alembic.ini
- src/orchestrator.py
- .env.example (exact 22 variables)
- docs/system_architecture.md
- docs/risk_management.md
Resolve every discrepancy before final write.

## Step 5b — Reflection Pass (NEW)
Tool: Codex Chat Panel (Antigravity)
Prompt: "Review the changes made in this session against:
  1. business_logic_wi10.md — did every rule get implemented?
  2. .agents/rules/db-engineer.md — any violations?
  3. PRD-v3.0 acceptance criteria — all met?
List any gaps before I approve the commit."

## Git Commit (Codex CLI)
`docs(readme): add onboarding, architecture, and ops guide`