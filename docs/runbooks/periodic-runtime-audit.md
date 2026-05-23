# Periodic Runtime Audit — WI-61 Runbook

## Overview

The Periodic Runtime Audit is a deterministic, read-only, out-of-process
auditor that probes the running paper-trading deployment and produces typed
JSON + markdown safety evidence artifacts. It alerts through Telegram on
degradation when enabled.

## Cadence

- **Audit timer:** 15-minute cadence (systemd `OnUnitActiveSec=15min`)
- **Review timer:** 60-minute cadence, **disabled by default**

## Exit Codes

| Code | Status | Meaning |
|------|--------|---------|
| 0 | HEALTHY | All mandatory probes and safety gates pass |
| 1 | DEGRADED | Warning findings exist without safety-gate failure |
| 2 | SAFETY_GATE_FAILED | Mandatory safety gate failed (e.g. `dry_run=false`, forbidden metric labels) |
| 3 | PROBE_ERROR | Config, artifact, repository, parse, timeout, or probe error |

## Probe Inputs

| Probe | Source | Mandatory |
|-------|--------|-----------|
| Health | `/healthz` HTTP endpoint | Yes |
| Readiness | `/readyz` HTTP endpoint (includes `dry_run` posture) | Yes |
| Metrics | `/metrics` Prometheus text exposition | Yes |
| Database | SQLite file existence and size | No |
| Docker | `docker compose ps` (read-only) | No |
| Log tail | Bounded byte/line cap inspection | No |
| Ledger | `OperationalEventRepository` bounded read-window | No |
| Decisions | `DecisionRepository` bounded read | No |
| Markets | `MarketRepository` bounded read | No |
| Positions | `PositionRepository` bounded read | No |
| Executions | `ExecutionRepository` bounded read | No |

## Safety Gates

1. **`dry_run=true`** — mandatory. Exit code 2 if `dry_run=false` or posture missing.
2. **Forbidden metric labels** — `condition_id`, `token_id`, `wallet_address`, `prompt_text`, `reasoning_text`, `secret`. Exit code 2 if detected.

## Artifacts

- **JSON:** `docs/operations/runtime_audits/runtime-audit-YYYYMMDDTHHMMSSZ.json`
- **Markdown:** `docs/operations/runtime_audits/runtime-audit-YYYYMMDDTHHMMSSZ.md`
- **Latest:** `docs/operations/runtime_audits/latest.json` and `latest.md` (atomic replacement)

## Telegram Opt-In

Set `ENABLE_RUNTIME_AUDIT_ALERTS=True` in `.env` to enable Telegram alerts
for exit code >= 1. Requires valid `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

## Reviewer Opt-In

The optional advisory LLM reviewer is **disabled by default**. To enable:

1. Set `RUNTIME_REVIEW_ENABLED=True` in `.env`
2. Set `MOONSHOT_API_KEY` in `.env`
3. Enable the reviewer timer: `systemctl enable --now poly-oracle-runtime-review.timer`

The reviewer uses direct `httpx` against `https://api.moonshot.ai/v1/chat/completions`.
It has no write authority beyond its own advisory markdown artifact under
`docs/operations/runtime_reviews/`.

## systemd Installation

```bash
# Copy unit files
sudo cp deploy/systemd/poly-oracle-runtime-audit.* /etc/systemd/system/
sudo cp deploy/systemd/poly-oracle-runtime-review.* /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable audit timer (enabled by default)
sudo systemctl enable --now poly-oracle-runtime-audit.timer

# Review timer is DISABLED by default — enable only if reviewer is configured
# sudo systemctl enable --now poly-oracle-runtime-review.timer
```

### Hardening

- `ProtectSystem=strict` — read-only filesystem except `ReadWritePaths`
- `ReadWritePaths` — constrained to artifact output directories only
- `NoNewPrivileges=yes` — prevents privilege escalation
- `PrivateTmp=yes` — isolated /tmp

## Troubleshooting

### Exit code 2 — Safety gate failed

Check the JSON artifact for `findings` with `finding_type: "SAFETY_GATE"`.
Common causes:
- `dry_run=false` in readiness response → verify `DRY_RUN=True` in `.env`
- Forbidden metric labels → check `/metrics` endpoint for high-cardinality labels

### Exit code 3 — Probe error

Check the JSON artifact for `findings` with `finding_type: "PROBE_ERROR"`.
Common causes:
- Health/readiness/metrics endpoints unreachable → verify the orchestrator is running
- Database file missing → verify `SQLITE_DB_PATH` or default `poly_oracle.db` exists

### Telegram alerts not sending

- Verify `ENABLE_RUNTIME_AUDIT_ALERTS=True` in `.env`
- Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set
- Check journal logs: `journalctl -u poly-oracle-runtime-audit.service`

## Advisory-Only Reviewer Guarantee

The optional LLM reviewer:
- Is **disabled by default** and must be explicitly enabled
- Has **no write authority** beyond its own advisory markdown artifact
- Has **no shell, Docker, git, environment, repository-write, signing, broadcasting, order-routing, or trading-authorization authority**
- Output is **advisory only** and **never gates trading**
- Uses direct `httpx` — no OpenCode, Hermes, OpenClaw, or OpenAI SDK dependency
