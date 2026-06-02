# Implementation Prompt - WI-68 Prompt Context Enrichment + Re-diagnosis

## Session Context

You are working in `poly-oracle-agent` at the start of Phase 17 (Alpha Discovery). The
WI-67 profile-comparison backtest (`scripts/run_profile_comparison_backtest.py`, merged
`develop`) empirically proved that loosening the terminal Gatekeeper produces no edge:
on 217 snapshots / 6 resolved markets, conservative (0.75/0.02) → 0 trades; aggressive
(0.65/0.005) → 3 trades, 0 wins, net −5.30 USDC. DeepSeek's `p_true` tracked the
midpoint, so EV ≈ 0. **The gate is not the bottleneck — the signal is.**

But that diagnostic ran on a **starved prompt**. `PromptFactory.build_evaluation_prompt`
renders only `condition_id` + prices + timestamp into its market block — it never shows
the LLM the market **question**. A model that cannot read the question has no basis to
diverge from the midpoint, so `p_true ≈ midpoint` may be a starved-input artifact rather
than proven LLM weakness. WI-68 removes that confound: feed the LLM the real question
text (already present upstream) and re-run the diagnostic.

Current baseline (read before implementing):

- `src/agents/context/prompt_factory.py` — `build_evaluation_prompt(market_state,
  category, sentiment)` renders `market_state["condition_id"]`, `best_bid`, `best_ask`,
  `midpoint`, `spread`, `timestamp`, plus a sentiment block. It **does not** render the
  market question even though the dict carries it.
- `src/agents/context/aggregator.py` (lines ~520-531 and ~659-673) — the production
  `state` / `state_dict` dict **already contains** `"question"`, `"title"`,
  `"category"`, `"tags"` (from the WS-tracked `MarketMetadata.question`). No new
  plumbing is needed to reach the question in production.
- `src/agents/evaluation/claude_client.py` (~line 819) — production call site passing
  `market_state`, `category`, `sentiment`.
- `scripts/run_profile_comparison_backtest.py` — calls `build_evaluation_prompt(market_state)`
  with no category/sentiment; builds `market_state` from dataset rows that carry **no
  question**; caches candidates keyed by `token|timestamp`.
- `data/historical_sample/` — WI-43 dataset: snapshot rows (`token_id`, `condition_id`,
  prices, `market_end_date`) + separate `*_outcomes.json` (`resolved_outcome`). No
  question text, no `manifest.json`.
- `src/schemas/market.py` — `MarketMetadata.question` / `MarketSnapshotSchema.question`
  are the authoritative, non-invented source of the question text.
- `DRY_RUN=false`, live signing, live broadcasting, and any path bypassing
  `LLMEvaluationResponse` remain out of scope and forbidden.

Before implementing, read: `AGENTS.md`, `STATE.md`, `README.md`,
`docs/system_architecture.md`, `docs/PRD-v17.0.md`,
`docs/deliverables/business_logic/business_logic_WI-68-prompt-context-enrichment.md`,
`src/agents/context/prompt_factory.py`, `src/agents/context/aggregator.py` (the two
state-dict build sites), `src/agents/evaluation/claude_client.py` (the prompt call site
+ `evaluate_for_backtest`), and `scripts/run_profile_comparison_backtest.py`.

## Objective

1. Enrich `build_evaluation_prompt` to render the market **question** (from
   `market_state["question"]`, with a neutral fallback when absent), surfacing context
   the production pipeline already carries but silently drops.
2. Extend `scripts/run_profile_comparison_backtest.py` to (a) source the question per
   `condition_id` from the Gamma metadata client (lookahead-safe static metadata), (b)
   produce both a **baseline** (no question) and an **enriched** (question) candidate
   per snapshot, and (c) emit a per-snapshot `|p_true − midpoint|` delta diagnostic
   comparing the two arms.
3. Produce a documented verdict — (a) enriched context moves `p_true` materially off the
   midpoint (alpha discarded at the prompt layer) or (b) it does not (LLM-alone is
   genuinely weak, justifying WI-71) — written to `docs/backtests/` and summarized in
   `STATE.md`.

## Inputs

- `market_state` dict (production already carries `question`; backtest must inject it).
- `MarketMetadata.question` (production source) and the Gamma metadata client (backtest
  source, fetched by `condition_id`).
- `SentimentResponse` — production sentiment, already wired; **not** reconstructed for
  the backtest.
- The WI-43 historical dataset under `data/historical_sample/` (lookahead-safe; outcomes
  separate from snapshots).
- No new Python package dependency. Standard library, `pydantic` (>=2.x), `Decimal`,
  `httpx`/existing Gamma client, `anthropic` via `ClaudeClient` only.

## Outputs

- `src/agents/context/prompt_factory.py`:
  - `build_evaluation_prompt` renders the market question from
    `market_state.get("question")` inside the market block; an absent/empty question
    yields a neutral "market question unavailable" line (never fabricated text).
    Sentiment handling unchanged. Optionally surface category/tags already in the dict.
