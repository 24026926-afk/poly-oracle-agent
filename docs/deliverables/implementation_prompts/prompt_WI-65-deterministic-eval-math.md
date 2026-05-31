# Implementation Prompt - WI-65 Deterministic Eval Math

## Session Context

You are working in `poly-oracle-agent` after a zero-trade production run was root-caused. The market data and order-book pipeline are healthy (WI-63 fixed REST best-of-book; 97.2% of snapshots have ≤2¢ spreads; eval-context midpoints are realistic). The actual blocker is the Stage-C reflection auditor **rejecting 95.4% of candidates** (649/680); a REJECTED verdict forces `confidence_score=0.0` → Gatekeeper `MIN_CONFIDENCE` → HOLD.

The two largest *mechanical* rejection classes are EV-arithmetic flags (~290) and spread-%-arithmetic flags (~113). They are manufactured because the primary prompt asks the model to compute EV and apply spread filters in prose, and the reflection prompt then asks the auditor to verify that arithmetic — even though `LLMEvaluationResponse` already recomputes EV, Kelly, and spread deterministically in its validators. WI-65 removes that contradiction and makes the Gatekeeper's market facts code-authoritative.

WI-65 is **fix A** of a remediation sequence. Fix B (recalibrate the reflection auditor so soft bias flags ADJUST instead of REJECT) and fix C (discovery price-band) are separate, later Work Items — do not implement them here.

Current baseline:

- `src/agents/evaluation/claude_client.py::ClaudeClient._evaluate` fetches a fresh authoritative order book via `PolymarketClient.fetch_order_book(yes_token_id)` → `wi14_snapshot` (a `MarketSnapshot` with Decimal `best_bid`, `best_ask`, `midpoint_probability`, `spread`). It then builds the primary prompt, runs the reflection audit, applies the verdict (`_apply_reflection_verdict`), and validates the final candidate with `LLMEvaluationResponse.model_validate_json(final_json)` (~line 844).
- `LLMEvaluationResponse` (`src/schemas/llm.py`) computes `expected_value` (`_compute_ev_and_kelly`), the gatekeeper filters and Kelly/position size (`_apply_gatekeeper_filters`), and the HOLD override (`_enforce_decision_override`). `MarketContext.spread_pct = (best_ask - best_bid)/best_ask`. `ProbabilisticEstimate.compute_kelly_and_ev` computes EV/net-odds/Kelly from `p_true`/`p_market`.
- `PromptFactory.build_evaluation_prompt` currently instructs the model (steps 3–4) to "Calculate the Expected Value (EV)" and "Apply the required safety filters (EV > 2%, Spread < 1.5%, Confidence ≥ 75%)". `PromptFactory.build_reflection_prompt` currently includes audit question 2 (bid/ask/midpoint/spread coherence) and audit question 3 (p_true/p_market/EV arithmetic consistency).
- `_build_hold_candidate` sets `confidence_score=0.0` on REJECTED.
- `DRY_RUN=false`, live signing, live broadcasting, and any path bypassing `LLMEvaluationResponse` remain out of scope and forbidden.

