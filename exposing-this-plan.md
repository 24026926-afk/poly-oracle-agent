# Aggressive 48-Hour Dry-Run Analysis Plan

## Executive Summary

This document describes a proposed **paper-trading only** experiment for the
`poly-oracle-agent` bot. The goal is to test whether the bot can produce enough
accepted simulated BUY/SELL decisions over a 48-hour window to evaluate whether
there is any real edge worth improving.

This plan is **not** approval to trade live. It does not authorize
`DRY_RUN=false`, wallet signing, order broadcasting, or any bypass of the
terminal `LLMEvaluationResponse` Gatekeeper.

The current evidence suggests the bot is no longer blocked primarily by broken
market data or reflection hard rejects. It is seeing positive-EV candidates, but
those candidates usually fail the confidence gate. Therefore, the proposed
experiment is to loosen confidence in dry-run while keeping EV, spread,
exposure, sizing, and validation controls intact.

## Current Bot Status And Last Observed Metrics

Last observed server state:

- Orchestrator container: running and healthy.
- Readiness endpoint: `READY`.
- Database: reachable.
- WebSocket: `CONNECTED`.
- Runtime mode: `DRY_RUN=true`.
- Active markets: approximately `28`.

Last observed 2-hour decision sample:

- Completed evaluations: `770`.
- Persisted decisions: `770`.
- Persisted actions: all `HOLD`.
- Accepted trade routes: `0`.
- Positive-EV candidates: `275`.
- Highest confidence among positive-EV candidates: `0.675`.
- Soft-flag downgrades: `115`.
- Final Gatekeeper validation failures: `2`.
- LLM budget blocks: `0`.

Last observed LLM/budget metrics:

- Primary LLM calls: `4658`.
- Reflection LLM calls: `4657`.
- Token usage: `19.37M / 30M`.
- Estimated spend: `$46.01`.

Last observed preflight counters:

- Preflight passes: `41,378`.
- Rejected for spread too wide: `10,910`.
- Rejected for unavailable order book: `3,717`.

Interpretation: the system is operationally healthy and evaluating markets at
scale. The remaining blocker is not a total lack of positive EV. The remaining
blocker is that confidence does not clear the configured trade gate.

## Why The Current Bottleneck Is Confidence, Not EV

The bot is producing positive-EV candidates, but they are not becoming accepted
dry-run trades because confidence stays below the configured minimum.

Current relevant default/running risk gate:

- `MIN_CONFIDENCE=0.75`

Observed evidence:

- `275` positive-EV candidates appeared in the last 2-hour sample.
- The highest confidence among positive-EV candidates was only `0.675`.
- Because `0.675 < 0.75`, even the best positive-EV candidate in that sample
  remained below the confidence gate.

This means simply waiting may not be enough. The bot may continue producing
positive-EV HOLDs indefinitely if the confidence threshold is stricter than the
model's calibrated confidence distribution.

The experiment should therefore test whether a lower confidence threshold
creates enough accepted dry-run decisions to analyze.

## Why This Must Remain `DRY_RUN=true`

The Facebook post attached for context makes a useful distinction: the valuable
part of a real prediction-market trading system is not the LLM wrapper. The edge
must come from a fair-value model that can estimate true probability better than
the market price after fees, spread, slippage, and resolution risk.

The current bot has strong infrastructure:

- live market data ingestion,
- structured evaluation,
- deterministic Gatekeeper validation,
- preflight filtering,
- repository-backed audit logs,
- LLM reflection,
- position and execution infrastructure,
- health and readiness checks.

But infrastructure is not proof of alpha. Before risking capital, the bot must
show in paper trading that it can:

- produce enough accepted simulated trades to analyze,
- avoid obvious validation and data-integrity failures,
- preserve positive expected value after deterministic checks,
- avoid overtrading illiquid or wide-spread markets,
- produce explainable decisions,
- maintain controlled simulated exposure,
- generate a reviewable audit trail.

For that reason, every step in this plan keeps:

```env
DRY_RUN=true
```

Live trading remains out of scope.

## Proposed 48-Hour Dry-Run Settings

The proposed change is an aggressive dry-run profile that increases the chance
of accepted simulated trades while preserving the core financial-integrity
rails.

Initial 48-hour dry-run configuration:

