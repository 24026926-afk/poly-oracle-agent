# WI-11 Business Logic — Dynamic Market Routing

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — `_route_market` MUST be async; no blocking calls inside the classification step.
- `.agents/rules/db-engineer.md` — No new persistence paths introduced by routing; `ClaudeClient` already owns `agent_decision_logs` through `DecisionRepository`.
- `.agents/rules/risk-auditor.md` — R-05 MEDIUM: Domain-specific prompts alter LLM output distribution; the `LLMEvaluationResponse` Pydantic Gatekeeper MUST remain the final validation gate regardless of which persona was injected upstream.

## 1. Problem Statement

`PromptFactory.build_evaluation_prompt` currently emits a single generic "Quant Developer" persona for every market. This is sub-optimal:

- A binary politics market (e.g., "Will Candidate X win the election?") requires a political science framing, not an options-pricing framing.
- A crypto price market (e.g., "Will BTC exceed $100k by EOY?") requires on-chain and macro awareness the generic prompt does not surface.
- A sports outcome market requires statistical modelling context (injury reports, team form) that is entirely absent from the current prompt.

The result is that the LLM receives no domain priors, forcing it to reason from first principles every call. This increases token count, degrades confidence calibration, and raises the probability of Gatekeeper rejection due to low confidence scores.

## 2. Target State Architecture

### 2.1 New Enum: `MarketCategory`

A lightweight enum added to `src/schemas/llm.py` that classifies incoming markets:

```
MarketCategory: CRYPTO | POLITICS | SPORTS | GENERAL
```

`GENERAL` is the safe default / catch-all for markets that do not match a known domain keyword set.

### 2.2 New Async Method: `ClaudeClient._route_market`

A **Layer 0** classification step executed inside `ClaudeClient.evaluate()` **before** the prompt is built. It inspects the market's `condition_id`, title, and available tags, and returns a `MarketCategory`. This must be a fast, local classification — no additional LLM call is required at this stage.

Routing logic uses a keyword/pattern table keyed by `MarketCategory`. Matches are checked in priority order: `CRYPTO` → `POLITICS` → `SPORTS` → `GENERAL`.

### 2.3 Updated `PromptFactory.build_evaluation_prompt`

The method signature is extended to accept an optional `category: MarketCategory` parameter. When a category is provided, a domain-specific persona preamble replaces the generic "elite Quant Developer" preamble:

| Category  | Persona injected |
|-----------|-----------------|
| `CRYPTO`  | "You are a senior on-chain analyst and crypto derivatives trader…" |
| `POLITICS` | "You are a political risk analyst with expertise in electoral forecasting and prediction markets…" |
| `SPORTS`  | "You are a quantitative sports analyst specialising in statistical modelling and real-time line movements…" |
| `GENERAL` | (current generic Quant Developer persona — unchanged) |

All four persona variants MUST:
1. Contain the identical `### LIVE MARKET DATA SNAPSHOT` and `### INSTRUCTIONS` blocks as the current prompt.
2. Require the LLM to output a JSON object conforming to the exact `LLMEvaluationResponse` JSON schema.
3. Preserve the `### CRITICAL OUTPUT FORMAT` constraint block verbatim.

### 2.4 Wiring in `ClaudeClient.evaluate()`

The updated call flow inside `_process_evaluation`:

```
item → _route_market(item) → MarketCategory
     → PromptFactory.build_evaluation_prompt(market_state, category=category)
     → _evaluate_with_retries(prompt, snapshot_id)
     → LLMEvaluationResponse (Gatekeeper)
     → _persist_decision(...)
     → out_queue (if approved)
```

`_route_market` is called once per item. Its result is passed directly into `build_evaluation_prompt` and also stored in the structured log at the `evaluation complete` log event for observability.

## 3. Acceptance Criteria

### 3.1 Schema
- `MarketCategory` enum exists in `src/schemas/llm.py` with members `CRYPTO`, `POLITICS`, `SPORTS`, `GENERAL`.
- `MarketCategory` is exported from `src/schemas/__init__.py`.

### 3.2 Routing Method
- `ClaudeClient._route_market(item: Dict[str, Any]) -> MarketCategory` is an `async` method.
- It classifies correctly for representative keyword fixtures (at minimum one per non-GENERAL category).
- It returns `MarketCategory.GENERAL` for any input with no matching domain keywords.
- It does NOT call the Anthropic API.

### 3.3 PromptFactory
- `build_evaluation_prompt(market_state, category=MarketCategory.GENERAL)` is backward-compatible; existing call sites that omit `category` continue to receive the generic Quant Developer persona.
- Domain-specific personas contain unique, domain-appropriate context not present in the GENERAL persona.
- All four prompt variants contain the `### CRITICAL OUTPUT FORMAT` block and the full JSON schema.

### 3.4 Integration
- `_process_evaluation` calls `_route_market` before building the prompt.
- `category` is logged at `INFO` level alongside `snapshot_id` at the evaluation-complete log event.
- `MarketCategory` is stored in `AgentDecisionLog` if the model supports it (field addition is in scope for this WI only if it does not require a migration; otherwise log-only is acceptable).

### 3.5 Test Coverage
- Existing test suite passes (≥ 92 tests, ≥ 80% coverage).
- New unit tests cover:
  - `_route_market` for `CRYPTO`, `POLITICS`, `SPORTS`, and `GENERAL` inputs.
  - `build_evaluation_prompt` with each `MarketCategory` variant — assert persona preamble differs and JSON schema block is present.
  - End-to-end `_process_evaluation` mock — assert correct category is logged.

### 3.6 Gatekeeper Invariant (NON-NEGOTIABLE)
Every LLM response, regardless of which persona was used, MUST pass through `LLMEvaluationResponse.model_validate_json(...)`. No persona path is permitted to bypass or short-circuit this validation step.

## 4. Constraints

### 4.1 No New LLM Calls for Routing
`_route_market` is a synchronous keyword classifier executed asynchronously. It MUST NOT fire an Anthropic API call. Token budget and latency are the primary concerns.

### 4.2 Backward Compatibility
`PromptFactory.build_evaluation_prompt` is called from at least one integration test with the current single-argument signature. The `category` parameter MUST default to `MarketCategory.GENERAL` to preserve all existing call sites.

### 4.3 Gatekeeper Is Not Modified
`LLMEvaluationResponse` and its validator chain are read-only for this WI. The Gatekeeper filters (`MIN_CONFIDENCE`, `MAX_SPREAD`, `MIN_EV_THRESHOLD`, `MIN_TTR_HOURS`, `EV_NON_POSITIVE`) are unchanged.

### 4.4 No Orchestrator Changes
The orchestrator does not know about `MarketCategory`. Routing is an internal concern of `ClaudeClient`.

### 4.5 Decimal / Numeric Invariants Unchanged
WI-09 Decimal math invariants in `BankrollPortfolioTracker` and `ExecutionRepository` are not touched.

## 5. Verification Checklist

### 5.1 Routing Correctness
```
pytest tests/unit/test_market_router.py -v
```
All keyword fixture cases pass for each `MarketCategory`.

### 5.2 Prompt Integrity
```
pytest tests/unit/test_prompt_factory.py -v
```
Each category emits a unique persona block. All variants contain the JSON schema constraint block.

### 5.3 Gatekeeper Gate
```
grep -r "model_validate_json\|model_validate" src/agents/evaluation/claude_client.py
```
Exactly one call site exists, downstream of every prompt variant.

### 5.4 Full Suite
```
pytest --asyncio-mode=auto tests/ -q
```
≥ 92 tests pass, coverage ≥ 80%.
