# P11-WI-11 — Dynamic Market Routing

## Execution Target
- Primary: Claude Code (Plan Mode) — code.claude.com
- Review: Codex Chat Panel (Antigravity IDE)
- Git ops: Codex CLI → `feat(eval): add Layer 0 market category routing`

## Active Agents
- .agents/rules/async-architect.md
- .agents/rules/risk-auditor.md

## Agent Constraints (extracted — do not override)
- `.agents/rules/async-architect.md` — `_route_market` MUST be declared `async`; no blocking I/O inside classification.
- `.agents/rules/risk-auditor.md` — The `LLMEvaluationResponse` Pydantic Gatekeeper is the non-negotiable terminal gate. No persona path may bypass `model_validate_json`.

## Role
You are a Staff Engineer implementing a low-latency classification layer inside an existing async Python trading agent. You will add a `MarketCategory` enum to the schema module, build a keyword-based routing method inside `ClaudeClient`, update `PromptFactory` to inject domain-specific personas, wire these together in `_process_evaluation`, and add covering tests. You will not modify any downstream Gatekeeper logic.

## Context Hydration (Claude Code runs this FIRST)

Read the following files before writing any code:

1. `src/schemas/llm.py` — understand all existing enums and the `LLMEvaluationResponse` Gatekeeper structure.
2. `src/agents/evaluation/claude_client.py` — understand `_process_evaluation`, `_evaluate_with_retries`, `_persist_decision`, and the existing `decision_repo_factory` injection pattern.
3. `src/agents/context/prompt_factory.py` — understand the current `build_evaluation_prompt` signature and persona text.
4. `docs/business_logic/business_logic_wi11.md` — the authoritative spec for this WI. All acceptance criteria are defined there.
5. `tests/unit/` (glob) — identify existing test files for `claude_client` and `prompt_factory` to understand fixture patterns before adding new tests.

Do NOT read `src/orchestrator.py` — it is not modified by this WI.

## Pre-Flight Checklist

Before any edit, run these searches and record the output:

1. `grep -n "class.*Enum" src/schemas/llm.py` — confirm existing enum list.
2. `grep -n "build_evaluation_prompt" src/agents/context/prompt_factory.py src/agents/evaluation/claude_client.py tests/` — find all call sites.
3. `grep -rn "_process_evaluation\|_route_market" src/agents/evaluation/claude_client.py` — confirm `_route_market` does not exist yet.
4. `grep -rn "MarketCategory" src/` — confirm zero existing references.
5. `pytest --asyncio-mode=auto tests/ -q --tb=no` — record baseline test count and coverage.

Confirm exactly zero references to `MarketCategory` and zero definitions of `_route_market` before proceeding.

## Plan Mode Instructions

1. Read all context files listed in Context Hydration.
2. Propose a complete atomic multi-file plan before touching any file:
   - File 1: `src/schemas/llm.py` — add `MarketCategory` enum.
   - File 2: `src/agents/context/prompt_factory.py` — update `build_evaluation_prompt` signature and add persona variants.
   - File 3: `src/agents/evaluation/claude_client.py` — add `_route_market`, update `_process_evaluation`.
   - File 4: `tests/unit/test_market_router.py` — new test file for `_route_market`.
   - File 5: `tests/unit/test_prompt_factory.py` — update or extend with category variant tests.
3. Output the full plan with file-by-file descriptions and wait for confirmation before writing code.

## Per-File Implementation Steps

### Step 1 — `src/schemas/llm.py`: Add `MarketCategory` Enum

Add the following enum directly after the existing `GatekeeperFilter` enum block (before `class MarketContext`):

```python
class MarketCategory(str, Enum):
    CRYPTO   = "CRYPTO"
    POLITICS = "POLITICS"
    SPORTS   = "SPORTS"
    GENERAL  = "GENERAL"
```

- Do NOT modify any existing enum, constant, model, or validator.
- Do NOT add `MarketCategory` to `LLMEvaluationResponse`; it is routing metadata, not Gatekeeper output.

