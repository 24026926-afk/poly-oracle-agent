# P14-WI-14 — Polymarket Market Data Client Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi14-polymarket-client` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/ingestion-specialist.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/test-engineer.md`
- `.agents/rules/security-auditor.md`

## Role

You are implementing WI-14 for Phase 5: a read-only Polymarket CLOB market data client that supplies fresh bid/ask pricing to the cognitive evaluation path before Claude reasoning.

This WI is market-data-only. It must increase pricing integrity without expanding execution or signing surface area.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi14.md`
4. `docs/PRD-v5.0.md` (Phase 5 / WI-14 section)  
   If `PRD-v5.0.md` is not present, read the current Phase 5 PRD section from:
   - `docs/archive/ARCHIVE_PHASE_4.md` (`## Next Phase (Phase 5)`)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/agents/evaluation/claude_client.py`
9. `src/agents/context/prompt_factory.py`
10. `src/core/config.py`
11. Existing tests:
    - `tests/integration/test_claude_client.py`
    - `tests/integration/test_pipeline_e2e.py`
    - `tests/unit/test_prompt_factory.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create/extend WI-14 test files first:
   - `tests/unit/test_polymarket_client.py`
   - `tests/integration/test_market_data_injection.py` (or equivalent integration target)
2. Write failing tests for all required behaviors:
   - `PolymarketClient` initializes in read-only mode (no private key, no signer dependencies).
   - `fetch_order_book(token_id: str)` fetches order book data through `pyclob` and returns top-of-book price context.
   - Midpoint/Market Probability is computed using strict `Decimal` math (no float in assertion path).
   - `pyclob` connection errors/timeouts are handled gracefully with conservative non-tradable behavior.
3. Run RED tests:
   - `pytest tests/unit/test_polymarket_client.py -v`
   - `pytest tests/integration/test_market_data_injection.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Create WI-14 Client Module

Target:
- `src/agents/execution/polymarket_client.py` (new)

Requirements:
1. Add async `PolymarketClient` class.
2. Keep class purpose strictly read-only market data retrieval.
3. No signing/execution/private-key parameters in this WI-14 client surface.
4. Implement the typed snapshot contract as a Pydantic `BaseModel` named `MarketSnapshot`, defined in `src/agents/execution/polymarket_client.py` alongside `PolymarketClient`.

### Step 2 — Integrate Official `pyclob` Safely (Read-Only)

Target:
- `src/agents/execution/polymarket_client.py`
- dependency/config wiring files as needed (`pyproject.toml`, `src/core/config.py`)

Requirements:
1. Use official `pyclob` Python SDK as primary order book dependency.
2. Initialize in read-only/public mode only.
3. Add robust timeout/error handling for SDK/network failures.
4. Structured logging only (`structlog`), no `print()`.
5. Hard constraint: Wrap all `pyclob` SDK calls with `asyncio.wait_for(..., timeout=0.5)`. The market data fetch budget is 500ms maximum to preserve the 2.0–3.0s total chain latency target.

### Step 3 — Implement `fetch_order_book(token_id: str)` Async Contract

Target:
- `src/agents/execution/polymarket_client.py`

Requirements:
1. Implement async method `fetch_order_book(token_id: str)` as the primary read method for WI-14.
2. Method must fetch order book for YES `token_id` and extract best bid and best ask.
3. Validate top-of-book integrity:
   - missing bid or ask -> non-tradable outcome
   - `best_ask < best_bid` -> reject snapshot
4. Return a typed, deterministic market snapshot payload for downstream evaluation use.

### Step 4 — Implement Decimal Midpoint Logic

Target:
- `src/agents/execution/polymarket_client.py`

Requirements:
1. Compute Market Probability with Decimal-only arithmetic:
   - midpoint = `(best_bid + best_ask) / Decimal("2")`
2. Convert incoming numeric values via `Decimal(str(value))`.
3. No float arithmetic in midpoint/spread calculations.

### Step 5 — Inject WI-14 Market Data Before Claude Prompt Build

Target:
- `src/agents/evaluation/claude_client.py`
- `src/agents/context/prompt_factory.py` (only if field naming adaptation is needed)

Requirements:
1. In `_process_evaluation`, fetch fresh market price using `fetch_order_book(token_id: str)` before `PromptFactory.build_evaluation_prompt(...)`.
2. Enrich/overwrite evaluation `market_state` with fetched bid/ask/midpoint/spread.
3. Ensure Claude prompt receives actual spread-aware market data from WI-14.
4. On WI-14 market data failure, enforce conservative non-trading behavior (skip or forced HOLD path), with explicit logs.
5. Keep terminal Gatekeeper invariant unchanged:
   - `LLMEvaluationResponse.model_validate_json(...)` remains final authority.

### Step 6 — GREEN Validation

Run:
```bash
pytest tests/unit/test_polymarket_client.py -v
pytest tests/integration/test_market_data_injection.py -v
pytest tests/integration/test_claude_client.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. STRICTLY NO `float` math for midpoint/market-probability/spread logic.
2. STRICTLY NO order execution, signing, nonce, or private-key handling in WI-14 market client.
3. `PolymarketClient` is read-only market data only.
4. No bypass of `LLMEvaluationResponse` terminal Gatekeeper.
5. No queue topology changes; preserve async pipeline order.
6. `dry_run` behavior remains untouched.

---

## Required Test Matrix

At minimum, WI-14 tests must prove:
1. Read-only initialization of `PolymarketClient` (no private-key dependency).
2. `fetch_order_book(token_id: str)` uses `pyclob` to obtain order book and extracts best bid/ask.
3. Midpoint market probability is Decimal-correct under precision-sensitive fixtures.
4. `pyclob` timeout/connection failures produce graceful conservative behavior (no crash, no execution-eligible path).
5. `ClaudeClient` fetches WI-14 price data before prompt construction.
6. Prompt input reflects WI-14 refreshed spread/midpoint values.

---

## Deliverables

1. RED-phase failing test summary.
2. GREEN implementation summary by file.
3. Passing targeted test summary + full regression summary.
4. Final staged `git diff` for MAAP checker review.

---

## MAAP Reflection Pass (Checker Prompt for Gemini 2.5 Pro)

Use the following exact prompt with the final staged `git diff`:

```text
You are the MAAP Checker for WI-14 (Polymarket Market Data Client) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi14.md
2) Phase 5 PRD section (docs/PRD-v5.0.md WI-14 section, or ARCHIVE_PHASE_4.md Next Phase section if PRD-v5.0 is unavailable)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Decimal violations (any float usage in bid/ask/midpoint/spread money-path logic)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation)
- Business logic drift (deviation from WI-14 required read-only market-data behavior)
- Read-only violations (any signing/execution/private-key coupling introduced in WI-14 scope)

Additional required checks:
- fetch_order_book(token_id: str) exists and is async
- pyclob integration is read-only and failure-safe (timeout/connection error handling)
- midpoint market probability uses Decimal-only arithmetic
- ClaudeClient fetches WI-14 market data before prompt construction
- market-data failure path is conservative (no execution enqueue)

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-14/invariants
4) Explicit statement on each MAAP critical category:
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Business logic drift: CLEARED/FLAGGED
   - Read-only violations: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