```env
DRY_RUN=true
MIN_CONFIDENCE=0.65
MIN_EV_THRESHOLD=0.02
MAX_SPREAD_PCT=0.015
PREFLIGHT_MAX_SPREAD_PCT=0.05
MAX_EXPOSURE_PCT=0.03
KELLY_FRACTION=0.25
MAX_ORDER_USDC=50
LLM_DAILY_CALL_LIMIT=20000
LLM_DAILY_TOKEN_LIMIT=100000000
LLM_DAILY_COST_LIMIT_USD=250
```

Reasoning:

- `MIN_CONFIDENCE=0.65` is below the observed positive-EV max confidence
  (`0.675`), so it should allow at least some simulated trades if similar
  candidates recur.
- `MIN_EV_THRESHOLD=0.02` remains unchanged, so the bot still needs a minimum
  2% edge before a simulated trade can route.
- `MAX_SPREAD_PCT=0.015` remains unchanged, so execution quality is not
  weakened.
- `PREFLIGHT_MAX_SPREAD_PCT=0.05` remains unchanged, so poor order books are
  still filtered before evaluation.
- `MAX_EXPOSURE_PCT=0.03`, `KELLY_FRACTION=0.25`, and `MAX_ORDER_USDC=50`
  remain unchanged, preserving conservative position sizing.
- LLM budget limits increase only to keep the 48-hour experiment from going
  dark prematurely.

## Six-Hour Adjustment Rule

At the 6-hour checkpoint, review accepted dry-run BUY/SELL count.

If accepted dry-run trades are `>= 5`:

- Keep `MIN_CONFIDENCE=0.65`.
- Do not loosen EV, spread, or exposure.
- Continue monitoring until the 12-hour checkpoint.

If accepted dry-run trades are `< 5`:

- Lower confidence once more:

```env
MIN_CONFIDENCE=0.60
```

- Keep `MIN_EV_THRESHOLD=0.02`.
- Keep `MAX_SPREAD_PCT=0.015`.
- Keep `PREFLIGHT_MAX_SPREAD_PCT=0.05`.
- Keep exposure and sizing controls unchanged.

Rationale: if the bot still cannot accept simulated trades at `0.65`, the next
least dangerous dry-run experiment is to lower confidence to `0.60` while still
requiring positive deterministic EV and tight spreads.

## Twelve-Hour Adjustment Rule

At the 12-hour checkpoint, review accepted dry-run BUY/SELL count again.

If accepted dry-run trades are `>= 5`:

- Continue the run with current settings.
- Do not further loosen thresholds.
- Focus analysis on trade quality rather than trade quantity.

If accepted dry-run trades are still `< 5`:

- Consider lowering EV threshold modestly:

```env
MIN_EV_THRESHOLD=0.015
```

- Do not lower `MAX_SPREAD_PCT`.
- Do not lower preflight quality.
- Do not increase position sizing.
- Do not disable reflection.
- Do not bypass Gatekeeper validation.

Rationale: lowering EV from 2.0% to 1.5% increases candidate volume while still
requiring a positive deterministic edge. This is riskier than lowering
confidence, so it should only happen after the 12-hour checkpoint if the bot is
still producing too few accepted dry-run trades to analyze.

## Controls That Must Not Be Weakened

The following controls must remain intact for the entire experiment:

- `DRY_RUN=true`.
- No live order signing.
- No live order broadcast.
- No wallet mutation.
- No bypass of `LLMEvaluationResponse`.
- No bypass of reflection.
- No bypass of market-data authority from system snapshots.
- No bypass of deterministic EV/spread/Kelly computation.
- No acceptance of crossed books.
- No acceptance of invalid or missing quotes.
- No weakening of repository/audit persistence.
- No direct raw SQL in agent runtime paths.
- No manual edits to persisted decision history.

Specific settings that should remain unchanged during the initial experiment:

```env
MAX_SPREAD_PCT=0.015
PREFLIGHT_MAX_SPREAD_PCT=0.05
MAX_EXPOSURE_PCT=0.03
KELLY_FRACTION=0.25
MAX_ORDER_USDC=50
REFLECTION_SOFT_FLAG_CONFIDENCE_FACTOR=0.90
```

These controls are what prevent the dry-run experiment from becoming a test of
recklessness rather than a test of signal quality.

## Metrics To Monitor During The 48-Hour Run

The run should be reviewed at minimum after 6 hours, 12 hours, 24 hours, and 48
hours.

Decision metrics:

