# Aggressive Dry-Run Experiment Runbook

Operator runbook for running the **less-conservative ("ride or die") paper-trading
experiment** on the DigitalOcean Droplet, enabled by **WI-67** (configurable
Gatekeeper risk profiles).

The goal is narrow: gather enough **accepted simulated** BUY/SELL decisions to judge
whether the bot's all-HOLD behavior is excessive conservatism or a genuine lack of
edge — **without risking real capital.**

---

## 0. Safety boundary (read first)

- **`DRY_RUN=true` is mandatory and never changes.** This runbook does not authorize
  live signing, broadcasting, wallet mutation, or any bypass of the terminal
  `LLMEvaluationResponse` Gatekeeper. Phase 14 mandate stands.
- This experiment **does not prove edge.** Within the window almost no market
  resolves, so "EV of accepted trades" is the model's own estimate, not realized
  PnL. A historical backtest (now also profile-aware via WI-67 — run
  `BacktestRunner` with an aggressive `BacktestConfig`) is the cheaper, more direct
  test of edge. Prefer it before, or alongside, this run.
- It loosens **one** soft, epistemic gate (confidence). It does **not** loosen EV,
  spread, exposure, sizing, or the circuit breaker.

---

## 1. What WI-67 changed (why this now works)

Before WI-67 the terminal Gatekeeper read its thresholds from **hardcoded module
constants** in `src/schemas/llm.py`, never from `AppConfig`. Setting
`MIN_CONFIDENCE=0.65` in `.env` therefore changed nothing at the gate — a positive-EV
candidate at 0.675 confidence still HOLD'd at the 0.75 floor. WI-67 routes the six
risk knobs (`min_confidence`, `min_ev_threshold`, `max_spread_pct`,
`max_exposure_pct`, `min_ttr_hours`, `kelly_fraction`) into the gate via Pydantic
validation context, defaulting to the conservative constants when a value is absent
(fail-safe). **Only after WI-67 is deployed does lowering `MIN_CONFIDENCE` take
effect.**

---

## 2. Prerequisites

1. **WI-67 is merged into `develop`.** Run `/wi-done WI-67` locally, then push
   `develop` (and merge the PR to `main` per your flow). Without this, `git pull`
   on the server brings no behavior change.
2. **Droplet is running** (`159.223.130.81`, deploy dir `/opt/poly-oracle-agent/`).
   If it is powered off, power it on from the DigitalOcean console first.
3. **Budget headroom.** The hourly caps are already at 600; the daily caps are
   raised in the profile below so the run does not go dark (~4h to exhaustion was
   the prior failure mode).

---

## 3. Stage 1 profile — lower confidence only

The first experiment changes **one lever**. Everything else stays at the
conservative default so the result is interpretable.

```env
# --- Aggressive dry-run profile (WI-67) — Stage 1 ---
DRY_RUN=true              # MANDATORY — never false
MIN_CONFIDENCE=0.65       # the single lever (default 0.75)

# Unchanged conservative rails (explicit for auditability):
MIN_EV_THRESHOLD=0.02
MAX_SPREAD_PCT=0.015
MAX_EXPOSURE_PCT=0.03
KELLY_FRACTION=0.25
MIN_TTR_HOURS=4.0

# Budget headroom so the 48h run does not go dark:
LLM_DAILY_CALL_LIMIT=20000
LLM_DAILY_TOKEN_LIMIT=100000000
LLM_DAILY_COST_LIMIT_USD=250
```

> `MIN_CONFIDENCE=0.65` sits just below the highest confidence observed among
> positive-EV candidates (~0.675), so it is a targeted test, not a collapse of the
> gate. Note the WI-66 soft-flag factor (`REFLECTION_SOFT_FLAG_CONFIDENCE_FACTOR=0.90`)
> still penalizes confidence on soft bias flags — leave it unchanged.

---

## 4. Deploy steps (on the Droplet)

