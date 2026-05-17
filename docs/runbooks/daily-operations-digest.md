# Daily Operations Digest — Operator Runbook

## What This Is

WI-60 ships a deterministic, repository-backed daily digest generator that
turns the operational event ledger (WI-56), the deterministic narrative
layer (WI-57), and the position repository into a one-page operator
summary at `03_Daily/YYYY-MM-DD-bot.md`.

The digest never overwrites your manual coding daily notes at
`03_Daily/YYYY-MM-DD.md`. It is intended for autonomous daily review of
the dry-run server and for non-technical operator hand-off.

## What It Reports

Each digest section is derived strictly from typed event evidence:

- **Run summary** — start / stop / uptime / partial-run flag,
  active provider, latest readiness, dry-run state, market counts.
- **Decisions** — `accepted_buy`, `accepted_hold`, and per-reason skip
  counts (`low_conf`, `low_ev`, `high_spread`, `exposure`, `ttr`).
- **LLM guard** — call count, budget blocks, cooldown blocks, provider
  failures, and Decimal-only estimated spend.
- **Paper PnL** — repository-backed realized PnL, gas + fees, closed
  position count, open position count. Missing data renders as
  `(unavailable)` — never fabricated zero.
- **Top operational events** — bounded list, deterministic ordering
  (severity → timestamp → id).
- **Unresolved warnings / errors** — typed events whose category has
  not been superseded by a typed recovery event.
- **Recommended operator checks** — deterministic next actions based
  on typed run state, LLM state, and unresolved events.
- **Telegram delivery footer** — disabled / sent / skipped / failed
  with a typed reason on non-success.

## Safety Guarantees

- The digest service **never** appends, updates, deletes, or backfills
  operational events.
- The CLI **never** writes outside `03_Daily/YYYY-MM-DD-bot.md` after
  path validation.
- The manual coding note at `03_Daily/YYYY-MM-DD.md` is never created,
  overwritten, truncated, appended, renamed, or deleted.
- Output is scanned for secret / high-cardinality content at the
  Pydantic schema boundary AND on the final rendered text before any
  disk write or Telegram send.
- All money math is `Decimal`-native; raw `float` is rejected.
- No LLM call, signing, broadcasting, or live wallet path is ever
  invoked from digest code.

## Manual Generation

```bash
.venv/bin/python -m scripts.ops.generate_daily_ops_digest \
    --date 2026-05-15
```

If `--date` is omitted, the CLI defaults to today's UTC date. The
output path is derived as `03_Daily/YYYY-MM-DD-bot.md` under the
configured `--daily-notes-dir`.

### Explicit output path

```bash
.venv/bin/python -m scripts.ops.generate_daily_ops_digest \
    --date 2026-05-15 \
    --output 03_Daily/2026-05-15-bot.md \
    --daily-notes-dir 03_Daily
```

If `--output` is supplied it must:

- end with `YYYY-MM-DD-bot.md` matching the requested `--date`;
- be a direct child of the configured `--daily-notes-dir`
  (a leading `<daily-notes-basename>/` prefix on a relative path
  is accepted and stripped, so the documented form above is valid);
- not coincide with the manual coding note pattern `YYYY-MM-DD.md`.

Otherwise the CLI exits non-zero with a typed `PATH_FAILURE` reason
and no file is written.

### Explicit UTC window (optional)

```bash
.venv/bin/python -m scripts.ops.generate_daily_ops_digest \
    --date 2026-05-15 \
    --from-utc 2026-05-15T06:00:00Z \
    --to-utc   2026-05-15T18:00:00Z
```

`--from-utc` and `--to-utc` must:

- be supplied together;
- be ISO-8601 UTC timestamps (trailing `Z` or explicit `+00:00`);
- satisfy `from < to`;
- both fall on the same UTC calendar day as `--date`.

Any violation returns exit code `2` (invalid input) without writing a
file. Omitting these flags is the common case and produces the full UTC
calendar-day window implied by `--date`.

### Telegram delivery (optional)

```bash
.venv/bin/python -m scripts.ops.generate_daily_ops_digest \
    --date 2026-05-15 \
    --enable-telegram
```

