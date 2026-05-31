# Business Logic - WI-63 REST Order Book Best-of-Book Selection

## Objective

Fix `PolymarketClient._parse_order_book` in `src/agents/execution/polymarket_client.py` so it selects **best-of-book** — the highest positive bid price and the lowest positive ask price — across the full `bids`/`asks` arrays, instead of naively reading index `[0]`.

The Polymarket CLOB REST order book returns price levels ordered worst → best. Reading `bids[0]` / `asks[0]` therefore selects the **worst** level on each side, fabricating a near-empty book (e.g. `bid≈0.001`, `ask≈0.999`) with a ~99.8% spread and a 0.5 midpoint on markets that are in fact liquid.

This is a financial-integrity defect in a core evaluation input path:
- The REST snapshot feeds the LLM evaluation context (`claude_client.py` overwrites `best_bid`/`best_ask`/`midpoint`/`spread` from this snapshot before prompt assembly).
- The same REST client gates market eligibility in the WI-53 discovery preflight (`market_discovery.py::_get_order_book_quotes`).

The identical defect was already corrected in the WebSocket path (`ws_client.py::_best_bid_from_levels` / `_best_ask_from_levels`, which use `max` / `min`). The 2026-05-30 60-hour production debrief proved the divergence empirically: the WS-fed `market_snapshots` table shows ~69% of 749k snapshots had < 10% spread (tradable), while the REST-fed decision log shows decisions clustered at implied-probability ≈ 0.5 with `reasoning_log` entries citing the literal fabricated arithmetic `0.999-0.001=0.998`. Result: 100% HOLD, zero trades.

This WI corrects the REST parser to mirror the proven WS selection logic. It must not change execution, signing, broadcasting, `dry_run`, Gatekeeper, or schema behavior.

## Data Models

Pydantic schema names only:

- `MarketSnapshot` (existing, in `src/agents/execution/polymarket_client.py` — corrected in place, not redefined; field set and validators unchanged)

No new schemas, enums, config fields, or migrations are introduced by this WI.

## Key Rules

1. `_parse_order_book` must compute `best_bid` as the **maximum** positive bid price across all entries in `bids`, not `bids[0]`.
2. `_parse_order_book` must compute `best_ask` as the **minimum** positive ask price across all entries in `asks`, not `asks[0]`.
3. Selection must be robust to array ordering (worst→best, best→worst, or unsorted). The result depends only on the set of price levels, never on their position.
4. Both `dict` entries (`entry["price"]`) and SDK dataclass entries (`entry.price`, e.g. `OrderSummary`) must be supported, matching the existing normalization.
5. Only **positive** price levels participate in selection. Levels with price `<= 0` are ignored for best-of-book selection (consistent with the WS helper, which filters non-positive levels).
6. If, after filtering, there is no positive bid or no positive ask, the method returns `None` (non-tradable), matching existing missing-side behavior.
7. All price arithmetic uses `Decimal`. Prices are converted via `Decimal(str(price))` exactly as today. No `float` in the money path.
8. Crossed-book rejection (`best_ask < best_bid`) must be applied **after** correct best-of-book selection and continue to return `None`.
9. `midpoint = (best_bid + best_ask) / Decimal("2")` and `spread = best_ask - best_bid`, unchanged, computed from the corrected best-of-book values.
10. The `MarketSnapshot` field set, validators, `source` value (`"clob_orderbook"`), and `fetched_at_utc` semantics remain unchanged.
11. Malformed price fields (`KeyError`, `TypeError`, `ArithmeticError`) must continue to produce a warning log and `None`, never a crash.
12. Logging remains `structlog` only. No `print()`. No new high-cardinality or secret fields added to logs.
13. No change to `fetch_order_book`'s public contract, timeout (`_FETCH_TIMEOUT`), or error→`None` semantics.
14. No change to `claude_client.py` or `market_discovery.py` behavior beyond the corrected values they already consume — those call sites are not modified by this WI.
15. No new Python package dependencies. Standard library, `pydantic`, `structlog`, `Decimal` only.

## Edge Cases

1. `bids` ordered worst→best (`[0.001, ..., 0.61]`): `best_bid` must be `0.61`, not `0.001`.
2. `asks` ordered worst→best (`[0.999, ..., 0.63]`): `best_ask` must be `0.63`, not `0.999`.
3. Single-level book: `best_bid`/`best_ask` equal that single level (selection is identity).
4. Empty `bids` or empty `asks`: return `None` (existing behavior, preserved).
5. All bid levels non-positive (`<= 0`): no positive bid → return `None`.
6. All ask levels non-positive: no positive ask → return `None`.
7. Mixed positive and non-positive levels on a side: non-positive levels are ignored; selection uses positives only.
8. Mixed `dict` and dataclass entries within the same array: each entry's price is extracted by its own type.
9. Crossed book after correct selection (`best_bid > best_ask`, e.g. real crossed quotes): return `None`.
10. Tight book (`bid=0.985`, `ask=0.986`): yields ~0.1% spread, correctly tradable — no longer mis-flagged.
11. Genuinely wide book (only far-apart levels actually present): a real wide spread is reported truthfully and downstream gates may legitimately reject it — the fix does not mask real illiquidity.
12. Duplicate price levels on a side: `max`/`min` are idempotent; result unchanged.
13. Malformed price string on one entry: existing `Decimal(str(...))` / try-except path returns `None` for the parse, unchanged.
14. SDK dataclass with `model_dump`/`asdict` normalization: corrected selection applies after normalization, identical to today.

## Invariants

1. Best-of-book selection is order-independent: identical level sets produce identical `best_bid`/`best_ask` regardless of array order.
2. `best_bid` is the maximum positive bid; `best_ask` is the minimum positive ask. Always.
3. The REST parser and the WS parser (`ws_client._best_bid_from_levels` / `_best_ask_from_levels`) use the same best-of-book selection semantics. They must not diverge again.
4. All pricing arithmetic uses `Decimal` end to end. No `float` coercion in the money/price path.
5. The client remains strictly read-only: no signing, no broadcasting, no order execution, no wallet mutation, no private keys.
6. `MarketSnapshot` schema, validators, and field set are unchanged.
7. No execution path, no `LLMEvaluationResponse`, no Gatekeeper logic is modified.
8. No `dry_run` weakening; no `DRY_RUN=false` behavior introduced or changed.
9. No raw DB sessions, no raw SQL, no Alembic migration, no `Base.metadata.create_all()`.
10. Crossed-book and missing-side inputs still return `None` (fail-closed to non-tradable).
11. Real wide spreads are reported truthfully; the fix never narrows a genuinely wide book.
12. Tests cover worst→best ordering correction on both sides, order-independence, non-positive filtering, single-level, empty-side, crossed-book, dict/dataclass parity, and Decimal integrity.
