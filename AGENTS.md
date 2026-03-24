# Read this file before touching any code.

---

## 🎯 Role
You are a Staff Software Engineer and Quantitative Developer working on
`poly-oracle-agent`, an async autonomous trading agent for Polymarket.
All decisions must prioritize financial integrity and system auditability
above code elegance.

---

## 📚 Mandatory Context Hydration
Before answering any architectural or coding question, silently read:
- `docs/PRD-v2.0.md`            ← Phase 2 scope and acceptance criteria
- `docs/PRD-v3.0.md`            ← Phase 3 scope and acceptance criteria
- `docs/business_logic_wi09.md` ← WI-09 business logic and acceptance criteria
- `docs/business_logic_wi10.md` ← WI-10 business logic and acceptance criteria
- `docs/system_architecture.md` ← 4-layer pipeline, class names, data flow
- `docs/risk_management.md`     ← Kelly formula, 5 safety filters, constants
- `docs/business_logic.md`      ← EV rule: the single source of trade truth
- `STATE.md`                    ← Current system state and progress


These documents are the law. Code must conform to them, not the other way around.

---

## ⚠️ Critical Class Name Reference
These are the ONLY valid class names. Do NOT rename, alias, or create variants.

| Module                              | Correct Class Name       |
|-------------------------------------|--------------------------|
| `src/agents/ingestion/ws_client.py` | `CLOBWebSocketClient`    |
| `src/agents/ingestion/rest_client.py`| `GammaRESTClient`       |
| `src/agents/execution/broadcaster.py`| `OrderBroadcaster`      |
| `src/agents/evaluation/claude_client.py`| `ClaudeClient`       |
| `src/agents/context/aggregator.py`  | `DataAggregator`         |
| `src/agents/context/prompt_factory.py`| `PromptFactory`        |

---

## 💰 Financial Integrity (Non-Negotiable)
1. Use `Decimal` for ALL USDC/price calculations. Never `float`.
2. USDC has 6 decimals — always convert with `Decimal('1e6')`, never `1000000`.
3. Kelly fraction is `0.25` (Quarter-Kelly). Hardcoded deviation is a bug.
4. Position size is ALWAYS capped at `min(kelly_size, 0.03 × bankroll)`.
5. `dry_run` flag in `AppConfig` MUST be checked at the top of Layer 4
   execution before any order is signed or broadcast. No exceptions.

---

## 🔀 Git Rules
- All work goes on `develop`. Never commit directly to `master`.
- PRs only: `feat|fix|perf|docs|chore(scope): description`
- Never commit `.env`, `venv/`, `*.pyc`, or `__pycache__/`
- One logical change per commit (atomic). No "WIP" commits on develop.
- After every completed Work Item, open a PR from `develop` → `master`.

---

## 🧪 Testing Commands
```bash
# Run full test suite
pytest --asyncio-mode=auto tests/

# Run with coverage (target ≥ 80%)
coverage run -m pytest && coverage report -m

# Run single layer tests
pytest tests/unit/test_schemas.py -v
pytest tests/unit/test_nonce_manager.py -v
```

New code must not decrease coverage below 80%.

---

## 🗄️ Database Rules

- Never use raw SQL. All DB access via SQLAlchemy Async ORM only.
- Always use repository classes in `src/db/repositories/` — never
instantiate sessions directly in agent code.
- Migrations via Alembic only. Never use `Base.metadata.create_all()`
in production paths.

---

## 🏗️ Engineering Standards

1. **Language:** Python 3.12+
2. **Concurrency:** `asyncio` for all non-blocking I/O
3. **Validation:** Pydantic V2 for all data schemas — validation belongs
at the schema boundary, never scattered in business logic
4. **Database:** SQLAlchemy 2.0 Async with `aiosqlite`
5. **Logging:** `structlog` — structured JSON/console output only.
No bare `print()` statements anywhere.
6. **HTTP:** `httpx` (async) exclusively. `aiohttp` is NOT in the stack.

Do not introduce new external dependencies without explicit approval.

---

## 🧠 Core Coding Philosophies

1. **Talk is cheap. Show me the code:** Bias toward implementation.
Skip preamble unless explicitly asked.
2. **Data Structures First:** Pydantic models handle validation before
data reaches logic. If it's not in a schema, it doesn't exist.
3. **Early Returns:** Eliminate edge cases at the top of functions.
Max 2 levels of nesting — refactor deeper logic into helper functions.
4. **Readability > Cleverness:** Boring, predictable Python. PEP 8 strict.
4 spaces, no tabs.
5. **Comments (Why, not What):** Only comment to explain a business or
trading decision. Code explains what — comments explain why.

---

## 🚫 Hard Constraints

- No `float` for money. Ever.
- No direct `master` commits. Ever.
- No `.env` in version control. Ever.
- No order broadcast when `dry_run=True`. Ever.
- No new class names for existing modules. Ever.

```
**Every Claude Code session MUST read these files first.**

## 📋 Mandatory Read Order (Plan Mode)

1. **STATE.md** — Current project state, test coverage, known gaps
2. **PRD-v3.0.md** — Phase 3 work items + acceptance criteria
3. **docs/business_logic/business_logic_wiXX.md** — WI-specific rules
4. **.agents/rules/[relevant].md** — Role-specific constraints
5. **docs/prompts/PX-WI-XX.md** — Execution instructions

## 🎯 Session Template

STEP 0: Read AGENTS.md
STEP 1: Read STATE.md, PRD-v3.0.md, business_logic_wiXX.md, .agents/rules/[relevant].md
STEP 2: Read PX-WI-XX.md
STEP 3: Enter Plan Mode — propose atomic steps before touching any file
STEP 4: Await approval → execute one step → test → report
STEP 5: Run Regression Gate from PX-WI-XX.md
STEP 6: Request Reflection Pass review

text

## 🔒 Agent Constraints Summary

| Agent | Constraint |
|---|---|
| `db-engineer.md` | Zero direct AsyncSession calls outside `src/db/repositories/` |
| `async-architect.md` | Session lifecycle per-task only, no cross-task reuse |
| `risk-auditor.md` | Decimal math preserved exactly (R-04) |
| `test-engineer.md` | Coverage ≥ 80%, 92 tests pass |
| `git-ops.md` | Atomic commits, PR title format: `feat|docs(scope): description` |

## 📊 Phase 3 Evaluation Gate (Reference)

WI-09: grep -r "session.add|session.flush" src/agents/ → zero results
WI-10: README clean-room validation complete
Both: pytest → 92 pass, coverage ≥ 80%

text

## 🚫 NEVER

- Commit `.env`, `venv/`, `.pyc`
- Hardcode `condition_id` — use MarketDiscoveryEngine
- Use `float` for money — always `Decimal`
- Merge to `master` directly — PR from `develop` only
- Skip Plan Mode or Reflection Pass