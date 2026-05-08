# Paper-Trading Soak Test Runbook

**Phase 14 — WI-51**
**Purpose:** Prove the deployed dry-run system can run continuously with durable audit data, observable health, and clear operator recovery steps.

---

## 1. Prerequisites

- DigitalOcean Droplet deployed per `docs/runbooks/digitalocean-droplet-deployment.md`
- Docker Compose stack running with `DRY_RUN=true`
- Operator has SSH access to the Droplet
- `/healthz` (port 8080), `/readyz` (port 8080), and `/metrics` (port 8081) endpoints reachable
- SQLite database at `/data/poly_oracle.db`
- Python 3.12+ available on the machine running the evidence collector (can be the Droplet itself or an operator laptop with network access)

---

## 2. Soak Test Setup

### 2.1 Start the Soak

1. Confirm `DRY_RUN=true` in `.env`:
   ```bash
   grep DRY_RUN .env
   # Must output: DRY_RUN=true
   ```

2. Record the soak start time (ISO 8601 UTC):
   ```bash
   date -u +"%Y-%m-%dT%H:%M:%SZ" > /data/phase14_soak_start.txt
   cat /data/phase14_soak_start.txt
   ```

3. Record baseline DB size BEFORE starting the soak:
   ```bash
   BASELINE=$(stat -f%z /data/poly_oracle.db 2>/dev/null \
     || stat -c%s /data/poly_oracle.db 2>/dev/null \
     || echo 0)
   printf "%s\n" "$BASELINE" > /data/phase14_soak_db_baseline_size.txt
   cat /data/phase14_soak_db_baseline_size.txt
   ```

4. Start the orchestrator if not already running:
   ```bash
   docker compose up -d orchestrator
   ```

5. Verify the service is healthy:
   ```bash
   curl -sf http://127.0.0.1:8080/healthz && echo "OK"
   ```

### 2.2 During the Soak

- **Do NOT** restart the Droplet or containers unless testing recovery (see §5).
- **Do NOT** set `DRY_RUN=false` at any point.
- Monitor logs periodically:
  ```bash
  docker compose logs --tail=50 orchestrator
  ```
- Check restart count (should stay at 0 unless testing recovery):
  ```bash
  docker compose ps orchestrator --format json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Status',''))"
  ```

---

## 3. Duration Requirements

| Requirement | Minimum | Preferred |
|---|---|---|
| Soak duration | **24 hours** | **72 hours** |

**Duration shorter than 24 hours:** Evidence collector will emit a **FAILED** verdict.

**Duration 24–72 hours:** Evidence collector will evaluate evidence. If all mandatory gates pass, verdict is **PASS**.

**Duration ≥ 72 hours:** Preferred for any later live-readiness discussion.

---

## 4. Evidence Collection

### 4.1 Run the Collector

From the project root (on the Droplet or from an operator laptop with access to the endpoints and SQLite file):

```bash
python3 scripts/ops/collect_soak_evidence.py \
  --soak-start "$(cat /data/phase14_soak_start.txt)" \
  --db-path /data/poly_oracle.db \
  --db-baseline-size "$(cat /data/phase14_soak_db_baseline_size.txt)" \
  [--telegram-enabled] \
  [--recovery-tested "docker compose restart"]
```

**Flags:**

| Flag | Purpose |
|---|---|
| `--soak-start` | **Required.** ISO 8601 UTC timestamp of soak start. |
| `--target-host` | Host for HTTP probes (default: `127.0.0.1`). |
| `--db-path` | Path to SQLite file (default: `/data/poly_oracle.db`). |
| `--db-baseline-size` | **Required.** SQLite file size in bytes at soak start (for real growth delta). |
| `--telegram-enabled` | Include if Telegram operational alerts are configured (WI-50). |
| `--recovery-tested` | Include if restart/reboot recovery was tested (see §5). Value: `"docker compose restart"` or `"host reboot"`. |

### 4.2 Output Files

The collector writes two files under `docs/operations/`:

