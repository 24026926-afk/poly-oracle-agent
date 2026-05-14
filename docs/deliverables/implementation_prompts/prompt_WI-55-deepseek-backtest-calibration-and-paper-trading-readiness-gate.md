# Implementation Prompt - WI-55 DeepSeek Backtest Calibration and Paper-Trading Readiness Gate

## Session Context

You are working in `poly-oracle-agent` on Phase 15: LLM Cost Containment and DeepSeek Provider Optionality.

Current baseline:

- Phase 14 deployment and paper-trading operational tooling is complete.
- WI-52 added a hard LLM cost guard and cognitive circuit breaker before paid provider calls.
- WI-53 added market eligibility preflight, per-market evaluation deduplication, and bounded prompt queue backpressure.
- WI-54 added configurable DeepSeek V4 Pro provider support through the existing `anthropic` SDK while preserving the canonical `ClaudeClient` class.
- Phase 15 was triggered by a DigitalOcean paper-trading run that exhausted Claude usage while the bot repeatedly evaluated one market.
- `DRY_RUN=false` remains out of scope. Live signing, live broadcasting, and execution paths that bypass `LLMEvaluationResponse` remain forbidden.
- `LLMEvaluationResponse` remains the terminal Gatekeeper for every provider-produced trading decision.
- DeepSeek cannot become the recommended primary paper-trading provider until this WI produces a passing typed readiness verdict.
- Full-time Claude/DeepSeek shadow mode is prohibited by default because it doubles provider spend.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/PRD-v15.0.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-55-deepseek-backtest-calibration-and-paper-trading-readiness-gate.md`
- `docs/deliverables/business_logic/business_logic_WI-52-llm-cost-guard-and-cognitive-circuit-breaker.md`
- `docs/deliverables/business_logic/business_logic_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.md`
- `docs/deliverables/business_logic/business_logic_WI-54-configurable-deepseek-provider-via-anthropic-compatible-endpoint.md`
- `src/backtest_runner.py`
- `src/backtesting/validation.py`
- `src/backtesting/schemas.py`
- `src/agents/evaluation/claude_client.py`
- `src/core/config.py`
- `src/schemas/llm.py`
- Existing backtest, real-data validation, provider selection, cost guard, and Gatekeeper tests.

## Objective

Build a DeepSeek provider comparison and readiness gate on top of the existing real-data backtest and validation paths so the operator can determine whether DeepSeek V4 Pro is safe and economical enough to use as the primary provider in `DRY_RUN=true` paper trading.

## Inputs

- Historical market datasets compatible with the existing `BacktestDataLoader`.
- Existing real-data validation outputs and historical outcomes where available.
- Operator-selected provider configuration for Anthropic and DeepSeek.
- Provider comparison configuration, including JSON validity thresholds, calibration thresholds, EV/PnL thresholds, latency thresholds, cost-reduction thresholds, and optional bounded Claude sampling.
- Existing WI-52 budget guard and market cognitive circuit breaker outputs.
- Existing WI-53 market eligibility, dedupe, and backpressure outputs.
- Existing WI-54 provider metadata and provider usage accounting.
- Existing `LLMEvaluationResponse` Gatekeeper validation results.

## Outputs

- Typed provider comparison configuration, run, result, metric, recommendation, and report schemas.
- Typed deterministic DeepSeek readiness verdict and reason schemas.
- Provider-aware backtest or validation extension that reuses the existing backtesting path without duplicating trading logic.
- `scripts/run_llm_provider_comparison.py` for bounded provider comparison execution.
- `docs/backtests/phase15-deepseek-calibration-report.md` or another approved `docs/backtests/` report path.
- `docs/runbooks/deepseek-paper-trading-readiness.md`.
- Provider comparison report containing JSON validity, Gatekeeper pass/fail counts, decision distribution, confidence distribution, EV calibration, realized PnL/EV calibration where outcomes exist, latency distribution, token usage, estimated cost, budget block counts, cooldown block counts, and readiness verdict.
- Provider-specific calibration recommendations for confidence threshold, EV threshold, max output tokens, and cooldown/budget settings.
- Secret-safe and low-cardinality logs/metrics for provider comparison execution.
- `tests/unit/test_WI-55-deepseek-backtest-calibration-and-paper-trading-readiness-gate.py`.
- `tests/integration/test_WI-55-deepseek-backtest-calibration-and-paper-trading-readiness-gate.py`.

## Acceptance Criteria

1. Provider comparison runs only with `DRY_RUN=true` and cannot sign or broadcast orders.
2. Existing backtesting and validation logic is reused; the implementation does not fork a separate trading simulator.
3. DeepSeek and sampled Anthropic evaluations flow through the existing `ClaudeClient` provider selection path.
4. `LLMEvaluationResponse` remains the terminal Gatekeeper for every provider-produced decision.
5. Provider comparison report includes JSON validity rate.
6. Provider comparison report includes Gatekeeper validation pass/fail counts.
7. Provider comparison report includes decision distribution for `BUY`, `HOLD`, `SKIP`, and `SELL` if applicable.
8. Provider comparison report includes confidence distribution and EV calibration.
9. Provider comparison report includes realized PnL/EV calibration where historical outcomes exist and reports outcome coverage when they do not.
10. Provider comparison report includes latency distribution.
11. Provider comparison report includes provider/model token usage and estimated cost.
12. Provider comparison report includes budget and cooldown block counts.
13. All money, token-cost, EV, PnL, and provider spend calculations use `Decimal`.
14. Missing or malformed usage fields use conservative configured defaults and do not silently report zero cost.
15. Claude audit sampling is disabled by default and, when enabled, uses a bounded configured sample fraction.
16. Full-time shadow mode that calls both providers for every evaluation is not enabled by default.
17. Readiness verdict derivation is typed, deterministic, and ordered from hard rejection to limited recommendation to primary-ready recommendation.
18. DeepSeek cannot receive `PROVIDER_READY_FOR_DRY_RUN_PRIMARY` unless JSON validity, Gatekeeper validation, calibration, EV/PnL, latency, and cost-reduction thresholds all pass.
19. DeepSeek receives `PROVIDER_REJECTED_FOR_JSON_VALIDITY` when malformed or invalid JSON exceeds the configured tolerance.
20. DeepSeek receives `PROVIDER_REJECTED_FOR_NEGATIVE_EV` when realized outcome calibration shows unacceptable negative EV where historical outcomes exist.
21. DeepSeek receives `PROVIDER_REJECTED_FOR_COST_OR_LATENCY` when cost reduction or latency criteria fail.
22. DeepSeek receives `PROVIDER_NEEDS_THRESHOLD_RECALIBRATION` when validity is acceptable but confidence or EV thresholds require provider-specific adjustment.
23. DeepSeek receives `PROVIDER_READY_FOR_SAMPLED_AUDIT_ONLY` when it is promising but not safe for primary provider use.
24. Reports are written only under approved docs paths, preferably `docs/backtests/`.
25. Reports, logs, metrics, and tests do not contain raw prompts, raw reasoning text, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, or other high-cardinality sensitive identifiers.
26. Report generation fails closed or redacts when forbidden sensitive fields are detected.
27. Calibration recommendations are advisory only and do not mutate runtime configuration automatically.
28. The runbook explains how to enable DeepSeek as primary for `DRY_RUN=true` only after a passing readiness verdict.
29. Targeted WI tests pass, covering readiness verdict ordering, report redaction, Decimal cost math, failed JSON validity, negative EV rejection, sampled Claude audit mode, and the `DRY_RUN=true` invariant.
30. Full regression remains compatible with the documented baseline and coverage stays at or above 80%.
31. MAAP is run before commit for any change touching `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, or `src/backtest_runner.py`.