`--enable-telegram` only requests delivery. The notifier is wired by the
caller at the CLI entry point. The digest prefers the typed
`try_send_execution_event(...) -> bool` interface when the wired
notifier exposes it (the production `TelegramNotifier` does), so a
swallowed transport failure is reported as `failed` rather than silently
as `sent`. Telegram failures never corrupt or delete a successfully
written file digest; the failure surfaces in the digest file footer and
in the CLI's printed `telegram_status`.

## Scheduling

### Cron (per-host)

Generate yesterday's digest at 00:30 UTC daily, log to a per-day file:

```cron
30 0 * * * cd /srv/poly-oracle-agent && \
    .venv/bin/python -m scripts.ops.generate_daily_ops_digest \
    --date "$(date -u -d yesterday +\%Y-\%m-\%d)" \
    >> /var/log/poly-oracle/daily-digest.log 2>&1
```

### systemd timer

```ini
# /etc/systemd/system/poly-oracle-daily-digest.service
[Unit]
Description=poly-oracle-agent daily ops digest
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/srv/poly-oracle-agent
ExecStart=/srv/poly-oracle-agent/.venv/bin/python -m \
    scripts.ops.generate_daily_ops_digest \
    --date %i
User=poly-oracle
```

```ini
# /etc/systemd/system/poly-oracle-daily-digest.timer
[Unit]
Description=Run poly-oracle daily ops digest

[Timer]
OnCalendar=*-*-* 00:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

Activate with:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now poly-oracle-daily-digest.timer
```

## Exit Codes

| Code | Meaning                                                                |
|------|------------------------------------------------------------------------|
| `0`  | Digest written successfully (`SUCCESS` or `EMPTY_WINDOW`).             |
| `2`  | Invalid CLI input (date, output path, manual-note collision).          |
| `3`  | Repository or database failure (`MISSING_TABLE`, `DATABASE_UNAVAILABLE`, `REPOSITORY_FAILURE`). |
| `4`  | Forbidden content blocked digest generation.                           |

## Handling Common Outcomes

### No-run / empty days

Status `EMPTY_WINDOW`. The bot digest is still written with an explicit
operator note advising to confirm the bot was scheduled to run. The
manual note is not touched.

### Partial runs

Status `SUCCESS`, `run_summary.run_status = "partial"`. The digest
records the observed `start_utc` but leaves `stop_utc` and
`uptime_seconds` unavailable. An operator check is appended recommending
the operator confirm whether the process is still running.

### Missing `operational_events` table

Status `MISSING_TABLE`. No file is created. Confirm the deployment
provisioned the WI-56 ledger via Alembic migrations.

### Database unreachable

Status `DATABASE_UNAVAILABLE`. No file is created. Confirm the SQLite
volume mount, file permissions, and that the dry-run server has not
been moved to a new path.

### Forbidden content blocked the write

Status `FORBIDDEN_CONTENT`. The defense-in-depth secret scan rejected
the rendered markdown before disk write. Inspect the persisted event
that introduced the offending payload via `scripts/ops/replay.py` and
report the source so the upstream payload schema can be tightened.

### Telegram delivery failed

The file digest still wrote. `telegram_result.status == "failed"` with
a typed `failure_reason`. Re-running the digest is safe — operational
events are immutable; the regenerated file content is identical.

## Troubleshooting

- **Manual note got "corrupted"** — the digest service never writes to
  `YYYY-MM-DD.md`. If you observe a change to that file it did not come
  from the digest path; check version control.
- **Re-running produces different output** — operational events are
  immutable. Output drift means new events were appended between runs
  (or the position table changed). Confirm with `replay.py`.
- **Decimal values look weird** — the SQLite driver round-trips
  `Numeric(38, 18)` columns; trailing zeros are expected. The CLI
  prints values with a fixed precision; the typed `report.pnl_summary`
  in tests retains full precision.
- **High-cardinality identifiers appear in the digest** — they should
  never; the schema secret scan rejects them at the Pydantic boundary
  and again at the rendered-text boundary. If you find a leak, do not
  patch the digest — open an incident against the upstream payload
  schema.

## See Also

- `docs/runbooks/operational-event-ledger.md` (WI-56)
- `docs/runbooks/incident-replay.md` (WI-58)
- `docs/PRD-v16.0.md` (Phase 16 — Operator Clarity and Runtime Audit Trail)
- `docs/deliverables/business_logic/business_logic_WI-60-daily-operations-digest.md`
- `docs/deliverables/implementation_prompts/prompt_WI-60-daily-operations-digest.md`