### Step 2 — `src/agents/context/prompt_factory.py`: Domain-Specific Personas

Update `build_evaluation_prompt` as follows:

1. Add import: `from src.schemas.llm import LLMEvaluationResponse, MarketCategory`
2. Update signature to: `build_evaluation_prompt(market_state: Dict[str, Any], category: MarketCategory = MarketCategory.GENERAL) -> str`
3. Build a `_PERSONA_MAP: Dict[MarketCategory, str]` at module level (or as a local dict inside the method) that maps each category to a persona preamble string:
   - `CRYPTO`: "You are a senior on-chain analyst and crypto derivatives trader with deep expertise in blockchain fundamentals, tokenomics, and macro crypto market cycles."
   - `POLITICS`: "You are a political risk analyst with expertise in electoral forecasting, geopolitical event modelling, and prediction market calibration."
   - `SPORTS`: "You are a quantitative sports analyst specialising in statistical modelling, real-time line movement analysis, and injury-impact assessment."
   - `GENERAL`: current "elite Staff Quantitative Developer" preamble (extracted verbatim from the existing prompt, unchanged).
4. Replace the hardcoded persona preamble in the prompt f-string with `persona = _PERSONA_MAP[category]`, and insert `{persona}` at the top of the prompt.
5. Preserve the `### LIVE MARKET DATA SNAPSHOT`, `### INSTRUCTIONS`, and `### CRITICAL OUTPUT FORMAT` blocks verbatim — no content changes to these sections.
6. All existing call sites that pass only `market_state` continue to work unchanged (default `category=MarketCategory.GENERAL`).

### Step 3 — `src/agents/evaluation/claude_client.py`: Add `_route_market` and Wire It

#### 3a. Add import
```python
from src.schemas.llm import LLMEvaluationResponse, MarketCategory
```

#### 3b. Add `_ROUTING_TABLE` module-level constant
Define a dict mapping `MarketCategory` to a list of lowercase keyword strings. Minimum keyword sets:

```
CRYPTO:   ["btc", "bitcoin", "eth", "ethereum", "crypto", "token", "defi", "blockchain", "sol", "solana"]
POLITICS: ["election", "president", "senate", "congress", "vote", "candidate", "party", "referendum", "governor", "minister"]
SPORTS:   ["nfl", "nba", "mlb", "nhl", "soccer", "football", "basketball", "baseball", "tennis", "ufc", "match", "game", "tournament"]
```

#### 3c. Implement `_route_market`
```python
async def _route_market(self, item: Dict[str, Any]) -> MarketCategory:
```
- Extract a combined classification string from `item.get("condition_id", "")`, `item.get("title", "")`, and `item.get("tags", [])`. Lowercase the combined string.
- Iterate `MarketCategory` members in priority order: `CRYPTO` → `POLITICS` → `SPORTS`.
- Return the first category whose keyword list has at least one match in the classification string.
- Return `MarketCategory.GENERAL` if no match found.
- No `await` expressions required inside this method body; `async` declaration satisfies the async-architect constraint.

#### 3d. Update `_process_evaluation`
Insert `_route_market` call as the first step:

```python
async def _process_evaluation(self, item: Dict[str, Any]) -> None:
    category = await self._route_market(item)          # Layer 0 routing
    prompt = PromptFactory.build_evaluation_prompt(
        market_state=item, category=category           # inject domain persona
    )
    snapshot_id = item.get("snapshot_id", "local_test_no_id")
    ...
```

Remove the inline `prompt = item.get("prompt")` extraction. The prompt is now always constructed from `item` by `PromptFactory`.

Update the `logger.info("Evaluation complete…")` call to include `market_category=category.value`.

- Preserve ALL existing logic in `_process_evaluation` after prompt construction: retry loop, persistence, routing to `out_queue`.
- Do NOT modify `_evaluate_with_retries` or `_persist_decision`.