## Anti-Patterns

- Do not enable `DRY_RUN=false`, live signing, live broadcasting, or live trading approval.
- Do not bypass `LLMEvaluationResponse` for DeepSeek, Anthropic, backtests, or comparison reports.
- Do not duplicate the backtesting engine instead of extending the existing real-data validation path.
- Do not rename, alias, or wrap the canonical `ClaudeClient` class.
- Do not add the `openai` SDK or any other provider SDK.
- Do not use `float` for money, token cost, estimated spend, EV, Kelly, PnL, exposure, sizing, or calibration calculations.
- Do not silently treat missing usage fields as zero tokens or zero cost.
- Do not run full-time Claude/DeepSeek shadow mode by default.
- Do not let a cheaper provider override JSON validity, Gatekeeper, calibration, EV, or safety failures.
- Do not store raw prompts, raw reasoning text, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, or high-cardinality market identifiers in reports, tests, logs, or metrics labels.
- Do not write reports outside approved docs paths.
- Do not auto-apply calibration recommendations to runtime config.
- Do not fabricate historical outcomes or market metadata when datasets lack them.
- Do not introduce real network calls into unit tests; stub provider clients and datasets.

## Dependencies

- Phase 15 PRD (`docs/PRD-v15.0.md`).
- WI-52 LLM cost guard and cognitive circuit breaker.
- WI-53 market eligibility, evaluation deduplication, and queue backpressure.
- WI-54 configurable DeepSeek provider via Anthropic-compatible endpoint.
- Existing `BacktestDataLoader`, `BacktestRunner`, and real-data validation report logic.
- Existing historical dataset pipeline and lookahead-safe outcome data.
- Existing `ClaudeClient` provider selection, reflection audit, JSON extraction, and Gatekeeper validation.
- Existing `LLMEvaluationResponse` schema.
- Existing provider usage accounting and Decimal-safe cost guard semantics.
- Existing structlog logging conventions and Prometheus-safe metrics infrastructure.
- Existing async test stack and stubbed-provider patterns.

## Target Layer

Backtesting and validation layer with Layer 3 provider-evaluation inputs. This WI adds provider comparison, readiness verdicts, calibration recommendations, reporting, and runbook guidance. It does not change Layer 1 ingestion, Layer 2 prompt construction, `LLMEvaluationResponse` Gatekeeper authority, execution routing, repository boundaries, Alembic schema management, live-trading authorization, signing, or broadcasting.