- Total evaluations.
- Persisted decisions.
- BUY count.
- SELL count.
- HOLD count.
- Accepted dry-run trade count.
- Positive-EV candidates rejected by confidence.
- Positive-EV candidates rejected by spread.
- Positive-EV candidates rejected by TTR.
- Positive-EV candidates rejected by validation.

Quality metrics:

- Average EV of accepted dry-run trades.
- Median EV of accepted dry-run trades.
- Average confidence of accepted dry-run trades.
- Median confidence of accepted dry-run trades.
- Distribution of `reflection_verdict`.
- Frequency of `reflection.soft_flag_downgrade`.
- Top recurring reflection flags.
- Top recurring validation errors.

Market-quality metrics:

- Active market count.
- Preflight pass count.
- Preflight failures by reason.
- Spread distribution of accepted candidates.
- Best bid / best ask availability.
- Number of markets repeatedly quarantined.

Risk and exposure metrics:

- Simulated open position count.
- Simulated notional exposure.
- Exposure by market.
- Exposure by category.
- Largest simulated position.
- Position-size distribution.
- Simulated drawdown, if available.
- Simulated realized PnL, if exits occur.
- Unrealized PnL, if tracked.

Budget metrics:

- Primary LLM calls.
- Reflection LLM calls.
- Daily token usage.
- Estimated LLM spend.
- Hourly budget blocks.
- Daily budget blocks.
- Per-market hourly budget blocks.

Operational metrics:

- Container health.
- `/healthz`.
- `/readyz`.
- Database reachability.
- WebSocket connectivity.
- Log volume.
- Disk usage.
- Runtime exceptions.

## Decision Criteria After 48 Hours

The 48-hour result should be judged by evidence, not by whether the bot merely
produced more trades.

### Strong Result

A strong result means:

- Enough accepted dry-run trades exist to analyze.
- Accepted trades have clearly positive deterministic EV.
- Spread and preflight quality remain clean.
- Validation failures remain rare.
- Simulated exposure remains controlled.
- Decisions are explainable and not mostly reflection-repaired mistakes.
- Budget use is acceptable for the number of useful decisions produced.

If this happens, the next step should be deeper analysis:

- inspect accepted trade rationales,
- compare entry prices against later market movement,
- calculate simulated PnL if possible,
- look for market/category concentration,
- identify whether edge comes from repeatable conditions or random noise.

This still does **not** automatically approve live trading.

### Weak Result

A weak result means:

- Accepted trades remain near zero even after threshold adjustment.
- Positive-EV candidates still fail confidence or validation.
- Most accepted candidates are marginal or reflection-repaired.
- Preflight rejects dominate the market surface.
- LLM spend is high relative to useful signal volume.

If this happens, the next step should not be to keep loosening risk gates.
Instead, the next work should improve the fair-value model and market selection.

Possible next work:

- explicit discovery price-band filter for longshots,
- category-specific fair-value models,
- deterministic statistical features,
- cross-market comparison,
- better calibration of confidence,
- market-making or spread-capture strategy,
- backtest against historical Polymarket data.

### Unsafe Result

An unsafe result means:

- validation failures increase materially,
- accepted trades appear despite bad quotes,
- spread checks are frequently near the limit,
- confidence appears inflated without evidence,
- simulated exposure concentrates in one market/category,
- operational health degrades,
- budget is consumed without useful signal,
- any path appears to bypass `LLMEvaluationResponse`.

If this happens, revert to the prior conservative config and investigate before
continuing.

## Relationship To The Facebook Comment

The attached Facebook comment is directionally useful because it points out that
the hard part is not building a polished trading dashboard or wrapping an LLM
around market data. The hard part is the fair-value model.

The bot should not be treated as profitable just because it has:

- live APIs,
- LLM reasoning,
- a reflection auditor,
- a database,
- risk settings,
- a dashboard,
- or a 48-hour runtime.

Those are infrastructure. The real question is whether the bot can repeatedly
identify markets where:

```text
true probability - market-implied probability > fees + spread + slippage + risk margin
```

This plan uses aggressive dry-run settings to gather evidence for or against
that question. It does not assume the answer is yes.

## Explicit Non-Authorization For Live Trading

This document does not authorize:

- `DRY_RUN=false`.
- real capital deployment.
- live order signing.
- live order broadcasting.
- wallet credential changes.
- bypassing Gatekeeper validation.
- lowering spread quality for live execution.
- increasing exposure limits.
- increasing Kelly sizing.

