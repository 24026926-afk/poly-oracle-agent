# Implementation Prompt - WI-63 REST Order Book Best-of-Book Selection

## Session Context

You are working in `poly-oracle-agent` after Phase 16, the post-Phase 16 runtime stabilization series, WI-61/WI-62 operational hardening, and the 2026-05-30 log-disk hotfix. WI-63 is a standalone financial-integrity bug fix surfaced by the 2026-05-30 60-hour production dry-run debrief.

Current baseline and root cause:

- A 60-hour DigitalOcean dry-run made zero trades: 100% HOLD across ~3,000 decisions.
- Root cause: `PolymarketClient._parse_order_book` (`src/agents/execution/polymarket_client.py`) reads `bids[0]` / `asks[0]`. The Polymarket CLOB REST order book returns levels ordered worst → best, so index `[0]` is the worst level on each side. This fabricates a near-empty book (`bid≈0.001`, `ask≈0.999`), a ~99.8% spread, and a 0.5 midpoint on liquid markets.
- This REST snapshot feeds the LLM evaluation context: `claude_client.py` overwrites `market_state["best_bid"|"best_ask"|"midpoint"|"spread"]` from `wi14_snapshot` before prompt assembly. It also gates WI-53 discovery preflight via `market_discovery.py::_get_order_book_quotes`.
- The WebSocket path was already fixed for this exact defect: `ws_client.py::_best_bid_from_levels` uses `max(positive)` and `_best_ask_from_levels` uses `min(positive)`, with the comment "Pick max bid and min ask so a full book cannot fabricate a [wide spread]." The REST path was never given the same fix.
- Empirical proof (2026-05-30 debrief): the WS-fed `market_snapshots` table shows ~69% of 749k snapshots had < 10% spread (tradable), while the REST-fed `agent_decision_logs` cluster at implied-probability ≈ 0.5 with `reasoning_log` citing `0.999-0.001=0.998`.
- `DRY_RUN=false` remains out of scope. Live signing, live broadcasting, and execution paths that bypass `LLMEvaluationResponse` remain forbidden.

Before implementing code, read:

- `AGENTS.md`
- `STATE.md`
- `README.md`
- `docs/system_architecture.md`
- `docs/deliverables/business_logic/business_logic_WI-63-rest-orderbook-best-of-book-selection.md`
- `src/agents/execution/polymarket_client.py` (target file)
- `src/agents/ingestion/ws_client.py` (reference: `_best_bid_from_levels`, `_best_ask_from_levels`)
- `src/agents/evaluation/claude_client.py` (consumer: WI-14 snapshot enrichment)
- `src/agents/ingestion/market_discovery.py` (consumer: preflight `_get_order_book_quotes`)

## Objective

Correct `PolymarketClient._parse_order_book` to select best-of-book — the maximum positive bid price and the minimum positive ask price across the full `bids`/`asks` arrays — instead of reading index `[0]`. Mirror the proven WebSocket selection semantics so the two parsers cannot diverge again. Preserve all existing contracts, schemas, Decimal integrity, and fail-closed behavior.

## Inputs

- Raw Polymarket CLOB order book responses: `dict` with `bids` / `asks` lists, where each entry is a `dict` (`{"price": ..., "size": ...}`) or an SDK dataclass (`OrderSummary` with `.price` / `.size`). Levels are ordered worst → best.
- SDK dataclass responses normalized via `model_dump` / `asdict` / `vars` (existing logic, unchanged).
- The WS helpers `_best_bid_from_levels` (`max(positive)`) and `_best_ask_from_levels` (`min(positive)`) as the canonical selection reference.
- No new Python package dependencies. Standard library, `pydantic`, `structlog`, `Decimal` only.

## Outputs

