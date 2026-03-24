# P4-WI-07-alembic-migrations-setup.md
**WI:** WI-07  
**Agent:** DB Specialist  
**Depends on:** None  
**Risk:** MEDIUM  

## Context
`migrations/env.py` is empty and schema creation still uses raw `Base.metadata.create_all()` in scripts. PRD-v2.0.md requires versioned migrations for production safety.

## Objective
Configure Alembic for async SQLAlchemy and create initial baseline migration for the existing three-table schema.

## Exact Files to Touch
- `migrations/env.py` — configure for async + project metadata
- `migrations/versions/0001_initial_schema.py` — autogenerate
- `scripts/init_db.py` — replace create_all() with alembic upgrade head

## Step-by-Step Task
1. Update `migrations/env.py` to use `asyncio` engine and import models from `src.db.models`.
2. Run `alembic revision --autogenerate -m "initial schema"` (manual step for Codex).
3. Modify `scripts/init_db.py` to use `alembic upgrade head` instead of `create_all()`.
4. Add smoke test for upgrade/downgrade.

## Step 5b — Reflection Pass (NEW)
Tool: Codex Chat Panel (Antigravity)
Prompt: "Review the changes made in this session against:
  1. business_logic_wi07.md — did every rule get implemented?
  2. .agents/rules/db-engineer.md — any violations?
  3. PRD-v3.0 acceptance criteria — all met?
List any gaps before I approve the commit."

## Acceptance Criteria (must match PRD exactly)
- [ ] Alembic is configured against the project's SQLAlchemy metadata and can run in the project environment.
- [ ] An initial baseline migration exists for the current three-table schema.
- [ ] Running `alembic upgrade head` against an empty database produces the expected schema successfully.
- [ ] A migration smoke test performs upgrade, downgrade, and re-upgrade successfully in a test environment.
- [ ] Managed environment setup documentation and scripts use Alembic migrations instead of raw `create_all()`.

## Hard Constraints
- Never use `Base.metadata.create_all()` in production paths.
- All DB access via repositories after this WI.

## Verification Command
```
alembic upgrade head && alembic downgrade base && alembic upgrade head && echo "Migration smoke test passed"
```