Any future move toward live trading should require a separate plan, separate
approval, and a review of the 48-hour dry-run evidence.

## Summary Recommendation

The bot should take **more risk only inside dry-run**.

The recommended experiment is:

1. Keep all live-trading protections enabled.
2. Lower `MIN_CONFIDENCE` to `0.65`.
3. Run for 6 hours.
4. Lower to `0.60` only if accepted simulated trades remain too low.
5. Consider `MIN_EV_THRESHOLD=0.015` only after 12 hours if still starved.
6. Analyze the full 48-hour result before considering any further action.

This is the right direction because it tests whether the current all-HOLD
behavior is excessive conservatism or genuine lack of edge, without risking
real capital.

## Codex Review Opinion

Reviewer: Codex

Model used for reasoning: GPT-5-based Codex coding agent

Review date: 2026-06-01

### Overall Opinion

I agree with the central direction of this plan: the bot should become more
aggressive only inside `DRY_RUN=true`, and only for the purpose of collecting
enough simulated BUY/SELL decisions to judge whether the system has any
repeatable edge.

I do not think the current bot should move to live trading yet. The latest
evidence shows that the infrastructure is now functioning: market data is
flowing, the orchestrator is healthy, preflight is filtering bad books, the LLM
budget guard is no longer blocking evaluations, and the Gatekeeper is producing
auditable decisions. That is necessary progress, but it is not proof of alpha.

The current failure mode is no longer "the bot is broken and cannot evaluate."
The current failure mode is "the bot evaluates many markets, finds some
positive-EV candidates, but does not assign enough confidence for those
candidates to pass the trade gate." That is a materially different problem. It
is reasonable to test whether the confidence gate is overly conservative, but it
would be unsafe to treat that as proof that the bot should risk real capital.

My recommendation is to approve a revised version of this plan for a 48-hour
dry-run experiment, with stricter stop rules and a required review of the
high-EV/low-confidence candidates before any EV threshold is lowered.

### What I Agree With

I agree with keeping `DRY_RUN=true` as a hard boundary. The plan is correct to
state repeatedly that this is not approval for live trading, signing, order
broadcasting, wallet mutation, or any bypass of `LLMEvaluationResponse`.

I agree that the next useful experiment is not simply "wait longer" under the
current thresholds. The latest observed two-hour sample had hundreds of
positive-EV candidates but zero accepted dry-run trades. If that distribution is
stable, the bot can keep running for many more hours and still produce only
HOLD decisions. That would consume LLM budget without answering the question of
whether the bot can produce analyzable simulated trades.

I agree with lowering `MIN_CONFIDENCE` from `0.75` to `0.65` for dry-run. The
observed maximum confidence among positive-EV candidates was approximately
`0.675`, so `0.65` is a targeted test. It is not an arbitrary collapse of the
risk gate. It asks a specific question: if the confidence gate is moved just
below the observed positive-EV ceiling, do accepted simulated trades appear, and
are they any good?

I agree with preserving these controls:

```env
DRY_RUN=true
MIN_EV_THRESHOLD=0.02
MAX_SPREAD_PCT=0.015
PREFLIGHT_MAX_SPREAD_PCT=0.05
MAX_EXPOSURE_PCT=0.03
KELLY_FRACTION=0.25
MAX_ORDER_USDC=50
REFLECTION_SOFT_FLAG_CONFIDENCE_FACTOR=0.90
```

Those settings keep the experiment focused. Lowering confidence alone tests
whether the confidence gate is too strict. Weakening EV, spread, exposure, and
sizing at the same time would make the result much harder to interpret.

I also agree with the plan's framing that a 48-hour run is evidence gathering,
not validation of profitability. A successful dry-run only earns a deeper
review. It does not automatically justify live trading.

### Main Concern: Trade Count Is Not Enough

The checkpoint logic currently uses accepted dry-run trade count as the primary
trigger. That is useful but insufficient.

For example, if the 6-hour checkpoint produces five accepted simulated trades,
that does not automatically mean the experiment is healthy. Those five trades
could all be marginal, concentrated in one market, reflection-repaired, based on
unsupported probability estimates, or near the spread limit.

Conversely, if the 6-hour checkpoint produces fewer than five accepted simulated
trades, that does not automatically mean confidence should be lowered again. It
may mean the bot is correctly refusing weak opportunities. A low trade count is
only a problem if the rejected candidates look plausibly tradeable after review.