### Step 4 — `tests/unit/test_market_router.py`: New Test File

Create a new test file with the following test cases (use `pytest` + `pytest-asyncio`):

1. `test_route_crypto_by_condition_id` — item with condition_id containing "bitcoin" → `CRYPTO`
2. `test_route_crypto_by_title` — item with title "Will ETH hit $5000?" → `CRYPTO`
3. `test_route_politics_by_title` — item with title "Will the election result in a runoff?" → `POLITICS`
4. `test_route_sports_by_title` — item with title "NBA Finals Game 7 winner" → `SPORTS`
5. `test_route_general_fallback` — item with no matching keywords → `GENERAL`
6. `test_route_priority_crypto_over_politics` — item containing both "crypto" and "election" → `CRYPTO` (first priority wins)

All tests use a minimal `ClaudeClient` stub (no real Anthropic client, no DB factory). Use `AsyncMock` or `asyncio.run` as appropriate.

### Step 5 — `tests/unit/test_prompt_factory.py`: Extend with Category Variants

Add the following test cases (or update the existing file if it exists):

1. `test_build_prompt_default_is_general` — calling with only `market_state` produces a prompt containing the GENERAL persona keyword ("Quantitative").
2. `test_build_prompt_crypto_persona` — calling with `category=CRYPTO` produces a prompt containing "on-chain analyst" and NOT the GENERAL persona text.
3. `test_build_prompt_politics_persona` — contains "political risk analyst".
4. `test_build_prompt_sports_persona` — contains "quantitative sports analyst".
5. `test_all_variants_contain_json_schema_block` — parametrised across all four categories; every output contains `"### CRITICAL OUTPUT FORMAT"` and `"JSON Schema"`.
6. `test_backward_compatibility` — assert `build_evaluation_prompt(market_state)` raises no error and returns a non-empty string (existing call-site safety).

## Regression Gate (Claude Code runs this AFTER all edits)

1. `pytest tests/unit/test_market_router.py -v` — all 6 new routing tests pass.
2. `pytest tests/unit/test_prompt_factory.py -v` — all new and existing prompt tests pass.
3. `pytest --asyncio-mode=auto tests/ -q` — full suite passes (≥ 92 tests), coverage ≥ 80%.
4. `grep -n "model_validate_json\|model_validate" src/agents/evaluation/claude_client.py` — exactly one call site remains; it is inside `_evaluate_with_retries`.
5. `grep -rn "build_evaluation_prompt" src/` — confirm every call site outside `_process_evaluation` still works (no required arg added without default).

## Step 5b — Reflection Pass
Tool: Codex Chat Panel (Antigravity)
Prompt: "Review the changes made in this session against:
  1. business_logic_wi11.md — did every acceptance criterion get implemented?
  2. .agents/rules/async-architect.md — any violations?
  3. .agents/rules/risk-auditor.md — does any code path skip `model_validate_json`?
List any gaps before I approve the commit."

## Git Commit Sequence (Codex CLI)
1. `feat(schemas): add MarketCategory enum to src/schemas/llm.py`
2. `feat(prompt): add domain-specific persona variants to PromptFactory`
3. `feat(eval): add _route_market Layer 0 classification to ClaudeClient`
4. `test(eval): add unit tests for _route_market and PromptFactory category variants`
Final PR title: `feat(eval): add Layer 0 market category routing with domain-specific personas`

## Exception Protocol
- `_route_market` returns unexpected category → Check keyword table ordering; CRYPTO has highest priority.
- `build_evaluation_prompt` call site breaks → Confirm `category` parameter has default `MarketCategory.GENERAL`.
- Gatekeeper test failure after persona change → Persona preamble altered the JSON schema block — restore `### CRITICAL OUTPUT FORMAT` verbatim.
- Coverage drops below 80% → Add parametrised test covering all four `MarketCategory` values through `_process_evaluation` mock.
- Risk R-05 triggered → Escalate to Grok for persona re-spec before retry.
