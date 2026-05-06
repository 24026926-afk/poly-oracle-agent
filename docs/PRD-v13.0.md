# PRD-v13.0 — Phase 13: Real-Data Validation & 24/7 Readiness

**Version:** 13.0  
**Status:** READY FOR IMPLEMENTATION  
**Phase:** 13  
**Author:** Staff Architect / Quantitative Systems Engineer  
**Date:** 2026-05-05  
**Baseline:** Phase 12 sealed — 678 tests, 94% coverage, local Streamlit Command Center dashboard

---

## 1. Objective

Validate the trading decision pipeline against real historical Polymarket data before any live trust is placed in the strategy, then replace mock sentiment and add the minimum operational health/metrics surfaces required for 24/7 dry-run operation.

Phase 13 is a go/no-go phase. If real-data backtesting shows negative PnL, unacceptable drawdown, weak calibration, or no defensible edge, live execution remains disabled and the next phase must redesign model prompts, data inputs, or risk gates rather than hardening live trading.

---

## 2. Scope Boundaries

**In scope:**
- Historical Polymarket dataset builder for resolved markets and point-in-time market snapshots.
- Real-data backtest run using `BacktestRunner` with strict `dry_run=True`.
- Typed validation report with explicit live-readiness verdict.
- Replacement of mock-first Grok sentiment with a real xAI/Grok API path while preserving neutral fallback behavior.
- WebSocket reconnect hardening with bounded exponential backoff and structured health state.
- Local HTTP health endpoint for liveness/readiness inspection.
- Prometheus-compatible `/metrics` endpoint with operator-critical counters and gauges.

**Out of scope:**
- Live trading enablement or any recommendation to set `DRY_RUN=false`.
- Strategy optimization, prompt tuning, parameter search, or walk-forward optimization.
- Paid data-vendor integration unless explicitly approved in a later WI.
- Cloud deployment, Kubernetes, managed Prometheus, Grafana dashboards, or alert routing.
- Replacing SQLite with PostgreSQL or any remote database.
- Storing raw API secrets in files, logs, fixtures, or committed config.
- Generating WI business-logic or implementation-prompt deliverables during PRD creation. Those are generated one at a time via `/wi-start`.

---

## 3. Work Items

### WI-43 — Historical Polymarket Dataset Pipeline

**Goal:** Build a deterministic historical-data pipeline that downloads, validates, and normalizes resolved Polymarket market data into the JSON format consumed by `BacktestDataLoader`, without lookahead leakage.

#### 3.1 File Structure

```
src/
└── backtesting/
    ├── __init__.py
    ├── historical_dataset.py
    └── polymarket_history_client.py

scripts/
└── build_historical_dataset.py

tests/
├── unit/
│   └── test_WI-43-historical-dataset-pipeline.py
└── integration/
    └── test_WI-43-historical-dataset-pipeline.py

data/
└── historical/
    └── .gitkeep
```

#### 3.2 Core Requirements

- Use `httpx.AsyncClient` for Gamma API / public source reads with explicit timeout and bounded retries.
- Discover resolved markets only; active/open markets are excluded from validation datasets.
- Persist normalized JSON snapshots as `{token_id}_{YYYY-MM-DD}.json`, compatible with current `BacktestDataLoader`.
- Each snapshot must include at minimum:
  - `token_id`
  - `condition_id`
  - `timestamp_utc`
  - `best_bid`
  - `best_ask`
  - `midpoint`
  - `spread`
  - `volume_24h` when available
  - `market_end_date`
  - `resolved_outcome`
  - `realized_pnl_usdc` or enough outcome fields for WI-44 to compute it
- All prices, spreads, PnL, and sizing fields must be parsed as `Decimal`; raw `float` input is rejected at schema boundary.
- The pipeline must explicitly separate point-in-time observable fields from resolution/outcome fields to prevent lookahead leakage.
- Malformed, crossed, missing, non-positive, or unresolved snapshots are skipped with structured `structlog` events and counted in a summary.
- Add a CLI:

```bash
python scripts/build_historical_dataset.py \
  --start-date YYYY-MM-DD \
  --end-date YYYY-MM-DD \
  --output-dir data/historical
```

- Output a manifest file containing market counts, snapshot counts, skipped counts, date range, source URLs, and generation timestamp.

#### 3.3 Definition of Done — WI-43