Before implementing, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-65-deterministic-eval-math.md`
- `src/schemas/llm.py` (`LLMEvaluationResponse`, `MarketContext`, `ProbabilisticEstimate`, `ReflectionResponse`, gatekeeper validators)
- `src/agents/context/prompt_factory.py` (`build_evaluation_prompt`, `build_reflection_prompt`)
- `src/agents/evaluation/claude_client.py` (`_evaluate`, `_run_reflection_audit`, `_apply_reflection_verdict`, `_build_hold_candidate`)
- `src/agents/execution/polymarket_client.py` (`MarketSnapshot`, `fetch_order_book`)

## Objective

Make the LLM chain supply judgment only (`p_true`, `confidence_score`, `reasoning_log`, qualitative `risk_assessment`) and the system own all market facts and arithmetic. Concretely: (1) strip EV/spread/Kelly *calculation* instructions from the primary prompt; (2) strip the EV/spread *arithmetic-verification* audit questions from the reflection prompt and tell the auditor those values are system-computed; (3) inject authoritative `wi14_snapshot` market facts (`best_bid`, `best_ask`, `midpoint`, `p_market`) into the final candidate immediately before terminal Gatekeeper validation, across all reflection verdict paths.

## Inputs

- `wi14_snapshot: MarketSnapshot` — authoritative Decimal `best_bid`, `best_ask`, `midpoint_probability` already fetched in `_evaluate`.
- `final_json: str` — the candidate JSON chosen by `_apply_reflection_verdict` (APPROVED/ADJUSTED/REJECTED) immediately before `LLMEvaluationResponse.model_validate_json`.
- `PromptFactory.build_evaluation_prompt` and `build_reflection_prompt` (prompt text only).
- Existing schema validators in `LLMEvaluationResponse` / `ProbabilisticEstimate` / `MarketContext` (do not modify their arithmetic).
- No new Python package dependencies. Standard library, `pydantic`, `structlog`, `Decimal`, `json` only.

## Outputs

- `src/agents/context/prompt_factory.py`:
  - `build_evaluation_prompt` — remove the EV-calculation and safety-filter-application instructions; instruct the model to estimate `p_true` from evidence, state confidence + reasoning, and assess risk; state explicitly that the system computes EV, spread, Kelly, and sizing deterministically.
  - `build_reflection_prompt` — remove the bid/ask/spread arithmetic-coherence audit question and the p_true/p_market/EV arithmetic-consistency audit question; add an explicit statement that EV/net-odds/Kelly/spread/sizing are system-computed from authoritative market data and must not be recomputed or flagged; keep evidence-support, bias, uncertainty→HOLD, and decision-coherence checks.
- `src/agents/evaluation/claude_client.py`:
  - Add an authoritative-market-fact override applied to `final_json` (or the parsed final candidate) immediately before `LLMEvaluationResponse.model_validate_json`, setting `market_context.best_bid/best_ask/midpoint` and `probabilistic_estimate.p_market` from `wi14_snapshot`. Use a small, testable helper (e.g. `_apply_authoritative_market_facts`). Decimal→schema-boundary conversion must match the existing prompt-path convention; introduce no new float arithmetic.
- `tests/unit/test_WI-65-deterministic-eval-math.py` — unit tests (RED first, then GREEN).
- `STATE.md` — WI-65 completion entry on `/wi-done`.

## Acceptance Criteria

1. After WI-65, the validated final `LLMEvaluationResponse.market_context.best_bid/best_ask/midpoint` and `probabilistic_estimate.p_market` equal the authoritative `wi14_snapshot` values, even when the LLM candidate JSON carried different (wrong) values — verified on APPROVED, ADJUSTED, and REJECTED paths.
2. `expected_value` and `gatekeeper_audit.computed_spread_pct` in the validated response are computed from the authoritative facts plus the LLM's `p_true` (i.e. recomputing by hand from `wi14_snapshot` + `p_true` matches the response).
3. `build_evaluation_prompt` output contains no instruction to calculate EV and no instruction to apply numeric EV/spread/confidence thresholds; it still requests `p_true`, confidence, reasoning, and risk.
4. `build_reflection_prompt` output contains no audit question asking the auditor to verify EV arithmetic or bid/ask/spread arithmetic; it contains an explicit statement that those values are system-computed; it still asks about evidence support for `p_true`, bias, uncertainty, and decision coherence.
5. The override is a pure function of `wi14_snapshot` and the candidate; it runs only after the existing `wi14_snapshot is None` conservative-skip guard.
6. No new `float` arithmetic is introduced; the schema field types are unchanged.
7. `LLMEvaluationResponse` remains the terminal gate; no execution/signing/broadcasting/`dry_run` behavior changes.
8. Full regression passes with coverage ≥ 80%; no existing test regresses. `ruff format` and `ruff check` are clean.

## Anti-Patterns

- Do not change the arithmetic inside `LLMEvaluationResponse`, `ProbabilisticEstimate`, or `MarketContext` validators (EV, net odds, Kelly, spread %, gatekeeper order, position sizing). WI-65 changes *inputs*, not the math.
- Do not retune reflection verdict mapping (REJECTED→ADJUSTED, soft-flag handling) — that is fix B.
- Do not add a discovery price-band filter — that is fix C.
- Do not trust LLM-echoed `best_bid`/`best_ask`/`midpoint`/`p_market` for the money path; always override from `wi14_snapshot`.
- Do not apply the override on only one verdict path; it must run at a single chokepoint covering APPROVED, ADJUSTED, and REJECTED.
- Do not introduce `float` arithmetic in money/price/EV paths; convert at the schema boundary exactly as the existing prompt path does.
- Do not add new schemas, schema fields, enum values, Alembic migrations, or `Base.metadata.create_all()`.
- Do not weaken `DRY_RUN`, add execution/signing/broadcasting, or create any path that bypasses `LLMEvaluationResponse`.
- Do not add `print()`. Use `structlog`. Do not log secrets or high-cardinality fields.
- Do not add new Python package dependencies.
- Do not delete files outside this Work Item's scope.

## Dependencies

- WI-63 — REST order-book best-of-book selection (provides the correct `wi14_snapshot` this WI treats as authoritative).
- `src/agents/evaluation/claude_client.py` — `_evaluate`, `_apply_reflection_verdict`, terminal validation chokepoint.
- `src/agents/context/prompt_factory.py` — `build_evaluation_prompt`, `build_reflection_prompt`.
- `src/schemas/llm.py` — `LLMEvaluationResponse`, `MarketContext`, `ProbabilisticEstimate`, `ReflectionResponse` (read-only; not modified).
- `src/agents/execution/polymarket_client.py` — `MarketSnapshot`, `fetch_order_book` (read-only).

## Target Layer

Cognitive evaluation (Layer 3): prompt construction (`PromptFactory`) and the evaluation orchestration in `ClaudeClient._evaluate`. The change is confined to two prompt builders and one authoritative-fact override at the terminal-validation chokepoint. It does not touch market discovery, execution routing, signing, broadcasting, the Gatekeeper's arithmetic, or persistence.