I would revise the 6-hour checkpoint so that lowering from `0.65` to `0.60`
requires all of the following:

- accepted dry-run trades are fewer than the target count,
- validation failures remain rare,
- no accepted candidate has bad quote quality,
- positive-EV rejected candidates cluster just below the confidence threshold,
- the top rejected candidates look coherent on manual inspection,
- LLM spend remains acceptable relative to useful signal generated.

This turns the checkpoint from "we did not get enough trades" into "we have
evidence that the confidence threshold is still the bottleneck."

### Main Concern: Lowering EV Is Riskier Than Lowering Confidence

The proposed 12-hour rule considers lowering `MIN_EV_THRESHOLD` from `0.02` to
`0.015`. I would treat this as a materially riskier move than lowering
confidence.

Confidence is an epistemic gate. Lowering it in dry-run helps test whether the
model is being too cautious. EV is closer to the financial edge itself. Lowering
the EV threshold allows thinner opportunities into the simulated execution path.
That may increase trade count, but it can also make the experiment less useful
by filling the dataset with marginal decisions that would likely be eaten by
fees, spread, slippage, stale data, or model error.

I would not lower `MIN_EV_THRESHOLD` automatically after 12 hours. I would only
consider it after reviewing the actual distribution of rejected candidates.
Before lowering EV, I would want to know:

- how many candidates had EV between `0.015` and `0.02`,
- what their confidence distribution looked like,
- whether their spreads were comfortably below `MAX_SPREAD_PCT`,
- whether they were concentrated in one market or category,
- whether reflection mostly approved them or repaired them,
- whether their p_true estimates were evidence-backed or unsupported,
- whether their later market movement would have supported the entry.

If the candidates between `0.015` and `0.02` are mostly clean and just narrowly
below threshold, a dry-run-only EV experiment is defensible. If they are mostly
unsupported or reflection-repaired, lowering EV would only create noise.

### High-EV / Low-Confidence Candidates Need Review First

The latest observed run included positive-EV candidates with low confidence,
including very high EV values. That pattern deserves manual review before any
thresholds are loosened further.

A very high EV paired with low confidence can mean a real opportunity, but it
can also mean:

- the LLM produced an aggressive p_true estimate without enough evidence,
- reflection repaired the candidate into HOLD,
- the market was an extreme-price or longshot artifact,
- the probability estimate was mathematically valid but economically fragile,
- the quote was technically valid but not reliably executable,
- the market had resolution or metadata risk not captured by the EV formula.

Before lowering `MIN_EV_THRESHOLD`, I would sample the top positive-EV HOLDs and
classify them into buckets:

- real candidate blocked mainly by confidence,
- unsupported p_true estimate,
- reflection-repaired candidate,
- bad market or metadata risk,
- longshot/extreme-price artifact,
- spread or liquidity concern,
- duplicate/repeated market pattern.

This review would make the threshold decision much more defensible.

### LLM Budget Needs Hard Stop Rules

The proposed budget caps are large enough to keep the experiment from going
dark, but they need stronger guardrails.

The plan proposes:

```env
LLM_DAILY_CALL_LIMIT=20000
LLM_DAILY_TOKEN_LIMIT=100000000
LLM_DAILY_COST_LIMIT_USD=250
```

That may be acceptable for a deliberate 48-hour dry-run, but the plan should
define explicit budget stop conditions. Without those, the bot can spend heavily
while producing little useful evidence.

I would add hard stops such as:

- stop or reduce cadence if estimated spend exceeds a configured checkpoint
  budget and accepted dry-run trades remain near zero,
- stop if cost per accepted simulated trade is unreasonably high,
- stop if token usage grows faster than expected because of repeated
  reflection repair or validation failure loops,
- stop if daily budget blocks appear despite the raised caps,
- stop if the run is producing mostly duplicate evaluations of the same market
  without new information.

For a 48-hour evidence-gathering run, the right budget question is not "can the
bot keep calling the LLM?" The right question is "how much useful trading signal
did each dollar of LLM spend buy?"

### Validation Failure Rules Should Be Explicit

The plan correctly tracks validation failures, but it should define what counts
as too many.

The current system has already seen residual validation failures after the
preflight mitigation. The most recent failures were not catastrophic, but they
matter because final candidate validation is the terminal Gatekeeper boundary.

I would add explicit stop or revert criteria:

- stop if final-candidate validation failures exceed 1% of evaluations over any
  one-hour window,
