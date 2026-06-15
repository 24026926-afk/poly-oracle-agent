# PRD v17.0 — Phase 17: Alpha Discovery

**Phase:** 17
**Status:** Ready for implementation
**Author:** Claude Code (Staff Software / Quantitative Systems Engineer)
**Date:** 2026-06-02
**Predecessor:** Phase 16 (WI-67 Configurable Gatekeeper Risk Profiles, COMPLETE)

---

## 1. Objective

Determine whether `poly-oracle-agent` can extract any tradable edge over Polymarket
prices, and if so isolate it to a single market domain — instead of loosening risk
gates, which the WI-67 profile-comparison backtest empirically proved only converts
a zero-trade gate into a *losing* one.

**Triggering evidence (WI-67 follow-on, 2026-06-02):** a real-DeepSeek
profile-comparison backtest (217 snapshots / 6 resolved markets, same LLM candidate
under two gates) returned: conservative (0.75 conf / 0.02 EV) → 0 trades; aggressive
(0.65 / 0.005) → 3 trades, **0 wins, net −5.30 USDC**, average confidence pinned
exactly at the 0.65 floor. DeepSeek's `p_true` tracks the market midpoint, so EV ≈ 0
and there is no information edge. **The gate is not the bottleneck — the signal is.**

This Phase replaces "loosen the gate" with "find a signal," and front-loads the
cheapest experiment that can falsify the whole premise.

---

## 2. Scope Boundaries

### In scope
- Enriching the evaluation prompt with context the repositories already hold
  (market question + Grok sentiment) and re-running the diagnostic to settle the
  "weak LLM vs. starved LLM" confound.
- Building a larger, category-tagged, lookahead-safe historical dataset.
- A per-category alpha diagnostic backtest (edge vs. midpoint, Brier calibration,
  realized ROI, confidence-bucket breakdown).
- A single-domain external-fair-value edge experiment (the cleanest domain surfaced
  by the diagnostic), traded only when external fair value disagrees with Polymarket
  after spread — backtested offline.

### Out of scope
- **Any further loosening of Gatekeeper thresholds.** WI-67 settled this.
- **Live trading.** Everything in this Phase runs offline / `DRY_RUN=true`. No
  signing, no broadcasting, no state-mutating execution.
- New live execution paths, new order-routing logic, schema migrations to the live
  decision/execution tables (the diagnostic backtest is read-only against history).
- Multi-domain external integrations — WI-71 wires exactly one domain. Additional
  domains are a future Phase, gated on WI-71 showing positive net ROI.

---

## 3. Work Items

### WI-68 — Prompt Context Enrichment + Re-diagnosis

**Goal:** Eliminate the prompt-starvation confound. The production
`PromptFactory.build_evaluation_prompt` currently passes the LLM only `condition_id`
+ prices — no market question, no news, no sentiment — so `p_true ≈ midpoint` is
partly forced by starved input rather than proven LLM weakness. Feed the LLM the
real context already present in the repositories and Grok sentiment oracle, then
re-run the profile-comparison backtest.

**File structure:**
- `src/agents/context/prompt_factory.py` — enrich `build_evaluation_prompt` (and/or
  the backtest prompt path) with market question text + sentiment, sourced from
  existing repositories/oracles. **No invented market metadata** (LLM Evaluation
  Guard): only fields actually present upstream.
- `scripts/run_profile_comparison_backtest.py` — extend to carry the enriched
  context; emit an enriched-vs-baseline diagnostic.
- `tests/unit/test_WI-68-prompt-context-enrichment.md` → `tests/unit/test_WI-68-prompt-context-enrichment.py`.

**Core requirements:**
- Enriched context must come only from real upstream data (repos, Grok oracle); no
  fabricated questions, balances, or metadata.
- Backtest dataset stays lookahead-safe — the LLM never sees the resolved outcome.
- Diagnostic must report whether enriched `p_true` diverges from midpoint
  (per-snapshot delta distribution), not just trade counts.
- All money/price/EV math `Decimal`. No `float` in money paths.

**Definition of Done:**
- Re-run diagnostic produces a documented verdict: enriched context either (a) moves
  `p_true` materially off the midpoint → alpha was discarded at the prompt layer, or
  (b) does not → LLM-alone is genuinely weak and WI-71's external-data path is
  justified.
- Result written to `docs/backtests/` and summarized in `STATE.md`.
- Tests green; coverage ≥ 80%.

