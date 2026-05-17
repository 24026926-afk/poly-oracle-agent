# Bootstrap Project Scaffolding
Before creating any directory, read these files in order:
1. `QWEN.md` — project rules
2. `AGENTS.md` — protocols
3. `README.md` — stack, architecture, and commands

Then create exactly:
- `src/agents/{context,evaluation,execution,ingestion}/` with `__init__.py` in each
- `src/{core,db,schemas,ui}/` with `__init__.py` where applicable
- `src/db/repositories/` — empty, ready for repository classes
- `tests/{unit,integration}/` — empty, ready for Red Phase
- `docs/deliverables/{business_logic,implementation_prompts}/` — empty, ready for WI markdown files
- `migrations/versions/` — empty, ready for Alembic revisions
- `requirements.txt` with: alembic, pydantic, pydantic-settings, sqlalchemy, aiosqlite, web3, anthropic, websockets, httpx, structlog

Never infer structure. Always derive it from `README.md`, `STATE.md`, and `src/`.
