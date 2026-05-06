# DigitalOcean Droplet Deployment Runbook

This runbook documents the complete deployment path for running `poly-oracle-agent`
on a single DigitalOcean Ubuntu 24.04 Droplet using Docker Compose with persistent
SQLite storage and mandatory `DRY_RUN=true`.

**Target Droplet:** Basic ($6/mo, 1 vCPU, 1 GB RAM, 25 GB SSD)
**DRY_RUN is mandatory.** No live signing or broadcasting is permitted under Phase 14.

---

## 1. Create the Droplet

1. Log into [DigitalOcean](https://cloud.digitalocean.com).
2. Click **Create → Droplets**.
3. Select **Ubuntu 24.04 (LTS) x64**.
4. Choose **Basic → Regular → $6/mo** (1 GB RAM, 1 vCPU, 25 GB SSD).
5. Choose a datacenter region close to you.
6. Under **Authentication**, select **SSH Key** and choose your public key.
   - **Password authentication must be disabled.**
7. Under **Additional Options**, enable **Monitoring** (free).
8. Set hostname to `poly-oracle-agent`.
9. Click **Create Droplet**.

---

## 2. Initial Droplet Hardening

SSH into the Droplet as `root`:

```bash
ssh root@<DROPLET_IP>
```

### 2.1 Create Non-Root Deploy User

```bash
adduser --disabled-password deploy
usermod -aG sudo deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/authorized_keys
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
```

Test the new user:

```bash
ssh deploy@<DROPLET_IP>
```

### 2.2 Configure Firewall

**Option A — UFW (recommended for single-node setup):**

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status verbose
```

Only port 22/tcp (SSH) should be open. No other inbound ports.

**Option B — DigitalOcean Cloud Firewall (recommended for managed environments):**

1. Go to **Networking → Firewalls**.
2. Create a firewall applied to the Droplet.
3. Allow inbound: SSH (22) only.
4. Allow all outbound.

### 2.3 Configure Swap (1 GB)

```bash
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 2.4 Disk Check

```bash
df -h
```

Ensure the root filesystem has at least 10 GB free.

---

## 3. Install Docker Engine and Compose Plugin

Run the following as `deploy` (with `sudo` where required):

```bash
# Install Docker Engine
curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
sudo sh /tmp/get-docker.sh
sudo usermod -aG docker deploy

# Log out and back in for group membership to take effect
exit
ssh deploy@<DROPLET_IP>

# Verify Docker
docker --version
docker compose version
```

### 3.1 Configure Docker Log Rotation

```bash
sudo mkdir -p /etc/docker
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF
sudo systemctl restart docker
```

### 3.2 Enable Docker Autostart

```bash
sudo systemctl enable docker
```

---

## 4. Deploy poly-oracle-agent

### 4.1 Clone the Repository

```bash
ssh deploy@<DROPLET_IP>
git clone https://github.com/<YOUR_ORG>/poly-oracle-agent.git
cd poly-oracle-agent
git checkout develop  # All work is on develop
```

### 4.2 Create and Secure the .env File

**CRITICAL:** `.env` is operator-managed secret material. It is `.gitignore`'d
and must never be committed, copied into reports, or exposed through any
observability surface.

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your real values:

```bash
nano .env
```

Required fields for deployment:

| Variable | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | Real Claude API key |
| `WALLET_ADDRESS` | EIP-55 address (dry-run placeholder is fine) |
| `WALLET_PRIVATE_KEY` | Private key (dry-run placeholder is fine) |
| `DRY_RUN` | **MUST be `true`** |
| `TELEGRAM_BOT_TOKEN` | Optional — leave empty to disable |
| `TELEGRAM_CHAT_ID` | Optional — leave empty to disable |

**Verify `DRY_RUN=true` before proceeding:**

```bash
grep DRY_RUN .env
# Expected: DRY_RUN=true
```

### 4.3 Build and Start

```bash
docker compose up -d --build
```

### 4.4 Verify the Service

```bash
# Check container status
docker compose ps

# Check logs
docker compose logs -f --tail=50 orchestrator

# Wait ~10s for health server to start, then:
curl -sf http://127.0.0.1:8080/healthz && echo "HEALTH OK"
curl -sf http://127.0.0.1:8080/readyz && echo "READY"
curl -sf http://127.0.0.1:8081/metrics | head -20
```

### 4.5 Run the Deployment Checker

```bash
python3 scripts/ops/check_deployment.py
```

All probes must show `"status": "pass"` for a healthy deployment.

---

## 5. Persistent SQLite and Volume Management

### 5.1 Volume Location

The SQLite database is stored in the named Docker volume `poly_oracle_data`,
mounted at `/data` inside the container. The database file is `/data/poly_oracle.db`.

### 5.2 Verify Persistence

```bash
# Check volume exists
docker volume ls | grep poly_oracle_data

# Check DB file size
docker compose exec orchestrator ls -lh /data/poly_oracle.db
```

### 5.3 SQLite Backup

```bash
# Quick backup — copy from the running container
docker compose exec orchestrator cp /data/poly_oracle.db /data/poly_oracle_backup.db
docker compose cp orchestrator:/data/poly_oracle_backup.db ./backup_$(date +%Y%m%d).db

# Or use sqlite3 from host (if installed)
sudo cp /var/lib/docker/volumes/poly_oracle_data/_data/poly_oracle.db ./backup.db
```

### 5.4 Restore from Backup

```bash
docker compose stop orchestrator
sudo cp ./backup.db /var/lib/docker/volumes/poly_oracle_data/_data/poly_oracle.db
docker compose start orchestrator
```

---

## 6. Service Management

| Command | Description |
|---|---|
| `docker compose up -d --build` | Build and start |
| `docker compose ps` | Check service status |
| `docker compose logs -f orchestrator` | Follow logs |
| `docker compose stop orchestrator` | Stop the service |
| `docker compose start orchestrator` | Start a stopped service |
| `docker compose restart orchestrator` | Restart |
| `docker compose down` | Stop and remove containers |
| `docker compose down -v` | **DESTRUCTIVE** — stop, remove containers AND volumes |

### 6.1 Restart Policy

`restart: unless-stopped` is configured — the container will restart automatically
unless explicitly stopped by the operator. If the Droplet reboots, the container
will start automatically.

---

## 7. Healthcheck Configuration

The container healthcheck calls `/healthz` (liveness) via `curl` every 30 seconds.
The Docker health status can be checked with:

```bash
docker compose ps --format json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('Health','unknown'))"
```

Upstream health is defined in Compose:

```yaml
healthcheck:
  test: ["CMD-SHELL", "curl -sf http://localhost:8080/healthz || exit 1"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 15s
```

The orchestrator also exposes `/readyz` (readiness with DB + WebSocket status) and
`/metrics` (Prometheus text) — both bound to loopback only.

---

## 8. Observability Endpoints

All bound to `127.0.0.1` — not publicly accessible. Access requires SSH:

| Endpoint | Port | Purpose |
|---|---|---|
| `/healthz` | 8080 | Liveness — 200 if event loop is alive |
| `/readyz` | 8080 | Readiness — 200/503 with DB + WS state |
| `/metrics` | 8081 | Prometheus text exposition |

### 8.1 Check from Host

```bash
curl -sf http://127.0.0.1:8080/healthz
curl -sf http://127.0.0.1:8080/readyz
curl -sf http://127.0.0.1:8081/metrics | grep -v '^#'
```

### 8.2 Port Exposure

Ports 8080 and 8081 are mapped to the host's loopback in `docker-compose.yml`:

```yaml
ports:
  - "127.0.0.1:8080:8080"
  - "127.0.0.1:8081:8081"
```

These are NOT open to the public internet. External access requires SSH tunneling:

```bash
ssh -L 8080:127.0.0.1:8080 -L 8081:127.0.0.1:8081 deploy@<DROPLET_IP>
```

---

## 9. Log Rotation

Docker logs are rotated per `daemon.json` (max 10 MB per file, 3 files retained).
Application logs via `structlog` are written to stdout/stderr and captured by Docker.

```bash
# Check log sizes
docker compose logs --tail=100
du -sh /var/lib/docker/containers/*/*.log
```

---

## 10. Updating the Agent

```bash
ssh deploy@<DROPLET_IP>
cd poly-oracle-agent
git pull origin develop
docker compose up -d --build
docker compose ps
python3 scripts/ops/check_deployment.py
```

Verify that the deployment checker passes all probes before confirming the update.

---

## 11. Troubleshooting

### Container fails to start

```bash
docker compose logs orchestrator
# Common issues: missing .env, invalid API key, port conflict
```

### Healthcheck fails

```bash
# Is the health server port accessible?
curl -v http://127.0.0.1:8080/healthz

# Are the ports properly mapped?
docker compose port orchestrator 8080
```

### Database issues

```bash
# Run Alembic manually
docker compose exec orchestrator alembic upgrade head

# Check DB file permissions
docker compose exec orchestrator ls -la /data/
```

### WebSocket stale / readiness degraded

```bash
curl -s http://127.0.0.1:8080/readyz | python3 -m json.tool
# Check the "websocket" field in the response
docker compose logs orchestrator | grep -i websocket | tail -20
```

### Disk full

```bash
df -h /
# Clean Docker build cache
docker builder prune -a
# Clean unused volumes
docker volume prune
```

---

## 12. Security Checklist

- [ ] SSH password authentication disabled; key-only access enforced.
- [ ] Non-root `deploy` user created with minimal sudo.
- [ ] UFW or DigitalOcean firewall active; only port 22 open publicly.
- [ ] `.env` has `chmod 600` and is never committed.
- [ ] `DRY_RUN=true` verified in `.env`.
- [ ] Health, readiness, metrics bound to `127.0.0.1`.
- [ ] No public port for 8080, 8081, or Streamlit.
- [ ] Telegram token and API keys are in `.env` only, not in logs.
- [ ] Docker runs with `appuser` (non-root) inside containers.
