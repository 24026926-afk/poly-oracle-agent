# Business Logic - WI-64 Discovery Volume Floor

## Objective

Add a configurable 24-hour volume floor to market discovery so genuinely illiquid markets are pruned **before** they consume a preflight order-book fetch or any LLM evaluation budget.

WI-63 corrected the REST order-book parser, restoring true (narrow) spreads on liquid markets. But even with correct spreads, the discovery surface still includes low-volume markets that are not worth spending LLM budget on. WI-64 adds a cheap, metadata-only filter using `MarketMetadata.volume_24h` (Gamma `volume24hr`, already fetched during discovery — no extra API call), applied inside the existing cheap pre-preflight loop in `MarketDiscovery` alongside the metadata, time-to-resolution, and exposure checks.

The filter is **opt-in and default-disabled**: with the threshold at its `Decimal("0")` default, behavior is identical to today. It must not change execution, signing, broadcasting, `dry_run`, Gatekeeper, the WI-53 order-book preflight logic, or any schema persisted to the database.

## Data Models

Pydantic schema names only:

- `AppConfig` (existing, `src/core/config.py`) — add one field: `min_market_volume_24h_usdc: Decimal` (default `Decimal("0")`, meaning disabled). Registered in the existing Decimal-coercion field validator list.
- `MarketMetadata` (existing, `src/schemas/market.py`) — consumed read-only via `volume_24h`; not modified.
- `MarketEligibilitySkipReason` (existing, `src/schemas/market_eligibility.py`) — not modified; the volume filter is a cheap pre-preflight filter, not a preflight order-book check, so it reports via the operational-event rejection path, not a preflight skip reason.

No new schemas, no new enum values, no migration, no new config beyond the single threshold field.

## Key Rules

1. The volume floor is read from `AppConfig.min_market_volume_24h_usdc`, a `Decimal`. Default `Decimal("0")`.
2. When `min_market_volume_24h_usdc <= 0`, the filter is **disabled**: no market is ever excluded for volume, and discovery behavior is byte-for-byte unchanged from pre-WI-64.
3. When `min_market_volume_24h_usdc > 0`, the filter is active and evaluated inside the existing cheap pre-preflight loop in `MarketDiscovery.discover_markets` (the same loop that runs metadata, TTR, and exposure checks), **before** a market is appended to `pre_preflight` and therefore before any order-book preflight fetch or LLM evaluation.
4. The filter compares `Decimal(str(market.volume_24h))` against the threshold. `MarketMetadata.volume_24h` is a `float` from the Gamma boundary; it is converted to `Decimal` via `str()` exactly once, at the comparison site. No `float` arithmetic occurs in the decision.
5. A market with `volume_24h` present and `>= threshold` passes the volume gate.
6. A market with `volume_24h` present and `< threshold` is excluded: increment a `volume_fail` stat counter and emit a market-rejection operational event (reusing `OperationalEventReasonCode.MARKET_INELIGIBLE` with a clear volume-specific message), then `continue` to the next candidate.
7. A market with `volume_24h is None` (Gamma omitted the field) is treated as **unknown liquidity** and excluded when the filter is active (fail-closed: do not spend LLM budget on a market whose liquidity cannot be confirmed). It is counted in `volume_fail`.
8. The volume check is ordered after `_has_required_metadata` and TTR, and may be placed before or after the exposure check; it must run before `pre_preflight.append(market)`.
9. The `stats` dict gains a `volume_fail` key initialized to `0`; it is included in the existing `market_discovery.no_eligible_markets` / `market_discovery.eligible_markets_found` structured logs.
10. The filter performs no network I/O, no DB access, and no order-book fetch. It uses only already-loaded `MarketMetadata`.
11. Rejection events continue to use the existing `_publish_market_rejection_once` path and its idempotency semantics; no new event type is introduced.
12. Logging is `structlog` only. No `print()`. No secret or high-cardinality fields added to logs (condition_id is already used in the existing rejection path and is acceptable as today).
13. No change to `_run_preflight`, `_get_order_book_quotes`, or the WI-53 spread/crossed/non-positive checks.
14. No new Python package dependencies.

## Edge Cases

1. `min_market_volume_24h_usdc == Decimal("0")` (default): filter disabled; no market excluded for volume; `volume_fail` remains 0.
2. `min_market_volume_24h_usdc` negative (misconfiguration): treated as disabled (`<= 0` guard); never excludes markets.
3. `volume_24h` exactly equal to the threshold: market passes (inclusive `>=`).
4. `volume_24h` just below the threshold: market excluded.
5. `volume_24h is None` with filter active: excluded as unknown liquidity, counted in `volume_fail`.
6. `volume_24h is None` with filter disabled: not excluded (default behavior preserved).
7. `volume_24h == 0.0` with filter active (`threshold > 0`): excluded (0 < threshold).
8. Very large `volume_24h` (e.g. high-volume market): converted to `Decimal` without precision loss via `str()`; passes.
9. All candidates excluded by the volume floor: `discover_markets` returns an empty eligible list and logs `market_discovery.no_eligible_markets` with the `volume_fail` count — no crash, no fabricated markets.
10. Filter active but preflight disabled (`enable_market_discovery_preflight=False`): the volume floor still applies in the cheap loop; eligible markets bypass only the order-book preflight, not the volume gate.
11. `volume_24h` is a non-finite float (NaN/inf) from a malformed Gamma payload: `Decimal(str(...))` of `"nan"`/`"inf"` — comparison treats it as not `>= threshold` and the market is excluded (fail-closed). No exception escapes the loop.

## Invariants

1. With the threshold at its `Decimal("0")` default, discovery behavior is identical to pre-WI-64 (the filter is a no-op).
2. The volume comparison uses `Decimal`; the only `float`→`Decimal` conversion is `Decimal(str(volume_24h))` at the comparison site. No `float` arithmetic in the decision path.
3. The filter is read-only: no network I/O, no order-book fetch, no DB session, no raw SQL.
4. The filter runs before any preflight order-book fetch and before any LLM evaluation, so excluded markets consume no LLM budget.
5. No execution, signing, broadcasting, `LLMEvaluationResponse`, or Gatekeeper path is touched.
6. No `dry_run` weakening; no `DRY_RUN=false` behavior introduced.
7. No Alembic migration, no `Base.metadata.create_all()`, no persisted-schema change. `MarketMetadata` and `MarketEligibilitySkipReason` are unchanged.
8. The WI-53 order-book preflight (spread, crossed book, non-positive quote, timeout) is unchanged.
9. Missing volume data is never treated as sufficient liquidity; unknown liquidity fails closed when the filter is active.
10. Tests cover: disabled-by-default no-op, threshold boundary (equal/below/above), None handling (active and disabled), Decimal comparison integrity, `volume_fail` stat accounting, and all-excluded empty result.
