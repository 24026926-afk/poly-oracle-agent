# LLM Cost Guard Runbook

## Overview

The LLM Cost Guard (WI-52) prevents uncontrolled paid LLM usage by enforcing budget limits and per-market cognitive cooldowns before any primary evaluation or reflection call reaches an external LLM provider.

This is a **financial-integrity control**. A market loop, malformed provider response, or repeated non-actionable decision must not drain API credit indefinitely.

## Configuration

All settings are in `.env` or environment variables. Recommended **paper-trading** defaults:

| Variable | Paper-Trading Default | Purpose |
|---|---|---|
| `ENABLE_LLM_COST_GUARD` | `true` | Master enable |
| `LLM_HOURLY_CALL_LIMIT` | `240` | Max primary evaluation calls/hour |
| `LLM_REFLECTION_HOURLY_CALL_LIMIT` | `240` | Max reflection audit calls/hour |
| `LLM_DAILY_CALL_LIMIT` | `2000` | Max calls/day globally across primary and reflection |
| `LLM_DAILY_TOKEN_LIMIT` | `1000000` | Max rolling daily tokens globally; Run 3 sustained dry-run calibration uses `10000000` |
| `LLM_DAILY_COST_LIMIT_USD` | `30` | Max rolling daily spend in USD for sustained DeepSeek dry-runs |
| `LLM_MARKET_HOURLY_CALL_LIMIT` | `120` | Max calls/hour per market for Run 5 dry-run calibration |
| `LLM_REPEATED_HOLD_THRESHOLD` | `5` | HOLDs before cooldown |
| `LLM_REPEATED_INVALID_THRESHOLD` | `3` | Invalid outputs before cooldown |
| `LLM_MARKET_COOLDOWN_SECONDS` | `300` | Cooldown duration (5 min) |
| `LLM_FALLBACK_TOKENS_PER_CALL` | `4096` | Fallback when usage missing |
| `LLM_COST_PER_INPUT_TOKEN_USD` | `0.0000015` | Cost per input token |
| `LLM_COST_PER_OUTPUT_TOKEN_USD` | `0.000006` | Cost per output token |

Run 5 dry-run calibration also uses:

| Variable | Runtime value | Purpose |
|---|---:|---|
| `ENABLE_MARKET_DISCOVERY_PREFLIGHT` | `true` | Reject non-tradable order books before activation |
| `PREFLIGHT_MAX_SPREAD_PCT` | `0.90` | Reject extreme-spread books using existing `spread / best_ask` semantics while avoiding the all-blocking `0.80` calibration |
| `ENABLE_CATEGORY_EVALUATION_CADENCE` | `true` | Reduce evaluation spend on low-yield categories without removing signal coverage |
| `GROK_ELIGIBLE_EVALUATION_INTERVAL_SEC` | `30` | Preserve normal cadence for signal-rich categories |
| `NON_GROK_EVALUATION_INTERVAL_SEC` | `120` | Evaluate non-Grok categories at one quarter of the normal cadence |
| `CULTURE_EVALUATION_INTERVAL_SEC` | `600` | Keep CULTURE Grok signal coverage while preventing it from consuming the evaluation budget |
| `OPERATIONAL_EVENT_DIAGNOSTIC_THROTTLE_SEC` | `60` | Preserve required event types while throttling durable high-frequency diagnostics |

## How It Works

### Budget Enforcement

Before every primary evaluation and reflection call:

1. **Primary/reflection hourly call limits** — primary evaluations and reflection audits have separate hourly counters
2. **Daily call limit** — blocks if global daily count >= limit across both call types
3. **Daily token limit** — blocks if total tokens consumed >= limit
4. **Daily cost limit** — blocks if estimated spend >= limit
5. **Per-market hourly limit** — blocks if that market's hourly count >= limit

For WI-52, a limit set to `0` is a hard stop for that dimension, not unlimited.

All windows reset on their natural boundary (hourly = 1h, daily = 24h).

