# Business Logic - WI-66 Reflection Verdict Calibration

## Objective

Stop the Stage-C reflection auditor's **soft, subjective bias concerns** from forcing a hard HOLD. Reserve a REJECT→HOLD outcome for genuine integrity failures and infrastructure failures; when a REJECT is justified only by soft bias flags (overconfidence, narrative anchoring, recency/confirmation bias, unsupported assertion), downgrade it to a **confidence-penalized** candidate so a genuinely high-conviction estimate can still reach the terminal Gatekeeper, while a marginal one still HOLDs through the existing `MIN_CONFIDENCE` filter.

### Why

After WI-65 removed the arithmetic-flag rejection classes, the dominant remaining reason the bot never trades is that the reflection auditor REJECTs ~95% of candidates, and a REJECTED verdict calls `_build_hold_candidate`, which sets `confidence_score=0.0` → Gatekeeper `MIN_CONFIDENCE` → HOLD. The largest rejection flags are soft and subjective: `overconfidence_unsupported` (~509 across spellings) and `narrative_anchoring` (~330). These are advisory quality concerns, not safety violations — yet a single such flag currently produces the same fatal outcome as a real data-integrity failure.

### Why this is safe

`LLMEvaluationResponse` is the **terminal, unconditional** Gatekeeper. It always runs after the reflection verdict and independently enforces EV ≥ threshold, `MIN_CONFIDENCE`, `MAX_SPREAD_PCT`, `MIN_TTR_HOURS`, and `MAX_EXPOSURE_PCT`, forcing HOLD on any failure. The reflection stage is advisory quality control layered *on top of* that gate. Reframing a soft-bias REJECT as a confidence **penalty** (rather than a forced zero) does not bypass any safety limit: the Gatekeeper still guards execution, still HOLDs marginal candidates, and still caps sizing at quarter-Kelly / 3%. Hard-integrity and infrastructure REJECTs remain fail-closed.

This is **fix B** of the remediation sequence (A = WI-65 deterministic eval math; C = discovery price-band, a later WI). It is the change expected to actually lift the trade rate above zero. Unlike WI-64, it is **default-on**: the new behavior is the intended behavior, governed by a single tunable factor.

## Data Models

Pydantic schema names only:

- `ReflectionSeverity` (new enum, `str`-mixed, in `src/schemas/llm.py`) — values `HARD`, `SOFT`, `NONE`. Classifies a reflection REJECT for downgrade eligibility. (Enum only; no persisted field, no migration.)
- `ReflectionResponse` (existing, `src/schemas/llm.py`) — unchanged structurally; its free-text `bias_flags` / `consistency_flags` / `risk_flags` and `audit_note` are read by the classifier.
- `LLMEvaluationResponse` (existing) — unchanged; remains the terminal Gatekeeper. The downgrade produces a candidate it validates normally.
- `AppConfig` (existing, `src/core/config.py`) — add one field: `reflection_soft_flag_confidence_factor: Decimal` (default `Decimal("0.90")`, constrained `> 0` and `<= 1`). Registered in the existing Decimal-coercion field-validator list.
- `MarketCognitiveCircuitBreaker` / budget guard — unchanged.

No new persisted schema, no new enum value on any DB model, no migration.

## Key Rules

1. A new pure classifier `_classify_reflection_severity(reflection) -> ReflectionSeverity` lives in the evaluation layer:
   - `audit_note` matching an infrastructure-failure marker (`BUDGET_EXHAUSTED`, `REFLECTION_ERROR`, `ADJUSTED_MISSING_PAYLOAD`) → `HARD`.
   - Otherwise, any flag (across all three flag lists) matching the enumerated **hard-integrity** keyword set (e.g. fabricated/hallucinated data, stale/missing/unavailable data, crossed book, internal self-contradiction, explicit safety violation) → `HARD`.
   - Otherwise, if at least one flag is present → `SOFT` (this includes recognized soft bias flags and any unrecognized flag, because the terminal Gatekeeper still independently guards execution).
   - Otherwise (a REJECT carrying no flags and no infra marker) → `NONE`.
2. `_apply_reflection_verdict` REJECTED branch:
   - `HARD` or `NONE` → `_build_hold_candidate(primary)` (confidence `0.0` → HOLD), exactly as today (fail-closed).
   - `SOFT` → `_build_confidence_penalized_candidate(primary, factor)`: the primary candidate with `confidence_score` multiplied by `reflection_soft_flag_confidence_factor`, everything else unchanged. The Gatekeeper then decides.
