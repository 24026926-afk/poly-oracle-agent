# Runbook: Market Eligibility, Evaluation Deduplication, and Queue Backpressure

## Overview

WI-53 adds three safety layers between market discovery and LLM evaluation:

1. **Preflight** — validates order book health before market activation
2. **Dedupe** — suppresses repeated unchanged contexts for the same market
3. **Backpressure** — bounds the prompt queue with coalescing or stale-drop

## Configuration

| Env Var | Default | Description |
|---|---|---|
| `ENABLE_MARKET_DISCOVERY_PREFLIGHT` | `false` | Enable preflight checks |
| `PREFLIGHT_TIMEOUT_SECONDS` | `5` | Timeout per candidate |
| `PREFLIGHT_MAX_CANDIDATES` | `10` | Max candidates per cycle |
| `PREFLIGHT_QUARANTINE_DURATION_SECONDS` | `300` | Quarantine duration |
| `PREFLIGHT_MAX_SPREAD_PCT` | `0.05` | Max spread (5%) |
| `ENABLE_MARKET_EVALUATION_DEDUPE` | `false` | Enable dedupe |
| `DEDUPE_MIN_EVALUATION_INTERVAL_SEC` | `30` | Min interval between evals |
| `DEDUPE_MIDPOINT_DELTA` | `0.01` | Min midpoint change (1pp) |
| `DEDUPE_SPREAD_DELTA` | `0.005` | Min spread change (0.5pp) |
| `PROMPT_QUEUE_MAX_SIZE` | `50` | Max queue depth |
| `PROMPT_QUEUE_COALESCING_MODE` | `true` | Coalesce by market when full |

## Preflight Skip Reasons

| Reason | Meaning | Recovery |
|---|---|---|
| `MISSING_TOKEN_CONTEXT` | No YES token ID | Fix market metadata in Gamma |
| `ORDER_BOOK_UNAVAILABLE` | CLOB fetch failed | Check CLOB connectivity |
| `NON_POSITIVE_QUOTE` | Bid or ask ≤ 0 | Market may be settling |
| `CROSSED_BOOK` | Bid ≥ Ask | Temporary book anomaly |
| `SPREAD_TOO_WIDE` | Spread > threshold | Adjust threshold or wait |
| `PREFLIGHT_TIMEOUT` | Lookup timed out | Check network latency |

## Quarantine

- Markets with ≥ 3 consecutive preflight failures are quarantined.
- Quarantine duration defaults to 300 seconds.
- After expiry, the market is eligible for preflight again.
- Quarantine is per-market — does not affect unrelated markets.
- **System restart clears quarantine** (in-memory only).

## Dedupe Behavior

- When enabled, unchanged midpoint + spread within the minimum interval suppresses evaluation.
- Material midpoint or spread movement always emits.
- Dedupe is per-market — Market A can be suppressed while Market B emits.
- **System restart clears dedupe fingerprints** (in-memory only).

## Queue Backpressure

- When the prompt queue is full:
  - **Coalescing mode (default):** Replace the stale payload for the same market.
  - **Non-coalescing mode:** Drop the new payload with a typed reason.
- Queue depth is exposed via the `poly_agent_evaluation_queue_depth` gauge.

## Troubleshooting

### No markets activating

1. Check preflight logs for `market_discovery.preflight_failed` with skip reason.
2. If all markets fail with `ORDER_BOOK_UNAVAILABLE`, verify CLOB connectivity.
3. If `SPREAD_TOO_WIDE`, consider increasing `PREFLIGHT_MAX_SPREAD_PCT`.
4. Check quarantine: markets may be quarantined from prior failures.

### Repeated LLM evaluations for same market

1. Enable dedupe: `ENABLE_MARKET_EVALUATION_DEDUPE=true`.
2. Adjust `DEDUPE_MIDPOINT_DELTA` and `DEDUPE_SPREAD_DELTA` to match market volatility.
3. Check `poly_agent_deduped_contexts_total` metric.

### Prompt queue growing unbounded

1. Verify `PROMPT_QUEUE_MAX_SIZE` is set (default: 50).
2. Check `poly_agent_evaluation_queue_depth` gauge.
3. If coalescing is disabled, enable it: `PROMPT_QUEUE_COALESCING_MODE=true`.
4. Check `poly_agent_dropped_stale_contexts_total` and `poly_agent_coalesced_contexts_total`.

### Metrics

| Metric | Type | Labels |
|---|---|---|
| `poly_agent_preflight_pass_total` | Counter | — |
| `poly_agent_preflight_fail_total` | Counter | `reason` |
| `poly_agent_quarantine_total` | Counter | `reason` |
| `poly_agent_emitted_contexts_total` | Counter | — |
| `poly_agent_deduped_contexts_total` | Counter | — |
| `poly_agent_dropped_stale_contexts_total` | Counter | — |
| `poly_agent_coalesced_contexts_total` | Counter | — |
| `poly_agent_evaluation_queue_depth` | Gauge | — |
