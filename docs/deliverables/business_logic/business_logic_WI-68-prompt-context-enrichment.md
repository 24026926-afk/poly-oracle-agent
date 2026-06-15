# Business Logic - WI-68 Prompt Context Enrichment + Re-diagnosis

## Objective

Eliminate the **prompt-starvation confound** behind the WI-67 finding that DeepSeek's
`p_true` tracks the Polymarket midpoint (EV ≈ 0, no edge). The terminal-Gatekeeper
profile-comparison backtest proved that loosening the gate only converts a zero-trade
gate into a losing one — but it ran on a prompt that fed the LLM **only `condition_id`
+ prices**, never the market question. WI-68 feeds the LLM the real context that is
already present upstream (the market **question** text, and — production only — the
Grok **sentiment** signal), then re-runs the diagnostic to settle whether `p_true ≈
midpoint` is a *starved-input artifact* (alpha discarded at the prompt layer) or
*genuine LLM weakness* (justifying WI-71's external-data path).

This is the cheapest experiment that can falsify the whole Phase premise, so it runs
first and gates the rest of Phase 17.

### Why

`PromptFactory.build_evaluation_prompt(market_state, category, sentiment)` renders only
`market_state["condition_id"]`, the four price fields, and the timestamp into the
`### LIVE MARKET DATA SNAPSHOT` block. The production `market_state` dict assembled by
`ContextAggregator` (`aggregator.py:520-531` and `:659-673`) **already carries**
`question`, `title`, `category`, and `tags` (sourced from the WS-tracked
`MarketMetadata.question`), but the prompt template **silently drops every one of
them**. The LLM is therefore asked to estimate `P(YES)` for a market it cannot name.
A model with no question text has no choice but to echo the midpoint — which is exactly
the WI-67 symptom. The question is plumbed all the way to the template and discarded at
the last inch; rendering it is the core fix and requires **no new production plumbing**.

The backtest path (`scripts/run_profile_comparison_backtest.py`) is worse: it calls
`build_evaluation_prompt(market_state)` with **no category and no sentiment**, and the
WI-43 historical dataset rows carry no question text at all (only `token_id`,
`condition_id`, prices, `market_end_date`, and a separate `resolved_outcome`). To
re-run the diagnostic with enriched context, the backtest must source each market's
question text from the Gamma metadata API by `condition_id` — static market metadata
that existed at snapshot time and does **not** encode the resolution (lookahead-safe).

### Why this is safe

The change adds **no new bypass** and touches **no money path**. `build_evaluation_prompt`
is a pure string builder; enriching it changes what the LLM reads, never how the
Gatekeeper computes EV/Kelly/spread or how the system decides. `LLMEvaluationResponse`
remains the unconditional terminal Gatekeeper, recomputing all arithmetic from
authoritative market facts (WI-65). The enrichment surfaces **only fields already
present upstream** — `MarketMetadata.question` for the question, the existing
`SentimentResponse` for sentiment — so the LLM Evaluation Guard ("no invented market
metadata") holds: no fabricated questions, balances, fees, or odds. The dataset stays
lookahead-safe: the resolved outcome is read **only** for PnL scoring after the
candidate is produced, never injected into the prompt. The entire WI is offline /
`DRY_RUN`; no signing, broadcasting, schema migration, or execution path is touched.

## Data Models

Pydantic schema names only (all existing unless marked **new**):

- `LLMEvaluationResponse` (existing, `src/schemas/llm.py`) — terminal Gatekeeper.
  Unchanged. Continues to recompute EV/Kelly/spread from authoritative facts.
- `ProbabilisticEstimate` (existing, nested in `LLMEvaluationResponse`) — carries the
  LLM's `p_true` and `p_market`. The diagnostic reads `p_true` vs `midpoint`; no change.
- `SentimentResponse` (existing, `src/schemas/llm.py`) — the Grok Stage-A artifact the
  production prompt already accepts via `build_evaluation_prompt(sentiment=...)`.
- `MarketCategory` (existing) — persona selection; unchanged.
- `MarketMetadata` (existing, `src/schemas/market.py:115`) — `question`/`category`/`tags`
  fields are the authoritative source of the enrichment text. **No fabrication beyond
  these.**
- `MarketSnapshotSchema` (existing, `src/schemas/market.py:68`) — already carries a
  `question` field (default `""`); confirms `question` is a first-class, non-invented
  market attribute.
- **new (script-local, frozen Pydantic V2):** `EnrichmentDeltaRecord` — one row per
  snapshot: `token_id`, `condition_id`, `midpoint` (`Decimal`-as-str), `p_true_baseline`
  (`Decimal`-as-str), `p_true_enriched` (`Decimal`-as-str), `delta_baseline`
  (`|p_true_baseline − midpoint|`), `delta_enriched` (`|p_true_enriched − midpoint|`),
  `question_present` (bool).
- **new (script-local, frozen Pydantic V2):** `EnrichmentDiagnosticReport` — aggregate
  over all records: count, mean/median/max of `delta_enriched` and `delta_baseline`,
  the count of snapshots where enriched delta exceeds a materiality threshold, plus the
  two profile tallies carried over from the WI-67 script. Emitted as JSON to
  `docs/backtests/`.

No new persisted DB schema, no DB model change, no enum, no Alembic migration. The new
typed models live in the backtest script (Pydantic at the script boundary), not in
`src/schemas/`, to respect the WI-68 file structure.

## Key Rules

1. **Enrichment text comes only from real upstream data.** The question is read from
   `market_state["question"]` (production) or fetched per `condition_id` from the Gamma
   metadata client (backtest). Never synthesize, paraphrase into facts, or invent a
   question, description, balance, fee, or odds. An absent/empty question falls back to
   a neutral "question unavailable" line — never a fabricated one.
2. **Production: render the question already in `market_state`.** `build_evaluation_prompt`
   adds the market question (and optionally category/tags) to the snapshot block,
   reading `market_state.get("question")`. No change to `ContextAggregator` plumbing is
   required — the dict already carries it.
3. **Backtest: source the question lookahead-safely.** The question text is static
   market metadata fetched by `condition_id`; it existed at snapshot time and does not
   encode the resolution. It is injected into `market_state` before
   `build_evaluation_prompt`. The fetch uses an explicit timeout + bounded retry. If the
   question cannot be fetched, the snapshot is evaluated baseline-only (typed skip of
   the enriched arm), never with a fabricated question.
4. **Grok sentiment is production-only for this WI.** `SentimentResponse` is a real-time
   "last 60 min" signal; it cannot be reconstructed for a historical snapshot date
   without lookahead/availability corruption. The backtest re-diagnosis enriches with
   **question text only** and keeps sentiment neutral (documented), so the measured
   delta is attributable to the question, not to a contaminated sentiment proxy.
5. **The diagnostic measures the right thing.** It reports the per-snapshot
   `|p_true − midpoint|` **delta distribution** for baseline vs enriched candidates
   (mean/median/max + count above a materiality threshold), not just trade counts. A
   trade-count-only verdict cannot distinguish "moved off midpoint but still HOLD" from
   "still pinned to midpoint."
6. **Lookahead safety is absolute.** The resolved outcome is read only for realized-PnL
   scoring after the candidate exists; it is never part of any prompt, baseline or
   enriched.
7. **Decimal integrity.** All probability/midpoint/EV/PnL math is `Decimal`. `p_true`,
   `midpoint`, and the deltas are carried as `Decimal` and serialized via `str()` at the
   JSON boundary. The pre-existing `float()` conversions inside the script's
   `market_state` (mirroring the schema's float price comparison) are unchanged; no new
   `float` enters a money/EV/PnL path.
8. `structlog` only in library code; the backtest CLI may `print()` its final report
   (PRD §5 carve-out). No new package dependency — reuse `httpx`/`anthropic`/existing
   Gamma client.
9. Class names unchanged (`PromptFactory`, `ClaudeClient`, `LLMEvaluationResponse`).

## Edge Cases

1. **Empty/missing question** (`market_state["question"] == ""` or fetch fails) →
   prompt renders a neutral "market question unavailable" line; never a fabricated one.
   In the backtest, the enriched arm for that snapshot is skipped (baseline still runs).
2. **Question present in production dict but template unchanged (pre-WI-68 regression
   baseline)** → confirms the field was being dropped; the fix renders it.
3. **Gamma fetch timeout / HTTP error in the backtest** → bounded retry, then typed
   skip of the enriched arm for that snapshot; the run continues; the skip is counted.
4. **Sentiment present (production) vs neutral fallback** → existing
   `_build_sentiment_block` behavior is preserved; WI-68 does not change sentiment
   handling, only adds the question.
5. **Enriched `p_true` still tracks midpoint** (delta distribution unchanged vs
   baseline) → documented verdict (b): LLM-alone is genuinely weak; WI-71's external-data
   path is justified.
6. **Enriched `p_true` moves materially off midpoint** (enriched delta distribution
   shifts right of baseline) → documented verdict (a): alpha was being discarded at the
   prompt layer; the production prompt fix is itself a result, and per-category edge
   analysis (WI-70) becomes worthwhile.
7. **Cache reuse:** the WI-67 candidate cache is keyed by `token|timestamp`; WI-68 must
   extend the key (or use a separate cache) so baseline and enriched candidates are not
   cross-served from one cache entry.
8. **Decimal/`float` boundary:** a snapshot price that is non-finite or out of `[0,1]`
   is handled by the existing `_realized_pnl` guard (midpoint ∉ (0,1) → 0 PnL); WI-68
   adds no new arithmetic that could divide by zero.

## Invariants

1. `LLMEvaluationResponse` remains the terminal, unconditional Gatekeeper; no execution
   path bypasses it. EV/Kelly/spread arithmetic is unchanged.
2. Enrichment surfaces only fields actually present upstream (`MarketMetadata.question`,
   `SentimentResponse`); no fabricated market metadata (LLM Evaluation Guard holds).
3. The LLM never sees a resolved outcome during evaluation; the dataset stays
   lookahead-safe (outcomes read only for post-hoc PnL).
4. No new `float` in any probability/midpoint/EV/Kelly/PnL/ROI path; new diagnostic
   fields are `Decimal`, serialized as strings.
5. No `dry_run` weakening, no `DRY_RUN=false` behavior, no signing or broadcasting; the
   entire WI is offline.
6. No Alembic migration, no `Base.metadata.create_all()`, no persisted-schema change.
7. Every new HTTP path (the backtest's Gamma question fetch) has an explicit timeout and
   bounded retry; it fails closed to a typed skip of the enriched arm.
8. The diagnostic produces a **written, documented verdict** ((a) prompt-starvation or
   (b) genuine LLM weakness) persisted to `docs/backtests/` and summarized in `STATE.md`.
9. Tests cover: question rendered when present; neutral fallback when absent/empty; no
   fabricated text on fallback; sentiment block unchanged; the per-snapshot delta-record
   math (`Decimal`, `|p_true − midpoint|`); baseline-vs-enriched aggregate; the backtest
   Gamma-fetch timeout → typed skip; cache key separation; lookahead safety (outcome
   never in prompt).
