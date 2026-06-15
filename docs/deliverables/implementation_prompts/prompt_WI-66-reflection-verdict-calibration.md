# Implementation Prompt - WI-66 Reflection Verdict Calibration

## Session Context

You are working in `poly-oracle-agent` immediately after WI-65 (deterministic eval math). WI-65 removed the arithmetic-flag rejection classes and made the Gatekeeper's market facts authoritative. The remaining reason the bot never trades is that the Stage-C reflection auditor REJECTs ~95% of candidates on **soft, subjective bias flags** (`overconfidence_unsupported` ~509, `narrative_anchoring` ~330), and a REJECTED verdict calls `_build_hold_candidate`, which sets `confidence_score=0.0` → Gatekeeper `MIN_CONFIDENCE` → HOLD.

WI-66 is **fix B** of the remediation sequence. It downgrades soft-bias REJECTs to a confidence penalty (so high-conviction candidates can still reach the Gatekeeper) while keeping hard-integrity and infrastructure REJECTs fail-closed. Fix C (discovery price-band) is a later, separate WI — do not implement it here.

Current baseline:

- `ClaudeClient._apply_reflection_verdict(reflection, primary_candidate_json)` maps APPROVED → primary unchanged; ADJUSTED → `reflection.corrected_candidate_json`; REJECTED → `_build_hold_candidate(primary)` (confidence `0.0`).
- `_run_reflection_audit` returns a `ReflectionResponse` whose `verdict` is REJECTED on three infrastructure failures, with `audit_note` markers `BUDGET_EXHAUSTED`, `REFLECTION_ERROR`, and `ADJUSTED_MISSING_PAYLOAD`.
- `_build_hold_candidate(primary_candidate_json)` loads the JSON, sets `decision_boolean=False`, `recommended_action="HOLD"`, `confidence_score=0.0`.
- `ClaudeClient._evaluate` applies the verdict, then (WI-65) `_apply_authoritative_market_facts(final_json, wi14_snapshot)`, then `LLMEvaluationResponse.model_validate_json(final_json)`.
- `LLMEvaluationResponse` (`src/schemas/llm.py`) is the terminal Gatekeeper: `_apply_gatekeeper_filters` enforces EV/`MIN_CONFIDENCE`/`MAX_SPREAD_PCT`/`MIN_TTR_HOURS`/`MAX_EXPOSURE_PCT`; `_enforce_decision_override` forces HOLD when any filter fails. `MIN_CONFIDENCE = 0.75`.
- `ReflectionResponse` has free-text `bias_flags`, `consistency_flags`, `risk_flags`, and `audit_note`.
- `PromptFactory.build_reflection_prompt` (post-WI-65) audits evidence/bias/uncertainty/coherence and states EV/spread are system-computed.
- `DRY_RUN=false`, live signing, live broadcasting, and any path bypassing `LLMEvaluationResponse` remain out of scope and forbidden.

Before implementing, read:

- `AGENTS.md`, `STATE.md`, `README.md`, `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-66-reflection-verdict-calibration.md`
- `docs/deliverables/business_logic/business_logic_WI-65-deterministic-eval-math.md` (immediate predecessor)
- `src/schemas/llm.py` (`ReflectionResponse`, `ReflectionVerdict`, `LLMEvaluationResponse`, `MIN_CONFIDENCE`)
- `src/agents/evaluation/claude_client.py` (`_apply_reflection_verdict`, `_run_reflection_audit`, `_build_hold_candidate`, `_evaluate`)
- `src/agents/context/prompt_factory.py` (`build_reflection_prompt`)
- `src/core/config.py` (`AppConfig`, Decimal-coercion validator list)

## Objective

Add a deterministic severity classifier for reflection REJECTs and a confidence-penalty downgrade path so that soft-bias-only REJECTs lower confidence (and let the terminal Gatekeeper decide) instead of forcing HOLD, while hard-integrity and infrastructure REJECTs remain fail-closed. Add one tunable config factor (default `0.90`). Update the reflection prompt to use an enumerated flag vocabulary and reserve REJECT for hard failures.

## Inputs

- `ReflectionResponse` (verdict, the three flag lists, `audit_note`).
- `AppConfig.reflection_soft_flag_confidence_factor` — new `Decimal` field (default `Decimal("0.90")`, `> 0`, `<= 1`).
- The primary candidate JSON chosen before the REJECTED branch.
- Existing `_build_hold_candidate`, `_apply_reflection_verdict`, `_evaluate`, and the terminal `LLMEvaluationResponse` validation.
- No new Python package dependencies. Standard library, `pydantic`, `structlog`, `Decimal`, `json` only.

## Outputs