---

### WI-69 — Multi-Category Historical Dataset

**Goal:** The WI-67 diagnostic ran on 6 markets — too few for any per-category
statistic. Extend the WI-43 historical-dataset builder to assemble ≥200 resolved
markets tagged by category (sports / weather / crypto / politics / macro), suitable
for calibration scoring.

**File structure:**
- `src/backtesting/polymarket_history_client.py` — category-aware fetch (Gamma
  market tags/category field), bounded retry + explicit timeout (unchanged
  invariant).
- `src/backtesting/historical_dataset.py` — persist a category label per market;
  remain `BacktestDataLoader`-compatible.
- `src/backtesting/schemas.py` — add category to the typed manifest/outcome schema.
- `tests/unit/test_WI-69-multi-category-dataset.md` → `tests/unit/test_WI-69-multi-category-dataset.py`.

**Core requirements:**
- ≥200 resolved markets across ≥4 categories; each market category-labelled.
- Lookahead-safe: outcomes stored separately from snapshots (mirrors WI-43).
- Every HTTP path explicit-timeout / bounded-retry.
- Prices as `Decimal`-safe strings.

**Definition of Done:**
- Builder yields ≥200 markets with category labels; manifest validates.
- Dataset loads via `BacktestDataLoader` without error.
- Tests green (mock-HTTP, no live network in CI); coverage ≥ 80%.

---

### WI-70 — Alpha Diagnostic Backtest

**Goal:** The core deliverable. Produce a per-category calibration + edge report
answering "where, if anywhere, is there signal," using the WI-69 dataset and the
WI-68-enriched prompt.

**File structure:**
- `scripts/run_alpha_diagnostic_backtest.py` (new) — runs the real-LLM eval over the
  WI-69 dataset; emits a per-category report.
- `src/backtesting/diagnostics.py` (new) — typed (Pydantic V2) Brier/ROI/calibration
  computations; `Decimal` throughout.
- `tests/unit/test_WI-70-alpha-diagnostic.md` → `tests/unit/test_WI-70-alpha-diagnostic.py`.

**Core requirements:**
- Per-category report columns: `p_true` edge vs. midpoint, realized outcome, Brier
  score, ROI if traded, confidence-bucket breakdown (Codex steps 2 + 5).
- Negative filter built in: flag categories where `p_true ≈ midpoint`, confidence
  sits at threshold, or spread eats EV.
- Reuses the WI-67 gatekeeper-via-validation-context mechanism; does NOT bypass
  `LLMEvaluationResponse`.
- `Decimal` for all money/probability/EV/ROI math.

**Definition of Done:**
- Report identifies the category with the best calibration/edge, or explicitly
  proves none exists (all categories track midpoint → documented kill of the
  LLM-alone thesis).
- Report persisted to `docs/backtests/`; summary in `STATE.md`.
- Tests green; coverage ≥ 80%.

---

### WI-71 — Single-Domain External-Odds Edge

**Goal:** Take the cleanest domain surfaced by WI-70 (expected: sports or weather —
cleaner external truth than politics), wire ONE external fair-value source, and
backtest a rule that trades only when external fair value disagrees with Polymarket
after spread.

**File structure:**
- `src/agents/context/<domain>_fair_value.py` (new) — async external fair-value
  client; explicit timeout + bounded retry; typed Pydantic V2 response.
- `scripts/run_external_edge_backtest.py` (new) — offline backtest comparing
  external fair value vs. Polymarket midpoint on held-out WI-69 markets.
- `tests/unit/test_WI-71-external-odds-edge.md` → `tests/unit/test_WI-71-external-odds-edge.py`.

**Core requirements:**
- Trade signal fires only when `|external_fair_value − polymarket_midpoint| > spread`
  in the favorable direction; all comparisons `Decimal`.
- External client mock-first; no live keys required for CI.
- Offline / `DRY_RUN` only — no live signing or broadcast. Fail closed on missing
  external data (typed skip, never silent fallthrough).
- Held-out evaluation (train/test split or disjoint market set) to avoid overfitting
  the WI-70 selection.

**Definition of Done:**
- Backtested net ROI > 0 after spread on held-out markets → candidate edge documented
  for a future live-readiness Phase; OR negative result → documented kill with the
  reason.
- Result persisted to `docs/backtests/`; summary in `STATE.md`.
- Tests green; coverage ≥ 80%.

---

## 4. Phase Definition of Done (global gate)