3. `_build_confidence_penalized_candidate` computes the penalty with `Decimal`: `penalized = Decimal(str(original_confidence)) * factor`, clamped to `[0, 1]`, serialized at the JSON boundary. `decision_boolean` and `recommended_action` are left as the primary stated them; the Gatekeeper's existing `_apply_gatekeeper_filters` + `_enforce_decision_override` make the final call from the penalized confidence and the (authoritative, WI-65) EV/spread.
4. The penalty is applied **once** per soft-only REJECT (not per flag), keeping the outcome bounded and predictable.
5. `APPROVED` and `ADJUSTED` verdict handling is unchanged. The WI-65 authoritative-market-fact override still runs at the terminal chokepoint for every path, including the penalized candidate.
6. The reflection prompt (`PromptFactory.build_reflection_prompt`) is updated to: (a) present an enumerated flag vocabulary so the auditor's flags map cleanly onto the classifier; (b) instruct the auditor to reserve REJECT for hard integrity failures, use ADJUST for correctable issues, and record soft bias concerns as flags (which the system penalizes) rather than as blanket rejection. This is prompt text only.
7. When a soft-flag downgrade occurs, emit one `structlog` line (`reflection.soft_flag_downgrade`) with the applied factor and the count of contributing flags. No secrets, no high-cardinality fields. No `print()`.
8. `confidence_score` is an epistemic score, not a money/price/EV/PnL value; computing the penalty in `Decimal` and storing a `float` at the schema boundary introduces no money-path float arithmetic.
9. No change to: budget guard, cognitive cooldown, `dry_run`, execution routing, signing, broadcasting, discovery, Grok, the Gatekeeper's arithmetic, or persistence.

## Edge Cases

1. REJECT with a hard-integrity flag → `HARD` → HOLD (unchanged).
2. REJECT from reflection timeout/error/missing-payload (infra) → `HARD` → HOLD (fail-closed; never downgraded).
3. REJECT with soft bias flags only, high primary confidence (e.g. `0.95`, factor `0.90` → `0.855`) → penalized confidence ≥ `MIN_CONFIDENCE`; if EV/spread/TTR also pass, the candidate can trade.
4. REJECT with soft bias flags only, marginal confidence (e.g. `0.80`, factor `0.90` → `0.72`) → below `MIN_CONFIDENCE` → HOLD via the Gatekeeper. Correct.
5. REJECT with a mix of one soft and one hard flag → `HARD` → HOLD.
6. REJECT with no flags and no infra marker → `NONE` → HOLD (fail-closed).
7. `reflection_soft_flag_confidence_factor == Decimal("1.0")` → soft-only REJECTs keep full confidence (auditor soft flags become advisory only); the Gatekeeper still gates. Allowed.
8. Factor out of range (`<= 0` or `> 1`) → rejected at config validation; the system never runs with an invalid factor.
9. Unrecognized free-text flag on a REJECT → `SOFT` (penalize, let the Gatekeeper decide), because the terminal Gatekeeper independently enforces every hard limit; reflection cannot be the sole safety gate.
10. Primary candidate JSON is `null`/non-dict (degenerate) → penalized builder, like `_build_hold_candidate`, must not raise; on a non-dict candidate it falls back to the HOLD path.
11. APPROVED / ADJUSTED verdicts → unchanged; no penalty applied.

## Invariants

1. Hard-integrity REJECTs and all infrastructure-failure REJECTs always force HOLD (`confidence_score=0.0`). Fail-closed is preserved for every non-soft path.
2. A soft-flag downgrade never bypasses the terminal Gatekeeper: `MIN_CONFIDENCE`, EV threshold, `MAX_SPREAD_PCT`, `MIN_TTR_HOURS`, `MAX_EXPOSURE_PCT`, and the quarter-Kelly / 3% sizing cap all still apply unconditionally.
3. The downgrade only ever **lowers** confidence relative to the primary candidate; it never raises confidence, fabricates a trade, or alters EV/spread/sizing.
4. The confidence penalty is computed in `Decimal`; no money/price/EV/PnL float arithmetic is introduced.
5. `LLMEvaluationResponse` remains the terminal Gatekeeper; no execution path bypasses it. No `dry_run` weakening; no `DRY_RUN=false` behavior.
6. No Alembic migration, no `Base.metadata.create_all()`, no persisted-schema change.
7. The classifier is a pure function of the `ReflectionResponse`; it performs no I/O, no DB access, no network call.
8. With `reflection_soft_flag_confidence_factor` set such that no penalized candidate can clear `MIN_CONFIDENCE` (e.g. a very low factor), behavior degrades safely toward the pre-WI-66 all-HOLD outcome — never toward unsafe trading.
9. Tests cover: each `ReflectionSeverity` classification (hard keyword, infra note, soft-only, unknown→soft, none); soft-only downgrade preserves and penalizes confidence; hard/infra/none still HOLD; Gatekeeper still HOLDs a penalized-but-marginal candidate and can pass a penalized-but-strong one; factor boundary (1.0 and validation rejection); non-dict candidate falls back to HOLD; APPROVED/ADJUSTED unchanged; Decimal penalty integrity.
