# Arquitectura del Sistema - Poly-Oracle-Agent

**Fase 1: Infraestructura y Modelado de Datos**

El stack está confirmado. Polymarket opera un CLOB (Central Limit Order Book) híbrido *off-chain* con liquidación *on-chain* en Polygon PoS. Se utiliza WebSocket para el streaming del orderbook público en `wss://ws-subscriptions-clob.polymarket.com/ws/market` y REST para la ejecución de órdenes en `https://clob.polymarket.com`.

---

## 1. Diagrama de Arquitectura del Sistema

```mermaid
graph TB
    subgraph External["🌐 External Layer"]
        PM_WS["Polymarket CLOB WebSocket<br/>wss://ws-subscriptions-clob.polymarket.com/ws/market"]
        PM_REST["Polymarket REST API<br/>https://clob.polymarket.com"]
        GAMMA["Gamma API<br/>https://gamma-api.polymarket.com"]
        ANTHROPIC["Anthropic API<br/>Claude 3.5 Sonnet / claude-3-7-sonnet"]
        POLYGON["Polygon PoS<br/>RPC Node / web3.py"]
    end

    subgraph Core["⚙️ poly-oracle-agent Core (asyncio event loop)"]
        direction TB

        subgraph MIE["Module 1 — Market Ingestion Engine"]
            WS_CLIENT["AsyncWebSocketClient"]
            REST_CLIENT["AsyncRESTClient"]
            MARKET_Q["asyncio.Queue[MarketSnapshot]"]
        end

        subgraph CTX["Module 2 — Context Builder"]
            AGG["DataAggregator"]
            PROMPT_F["PromptFactory"]
            CTX_Q["asyncio.Queue[EvaluationContext]"]
        end

        subgraph LLM["Module 3 — LLM Evaluation Node"]
            CLAUDE["ClaudeClient (CoT)"]
            VALIDATOR["Pydantic: LLMEvaluationResponse"]
            DEC_Q["asyncio.Queue[AgentDecision]"]
        end

        subgraph WEB3["Module 4 — Web3 Execution Node"]
            SIGNER["TransactionSigner (EIP-712)"]
            NONCE_MGR["NonceManager"]
            GAS_EST["GasEstimator"]
            TX_BROAD["TxBroadcaster"]
        end

        subgraph PERSIST["🗄️ Persistence Layer (SQLAlchemy Async)"]
            DB[("SQLite / PostgreSQL")]
            SNAP_TBL["MarketSnapshot"]
            DEC_TBL["AgentDecisionLog"]
            TX_TBL["ExecutionTx"]
        end
    end

    PM_WS -->|"book / price_change / last_trade_price"| WS_CLIENT
    GAMMA -->|"market metadata"| REST_CLIENT
    WS_CLIENT --> MARKET_Q
    REST_CLIENT --> MARKET_Q

    MARKET_Q --> AGG
    AGG --> PROMPT_F
    PROMPT_F --> CTX_Q

    CTX_Q --> CLAUDE
    ANTHROPIC <-->|"CoT prompt / structured JSON"| CLAUDE
    CLAUDE --> VALIDATOR
    VALIDATOR --> DEC_Q

    DEC_Q --> SIGNER
    SIGNER --> NONCE_MGR --> GAS_EST --> TX_BROAD
    TX_BROAD <-->|"signed tx / receipt"| POLYGON
    TX_BROAD -->|"POST /order"| PM_REST

    MARKET_Q -.->|"persist snapshot"| SNAP_TBL
    DEC_Q -.->|"persist decision + raw CoT"| DEC_TBL
    TX_BROAD -.->|"persist tx hash + status"| TX_TBL
    SNAP_TBL & DEC_TBL & TX_TBL --- DB
```

---

## 2. Diagrama de Secuencia - Bucle de Trading Asíncrono

```mermaid
sequenceDiagram
    autonumber
    participant WS as WebSocket<br/>(Polymarket CLOB)
    participant MIE as Market Ingestion<br/>Engine
    participant CTX as Context Builder
    participant DB as Persistence<br/>(SQLAlchemy Async)
    participant LLM as LLM Evaluation<br/>Node (Claude)
    participant WEB3 as Web3 Execution<br/>Node

    loop Continuous asyncio Event Loop
        WS-->>MIE: WS frame: book/price_change/last_trade_price
        activate MIE
        MIE->>MIE: Parse & validate via MarketSnapshot (Pydantic)
        MIE->>DB: INSERT MarketSnapshot (async)
        MIE->>CTX: Enqueue MarketSnapshot
        deactivate MIE

        activate CTX
        CTX->>CTX: Aggregate rolling window + historical context
        CTX->>CTX: PromptFactory.build_cot_prompt()
        CTX->>LLM: Enqueue EvaluationContext
        deactivate CTX

        activate LLM
        LLM->>LLM: Build CoT system + user prompt
        LLM-->>LLM: POST /messages → Claude API (async httpx)
        LLM->>LLM: Validate response via LLMEvaluationResponse (Pydantic)

        alt Validation fails (JSON malformed / schema mismatch)
            LLM->>LLM: Retry with stricter schema reminder (max 2 retries)
        end

        LLM->>DB: INSERT AgentDecisionLog (raw CoT + structured fields)
        LLM->>WEB3: Enqueue AgentDecision (if decision_boolean=True)
        deactivate LLM

        activate WEB3
        WEB3->>WEB3: NonceManager.get_next_nonce() (async lock)
        WEB3->>WEB3: GasEstimator.estimate() → polygon_gas_price
        WEB3->>WEB3: Build & EIP-712 sign order payload
        WEB3-->>WEB3: POST /order → Polymarket REST (async)
        WEB3-->>WEB3: Await tx receipt on Polygon RPC
        WEB3->>DB: INSERT ExecutionTx (hash, status, gas_used)
        deactivate WEB3
    end
```

