# Incident Replay CLI (WI-58)

Operator runbook for `scripts/ops/replay.py` — a read-only command that
reconstructs a bounded UTC time window from the WI-56 operational event
ledger and prints a chronological, secret-safe replay using the WI-57
deterministic narrative layer.

The CLI is **read-only**. It never appends, mutates, or deletes
operational events. It never calls an LLM, signs a transaction, or
broadcasts an order. `DRY_RUN=false` semantics remain untouched.

---

## When to use

* After any incident in DigitalOcean dry-run paper trading where stdout
  or Docker logs are insufficient to reconstruct behavior.
* Before opening a Telegram operator alert post-mortem.
* As input to WI-59 dashboard activity feed and WI-60 daily operations
  digest (the same typed schemas back both surfaces).

## Quick reference

```bash
# Full hour reconstruction
python -m scripts.ops.replay \
    --from 2026-05-15T00:00:00Z \
    --to   2026-05-15T01:00:00Z

# Decision-skip post-mortem
python -m scripts.ops.replay \
    --from 2026-05-15T00:00:00Z \
    --to   2026-05-15T01:00:00Z \
    --event-type DECISION_SKIPPED

# Provider failure window
python -m scripts.ops.replay \
    --from 2026-05-15T00:00:00Z \
    --to   2026-05-15T01:00:00Z \
    --source EVALUATION \
    --event-type PROVIDER_FAILURE
```

## Inputs

| Flag | Required | Description |
| --- | --- | --- |
| `--from` | yes | Window start, ISO-8601 UTC. Trailing `Z` is accepted. |
| `--to` | yes | Window end, ISO-8601 UTC. Trailing `Z` is accepted. |
| `--severity` | no | Typed severity filter; repeatable. |
| `--source` | no | Typed source-component filter; repeatable. |
| `--event-type` | no | Typed event-type filter; repeatable. |
| `--reason-code` | no | Typed reason-code filter; repeatable. |
| `--limit` | no | Maximum lines (1..1000). Default: 1000. |

All filter values must match a typed enum. Unknown values are rejected
with the full list of allowed enum members; the CLI exits non-zero and
prints no event data.

## Filters

* Filters are independent (any one may be passed alone).
* Filters combine by **intersection** — events must match all supplied
  categories.
* Repeating a flag within a category ORs the values.
* Comma-separated values are also accepted: `--severity WARNING,ERROR`.

## Output

The CLI prints:

1. A header showing the window, active filters, status, and any
   typed failure reason or note.
2. One line per event in **chronological order** by `created_at_utc`.
   Tied timestamps fall back to a stable persisted event id, so the
   same window + filters always produce identical output.
3. A typed summary footer with bounded counts:
   * `total_events`
   * `warnings`, `errors`
   * `markets_seen` — bounded count of typed market-discovery /
     market-rejection / market-quarantine events. **Never** prints
     token IDs, condition IDs, raw market IDs, or market names.
   * `decisions_by_action` — `BUY` / `HOLD` / `SKIP` only, derived from
     typed reason codes.
   * `skips_by_reason` — typed `OperationalEventReasonCode` keys only.
   * `llm_calls`, `budget_blocks`, `cooldown_blocks`,
     `provider_failures`, `readiness_changes`.

The output is scanned for forbidden secret / high-cardinality content
at the schema boundary before printing. Any line that would expose a
private key, wallet address, API key, Telegram token, token ID,
condition ID, raw prompt, raw reasoning, raw provider response, or raw
exception text is replaced with the WI-57 conservative generic
narrative or dropped entirely.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | `SUCCESS`, `EMPTY_WINDOW`, or `TRUNCATED` (valid replay). |
| `2` | `INVALID_WINDOW`, `INVALID_TIMESTAMP`, or `INVALID_FILTER`. |
| `3` | `REPOSITORY_FAILURE` or `DATABASE_UNAVAILABLE`. |

