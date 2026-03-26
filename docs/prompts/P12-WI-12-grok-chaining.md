# P12-WI-12 — Prompt Chaining & Grok Sentiment Integration

## Execution Target
- Primary: Claude Code (Plan Mode) — implementation agent ("Maker")
- Review: Codex/GPT-5.4 checker against WI-12 business logic + Phase 1-3 invariants
- Git ops: follow `develop` branch + atomic commits

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-12 cognitive chaining with an external Sentiment Oracle.
You will add a typed Stage A sentiment artifact, integrate a category-gated Grok call with strict timeout behavior, inject sentiment into PromptFactory context, and keep `LLMEvaluationResponse` as the immutable terminal gate.

Do not modify execution-layer behavior or Gatekeeper thresholds.

## Context Hydration (Run First)

1. `STATE.md`
2. `docs/PRD-v4.0.md`
3. `docs/archive/ARCHIVE_PHASES_1_TO_3.md`
4. `docs/business_logic/business_logic_wi12.md`
5. `src/agents/evaluation/claude_client.py`
6. `src/agents/context/prompt_factory.py`
7. `src/schemas/llm.py`
8. `tests/unit/` + `tests/integration/test_claude_client.py`

## 5-Step Atomic Execution Plan

### Step 1 — Add Typed Stage A Sentiment Schema

Target files:
- `src/schemas/llm.py`
- `src/schemas/__init__.py` (export)

Add `SentimentResponse` Pydantic model (Gatekeeper-adjacent, not a replacement):

```python
from decimal import Decimal
from pydantic import BaseModel, Field, field_validator

class SentimentResponse(BaseModel):
    sentiment_score: Decimal = Field(ge=Decimal("-1.0"), le=Decimal("1.0"))
    tweet_volume_delta: int = Field(ge=-10000, le=10000)
    top_narrative_summary: str = Field(min_length=10, max_length=320)

    @field_validator("sentiment_score", mode="before")
    @classmethod
    def _parse_decimal(cls, v):
        return Decimal(str(v))
```

Rules:
- `SentimentResponse` validates Stage A output only.
- `LLMEvaluationResponse` remains terminal Gatekeeper for execution decisions.

### Step 2 — Introduce `GrokClient` Interface (Mocked, httpx Signature-Ready)

Target file:
- `src/agents/evaluation/grok_client.py` (new)

Define an async client with real HTTP method signatures and mock-first behavior:

```python
class GrokClient:
    def __init__(
        self,
        api_key: SecretStr,
        base_url: str,
        model: str,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 2.0,
        mocked: bool = True,
    ): ...

    async def analyze_sentiment(
        self,
        *,
        condition_id: str,
        market_title: str,
        market_category: MarketCategory,
        reference_timestamp_utc: str,
        tags: list[str] | None = None,
    ) -> SentimentResponse: ...
```

HTTP call contract (for live mode):
- Method: `POST`
- Path: `/chat/completions` (provider base URL configurable)
- Library: `httpx.AsyncClient`
- Timeout: strict `2.0s` per request
- Response: strict JSON mapped to `SentimentResponse`

Mock mode:
- Return deterministic `SentimentResponse` fixture for tests/local runs.

### Step 3 — Extend PromptFactory for Stage B Context Injection

Target file:
- `src/agents/context/prompt_factory.py`

Update prompt builder contract to accept validated sentiment artifact:

```python
build_evaluation_prompt(
    market_state: Dict[str, Any],
    category: MarketCategory = MarketCategory.GENERAL,
    sentiment: SentimentResponse | None = None,
) -> str
```

Requirements:
1. Add a dedicated section in prompt text:
   `### SENTIMENT ORACLE (LAST 60 MIN)`
2. Inject `sentiment_score`, `tweet_volume_delta`, and `top_narrative_summary` from validated artifact.
3. When sentiment is missing, inject neutral fallback block (not blank).
4. Preserve `### CRITICAL OUTPUT FORMAT` and JSON schema block verbatim.

### Step 4 — Wire ClaudeClient Chaining + Timeout/Fallback Logic

Target file:
- `src/agents/evaluation/claude_client.py`

Integrate Stage A -> Stage B orchestration in `_process_evaluation`:

1. Route category with existing `_route_market`.
2. For `CRYPTO` and `POLITICS`, call Grok sentiment.
3. Enforce strict timeout:
   - `await asyncio.wait_for(..., timeout=2.0)`
   - plus `httpx` request timeout at client level.
4. On any Grok failure, use neutral `SentimentResponse`:
   - `sentiment_score=Decimal("0.0")`
   - `tweet_volume_delta=0`
   - fallback narrative string
5. For `SPORTS` and `GENERAL`, skip Grok and inject neutral sentiment immediately.
6. Build final prompt via PromptFactory with `category` + `sentiment`.
7. Keep `_evaluate_with_retries` and `LLMEvaluationResponse.model_validate_json(...)` path unchanged.
8. Log sentiment status and fallback reason in structured logs.

### Step 5 — Tests + Regression Gate

Add/extend tests:
- `tests/unit/test_grok_client.py`
- `tests/unit/test_prompt_factory.py`
- `tests/integration/test_claude_client.py` (chain behavior)

Minimum scenarios:
1. CRYPTO triggers Grok call.
2. POLITICS triggers Grok call.
3. SPORTS skips Grok.
4. GENERAL skips Grok.
5. Grok timeout -> neutral fallback, evaluation continues.
6. Malformed Grok JSON -> neutral fallback, evaluation continues.
7. Prompt includes sentiment block values.
8. Gatekeeper remains terminal validation path.

Regression commands:
```bash
pytest tests/unit/test_grok_client.py -v
pytest tests/unit/test_prompt_factory.py -v
pytest tests/integration/test_claude_client.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Acceptance gate:
- Tests pass
- Coverage remains >= 80%
- No Gatekeeper bypass
- No blocking I/O introduced

## Invariants (Must Hold)

1. Grok timeout is exactly `2.0s`.
2. Sentiment failures never abort the evaluation pipeline.
3. `LLMEvaluationResponse` remains the final execution gate.
4. No float arithmetic in sentiment weighting logic (use `Decimal`).
5. `dry_run` enforcement in Layer 4 remains untouched.

## Reflection Pass Prompt (Checker Agent)

"Review WI-12 implementation against:
1. `docs/business_logic/business_logic_wi12.md`
2. `docs/PRD-v4.0.md` WI-12 acceptance criteria
3. `docs/archive/ARCHIVE_PHASES_1_TO_3.md` invariants

Explicitly flag:
- Decimal violations
- Gatekeeper bypass risk
- Timeout/fallback violations
- Any blocking I/O in async path"

## Assumptions & Defaults Chosen

- `tweet_volume_delta` is defined as signed integer percent change vs prior 60-minute baseline.
- WI-12 persists sentiment metadata via structured logs (no DB migration required in this blueprint).
- Grok integration is mock-first with production-ready `httpx` interface signatures.
- Neutral fallback sentiment is always injected, including skipped categories, to keep prompt shape deterministic.
