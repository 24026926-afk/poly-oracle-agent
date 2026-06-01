# Business Logic - WI-67 Configurable Gatekeeper Risk Profiles

## Objective

Make the terminal Gatekeeper's five risk thresholds — plus the Kelly fraction — **configurable per run** so an operator "risk profile" (e.g. a less-conservative dry-run experiment, or the eventual aggressive "ride or die" mode) actually reaches the terminal `LLMEvaluationResponse` validator. The thresholds are passed as Pydantic **validation context** from the construction sites; the existing module-level constants in `src/schemas/llm.py` are retained as **conservative fail-safe defaults**.

### Why

The thresholds the Gatekeeper enforces (`MIN_CONFIDENCE`, `MIN_EV_THRESHOLD`, `MAX_SPREAD_PCT`, `MAX_EXPOSURE_PCT`, `MIN_TTR_HOURS`) were hardcoded module constants read **directly** inside `LLMEvaluationResponse._apply_gatekeeper_filters`, completely separate from `AppConfig.min_confidence` (etc.), which only the downstream `ExecutionRouter` consumed. Because `LLMEvaluationResponse` is the **terminal** gate and forces a candidate to HOLD before the router ever sees it, lowering the env var / config value changed nothing at the gate: a positive-EV candidate at confidence `0.675` stayed HOLD no matter what `config.min_confidence` said. Any config-driven loosening was therefore a **silent no-op**. WI-67 closes that gap — it routes already-existing config values into the gate that should have honored them. The same parameterization is the foundation the aggressive-mode work requires.

### Why this is safe

The change adds **no new bypass**: the Gatekeeper still runs unconditionally as the terminal validator. The module constants remain the defaults and their values are unchanged, so with default config the behavior is **byte-identical** to pre-WI-67. A missing or partial context falls back to the conservative constant **per knob** (fail-safe — a partial profile never loosens an unspecified gate). The `EV > 0` hard floor stays **non-configurable**. Validation context is per-call, so there is **no global mutable state** (async/concurrency-safe). `dry_run`, signing, broadcasting, the Decimal sizing arithmetic in `ExecutionRouter`, discovery, and persistence are untouched.

## Data Models

Pydantic schema names only:

- `LLMEvaluationResponse` (existing, `src/schemas/llm.py`) — the terminal Gatekeeper. `_apply_gatekeeper_filters` and the nested `ProbabilisticEstimate.compute_kelly_and_ev` now accept `info: ValidationInfo` and read thresholds from `info.context`, each defaulting to the module constant. Structure, fields, and persisted shape unchanged.
- Module constants `MIN_CONFIDENCE`, `MIN_EV_THRESHOLD`, `MAX_SPREAD_PCT`, `MAX_EXPOSURE_PCT`, `MIN_TTR_HOURS`, `KELLY_FRACTION` (existing) — values unchanged; now serve as the conservative fail-safe defaults.
- `AppConfig` (existing, `src/core/config.py`) — **no new field**; it already carries `min_confidence`, `min_ev_threshold`, `max_spread_pct`, `max_exposure_pct`, `min_ttr_hours`, `kelly_fraction`. These become effective at the terminal gate for the first time.
- `BacktestConfig` (existing, `src/schemas/execution.py`) — add three fields: `max_spread_pct`, `max_exposure_pct`, `min_ttr_hours` (`Decimal`, defaults `0.015` / `0.03` / `4.0`), registered in the existing `float`-rejection field validator. (`kelly_fraction`, `min_confidence`, `min_ev_threshold` already existed but were dead config — now wired into the gate.)

No new persisted schema, no DB model change, no enum, no migration.

## Key Rules

