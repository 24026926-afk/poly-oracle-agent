# DeepSeek Paper-Trading Readiness Runbook

> **WI-55** — This runbook explains how to run the provider comparison,
> interpret the typed readiness verdict, and enable DeepSeek V4 Pro as the
> primary LLM evaluation provider in `DRY_RUN=true` paper trading **only
> after** a passing readiness verdict.

---

## 1. Prerequisites

- Python 3.12+ virtual environment with `pip install -e .` complete.
- `.env` configured with valid DeepSeek credentials:
  - `LLM_PROVIDER=deepseek`
  - `DEEPSEEK_API_KEY=<your-key>`
  - `DEEPSEEK_BASE_URL=https://api.deepseek.com/anthropic`
  - `DEEPSEEK_MODEL=deepseek-chat`
- Historical market dataset built via `scripts/build_historical_dataset.py`
  in `data/historical/`.
- `DRY_RUN=true` set in `.env`.

---

## 2. Running the Provider Comparison

```bash
python scripts/run_llm_provider_comparison.py \
  --data-dir data/historical \
  --output docs/backtests/phase15-deepseek-calibration.json
```

With optional config overrides:

```bash
python scripts/run_llm_provider_comparison.py \
  --data-dir data/historical \
  --config configs/deepseek_comparison.json \
  --output docs/backtests/phase15-deepseek-calibration.json
```

The comparison runs entirely in `DRY_RUN=true` — no orders are signed,
broadcast, or authorized.

---

## 3. Interpreting the Report

The output JSON report at `docs/backtests/phase15-deepseek-calibration.json`
contains:

| Section | What It Contains |
|---|---|
| `run.config` | Comparison configuration snapshot |
| `run.results[*].decision_metrics` | JSON validity rate, Gatekeeper pass/fail, BUY/HOLD/SKIP distribution |
| `run.results[*].calibration_metrics` | Confidence calibration per bucket, EV calibration, outcome coverage |
| `run.results[*].cost_metrics` | Token usage, estimated cost, budget/cooldown block counts |
| `run.results[*].latency_metrics` | min/max/mean/median/P95/P99 latency in ms |
| `run.results[*].readiness_verdict` | Typed deterministic readiness verdict |
| `run.results[*].calibration_recommendation` | Advisory configuration suggestions (if any) |

---

## 4. Readiness Verdicts

The typed verdict is deterministic.  Checks are applied in priority order
(from most severe to least):

| Verdict | Meaning |
|---|---|
| `PROVIDER_REJECTED_FOR_JSON_VALIDITY` | Invalid JSON rate exceeds configured tolerance — unsafe |
| `PROVIDER_REJECTED_FOR_NEGATIVE_EV` | Realized outcome calibration shows negative EV where outcomes exist |
| `PROVIDER_REJECTED_FOR_COST_OR_LATENCY` | Cost not sufficiently cheaper or latency too high vs Anthropic |
| `PROVIDER_NEEDS_THRESHOLD_RECALIBRATION` | Validity is OK but confidence/EV calibration needs provider-specific adjustment |
| `PROVIDER_READY_FOR_SAMPLED_AUDIT_ONLY` | Promising but not yet safe for primary — use for sampled audit |
| `PROVIDER_READY_FOR_DRY_RUN_PRIMARY` | All gates pass — eligible for primary paper-trading provider |

**DeepSeek must receive `PROVIDER_READY_FOR_DRY_RUN_PRIMARY` before it can
be recommended as the primary paper-trading provider.**

---

## 5. Enabling DeepSeek as Primary (After Passing Readiness)

1. Verify the verdict is `PROVIDER_READY_FOR_DRY_RUN_PRIMARY`.
2. Review the calibration recommendations in the report.
3. Update `.env`:
   ```bash
   LLM_PROVIDER=deepseek
   ```
4. Restart the orchestrator.
5. Monitor the first hour of paper trading for unexpected behavior.

**Important:** This enables DeepSeek as primary only in `DRY_RUN=true`
paper trading.  Live trading (`DRY_RUN=false`) remains out of scope
regardless of the readiness verdict.

---

## 6. Safety Statements

- The comparison path never signs, broadcasts, or authorizes live orders.
- `LLMEvaluationResponse` remains the terminal Gatekeeper for all
  provider-produced decisions.
- The canonical `ClaudeClient` class name is never renamed or aliased.
- Claude audit sampling is disabled by default and must be explicitly
  enabled with a bounded sample fraction.
- Full-time Claude/DeepSeek shadow mode is prohibited by default.
- Calibration recommendations are advisory only — they do not auto-apply
  to the runtime configuration.
- Reports are secret-free: no raw prompts, reasoning text, API keys,
  wallet keys, token IDs, or condition IDs appear in report output.
- `DRY_RUN=false` is never authorized by this WI.