- [ ] Historical dataset builder writes BacktestDataLoader-compatible JSON files.
- [ ] Resolved market outcomes are present but cannot be injected into prompt/evaluation context before simulated market close.
- [ ] Crossed books, missing token IDs, invalid timestamps, and non-Decimal financial values are rejected or skipped with typed reasons.
- [ ] CLI exits non-zero on source failure after bounded retries.
- [ ] Unit tests cover schema validation, Decimal parsing, lookahead separation, skipped malformed rows, and manifest generation.
- [ ] Integration test builds a small fixture dataset and confirms `BacktestDataLoader.load_all()` can read it.

---

### WI-44 — Real-Data Backtest Validation

**Goal:** Run `BacktestRunner` against the WI-43 historical dataset and produce a typed baseline report answering whether the current decision pipeline shows historical edge.

#### 4.1 File Structure

```
src/
└── backtesting/
    ├── validation_report.py
    └── live_readiness.py

scripts/
└── run_real_data_backtest.py

tests/
├── unit/
│   └── test_WI-44-real-data-backtest-validation.py
└── integration/
    └── test_WI-44-real-data-backtest-validation.py
```

#### 4.2 Core Requirements

- Invoke the existing `BacktestRunner` with `BacktestConfig.dry_run=True`; live signing, broadcasting, and DB writes remain impossible.
- Produce a machine-readable JSON report and a markdown summary under `docs/backtests/`.
- Extend report coverage beyond WI-33 baseline with:
  - total snapshots replayed
  - total decisions
  - BUY / HOLD / SKIP distribution
  - total trades
  - win rate
  - net PnL USDC
  - max drawdown USDC
  - Sharpe ratio
  - average EV
  - realized EV calibration
  - confidence calibration by bucket
  - per-market stats
  - explicit live-readiness verdict
- Add a typed verdict schema with at least:
  - `PASS`
  - `FAIL_NEGATIVE_PNL`
  - `FAIL_DRAWDOWN`
  - `FAIL_INSUFFICIENT_TRADES`
  - `FAIL_WEAK_CALIBRATION`
  - `FAIL_DATA_QUALITY`
- Use conservative defaults: if trade count, data quality, or calibration is insufficient, verdict is not live-ready.
- No prompt, threshold, Kelly, or risk-parameter optimization is allowed in WI-44.

#### 4.3 Definition of Done — WI-44

- [ ] `python scripts/run_real_data_backtest.py --data-dir data/historical --output docs/backtests/phase13_baseline.json` runs end to end.
- [ ] Report includes PnL, drawdown, action distribution, calibration metrics, and typed verdict.
- [ ] Negative or statistically weak results produce an explicit non-live-ready verdict.
- [ ] Backtest path performs zero DB writes and never constructs live signer/broadcaster paths.
- [ ] All report financial metrics use `Decimal` internally.
- [ ] Tests cover PASS and each failure verdict path.

---

### WI-45 — Real Grok Sentiment Integration

**Goal:** Replace deterministic mock sentiment for eligible categories with a real xAI/Grok API path, while preserving neutral fallback semantics and the terminal authority of `LLMEvaluationResponse`.

#### 5.1 File Structure

```
src/
└── agents/
    └── evaluation/
        └── grok_client.py

src/
└── core/
    └── config.py

tests/
├── unit/
│   └── test_WI-45-real-grok-sentiment.py
└── integration/
    └── test_WI-45-real-grok-sentiment.py
```

#### 5.2 Core Requirements

- Keep `GrokClient` as the canonical class name and integration point.
- `grok_mocked=True` remains the safe default for local tests unless an explicit integration environment enables live calls.
- When `grok_mocked=False`, `GrokClient` calls the xAI/Grok chat completions API via `httpx.AsyncClient`.
- API key is read only from `AppConfig.grok_api_key`; it is never logged, persisted, or added to fixtures.
- Add explicit config for live timeout, max retries, and failover behavior if missing:
  - `grok_timeout_seconds`
  - `grok_max_retries`
  - `grok_live_enabled` or equivalent explicit gate
- All response parsing must validate into `SentimentResponse`; `sentiment_score` remains `Decimal`.
- Any timeout, HTTP error, 429, malformed JSON, schema error, missing key, or safety refusal returns `NEUTRAL_SENTIMENT`.
- Real sentiment is used only for configured categories already eligible for sentiment (`CRYPTO` and `POLITICS` unless business logic later expands this).
- Sentiment remains an upstream signal. It cannot bypass `LLMEvaluationResponse`, confidence thresholds, EV thresholds, or execution safety gates.

#### 5.3 Definition of Done — WI-45

