# poly-oracle-agent

## 1. Project Overview

`poly-oracle-agent` is an async autonomous trading agent for Polymarket. It ingests live orderbook data from the CLOB WebSocket, enriches market context, evaluates opportunities with Anthropic Claude under strict Pydantic gatekeeping, and routes approved decisions into a Web3 execution path.

Current project state:
- Phase 2 complete (all 8 work items delivered)
- 92 automated tests passing
- 91% total coverage (target: >=80%)

Core stack:
- Python 3.12+ (project metadata allows 3.11+, but 3.12+ is the engineering standard)
- `asyncio` concurrency
- Pydantic V2 + `pydantic-settings`
- SQLAlchemy 2.0 Async + `aiosqlite`
- `httpx` async HTTP client
- `websockets` for market stream ingestion
- `anthropic` for Claude evaluation
- `web3.py` for Polygon/EIP-712 signing
- `structlog` for structured logs
- Alembic for schema migrations

## 2. Prerequisites

Before running the agent, create a `.env` file at repo root (copy from `.env.example`) and set these variables.

Required secrets and connectivity:
- `ANTHROPIC_API_KEY`
- `POLYGON_RPC_URL`
- `WALLET_ADDRESS` (checksummed EIP-55)
- `WALLET_PRIVATE_KEY`

Runtime defaults are provided but should be explicitly set for operations:
- `ANTHROPIC_MODEL`
- `ANTHROPIC_MAX_TOKENS`
- `ANTHROPIC_MAX_RETRIES`
- `CLOB_REST_URL`
- `CLOB_WS_URL`
- `GAMMA_API_URL`
- `KELLY_FRACTION`
- `MIN_CONFIDENCE`
- `MAX_SPREAD_PCT`
- `MAX_EXPOSURE_PCT`
- `MIN_EV_THRESHOLD`
- `MIN_TTR_HOURS`
- `MAX_GAS_PRICE_GWEI`
- `FALLBACK_GAS_PRICE_GWEI`
- `DATABASE_URL`
- `LOG_LEVEL`
- `DRY_RUN`
- `INITIAL_BANKROLL_USDC`

Quick start for env file:

```bash
cp .env.example .env
```

## 3. Installation

From repository root:

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

If you prefer non-editable install:

```bash
pip install .
```

## 4. Database Setup

Use Alembic for all schema creation and upgrades.

```bash
alembic upgrade head
```

Do not use `Base.metadata.create_all()` in runtime or deployment paths.

## 5. Configuration

Configuration is loaded by `AppConfig` (`src/core/config.py`) from environment variables and `.env`.

Operationally important fields:
- `anthropic_api_key`, `anthropic_model`, `anthropic_max_tokens`, `anthropic_max_retries`
- `polygon_rpc_url`, `wallet_address`, `wallet_private_key`
- `clob_rest_url`, `clob_ws_url`, `gamma_api_url`
- `kelly_fraction`, `min_confidence`, `max_spread_pct`, `max_exposure_pct`, `min_ev_threshold`, `min_ttr_hours`
- `initial_bankroll_usdc`
- `max_gas_price_gwei`, `fallback_gas_price_gwei`
- `database_url`
- `log_level`, `dry_run`

`dry_run` behavior:
- Set `DRY_RUN=true` to run ingestion, context building, evaluation, and persistence without live order execution.
- In `dry_run` mode, execution side effects are blocked before signing/broadcasting.
- Keep `DRY_RUN=true` for local validation, CI, and integration testing.

## 6. Running the Agent

After environment setup and migration:

```bash
python -m src.orchestrator
```

What happens at startup:
- Loads and validates `AppConfig`
- Runs market discovery (`GammaRESTClient` + `MarketDiscoveryEngine`)
- Wires the four-layer queue pipeline
- Starts concurrent tasks for ingestion, context, evaluation, execution, and periodic discovery

## 7. Running Tests

Run full suite:

```bash
pytest --asyncio-mode=auto tests/
```

Run coverage:

```bash
coverage run -m pytest && coverage report -m
```

Run focused tests:

```bash
pytest tests/unit/test_schemas.py -v
pytest tests/unit/test_nonce_manager.py -v
```

Current baseline:
- 92 tests
- 91% coverage

## 8. Git Workflow

Branching and PR flow:
1. Branch from `develop`.
2. Make one logical (atomic) change per commit.
3. Open PR from `develop` to `main`.
4. Merge only after tests pass and review is complete.

Commit message format:
- `feat(scope): description`
- `fix(scope): description`
- `perf(scope): description`
- `docs(scope): description`
- `chore(scope): description`

Guardrails:
- Never commit `.env`, `venv/`, `*.pyc`, or `__pycache__/`
- Avoid WIP-style commits on shared branches

## 9. Architecture Overview

The runtime is a four-layer async pipeline connected by `asyncio.Queue`:

1. Ingestion Layer
- `CLOBWebSocketClient` streams and validates market events.
- `GammaRESTClient` provides market metadata.
- Snapshots are persisted to `market_snapshots`.

2. Context Layer
- `DataAggregator` maintains market state and emits on time/volatility triggers.
- `PromptFactory` creates structured evaluation prompts for Claude.

3. Evaluation Layer
- `ClaudeClient` requests LLM decisions and validates output with `LLMEvaluationResponse`.
- Decisions and reasoning are persisted to `agent_decision_logs`.
- Approved decisions are forwarded to execution queue.

4. Execution Layer
- `TransactionSigner` builds/signs EIP-712 orders.
- `NonceManager` manages async-safe nonce allocation.
- `GasEstimator` computes EIP-1559 pricing with safety bounds.
- `OrderBroadcaster` submits orders and records execution in `execution_txs`.

Data integrity and safety highlights:
- Risk checks are enforced at schema boundary (gatekeeper model validation).
- Exposure and bankroll sizing use portfolio-aware logic.
- `dry_run` prevents live trade side effects while keeping upstream flow active.
