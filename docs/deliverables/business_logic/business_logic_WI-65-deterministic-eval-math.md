# Business Logic - WI-65 Deterministic Eval Math

## Objective

Stop the LLM evaluation chain from producing, stating, or re-auditing trade arithmetic, and make the Gatekeeper's market facts code-authoritative. The LLM provides **judgment only** (a true-probability estimate, a confidence, qualitative reasoning, and a qualitative risk assessment); the system owns **all market facts and all arithmetic** (best bid/ask, midpoint, market-implied probability, EV, net odds, Kelly, spread %, position size).

### Why

A 60-hour production run (then a fresh ~8-hour run on `main 4d9d442`) made zero trades. Root-cause analysis of the second run showed market data was healthy (97.2% of `market_snapshots` had ≤2¢ spreads; eval-context midpoints were realistic, none pinned at 0.5). The blocker was the **Stage-C adversarial reflection auditor rejecting 649/680 candidates (95.4%)**; a REJECTED verdict forces `confidence_score=0.0` → Gatekeeper `MIN_CONFIDENCE` → HOLD.

Aggregating the 649 rejection flags, the largest *mechanical* classes were:
- `ev_arithmetic_inconsistency` / `ev_miscalculation` / `ev_arithmetic_mismatch` (~290)
- `spread_pct_miscalculation` / `spread_pct_miscomputed` (~113)

These exist because the **primary prompt asks the model to compute EV and apply spread filters in prose**, and the **reflection prompt then asks the auditor to verify that arithmetic** (audit questions 2 and 3). But `LLMEvaluationResponse` already recomputes EV, net odds, Kelly, and spread % deterministically in its validators (`ProbabilisticEstimate.compute_kelly_and_ev`, `MarketContext.spread_pct`, `_apply_gatekeeper_filters`). The LLM's stated arithmetic is therefore irrelevant to the final decision, yet it manufactures rejections. WI-65 removes that contradiction.

Separately, the Gatekeeper currently computes `spread_pct` and EV from the **LLM-echoed** `market_context.best_bid/best_ask/midpoint` and `probabilistic_estimate.p_market`, not from the authoritative `wi14_snapshot` already fetched in `ClaudeClient._evaluate`. WI-65 injects the authoritative market facts into the final candidate before terminal validation, so a hallucinated or rounded echo can never move the money math.

The remaining (non-arithmetic) rejection classes — `overconfidence_unsupported`, `narrative_anchoring` — are **out of scope** for WI-65; they are addressed by the follow-up reflection-calibration Work Item (fix B).

## Data Models

Pydantic schema names only — **no new schemas, no new fields, no migration**:

- `LLMEvaluationResponse` (existing, `src/schemas/llm.py`) — the terminal Gatekeeper. Structurally unchanged. Continues to compute `expected_value`, Kelly, `spread_pct`, gatekeeper audit, and position size in its existing validators. Its *inputs* (`market_context`, `probabilistic_estimate.p_market`) become code-authoritative.
- `MarketContext` (existing, `src/schemas/llm.py`) — unchanged. `best_bid`, `best_ask`, `midpoint` are set from the authoritative snapshot before validation, not from the LLM echo. `spread_pct` property unchanged.
- `ProbabilisticEstimate` (existing, `src/schemas/llm.py`) — unchanged. `p_true` remains LLM-provided judgment; `p_market` is set from the authoritative midpoint before validation. `expected_value`/`net_odds_b`/`kelly_full`/`kelly_quarter` remain validator-computed.
- `RiskAssessment` (existing) — unchanged; remains LLM-provided qualitative judgment.
- `ReflectionResponse` (existing) — unchanged structurally; the *prompt* that produces it changes (see Key Rules).
- `MarketSnapshot` (existing, `src/agents/execution/polymarket_client.py`) — the authoritative source of `best_bid`, `best_ask`, `midpoint_probability` (already fetched as `wi14_snapshot`). Read-only; not modified.
- `PromptFactory` (existing, `src/agents/context/prompt_factory.py`) — `build_evaluation_prompt` and `build_reflection_prompt` are edited (prompt text only; no schema change).

## Key Rules

1. **LLM = judgment only.** The primary model supplies exactly: `p_true` (its estimated true probability), `confidence_score`, `reasoning_log` (qualitative justification), and the qualitative `risk_assessment` fields. It does **not** compute or state EV, net odds, Kelly, spread %, or position size.
2. **System = facts + arithmetic.** All market facts (`best_bid`, `best_ask`, `midpoint`, `p_market`) and all arithmetic (EV, net odds, Kelly full/quarter, `spread_pct`, position size) are owned by code: the authoritative `wi14_snapshot` plus the existing `LLMEvaluationResponse` validators. No change to the existing validator math.
3. **Authoritative market-fact injection.** Immediately before terminal `LLMEvaluationResponse.model_validate_json(final_json)` in `ClaudeClient._evaluate`, code overrides, on the final candidate object:
   - `market_context.best_bid` ← `wi14_snapshot.best_bid`
   - `market_context.best_ask` ← `wi14_snapshot.best_ask`
   - `market_context.midpoint` ← `wi14_snapshot.midpoint_probability`
   - `probabilistic_estimate.p_market` ← `wi14_snapshot.midpoint_probability`
   The LLM's echoed values for these four fields are discarded. This single chokepoint applies to **every** reflection verdict path (APPROVED, ADJUSTED, REJECTED).