- stop if any validation failure indicates a possible Gatekeeper bypass,
- stop if invalid candidates repeatedly involve non-zero position sizing on
  HOLD decisions,
- stop if p_true boundary failures return at material frequency,
- stop if validation failures cluster around accepted BUY/SELL paths.

Dry-run does not excuse schema drift. If the LLM or reflection chain repeatedly
produces candidates that cannot satisfy `LLMEvaluationResponse`, the correct
response is to fix the evaluation path, not to weaken validation.

### Simulated Exposure Needs Concentration Limits

The plan preserves exposure settings, which is good. I would also add explicit
concentration monitoring.

If the lower confidence threshold produces accepted simulated trades, the run
should track whether those trades cluster in:

- one market,
- one event type,
- one category,
- one correlated narrative,
- one side of a binary pair,
- one stale or repeated signal source.

A bot that makes 20 dry-run trades all based on the same weak narrative has not
produced 20 independent pieces of evidence. It has produced one repeated bet.

The 48-hour analysis should distinguish independent opportunities from repeated
exposure to the same underlying thesis.

### Live Trading Should Remain Out Of Scope

Even if the 48-hour dry-run produces accepted simulated BUY/SELL decisions, I
would not treat that as enough to turn on live trading.

Before any live-trading plan, I would require:

- a full review of accepted simulated trades,
- realized or mark-to-market simulated PnL analysis where possible,
- evidence that the edge survived after spread and slippage assumptions,
- confirmation that accepted trades were not mostly reflection-repaired,
- confirmation that validation failures stayed rare and unrelated to accepted
  routes,
- review of market/category concentration,
- review of LLM cost per useful decision,
- explicit operator approval for live settings,
- a separate live-trading risk plan with much smaller capital limits than the
  dry-run sizing assumptions.

The phrase "try to make as much money as possible" should not mean "take as
many trades as possible." In this system, it should mean maximizing
risk-adjusted expected value while preserving enough auditability to understand
why money was made or lost.

### My Revised Recommendation

I would revise the experiment as follows.

Initial dry-run settings:

```env
DRY_RUN=true
MIN_CONFIDENCE=0.65
MIN_EV_THRESHOLD=0.02
MAX_SPREAD_PCT=0.015
PREFLIGHT_MAX_SPREAD_PCT=0.05
MAX_EXPOSURE_PCT=0.03
KELLY_FRACTION=0.25
MAX_ORDER_USDC=50
REFLECTION_SOFT_FLAG_CONFIDENCE_FACTOR=0.90
```

Budget settings can be raised, but only with explicit checkpoint limits:

```env
LLM_DAILY_CALL_LIMIT=20000
LLM_DAILY_TOKEN_LIMIT=100000000
LLM_DAILY_COST_LIMIT_USD=250
```

At 6 hours:

- count accepted simulated BUY/SELL decisions,
- review all accepted trades if the count is small,
- sample the top positive-EV rejected candidates,
- check validation failures,
- check reflection verdict distribution,
- check spread distribution,
- check market/category concentration,
- check LLM spend per useful signal.

Only lower `MIN_CONFIDENCE` to `0.60` if the evidence still points to confidence
as the primary bottleneck and candidate quality remains acceptable.

At 12 hours:

- do not automatically lower EV,
- first inspect the EV `0.015` to `0.02` candidate band,
- lower `MIN_EV_THRESHOLD` only if that band contains clean, coherent,
  non-concentrated candidates that are narrowly missing the current EV gate.

At 24 and 48 hours:

- judge the run by trade quality, not only trade count,
- compare accepted trade entries against later market movement,
- estimate simulated PnL where the data allows,
- identify repeatable patterns if any exist,
- decide whether the next step is threshold tuning, fair-value model work,
  market-selection work, or stopping the experiment.

### Final Position

I support a more aggressive 48-hour dry-run.

I do not support live trading from the current evidence.

I support lowering `MIN_CONFIDENCE` to `0.65` as the first experiment.

I would not lower `MIN_EV_THRESHOLD` until after manual review of the
high-EV/low-confidence and near-threshold candidates.

I would add hard budget, validation, and concentration stop rules before
starting the 48-hour run.

This plan is directionally correct because it tests whether the all-HOLD
behavior is excessive conservatism or genuine lack of edge. The important
revision is to make sure the experiment optimizes for useful evidence, not just
for producing more simulated trades.
