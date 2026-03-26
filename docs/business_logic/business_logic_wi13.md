# WI-13 Business Logic — Reflection Auditor (Self-Correction Stage)

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — Reflection must remain fully async and preserve queue order (`market_queue -> prompt_queue -> execution_queue`).
- `.agents/rules/risk-auditor.md` — Reflection cannot alter EV/Kelly policy; Gatekeeper filters and thresholds remain immutable.
- `.agents/rules/test-engineer.md` — Reflection paths require unit + integration coverage; suite remains green at coverage >= 80%.
- `.agents/rules/security-auditor.md` — Reflection never bypasses `dry_run` behavior; Layer 4 safety remains unchanged.

## 1. Objective

WI-13 adds a mandatory Reflection Auditor stage after primary evaluation reasoning and before final Gatekeeper validation to reduce cognitive bias, catch internal inconsistencies, and enforce conservative behavior under uncertainty.

This WI is upstream decision quality control only. It MUST NOT:
1. bypass `LLMEvaluationResponse`,
2. weaken gatekeeper thresholds,
3. introduce float-based money math in risk adjustments.

## 2. Reflection Flow (Mandatory Trigger)

Every evaluation must pass through the Reflection Auditor before execution eligibility is determined.

Ordered flow:
1. Stage 0: route market category (`ClaudeClient._route_market`).
2. Stage A: fetch sentiment artifact (`GrokClient` + fallback rules from WI-12).
3. Stage B: generate primary decision candidate JSON (draft evaluation).
4. **Stage C (new): Reflection Auditor reviews Stage B output and returns verdict.**
5. Stage D: final candidate JSON is validated by `LLMEvaluationResponse.model_validate_json(...)` (terminal Gatekeeper).
6. Stage E: persist decision + reflection metadata, then route approved decisions to execution queue.

Invariant:
- No Stage B output may be persisted or routed directly to execution without Stage C verdict + Stage D Gatekeeper validation.

## 3. Reflection Auditor Prompt Contract

### 3.1 System Prompt (Auditor Persona)

"You are an adversarial quantitative risk auditor. Your job is to challenge the primary evaluation for bias, data inconsistency, and risk drift. Prefer conservative outcomes under unresolved uncertainty. Return strict JSON only."

### 3.2 Auditor Input Payload

Required fields:
- `snapshot_id`
- `market_category`
- `market_state` (condition_id, best_bid, best_ask, midpoint, spread, timestamp, market_end_date if present)
- `sentiment_artifact` (from WI-12, including fallback status)
- `primary_candidate_json` (raw Stage B output)
- `risk_constants` (`KELLY_FRACTION=0.25`, `MIN_CONFIDENCE=0.75`, `MAX_SPREAD_PCT=0.015`, `MAX_EXPOSURE_PCT=0.03`, `MIN_EV_THRESHOLD=0.02`, `MIN_TTR_HOURS=4.0`)

### 3.3 Required Audit Questions

The auditor must explicitly answer these checks:
1. **Bias check:** Does reasoning show confirmation bias, recency bias, narrative anchoring, or overconfidence unsupported by evidence?
2. **Data consistency check:** Are bid/ask/midpoint/spread relationships coherent with market snapshot values?
3. **Probability/EV consistency:** Are `p_true`, `p_market`, and EV arithmetic internally consistent?
4. **Risk sanity check:** Does proposed sizing align with quarter-Kelly and 3% cap policy?
5. **Gatekeeper pre-check:** Would any mandatory safety filter clearly fail (EV threshold, confidence, spread, TTR)?
6. **Decision coherence check:** Are `decision_boolean`, `recommended_action`, and size logically consistent?
7. **Uncertainty check:** If assumptions are unsupported or contradictory, should decision default to HOLD?

## 4. Verdict Contract

The auditor returns one of exactly three verdicts:
- `APPROVED`
- `ADJUSTED`
- `REJECTED`

### 4.1 Structured Output Schema (Reflection Stage)

```json
{
  "verdict": "APPROVED | ADJUSTED | REJECTED",
  "bias_flags": ["..."],
  "consistency_flags": ["..."],
  "risk_flags": ["..."],
  "audit_note": "short explanation",
  "correction_instructions": "required when ADJUSTED",
  "corrected_candidate_json": {},
  "latency_ms": 0
}
```