4. **Primary prompt (`build_evaluation_prompt`)** removes the "Calculate the Expected Value (EV)" instruction and the "Apply the required safety filters (EV > 2%, Spread < 1.5%, Confidence ≥ 75%)" instruction. It instructs the model to estimate `p_true` from evidence, state its confidence and reasoning, and assess risk — and states explicitly that the system computes EV, spread, Kelly, and sizing deterministically, so the model must not compute them.
5. **Reflection prompt (`build_reflection_prompt`)** removes the arithmetic-recheck audit questions — the bid/ask/midpoint/spread *arithmetic* coherence check (Q2) and the p_true/p_market/EV *arithmetic* consistency check (Q3). It adds an explicit statement that EV, net odds, Kelly, spread %, and sizing are computed deterministically by the system from authoritative market data and must **not** be recomputed, second-guessed, or flagged. The auditor's remaining mandate: assess whether `p_true` is supported by cited evidence, detect genuine bias (overconfidence, narrative anchoring, recency, confirmation), and default to HOLD under unresolved uncertainty.
6. The reflection verdict semantics are unchanged in WI-65: APPROVED → candidate unchanged; ADJUSTED → corrected candidate; REJECTED → conservative HOLD (`confidence_score=0.0`). (Re-tuning REJECTED→ADJUSTED for soft bias flags is fix B, not WI-65.)
7. **Decimal posture.** `MarketSnapshot` fields are `Decimal`. The override assigns them to the schema's existing `MarketContext`/`ProbabilisticEstimate` fields (which are `float` in the current schema) using the same boundary conversion the prompt path already uses. WI-65 introduces **no new float arithmetic**; it does not widen or narrow the schema's existing float/Decimal posture (that posture predates WI-65 and is out of scope).
8. The override is fail-closed by construction: it runs only after the existing `wi14_snapshot is None` guard has already caused a conservative skip, so authoritative values are always present when the override executes.
9. Logging is `structlog` only; no `print()`. No new secret or high-cardinality log fields.
10. No change to: budget guard, cognitive cooldown, `dry_run`, execution routing, signing, broadcasting, market discovery, Grok sentiment, or persistence schema.

## Edge Cases

1. **LLM omits or fabricates `p_market`** — overridden by authoritative midpoint; EV recomputed from `p_true` and the authoritative `p_market`.
2. **LLM echoes wrong, rounded, or crossed `best_bid`/`best_ask`** — overridden by authoritative snapshot values (which already passed the `_parse_order_book` crossed-book check). The Gatekeeper `spread_pct` is therefore always from real data.
3. **`wi14_snapshot is None`** — unchanged existing behavior: conservative skip before any candidate is built; the override never runs.
4. **Sub-penny / longshot market** (e.g. authoritative `0.002/0.003`, midpoint `0.0025`) — override sets these real values; `spread_pct` may legitimately exceed `MAX_SPREAD_PCT` and trigger a correct HOLD. This is a real market property, not an LLM artifact, and is the correct outcome.
5. **Reflection still REJECTS for non-arithmetic reasons** (overconfidence, narrative anchoring) — still forced to HOLD. Expected to persist after WI-65; resolved by fix B.
6. **ADJUSTED verdict with `corrected_candidate_json`** — the authoritative override still applies at the single chokepoint, so a corrected candidate cannot reintroduce bad market facts.
7. **Primary model still emits an EV/spread/Kelly number despite the prompt change** — ignored: the schema validators overwrite EV/net-odds/Kelly unconditionally, and the override overwrites the market facts; the stated numbers never reach the decision.
8. **Reflection model still emits an arithmetic flag despite the prompt change** — does not change the final decision math (code-owned); WI-65's success metric is the reduction in arithmetic-driven REJECTs, not their absolute elimination from free-text.
9. **Authoritative midpoint exactly at a `MarketContext` bound** (`midpoint`, `p_market` must be `>0` and `<1`) — `_parse_order_book` already guarantees positive, non-crossed, sub-1.0 prices, so injected values satisfy the field constraints.

## Invariants

1. EV, net odds, Kelly (full/quarter), `spread_pct`, and position size are computed exclusively by the existing `LLMEvaluationResponse` / `ProbabilisticEstimate` / `MarketContext` validators. WI-65 changes none of that math.
2. In the validated final candidate, `market_context.best_bid/best_ask/midpoint` and `probabilistic_estimate.p_market` equal the authoritative `wi14_snapshot` values — never the LLM echo — across all reflection verdict paths.
3. `p_true` and `confidence_score` remain LLM-provided judgment.
4. WI-65 introduces no new `float` arithmetic and does not alter the schema's pre-existing field types.
5. The reflection prompt no longer instructs the auditor to verify EV/spread/Kelly arithmetic.
6. `LLMEvaluationResponse` remains the terminal Gatekeeper; no execution path bypasses it. No `dry_run` weakening; no `DRY_RUN=false` behavior.
7. No Alembic migration, no `Base.metadata.create_all()`, no persisted-schema change.
8. With the reflection verdict held constant, WI-65 does not make any previously-HOLD-for-legitimate-reasons decision trade; it only removes arithmetic-manufactured rejections and hallucinated-fact risk.
9. Tests cover: authoritative override on each verdict path; override overriding a wrong LLM echo; EV/spread recomputed from authoritative facts; primary prompt contains no EV/filter-calculation instruction; reflection prompt contains no EV/spread arithmetic audit question; sub-penny legitimate-HOLD; Decimal-boundary integrity; no regression in the Gatekeeper filter order.