`EMPTY_WINDOW` is **not** a failure: a valid window with no matching
events exits 0 and prints a zero-event report so operators can confirm
"nothing happened" rather than "the tool broke."

`TRUNCATED` indicates the result exceeded the configured `--limit`.
Narrow the window or the filter set and re-run.

## Edge cases

* **`--from` is later than `--to`** → `INVALID_WINDOW`, exit `2`.
* **Naive timestamp (no timezone)** → `INVALID_TIMESTAMP`, exit `2`.
* **Non-UTC offset** → normalized to UTC before query.
* **Malformed timestamp** → `INVALID_TIMESTAMP`, exit `2`.
* **Unknown filter value** → `INVALID_FILTER`, exit `2`, with the full
  list of allowed enum members in the error note.
* **Empty window** → typed zero-event report, exit `0`.
* **Filters exclude all events** → typed zero-event report that records
  the active filters, exit `0`.
* **Contradictory filters** → typed zero-event report, exit `0`.
* **Repository default ordering is descending** → replay re-sorts into
  chronological order before rendering.
* **Two events share the same `created_at_utc`** → ordered by stable
  persisted `id` for deterministic output.
* **Malformed persisted payload JSON** → typed WI-57 fallback line; the
  rest of the window still renders.
* **Persisted payload still contains forbidden content** → typed WI-57
  redacted line; no unsafe text is printed.
* **`operational_events` table is missing on an older deployment** →
  `DATABASE_UNAVAILABLE` with `MISSING_EVENT_TABLE` failure reason,
  exit `3`.
* **Database is unreachable** → `DATABASE_UNAVAILABLE` with
  `DATABASE_UNREACHABLE` failure reason, exit `3`. The error note is a
  bounded low-cardinality message; raw exception text, SQL, and
  connection strings are never printed.

## Safety guarantees

* All event reads go through `OperationalEventRepository.read_window`.
  No raw SQLAlchemy sessions escape the replay service.
* `OperationalEventRepository` remains append/read-only. WI-58 does not
  add `update`, `delete`, or `backfill` methods.
* The CLI never imports an LLM client, the execution router, the
  transaction signer, or any live wallet mutation path.
* No `Base.metadata.create_all()` is invoked from the runtime or CLI
  paths.
* All Decimal-bearing fields remain `Decimal` at the schema boundary;
  no replay code performs trading, sizing, EV, Kelly, PnL, exposure,
  or provider-cost calculations.
* Metrics and structlog labels emitted by replay (if any) are
  low-cardinality only.

## Incident-response workflow

1. **Identify the window** from the Telegram alert, paging tool, or
   on-call observation. Convert the timestamps to UTC.
2. **Run replay with no filters** first to see the full sequence of
   typed events:

   ```bash
   python -m scripts.ops.replay --from <utc_start> --to <utc_end>
   ```

3. **Narrow by severity** to focus on warnings/errors:

   ```bash
   python -m scripts.ops.replay --from <utc_start> --to <utc_end> \
       --severity WARNING --severity ERROR --severity CRITICAL
   ```

4. **Narrow by source** (`EVALUATION`, `EXECUTION`, `INGESTION`,
   `ORCHESTRATOR`, etc.) once the most likely subsystem is known.
5. **Narrow by event type / reason code** to confirm the specific
   pattern (e.g., `--event-type PROVIDER_FAILURE`,
   `--reason-code BUDGET_DAILY`).
6. **Cite the replay output** verbatim in the incident retrospective.
   The output is deterministic and secret-safe, so it can be pasted
   directly into post-mortems, runbooks, or operator chats.

## Out of scope

* WI-58 does not enable live trading, change `DRY_RUN`, sign, or
  broadcast.
* WI-58 does not build the dashboard activity feed (WI-59) or the
  daily operations digest (WI-60); those reuse the same typed replay
  schemas.
* WI-58 adds no new external dependencies and no new database tables
  or migrations.