Rules:
1. `APPROVED`: `corrected_candidate_json` is null; Stage B candidate proceeds unchanged to Gatekeeper.
2. `ADJUSTED`: auditor provides correction instructions and a corrected candidate payload; corrected payload proceeds to Gatekeeper.
3. `REJECTED`: trade is killed via conservative HOLD candidate (forced non-execution path), then still passed through Gatekeeper for invariant consistency.

Loop guard:
- Reflection stage is single-pass. No unbounded recursion.
- If correction payload is malformed or missing on `ADJUSTED`, downgrade to `REJECTED`.

## 5. ClaudeClient Integration Specification

`ClaudeClient` remains the evaluation orchestrator. Class names remain unchanged.

Required integration points:
1. Add `_run_reflection_audit(...)` async method.
2. Add `_apply_reflection_verdict(...)` helper to choose final candidate JSON.
3. Keep exactly one terminal Gatekeeper call:
   - `LLMEvaluationResponse.model_validate_json(final_candidate_json)`
4. Reflection result must be included in:
   - structured logs (`reflection_verdict`, flags, reason codes, latency),
   - persisted decision audit trail (`agent_decision_logs`) via deterministic reflection block.

Recommended persistence format without schema migration:
- Prefix `reasoning_log` with a machine-parseable reflection JSON envelope:
  - `[REFLECTION_AUDIT]{...}[/REFLECTION_AUDIT]`
- Then append model reasoning text.

If migration is approved, preferred columns:
- `reflection_verdict`
- `reflection_flags_json`
- `reflection_note`
- `reflection_latency_ms`

## 6. Latency Budget Policy (2.0s Chain Target)

Target: keep Router -> Sentiment -> Primary Evaluation -> Reflection within a 2.0s wall-clock budget where possible.

Budget strategy:
1. Start deadline timer at evaluation start (`t0`).
2. Each stage consumes from shared deadline using remaining time.
3. Reflection timeout uses remaining budget with an absolute floor (for example `max(0.15s, remaining_budget)`).
4. If budget is exhausted before reflection completion, auditor returns `REJECTED` with reason `BUDGET_EXHAUSTED`.

Safety behavior:
- The system must never skip reflection to save latency.
- On timeout or budget exhaustion, default conservative (`REJECTED` -> HOLD path), not permissive.

## 7. Financial Integrity Rules for Reflection Adjustments

1. Reflection must not recompute bankroll USDC amounts using float arithmetic.
2. Any risk adjustment metadata involving size/exposure must be represented as Decimal-safe strings.
3. If adjusted candidate changes sizing fields, conversion/validation must use `Decimal` prior to any money-path arithmetic.
4. Final execution sizing remains governed by Gatekeeper + existing bankroll/exposure logic.

## 8. Acceptance Criteria

1. Reflection executes for every non-crash evaluation path before execution eligibility is determined.
2. `LLMEvaluationResponse` remains the final immutable Gatekeeper; no reflection bypass allowed.
3. Reflection verdict (`APPROVED|ADJUSTED|REJECTED`) and flags are emitted in structured logs for every evaluated snapshot.
4. Reflection artifacts are persisted in the decision audit trail (`agent_decision_logs`) in a machine-parseable format.
5. `ADJUSTED` path is bounded and deterministic (single-pass, no infinite loops).
6. `REJECTED` path guarantees no execution enqueue.
7. Decimal integrity remains preserved for any reflection-driven risk adjustment.
8. Existing tests pass and coverage remains >= 80%.

## 9. Verification Checklist

1. Unit test verdict parser and schema validation for `APPROVED`, `ADJUSTED`, `REJECTED`.
2. Unit test malformed `ADJUSTED` payload -> forced `REJECTED`.
3. Integration test: approved path still passes terminal `LLMEvaluationResponse` validation.
4. Integration test: rejected path never reaches execution queue.
5. Integration test: reflection timeout/budget exhaustion -> conservative HOLD + persisted audit metadata.
6. Integration test: structured reflection fields appear in logs and persisted decision audit trail.
7. Full regression:
   - `pytest --asyncio-mode=auto tests/`
   - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m`
