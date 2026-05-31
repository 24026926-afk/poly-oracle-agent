# Implementation Prompt - WI-64 Discovery Volume Floor

## Session Context

You are working in `poly-oracle-agent` after WI-63 (REST order-book best-of-book selection) restored true spreads on liquid markets. WI-64 is a standalone discovery-efficiency Work Item: it adds a configurable 24-hour volume floor so genuinely illiquid markets are pruned before they consume preflight order-book fetches or LLM evaluation budget.

Current baseline:

- WI-63 fixed `PolymarketClient._parse_order_book` to select `max(positive bids)` / `min(positive asks)`. Liquid markets now present narrow spreads to the LLM and the WI-53 preflight instead of a fabricated ~99.8% spread.
- WI-53 introduced read-only discovery preflight in `src/agents/ingestion/market_discovery.py`: a cheap pre-preflight loop (metadata, time-to-resolution, exposure) followed by a bounded-concurrency order-book preflight (`_run_preflight`) that checks spread/crossed/non-positive quotes.
- `MarketDiscovery.discover_markets` already iterates candidates through the cheap pre-preflight loop, accumulating a `stats` dict and emitting market-rejection operational events via `_publish_market_rejection_once`.
- `MarketMetadata.volume_24h` (`Optional[float]`, alias `volume24hr`) is already populated from the Gamma API during discovery — no extra API call is needed to read it.
- `DRY_RUN=false` remains out of scope. Live signing, live broadcasting, and execution paths that bypass `LLMEvaluationResponse` remain forbidden.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-64-discovery-volume-floor.md`
- `src/agents/ingestion/market_discovery.py` (target file; cheap pre-preflight loop in `discover_markets`)
- `src/schemas/market.py` (`MarketMetadata.volume_24h`)
- `src/core/config.py` (`AppConfig`, Grok/preflight field block, Decimal-coercion validator list)
- `src/schemas/ops.py` (`OperationalEventReasonCode` — reuse `MARKET_INELIGIBLE`)
- `docs/deliverables/business_logic/business_logic_WI-53-*.md` (preflight context)

## Objective

Add `AppConfig.min_market_volume_24h_usdc: Decimal` (default `Decimal("0")` = disabled) and apply it as a cheap, metadata-only volume floor inside the existing pre-preflight loop in `MarketDiscovery.discover_markets`. When active, markets whose `volume_24h` is below the threshold (or missing) are excluded before any order-book fetch or LLM evaluation, counted in a new `volume_fail` stat, and reported through the existing rejection-event path. The filter must be a no-op at the default threshold.

## Inputs

- `MarketMetadata.volume_24h` — `Optional[float]` 24h volume from Gamma, already loaded at discovery.
- `AppConfig.min_market_volume_24h_usdc` — new `Decimal` threshold field (default `Decimal("0")`).
- Existing `stats` dict, `_publish_market_rejection_once`, and structured discovery logs in `market_discovery.py`.
- `OperationalEventReasonCode.MARKET_INELIGIBLE` for the rejection event.
- No new Python package dependencies. Standard library, `pydantic`, `structlog`, `Decimal` only.

## Outputs

- `src/core/config.py` — new `min_market_volume_24h_usdc: Decimal` field (default `Decimal("0")`), added to the existing Decimal-coercion field validator list, with a clear description.
- `src/agents/ingestion/market_discovery.py` — volume-floor check in the cheap pre-preflight loop in `discover_markets`:
  - Skips when `min_market_volume_24h_usdc <= 0` (disabled; zero behavior change).
  - When active: excludes markets with `volume_24h` missing or `< threshold` (compared via `Decimal(str(volume_24h))`), increments `stats["volume_fail"]`, emits a market-rejection event with a volume-specific message, and `continue`s.
  - Runs before `pre_preflight.append(market)`.
  - Adds `volume_fail` to the `stats` dict and the existing discovery logs.
- `.env.example` — document `MIN_MARKET_VOLUME_24H_USDC` (default `0`, commented) with a one-line explanation.
- `tests/unit/test_WI-64-discovery-volume-floor.py` — unit tests (RED first, then GREEN).
- `STATE.md` — WI-64 completion entry on `/wi-done`.

## Acceptance Criteria

1. With `min_market_volume_24h_usdc == Decimal("0")` (default), no market is excluded for volume and `stats["volume_fail"] == 0` — discovery behavior is unchanged.
2. With a positive threshold, a market whose `volume_24h` is strictly below it is excluded and `stats["volume_fail"]` is incremented.
3. With a positive threshold, a market whose `volume_24h` equals the threshold passes the volume gate (inclusive `>=`).
4. With a positive threshold, a market whose `volume_24h` exceeds the threshold passes.
5. With a positive threshold, a market whose `volume_24h is None` is excluded (unknown liquidity, fail-closed) and counted in `volume_fail`.
6. With the filter disabled, a market whose `volume_24h is None` is not excluded for volume.
7. The volume comparison uses `Decimal`; the only conversion is `Decimal(str(volume_24h))`. No `float` arithmetic in the decision path.
8. Excluded markets are filtered before any order-book preflight fetch (no `fetch_order_book` call) and before any LLM evaluation.
9. A negative threshold is treated as disabled (no exclusions).
10. When all candidates are excluded by the volume floor, `discover_markets` returns an empty list and logs `market_discovery.no_eligible_markets` including `volume_fail` — no crash.
11. Full regression passes with coverage >= 80%; no existing test regresses.

## Anti-Patterns

- Do not perform `float` arithmetic. Convert `volume_24h` once via `Decimal(str(...))` and compare with `Decimal`.
- Do not make the filter active by default. Default `Decimal("0")` must be a complete no-op.
- Do not add a network call, order-book fetch, or DB access to the volume filter.
- Do not place the volume check after `pre_preflight.append` or inside `_run_preflight`; it belongs in the cheap pre-preflight loop, before the append.
- Do not modify the WI-53 order-book preflight (spread, crossed, non-positive, timeout) logic.
- Do not add a new `MarketEligibilitySkipReason` value, a new `OperationalEventType`, or a new persisted schema/migration. Reuse `MARKET_INELIGIBLE`.
- Do not treat missing `volume_24h` as sufficient liquidity when the filter is active.
- Do not modify `MarketMetadata`.
- Do not add `print()`. Use `structlog`. Do not add secret/high-cardinality log fields.
- Do not add execution, signing, broadcasting, wallet mutation, or Gatekeeper paths.
- Do not weaken `DRY_RUN` or introduce `DRY_RUN=false` behavior.
- Do not add raw DB sessions, raw SQL, Alembic migrations, or `Base.metadata.create_all()`.
- Do not add new Python package dependencies.

## Dependencies

- WI-53 — Market Eligibility Preflight (provides the discovery cheap-filter loop and rejection-event path this WI extends).
- WI-63 — REST order-book best-of-book selection (restores true spreads; WI-64 prunes by volume on top).
- `src/agents/ingestion/market_discovery.py` — target file.
- `src/schemas/market.py` — `MarketMetadata.volume_24h`.
- `src/core/config.py` — `AppConfig` and its Decimal-coercion field validator.
- `src/schemas/ops.py` — `OperationalEventReasonCode.MARKET_INELIGIBLE`.

## Target Layer

Market discovery / ingestion (Layer 1). The change is confined to `AppConfig` (one field), `MarketDiscovery.discover_markets` (the cheap pre-preflight loop), and `.env.example`. It is a read-only metadata filter that prunes the candidate set before the order-book preflight and the cognitive pipeline; it does not participate in signing, broadcasting, execution routing, or Gatekeeper logic.