---

## 3. Árbol de Directorios del Proyecto

```text
poly-oracle-agent/
├── .env                          # ANTHROPIC_API_KEY, POLYGON_RPC, WALLET_KEY, etc.
├── .env.example
├── pyproject.toml                # PEP 621: deps, ruff, mypy, pytest config
├── README.md
│
├── src/
│   ├── __init__.py
│   │
│   ├── core/                     # Shared primitives (no business logic)
│   │   ├── __init__.py
│   │   ├── config.py             # Pydantic-settings: AppConfig (env parsing)
│   │   ├── logging.py            # structlog structured async logger
│   │   └── exceptions.py        # Domain-specific exception hierarchy
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── engine.py             # Async SQLAlchemy engine + session factory
│   │   ├── models.py             # ← MarketSnapshot, AgentDecisionLog, ExecutionTx
│   │   └── repositories/
│   │       ├── __init__.py
│   │       ├── market_repo.py
│   │       ├── decision_repo.py
│   │       └── execution_repo.py
│   │
│   ├── schemas/
│   │   ├── __init__.py
│   │   ├── market.py             # MarketSnapshotSchema, OrderBookSchema
│   │   ├── llm.py                # ← LLMEvaluationResponse (strict Pydantic V2)
│   │   └── web3.py               # OrderPayloadSchema, TxReceiptSchema
│   │
│   ├── agents/
│   │   ├── __init__.py
│   │   ├── ingestion/
│   │   │   ├── __init__.py
│   │   │   ├── ws_client.py      # AsyncWebSocketClient (CLOB stream)
│   │   │   └── rest_client.py    # AsyncRESTClient (Gamma + CLOB REST)
│   │   │
│   │   ├── context/
│   │   │   ├── __init__.py
│   │   │   ├── aggregator.py     # DataAggregator: rolling window logic
│   │   │   └── prompt_factory.py # CoT prompt templating (Jinja2 or f-string)
│   │   │
│   │   ├── evaluation/
│   │   │   ├── __init__.py
│   │   │   └── claude_client.py  # ClaudeClient: async Anthropic call + retry
│   │   │
│   │   └── execution/
│   │       ├── __init__.py
│   │       ├── signer.py         # EIP-712 order signing (web3.py)
│   │       ├── nonce_manager.py  # Async-safe NonceManager (asyncio.Lock)
│   │       ├── gas_estimator.py  # Dynamic gas pricing (Polygon RPC)
│   │       └── broadcaster.py    # TX broadcast + receipt polling
│   │
│   └── orchestrator.py           # Top-level asyncio.gather() wiring all modules
│
├── tests/
│   ├── conftest.py               # pytest-asyncio fixtures, in-memory DB
│   ├── unit/
│   │   ├── test_schemas.py
│   │   ├── test_prompt_factory.py
│   │   └── test_nonce_manager.py
│   └── integration/
│       ├── test_ws_client.py
│       └── test_claude_client.py
│
├── migrations/                   # Alembic async migrations
│   ├── env.py
│   └── versions/
│
└── scripts/
    └── seed_markets.py           # Dev-time market seeding utility
```


## 5. Decisiones Clave de Diseño

| Concern | Decision | Rationale |
| :--- | :--- | :--- |
| Immutability of LLM output | `frozen=True` on `LLMEvaluationResponse` | Prevents accidental mutation in async queues before execution |
| Cross-field validation | `@model_validator(mode="after")` | Enforces EV+/decision coherence at schema boundary, never in business logic |
| 1-to-1 Decision → TX | `unique=True` on `ExecutionTx.decision_id` | DB-level guard against double-execution of a single decision |
| Raw CoT persistence | `reasoning_log: Text` in `AgentDecisionLog` | Full audit trail of every Claude reasoning chain, separate from structured fields |
| Async-safe nonce | `NonceManager` with `asyncio.Lock` (stub) | Prevents nonce collisions under concurrent Polygon tx submissions |
| WebSocket heartbeat | CLOB: ping every 10s, RTDS: every 5s | Per Polymarket connection management requirements |
| Enum sync between layers | `DecisionAction` (ORM) mirrors `RecommendedAction` (Pydantic) | Keeps DB enum and schema enum decoupled |

---

## 6. Próximos Pasos (Fase 2)
*   Implementar `orchestrator.py` para cablear todos los módulos.
*   Implementar `ClaudeClient` con plantillas de prompts CoT.
*   Construir `NonceManager` y `TransactionSigner`.