```bash
ssh deploy@159.223.130.81
cd /opt/poly-oracle-agent          # confirm with: docker compose config | grep -i env_file

# 4.1 Back up current config (revert anchor)
cp .env ".env.backup-$(date -u +%Y%m%dT%H%M%SZ)-pre-aggressive-dryrun"

# 4.2 Pull WI-67
git pull origin develop

# 4.3 Apply the Stage-1 profile (edit the keys in Section 3)
nano .env
grep -E "DRY_RUN|MIN_CONFIDENCE" .env     # MUST show DRY_RUN=true, MIN_CONFIDENCE=0.65

# 4.4 Rebuild + restart
docker compose up -d --build
docker compose ps

# 4.5 Verify
curl -sf http://127.0.0.1:8080/healthz && echo " HEALTH OK"
curl -sf http://127.0.0.1:8080/readyz   && echo " READY"
python3 scripts/ops/check_deployment.py   # all probes must "pass"
docker compose logs -f --tail=50 orchestrator
```

Confirm `dry_run=true` appears in the startup logs and `/readyz`.

---

## 5. Monitoring

Review at **6h, 12h, 24h, 48h**. Pull metrics over the SSH tunnel / dashboard
(see `streamlit-ssh-tunnel.md`) and the decision DB.

Watch, at minimum:
- **Decisions:** accepted dry-run BUY/SELL count; HOLD count; positive-EV candidates
  rejected by confidence / spread / TTR / validation.
- **Quality:** avg & median EV and confidence of accepted trades; `reflection_verdict`
  distribution; `reflection.soft_flag_downgrade` frequency; top recurring flags.
- **Concentration:** accepted trades by market / category / correlated narrative /
  side of a binary pair. *20 trades on one thesis is one bet, not 20 data points.*
- **Budget:** primary + reflection calls, daily tokens, estimated spend, any budget
  blocks. Track **spend per accepted simulated trade**.
- **Validation:** final-candidate Gatekeeper validation failures (terminal boundary).
- **Ops:** container health, `/healthz`, `/readyz`, DB reachability, WebSocket, disk.

---

## 6. Checkpoint logic (trade *quality*, not just count)

### 6 hours
Lower `MIN_CONFIDENCE` to **0.60** only if **all** hold:
- accepted dry-run trades are below target, **and**
- validation failures remain rare, **and**
- no accepted candidate had bad quote quality, **and**
- the positive-EV rejects cluster just below the confidence threshold, **and**
- the top rejected candidates look coherent on manual inspection, **and**
- spend per useful signal is acceptable.

A low trade count alone is **not** a reason to loosen — it may be the bot correctly
refusing weak opportunities.

### 12 hours
Do **not** auto-lower EV. First inspect the EV `0.015`–`0.02` band. Only set
`MIN_EV_THRESHOLD=0.015` if that band contains clean, coherent, non-concentrated
candidates narrowly missing the gate. EV is closer to the financial edge than
confidence — treat lowering it as materially riskier.

### 24h / 48h
Judge by quality: inspect accepted-trade rationales, compare entries against later
market movement, estimate simulated PnL where data allows, identify whether any edge
is repeatable. **A strong result earns deeper analysis — not live trading.**

---

## 7. Hard stop / revert triggers

Stop the experiment and **revert to the conservative config** if any of:
- final-candidate validation failures exceed ~1% of evaluations in any 1h window;
- accepted trades appear despite bad/missing quotes, or spread checks ride the limit;
- simulated exposure concentrates in one market/category/narrative;
- budget is consumed with little useful signal (spend-per-trade unreasonable);
- operational health degrades (readiness, DB, WebSocket, disk);
- **any** sign of a path bypassing `LLMEvaluationResponse`.

### Revert
```bash
ssh deploy@159.223.130.81
cd /opt/poly-oracle-agent
cp .env.backup-<TIMESTAMP>-pre-aggressive-dryrun .env   # restore Section 4.1 backup
grep -E "DRY_RUN|MIN_CONFIDENCE" .env                   # DRY_RUN=true, MIN_CONFIDENCE=0.75
docker compose up -d --build
python3 scripts/ops/check_deployment.py
```

---

## 8. After the run

- Keep the decision DB / daily digest for the window (see
  `daily-operations-digest.md`, `periodic-runtime-audit.md`).
- A **weak result** → do not keep loosening gates; invest in the fair-value model
  and market selection (discovery price-band, category fair-value, calibration).
- Any move toward live trading requires a **separate plan, separate approval**, and
  a review of this evidence — with much smaller capital limits than the dry-run
  sizing assumptions.