### Cognitive Cooldown

Per-market state tracks consecutive outcomes:

- **HOLD** → increments non-actionable counter
- **SKIP** → increments non-actionable counter
- **Invalid JSON / provider error** → increments invalid counter
- **BUY / SELL** → resets all counters (actionable outcome)

When a counter reaches its threshold, the market enters cooldown for `LLM_MARKET_COOLDOWN_SECONDS`. During cooldown, evaluation is skipped before any provider call.

### Fallback Usage Accounting

If the provider response lacks usage fields (or they are malformed), the guard uses `LLM_FALLBACK_TOKENS_PER_CALL` split 50/50 input/output and marks the record as `is_estimated=True`.

### Provider Errors

Timeouts and connection errors increment call counters but do **not** invent token usage. They do increment the cognitive breaker's invalid counter for the affected market.

### Runtime SQLite Contention

Local SQLite runtime connections use WAL mode with `busy_timeout=5000` and `synchronous=NORMAL` to reduce dashboard/read contention during dry-run observation. With WAL plus `synchronous=NORMAL`, a hard power loss can lose the most recent writes; use normal database backups before long observation windows when the audit trail must survive host failure.

## Recovery After Budget Exhaustion

1. **Hourly limit exhausted**: Wait for the hourly window to roll over (up to 60 min). No manual intervention needed.
2. **Daily limit exhausted**: Wait for the daily window to roll over (up to 24h). Alternatively, increase the limit in `.env` and restart.
3. **Daily cost limit exhausted**: Same as daily limit. Check `/metrics` for `poly_agent_llm_estimated_spend_usd_total` to see current spend.
4. **Market in cooldown**: Wait for cooldown expiry (default 5 min). The market automatically becomes eligible again.

## Disabling the Cost Guard

Set `ENABLE_LLM_COST_GUARD=false`. Current evaluation behavior is preserved. **Not recommended for paper-trading or live operation.**

## Metrics

| Metric | Type | Labels |
|---|---|---|
| `poly_agent_llm_calls_total` | Counter | `call_type` (primary/reflection) |
| `poly_agent_llm_budget_blocks_total` | Counter | `reason` (block reason enum) |
| `poly_agent_llm_cooldown_blocks_total` | Counter | none |
| `poly_agent_llm_tokens_total` | Counter | none |
| `poly_agent_llm_estimated_spend_usd_total` | Counter | none |
| `poly_agent_active_cooldown_count` | Gauge | none |

All metrics are low-cardinality. No prompt text, reasoning text, token IDs, condition IDs, wallet material, or API keys appear in labels.

`record_llm_budget_block` must remain a one-way counter update and must not call back into `LLMBudgetGuard`; the guard schedules that metric while holding its internal state lock.

## Logs

Budget and cooldown events are logged with bounded reason codes:

- `llm_budget_blocked` — reason, call_type, snapshot_id
- `market_in_cooldown` — market_key, reason, snapshot_id
- `llm_usage_recorded` — provider, model, token counts, estimated cost
- `llm_provider_error_recorded` — market_key
- `market_cooldown_activated` — market_key, reason, expires_in_seconds

No secrets, prompts, or high-cardinality identifiers are logged.

## Troubleshooting

### "All markets blocked but budget not exhausted"

Check per-market hourly limits. A single market hitting `LLM_MARKET_HOURLY_CALL_LIMIT` will block that market only. Other markets remain eligible.

### "Budget blocks but metrics show low spend"

Token/cost limits may be hit before call-count limits. Check `poly_agent_llm_tokens_total` and `poly_agent_llm_estimated_spend_usd_total`.

### "Cooldown never expires"

Verify `LLM_MARKET_COOLDOWN_SECONDS` is positive. System restart resets all in-memory cooldown state.

### "Usage records show is_estimated=true"

Provider response lacked usage fields. This is expected for some provider errors. The fallback is conservative and marks the record as estimated.