1. The six context keys honored by the Gatekeeper are `min_confidence`, `min_ev_threshold`, `max_spread_pct`, `max_exposure_pct`, `min_ttr_hours`, `kelly_fraction`. Each is read via `ctx.get(key, MODULE_CONSTANT)` (or equivalent) so an absent key uses the conservative constant.
2. The `EV > 0` hard floor (`ev <= 0.0` → `EV_NON_POSITIVE`) is **not** configurable; only the `ev < min_ev_threshold` comparison reads context.
3. Context is supplied only via `model_validate_json(..., context=...)` / `model_validate(..., context=...)` at the construction sites; direct-constructor or no-context construction uses defaults (fail-safe). In production all construction flows through `model_validate_json`.
4. `ClaudeClient._risk_profile_context()` builds the dict from `self.config`, emitting **only keys the config actually carries** (`getattr` guard); an absent key is omitted so the schema falls back to its constant — the evaluation pipeline never crashes on a partial config. It is passed at both Gatekeeper construction sites (Stage-D terminal validation and the raw primary-candidate parse).
5. `BacktestRunner._risk_profile_context()` builds the dict from `self.config` (`BacktestConfig`, `Decimal` → `float`). `_gatekeeper_validate` (now an instance method, no longer `@staticmethod`) passes the context to both the `model_validate_json` call and the `model_validate` fallback.
6. `kelly_fraction` flows into the **nested** `ProbabilisticEstimate` (Pydantic validation context propagates to nested models), scaling `kelly_quarter`. `MAX_EXPOSURE_PCT` caps `final_position_size_pct = min(kelly_q, max_exposure)`.
7. Decimal integrity: `BacktestConfig` thresholds are `Decimal` (float-rejection validator). The gate comparisons inside the schema remain `float` — the **pre-existing** convention for `ev` / `confidence_score` / `spread_pct` / `position_size_pct`. WI-67 introduces **no new** `float` into any money / price / EV / PnL / sizing path; the `float()` conversion at the context boundary only mirrors the schema's existing float comparison.
8. No `print()`; `structlog` only. No change to `dry_run`, signing, broadcasting, discovery, Grok, or persistence.

## Edge Cases

1. No context → all conservative constants → behavior byte-identical to pre-WI-67 (regression-locked).
2. Partial context (some keys present) → present keys honored, missing keys → conservative constant.
3. Empty context `{}` → all conservative.
4. Config missing a threshold field (partial / stub / mock config) → `_risk_profile_context` omits that key → schema falls back to the constant; the pipeline does not raise.
5. Confidence `0.675` + `min_confidence=0.65` → passes (was forced HOLD under the `0.75` default).
6. EV in `(0.005, 0.02)` + `min_ev_threshold=0.005` → passes; EV `<= 0` → still blocked regardless of context.
7. `kelly_fraction=0.50` → `kelly_quarter` doubles vs the `0.25` default; `max_exposure_pct=0.10` → position cap rises to `min(kelly_q, 0.10)`.
8. `max_spread_pct=0.03` admits a ~2.2% spread that fails the 1.5% default; `min_ttr_hours=1.0` admits a ~2h market that fails the 4h default.
9. `BacktestConfig` constructed with a `float` threshold → rejected at construction by the float-rejection validator.

## Invariants

1. `LLMEvaluationResponse` remains the terminal, unconditional Gatekeeper; no execution path bypasses it.
2. Context defaults equal the module constants → default behavior is unchanged.
3. A missing or partial context never **loosens** a gate (fail-safe per knob); it can only fall back to the stricter conservative constant.
4. The `EV > 0` hard floor is non-configurable and always blocks a non-positive-edge candidate.
5. No global mutable state — thresholds are per-validation-call (async / concurrency-safe).
6. No new `float` arithmetic in money / price / EV / PnL / sizing paths; `BacktestConfig` thresholds are `Decimal`.
7. No `dry_run` weakening; no `DRY_RUN=false` behavior; no signing or broadcasting added.
8. No Alembic migration, no `Base.metadata.create_all()`, no persisted-schema change.
9. Tests cover: fail-safe (no / partial / empty context); each of the six knobs individually; the `EV > 0` non-configurable floor; Kelly scaling; the exposure cap; `ClaudeClient` and `BacktestRunner` context mirrors config; partial-config key omission.
