# Risk Management Specification - Poly-Oracle-Agent

**Document Version:** 1.1.0  
**Aligned With:** `STATE.md` (v0.4.0-draft, 2026-03-26), `README.md`, and `src/schemas/llm.py`

## 1. Scope

This document defines the quantitative risk controls applied between Layer 3 (evaluation) and Layer 4 (execution).

It is the canonical policy for:
- expected value and Kelly-based sizing logic
- gatekeeper filter thresholds
- execution eligibility
- auditability requirements

## 2. Non-Negotiable Safety Invariants

1. `LLMEvaluationResponse` is the final pre-execution gate.
2. Any failed gatekeeper filter forces a HOLD outcome.
3. Monetary calculations must preserve financial integrity (`Decimal` for USDC sizing/exposure paths).
4. `dry_run=True` blocks all signing and broadcast side effects.
5. Position sizing is capped at `min(quarter_kelly_size, 0.03 * bankroll)`.

## 3. Core Quantitative Model

For binary outcome contracts:

- net odds: `b = (1 - p_market) / p_market`
- expected value: `EV = (p_true * b) - (1 - p_true)`
- equivalent EV form: `EV = (p_true / p_market) - 1`
- full Kelly: `f* = (b * p_true - (1 - p_true)) / b`
- quarter-Kelly: `f_quarter = 0.25 * f*`

Interpretation:
- `EV <= 0` means no structural edge and must result in HOLD.
- Positive EV alone is not sufficient for execution; all gatekeeper filters must pass.

## 4. Gatekeeper Filter Chain

Filters are evaluated in order inside `LLMEvaluationResponse`:

1. `EV_NON_POSITIVE`: `EV > 0`
2. `MIN_EV_THRESHOLD`: `EV >= 0.02`
3. `MIN_CONFIDENCE`: `confidence_score >= 0.75`
4. `MAX_SPREAD`: `spread_pct <= 0.015`
5. `MIN_TIME_TO_RESOLUTION`: `hours_to_resolution >= 4.0` (when market end date exists)

Additional risk modifier:
- If `information_asymmetry_flag=True` and filters pass, quarter-Kelly allocation is halved before final cap.

Final position sizing behavior:
- `final_position_size_pct = min(kelly_quarter_adjusted, 0.03)` when all filters pass
- `final_position_size_pct = 0` when any filter fails

## 5. Configuration Constants

| Parameter | Runtime Constant | Default |
|---|---|---|
| Quarter-Kelly multiplier | `KELLY_FRACTION` | `0.25` |
| Min confidence | `MIN_CONFIDENCE` | `0.75` |
| Max spread | `MAX_SPREAD_PCT` | `0.015` |
| Max exposure | `MAX_EXPOSURE_PCT` | `0.03` |
| Min EV threshold | `MIN_EV_THRESHOLD` | `0.02` |
| Min time-to-resolution (hours) | `MIN_TTR_HOURS` | `4.0` |

## 6. Decision Override Rules

After filters are applied:
- If any filter fails, output is forcibly normalized to:
  - `decision_boolean=False`
  - `recommended_action=HOLD`
  - `position_size_pct=0.0`
- A gatekeeper audit prefix is added to `reasoning_log`.

Consistency invariants must hold:
- `decision_boolean=True` cannot coexist with `recommended_action=HOLD`.
- `decision_boolean=False` cannot have positive `position_size_pct`.
- `decision_boolean=True` cannot have non-positive EV.

## 7. Decimal Financial Integrity Rules

The following paths are financially sensitive and must use Decimal-safe handling:

1. Micro-USDC conversion:
   - `size_usdc = Decimal(str(maker_amount)) / Decimal('1e6')`
2. Aggregate exposure:
   - sums for `PENDING` + `CONFIRMED` execution rows
   - cast DB numeric result via `str()` before `Decimal(...)`
3. Bankroll and exposure cap calculations:
   - no float arithmetic in money paths

## 8. Audit Trail Requirements

Every decision must remain auditable through persisted decision logs, including:
- computed EV
- computed Kelly values
- triggered filter (if any)
- override status
- final position size
- full reasoning log with `[GATEKEEPER]` prefix context

## 9. Phase 4 Cognitive Constraints

Phase 4 planning introduces Routing, Prompt Chaining, and Reflection. These are upstream cognitive steps only.

They must not:
- bypass `LLMEvaluationResponse`
- change gatekeeper thresholds without explicit risk policy update
- alter Decimal money rules
- introduce synchronous execution bottlenecks
