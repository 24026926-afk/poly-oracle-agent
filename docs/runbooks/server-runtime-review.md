# Server Runtime Review — WI-62 Runbook

## Overview

The Server Runtime Review is an autonomous, headless skill that aggregates
72 hours of WI-61 periodic audit artifacts using deterministic Python
arithmetic, then produces a 12-section observation report and a conditional
14-section fix plan. It runs unattended on the server via systemd timer.

## Architecture

```
WI-61 audit timer (15 min)
    → docs/operations/runtime_audits/runtime-audit-*.json (~288 per 72h)
        ↓
aggregate_audits.py (Python, Decimal-safe)
    → JSON summary (50-100 lines)
        ↓
opencode -p "/server-runtime-review" -q (headless LLM)
    → docs/runtime_observations/{YYYY-MM-DD}-server-runtime-session.md
    → docs/runtime_observations/{YYYY-MM-DD}-server-fix-plan.md (conditional)
```

## Cadence

- **Review timer:** 24-hour cadence with 72-hour lookback window.
- Each review overlaps the previous two, providing rolling coverage with no blind spots.
- `Persistent=true` ensures missed runs are caught up on next boot.

## Prerequisites

### 1. Install opencode CLI

OpenCode is a Go binary. Install from https://opencode.ai or via Go:

```bash
go install github.com/opencode-ai/opencode@latest
# Or download the release binary and place in /usr/local/bin/
```

Verify: `opencode --version`

### 2. Verify headless mode

```bash
opencode -p "echo test" -q
```

The `-p` flag runs non-interactively (auto-approves all tool calls). The `-q` flag
suppresses the spinner for clean stdout. If this fails, verify your provider API
key is configured.

### 3. Configure provider API key

OpenCode reads API keys from environment variables. Add to `/opt/poly-oracle-agent/.env`:

```bash
# For Anthropic (Claude)
ANTHROPIC_API_KEY=sk-ant-...

# Or for OpenAI-compatible providers
OPENAI_API_KEY=sk-...
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

### 4. Verify WI-61 audit timer is active

```bash
systemctl status poly-oracle-runtime-audit.timer
```

The server-runtime-review depends on WI-61 artifacts. If the audit timer is
not active, no artifacts will be produced and the review will abort with
exit code 1.

### 5. Ensure output directory exists

```bash
mkdir -p /opt/poly-oracle-agent/docs/runtime_observations
```

## Installation

```bash
# Copy systemd files
sudo cp deploy/systemd/poly-oracle-server-review.service /etc/systemd/system/
sudo cp deploy/systemd/poly-oracle-server-review.timer /etc/systemd/system/

# Reload and enable
sudo systemctl daemon-reload
sudo systemctl enable --now poly-oracle-server-review.timer

# Verify
systemctl list-timers poly-oracle-server-review.timer
```

## Aggregator Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Artifacts found and aggregated successfully |
| 1 | Zero artifacts in the requested window (no data to report) |
| 2 | Configuration or argument error |

## Fix Plan Thresholds

The fix plan is generated only when explicit thresholds are exceeded:

| Metric | Threshold | Severity |
|--------|-----------|----------|
| `critical_safety_gates` | > 0 | HIGH |
| `total_errors` | > 50 | MEDIUM |
| `budget_blocks` | > 10 | MEDIUM |

These thresholds are defined in `scripts/ops/aggregate_audits.py` and can be
adjusted by the operator. Changes require MAAP review if the aggregator is
under `src/`.

## Findings Reference

| Aggregator Field | Threshold | Finding Severity |
|------------------|-----------|------------------|
| `critical_safety_gates` | > 0 | HIGH |
| `dry_run_changed` | true | HIGH |
| `total_errors` | > 50 | MEDIUM |
| `budget_blocks` | > 10 | MEDIUM |
| `ws_reconnects` | > 5 | MEDIUM |
| `provider_failures` | > 5 | MEDIUM |
| `avg_response_time_ms` | > 500 | LOW |
| `db_growth_bytes` | > 100,000,000 | LOW |

## Manual Execution

To run the review manually (e.g., for testing):

```bash
cd /opt/poly-oracle-agent

# Step 1: Run the aggregator directly
.venv/bin/python scripts/ops/aggregate_audits.py --hours 72 --project-root .

# Step 2: Run the skill via opencode (headless)
opencode -p "/server-runtime-review" -q

# Or run interactively:
opencode
# Then type: /server-runtime-review
```

## Troubleshooting

### "No audit artifacts found"

- Verify `poly-oracle-runtime-audit.timer` is active: `systemctl status poly-oracle-runtime-audit.timer`
- Check the audits directory: `ls -la docs/operations/runtime_audits/`
- Verify the orchestrator is running: `pgrep -f "python -m src.orchestrator"`

### "opencode: command not found"

- Install from https://opencode.ai or `go install github.com/opencode-ai/opencode@latest`
- Verify PATH includes the Go bin directory: `which opencode`

### Review produces empty or malformed report

- Check the aggregator output manually: `.venv/bin/python scripts/ops/aggregate_audits.py --hours 72`
- Verify the JSON is valid: pipe output through `python -m json.tool`
- Check opencode logs: `journalctl -u poly-oracle-server-review.service --since "1 hour ago"`

## Security

- The aggregator scrubs all output for secrets (wallet addresses, API keys,
  condition IDs, token IDs) using the same patterns as `runtime_audit.py`.
- The systemd service uses `EnvironmentFile` (not hardcoded secrets).
- `ProtectSystem=strict` prevents filesystem writes outside `ReadWritePaths`.
- The skill never modifies source code — read-only reporting only.

## Anti-Patterns

- **Do not perform arithmetic in the LLM context.** All numbers come from the
  Python aggregator. The LLM synthesizes narrative only.
- **Do not flag `dry_run=true` as a finding.** It is the expected posture.
  Only flag if `dry_run_changed=true` (posture flipped during the window).
- **Do not generate fix plans for subjective reasons.** Only when explicit
  thresholds are exceeded.