| File | Format | Purpose |
|---|---|---|
| `phase14-soak-report.md` | Markdown | Human-readable audit artifact |
| `phase14-soak-report.json` | JSON | Machine-readable evidence record |

### 4.3 Exit Codes

| Code | Meaning |
|---|---|
| `0` | All mandatory gates passed — verdict is **PASS**. |
| `1` | One or more mandatory gates failed — verdict is **FAIL** or **INCOMPLETE**. |
| `2` | Usage error (e.g., invalid `--soak-start` format). |

---

## 5. Recovery Testing (Optional but Recommended)

Recovery testing verifies the system can survive a restart and return to healthy state. This is not mandatory for a passing soak verdict — but recovery marked as untested will result in an **INCOMPLETE** verdict.

### 5.1 Container Restart Recovery

```bash
# 1. Record pre-restart state
curl -s http://127.0.0.1:8080/readyz | python3 -m json.tool

# 2. Restart the orchestrator
docker compose restart orchestrator

# 3. Wait for health to recover (up to 60s)
for i in $(seq 1 12); do
  if curl -sf http://127.0.0.1:8080/healthz; then
    echo "Recovered after $((i*5))s"
    break
  fi
  sleep 5
done

# 4. Verify readiness
curl -s http://127.0.0.1:8080/readyz | python3 -m json.tool

# 5. Run evidence collector with recovery flag
python3 scripts/ops/collect_soak_evidence.py \
  --soak-start "$(cat /data/phase14_soak_start.txt)" \
  --db-path /data/poly_oracle.db \
  --db-baseline-size "$(cat /data/phase14_soak_db_baseline_size.txt)" \
  --recovery-tested "docker compose restart"
```

### 5.2 Host Reboot Recovery

```bash
# 1. Verify Compose services are enabled for auto-start
docker compose config --services

# 2. Reboot the Droplet
sudo reboot

# 3. After reboot, SSH back in and verify
docker compose ps
curl -sf http://127.0.0.1:8080/healthz && echo "OK"

# 4. Check SQLite survived
ls -la /data/poly_oracle.db

# 5. Run evidence collector with recovery flag
python3 scripts/ops/collect_soak_evidence.py \
  --soak-start "$(cat /data/phase14_soak_start.txt)" \
  --db-path /data/poly_oracle.db \
  --db-baseline-size "$(cat /data/phase14_soak_db_baseline_size.txt)" \
  --recovery-tested "host reboot"
```

---

## 6. Pass / Fail Criteria

### Mandatory Gates (any failure → **FAIL**)

| Gate | Probe | Requirement |
|---|---|---|
| **Dry Run** | `dry_run_guard` | `DRY_RUN=true` in `.env` |
| **Duration** | `soak_duration` | ≥ 24 hours elapsed since soak start |
| **Health** | `health` | `/healthz` returns 200, `/readyz` status is `READY` |
| **Metrics** | `metrics` | `/metrics` returns 200 with valid Prometheus text format |
| **Database** | `database` | SQLite file exists, has grown, contains decisions or snapshots |

### Non-Mandatory Gates (may affect completeness)

| Gate | Effect if Failing |
|---|---|
| **Recovery** | Verdict is **INCOMPLETE** (not FAIL) if recovery not tested |
| **Compose Service** | Verdict is **FAIL** if service not running |
| **Telegram** | Records `not_applicable` if disabled; no effect on verdict |

### Verdict Summary

| All Mandatory Pass | Recovery Tested | Compose Running | Verdict |
|---|---|---|---|
| ✅ | ✅ | ✅ | **PASS** |
| ✅ | ❌ | ✅ | **INCOMPLETE** |
| ❌ | — | — | **FAIL** |
| ✅ | ✅ | ❌ | **FAIL** |

---

## 7. Recovery Steps (Troubleshooting)

### 7.1 Container Stopped

```bash
# Check status
docker compose ps orchestrator

# Restart
docker compose up -d orchestrator

# Check logs for crash cause
docker compose logs --tail=100 orchestrator
```

