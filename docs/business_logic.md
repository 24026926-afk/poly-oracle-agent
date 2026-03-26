# Business Logic - Poly-Oracle-Agent

**Document Version:** 1.1.0  
**Aligned With:** `STATE.md` (v0.4.0-draft, 2026-03-26), `README.md`, and `docs/risk_management.md`

## 1. Single Source of Trade Truth

This document defines the minimum decision logic that must be true before any BUY execution path is allowed.

Core rule:
- `Action = BUY` is only valid when expected value is positive and the full gatekeeper chain passes.

## 2. Expected Value Rule (Mandatory)

For binary contracts, the decision edge is based on expected value:

- `EV = (p_true * b) - (1 - p_true)`
- where `b = (1 - p_market) / p_market`
- equivalent form: `EV = (p_true / p_market) - 1`

Hard rule:
- If `EV <= 0`, the system must HOLD.
- If `EV > 0`, trade is still conditional on all risk filters.

## 3. Gatekeeper Authority

`LLMEvaluationResponse` is the final decision authority before execution.

Required implications:
1. Any failed filter forces `decision_boolean=False`, `recommended_action=HOLD`, and `position_size_pct=0`.
2. A model-proposed BUY cannot bypass override logic.
3. Execution queue receives only gatekeeper-approved decisions.

## 4. Position Sizing Rule

Sizing follows quarter-Kelly with an absolute exposure cap:

- `f_quarter = 0.25 * f*`
- `position_size = min(kelly_size, 0.03 * bankroll)`

Additional constraints:
- Exposure checks must include `PENDING` and `CONFIRMED` positions.
- Money paths must keep Decimal-safe conversion and aggregation.

## 5. Execution Safety Rule

Even with an approved decision:
- If `dry_run=True`, no order signing or broadcasting may occur.
- Execution side effects are allowed only when `dry_run=False`.

## 6. Phase 4 Cognitive Planning Compatibility

Phase 4 introduces planned cognitive steps:
- WI-11 Market Router
- WI-12 Chained Prompt Factory
- WI-13 Reflection Auditor

These steps can improve analysis quality but cannot alter the core trade truth:
- No BUY without positive EV
- No execution without gatekeeper pass
- No bypass of financial integrity constraints
