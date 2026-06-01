# Implementation Prompt - WI-67 Configurable Gatekeeper Risk Profiles

## Session Context

You are working in `poly-oracle-agent` after WI-65 (deterministic eval math) and WI-66 (reflection verdict calibration). Those WIs moved the trade bottleneck from broken arithmetic and hard reflection rejection onto the **confidence gate**: positive-EV candidates now appear but cluster just under `MIN_CONFIDENCE`, so they still HOLD. The operator wants to run a less-conservative dry-run (and, later, an aggressive "ride or die" mode) by lowering thresholds such as `MIN_CONFIDENCE`.

The blocker is architectural: the terminal Gatekeeper `LLMEvaluationResponse` enforces its five thresholds via **hardcoded module constants** read directly inside its Pydantic validators (`src/schemas/llm.py`), entirely separate from `AppConfig`. `config.min_confidence` is consumed only by the downstream `ExecutionRouter`, but the terminal Gatekeeper forces HOLD **before** the router ever sees a candidate. So lowering the config / env value is a **silent no-op** at the gate.

WI-67 makes the Gatekeeper's thresholds configurable by passing them as **Pydantic validation context**, with the module constants kept as conservative fail-safe defaults. This is the foundation the aggressive-mode work needs; it does not implement the aggressive mode itself, the time-window lifecycle, or any safety-rail bounds (later, separate WIs).

Current baseline:

- `src/schemas/llm.py`: module constants `KELLY_FRACTION=0.25`, `MIN_CONFIDENCE=0.75`, `MAX_SPREAD_PCT=0.015`, `MAX_EXPOSURE_PCT=0.03`, `MIN_EV_THRESHOLD=0.02`, `MIN_TTR_HOURS=4.0`. `LLMEvaluationResponse._apply_gatekeeper_filters` reads them directly; `ProbabilisticEstimate.compute_kelly_and_ev` uses `KELLY_FRACTION`. `_enforce_decision_override` forces HOLD on any failed filter.
- All production construction of `LLMEvaluationResponse` flows through `model_validate_json`: `claude_client.py` (Stage-D terminal validation and the raw primary-candidate parse) and `backtest_runner.py` (`_gatekeeper_validate`, a `@staticmethod`). No direct-constructor call sites in `src/`.
- `AppConfig` (`src/core/config.py`) already carries `min_confidence`, `min_ev_threshold`, `max_spread_pct`, `max_exposure_pct`, `min_ttr_hours`, `kelly_fraction`.
- `BacktestConfig` (`src/schemas/execution.py`) carries `kelly_fraction`, `min_confidence`, `min_ev_threshold` (dead — unused by the runner), guarded by a `float`-rejection validator; it lacks `max_spread_pct`, `max_exposure_pct`, `min_ttr_hours`.
- `DRY_RUN=false`, live signing, live broadcasting, and any path bypassing `LLMEvaluationResponse` remain out of scope and forbidden.

Before implementing, read:

- `AGENTS.md`, `STATE.md`, `README.md`, `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-67-configurable-gatekeeper-risk-profiles.md`
- `src/schemas/llm.py` (`LLMEvaluationResponse`, `_apply_gatekeeper_filters`, `ProbabilisticEstimate.compute_kelly_and_ev`, the six risk constants)
- `src/agents/evaluation/claude_client.py` (the two `model_validate_json` call sites; `self.config`)
- `src/backtest_runner.py` (`BacktestRunner._gatekeeper_validate`, `self.config`)
- `src/schemas/execution.py` (`BacktestConfig` and its float-rejection validator)

## Objective

Route the six risk thresholds from config into the terminal Gatekeeper via Pydantic validation context, defaulting to the conservative module constants when context (or a key) is absent. Wire both the live path (`ClaudeClient`) and the backtest path (`BacktestRunner`) so config — not a hardcoded constant — is the source of truth for the gate, while default behavior stays byte-identical.

## Inputs

- `LLMEvaluationResponse` validators and the six module constants (defaults).
- `AppConfig` (six existing threshold fields) and `BacktestConfig` (three to add).
- `info.context` (`pydantic.ValidationInfo`) supplied at the `model_validate_json` / `model_validate` call sites.
- No new Python package dependencies. Standard library, `pydantic` (>=2.x, `ValidationInfo`), `Decimal`, `json` only.

## Outputs

- `src/schemas/llm.py`:
  - Import `ValidationInfo`.
  - `_apply_gatekeeper_filters(self, info)` reads `min_confidence`, `min_ev_threshold`, `max_spread_pct`, `max_exposure_pct`, `min_ttr_hours` from `info.context` (default = module constant). Keep `ev <= 0.0` (`EV_NON_POSITIVE`) **non-configurable**.
  - `ProbabilisticEstimate.compute_kelly_and_ev(self, info)` reads `kelly_fraction` from `info.context` (default `KELLY_FRACTION`).
