# Business Logic — WI-43 Historical Polymarket Dataset Pipeline

## Objective

Build a deterministic historical-data pipeline that downloads, validates, and normalizes resolved Polymarket market data into `BacktestDataLoader`-compatible JSON without lookahead leakage.

## Data Models

Pydantic schema names only:

- `BacktestConfig`
- `BacktestReport`
- `BacktestDecision`
- `BacktestMarketStats`
- `MarketMetadata`
- `MarketSnapshotSchema`
- `HistoricalMarketRecord`
- `HistoricalSnapshotRecord`
- `HistoricalDatasetManifest`
- `HistoricalDatasetBuildResult`
- `HistoricalDataSkipReason`

## Key Rules

1. The dataset pipeline is an offline validation input path, not a runtime trading path.
2. The pipeline must include resolved markets only. Active, open, unresolved, cancelled, or ambiguous markets are excluded.
3. Historical JSON output must remain compatible with the existing `BacktestDataLoader` file contract: `{token_id}_{YYYY-MM-DD}.json`.
4. Every emitted snapshot must contain the point-in-time market fields required for replay: `token_id`, `timestamp_utc`, `best_bid`, `best_ask`, and `midpoint`.
5. WI-43 may add extra metadata fields needed by WI-44, including `condition_id`, `spread`, `volume_24h`, `market_end_date`, `resolved_outcome`, and realized-outcome inputs.
6. Resolution and outcome fields must be stored separately from point-in-time observable context so they cannot enter prompts before simulated market close.
7. All money, pricing, spread, EV, PnL, and sizing values must be parsed as `Decimal`; raw `float` values are invalid at the schema boundary.
8. HTTP access must use `httpx.AsyncClient` with explicit timeout and bounded retries.
9. Malformed rows must not silently disappear. Every skipped or rejected row must have a typed reason and structured `structlog` event.
10. The builder must write a manifest with market counts, snapshot counts, skipped counts, date range, source identifiers, and generation timestamp.
11. Generated production-scale datasets are local artifacts unless explicitly approved for version control. Tests must use small deterministic fixtures.
12. The pipeline must not write to the runtime database, construct live execution components, sign orders, broadcast orders, or mutate trading state.

## Edge Cases

1. Missing `token_id` or malformed token ID: skip with typed reason.
2. Missing `condition_id`: skip unless a reliable source mapping exists in the same record.
3. Invalid or timezone-naive timestamp: normalize to UTC when unambiguous; otherwise reject.
4. Non-positive `best_bid`, non-positive `best_ask`, or non-positive `midpoint`: skip.
5. Crossed book where `best_bid > best_ask`: skip.
6. Missing bid or ask with available last trade only: skip unless midpoint can be reconstructed from validated bid/ask source data.
7. JSON numeric values decoded as `float`: reject at schema boundary to preserve Decimal integrity.
8. Resolved market with missing or ambiguous outcome: skip from validation dataset.
9. Duplicate snapshots for the same token and timestamp: keep deterministic ordering and de-duplicate consistently.
10. Source HTTP timeout, 429, 5xx, malformed response, or exhausted retry: fail the CLI with a non-zero exit after logging source failure.
11. Empty source result for a requested date range: write no snapshot files and return a manifest that clearly reports zero eligible markets.
12. Partial dataset build: write a manifest that reports completed and skipped counts; do not claim success if source failures exhausted retries.

## Invariants

1. No raw `float` is permitted in money, price, EV, PnL, spread, or sizing paths.
2. Historical resolved outcome data must never leak into pre-resolution prompt or evaluation context.
3. `BacktestRunner` remains the only replay coordinator; WI-43 only prepares input data.
4. Backtesting and dataset generation remain `dry_run` validation workflows and never touch live execution.
5. `LLMEvaluationResponse` remains the terminal Gatekeeper in the later replay path; WI-43 must not duplicate or bypass decision logic.
6. Runtime database access rules remain unchanged; no direct DB session or raw SQL is introduced.
7. All external I/O has bounded timeout and retry behavior.
8. All skip and reject decisions are auditable through typed reasons and `structlog`.
9. Secret values, wallet details, prompt text, and private identifiers are never logged or written to dataset manifests.
10. Dataset generation must be deterministic for the same source inputs, date range, and configuration.