- `scripts/run_profile_comparison_backtest.py`:
  - Per `condition_id`, fetch the question from the Gamma metadata client (explicit
    timeout + bounded retry; fail closed to a typed skip of the enriched arm).
  - Build a **baseline** `market_state` (no question) and an **enriched** `market_state`
    (with question); evaluate both via `ClaudeClient.evaluate_for_backtest`.
  - Extend the candidate cache key so baseline and enriched candidates are stored
    separately (no cross-serving).
  - Emit `EnrichmentDeltaRecord` per snapshot and an aggregate `EnrichmentDiagnosticReport`
    (frozen Pydantic V2, script-local, `Decimal`-native): mean/median/max
    `|p_true − midpoint|` for baseline vs enriched + count above a materiality threshold.
- `docs/backtests/wi68_enrichment_diagnostic.json` — the diagnostic report.
- `tests/unit/test_WI-68-prompt-context-enrichment.py` — unit tests (RED first, then
  GREEN).
- `STATE.md` — WI-68 completion entry with the verdict, on `/wi-done`.

## Acceptance Criteria

1. When `market_state["question"]` is present, `build_evaluation_prompt` includes the
   exact question text in the prompt; when absent/empty, it renders a neutral
   unavailable line and never fabricated text.
2. Sentiment block behavior is byte-identical to pre-WI-68 (no regression in
   `_build_sentiment_block`).
3. The backtest produces, per snapshot, both a baseline (no question) and an enriched
   (question) candidate, and records `p_true` for each.
4. The diagnostic report contains the per-snapshot `|p_true − midpoint|` delta for both
   arms and the aggregate (mean/median/max + count above the materiality threshold),
   all `Decimal`, serialized as strings.
5. The backtest's Gamma question fetch uses an explicit timeout + bounded retry and
   fails closed to a typed skip of the enriched arm (baseline still runs); the skip is
   counted in the report.
6. Lookahead safety: the resolved outcome never appears in any prompt (baseline or
   enriched); a test asserts the prompt string contains no outcome token.
7. The candidate cache cannot cross-serve a baseline candidate as an enriched one (cache
   key includes the arm).
8. A written verdict ((a) or (b)) is produced in `docs/backtests/` and summarized in
   `STATE.md`.
9. Full regression passes with coverage ≥ 80%; no existing test regresses.
   `ruff format` and `ruff check` clean.

## Anti-Patterns

- Do not invent, paraphrase-into-fact, or synthesize a market question, description,
  balance, fee, or odds. Enrichment uses only `MarketMetadata.question` /
  `market_state["question"]` (LLM Evaluation Guard).
- Do not inject the resolved outcome — or anything derived from it — into any prompt.
- Do not reconstruct historical Grok sentiment for the backtest; it is real-time
  "last 60 min" data and is not lookahead-safe. Keep backtest sentiment neutral.
- Do not change `LLMEvaluationResponse`, its arithmetic, filter order, or the WI-67
  validation-context mechanism. The prompt is the only LLM-facing change.
- Do not introduce `float` into any probability/midpoint/EV/Kelly/PnL/ROI path; new
  diagnostic fields are `Decimal`, serialized as strings.
- Do not report a verdict on trade counts alone; the delta distribution is the metric.
- Do not add new production plumbing for the question — it is already in `market_state`.
- Do not weaken `DRY_RUN`; do not add signing or broadcasting; do not add a Gamma fetch
  without explicit timeout + bounded retry.
- Do not add DB schema/fields, Alembic migrations, or `Base.metadata.create_all()`.
- Do not add `print()` to library code (the backtest CLI's final report is the only
  permitted print); do not add new package dependencies; do not delete files outside
  this WI's scope.

## Dependencies

- WI-67 — configurable Gatekeeper risk profiles (the profile-comparison script and its
  validation-context mechanism that WI-68 extends).
- WI-65 — deterministic eval math (authoritative market facts override LLM-echoed
  quotes before gatekeeping; the diagnostic relies on it).
- WI-43 — historical-dataset builder (the lookahead-safe dataset under
  `data/historical_sample/`).
- `src/agents/context/prompt_factory.py` — `build_evaluation_prompt`,
  `_build_sentiment_block`.
- `src/agents/context/aggregator.py` — the production `market_state` dict that already
  carries `question`.
- `scripts/run_profile_comparison_backtest.py` — the backtest harness to extend.
- The Gamma metadata client (`MarketMetadata`) — backtest question source.

## Target Layer

Prompt-construction layer (`PromptFactory`, a pure string builder) plus the offline
backtest harness (`scripts/run_profile_comparison_backtest.py`). WI-68 changes **what
context the LLM reads** and **what the diagnostic measures**; it does not touch market
discovery, execution routing, signing, broadcasting, the Gatekeeper's arithmetic or
filter order, the live DB, or persistence. `LLMEvaluationResponse` remains the terminal,
unconditional Gatekeeper, and the entire WI runs offline / `DRY_RUN`.