ALL of the following must hold before Phase 17 is marked COMPLETE:

1. WI-68, WI-69, WI-70 DoDs all pass. (WI-71 is conditional — see note.)
2. Full test suite green; coverage ≥ 80% (project floor; current ≈ 93%).
3. A single written conclusion answering the Phase objective: *is there a tradable
   edge, and in which domain* — stored in `docs/backtests/` and `STATE.md`.
4. No new `float` in any money / price / EV / Kelly / PnL / ROI path.
5. No new live-execution path; `DRY_RUN` posture unchanged; `LLMEvaluationResponse`
   remains the unconditional terminal Gatekeeper everywhere.
6. MAAP cleared on every commit touching `src/agents/`, `src/schemas/`,
   `src/backtest_runner.py`, or `src/backtesting/`.

**Conditional note on WI-71:** WI-71 only proceeds if WI-68 or WI-70 surfaces a
plausible signal. If WI-70 proves every category tracks the midpoint *with enriched
context* (WI-68 result (b) + WI-70 all-null), the honest outcome is to close the
Phase on the documented kill and NOT build WI-71. This is an explicit, approved
off-ramp — not a failure.

---

## 5. Constraints & Non-Negotiables

Per `CLAUDE.md` / `AGENTS.md` (these documents are the law):

- **No `float` for money, price, EV, Kelly, sizing, PnL, or ROI. Ever.** `Decimal()` only.
- **No live order signing or broadcast.** This entire Phase is offline / `DRY_RUN=true`.
- **No execution path that bypasses `LLMEvaluationResponse`.** The diagnostic reuses
  the WI-67 validation-context mechanism.
- **`PromptFactory` must assemble real market context, not invented data** — WI-68
  enriches only with fields actually present in repos/oracles.
- **No direct `main` commits.** Work on `develop`; PR `develop` → `main` per WI.
- **Repository pattern only** for any DB access; no raw SQL in agent code (the
  diagnostic is read-only against historical files, not the live DB).
- **Every HTTP/RPC path** explicit-timeout + bounded-retry.
- **Lookahead safety:** the LLM never sees a resolved outcome during evaluation.
- Python 3.12+, Pydantic V2 at boundaries, `asyncio` for I/O, `structlog` only
  (no `print()` in production paths — backtest CLIs may print their final report).
- CI `ruff format` gate: run `ruff format` before pushing.
- Class names unchanged (`ClaudeClient`, `PromptFactory`, `Orchestrator`, etc.).

---

## 6. Dependencies to Add

- **WI-68, WI-69, WI-70:** none expected (reuse `httpx`, `anthropic`, existing stack).
- **WI-71:** one external-data client dependency may be required for the chosen domain
  (e.g. a sports-odds or weather API SDK, or plain `httpx` against a public endpoint).
  Selected and justified at `/wi-start WI-71`, not pre-committed here.

No dependency is added during PRD creation. New packages are introduced only in the
WI that needs them, with rationale.

---

## 7. Deliverables Summary

| WI | Primary new/changed artifact |
|----|------------------------------|
| WI-68 | `prompt_factory.py` enrichment + re-diagnosis report in `docs/backtests/` |
| WI-69 | category-aware `polymarket_history_client.py` / `historical_dataset.py` + ≥200-market dataset |
| WI-70 | `scripts/run_alpha_diagnostic_backtest.py` + `src/backtesting/diagnostics.py` + per-category report |
| WI-71 | `src/agents/context/<domain>_fair_value.py` + `scripts/run_external_edge_backtest.py` + ROI report |

Per the `/prd` scope boundary in `CLAUDE.md`, this PRD does **not** generate
`business_logic_WI-XX-*.md` or `prompt_WI-XX-*.md`. Those are produced one at a time,
only when `/wi-start {WI}` is explicitly invoked.

---

## 8. State & Documentation Updates on Phase Completion

On Phase 17 completion:
- Update `STATE.md`: version bump, Phase 17 section, per-WI completion notes, final
  test count + coverage, the single alpha verdict.
- Persist all backtest reports under `docs/backtests/`.
- Append the session summary to `~/documents/integration_task/03_Daily/YYYY-MM-DD.md`.
- Open PR `develop` → `main` for each completed WI per the Git rules.
- If the Phase closes on the documented "no edge" off-ramp, record that conclusion
  prominently in `STATE.md` and the daily note — a kill is a valid Phase outcome.