- `src/agents/evaluation/claude_client.py`:
  - `_risk_profile_context() -> dict[str, float]` — builds the dict from `self.config`, emitting only keys the config carries (`getattr` guard; absent key omitted → schema falls back to the constant; never raises on a partial config).
  - Pass `context=self._risk_profile_context()` at both `LLMEvaluationResponse.model_validate_json` sites.
- `src/schemas/execution.py`:
  - `BacktestConfig` + `max_spread_pct`, `max_exposure_pct`, `min_ttr_hours` (`Decimal`, defaults `0.015` / `0.03` / `4.0`), added to the float-rejection validator list.
- `src/backtest_runner.py`:
  - `_risk_profile_context() -> dict[str, float]` from `self.config` (`Decimal` → `float`).
  - `_gatekeeper_validate` becomes an instance method passing `context` to both `model_validate_json` and the `model_validate` fallback.
- `tests/unit/test_WI-67-configurable-gatekeeper-risk-profiles.py` — unit tests (RED first, then GREEN).
- `tests/integration/test_wi33_backtest_runner.py` — update gatekeeper fakes to accept the `context` kwarg.
- `STATE.md` — WI-67 completion entry on `/wi-done`.

## Acceptance Criteria

1. With no context, a confidence-`0.675` positive-EV candidate is still forced to HOLD on `MIN_CONFIDENCE`; behavior is byte-identical to pre-WI-67.
2. With `context={"min_confidence": 0.65}`, the same candidate passes the gate (BUY, `position_size_pct > 0`).
3. Partial context honors present keys and falls back to the conservative constant for absent keys; empty context `{}` is fully conservative.
4. Each of the six knobs is individually configurable via context (confidence, EV, spread, exposure cap, TTR, Kelly fraction), verified independently.
5. The `EV > 0` floor is non-configurable: a non-positive-edge candidate is held even with `min_ev_threshold` set negative.
6. `kelly_fraction` propagates into the nested `ProbabilisticEstimate`, scaling `kelly_quarter` proportionally; `kelly_full` is unchanged by the fraction.
7. `ClaudeClient._risk_profile_context` mirrors `self.config`; a partial config yields a context omitting the absent keys and never raises.
8. `BacktestConfig` gains the three Decimal fields (defaults match the constants) and rejects `float` values; `BacktestRunner._risk_profile_context` mirrors it; the backtest gatekeeper honors an aggressive profile.
9. Full regression passes with coverage ≥ 80%; no existing test regresses. `ruff format` and `ruff check` clean.

## Anti-Patterns

- Do not mutate the module constants at runtime or introduce any global mutable threshold state; thresholds must be per-validation-call context.
- Do not make the `EV > 0` floor configurable.
- Do not let a missing / partial context **loosen** a gate; absent keys must fall back to the stricter conservative constant.
- Do not introduce `float` into money / price / EV / PnL / sizing math; `BacktestConfig` thresholds stay `Decimal`. (Converting to `float` only at the context boundary, to match the schema's pre-existing float comparison, is acceptable; do not refactor the schema's float comparisons to Decimal in this WI.)
- Do not change the Gatekeeper's filter order, the override logic, or the default constant values.
- Do not add a bypass of `LLMEvaluationResponse`; do not weaken `DRY_RUN`; do not add signing or broadcasting.
- Do not implement the aggressive risk-profile presets, the time-window lifecycle, or safety-rail bounds (separate WIs).
- Do not add DB schema/fields, Alembic migrations, or `Base.metadata.create_all()`.
- Do not add `print()` or new package dependencies; do not delete files outside this WI's scope.

## Dependencies

- WI-65 — deterministic eval math (authoritative market facts feed the gate).
- WI-66 — reflection verdict calibration (soft-flag confidence penalty; the candidates now clustering under the confidence gate).
- `src/schemas/llm.py` — `LLMEvaluationResponse`, `ProbabilisticEstimate`, the six risk constants.
- `src/agents/evaluation/claude_client.py` — the two terminal-Gatekeeper construction sites; `self.config`.
- `src/backtest_runner.py` — `BacktestRunner._gatekeeper_validate`; `self.config`.
- `src/schemas/execution.py` — `BacktestConfig` and its float-rejection validator.

## Target Layer

Schema / contract boundary (the terminal Gatekeeper) plus its two construction sites in the cognitive-evaluation layer (`ClaudeClient`) and the offline-replay layer (`BacktestRunner`). It changes **where** the gate's thresholds come from (config via validation context, defaulting to the conservative constants); it does not touch market discovery, execution routing, signing, broadcasting, the Gatekeeper's arithmetic or filter order, or persistence. `LLMEvaluationResponse` remains the terminal, unconditional safety gate.
