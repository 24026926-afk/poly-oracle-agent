
```markdown
# business_logic_wi10.md

## Active Agents + Constraints
- `.agents/rules/db-engineer.md` — README must document Alembic as sole schema path.  
- `.agents/rules/async-architect.md` — All run commands assume single event-loop orchestrator.

## 1. README Section Map
1. Project Overview — purpose, current phase, test/coverage metrics.  
2. Prerequisites — Python, secrets, RPC access.  
3. Installation — venv + editable pip install.  
4. Database Setup — alembic upgrade head only.  
5. Configuration — .env copy + full var list.  
6. Running the Agent — orchestrator command + startup flow.  
7. Running Tests — pytest + coverage commands.  
8. Git Workflow — develop → PR → main rules.  
9. Architecture Overview — 4-layer pipeline summary + Mermaid reference.  
10. Operational Notes — DRY_RUN mandatory, troubleshooting, safety.

## 2. Environment Variable Classification Table
| Variable                  | Classification      | Notes                     |
|---------------------------|---------------------|---------------------------|
| ANTHROPIC_API_KEY        | Required Secret    | Anthropic key            |
| ANTHROPIC_MODEL          | Required Config    | Model ID                 |
| ANTHROPIC_MAX_TOKENS     | Optional Tunable   | Token limit              |
| ANTHROPIC_MAX_RETRIES    | Optional Tunable   | Retry count              |
| POLYGON_RPC_URL          | Required Config    | RPC endpoint             |
| WALLET_ADDRESS           | Required Config    | EIP-55 checksum          |
| WALLET_PRIVATE_KEY       | Required Secret    | Signing key              |
| CLOB_REST_URL            | Required Config    | Order API                |
| CLOB_WS_URL              | Required Config    | Market stream            |
| GAMMA_API_URL            | Required Config    | Metadata                 |
| KELLY_FRACTION           | Optional Tunable   | Risk param               |
| MIN_CONFIDENCE           | Optional Tunable   | Gatekeeper               |
| MAX_SPREAD_PCT           | Optional Tunable   | Gatekeeper               |
| MAX_EXPOSURE_PCT         | Optional Tunable   | Bankroll cap             |
| MIN_EV_THRESHOLD         | Optional Tunable   | Gatekeeper               |
| MIN_TTR_HOURS            | Optional Tunable   | Discovery                |
| MAX_GAS_PRICE_GWEI       | Optional Tunable   | Safety ceiling           |
| FALLBACK_GAS_PRICE_GWEI  | Optional Tunable   | Fallback                 |
| DATABASE_URL             | Required Config    | SQLite path              |
| LOG_LEVEL                | Optional Tunable   | structlog level          |
| DRY_RUN                  | Optional Tunable   | Safety flag (default true)|
| INITIAL_BANKROLL_USDC    | Optional Tunable   | Starting capital         |

## 3. Command Validation Checklist
| Command                                      | Working Directory | Expected Output Signal                     |
|----------------------------------------------|-------------------|--------------------------------------------|
| `cp .env.example .env`                      | repo root        | .env file created                          |
| `alembic upgrade head`                      | repo root        | "No new upgrade" or schema applied + tables visible |
| `python -m src.orchestrator`                | repo root        | "Starting Poly-Oracle-Agent" + discovery logs |
| `pytest --asyncio-mode=auto tests/`         | repo root        | 92 passed, no failures                     |
| `coverage run -m pytest && coverage report` | repo root        | 91%+ coverage, no missing lines            |
| `DRY_RUN=true python -m src.orchestrator`   | repo root        | Dry-run banner + no broadcast calls        |

All commands validated from clean venv against current repo layout.

## 4. Consistency Matrix
| README Section          | Must Sync With                          |
|-------------------------|-----------------------------------------|
| Overview + status      | STATE.md (v0.2.0, 92 tests, 91%)      |
| Installation           | pyproject.toml, setup.cfg               |
| Database Setup         | alembic.ini, migrations/env.py          |
| Running Agent          | src/orchestrator.py, config.py          |
| Tests                  | tests/conftest.py, STATE.md metrics     |
| Architecture           | docs/system_architecture.md             |
| dry_run + safety       | src/core/config.py, docs/risk_management.md |
| Env vars               | .env.example (exact 22 entries)         |