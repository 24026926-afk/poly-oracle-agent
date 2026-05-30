# 72-Hour Dry-Run Checkpoint Template

**Run start (T0):** TBD (recorded during P6 clean restart on Droplet)
**Target end (T+72h):** TBD
**Branch deployed:** `main` @ TBD (post PR #15 → develop → main merges)
**Calibration baseline:** F1–F6 hotfixes (PREFLIGHT_MAX_SPREAD_PCT=0.99, WS snapshot throttle 25bps/2.0s, cognitive_cooldown_block_rate surfaced, market_activated dedup, degenerate-quote + book-warmup burst markers).

---

## Cadence

| Checkpoint | Wall-clock target | Purpose |
|---|---|---|
| T+6h | first daylight check | confirm cold-start stabilized, no early degradation |
| T+12h | overnight burn-in | first full sleep cycle through the bot |
| T+24h | 1-day mark | first full daily-digest comparison vs WI-60 baseline |
| T+36h | mid-window | early indicator of any slow leak |
| T+48h | 2-day mark | second daily-digest, confirms 24h-period stability |
| T+60h | late window | last chance to abort gracefully before the end |
| T+72h | final | full aggregate_audits + WI-62 server-runtime-review narrative |

Operator returns to this conversation at each checkpoint. Claude runs the checkpoint script below.

---

## Checkpoint script (Claude runs each visit)

For each checkpoint Tn:

1. **Latest WI-61 audit** — read most recent `docs/operations/runtime_audits/latest.json` from Droplet via `ssh $DROPLET 'cat /home/deploy/poly-oracle-agent/docs/operations/runtime_audits/latest.json'`. Extract: `status`, `exit_code`, `findings[]`, `summary.cognitive_cooldown_block_rate`, `summary.markets_active`, `summary.decisions_window`.
2. **Stats snapshot** — write `logs/stats-snapshot-T{n}h.txt` with:
   - DB size + per-table row counts (`SELECT COUNT(*) FROM market_snapshots; ...`)
   - Orchestrator RSS / CPU (via `ps aux | grep orchestrator`)
   - Log file size + lines added since last checkpoint
   - Telegram alerts fired count (read from operational_event ledger, type=TELEGRAM_DISPATCH)
   - WS reconnect count (from `/metrics`)
3. **Delta vs prior checkpoint** — diff against `logs/stats-snapshot-T{n-6}h.txt`. Flag any of:
   - DB growth > 30 MB/hr (F4 throttle regression)
   - RSS growth > 1 MB/min (leak indicator)
   - WS reconnects in last 6h > 5
   - `cognitive_cooldown_block_rate` change ±15% from prior
   - Any new error events in operational_event ledger
   - `/readyz` not READY
4. **HIGH/MEDIUM findings** — if any, write incident note to `docs/runtime_observations/2026-05-2X-72h-checkpoint-T{n}h-incident.md` matching the canonical 12-section format. Optionally abort run via `ssh $DROPLET 'docker compose down'`.
5. **Daily note append** — short summary to `~/documents/integration_task/03_Daily/YYYY-MM-DD.md` under "Session Summary".

---

## Stats snapshot fields (record exactly)

```
TIMESTAMP_UTC: <ISO-8601>
T_HOURS_SINCE_START: <decimal>
DB_FILE_BYTES: <int>
DB_FILE_BYTES_DELTA: <int>  # vs prior checkpoint
ROWS_MARKET_SNAPSHOTS: <int>
ROWS_AGENT_DECISION_LOGS: <int>
ROWS_OPERATIONAL_EVENTS: <int>
ROWS_EXECUTIONS: <int>
ROWS_POSITIONS: <int>
ORCHESTRATOR_PID: <int>
ORCHESTRATOR_RSS_MB: <decimal>
ORCHESTRATOR_RSS_MB_DELTA: <decimal>
ORCHESTRATOR_CPU_PCT_AVG_6H: <decimal>
LOG_FILE_BYTES: <int>
LOG_FILE_BYTES_DELTA: <int>
LOG_LINES_LAST_6H: <int>
TELEGRAM_ALERTS_FIRED_LAST_6H: <int>
WS_RECONNECTS_LAST_6H: <int>
READYZ_STATUS: <READY|NOT_READY|DEGRADED>
COGNITIVE_COOLDOWN_BLOCK_RATE_LATEST: <decimal 0-1>
LAST_AUDIT_EXIT_CODE: <0|1|2|3>
LAST_AUDIT_FINDINGS_COUNT: <int>
NEW_HIGH_MEDIUM_FINDINGS: <count>
ABORT_RECOMMENDATION: <NONE|CONTINUE_WITH_ALERT|ABORT>
```

---

## Abort decision matrix

Trigger `ABORT` if any of:
- `READYZ_STATUS != READY` for >2 consecutive checkpoints
- DB growth >30 MB/hr sustained over 12h
- RSS growth >1 MB/min sustained over 12h
- Any safety-gate finding (audit exit code 2)
- Probe error sustained over 2 checkpoints (audit exit code 3)
- `cognitive_cooldown_block_rate` >0.85 sustained over 12h (cooldown saturation)

Trigger `CONTINUE_WITH_ALERT` if any of:
- Single missed checkpoint (no Telegram, no audit) — investigate but do not abort
- One-time WS reconnect burst (>5 in 6h, returns to <2/6h on next checkpoint)
- One-time degenerate-quote first-detection on new market (expected per F5)

Otherwise `NONE` (healthy continuation).

---

## Communication

- **Telegram**: real-time degradation alerts (WI-50 bridge) — operator-side, asynchronous.
- **This conversation**: 6h checkpoints, planned + ad-hoc incident response.
- **Daily note**: append session summary at each checkpoint.

---

## Files generated during the run

| Path | Owner | Update cadence |
|---|---|---|
| `logs/orchestrator-run.log` (on Droplet) | orchestrator | continuous |
| `docs/operations/runtime_audits/latest.{json,md}` (on Droplet) | WI-61 timer | 15 min |
| `docs/operations/runtime_audits/<timestamp>.{json,md}` (on Droplet) | WI-61 timer | 15 min |
| `logs/stats-snapshot-T{n}h.txt` (local) | Claude | 6h |
| `docs/runtime_observations/2026-05-2X-72h-checkpoint-T{n}h-{incident,ok}.md` (local) | Claude | 6h |
| `docs/runtime_observations/2026-05-2X-72h-soak-final-report.md` (local) | Claude | T+72h |