- `src/schemas/llm.py` — new `ReflectionSeverity(str, Enum)` with `HARD`, `SOFT`, `NONE`. (Enum only.)
- `src/core/config.py` — new `reflection_soft_flag_confidence_factor: Decimal` field (default `Decimal("0.90")`, `gt=0`, `le=1`), added to the Decimal-coercion validator list, with a clear description.
- `src/agents/evaluation/claude_client.py`:
  - `_classify_reflection_severity(reflection) -> ReflectionSeverity` — pure classifier (infra `audit_note` → HARD; hard-keyword flag → HARD; any other flag → SOFT; no flags → NONE). Define the hard-integrity keyword set and the infra-marker set as module-level frozensets.
  - `_build_confidence_penalized_candidate(primary_candidate_json, factor) -> str` — primary candidate with `confidence_score` multiplied by `factor` (Decimal math, clamped `[0,1]`); a non-dict candidate is returned unchanged (no raise) so the terminal-validation `try/except` conservatively skips it — never routed to `_build_hold_candidate`, which would raise on a non-dict.
  - `_apply_reflection_verdict` REJECTED branch: `SOFT` → penalized candidate; `HARD`/`NONE` → `_build_hold_candidate` (unchanged). Emit `structlog` `reflection.soft_flag_downgrade` on downgrade.
- `src/agents/context/prompt_factory.py` — `build_reflection_prompt`: enumerated flag vocabulary + "reserve REJECT for hard integrity failures; record soft bias as flags" guidance (prompt text only).
- `.env.example` — document `REFLECTION_SOFT_FLAG_CONFIDENCE_FACTOR=0.90` with a one-line explanation.
- `tests/unit/test_WI-66-reflection-verdict-calibration.py` — unit tests (RED first, then GREEN).
- `STATE.md` — WI-66 completion entry on `/wi-done`.

## Acceptance Criteria

1. `_classify_reflection_severity` returns HARD for each infra marker (`BUDGET_EXHAUSTED`, `REFLECTION_ERROR`, `ADJUSTED_MISSING_PAYLOAD`) and for any hard-integrity keyword flag; SOFT for soft-bias-only and for unrecognized flags; NONE for a flagless REJECT.
2. On a soft-only REJECT, `_apply_reflection_verdict` returns the primary candidate with `confidence_score` multiplied by the configured factor (verified by parsing the returned JSON); on HARD/NONE it returns a `confidence_score=0.0` HOLD candidate.
3. A penalized candidate with high primary confidence (e.g. `0.95`, factor `0.90`) validates through `LLMEvaluationResponse` with confidence ≥ `MIN_CONFIDENCE`, and (given passing EV/spread/TTR) is not forced to HOLD by the Gatekeeper.
4. A penalized candidate with marginal primary confidence (e.g. `0.80`, factor `0.90`) is forced to HOLD by the Gatekeeper `MIN_CONFIDENCE` filter.
5. The confidence penalty is computed with `Decimal` and clamped to `[0,1]`; no money/price/EV/PnL float arithmetic is introduced.
6. `reflection_soft_flag_confidence_factor` validates `gt=0` and `le=1`; an out-of-range value raises at config construction.
7. A non-dict primary candidate routed to the penalized builder is returned unchanged without raising (terminal validation then conservatively skips it — no trade).
8. `build_reflection_prompt` output contains the enumerated flag vocabulary and the "reserve REJECT for hard failures" guidance.
9. APPROVED and ADJUSTED verdict handling is unchanged; the WI-65 authoritative-fact override still runs for the penalized path.
10. Full regression passes with coverage ≥ 80%; no existing test regresses. `ruff format` and `ruff check` clean.

## Anti-Patterns

- Do not downgrade infrastructure REJECTs (timeout/error/missing-payload) — they must stay fail-closed HOLD.
- Do not let a soft-flag downgrade bypass the terminal Gatekeeper; `MIN_CONFIDENCE`/EV/spread/TTR/exposure must still apply unconditionally.
- Do not raise confidence, fabricate a trade, or alter EV/spread/sizing in the downgrade — only lower confidence.
- Do not change the Gatekeeper's arithmetic or filter order, `MIN_CONFIDENCE`, or any risk constant.
- Do not introduce `float` arithmetic in money/price/EV/PnL paths; compute the penalty in `Decimal`.
- Do not change `_build_hold_candidate`'s existing behavior; add the penalized builder alongside it.
- Do not implement the discovery price-band (fix C) or re-tune the primary prompt (WI-65) here.
- Do not add new schemas/fields on DB models, Alembic migrations, or `Base.metadata.create_all()`.
- Do not weaken `DRY_RUN`, add execution/signing/broadcasting, or any path that bypasses `LLMEvaluationResponse`.
- Do not add `print()`. Use `structlog`. Do not log secrets or high-cardinality fields.
- Do not add new Python package dependencies. Do not delete files outside this WI's scope.

## Dependencies

- WI-65 — deterministic eval math (removed arithmetic flags; authoritative market facts). Direct predecessor.
- `src/agents/evaluation/claude_client.py` — `_apply_reflection_verdict`, `_build_hold_candidate`, `_evaluate`.
- `src/schemas/llm.py` — `ReflectionResponse`, `ReflectionVerdict`, `LLMEvaluationResponse`, `MIN_CONFIDENCE`.
- `src/agents/context/prompt_factory.py` — `build_reflection_prompt`.
- `src/core/config.py` — `AppConfig` and its Decimal-coercion validator.

## Target Layer

Cognitive evaluation (Layer 3): reflection verdict application and prompt construction in `ClaudeClient` / `PromptFactory`, plus one `AppConfig` field. It refines how an advisory QC verdict maps to a candidate; it does not touch market discovery, execution routing, signing, broadcasting, the terminal Gatekeeper's arithmetic, or persistence. The terminal `LLMEvaluationResponse` Gatekeeper remains the unconditional safety gate.