### 7.2 Readiness Degraded

```bash
# Inspect readiness detail
curl -s http://127.0.0.1:8080/readyz | python3 -m json.tool

# Common causes:
# - "database" check failing → SQLite may be locked or missing
# - "websocket" check failing → Polymarket CLOB WebSocket may be unreachable

# Restart to clear transient state
docker compose restart orchestrator
```

### 7.3 WebSocket Stale

The WebSocket connection to Polymarket CLOB may drop. The orchestrator should auto-reconnect. If readiness remains degraded for > 5 minutes:

```bash
docker compose restart orchestrator
```

If persistent, check network connectivity:
```bash
curl -sf https://clob.polymarket.com/health && echo "CLOB reachable"
```

### 7.4 Disk Nearly Full

```bash
# Check disk usage
df -h /data

# Check SQLite size
ls -lh /data/poly_oracle.db

# If disk is > 80% full:
# 1. Export a backup (see §7.5)
# 2. Consider expanding the Droplet volume or archiving old data
```

### 7.5 SQLite Backup Needed

```bash
# Backup while service is running (read-only copy)
sqlite3 /data/poly_oracle.db ".backup /data/poly_oracle_backup_$(date +%Y%m%d).db"

# Verify backup
sqlite3 /data/poly_oracle_backup_$(date +%Y%m%d).db "SELECT COUNT(*) FROM agent_decision_logs;"

# Copy off-Droplet if needed
scp deploy@<droplet-ip>:/data/poly_oracle_backup_*.db ./
```

### 7.6 Dashboard Tunnel Unavailable

Per `docs/runbooks/streamlit-ssh-tunnel.md`:

```bash
# Check SSH tunnel is alive
ps aux | grep "ssh.*-L.*8501"

# Re-establish tunnel
ssh -N -L 8501:127.0.0.1:8501 deploy@<droplet-ip> &

# Verify dashboard is running on Droplet
ssh deploy@<droplet-ip> "docker compose -f ~/poly-oracle-agent/docker-compose.yml --profile dashboard ps"
```

---

## 8. Important Safety Statements

- **`DRY_RUN=true` is required for the full soak duration.** Setting `DRY_RUN=false` during the soak invalidates the test.
- **Passing the soak test does NOT authorize `DRY_RUN=false`.**
- **Passing the soak test does NOT authorize live trading.**
- **The soak report is an audit artifact only.** It proves the system can run continuously in dry-run mode with durable persistence.
- **All secrets, private keys, tokens, and credentials are redacted from reports.** The evidence collector scrubs these before writing markdown or JSON.

---

## 9. Post-Soak Checklist

- [ ] Soak ran for ≥ 24 hours (72 preferred)
- [ ] `DRY_RUN=true` confirmed for full duration
- [ ] Evidence collector ran and produced `docs/operations/phase14-soak-report.md` and `.json`
- [ ] Verdict is **PASS** or **INCOMPLETE** (not FAIL)
- [ ] Health and readiness probes passed
- [ ] Metrics endpoint served valid Prometheus format
- [ ] SQLite file grew and contains decision/snapshot records
- [ ] Container restart count documented
- [ ] Recovery testing attempted and documented (or explicitly deferred)
- [ ] No secrets, private keys, tokens, or raw prompt/reasoning text in reports
- [ ] Report files committed to the repository (they contain no secrets by construction)

---

## 10. Reference

| Document | Purpose |
|---|---|
| `docs/runbooks/digitalocean-droplet-deployment.md` | Droplet setup, hardening, deploy |
| `docs/runbooks/streamlit-ssh-tunnel.md` | Dashboard SSH tunnel setup |
| `docs/runbooks/telegram-operational-alerts.md` | Telegram alert bridge config |
| `scripts/ops/check_deployment.py` | Pre-flight deployment validation |
| `scripts/ops/collect_soak_evidence.py` | Soak evidence collector |

---

*Phase 14 is not complete until a real soak report exists under `docs/operations/`.*
