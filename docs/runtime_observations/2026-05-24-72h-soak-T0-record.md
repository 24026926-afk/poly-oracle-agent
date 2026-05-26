# 72h Dry-Run Soak — T0 Record

## Identity
- **RUN_ID:** `soak-2026-05-24-72h`
- **T0 (orchestrator.starting):** `2026-05-24T05:27:54Z`
- **T+72h target:** `2026-05-27T05:27:54Z`
- **GIT_HEAD:** `0143f25` (Merge PR #17 develop → main)
- **Branch:** `main`
- **DRY_RUN:** `true` (enforced)

## Server
- **Host:** `159.223.130.81` (`ubuntu-s-1vcpu-1gb-nyc1`, 1vCPU/1GB/24GB)
- **Container:** `poly-oracle-agent-orchestrator-1`
- **Image:** `poly-oracle-agent:latest` (rebuilt 2026-05-24T05:23:xxZ with main @ 0143f25)
- **SSH:** `root@159.223.130.81`

## Calibration at T0
- `LLM_PROVIDER=deepseek`
- `PREFLIGHT_MAX_SPREAD_PCT=0.99` (F1 reconciled)
- `LLM_HOURLY_CALL_LIMIT=240`, `LLM_DAILY_CALL_LIMIT=2000`, `LLM_DAILY_COST_LIMIT_USD=30`
- `LLM_MARKET_HOURLY_CALL_LIMIT=120`
- `ENABLE_OPERATIONAL_ALERTS=true`, `ENABLE_OPERATIONAL_EVENT_LEDGER=true`, `ENABLE_RUNTIME_AUDIT_ALERTS=true`
- `ENABLE_TELEGRAM_NOTIFIER=true`, `ENABLE_STARTUP_ALERT=true`
- `ENABLE_CIRCUIT_BREAKER=false` (intentional for dry-run)
- `TELEGRAM_CHAT_ID=8840799632`

## Baseline at T0
- **Active markets:** 30 (of 100 fetched, 85 eligible after preflight, 15 ttr_fail filtered)
- **DB size:** 448,458,752 bytes (≈428 MB — pre-existing data preserved on `poly_oracle_data` volume)
- **WS errors / reconnects:** 0 / 0
- **Evaluations T0+90s:** 9 (all HOLD, all IRAN, all reflection REJECTED — correct gatekeeper behavior on extreme-spread markets)
- **Operational events persisted T0+90s:** 124
- **Telegram startup alert:** dispatched at `2026-05-24T05:27:55.004278Z` (msg to chat 8840799632)
- **/healthz:** `{"status": "ok"}`
- **/readyz:** HTTP 200

## Checkpoint schedule (UTC)
| Checkpoint | T-stamp UTC | T-stamp local CDMX (UTC-6) |
|---|---|---|
| T+6h | 2026-05-24T11:27:54Z | 2026-05-24 05:27 |
| T+12h | 2026-05-24T17:27:54Z | 2026-05-24 11:27 |
| T+24h | 2026-05-25T05:27:54Z | 2026-05-24 23:27 |
| T+36h | 2026-05-25T17:27:54Z | 2026-05-25 11:27 |
| T+48h | 2026-05-26T05:27:54Z | 2026-05-25 23:27 |
| T+60h | 2026-05-26T17:27:54Z | 2026-05-26 11:27 |
| T+72h | 2026-05-27T05:27:54Z | 2026-05-26 23:27 |

## Operator protocol at each checkpoint
Operator returns to a Claude Code session at the target T-stamp. Claude runs:
1. SSH stats snapshot → `logs/stats-snapshot-T{n}h.txt`
2. Read latest `/opt/poly-oracle-agent/docs/operations/runtime_audits/latest.json` (NOTE: only present if WI-61 systemd timer was later installed; otherwise extract equivalent from container metrics + DB)
3. Diff vs prior checkpoint → flag anomalies per abort matrix in `2026-05-24-72h-run-checkpoint-template.md`
4. Append summary to `~/documents/integration_task/03_Daily/YYYY-MM-DD.md`

## Deferred (can install during the run without restart)
- **WI-61 `poly-oracle-runtime-audit.timer`** — systemd unit assumes `poly-oracle` host user + host Python venv; deferred to avoid restart at T0. Audit can be invoked manually on demand via `docker exec` workaround, or installed during run.
- **WI-62 `poly-oracle-server-review.timer`** — 24h cadence with 72h lookback; not needed during this single 72h window. Run `scripts/ops/aggregate_audits.py` + WI-62 skill manually at T+72h.

## Telegram chat
Operator should now receive (or has received) one INFO message from bot 8716594268 at chat 8840799632 with content "process_started" — confirmation that the bridge works.

## Rollback / abort
- **Abort orchestrator:** `ssh root@159.223.130.81 'cd /opt/poly-oracle-agent && docker compose down'`
- **Revert .env to pre-soak:** `ssh root@159.223.130.81 'cd /opt/poly-oracle-agent && cp .env.backup-2026-05-24-pre-soak .env && cp .env.backup-2026-05-24-pre-tg-sync .env'` (note: two backups exist; pre-soak is the truly original, pre-tg-sync is just after env-merge)
- **Revert code to pre-soak HEAD:** `git -C /opt/poly-oracle-agent checkout 1fea59d` (THEN docker compose build + up — but this would lose all WI-56→62 + F1-F6)
