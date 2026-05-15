# Operational Event Ledger (WI-56) — Operator Runbook

## 1. Overview

The operational event ledger records runtime lifecycle, discovery, WebSocket,
readiness, LLM budget/cooldown/provider, decision, dry-run execution, circuit
breaker, alert, and recovery events in a durable, append-only SQLite table
(`operational_events`). Events are immutable after persistence. No update or
delete paths exist.

The ledger begins recording from Phase 16 (2026-05-14) onward. Historical
Docker log backfill is out of scope.

## 2. Configuration

Enable the ledger in `.env`:

```env
ENABLE_OPERATIONAL_EVENT_LEDGER=true
```

Optional tuning (defaults shown):

```env
EVENT_LEDGER_QUEUE_SIZE=1000
EVENT_LEDGER_BATCH_SIZE=50
EVENT_LEDGER_FLUSH_INTERVAL_SEC=10
EVENT_LEDGER_SHUTDOWN_FLUSH_TIMEOUT_SEC=30
EVENT_LEDGER_OVERFLOW_POLICY=drop_oldest
```

| Field | Default | Description |
|---|---|---|
| `ENABLE_OPERATIONAL_EVENT_LEDGER` | `false` | Master enable |
| `EVENT_LEDGER_QUEUE_SIZE` | `1000` | Maximum buffered events |
| `EVENT_LEDGER_BATCH_SIZE` | `50` | Events per flush batch |
| `EVENT_LEDGER_FLUSH_INTERVAL_SEC` | `10` | Seconds between flushes |
| `EVENT_LEDGER_SHUTDOWN_FLUSH_TIMEOUT_SEC` | `30` | Max seconds for final flush |
| `EVENT_LEDGER_OVERFLOW_POLICY` | `drop_oldest` | `drop_oldest`, `drop_newest`, or `drop_diagnostic` |

## 3. Event Types

### Lifecycle
- `START` — Orchestrator started
- `SHUTDOWN` — Orchestrator stopped
- `CONFIG_LOADED` — Config validated and loaded

### Market Discovery
- `MARKET_DISCOVERED` — Market found by discovery engine
- `MARKET_REJECTED` — Market failed eligibility
- `MARKET_QUARANTINE` — Market placed in cooldown quarantine

### WebSocket
- `WS_CONNECTED` — WebSocket connection established
- `WS_RECONNECT` — WebSocket reconnected after loss
- `WS_PONG_STALE` — PONG timeout detected

### Readiness
- `READY_STATE_CHANGED` — /readyz status changed

### LLM
- `LLM_CALL_STARTED` — Provider call initiated
- `LLM_CALL_BLOCKED` — Provider call blocked
- `BUDGET_BLOCK` — Hourly/daily/token/cost budget reached
- `COOLDOWN_BLOCK` — Market in cognitive cooldown
- `PROVIDER_FAILURE` — Provider call failed

### Decision
- `DECISION_ACCEPTED` — LLM evaluation approved
- `DECISION_SKIPPED` — LLM evaluation rejected/skipped

### Execution
- `EXECUTION_DRY_RUN` — Trade skipped (dry run)

### Circuit Breaker
- `CIRCUIT_BREAKER_OPEN` — Breaker tripped
- `CIRCUIT_BREAKER_CLOSED` — Breaker restored

### Alert
- `ALERT_SENT` — Operational alert dispatched

### Recovery
- `ERROR_RECOVERED` — Error handled and recovered

## 4. Querying the Ledger

Use the `OperationalEventRepository.read_window()` API or direct SQL:

```sql
-- Last 20 events
SELECT * FROM operational_events ORDER BY created_at_utc DESC LIMIT 20;

-- Events in the last hour
SELECT * FROM operational_events
WHERE created_at_utc >= datetime('now', '-1 hour')
ORDER BY created_at_utc DESC;

-- Critical events today
SELECT * FROM operational_events
WHERE severity = 'CRITICAL'
AND date(created_at_utc) = date('now')
ORDER BY created_at_utc DESC;

-- Event count by type
SELECT event_type, COUNT(*) as count
FROM operational_events
GROUP BY event_type
ORDER BY count DESC;
```

## 5. Queue Overflow Behavior

When the bounded queue reaches capacity:

| Policy | Behavior |
|---|---|
| `drop_oldest` | Oldest non-critical event dropped; newest accepted |
| `drop_newest` | Newest event rejected |
| `drop_diagnostic` | INFO diagnostic event dropped to make room |

**CRITICAL and ERROR events are never dropped during overflow when room can be
made by removing a diagnostic event.**

## 6. Safety Guarantees

- **Secret-free**: payloads are validated against forbidden patterns (API keys,
  wallet keys, Telegram tokens, condition IDs, token IDs, wallet addresses)
- **Immutable**: events cannot be updated or deleted after persistence
- **Non-blocking**: `publish()` returns immediately; persistence is batched
- **Bounded**: queue size, batch size, and flush timeout are config-capped
- **Fail-closed**: safety-critical audit failures mark readiness degraded
- **Low-cardinality**: all metric labels use stable enum values only

## 7. Troubleshooting

| Symptom | Likely Cause | Resolution |
|---|---|---|
| No events in ledger | `ENABLE_OPERATIONAL_EVENT_LEDGER=false` | Set to `true` in `.env` |
| Events dropped (overflow) | Queue capacity too small | Increase `EVENT_LEDGER_QUEUE_SIZE` |
| Flush failures | DB locked or disk full | Check SQLite DB health and disk space |
| Slow shutdown | Large queue to drain | Reduce `EVENT_LEDGER_SHUTDOWN_FLUSH_TIMEOUT_SEC` or batch size |
| `alembic upgrade head` fails | Migration not applied | Run `alembic upgrade head` |