- `src/agents/execution/polymarket_client.py` — corrected `_parse_order_book`:
  - `best_bid` = maximum positive bid price across all `bids` entries.
  - `best_ask` = minimum positive ask price across all `asks` entries.
  - Order-independent selection; supports `dict` and dataclass entries.
  - Non-positive levels filtered out of selection; no positive level on a side → `None`.
  - Crossed-book (`best_ask < best_bid`) → `None`, applied after correct selection.
  - `midpoint` / `spread` computed from corrected values; `MarketSnapshot` field set unchanged.
  - Malformed price fields → warning log + `None` (unchanged).
- `tests/unit/test_WI-63-rest-orderbook-best-of-book-selection.py` — unit tests (RED first, then GREEN).
- `STATE.md` — WI-63 completion entry on `/wi-done`.

## Acceptance Criteria

1. Given `bids` ordered worst→best (e.g. prices `[0.001, 0.30, 0.61]`), `_parse_order_book` returns `best_bid == Decimal("0.61")`.
2. Given `asks` ordered worst→best (e.g. prices `[0.999, 0.70, 0.63]`), `_parse_order_book` returns `best_ask == Decimal("0.63")`.
3. Selection is order-independent: shuffling the same level set yields identical `best_bid` / `best_ask`.
4. A tight book (`bid=0.985`, `ask=0.986`) yields `spread == Decimal("0.001")` and is not mis-flagged as wide.
5. Non-positive bid/ask levels are excluded from selection; a side with no positive level yields `None`.
6. Empty `bids` or empty `asks` yields `None` (preserved).
7. Genuinely crossed book (`best_bid > best_ask` after correct selection) yields `None`.
8. `dict` entries and SDK dataclass entries produce identical results for the same prices.
9. All returned price fields are `Decimal`; no `float` appears in the price path.
10. `MarketSnapshot` schema, validators, and `source="clob_orderbook"` are unchanged.
11. The REST selection semantics match the WS helpers (`max` bid / `min` ask, positive-only).
12. Full regression passes with coverage >= 80%; no existing test regresses.

## Anti-Patterns

- Do not read `bids[0]` / `asks[0]` for best-of-book. Always select `max` positive bid / `min` positive ask.
- Do not assume any array ordering. Selection must depend only on the level set.
- Do not introduce `float` anywhere in the price path. `Decimal` end to end.
- Do not change the `MarketSnapshot` schema, validators, or `source` value.
- Do not modify `claude_client.py` or `market_discovery.py` — they consume the corrected values unchanged.
- Do not change `fetch_order_book`'s public contract, timeout, or error→`None` semantics.
- Do not narrow or mask a genuinely wide book. Report real spreads truthfully.
- Do not add `print()`. Use `structlog` only. Do not add secret or high-cardinality log fields.
- Do not add execution, signing, broadcasting, wallet mutation, or Gatekeeper paths.
- Do not weaken `DRY_RUN` or introduce `DRY_RUN=false` behavior.
- Do not add raw DB sessions, raw SQL, Alembic migrations, or `Base.metadata.create_all()`.
- Do not add new Python package dependencies.

## Dependencies

- `src/agents/execution/polymarket_client.py` — target file (WI-14 read-only market data client).
- `src/agents/ingestion/ws_client.py` — `_best_bid_from_levels` / `_best_ask_from_levels` canonical reference (WS hotfix history in STATE.md, "use max bid and min ask").
- `src/agents/evaluation/claude_client.py` — downstream consumer (WI-14 snapshot enrichment of LLM context); not modified.
- `src/agents/ingestion/market_discovery.py` — downstream consumer (WI-53 preflight eligibility); not modified.
- 2026-05-30 60-hour dry-run debrief (root-cause evidence) recorded in `STATE.md`.

## Target Layer

Market data ingestion (Layer 1 input to the cognitive pipeline). The fix is confined to `PolymarketClient._parse_order_book`, a read-only public-market-data parser. It does not participate in signing, broadcasting, execution routing, or Gatekeeper logic; it corrects the pricing values those downstream layers already consume.