- [ ] Mock mode still returns deterministic `_MOCK_SENTIMENT` for existing tests.
- [ ] Live mode posts to the configured xAI/Grok endpoint with bounded timeout and retry behavior.
- [ ] API failures return `NEUTRAL_SENTIMENT` and log structured reason without raising into the pipeline.
- [ ] No secret value appears in logs, test fixtures, or committed files.
- [ ] `PromptFactory` receives validated `SentimentResponse` only.
- [ ] Integration tests use mocked HTTP responses; no real network call is required in CI.

---

### WI-46 — 24/7 Connectivity Hardening

**Goal:** Harden the WebSocket and runtime liveness behavior so long dry-run sessions can recover from disconnects and expose health state to the operator.

#### 6.1 File Structure

```
src/
├── agents/
│   └── ingestion/
│       └── ws_client.py
├── observability/
│   ├── __init__.py
│   ├── health.py
│   └── health_server.py
└── orchestrator.py

tests/
├── unit/
│   └── test_WI-46-connectivity-hardening.py
└── integration/
    └── test_WI-46-connectivity-hardening.py
```

#### 6.2 Core Requirements

- Preserve `CLOBWebSocketClient` as the canonical WebSocket class.
- Make reconnect behavior observable and configurable:
  - initial backoff
  - max backoff
  - jitter
  - max consecutive failures before degraded health
- Track health fields:
  - current connection state
  - last successful connection timestamp
  - last heartbeat sent timestamp
  - last PONG received timestamp
  - reconnect count
  - consecutive failure count
  - last error reason
  - active subscribed asset count
- Handle market closed / inactive / expired conditions explicitly from metadata or frames; closed markets should trigger typed skip/rotation behavior, not noisy reconnect loops.
- Add local HTTP health endpoints using the standard library or existing approved stack only:
  - `GET /healthz` returns liveness.
  - `GET /readyz` returns readiness/degraded status based on DB, WebSocket, and configured market subscription state.
- Health server must use explicit startup/shutdown lifecycle in `Orchestrator`.
- No health endpoint may expose secrets, private keys, wallet address, raw prompt text, or full decision reasoning.

#### 6.3 Definition of Done — WI-46

- [ ] WebSocket reconnect path has bounded exponential backoff and jitter.
- [ ] Consecutive failure state is visible through a typed health snapshot.
- [ ] Market closed/inactive cases are handled explicitly without infinite error churn.
- [ ] `/healthz` and `/readyz` return deterministic HTTP statuses and minimal JSON bodies.
- [ ] Orchestrator starts and stops the health server cleanly.
- [ ] Tests simulate disconnect, reconnect, heartbeat loss, market closed state, and graceful shutdown.

---

### WI-47 — Prometheus Metrics Export

**Goal:** Expose a Prometheus-compatible `/metrics` endpoint with the minimum counters and gauges needed to operate dry-run 24/7 without blindness.

#### 7.1 File Structure

```
src/
└── observability/
    ├── metrics.py
    └── metrics_server.py

tests/
├── unit/
│   └── test_WI-47-prometheus-metrics.py
└── integration/
    └── test_WI-47-prometheus-metrics.py
```

#### 7.2 Core Requirements

- Export Prometheus text exposition format at `GET /metrics`.
- Do not add a heavyweight web framework. Prefer a small `asyncio` standard-library HTTP responder unless a later implementation prompt proves a dependency is necessary.
- Required metrics:
  - decisions per hour
  - `BUY` / `HOLD` / `SKIP` decision counts
  - execution result counts by `ExecutionAction`
  - evaluation latency
  - context-build latency
  - execution-routing latency
  - WebSocket reconnect count
  - WebSocket error count
  - last heartbeat age seconds
  - active subscribed market count
  - latest backtest live-readiness verdict as a labeled gauge
- Metric labels must be low-cardinality. Do not label by raw `condition_id`, prompt text, reasoning text, wallet address, token ID, or exception message.
- Metrics collection must not mutate trading state and must not block the evaluation or execution queues.
- Metrics endpoint and health endpoint may share the same lightweight server if the implementation keeps route ownership clear.

#### 7.3 Definition of Done — WI-47

- [ ] `GET /metrics` returns valid Prometheus text exposition.
- [ ] Required counters/gauges are emitted with low-cardinality labels.
- [ ] No secrets, wallet details, prompt text, reasoning text, or high-cardinality market IDs appear in metrics.
- [ ] Metrics updates are non-blocking and safe under concurrent queue activity.
- [ ] Tests validate format, labels, counter increments, heartbeat age, and no forbidden sensitive fields.

