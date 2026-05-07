# Telegram Operational Alerts Runbook

This runbook documents configuration, verification, and troubleshooting
for the WI-50 operational alert bridge. The bridge sends deduplicated,
secret-free Telegram notifications when the deployed dry-run runtime
needs operator attention.

**Prerequisite:** WI-48 and WI-49 complete. Telegram Bot API token and
chat ID configured.

---

## 1. Architecture

The operational alert bridge (`OperationalAlertBridge`) runs as a
background task in the orchestrator. It evaluates five alert types
every 60 seconds:

| Alert Type               | Severity | Trigger Condition                          |
|--------------------------|----------|--------------------------------------------|
| `process_started`        | INFO     | Orchestrator startup (if enabled)          |
| `readiness_degraded`     | WARNING  | `/readyz` not ready for ≥ 5 min            |
| `websocket_stale`        | WARNING  | WS disconnected or PONG stale for ≥ 5 min  |
| `circuit_breaker_opened` | CRITICAL | Circuit breaker transitions CLOSED → OPEN  |
| `circuit_breaker_closed` | INFO     | Circuit breaker transitions OPEN → CLOSED  |

Alerts are deduplicated with a 10-minute cooldown per alert type.
Sustained thresholds (readiness, WebSocket) default to 5 minutes.

---

## 2. Configuration

All configuration lives in `.env`:

```bash
# --- WI-26: Telegram Notifier (required transport) ---
ENABLE_TELEGRAM_NOTIFIER=true
TELEGRAM_BOT_TOKEN=<telegram-bot-token-from-botfather>
TELEGRAM_CHAT_ID=<telegram-chat-id>
TELEGRAM_SEND_TIMEOUT_SEC=5

# --- WI-50: Operational Alert Bridge ---
ENABLE_OPERATIONAL_ALERTS=true
ENABLE_STARTUP_ALERT=true
OPERATIONAL_READINESS_DEGRADED_THRESHOLD_SEC=300
OPERATIONAL_WEBSOCKET_STALE_THRESHOLD_SEC=300
OPERATIONAL_ALERT_COOLDOWN_SEC=600
```

### Field Reference

| Variable                                    | Default | Description                                        |
|---------------------------------------------|---------|----------------------------------------------------|
| `ENABLE_OPERATIONAL_ALERTS`                 | `false` | Master enable for the alert bridge                 |
| `ENABLE_STARTUP_ALERT`                      | `false` | Send a `process_started` alert on orchestrator start |
| `OPERATIONAL_READINESS_DEGRADED_THRESHOLD_SEC` | `300` | Seconds of sustained degraded readiness before alert |
| `OPERATIONAL_WEBSOCKET_STALE_THRESHOLD_SEC` | `300`   | Seconds of sustained WS stale before alert         |
| `OPERATIONAL_ALERT_COOLDOWN_SEC`            | `600`   | Minimum seconds between duplicate alerts of same type |

**Important:** `ENABLE_TELEGRAM_NOTIFIER`, `TELEGRAM_BOT_TOKEN`, and
`TELEGRAM_CHAT_ID` must all be configured for the bridge to dispatch.
If any are missing, the bridge evaluates alert conditions but does not
send Telegram messages.

---

## 3. Verification

### 3.1 Verify Bridge Initialization

Check orchestrator logs on startup:

```
operational_alerts.enabled   # Bridge initialized successfully
```

If you see `operational_alerts.disabled`, verify `ENABLE_OPERATIONAL_ALERTS=true`.

If you see `telegram.disabled`, verify the Telegram notifier configuration.

### 3.2 Verify Startup Alert

If `ENABLE_STARTUP_ALERT=true`, you should receive a Telegram message
within seconds of orchestrator startup:

```
[INFO] Process Started

Poly-Oracle Agent started. DRY_RUN active. Service: poly-oracle-agent.

First seen: 2026-05-07T12:00:00+00:00
Service: poly-oracle-agent
```

### 3.3 Verify Alert Dispatch

Check logs for dispatch events:

```
operational_alerts.dispatched  alert_type=readiness_degraded severity=WARNING
```

### 3.4 Verify Cooldown

Inside the cooldown window, repeated degraded checks produce:

```
# No telegram.message_sent log — alert is suppressed
```

The bridge returns `SUPPRESSED_COOLDOWN` status.

---

## 4. Troubleshooting

### 4.1 No Telegram Messages Received

1. Verify `ENABLE_OPERATIONAL_ALERTS=true` in `.env`.
2. Verify `ENABLE_TELEGRAM_NOTIFIER=true`, token, and chat ID are set.
3. Check orchestrator logs for `telegram.send_failed`.
4. Test Telegram bot connectivity:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getMe"
   ```

### 4.2 Too Many Alerts (Spam)

- Increase `OPERATIONAL_ALERT_COOLDOWN_SEC` (default 600 = 10 min).
- Verify the bridge is not being restarted rapidly (startup alert disabled?).

### 4.3 No Alerts During Degradation

- Verify `OPERATIONAL_READINESS_DEGRADED_THRESHOLD_SEC` is appropriate.
- The bridge requires sustained degradation for the full threshold duration.
- Check that `ENABLE_OPERATIONAL_ALERTS=true`.

### 4.4 Readiness Flaps Causing Endless Alerts

The bridge tracks `first_seen_at_utc` per alert type. When readiness
returns to healthy, the state resets. A new degradation starts a fresh
timer. Combined with the cooldown, this prevents alert storms during
flapping conditions.

### 4.5 Telegram Send Fails

The bridge logs `operational_alerts.dispatch_error` with the error detail.
The runtime continues normally — alert dispatch is best-effort.
Common causes:
- Network unreachable from Droplet to `api.telegram.org`.
- Bot token revoked or invalid.
- Chat ID incorrect.

---

## 5. Operational Notes

- **Alert evaluation is read-only.** It does not mutate trading state.
- **Alert dispatch is non-blocking.** It does not block ingestion,
  context, evaluation, execution, health, or metrics loops.
- **Alerts are secret-free.** Payloads reject private keys, API keys,
  Telegram tokens, prompt text, reasoning text, condition IDs, token IDs,
  and raw exception messages at the Pydantic boundary.
- **Alert payloads are low-cardinality.** Reason codes are bounded to
  128 characters. Messages are bounded to 512 characters.
- **Telegram disabled is not fatal.** The bridge evaluates but does not
  dispatch. The orchestrator logs a structured disabled reason.
- **Do not use alerts for trade authorization.** Alerts are operational
  evidence only.
