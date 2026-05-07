# Streamlit Dashboard SSH Tunnel Runbook

This runbook documents the SSH tunnel path for accessing the Streamlit
Command Center dashboard running on a DigitalOcean Droplet.  Streamlit
listens on `0.0.0.0:8501` inside the container (required for Docker port
forwarding), but the Compose `ports:` directive publishes it exclusively to
the Droplet's loopback interface — **it is never exposed publicly.**

**Prerequisite:** WI-48 Droplet deployment complete.  SSH key access to the
Droplet as the `deploy` user.

---

## 1. Start the Dashboard Service

The dashboard runs under a Compose profile and does **not** start by default
with the orchestrator.

```bash
ssh deploy@<DROPLET_IP>
cd ~/poly-oracle-agent
docker compose --profile dashboard up -d dashboard
```

Verify the dashboard container is healthy:

```bash
docker compose ps dashboard
```

---

## 2. Open the SSH Tunnel

From your **local operator machine**, open a tunnel that forwards local port
8501 to the Droplet's loopback port 8501:

```bash
ssh -N -L 8501:127.0.0.1:8501 deploy@<DROPLET_IP>
```

- `-N` — do not execute a remote command (tunnel only).
- `-L 8501:127.0.0.1:8501` — forward local port 8501 to the Droplet's
  `127.0.0.1:8501`.

The command runs in the foreground.  Leave the terminal open while you use
the dashboard.

---

## 3. Access the Dashboard

Open your local browser and navigate to:

```
http://localhost:8501
```

The Poly-Oracle Command Center should appear with the dark terminal theme.

---

## 4. Verification

If the dashboard does not load:

1. **Check the tunnel process** — is `ssh -N -L ...` still running?
2. **Check the dashboard container** —
   ```bash
   ssh deploy@<DROPLET_IP> 'docker compose ps dashboard'
   ```
3. **Check container logs** —
   ```bash
   ssh deploy@<DROPLET_IP> 'docker compose logs --tail=50 dashboard'
   ```
4. **Verify port binding** — the host must publish the dashboard port only
   on loopback, not on the public interface:
   ```bash
   ssh deploy@<DROPLET_IP> 'ss -tlnp | grep 8501'
   ```
   Expected: `LISTEN 0 128 127.0.0.1:8501`

   Inside the container, Streamlit listens on `0.0.0.0` so Docker can
   forward the port, but the Docker Compose `ports:` directive restricts
   the host-side bind to `127.0.0.1`.  The dashboard is never reachable
   from the Droplet's public IP.

---

## 5. Shutdown

1. **Close the tunnel** — press `Ctrl+C` in the terminal running the SSH
   tunnel command.
2. **Stop the dashboard service** (optional — it can keep running safely
   since it binds to loopback only):
   ```bash
   ssh deploy@<DROPLET_IP> 'cd ~/poly-oracle-agent && docker compose --profile dashboard stop dashboard'
   ```

---

## 6. Optional: Reverse Proxy (Non-Default)

As an alternative to SSH tunneling, an Nginx reverse proxy can be
configured on the Droplet.  This path is **non-default and requires
explicit operator approval.**  If chosen, all of the following controls
must be in place:

- **TLS** — a valid certificate (e.g., Let's Encrypt via certbot).
- **Authentication** — HTTP basic auth or stronger at the Nginx layer.
- **IP allowlisting** — restrict access to known operator IPs.
- **No public metrics/health/DB exposure** — only the dashboard port may
  be proxied, and only behind the controls above.

Example Nginx location block (for reference only — not a deployment
instruction):

```nginx
location / {
    satisfy any;
    allow <OPERATOR_STATIC_IP>;
    deny all;
    auth_basic "Poly-Oracle Dashboard";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:8501;
}
```

**This configuration is not enabled by default and must not be deployed
without explicit operator review.**  The SSH tunnel path is the secure
default.