---

## 4. Phase 13 Definition of Done

Phase 13 is complete when all WI DoDs pass and the following global gates are satisfied:

1. **Dataset gate:** Historical data builder produces a manifest and BacktestDataLoader-compatible files for resolved markets, with no lookahead leakage into model context.
2. **Validation gate:** Real-data backtest produces a typed live-readiness verdict and report under `docs/backtests/`.
3. **Kill criterion gate:** If WI-44 verdict is not `PASS`, `DRY_RUN=false` remains prohibited and Phase 14 must address strategy/model/risk redesign before operational hardening is considered sufficient for live trading.
4. **Sentiment gate:** Live Grok mode is available behind explicit config, mock mode remains default for tests, and all Grok failures fall back to neutral sentiment.
5. **Connectivity gate:** WebSocket reconnect, heartbeat, and market-closed states are observable through typed health snapshots and local health endpoints.
6. **Metrics gate:** `/metrics` exposes the required low-cardinality Prometheus metrics without secrets or high-cardinality identifiers.
7. **Trading integrity gate:** No money, pricing, EV, Kelly, PnL, or sizing calculation uses raw `float`.
8. **Gatekeeper gate:** No execution or backtest path bypasses `LLMEvaluationResponse`.
9. **Safety gate:** No signing, broadcasting, or state-mutating live execution call can occur in dry run.
10. **Regression gate:** Full test suite passes with coverage ≥ 80% and no regression from the 678-test / 94% baseline without explicit approval.
11. **MAAP gate:** Any core-logic changes under `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, or `src/backtest_runner.py` are MAAP-reviewed before commit.

---

## 5. Constraints & Non-Negotiables

1. All financial, pricing, EV, sizing, calibration, and PnL arithmetic must use `Decimal`.
2. `BacktestRunner` always runs with `dry_run=True`; backtests never sign, broadcast, or mutate live execution state.
3. `LLMEvaluationResponse` remains the terminal Gatekeeper schema before execution in live and backtest paths.
4. `PromptFactory` must assemble real market context only; never invent balances, positions, fees, market metadata, sentiment, or resolved outcomes.
5. Historical resolved outcomes must not enter prompts before the simulated market resolution timestamp.
6. WebSocket, HTTP, RPC, and LLM paths must use explicit timeout or bounded retry behavior.
7. Runtime DB access remains repository-based. Agent code must not introduce direct DB sessions or raw SQL.
8. Secrets must never be logged, committed, persisted in reports, or exposed via health/metrics endpoints.
9. Health and metrics endpoints are read-only observability surfaces.
10. No direct commits to `main`; all work remains on `develop` and feature branches.

---

## 6. Dependencies to Add

No new third-party dependencies are required at PRD time.

Implementation should first attempt WI-46 and WI-47 using Python standard-library `asyncio` networking plus the existing approved stack. If a future WI implementation prompt proves `prometheus-client` or a lightweight ASGI dependency is necessary, that dependency must be justified in that WI and added to both `pyproject.toml` and `requirements.txt`.

---

## 7. Deliverables Summary

| WI | Deliverable |
|---|---|
| WI-43 | Historical dataset client, builder CLI, manifest output, fixture dataset tests |
| WI-44 | Real-data backtest runner script, typed validation report, live-readiness verdict |
| WI-45 | Real xAI/Grok sentiment path in `GrokClient`, config gates, fallback tests |
| WI-46 | WebSocket health state, hardened reconnect behavior, `/healthz` and `/readyz` |
| WI-47 | Prometheus-compatible `/metrics` endpoint and low-cardinality metric registry |

PRD generation creates only this file and updates `STATE.md`. Business-logic and implementation-prompt deliverables are intentionally deferred until `/wi-start WI-XX`.

---

## 8. State & Documentation Updates on Phase Completion

On Phase 13 completion:

1. `STATE.md` version bumped to `0.13.0`, status updated to `Phase 13 — COMPLETE`.
2. `README.md` updated with:
   - historical dataset CLI usage
   - real-data backtest usage
   - Grok live-mode configuration
   - health and metrics endpoint documentation
3. `docs/system_architecture.md` updated to reflect the post-Phase-13 architecture, replacing stale Phase 4 planning context.
4. `docs/archive/ARCHIVE_PHASE_13.md` generated with final WI outcomes, test counts, coverage, validation verdict, and operational caveats.
5. `docs/backtests/phase13_baseline.md` and `docs/backtests/phase13_baseline.json` retained as audit artifacts.
