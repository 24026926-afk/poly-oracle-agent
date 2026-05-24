# STATE.md — Poly-Oracle-Agent Project State

**Last Updated:** 2026-05-23
**Version:** 0.16.7
**Status:** Phase 16 COMPLETE — WI-62 Server Runtime Review IN PROGRESS
**Active WI:** WI-62

## WI-62 — Server Runtime Review Skill (IN PROGRESS)

**Trigger:** Post-WI-61, the server produces ~288 typed JSON audit artifacts every 72 hours but has no autonomous mechanism to aggregate them into a narrative review. The existing `server-runtime-review` OpenCode command is a thin skeleton, and the systemd service references the wrong binary (`claude` instead of `openclaude`) with a hardcoded placeholder API key.

**Brief context:** `~/documents/integration_task/01_Brief Context/WI-62-server-runtime-review.md`

**Scope:**
- Harden `scripts/ops/aggregate_audits.py` — zero-artifact detection, explicit Fix Plan thresholds, decision distribution, DB growth delta, secret scrubbing.
- Rebuild `.opencode/commands/server-runtime-review.md` — full pre-flight, error handling, canonical 12/14-section templates (match dry-run-review rigor).
- Fix `deploy/systemd/poly-oracle-server-review.{service,timer}` — openclaude binary, 24h cadence with 72h lookback, proper hardening.
- Server prerequisite documentation.

**Out of scope:** Modifying orchestrator code, WI-61 audit logic, Moonshot reviewer, any `DRY_RUN=false` path.

**Dependencies:** WI-61 artifacts, openclaude CLI with headless mode on server.

---

## WI-61 — Periodic Runtime Audit (COMPLETE, 2026-05-23)

**Trigger:** Post-Phase 16, the dry-run paper-trading deployment needs an always-on, deterministic safety-evidence layer that runs out-of-process on a fixed cadence and surfaces degradation via Telegram before an operator notices it in the dashboard. Phase 17 has not been scoped yet; WI-61 is a standalone operational-hardening Work Item.

**Brief context:** `~/documents/integration_task/01_Brief Context/WI-61-periodic-runtime-audit.md`

**Delivered:**
- `src/schemas/runtime_audit.py` — typed Pydantic V2 schemas (`RuntimeAuditReport`, `RuntimeAuditExitCode`, `RuntimeAuditStatus`, `RuntimeAuditFailureReason`, probe/summary/artifact/Telegram/reviewer models). All numeric fields are `Decimal` with `_reject_float` validators. All models frozen; bounded string lengths; tz-aware `generated_at_utc`.
- `src/observability/runtime_audit.py` — deterministic, read-only auditor. Probes `/healthz`, `/readyz`, `/metrics`, SQLite file, Docker Compose (optional), bounded log tail (optional). Routes all DB reads through `OperationalEventRepository`, `DecisionRepository`, `MarketRepository`, `PositionRepository`, `ExecutionRepository` — no raw SQL, no direct sessions. Forbidden-content scan applied to JSON+MD artifacts, Telegram payloads, and reviewer input/output. Atomic `latest.{json,md}` swap with typed failure reasons. Optional advisory LLM reviewer (`run_llm_review`) is a separate function using direct `httpx` POST to `https://api.moonshot.ai/v1/chat/completions` — disabled by default, no OpenCode/Hermes/OpenClaw dependency.
- `scripts/ops/periodic_runtime_audit.py` — CLI entrypoint with read-only SQLite URI access (`mode=ro&uri=true`), typed exit codes.
- `src/db/repositories/{decision,execution,market}_repo.py` — added bounded `get_recent_*(cutoff, limit)` methods using SQLAlchemy ORM `select()`.
- `deploy/systemd/poly-oracle-runtime-audit.{service,timer}` — 15-min cadence, `ProtectSystem=strict`, `ReadWritePaths=` constrained to `docs/operations/runtime_audits/`.
- `deploy/systemd/poly-oracle-runtime-review.{service,timer}` — committed disabled by default.
- `docs/runbooks/periodic-runtime-audit.md` — operator runbook.
- `.env.example` — `SQLITE_DB_PATH`, `APP_LOG_PATH`, `ENABLE_RUNTIME_AUDIT_ALERTS`, `RUNTIME_REVIEW_ENABLED`, `MOONSHOT_API_KEY` documented.

**Safety posture:**
- `dry_run=true` enforced as mandatory safety gate → exit code 2 on failure.
- Typed exit codes: `0=healthy`, `1=degraded`, `2=safety-gate failure`, `3=probe error`.
- No execution, signing, broadcasting, Gatekeeper, or `LLMEvaluationResponse` paths imported.
- No Alembic migration, no schema mutation, no `Base.metadata.create_all()`.

**MAAP cleared:** all five checks (no `float` for money, no raw SQL outside repos, no Gatekeeper bypass, no `dry_run` bypass, no schema drift) plus zero-trust audit on path traversal, atomic swap failure, forbidden-content scanning, and timeout coverage.

**Regression:** 149 WI-61 tests; full regression 2482 passed; coverage 93% (`runtime_audit.py` 92%, `schemas/runtime_audit.py` 95%).

**Branch:** `feat/wi-61-periodic-runtime-audit`, final commit: `352e26c`, merge commit: `0587744`.

---

## Post-WI-61 Doc Reconciliation — Spread Calibration (2026-05-23)

**Trigger:** 2026-05-23 dry-run review (`docs/runtime_observations/2026-05-23-orchestrator-dry-run-session.md` §4 / F1) surfaced drift between live `.env` (`PREFLIGHT_MAX_SPREAD_PCT=0.99`) and the 2026-05-18 hotfix-documented value (`0.90`).

**Decision:** Operator confirmed `0.99` is the canonical live value. The 2026-05-18 material-move bypass in `src/agents/context/aggregator.py` lets price-discovery moves clear cadence suppression even at the looser spread tolerance, so `0.99` is the intentional working calibration.

**Changes (this entry only — no code):** None. STATE.md is the authoritative record; `.env.example` retains `0.90` as the conservative default for fresh deployments, but live operator config runs `0.99`.

**Next:** Continue with the 2026-05-23 fix-plan sequence (F2 cooldown metric next).

---

## Post-Phase 16 Hotfix — Run 5 Runtime Stabilization Calibration (2026-05-18)

**Trigger:** Run 5 fix-plan review surfaced a spread-threshold calibration gap and a category-activation imbalance for CULTURE.

**Changes (committed on develop as `10d78b4`):**
- `.env.example` and local runtime calibration aligned to `LLM_MARKET_HOURLY_CALL_LIMIT=120` and `PREFLIGHT_MAX_SPREAD_PCT=0.90`. *(Live `.env` was subsequently bumped to `0.99` by the operator; canonical value reconciled in the 2026-05-23 entry above.)*
- `src/agents/context/aggregator.py` now lets material midpoint/spread moves bypass category cadence suppression.
- `src/orchestrator.py` throttles only diagnostic operational events and counts throttled diagnostics as dropped.
- `docs/runbooks/llm-cost-guard.md` now matches the current cost calibration.

**Regression:** focused 367 passed; full regression 2329 passed; coverage-backed regression 2329 passed; coverage 93%.
**Next:** run a fresh dry-run validation against `10d78b4`.

## Post-Phase 16 Hotfix — Run 2 Runtime Stabilization (2026-05-17)

**Trigger:** Run 2 dry-run observation showed the previous Grok/DeepSeek fixes held, but the
runtime entered a budget-driven idle state: per-market LLM caps fired within minutes, budget-block
logs and ledger rows dominated observability, repeated market rejection events flooded the ledger,
and concurrent dashboard/CLI reads could hit SQLite lock contention.

**Changes (uncommitted on develop):**
- `src/agents/evaluation/llm_cost_guard.py` / `src/schemas/llm.py` — added per-market budget
  window expiry, throttled audit emission fields, and preserved per-occurrence metrics.
- `src/agents/evaluation/claude_client.py` / `src/agents/context/bounded_queue.py` — replaced
  duplicate provider-skip budget logs with throttled structured diagnostics and budget-quarantined
  prompt queue drops until the market budget window reopens.
- `src/agents/ingestion/market_discovery.py` / `src/schemas/ops.py` — emit repeated
  `MARKET_REJECTED` only on state changes and add a cycle-summary event.
- `src/db/engine.py`, `src/ui/dashboard.py`, `src/observability/dashboard_activity_feed.py` —
  enable SQLite WAL/busy-timeout behavior for runtime connections and dashboard reads.
- `src/orchestrator.py` — resolves activation categories using the evaluation resolver and suppresses
  unchanged WebSocket subscription-summary logs.
- `.env.example`, `README.md`, `docs/runbooks/llm-cost-guard.md` — updated dry-run LLM budget
  tuning to 240 primary calls/hour, 240 reflection calls/hour, 2000 daily calls, and 60 calls/market/hour.

**Regression:** Final wi-done full regression passed outside the sandbox: 2314 passed. Latest
checker coverage before the final correction pass: 92%. MAAP follow-ups removed the dead
`MarketQuarantineReason.BUDGET_EXHAUSTED` enum/test, made `peek_budget()` read-only for budget
window state, and drain queued snapshots when budget quarantine starts.
**Next:** Run the next dry-run validation window against the committed Run 2 stabilization hotfix.

---

## Post-Phase 16 Hotfix — Grok Sentiment Eligibility Expansion (2026-05-17)

**Trigger:** Live paper-trading run showed `grok_sentiment SKIPPED_CATEGORY` for all monitored
markets. Root cause: `_GROK_ELIGIBLE` in `claude_client.py` only allowed `CRYPTO` and `POLITICS`;
markets classified as `ELECTIONS`, `GEOPOLITICS`, `FINANCE`, `TECH`, `IRAN`, `ECONOMY` were
silently skipped with neutral fallback.

**Changes (uncommitted on develop):**
- `src/schemas/llm.py` — added authoritative `GROK_ELIGIBLE_CATEGORIES` frozenset
  (CRYPTO, POLITICS, ELECTIONS, GEOPOLITICS, FINANCE, TECH, IRAN, ECONOMY)
- `src/agents/evaluation/claude_client.py` — imports shared constant, removes local `_GROK_ELIGIBLE`,
  adds `market_category_resolved` debug log with `market_key_hash` + `grok_eligible` flag
- `src/agents/evaluation/grok_client.py` — imports shared constant, guard aligned to prevent drift

**Regression:** 2296 passed, coverage 93% — no regressions introduced.
**Next:** MAAP + commit on develop → PR to main.

---

## Phase 16 Plan

- **PRD:** `docs/PRD-v16.0.md`
- **Planning Date:** 2026-05-14
- **Objective:** Make every autonomous dry-run server run understandable, resumable, and reconstructable by a non-technical operator through a durable operational event ledger, deterministic human-readable narratives, incident replay, dashboard timeline visibility, and an automatic daily operations digest.
- **Operational trigger:** The previous DigitalOcean paper-trading run exposed that technical stdout/Docker logs were ephemeral and hard to inspect, while the dashboard did not provide a durable chronological story of runtime behavior.
- **Scope guard:** Live trading, `DRY_RUN=false` changes, live signing or broadcasting, Gatekeeper changes, LLM-generated narratives, historical Docker-log backfill, hash-chain ledgers, PostgreSQL migration, and public dashboard exposure remain out of scope.
- **WIs completed:**
  - **WI-56 — Operational Event Ledger:** COMPLETE. Added typed operational event schemas, Alembic migration `0006_add_operational_events.py`, append-only `OperationalEventRepository`, bounded async `OperationalEventBus`, readiness degradation for safety-critical ledger failures, low-cardinality event metrics, source hooks across orchestrator/evaluation/market discovery, and `docs/runbooks/operational-event-ledger.md`. MAAP findings resolved: no-market startup now flushes/shuts down cleanly, event ledger monitor task is owned, critical publish/persist failures fail closed, runtime hooks include LLM and market-quarantine events, overflow policy is typed, and `drop_diagnostic` preserves critical events over lower-priority queued data. 148 WI-specific tests; full regression 1972 passed; coverage 92%. Branch: `feat/wi-56-operational-event-ledger`, final commit: `57c5946`, merge commit: `27e1485`.
  - **WI-57 — Deterministic Human Narratives:** COMPLETE. Added presentation-layer schemas in `src/schemas/ops.py` (`NarrativeRenderStatus`, `NarrativeRenderFailureReason`, `NarrativeTemplateKey`, `NarrativeInspectionHint`, `OperationalNarrative`, `DecisionNarrative`, `RuntimeNarrative`, `NarrativeRenderResult`) and the deterministic renderer `src/observability/operational_narratives.py` (`render_event`, `render_window`) which maps every `(event_type, reason_code)` family to a stable English template, augments only with secret-safe bounded payload fields, scans output for forbidden patterns, and returns typed `SUCCESS` / `FALLBACK` / `REDACTED` / `FAILED` statuses. Layer is read-only — no LLM calls, no DB writes, no execution path, no `dry_run` weakening, no Gatekeeper bypass. MAAP findings resolved: (1) parsed `payload_json` is recursively scanned for forbidden secret / high-cardinality content and fails closed to `REDACTED` even when the template would not surface those fields; (2) `decision_action` on `DecisionNarrative` is derived strictly from the typed `reason_code` via `_REASON_CODE_TO_DECISION_ACTION`, so a persisted payload claiming `SELL` against `reason_code=DECISION_BUY` can never contradict the typed reason code in the rendered narrative. 84 WI-specific tests (75 unit + 9 integration); full regression 2056 passed; coverage 93%. Branch: `feat/wi-57-deterministic-human-narratives`.
  - **WI-58 — Incident Replay CLI:** COMPLETE. Read-only operator CLI that reconstructs bounded UTC time windows from the operational event ledger (WI-56) and renders deterministic, secret-safe narratives using the narrative layer (WI-57). Added `src/observability/incident_replay.py` (service layer with `run_replay()` entry point), `scripts/ops/replay.py` (~370-line CLI with secret-safe argument parsing and custom `_SafeArgParser` to scrub raw input from argparse errors), and `docs/runbooks/incident-replay.md` (operator runbook). Typed schemas: `IncidentReplayStatus`, `IncidentReplayFailureReason`, `IncidentReplayFilter`, `IncidentReplayRequest`, `IncidentReplayLine`, `IncidentReplaySummary`, `IncidentReplayReport`. CLI accepts `--from`/`--to` ISO-8601 timestamps (tz-aware UTC) and typed enum filters (severity, source, event-type, reason-code) with case-insensitive matching. Output is chronological, secret-safe (API keys, wallet addresses, token IDs, condition IDs, raw prompts, exception text redacted at multiple defense layers), and includes typed summary counts (total_events, warnings, errors, decisions_by_action, skips_by_reason, llm_calls, budget_blocks, provider_failures, readiness_changes). Exit codes: 0=success/empty/truncated, 2=invalid input, 3=repository/database failure. MAAP findings resolved: (1) invalid filter values echoed via `_safe_echo()` redaction, (2) --limit values outside [1, 1000] fail closed with LIMIT_OUT_OF_RANGE reason, (3) argparse error messages scrubbed by custom `_SafeArgParser.error()` override that runs all tokens through `_scrub_argparse_message()` before printing. 63 WI-specific unit tests + 6 integration tests; full regression 2132 passed; coverage 93%. Branch: `feat/wi-58-incident-replay-cli`, merge commit: `d77440e`.
  - **WI-59 — Dashboard Activity Feed:** COMPLETE. Read-only activity feed service `src/observability/dashboard_activity_feed.py` that queries operational events from the WI-56 ledger, renders deterministic narratives via WI-57, and derives current-state from strict latest-wins semantics. Added `OperationalEventRecord`, `DashboardActivityFeedItem`, `DashboardCurrentState`, `DashboardActivityFeedResult` schemas to `src/schemas/ops.py`. Wired timeline and current-state panel renderers in `src/ui/dashboard.py` with `@st.cache_data(ttl=30)` caching. MAAP findings resolved: (1) HIGH — `_resolve_overall_state` fixed to strict latest-wins so newer recovery events always supersede stale degraded/stopped states; (2) MEDIUM — `_parse_sqlite_timestamp` returns `Optional[datetime]`; rows with NULL or malformed timestamps are dropped at data-ingress, never promoted with fabricated `now()` timestamps. Regression tests added for both findings plus secret-safe logging guard. 68 WI-specific unit tests + 40 integration tests; full regression 2200 passed; coverage 93%. Branch: `feat/wi-59-dashboard-activity-feed`, final commit: `248bb46`, merge commit: `b00d374`.
  - **WI-60 — Daily Operations Digest:** COMPLETE. Added deterministic daily bot digest generation via `src/observability/daily_ops_digest.py`, operator CLI `scripts/ops/generate_daily_ops_digest.py`, typed digest schemas in `src/schemas/ops.py`, bounded operational-event pagination/read-cap handling in `OperationalEventRepository`, Telegram digest delivery support through `TelegramNotifier`, and `docs/runbooks/daily-operations-digest.md`. The digest writes only validated `03_Daily/YYYY-MM-DD-bot.md` paths, never overwrites manual daily notes, uses repository-backed event and position reads, preserves Decimal-native spend/PnL calculations, scans file and Telegram output for forbidden content, handles no-run/partial-run/read-cap states with typed results, and does not import LLM, Gatekeeper, signing, broadcasting, or order-routing paths. MAAP cleared. 77 WI-specific tests; full regression 2281 passed; coverage 93%. Branch: `feat/wi-60-daily-operations-digest`, final commit: `5559634`, merge commit: `ada8243`.
- **WIs remaining:** none
- **Deliverable boundary:** Per `AGENTS.md`, `/prd` created only `docs/PRD-v16.0.md` and updated `STATE.md`. WI business-logic and implementation-prompt deliverables must be generated one at a time via `/wi-start {WI}`.

---

## Phase 16 Archive

- **PRD:** `docs/PRD-v16.0.md`
- **Completion Report:** `04_Archive/poly-oracle-agent/Phase-16/PHASE-16-COMPLETE.md`
- **Close Date:** 2026-05-17
- **Final Tests:** 2285 | **Coverage:** 93%
- **Develop HEAD:** `c92b212`
- **WIs Completed:** WI-56, WI-57, WI-58, WI-59, WI-60

---

## Phase 15 Archive

- **PRD:** `docs/PRD-v15.0.md`
- **Completion Report:** `04_Archive/poly-oracle-agent/Phase-15/PHASE-15-COMPLETE.md`
- **Close Date:** 2026-05-14
- **Final Tests:** 1824 | **Coverage:** 93%
- **Develop HEAD:** `53f62c8`
- **WIs Completed:** WI-52, WI-53, WI-54, WI-55

---

## Phase 15 (Detail)

- **PRD:** `docs/PRD-v15.0.md`
- **Planning Date:** 2026-05-10
- **Objective:** Prevent uncontrolled LLM spend, fix repeated single-market evaluation loops, and add DeepSeek V4 Pro as a lower-cost configurable evaluation provider while preserving `DRY_RUN=true`, Gatekeeper authority, Decimal integrity, and auditability.
- **Operational trigger:** A DigitalOcean paper-trading run exhausted Claude usage while the bot remained stuck evaluating one market. Phase 15 treats this as a financial-integrity issue, not a normal provider preference.
- **Scope guard:** `DRY_RUN=false`, live trading approval, live signing, live broadcasting, Gatekeeper replacement, full-time Claude/DeepSeek shadow mode, prompt-strategy redesign, and adding the `openai` SDK remain out of scope.
- **Provider approach:** DeepSeek must be integrated through DeepSeek's Anthropic-compatible endpoint using the existing `anthropic` SDK unless implementation proves that path unsafe and escalates before dependency changes.
- **WIs completed:**
  - **WI-52 — LLM Cost Guard and Cognitive Circuit Breaker:** COMPLETE. `src/agents/evaluation/llm_cost_guard.py` (LLMBudgetGuard + MarketCognitiveCircuitBreaker), `src/schemas/llm.py` (11 new schemas), `src/core/config.py` (12 new fields), `src/observability/metrics.py` (6 new metrics), `src/orchestrator.py` (metrics wired), `docs/runbooks/llm-cost-guard.md`. 115 new tests (100 unit + 15 integration). Branch: `feat/wi-52-llm-cost-guard-and-cognitive-circuit-breaker`, merge commit: `b716f29`.
  - **WI-53 — Market Eligibility, Evaluation Deduplication, and Queue Backpressure:** COMPLETE. `src/schemas/market_eligibility.py` (12 new schemas), `src/agents/ingestion/market_quarantine.py` (MarketQuarantineManager), `src/agents/context/bounded_queue.py` (BoundedPromptQueue), `src/agents/ingestion/market_discovery.py` (preflight), `src/agents/context/aggregator.py` (dedupe), `src/core/config.py` (6 new fields), `src/observability/metrics.py` (7 new metrics), `src/orchestrator.py` (all wired), `docs/runbooks/market-eligibility-and-backpressure.md`. 113 new tests (95 unit + 18 integration). Branch: `feat/wi-53-market-eligibility-evaluation-deduplication-and-queue-backpressure`, merge commit: `9d3f0c7`.
  - **WI-54 — Configurable DeepSeek Provider via Anthropic-Compatible Endpoint:** COMPLETE. `src/schemas/llm.py` (9 new schemas: LLMProvider, LLMProviderConfig, LLMProviderRuntimeContext, LLMProviderUsage, LLMProviderMetadata, LLMProviderSelectionDecision, LLMProviderSelectionReason, LLMProviderConfigError, LLMProviderConfigErrorReason — all frozen Pydantic V2, Decimal-native cost fields, SecretStr for api_key), `src/core/config.py` (6 new fields + model_validator for fail-closed provider validation), `src/agents/evaluation/claude_client.py` (provider-aware `__init__` resolving api_key/base_url/model/max_tokens/max_retries from selected provider, `AsyncAnthropic` instantiated with provider-specific `max_retries`, `_max_retries` wired into both retry methods with `Optional[int]=None` defaults, preserved `ClaudeClient` class name, provider-neutral log/error messages, provider metadata in audit logs), `src/observability/metrics.py` (3 new provider metrics: selections, failures, active provider gauge — all low-cardinality labels), `.env.example` updated with DeepSeek + `LLM_PROVIDER` block, `README.md` provider config table added, `docs/system_architecture.md` Layer 3 updated for dual-provider operation, `AGENTS.md` entry for WI-54 added, `tests/unit/test_evaluation_budget.py` extended with WI-54 fields. 80 new unit tests. Branch: `feat/wi-54-configurable-deepseek-provider-via-anthropic-compatible-endpoint`, merge commit: `bfd8102`.
  - **WI-55 — DeepSeek Backtest Calibration and Paper-Trading Readiness Gate:** COMPLETE. `src/schemas/provider_comparison.py` (LLMProviderComparisonConfig, LLMProviderDecisionMetrics, LLMProviderCalibrationMetrics, LLMProviderCostMetrics, LLMProviderLatencyMetrics, LLMProviderComparisonResult, LLMProviderComparisonRun, LLMProviderComparisonReport, LLMProviderReadinessVerdict, LLMProviderReadinessReason, LLMProviderCalibrationRecommendation — all frozen Pydantic V2, Decimal-native), `src/backtesting/provider_comparison.py` (derive_readiness_verdict, derive_calibration_recommendation, redact_secrets, validate_report_path, generate_comparison_report), `scripts/run_llm_provider_comparison.py` (CLI runner driving the canonical `ClaudeClient.evaluate_for_backtest` chain for DeepSeek primary + optional Anthropic sampling), `src/agents/evaluation/claude_client.py` (new public `evaluate_for_backtest(prompt, snapshot_id, market_key)` returning `(parsed, usage, block_reason)`, `_get_primary_candidate` refactored to return `(result, block_reason)` — removed shared mutable `_last_primary_block_reason` to prevent cross-market bleed under concurrent calls), `src/schemas/execution.py` + `src/backtest_runner.py` (added `outcome_resolved: bool` field to `BacktestDecision`/`BacktestSnapshot`; loader sets when source carries explicit `realized_pnl_usdc` key; `_replay_snapshot` sets when a trade was executed — break-even zero-PnL outcomes now correctly counted as resolved data), `docs/runbooks/deepseek-paper-trading-readiness.md`. MAAP findings resolved: reflection-budget rejections counted in `budget_block_count` regardless of usage, no shared mutable block-reason state, outcome coverage detection no longer mis-classifies zero-PnL resolved outcomes. Branch: `feat/wi-55-deepseek-backtest-calibration-and-paper-trading-readiness-gate`.
- **Deliverable boundary:** Per `AGENTS.md`, `/prd` created only `docs/PRD-v15.0.md` and updated `STATE.md`. WI business-logic and implementation-prompt deliverables must be generated one at a time via `/wi-start {WI}`.

---

## Phase 13 Archive

- **PRD:** `docs/PRD-v13.0.md`
- **Completion Report:** `04_Archive/poly-oracle-agent/Phase-13/PHASE-13-COMPLETE.md`
- **Close Date:** 2026-05-06
- **Final Tests:** 1041 | **Coverage:** 93%
- **WIs Completed:** WI-43, WI-44, WI-45, WI-46, WI-47
- **Post-Phase 13 Configuration Updates (2026-05-06):**
  - Grok live mode enabled: `GROK_LIVE_ENABLED=True` added to `.env.example`, `GROK_MOCKED=False`, `GROK_MODEL=grok-4-1-fast` set.
  - Claude model updated: Default `anthropic_model` in `AppConfig` and `.env.example` set to `claude-sonnet-4-20250514`.


---

## Historical Context & Invariants

See `docs/archive/ARCHIVE_PHASES_1_TO_3.md` for:
- Core architectural invariants (4-layer pipeline, Decimal math, Repository Pattern, Pydantic Gatekeeper)
- Completed infrastructure inventory
- WI-01 through WI-10 achievement index

---

## Current Metrics

| Metric | Value |
|---|---|
| Total tests | 2333 |
| Latest local test result | 2333 passed; coverage-backed regression also 2333 passed |
| Coverage | 93% (target ≥ 80%) |
| Framework | `pytest` + `pytest-asyncio` |
| DB | `poly_oracle.db` (SQLite, Alembic-managed, 6 migrations) |

## Runtime Hotfixes

2026-05-19 - CULTURE Grok sentiment upgrade (commit `b4d18fc`):
- Added `MarketCategory.CULTURE` to `GROK_ELIGIBLE_CATEGORIES` so CULTURE markets receive live xAI Grok sentiment rather than a static neutral fallback.
- Injected `_CULTURE_SIGNAL_GUIDANCE` into the GrokClient user prompt for CULTURE markets, instructing the model to resolve broad hype, fandom noise, or stale discourse toward neutral (score ≈ 0.0) rather than inventing directional edge.
- Added `CULTURE_EVALUATION_INTERVAL_SEC=600` (`AppConfig.culture_evaluation_interval_sec`) and wired it through `DataAggregator.configure_category_cadence` and `Orchestrator` so CULTURE gets live signal coverage without consuming the normal 30s Grok cadence budget.
- The CULTURE cadence branch in `aggregator.py` is evaluated **before** the Grok-eligible check, ensuring the 600s floor holds even if CULTURE appears in `GROK_ELIGIBLE_CATEGORIES`.
- Validation: full regression 2333 passed; coverage 93%. MAAP cleared.

2026-05-17 - LLM/Grok dry-run throughput stabilization:
- Split primary and reflection hourly LLM budget accounting so `LLM_HOURLY_CALL_LIMIT` now applies to primary evaluations and `LLM_REFLECTION_HOURLY_CALL_LIMIT` applies to reflection audits, while daily/token/cost/per-market caps remain shared fail-closed safety controls.
- Added non-mutating `LLMBudgetGuard.peek_budget()` and used it to skip eligible Grok sentiment calls when the downstream primary LLM call is already budget-blocked, preventing wasted xAI calls and 429 pressure.
- Added Grok narrative summary truncation before `SentimentResponse` validation so otherwise-valid sentiment payloads are preserved when only `top_narrative_summary` exceeds the 320-character prompt-budget field.
- Enabled local runtime observability flags in `.env` for Telegram startup/operational alerts and the WI-56 operational event ledger, with circuit breaker explicitly left disabled for dry-run.
- Validation: targeted WI-52/Grok/sentiment tests passed; full regression passed (`2305 passed`); coverage remained 93%.

2026-05-17 - Prompt queue consumer and DeepSeek model stabilization:
- Fixed `BoundedPromptQueue` lock starvation by keeping coalesce/drop-stale mutation synchronous under the queue lock and recording coalescing metrics after releasing the lock.
- Fixed coalescing-miss audit metrics so no-match stale drops increment dropped-context metrics rather than coalesced-context metrics.
- Added `ClaudeClient` consumer startup logging and a done-callback error log so unexpected consumer termination is visible in the runtime log stream.
- Corrected the DeepSeek model from invalid `deepseek-v4-pro` to `deepseek-chat` (`DeepSeek-V3`) in config/runtime settings and matching test assertions.
- Restored WI-52 zero-limit fail-closed semantics while setting usable non-zero operational defaults for daily token and per-market hourly limits.
- Isolated WI-54 provider config tests from local `.env` provider settings.
- Fixed a `BoundedPromptQueue.get()` notification-clear race that could erase a producer wakeup and starve the evaluation consumer.
- Aligned WI-55 provider-comparison defaults and DeepSeek readiness runbook with the runtime `deepseek-chat` model.
- Latest local regression: 2296 passed; coverage 93%.

2026-05-17 - WebSocket subscription and book-frame stabilization:
- Aligned `CLOBWebSocketClient` market-channel initial and live subscription payloads with current Polymarket docs (`type: market` for initial subscriptions; `operation: subscribe` / `operation: unsubscribe` for live updates).
- Added live unsubscribe-all handling and stale aggregator-route pruning during market deactivation/rotation.
- Suppressed misleading `ws.frame_unrouted` warnings for known active tokens that intentionally flow through the standard persistence path rather than a registered per-token aggregator.
- Fixed full orderbook frame parsing to use max bid and min ask, preventing first-level array ordering from fabricating midpoint `0.5` / spread `0.98` snapshots.
- Added crossed-book fail-closed handling before snapshot emission.
- Moved `MarketSnapshotSchema` bid/ask/last-trade/midpoint validation to Decimal-native arithmetic before persistence.
- Focused validation: 363 hotfix-related tests passed; full regression 2296 passed; Ruff passed on touched files.

2026-05-17 - WebSocket YES/NO quote normalization:
- Added condition-level YES/NO token-pair propagation from `Orchestrator` into `CLOBWebSocketClient`.
- Normalized NO-token WebSocket quotes into canonical YES-probability bid/ask ranges before snapshot validation, persistence, and context enqueueing.
- Populated `no_token_id` on emitted `MarketSnapshot` rows when Gamma token-pair metadata is available.
- Added regressions for NO-token `price_change` and `book` frames so paired books no longer alternate the same condition between mirrored midpoints like `0.095` and `0.905`.
- Focused validation included NO-token `price_change` and `book` normalization, unsubscribe rotation, stale aggregator pruning, queue metrics, and provider env isolation; full regression passed unforced (`2296 passed`).

Phase 14 planned 2026-05-06 — DigitalOcean 24/7 Paper-Trading Deployment:
- `docs/PRD-v14.0.md` created as the Phase 14 planning source.
- **Objective:** Deploy the agent to a DigitalOcean Droplet for stable 24/7 paper trading while preserving `DRY_RUN=true`, secret hygiene, SQLite audit persistence, and operator visibility.
- **WIs ready for implementation:**
  - WI-48 — DigitalOcean Droplet Deployment Hardening
  - WI-49 — Secure Remote Operator Dashboard Access
  - WI-50 — Telegram Operational Alert Bridge
  - WI-51 — 24/7 Paper-Trading Soak Test and Runbook
- **Scope guard:** `DRY_RUN=false`, PostgreSQL migration, public unauthenticated dashboard/metrics exposure, Kubernetes, managed Prometheus/Grafana, and strategy optimization remain out of scope.
- Phase 14 business-logic and implementation-prompt deliverables generated on 2026-05-06 by explicit user request:
  - `docs/deliverables/business_logic/business_logic_WI-48-digitalocean-droplet-deployment-hardening.md`
  - `docs/deliverables/implementation_prompts/prompt_WI-48-digitalocean-droplet-deployment-hardening.md`
  - `docs/deliverables/business_logic/business_logic_WI-49-secure-remote-operator-dashboard-access.md`
  - `docs/deliverables/implementation_prompts/prompt_WI-49-secure-remote-operator-dashboard-access.md`
  - `docs/deliverables/business_logic/business_logic_WI-50-telegram-operational-alert-bridge.md`
  - `docs/deliverables/implementation_prompts/prompt_WI-50-telegram-operational-alert-bridge.md`
  - `docs/deliverables/business_logic/business_logic_WI-51-24-7-paper-trading-soak-test-and-runbook.md`
  - `docs/deliverables/implementation_prompts/prompt_WI-51-24-7-paper-trading-soak-test-and-runbook.md`

WI-48 — DigitalOcean Droplet Deployment Hardening: COMPLETE.
- `docker-compose.yml`: real curl-`/healthz` healthcheck, loopback port publishing (8080, 8081), `HEALTH_SERVER_HOST=0.0.0.0` / `METRICS_SERVER_HOST=0.0.0.0` for port publishing reachability.
- `Dockerfile`: curl installed, HEALTHCHECK uses `/healthz`, `--start-period=15s`.
- `.env.example`: `DRY_RUN=true` (Phase 14 mandate), deployment comment added.
- `scripts/ops/check_deployment.py`: stdlib-only deployment checker (no pip deps) — validates Docker/Compose, service status, `DRY_RUN=true` guard, `/healthz`, `/readyz` (rejects `not_ready`; degraded requires `--allow-degraded` + checks payload), `/metrics` (validates `text/plain` Content-Type + Prometheus text format + forbidden label scanning).
- `src/schemas/ops.py`: `DeploymentFailureReason` (16-value StrEnum), `DeploymentCheckStatus`, `DeploymentProbeResult`, `DeploymentValidationReport`, `ComposeServiceStatus`, `HTTPProbeResult`, `DryRunGuardResult`, `MetricsInspectionResult` — all frozen Pydantic V2.
- `docs/runbooks/digitalocean-droplet-deployment.md`: complete 12-section runbook (Droplet creation, hardening, Docker install, deploy, SQLite backup, service management, healthcheck config, observability endpoints, log rotation, update procedure, troubleshooting, security checklist).
- `docs/system_architecture.md`: trailing whitespace stripped (git diff --check clean).
- 38 new integration tests, 1079 total, 92% coverage. Branch: `feat/wi-48-digitalocean-droplet-deployment-hardening`, commit: `5edad0a`.

WI-49 — Secure Remote Operator Dashboard Access: COMPLETE.
- 7 new Pydantic schemas in `src/schemas/ops.py`: `DashboardAccessMode` (StrEnum), `DashboardRuntimeConfig`, `DashboardDatabaseTarget`, `DashboardTunnelSpec`, `DashboardReadOnlyCheck`, `DashboardExposureCheck`, `DashboardAccessValidationReport` — all frozen V2.
- `src/ui/dashboard.py`: `DASHBOARD_DB_PATH` env var support with read-only SQLite URI mode (`file:<path>?mode=ro`), `_resolve_db_uri()` helper. Removed `reasoning_log`/`reasoning` from both SQL queries (`fetch_decision_log`) and both table renderers (`render_audit_table`, `render_decision_table`).
- `docker-compose.yml`: profile-gated `dashboard` service (`--profile dashboard`), mounts `poly_oracle_data:/data`, `DASHBOARD_DB_PATH=/data/poly_oracle.db`, Streamlit binds `0.0.0.0` inside container with host publish restricted to `127.0.0.1:8501`.
- `.env.example`: `DASHBOARD_DB_PATH` commented entry.
- `docs/runbooks/streamlit-ssh-tunnel.md`: complete runbook with SSH tunnel command, verification, shutdown, and reverse proxy appendix.
- 91 new tests (76 unit + 15 integration), 1171 total, 93% coverage. Branch: `feat/wi-49-secure-remote-operator-dashboard-access`, merge commit on `develop`.

WI-50 — Telegram Operational Alert Bridge: COMPLETE.
- 7 new typed schemas in `src/schemas/ops.py`: `OperationalAlertType` (5-value StrEnum), `OperationalAlertSeverity`, `OperationalAlertStatus`, `OperationalAlert`, `OperationalAlertState`, `OperationalAlertEvaluation`, `OperationalAlertDispatchResult`, `OperationalAlertConfig` — all frozen Pydantic V2 with secret-detection validators.
- `src/observability/operational_alerts.py`: `OperationalAlertBridge` with per-type state tracking, sustained threshold evaluation (default 5 min), configurable cooldown dedupe (default 10 min), and non-blocking Telegram dispatch via existing `TelegramNotifier`. Evaluates readiness degraded, WebSocket stale (disconnected or PONG stale), and circuit breaker open/closed transitions using typed boolean state (not log parsing).
- `src/core/config.py`: 5 new config fields (`enable_operational_alerts`, `enable_startup_alert`, `operational_readiness_degraded_threshold_sec`, `operational_websocket_stale_threshold_sec`, `operational_alert_cooldown_sec`) — all Decimal-typed.
- `src/orchestrator.py`: `OperationalAlertBridge` initialized after Telegram notifier, `_operational_alert_loop` as background task (60s interval), startup alert dispatched in `start()`.
- `docs/runbooks/telegram-operational-alerts.md`: complete 5-section runbook covering configuration, verification, troubleshooting, and operational notes.
- Secret-free payload enforcement: `_scan_forbidden_payload()` rejects private keys, API keys, Telegram tokens, condition IDs, token IDs, and secret-like substrings at Pydantic boundary.
- 100 new tests (82 unit + 18 integration), 1271 total, 93% coverage. Branch: `feat/wi-50-telegram-operational-alert-bridge`, merge commit on `develop`.

WI-51 — 24/7 Paper-Trading Soak Test and Runbook: COMPLETE.
- `scripts/ops/collect_soak_evidence.py`: audit-grade soak evidence collector with mandatory runtime `dry_run=true` confirmation from `/readyz`, bounded Docker restart-count inspection, health/readiness validation, read-only SQLite persistence checks, post-recovery verification, project-root-constrained report output, and terminal `SoakEvidenceReport` validation.
- `src/schemas/soak.py`: bounded Pydantic V2 soak schemas/enums for verdicts, probe statuses, failure reasons, readiness statuses, recovery methods, service status, health, database, recovery, and full evidence reports; `live_trading_authorized` is `Literal[False]`.
- `src/observability/health.py` and `src/observability/health_server.py`: readiness now exposes uppercase typed statuses and runtime dry-run state without leaking secrets or token context.
- `src/orchestrator.py`: health server receives runtime `dry_run` from `AppConfig`, preserving the dry-run audit trail through `/readyz`.
- `docs/runbooks/paper-trading-soak-test.md`: operator flow records soak start and DB baseline under `/data`, requires baseline-size input, documents recovery evidence commands, and aligns readiness expectations to `READY`.
- 78 WI-specific tests, 1349 total, 93% coverage. Branch: `feat/wi-51-24-7-paper-trading-soak-test-and-runbook`, final commit: `517f0f2`.

Phase 13 planned 2026-05-05 — Real-Data Validation & 24/7 Readiness:
- `docs/PRD-v13.0.md` created as the Phase 13 planning source.
- WI-43 — Historical Polymarket Dataset Pipeline: COMPLETE. Built resolved-market historical data pipeline with lookahead-safe separation: `src/backtesting/schemas.py`, `src/backtesting/polymarket_history_client.py`, `src/backtesting/historical_dataset.py`, `scripts/build_historical_dataset.py`. Produces BacktestDataLoader-compatible JSON snapshots with `condition_id` and `market_end_date`, plus per-market outcomes files. CLI exits non-zero on source failures. 63 new tests (60 unit + 3 integration), 741 total, 93% coverage.
- WI-44 — Real-Data Backtest Validation: COMPLETE. Built validation layer on top of BacktestRunner: `LiveReadinessVerdict` enum (6 verdicts), `BacktestValidationReport` schema with realized EV calibration, confidence calibration buckets using PnL-based win detection (not gatekeeper_result), data-quality gating at >10% bad fraction, and `derive_verdict()` with deterministic ordering. Added `realized_pnl_usdc` field to `BacktestDecision` for correct win/loss tracking. CLI `scripts/run_real_data_backtest.py` constrained to `docs/backtests/` output, markdown report uses Decimal (no float). 80 new tests (64 unit + 16 integration), 819 total, 93% coverage.
- WI-45 — Real Grok Sentiment Integration: COMPLETE. Replaced mock-first sentiment with live xAI/Grok API path behind `grok_live_enabled` config gate. Added `GrokLiveConfig`, `GrokRequestEnvelope`, `GrokResponseEnvelope`, `GrokFailureReason` (StrEnum) typed models. `GrokClient` enforces category gate (CRYPTO/POLITICS only), per-attempt budget capping (`remaining_budget` from total chain budget), `SAFETY_REFUSAL` detection (9 patterns), and `json.loads(parse_float=Decimal)` to prevent float at Pydantic boundary. `SentimentResponse._parse_decimal` rejects raw Python float. ClaudeClient audit trail propagates typed `GrokFailureReason` via `FALLBACK` status. 49 new tests (39 unit + 10 integration), 868 total, 93% coverage.
- WI-46 — 24/7 Connectivity Hardening: COMPLETE. Hardened `CLOBWebSocketClient` with bounded exponential backoff (initial 1s, max 60s, ±25% jitter), typed `WebSocketHealthSnapshot` tracking 9 health fields (connection state, timestamps, reconnect/failure counts, error reason, asset count), explicit market-closed/inactive/expired detection via `MarketLifecycleState` + `MarketClosedSkipReason`, PONG timeout detection triggering reconnect. Added `HealthServer` using `asyncio` stdlib HTTP with `/healthz` (always 200) and `/readyz` (200/503 based on DB+WS state with configurable grace window). Wired health server lifecycle into `Orchestrator.start()`/`shutdown()`. New config: `ws_reconnect_*`, `ws_pong_timeout_seconds`, `ws_consecutive_failure_degraded_threshold`, `health_server_*`, `readiness_grace_window_seconds`. All health endpoints read-only — zero secrets, wallet, prompt, or token ID exposure. 88 new unit tests, 956 total, 93% coverage.
- WI-47 — Prometheus Metrics Export: COMPLETE. Exposed `GET /metrics` in Prometheus text exposition format via lightweight asyncio stdlib HTTP server. `MetricsRegistry` with lock-protected counters/gauges: decisions per hour, BUY/HOLD/SKIP counts, execution results by `ExecutionAction`, evaluation/context/routing latency, WS reconnect/error counts, heartbeat age, active market count, backtest readiness verdict. All numeric values use `Decimal`. Low-cardinality label enforcement at Pydantic boundary — `condition_id`, `token_id`, `wallet_address`, `prompt_text`, `reasoning_text`, `exception_message`, `secret` keys rejected. `MetricsServer` on configurable host:port with 200/404/405/500 paths and graceful lifecycle in orchestrator. Background `_metrics_sync_loop` updates WS-derived gauges every 15s. Metrics collection is read-only — no trade authorization, no LLMEvaluationResponse bypass, no dry_run weakening, no DB writes. 85 new unit tests, 983 total, 93% coverage.
- **Paper Trading Activation (2026-05-06):**
  - Grok live mode activated (`GROK_LIVE_ENABLED=True`, `GROK_MOCKED=False`).
  - Claude upgraded to `claude-sonnet-4-20250514`.
  - System verified starting in `dry_run=True` with live Grok and Sonnet-4.
  - HealthServer and MetricsServer active.
- WI-44 through WI-47 business-logic and implementation-prompt deliverables generated ahead of implementation per user request.
- Phase-level kill criterion: if real-data backtest does not show defensible edge, `DRY_RUN=false` remains prohibited and Phase 14 must address strategy/model/risk redesign before live trading.
- Per AGENTS.md PRD boundary, business-logic and implementation-prompt deliverables were not generated during `/prd`; they are created one at a time via `/wi-start`.

Phase 12 sealed 2026-04-15 — Command Center Dashboard (WI-39 through WI-42):
- `src/ui/__init__.py` and `src/ui/dashboard.py` created as the project's read-only operator UI.
- Dark-themed Streamlit dashboard (`"Poly-Oracle Command Center"`) with sidebar DB vitals, manual refresh, and four content sections.
- `render_metrics()` — `st.columns(5)` layout: Realized PnL, Win Rate, Open Exposure, Total Decisions, Active Positions; graceful zero-state on empty DB.
- Plotly `px.line` PnL-over-time chart with mock-curve fallback when no closed positions exist; visual distinction (dotted vs solid line).
- `render_decision_table()` — last 20 LLM decisions with confidence%, EV%, Kelly% columns; adapter layer handles both `decisions` and `agent_decision_logs` table schemas.
- `render_market_watch()` — all tracked markets sorted by 24h volume descending; adapter layer handles both `markets` and `market_snapshots` table schemas.
- `@st.cache_data(ttl=30)` applied to all DB query functions per PRD-v12.0 §3.2.
- Added UI dependencies to both manifests: `streamlit>=1.32.0`, `pandas>=2.0.0`, `plotly>=5.20.0`.
- Fixed pre-existing `PositionStatus` import collision (16 pytest collection errors): moved consumers to `from src.schemas.position import PositionStatus` in `exit_strategy_engine.py`, `position_tracker.py`, `orchestrator.py`, and two integration test files.
- Final regression gate: 678 passed, 94% coverage.

WI-40 completion 2026-04-15 (Metrics View + RED-phase import/dependency fixes):
- Resolved `PositionStatus` ownership violation that caused collection/import cascade failures by moving consumers to `from src.schemas.position import PositionStatus`.
- Fixed import sites:
  - `src/agents/execution/exit_strategy_engine.py`
  - `src/agents/execution/position_tracker.py`
  - `src/orchestrator.py`
  - `tests/integration/test_circuit_breaker_integration.py`
  - `tests/integration/test_telegram_notifier_integration.py`
- Added missing UI dependencies in both manifests: `streamlit>=1.32.0`, `pandas>=2.0.0`, `plotly>=5.20.0`.
- Refactored `src/ui/dashboard.py::render_metrics()` to `st.columns(5)` with all PRD metrics: Realized PnL, Win Rate, Open Exposure, Total Decisions, Active Positions.
- Regression gates after WI-40:
  - `.venv/bin/python -m pytest tests/ -q --asyncio-mode=auto` → 678 passed
  - `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` → 94%

Recent hotfixes (dry-run boot-to-evaluation stabilization + WS bugs, 2026-04-03):
- `NonceManager.initialize()` and `sync()` short-circuit when `dry_run=True` — zero RPC calls, nonce set to 0
- `GammaRESTClient` query updated: `?active=true&closed=false&limit=100&order=volume24hr&ascending=false` (was unbounded, returned empty)
- `MarketMetadata.token_ids` field validator handles Gamma API's JSON-encoded string `clobTokenIds` (was silently dropping all markets)
- `GammaRESTClient` parse loop now logs per-market validation errors and skipped count (was bare `except: continue`)
- `CLOBWebSocketClient` subscription fixed: uses `assets_ids` (token IDs) instead of `market_ids` (was rejected as `INVALID OPERATION`)
- `CLOBWebSocketClient._handle_message()` normalises list-wrapped WS frames to `list[dict]` before processing (was crashing on `.get()`)
- Orchestrator resolves token IDs from gamma cache and passes them to WS client via `set_assets_ids()` before `run()`
- `AppConfig` dry-run boot fallbacks: `wallet_address=0x1111...1111`, `wallet_private_key=0x1111...1111`, `polygon_rpc_url=https://rpc.ankr.com/polygon`
- Alembic test/runtime isolation hardened: an explicitly configured Alembic URL now wins over ambient `.env` `DATABASE_URL`
- **WS Client Bug Fixes (2026-04-03):**
  - BUG 1: `yes_token_id` propagation — added `token_id_to_yes_token_id` dict parameter to `CLOBWebSocketClient`, implemented `set_token_id_mapping()` setter, extended `MarketSnapshotSchema` and `MarketSnapshot` ORM to carry `yes_token_id` through validation to DB
  - BUG 2: Midpoint computation — fixed `_process_event()` to handle three frame types (book with bids/asks lists, price_change with direct best_bid/best_ask, last_trade_price), added fallback to top-level fields when lists are empty
  - BUG 3: Diagnostic logging — added `outbound_message` and `subscription_audit` logs for debugging INVALID_OPERATION server errors
  - `DataAggregator` now captures `yes_token_id` from incoming `MarketSnapshot` and includes it in the output payload dict, closing the propagation gap that caused `ClaudeClient` to always log "Missing yes_token_id"
  - Orchestrator token_id mapping corrected: all token IDs (YES and NO) now map to `token_ids[0]` (YES token); condition_id also added as key for book frames that lack `asset_id`
  - WS client `_process_event()` now falls back to condition_id for `yes_token_id` resolution when `asset_id` is absent in the frame
  - WS client skips snapshot emission when `best_bid <= 0` or `best_ask <= 0` on `price_change`/`book` frames (prevents midpoint=0.0 noise)

Hotfix 2026-04-04 (shared budget bypass in dry run):
- **`_CHAIN_BUDGET` (2.0s) blocks Claude evaluation even in dry run:** `ClaudeClient._process_evaluation()` consumed the shared wall-clock budget across Grok sentiment fetch + primary Claude call + reflection. In production this is a safety guard, but it also triggered `asyncio.TimeoutError("Primary evaluation exceeded shared budget.")` during dry-run testing/debugging even when Grok was mocked. Fixed by introducing `_CHAIN_BUDGET_DRY_RUN: float = 60.0` — when `dry_run=True` the 60s budget applies so the full evaluation chain (primary + reflection) completes. When `dry_run=False` the production 2s budget remains enforced. Reflection fallback for budget exhaustion returns REJECTED → conservative HOLD, preserving the safety invariant.
- **Missing `yes_token_id` column in `market_snapshots` table:** `yes_token_id` was added to the SQLAlchemy ORM model (`src/db/models.py`) but no Alembic migration was ever generated. Created `migrations/versions/0005_add_yes_token_id_to_market_snapshots.py` and applied `alembic upgrade head`. This fixed `sqlite3.OperationalError: table market_snapshots has no column named yes_token_id` during orchestrator startup.

Hotfix 2026-04-14 (WebSocket heartbeat INVALID OPERATION fix):
- **`CLOBWebSocketClient._heartbeat()` sending JSON instead of plain text:** The heartbeat was sending `{"type": "heartbeat"}` (JSON) which Polymarket CLOB rejected with `INVALID OPERATION`. Fixed to send the plain text string `"PING"` as required by Polymarket's WebSocket protocol. Server automatically responds with `"PONG"`. Added PONG handling in `_handle_message()` to silently acknowledge server responses. Enhanced error handling with specific `websockets.ConnectionClosed` catch and structlog warnings. Added test `test_ws_pong_response_is_handled` to verify PONG handling.

WI-30 completion 2026-04-15 (Global Portfolio Exposure Limits):
- Added `ExposureValidator` pre-routing gate in `Orchestrator._execution_consumer_loop()` to enforce portfolio-level exposure before `ExecutionRouter.route()`.
- Added repository exposure aggregation helper: `PositionRepository.get_total_open_exposure_usdc()`.
- Added SQLite-backed exposure summing path (`SUM(order_size_usdc)` for `status='OPEN'`) with `Decimal("0")` fallback when no open rows exist.
- Breaches short-circuit with typed skip result: `ExecutionResult(action=SKIP, reason="exposure_limit_exceeded")`.
- Full regression after WI-30 wiring: `644 passed`; coverage maintained at `94%`.

WI-31 completion 2026-04-15 (Live Wallet Balance Checks):
- Added `WalletBalanceProvider` in `src/agents/execution/wallet_balance_provider.py` with async httpx JSON-RPC reads for:
  - `eth_getBalance` (native MATIC in WEI)
  - `eth_call` `balanceOf(address)` against Polygon USDC proxy (6-decimal USDC normalization)
- Added `BalanceCheckResult` schema in `src/schemas/web3.py` with Decimal-only, float-rejecting validators on balance threshold/value fields.
- Added `AppConfig` fields: `enable_wallet_balance_check`, `min_matic_balance_wei`, `min_usdc_balance_usdc`.
- Wired wallet balance gate in `Orchestrator._execution_consumer_loop()` **after WI-30 exposure validation and before WI-29 gas checks**.
- Confirmed insufficiency emits typed skip: `ExecutionResult(action=SKIP, reason="insufficient_wallet_balance")`.
- Enforced fail-open semantics on RPC failures (`httpx.RequestError`, `httpx.HTTPStatusError`): gate returns `check_passed=True`, `fallback_used=True` and evaluation proceeds.
- WI-31 targeted suite green (`5 passed`), full regression green (`649 passed`), coverage maintained at `94%`.

WI-33 completion 2026-04-15 (Backtesting Framework):
- Added frozen WI-33 schemas in `src/schemas/execution.py`: `BacktestConfig`, `BacktestDecision`, `BacktestMarketStats`, `BacktestReport`.
- Added `src/backtest_runner.py` with:
  - `BacktestDataError`
  - `BacktestDataLoader` (historical JSON parsing, required-field validation, date filtering, strict chronological replay ordering)
  - `BacktestRunner` (hard `dry_run=True` invariant, sequential offline replay path, Gatekeeper validation + dry-run routing, Decimal-only report metrics, zero DB writes)
  - CLI entrypoint: `python -m src.backtest_runner --data-dir <dir> [--config <json|yaml>] [--output <json>]`
- WI-33 targeted gate green:
  - `.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi33_backtest_data_loader.py tests/integration/test_wi33_backtest_runner.py -v` → 29 passed
- Phase 10 regression gate green:
  - `.venv/bin/pytest --asyncio-mode=auto tests/ -q` → 678 passed
  - `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` → 94%

---

## Phase 4: Cognitive Architecture

### Work Items

- [x] **WI-11 — Market Router** (completed 2026-03-26)
  - `MarketCategory` enum (`CRYPTO | POLITICS | SPORTS | GENERAL`) in `src/schemas/llm.py`
  - `ClaudeClient._route_market()` — async keyword/pattern classification, no extra LLM call
  - `PromptFactory.build_evaluation_prompt(category=...)` — injects domain-specific persona preamble
  - Gatekeeper (`LLMEvaluationResponse`) remains final validation gate regardless of route
  - Key files: `src/schemas/llm.py`, `src/agents/context/prompt_factory.py`, `src/agents/evaluation/claude_client.py`

- [x] **WI-12 — Chained Prompt Factory** (completed 2026-03-26)
  - `SentimentResponse` schema with `Decimal` sentiment_score, int tweet_volume_delta, str top_narrative_summary
  - `GrokClient` async interface (mock-first, 2.0s timeout, httpx-ready, fallback on all failures)
  - `PromptFactory` injects `### SENTIMENT ORACLE (LAST 60 MIN)` block with sentiment values
  - `ClaudeClient._fetch_sentiment()` — category-gated Grok calls (CRYPTO/POLITICS only)
  - Normalized audit logging: `{status, reason, sentiment_score, tweet_volume_delta, top_narrative_summary}`
  - Gatekeeper (`LLMEvaluationResponse`) remains terminal gate; sentiment is upstream cognitive signal only
  - 8 integration tests (RED→GREEN), 115 total tests pass, zero regression
  - Key files: `src/schemas/llm.py`, `src/agents/evaluation/grok_client.py`, `src/agents/context/prompt_factory.py`, `src/agents/evaluation/claude_client.py`, `src/core/config.py`

- [x] **WI-13 — Reflection Auditor** (completed 2026-03-26)
  - Mandatory reflection pass after Stage B and before Gatekeeper validation
  - Enforces conservative HOLD path on bias/contradiction/timeout; ADJUSTED path is single-pass
  - Reflection artifacts persisted in decision audit log envelope; 119 tests passing

### Phase 4 Completion Gate

- [x] WI-12 implemented, tests pass (115 passed), no coverage regression ✅
- [x] WI-13 implemented, tests pass (119 passed), no coverage regression
- [x] `STATE.md` updated: version `0.4.0`, status `Phase 4 Complete`
- [ ] PRs merged to `develop` ✅, then `develop → main`

---

## Phase 5: Market Data Integration

### Work Items

- [x] **WI-14 — Polymarket Market Data Client** (completed 2026-03-26)
  - `PolymarketClient` read-only async client in `src/agents/execution/polymarket_client.py`
  - `MarketSnapshot` Pydantic model with Decimal-typed bid/ask/midpoint/spread
  - `fetch_order_book(token_id)` async method via official `pyclob` SDK (500ms timeout)
  - Decimal-only midpoint: `(best_bid + best_ask) / Decimal("2")`, no float in money path
  - Non-positive prices (≤ 0), crossed books, missing/malformed fields → `None` (non-tradable)
  - `ClaudeClient._process_evaluation` fetches fresh market data before `PromptFactory.build_evaluation_prompt`
  - Missing `yes_token_id` or fetch failure → conservative skip, no execution enqueue
  - `LLMEvaluationResponse` Gatekeeper remains terminal gate, unchanged
  - 34 new tests (24 unit + 6 integration + 4 MAAP fixes), 153 total, 91% coverage
  - Key files: `src/agents/execution/polymarket_client.py`, `src/agents/evaluation/claude_client.py`, `pyproject.toml`

- [x] **WI-15 — Wallet Signer** (completed 2026-03-27)
  - `TransactionSigner` is the single canonical WI-15 signer in `src/agents/execution/signer.py`
  - `KeyProvider` protocol: vault or encrypted keystore only — no `os.environ`, no `.env`
  - `SignRequest` Pydantic model: chain_id=137 enforcement, Decimal-only amounts, float rejected at boundary
  - `SignedArtifact` typed output: signature, owner, signed_at_utc, key_source_type
  - `sign_order_secure()` async WI-15 entry point, fail-closed, no transmission/broadcast capability
  - Source type enforcement: rejects all key sources except `vault` and `encrypted_keystore`
  - Address mismatch guard: derived key must match expected_address
  - Module isolation: zero imports from evaluation, context, or market-data modules
  - Orchestrator dry_run gate: `TransactionSigner` not constructed when `dry_run=True`
  - 46 WI-15 tests (31 unit + 15 integration) + 29 async fixture fixes, 200 total, zero regression
  - Key files: `src/agents/execution/signer.py`, `src/orchestrator.py`

- [x] **WI-16 — Execution Router** (completed 2026-03-27)
  - `ExecutionRouter` is the canonical WI-16 execution orchestrator in `src/agents/execution/execution_router.py`
  - `ExecutionResult` / `ExecutionAction` typed routing contract added in `src/schemas/execution.py`
  - Entry gate skips non-BUY and low-confidence decisions before any upstream order-book, bankroll, or signer call
  - Decimal-only Kelly sizing: `edge = midpoint - threshold`, `odds = (1 - midpoint) / midpoint`, `kelly_scaled = (edge / odds) * config.kelly_fraction`
  - Slippage guard rejects when `best_ask > midpoint_probability + max_slippage_tolerance`
  - Order size capped at `min(kelly_fraction * bankroll, max_order_usdc)` with `maker_amount = int(order_size * Decimal("1e6"))`
  - `dry_run=True` returns a typed `DRY_RUN` result with a full `OrderData` payload and never calls `sign_order()`
  - `signer=None` is tolerated in dry run and returns `FAILED(reason="signer_unavailable")` when live routing is attempted without a signer
  - New config: `max_order_usdc=Decimal("50")`, `max_slippage_tolerance=Decimal("0.02")`
  - 19 new WI-16 tests (4 unit + 15 integration), 230 total, 92% coverage, full regression green
  - Key files: `src/agents/execution/execution_router.py`, `src/schemas/execution.py`, `src/core/config.py`, `src/core/exceptions.py`, `src/orchestrator.py`

- [x] **WI-18 — Bankroll Sync** (completed 2026-03-27)
  - `BankrollSyncProvider` is the canonical WI-18 balance reader in `src/agents/execution/bankroll_sync.py`
  - Read-only Polygon USDC `balanceOf` call only; no `approve`, `transfer`, `transferFrom`, or state mutation
  - Typed `BalanceReadRequest` / `BalanceReadResult` contracts enforce chain_id `137`, canonical USDC proxy, and Decimal-only balance fields
  - `asyncio.wait_for(..., timeout=0.5)` wraps the live RPC read; timeout and RPC failures raise `BalanceFetchError`
  - `dry_run=True` returns `AppConfig.initial_bankroll_usdc` as a mock balance before any `Web3` construction or RPC contact
  - `BankrollPortfolioTracker.get_total_bankroll()` now delegates to `BankrollSyncProvider.fetch_balance()` for live Kelly bankroll
  - `Orchestrator` wires `BankrollSyncProvider` into `BankrollPortfolioTracker` at startup; queue topology unchanged
  - 11 new WI-18 tests (8 unit + 3 integration), 211 total, 91% coverage, full regression green
  - Key files: `src/agents/execution/bankroll_sync.py`, `src/agents/execution/bankroll_tracker.py`, `src/orchestrator.py`, `src/core/exceptions.py`

### Phase 5 Completion Gate

- [x] WI-14 implemented and merged into `develop`
- [x] WI-15 implemented and merged into `develop`
- [x] WI-16 implemented and merged into `develop`
- [x] WI-18 implemented and merged into `develop`
- [x] Full regression green: 230 tests passing
- [x] Coverage maintained at 92% (target ≥ 80%)
- [x] `STATE.md`, `README.md`, and `CLAUDE.md` updated for phase completion
- [x] `docs/archive/ARCHIVE_PHASE_5.md` created

---

## Phase 6: Position Lifecycle

### Work Items

- [x] **WI-17 — Position Tracker** (completed 2026-03-29)
  - `PositionTracker` persists execution outcomes as typed `PositionRecord` entries in `positions` table
  - `PositionStatus` enum (`OPEN | CLOSED | FAILED`) and `PositionRecord` Pydantic model in `src/schemas/position.py`, re-exported from `src/schemas/execution.py`
  - `Position` SQLAlchemy ORM model with `Numeric(38,18)` for all 5 financial columns, 3 indexes
  - `PositionRepository` async CRUD in `src/db/repositories/position_repository.py` (5 methods, follows `ExecutionRepository` pattern)
  - Alembic migration `0002_add_positions_table.py` (parent: `0001`)
  - `record_execution(result, condition_id, token_id) -> PositionRecord | None` — sole public async entry point
  - SKIP → `None`, EXECUTED/DRY_RUN → `OPEN`, FAILED → `FAILED` with `Decimal("0")` sentinels for None financials
  - `dry_run=True` logs full record via structlog, zero DB writes, zero session creation
  - Unreachable state guards: `EXECUTED+dry_run` and `DRY_RUN+live` log error and return `None`
  - Orchestrator: constructed in `__init__()`, called in `_execution_consumer_loop()` before dry_run gate
  - MAAP audit caught 2 orchestrator wiring defects (token_id field, dry_run bypass) — both fixed and re-cleared
  - 27 new tests (unit + integration), 257 total, 92% coverage, full regression green
  - Key files: `src/agents/execution/position_tracker.py`, `src/schemas/position.py`, `src/schemas/execution.py`, `src/db/models.py`, `src/db/repositories/position_repository.py`, `migrations/versions/0002_add_open_positions_table.py`, `src/orchestrator.py`

### Phase 6 Completion Gate

- [x] WI-17 implemented and merged into `develop`
- [x] WI-19 implemented and merged into `develop`
- [x] Full regression green: 295 tests passing
- [x] Coverage maintained at 92% (target ≥ 80%)
- [x] `STATE.md`, `README.md`, and `CLAUDE.md` updated for phase completion
- [x] `docs/archive/ARCHIVE_PHASE_6.md` created
- [ ] PRs merged to `develop` ✅, then `develop → main`

---

## Phase 7: Exit Path Decoupling

### Work Items

- [x] **WI-22 — Periodic Exit Scan** (completed 2026-03-30)
  - Added `AppConfig.exit_scan_interval_seconds: Decimal = Decimal("60")`
  - Added `Orchestrator._exit_scan_loop()` with sleep-first cadence:
    `await asyncio.sleep(float(self.config.exit_scan_interval_seconds))`
  - Added orchestrator task registration:
    `asyncio.create_task(self._exit_scan_loop(), name="ExitScanTask")`
  - Removed inline `scan_open_positions()` call from `_execution_consumer_loop()`
  - New structlog events:
    - `exit_scan_loop.completed` (`total`, `exits`, `holds`, `interval_seconds`)
    - `exit_scan_loop.error` (`error`)
  - Preserved invariants:
    - `ExitStrategyEngine`, `ExecutionRouter`, `PositionTracker`, and schemas unchanged
    - Queue topology unchanged (`market_queue -> prompt_queue -> execution_queue`)
    - `dry_run` write gate remains inside `ExitStrategyEngine` internals
  - Test additions:
    - `tests/unit/test_exit_scan_loop.py` (8 tests)
    - `tests/integration/test_exit_scan_integration.py` (5 tests)
  - Regression:
    - `pytest --asyncio-mode=auto tests/ -q` → 308 passed
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` → 93%

- [x] **WI-20 — Exit Order Router** (completed 2026-03-30)
  - Added `ExitOrderRouter` in `src/agents/execution/exit_order_router.py`
  - Added `ExitOrderAction` (`SELL_ROUTED | DRY_RUN | FAILED | SKIP`) and frozen `ExitOrderResult` with float-rejecting Decimal validators
  - Added `ExitRoutingError` to exception taxonomy in `src/core/exceptions.py`
  - Added `AppConfig.exit_min_bid_tolerance: Decimal = Decimal("0.01")`
  - Implemented SELL-only exit routing path:
    - Entry gate skip for `should_exit=False` and `exit_reason=ERROR`
    - Fresh `fetch_order_book(position.token_id)` lookup (token_id, never condition_id)
    - Exit bid floor guard (`best_bid < exit_min_bid_tolerance` fails closed)
    - Decimal-only sizing from position metadata:
      - `token_quantity = order_size_usdc / entry_price`
      - `maker_amount = int(token_quantity * Decimal("1e6"))`
      - `taker_amount = int((token_quantity * best_bid) * Decimal("1e6"))`
    - `dry_run=True` returns full payload without signing
    - `signer=None` live guard and signing-exception fail-closed handling
  - Orchestrator wiring:
    - `ExitOrderRouter` constructed in `Orchestrator.__init__()`
    - `_exit_scan_loop()` now routes actionable exits, catches per-exit routing errors, and continues (fail-open)
    - Exit broadcast attempted only when `SELL_ROUTED`, `signed_order` exists, `dry_run=False`, and broadcaster is present
  - Test additions:
    - `tests/unit/test_exit_order_router.py` (14 tests)
    - `tests/integration/test_exit_order_router_integration.py` (9 tests)
  - Regression:
    - `pytest --asyncio-mode=auto tests/ -q` → 331 passed
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` → 93%

- [x] **WI-21 — Realized PnL & Settlement** (completed 2026-03-30)
  - Added `PnLCalculator` in `src/agents/execution/pnl_calculator.py`
  - Added frozen `PnLRecord` schema with float-rejecting Decimal validators in `src/schemas/execution.py`
  - Added `PnLCalculationError` to exception taxonomy in `src/core/exceptions.py`
  - Extended `PositionRecord` with optional settlement fields:
    - `realized_pnl: Decimal | None`
    - `exit_price: Decimal | None`
    - `closed_at_utc: datetime | None`
  - Extended `Position` ORM with nullable settlement columns:
    - `realized_pnl Numeric(38,18)`
    - `exit_price Numeric(38,18)`
    - `closed_at_utc DateTime(timezone=True)`
  - Added Alembic migration `0003_add_pnl_columns.py` (parent `0002`)
  - Added additive `PositionRepository.record_settlement()` with idempotency guard (`position.settlement_already_recorded`)
  - Orchestrator wiring:
    - `PnLCalculator` constructed in `Orchestrator.__init__()`
    - `_exit_scan_loop()` settles PnL after `ExitOrderRouter.route_exit()` when action is `SELL_ROUTED`/`DRY_RUN` with non-null `exit_price`
    - Settlement failures logged as `exit_scan.pnl_settlement_error` and do not block scan/broadcast path
  - Test additions:
    - `tests/unit/test_pnl_calculator.py` (19 tests)
    - `tests/integration/test_pnl_settlement_integration.py` (12 tests)
  - Regression:
    - `pytest --asyncio-mode=auto tests/ -q` → 362 passed
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` → 93%

### Phase 7 Progress Gate

- [x] WI-22 implemented and validated
- [x] WI-20 implemented and validated
- [x] WI-21 implemented and validated
- [x] Full phase regression + archive seal

---

## Phase 8: Portfolio Analytics

### Work Items

- [x] **WI-23 — Portfolio Aggregator** (completed 2026-03-31)
  - Added `PortfolioAggregator` in `src/agents/execution/portfolio_aggregator.py`
  - Added frozen Decimal-safe `PortfolioSnapshot` schema in `src/schemas/risk.py`
  - Added `AppConfig.enable_portfolio_aggregator: bool = False`
  - Added `AppConfig.portfolio_aggregation_interval_sec: Decimal = Decimal("30")`
  - Added `Orchestrator._portfolio_aggregation_loop()` with sleep-first cadence
  - Added conditional task registration:
    `asyncio.create_task(self._portfolio_aggregation_loop(), name="PortfolioAggregatorTask")`
  - Fail-open semantics:
    - Per-position price fetch failure logs `portfolio.price_fetch_failed`
    - Fallback to `entry_price` preserves snapshot computation
    - Loop catches iteration failures and logs `portfolio_aggregation_loop.error`
  - Snapshot audit event:
    - `portfolio.snapshot_computed`
  - Read-only guarantees:
    - Loads via `PositionRepository.get_open_positions()`
    - Zero DB writes (`INSERT/UPDATE/DELETE`) in `compute_snapshot()`
  - Regression:
    - `pytest --asyncio-mode=auto tests/ -q` → 388 passed
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` → 94%

- [x] **WI-24 — Position Lifecycle Reporter** (completed 2026-03-31)
  - Added `PositionLifecycleReporter` in `src/agents/execution/lifecycle_reporter.py`
  - Added frozen Decimal-safe `PositionLifecycleEntry` + `LifecycleReport` schemas in `src/schemas/risk.py`
  - Added additive repository read methods in `PositionRepository`:
    - `get_all_positions()`
    - `get_settled_positions()`
    - `get_positions_by_status(status)`
  - Added optional `start_date`/`end_date` filtering on `routed_at_utc` with fail-open invalid-range handling
  - Added structlog events:
    - `lifecycle.report_generated`
    - `lifecycle.report_empty`
    - `lifecycle_report_loop.error` (orchestrator loop integration)
  - Added orchestrator integration:
    - constructs `PositionLifecycleReporter` in `__init__()`
    - invokes `generate_report()` in `_portfolio_aggregation_loop()` after snapshot computation
    - independent try/except preserves fail-open semantics
  - Read-only guarantees:
    - loads via `PositionRepository.get_all_positions()`
    - zero DB writes (`INSERT/UPDATE/DELETE`) in `generate_report()`
  - Regression:
    - `pytest --asyncio-mode=auto tests/ -q` → 421 passed
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m` → 94%

- [x] **WI-25 — Alert Engine** (completed 2026-04-01)
  - Added `AlertEngine` in `src/agents/execution/alert_engine.py` (synchronous, stateless, read-only)
  - Added `AlertSeverity` enum (`INFO | WARNING | CRITICAL`) and frozen `AlertEvent` schema in `src/schemas/risk.py`
  - Added `AppConfig` thresholds:
    - `alert_drawdown_usdc: Decimal = Decimal("100")`
    - `alert_stale_price_pct: Decimal = Decimal("0.50")`
    - `alert_max_open_positions: int = 20`
    - `alert_loss_rate_pct: Decimal = Decimal("0.60")`
  - Added orchestrator integration:
    - constructs `AlertEngine` in `Orchestrator.__init__()`
    - captures snapshot/report outputs in `_portfolio_aggregation_loop()`
    - evaluates alerts only when both outputs are non-None
    - logs `alert_engine.alerts_fired`, `alert_engine.all_clear`, `alert_engine.error`
  - Preserved fail-open semantics:
    - snapshot/report failures skip alert evaluation for that cycle
    - alert evaluation exceptions are caught and logged without terminating the loop
  - Added WI-25 test suites:
    - `tests/unit/test_alert_engine.py` (33 tests)
    - `tests/integration/test_alert_engine_integration.py` (8 tests)
  - Regression:
    - `pytest --asyncio-mode=auto tests/ -q` → 462 passed
    - `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` → 94%

### Phase 8 Progress Gate

- [x] WI-23 implemented and validated
- [x] WI-24 implemented and validated
- [x] WI-25 implemented and validated
- [x] Full phase regression + coverage gate: 462 passed, 94%
- [x] `docs/archive/ARCHIVE_PHASE_8.md` created

---

## Phase 9: Operator Safety & Telemetry

### Work Items

- [x] **WI-26 — Telegram Telemetry Sink** (completed 2026-04-01)
  - Added `TelegramNotifier` in `src/agents/execution/telegram_notifier.py`
  - Added config fields:
    - `enable_telegram_notifier: bool = False`
    - `telegram_bot_token: SecretStr = SecretStr("")`
    - `telegram_chat_id: str = ""`
    - `telegram_send_timeout_sec: Decimal = Decimal("5")`
  - Config-gated `Orchestrator` construction:
    - builds dedicated `self._telegram_client` only when feature flag and both credentials are present
    - sets `self.telegram_notifier = None` and logs `telegram.disabled` otherwise
  - Loop wiring:
    - `_portfolio_aggregation_loop()` sends each fired `AlertEvent`
    - `_execution_consumer_loop()` sends BUY-routed summaries for `EXECUTED` and `DRY_RUN`
    - `_exit_scan_loop()` sends SELL-routed summaries for `SELL_ROUTED` and `DRY_RUN`
  - Fail-open behavior:
    - `TelegramNotifier._send()` catches all exceptions and logs `telegram.send_failed`
    - orchestrator call sites use belt-and-suspenders `try/except Exception: pass`
    - `dry_run=True` prefixes messages with `[DRY RUN]` but does not suppress sends
  - Lifecycle:
    - dedicated `httpx.AsyncClient` is closed in `Orchestrator.shutdown()`
    - no new task, no new queue, no DB writes, no upstream execution mutation
  - Test additions:
    - `tests/unit/test_telegram_notifier.py` (17)
    - `tests/integration/test_telegram_notifier_integration.py` (14)
  - Regression:
    - `pytest --asyncio-mode=auto tests/ -q` → 493 passed
    - `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` → 94%

- [x] **WI-27 — Global Circuit Breaker** (completed 2026-04-01)
  - Added `CircuitBreaker` and `CircuitBreakerState` in `src/agents/execution/circuit_breaker.py`
  - Added config fields:
    - `enable_circuit_breaker: bool = False`
    - `circuit_breaker_override_closed: bool = False`
  - Config-gated `Orchestrator` construction:
    - sets `self.circuit_breaker = None` and logs `circuit_breaker.disabled` when feature flag is off
    - constructs in-memory breaker with initial `CLOSED` state when enabled
  - Entry-path wiring:
    - `_execution_consumer_loop()` checks `check_entry_allowed()` before `ExecutionRouter.route()`
    - blocked entries emit `ExecutionResult(action=SKIP, reason="circuit_breaker_open")`
    - blocked entries log `circuit_breaker.entry_blocked` and still pass through `PositionTracker.record_execution()` for audit continuity
  - Aggregation-loop wiring:
    - `_portfolio_aggregation_loop()` calls `evaluate_alerts(alerts)` after Telegram alert fan-out
    - `evaluate_alerts([])` still runs on all-clear cycles so one-shot overrides are processed without waiting for a new alert
    - CLOSED → OPEN transitions trigger Telegram execution-event summary: `CIRCUIT BREAKER TRIPPED`
  - Preserved invariants:
    - synchronous in-memory state machine only; no DB writes, no HTTP, no new queue, no new task
    - trips only on `AlertSeverity.CRITICAL` + `rule_name == "drawdown"`
    - exit path remains fully operational (`ExitStrategyEngine`, `ExitOrderRouter`, `PnLCalculator`, SELL notifications/broadcasts unchanged)
    - Gatekeeper authority unchanged; breaker is a downstream execution gate only
  - Test additions:
    - `tests/unit/test_circuit_breaker.py` (18)
    - `tests/integration/test_circuit_breaker_integration.py` (10)
  - Regression:
    - `.venv/bin/pytest --asyncio-mode=auto tests/ -q` → 521 passed
    - `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` → 94%

- [x] **WI-28 — Net PnL & Fee Accounting** (completed 2026-04-03)
  - Added Alembic migration `0004_add_fee_columns.py` with nullable `gas_cost_usdc` and `fees_usdc` on `positions`
  - Extended `Position` ORM model and `PositionRecord` / `PnLRecord` / `PositionLifecycleEntry` / `LifecycleReport` schemas with fee-aware fields
  - `PnLCalculator.settle()` now accepts optional `gas_cost_usdc` and `fees_usdc`, normalizes missing values to `Decimal("0")`, and computes `net_realized_pnl`
  - `PositionRepository.record_settlement()` persists gas and fee values through the repository-only settlement path
  - `PositionLifecycleReporter` coalesces legacy `NULL` fee fields to zero and exposes explicit gas, fee, and net-PnL aggregates
  - Preserved invariants:
    - `realized_pnl` remains gross PnL for backward compatibility
    - live settlement return values are aligned to the persisted `Numeric(38,18)` row to avoid audit/report drift
    - legacy pre-WI-28 rows deserialize with `gas_cost_usdc == Decimal("0")` and `fees_usdc == Decimal("0")`
  - Test additions:
    - `tests/unit/test_wi28_net_pnl.py` (22)
    - `tests/integration/test_wi28_net_pnl_integration.py` (6)
  - Regression:
    - `.venv/bin/pytest --asyncio-mode=auto tests/ -q` → 549 passed
    - `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` → 95%

### Phase 9 Progress Gate

- [x] WI-26 implemented and validated
- [x] WI-27 implemented and validated
- [x] WI-28 implemented and validated
- [x] Full regression green: 549 passed
- [x] Coverage maintained at 95% (target ≥ 80%)
- [x] `STATE.md`, `README.md`, and `CLAUDE.md` updated for phase completion
- [x] `docs/archive/ARCHIVE_PHASE_9.md` created

---

## Phase 10: Portfolio Controls + Concurrent Tracking

### Work Items

- [x] **WI-30 — Global Portfolio Exposure Limits** (completed 2026-04-15)
  - Added `ExposureValidator` in `src/agents/execution/exposure_validator.py`
  - Added `ExposureSummary` in `src/schemas/risk.py` (frozen, Decimal-safe)
  - Added `PositionRepository.get_total_open_exposure_usdc()` (`SUM(order_size_usdc)` on OPEN positions, `Decimal("0")` fallback)
  - Added config flags:
    - `enable_exposure_validator: bool = False`
    - `max_category_exposure_pct: Decimal = Decimal("0.015")`
  - Wired exposure gate in `Orchestrator._execution_consumer_loop()` before `ExecutionRouter.route()`
  - Breach behavior:
    - skip with `ExecutionResult(action=SKIP, reason="exposure_limit_exceeded")`
    - logs `exposure.summary_computed` and `exposure.limit_exceeded`
  - Regression:
    - `.venv/bin/pytest --asyncio-mode=auto tests/ -q` → 644 passed
    - `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` → 94%

- [x] **WI-31 — Live Wallet Balance Checks** (completed 2026-04-15)
  - Added `WalletBalanceProvider` with concurrent async balance checks via `asyncio.gather`.
  - Added RPC methods:
    - `get_matic_balance_wei()` using `eth_getBalance`
    - `get_usdc_balance_usdc()` using `eth_call` with ABI selector `0x70a08231` and 32-byte padded address.
  - Added fail-open fallback contract for RPC failures:
    - catches `httpx.RequestError` and `httpx.HTTPStatusError`
    - returns `BalanceCheckResult(check_passed=True, fallback_used=True)`.
  - Added orchestrator pre-evaluation gate order:
    - WI-30 exposure gate
    - WI-31 wallet balance gate
    - WI-29 gas gate
    - evaluation routing
  - Typed insufficient funds behavior:
    - `ExecutionResult(action=SKIP, reason="insufficient_wallet_balance")`.
  - Regression:
    - `.venv/bin/pytest --asyncio-mode=auto tests/unit/test_wi31_live_balances.py tests/integration/test_wi31_live_balances_integration.py -v` → 5 passed
    - `.venv/bin/pytest --asyncio-mode=auto tests/ -q` → 649 passed
    - `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` → 94%

- [x] **WI-32 — Concurrent Multi-Market Tracking** (completed 2026-04-14)
  - Replaced sequential `_track_single_market()` in `Orchestrator._market_tracking_loop()` with `asyncio.gather(*tasks, return_exceptions=True)` fan-out
  - Added `DataAggregator.track_market(token_ids: list[str])` — accepts list of token IDs, manages per-market subscription state via `PerMarketAggregatorState`
  - Added `CLOBWebSocketClient.subscribe_batch(assets_ids: list[str])` — multiplexed subscription via single WebSocket connection
  - Added `CLOBWebSocketClient.register_aggregator(asset_id, aggregator)` and `_aggregator_map` for frame routing via `asset_id`
  - Enhanced `CLOBWebSocketClient._handle_message()` with `asset_id`-based frame routing to per-market aggregators
  - New `AppConfig` fields: `max_concurrent_markets: int = 5`, `market_tracking_interval_sec: Decimal = Decimal("10")`, `enable_market_tracking: bool = False`
  - `MarketTrackingTask` — new optional asyncio task in `Orchestrator` (config-gated, sleep-first, fail-open)
  - `PerMarketAggregatorState` frozen Pydantic schema in `src/schemas/market.py`
  - structlog audit events: `market_tracking.fan_out`, `market_tracking.completed`, `market_tracking.gather_error`, `market_tracking.subscribed_batch`, `market_tracking.capped`, `ws.frame_unrouted`, `market_tracking_loop.error`
  - Preserved invariants:
    - Single WebSocket connection serves all markets (no per-market connections)
    - `asyncio.gather` always called with `return_exceptions=True` (fail-open)
    - `LLMEvaluationResponse` Gatekeeper unchanged
    - 4-layer pipeline topology unchanged
    - Zero DB schema changes
  - Test additions:
    - `tests/unit/test_wi32_concurrent_tracking.py` (20 tests)
    - `tests/integration/test_wi32_concurrent_tracking_integration.py` (7 tests)
  - Regression:
    - `.venv/bin/pytest --asyncio-mode=auto tests/ -q` → 620 passed
    - `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` → 94%

- [x] **WI-33 — Backtesting Framework** (completed 2026-04-15)
  - Added WI-33 schemas: `BacktestConfig`, `BacktestDecision`, `BacktestMarketStats`, `BacktestReport`.
  - Added `BacktestDataLoader` for historical CLOB JSON replay input (`{token_id}_{date}.json`), strict required-field validation, malformed-file erroring (`BacktestDataError`), and chronological sort.
  - Added `BacktestRunner` with invariant guard:
    - `config.dry_run` must be `True` at initialization or raises `RuntimeError`.
  - Offline replay path executes in strict sequence:
    - `BacktestDataLoader` → `DataAggregator` → `PromptFactory` → `ClaudeClient` → `LLMEvaluationResponse` → `ExecutionRouter(dry_run=True)`.
  - Added Decimal-safe backtest report metrics:
    - `total_trades`, `win_rate`, `net_pnl_usdc`, `max_drawdown_usdc`, `sharpe_ratio`, `per_market_stats`.
  - Added CLI support:
    - `python -m src.backtest_runner --data-dir <dir> [--config <json|yaml>] [--output <json>]`
  - WI-33 checklist:
    - [x] Schemas implemented with float-rejecting Decimal validators.
    - [x] Loader enforces malformed/missing-field errors via `BacktestDataError`.
    - [x] Replay order is strict chronological across all loaded snapshots.
    - [x] Runner enforces hard `dry_run=True` invariant.
    - [x] Gatekeeper path is invoked for each replayed snapshot.
    - [x] Execution routing is dry-run only and never live-signs/broadcasts.
    - [x] Backtest path performs zero DB writes.
    - [x] CLI writes JSON report output.
  - Regression:
    - `.venv/bin/pytest --asyncio-mode=auto tests/ -q` → 678 passed
    - `.venv/bin/coverage run -m pytest tests/ --asyncio-mode=auto && .venv/bin/coverage report -m` → 94%

### Phase 10 Progress Gate

- [x] WI-29 implemented and validated
- [x] WI-30 implemented and validated
- [x] WI-31 implemented and validated
- [x] WI-32 implemented and validated
- [x] WI-33 implemented and validated
- [x] Full phase regression green: 678 passed
- [x] Coverage maintained at 94% (target ≥ 80%)
- [x] `STATE.md`, `README.md`, and `CLAUDE.md` updated for phase completion
- [x] `docs/archive/ARCHIVE_PHASE_10.md` created
- [x] Phase 10 marked 100% COMPLETE

---

## Phase 11: Containerization + Continuous Integration

### Work Items

- [x] **WI-34 — Containerization** (completed 2026-04-15)
  - Added root container assets: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `entrypoint.sh`.
  - Implemented multi-stage image build on `python:3.12-slim-bookworm` with non-root runtime user (`appuser`, UID 1001).
  - Added migration-first startup contract: `entrypoint.sh` runs `alembic upgrade head` before delegating to process CMD.
  - Added shared named volume contract: `poly_oracle_data:/data` and runtime DB override `DATABASE_URL=sqlite+aiosqlite:////data/poly_oracle.db`.
  - Added dual service topology in Compose:
    - `orchestrator` (default, restart `unless-stopped`)
    - `backtester` (profile-gated via `backtester`)
  - Preserved invariant: zero modifications to any Python source file.

- [x] **WI-35 — Continuous Integration** (completed 2026-04-15)
  - Added `.github/workflows/ci.yml`.
  - Trigger scope:
    - `pull_request` to `develop` and `main`
    - `push` to `develop` and `main`
  - Added sequential blocking jobs:
    - `format-check`: `ruff format --check .` then `ruff check .`
    - `test` (`needs: format-check`): `pytest --asyncio-mode=auto --cov=src --cov-report=xml --cov-fail-under=94 tests/`
    - `docker-build` (`needs: test`): `docker build -t poly-oracle-agent:ci .`
  - Added pip cache with key `pip-${{ hashFiles('requirements.txt') }}` and coverage artifact upload (`coverage.xml`, 7-day retention).
  - Preserved invariants:
    - no `continue-on-error: true`
    - no secret values in workflow YAML
    - no modifications to files in `src/`, `tests/`, or `migrations/`.

### Phase 11 Progress Gate

- [x] WI-34 implemented and validated
- [x] WI-35 implemented and validated
- [x] Full regression baseline unchanged: 678 passed
- [x] Coverage baseline unchanged: 94% (target ≥ 80%)
- [x] `STATE.md`, `README.md`, and `CLAUDE.md` updated
- [x] `docs/archive/ARCHIVE_PHASE_11.md` created
- [x] Phase 11 marked 100% COMPLETE

---

## Phase 12: Command Center Dashboard

### Work Items

- [x] **WI-39 — Streamlit Core Setup** (completed 2026-04-15)
  - Created `src/ui/__init__.py` and `src/ui/dashboard.py`.
  - `st.set_page_config(title="Poly-Oracle Command Center", layout="wide")`.
  - `DB_PATH` resolved via `Path(__file__).resolve().parents[2] / "poly_oracle.db"` — read-only.
  - `render_sidebar()`: DB vitals block (`DB_CONNECTION`, `ENGINE_STATUS`, `LATENCY_MS`, `LAST_REFRESH`, `DB_FILE`), manual refresh button with `st.cache_data.clear()`.
  - `@st.cache_data(ttl=30)` on all four DB query functions.
  - Terminal dark theme injected via `st.markdown()` / inline CSS with IBM Plex Mono font and Bloomberg-palette colours.

- [x] **WI-40 — Metrics View** (completed 2026-04-15)
  - `render_metrics()` refactored to `st.columns(5)`: Realized PnL, Win Rate, Open Exposure, Total Decisions, Active Positions.
  - Delta indicators for PnL (24h), Win Rate (7-day WoW), and Exposure (24h); mock deltas rendered when no position data exists.
  - `fetch_metrics()` queries both `positions` and `decisions` tables with `COALESCE` guards; zero-state returns gracefully.
  - RED-phase fixes: resolved `PositionStatus` import collision across 3 production files + 2 test files (16 collection errors eliminated).
  - Added `streamlit>=1.32.0`, `pandas>=2.0.0`, `plotly>=5.20.0` to `requirements.txt` and `pyproject.toml`.

- [x] **WI-41 — Decision Audit Log** (completed 2026-04-15)
  - `render_decision_table()` renders last 20 decisions in `st.dataframe` with `use_container_width=True`.
  - Adapter layer handles both `decisions` schema (direct) and `agent_decision_logs` schema (joined with `market_snapshots`).
  - Column normalisation: `confidence_pct`, `expected_value_pct`, `kelly_pct` all formatted as `%.2f%%`.
  - Empty state renders `st.info("No decisions logged yet.")` without raising.

- [x] **WI-42 — Market Watch Panel** (completed 2026-04-15)
  - `render_market_watch()` renders all tracked markets sorted by `volume_24h DESC`.
  - Adapter layer handles both `markets` schema (direct) and `market_snapshots` schema (CTE with `ROW_NUMBER()` to get latest per `condition_id`).
  - Numeric formatting: `yes_price` and `no_price` at `%.4f`, `volume_24h` at `$%.2f`.
  - Empty state renders `st.info("No markets ingested yet.")` without raising.

- [x] **Plotly PnL Chart** (completed as part of Phase 12, beyond PRD floor)
  - `render_chart()` renders cumulative realised PnL using `plotly.express.line` with `template="plotly_dark"`.
  - Mock 36-hour sinusoidal curve rendered (dotted line) when no closed position history exists.
  - Live curve rendered as solid line when real data is available.

### Phase 12 Completion Gate

- [x] WI-39 implemented and validated
- [x] WI-40 implemented and validated (including RED-phase import/dependency fixes)
- [x] WI-41 implemented and validated
- [x] WI-42 implemented and validated
- [x] `streamlit run src/ui/dashboard.py` starts and displays all sections on empty DB
- [x] No `INSERT`, `UPDATE`, or `DELETE` SQL in `src/ui/`
- [x] Full regression baseline unchanged: 678 passed, 94% coverage
- [x] `STATE.md`, `README.md`, and `CLAUDE.md` updated
- [x] `docs/archive/ARCHIVE_PHASE_12.md` created
- [x] Phase 12 marked COMPLETE

---

## Active Constraints (always enforced)

1. **Decimal math** — all monetary values; no `float` in financial calculations
2. **Repository pattern** — `market_snapshots`, `agent_decision_logs`, `execution_txs`, `positions` only through their respective repositories
3. **Pydantic Gatekeeper** — `LLMEvaluationResponse` is the final validation gate; no bypass. Reflection budget exhaustion → REJECTED → conservative HOLD (audit trail persisted).
4. **No hardcoded `condition_id`** — market discovery via `MarketDiscoveryEngine` only
5. **`dry_run=True` blocks execution** — `OrderBroadcaster` enforces; always set in dev/test
6. **Async-only** — no blocking I/O in any agent task; `asyncio.Lock` for shared state
7. **Live bankroll sync** — Kelly sizing uses fresh Polygon USDC balance; `initial_bankroll_usdc` is mock-only when `dry_run=True`

---

## Key File Map (Phase 11 Snapshot)

| File | Purpose |
|---|---|
| `Dockerfile` | WI-34 multi-stage container build (`builder` + non-root `runtime`) |
| `docker-compose.yml` | WI-34 runtime topology (`orchestrator` default + profile-gated `backtester`) |
| `entrypoint.sh` | WI-34 migration-first startup (`alembic upgrade head` -> `exec "$@"`) |
| `.dockerignore` | WI-34 build-context hardening (exclude tests, caches, docs, local DBs, `.env`) |
| `.github/workflows/ci.yml` | WI-35 CI pipeline (`format-check` -> `test` -> `docker-build`) |
| `src/agents/execution/bankroll_sync.py` | `BankrollSyncProvider` — read-only Polygon USDC bankroll sync with typed request/result contracts |
| `src/agents/execution/execution_router.py` | `ExecutionRouter` — BUY-only execution routing, Decimal Kelly sizing, slippage guard, dry-run bypass |
| `src/agents/execution/signer.py` | `TransactionSigner` — canonical signer: legacy `sign_order()` + WI-15 `sign_order_secure()` |
| `src/agents/execution/polymarket_client.py` | `PolymarketClient` — read-only CLOB market data + `MarketSnapshot` |
| `src/agents/execution/position_tracker.py` | `PositionTracker` — persists execution outcomes as typed `PositionRecord` entries |
| `src/agents/execution/exit_strategy_engine.py` | `ExitStrategyEngine` — rule-based exit evaluation for open positions |
| `src/agents/execution/exit_order_router.py` | `ExitOrderRouter` — SELL-side exit routing from `ExitResult` + `PositionRecord` to signed/unsigned `OrderData` |
| `src/agents/execution/pnl_calculator.py` | `PnLCalculator` — WI-21 realized PnL computation + settlement persistence orchestration |
| `src/agents/execution/portfolio_aggregator.py` | `PortfolioAggregator` — WI-23 read-only portfolio exposure aggregation with fail-open price fallback |
| `src/agents/execution/lifecycle_reporter.py` | `PositionLifecycleReporter` — WI-24 read-only lifecycle aggregation over settled/open positions |
| `src/agents/execution/alert_engine.py` | `AlertEngine` — WI-25 deterministic rule-based alert evaluation over snapshot/report inputs |
| `src/agents/execution/telegram_notifier.py` | `TelegramNotifier` — WI-26 async Telegram Bot API sink for alerts and BUY/SELL routing summaries |
| `src/agents/execution/circuit_breaker.py` | `CircuitBreaker` — WI-27 synchronous in-memory global BUY gate that trips on CRITICAL drawdown alerts |
| `src/schemas/position.py` | `PositionRecord`, `PositionStatus` — position lifecycle schemas |
| `src/schemas/execution.py` | `ExecutionResult` / `ExecutionAction` / `ExitReason` / `ExitSignal` / `ExitResult` / `ExitOrderAction` / `ExitOrderResult` / `PnLRecord` |
| `src/schemas/risk.py` | `PortfolioSnapshot`, `PositionLifecycleEntry`, `LifecycleReport`, `AlertSeverity`, `AlertEvent` — immutable Decimal-safe analytics contracts |
| `src/db/repositories/position_repository.py` | `PositionRepository` — async CRUD for `positions` table |
| `src/db/models.py` | `Position` ORM model with `Numeric(38,18)` financial + WI-21 settlement columns |
| `migrations/versions/0002_add_open_positions_table.py` | Alembic migration adding `positions` table |
| `migrations/versions/0003_add_pnl_columns.py` | Alembic migration adding `realized_pnl`, `exit_price`, `closed_at_utc` |
| `src/schemas/llm.py` | `MarketCategory` enum + `SentimentResponse` + `LLMEvaluationResponse` Gatekeeper |
| `src/agents/context/prompt_factory.py` | `PromptFactory` — domain-aware + sentiment oracle injection |
| `src/agents/evaluation/claude_client.py` | `ClaudeClient` — WI-14 fetch + routing + sentiment + evaluation |
| `src/agents/evaluation/grok_client.py` | `GrokClient` — async sentiment oracle (mock-first, 2.0s timeout) |
| `src/agents/execution/exposure_validator.py` | `ExposureValidator` — WI-30 portfolio exposure gate with Decimal-safe aggregate/category checks |
| `src/agents/execution/wallet_balance_provider.py` | `WalletBalanceProvider` — WI-31 live wallet MATIC+USDC pre-evaluation gate with fail-open RPC fallback |
| `src/backtest_runner.py` | `BacktestDataLoader` + `BacktestRunner` + WI-33 offline replay CLI entrypoint |
| `src/core/config.py` | `AppConfig` — includes WI-30/WI-31 execution flags (`enable_exposure_validator`, `max_category_exposure_pct`, `enable_wallet_balance_check`, `min_matic_balance_wei`, `min_usdc_balance_usdc`) plus prior risk settings |
| `src/orchestrator.py` | Main entry point; includes WI-30 exposure gate, WI-31 wallet balance gate, and WI-29 gas gate ordering in `_execution_consumer_loop()` |
| `docs/PRD-v4.0.md` | Phase 4 scope and acceptance criteria |
| `docs/archive/ARCHIVE_PHASES_1_TO_3.md` | Historical invariants and completed WI index |
| `AGENTS.md` | Agent rules, class name reference, hard constraints |

WI-51 — 24/7 Paper-Trading Soak Test and Runbook: COMPLETE.
- `src/schemas/soak.py`: 9 typed Pydantic V2 soak evidence schemas (`SoakVerdict`, `SoakProbeStatus`, `SoakProbeResult`, `SoakServiceStatus`, `SoakHealthEvidence`, `SoakMetricsEvidence`, `SoakDatabaseEvidence`, `SoakRecoveryEvidence`, `SoakEvidenceReport`) — all frozen, `live_trading_authorized` always False.
- `scripts/ops/collect_soak_evidence.py`: stdlib-only soak evidence collector (8 probes) producing secret-free markdown and JSON reports under `docs/operations/` (hardcoded). Probes: dry-run guard (`.env` + runtime `/readyz`), soak duration (≥24h), Compose service status, health/readiness, metrics (Prometheus format), database persistence (growth delta via `--db-baseline-size`, soak-period decision/snapshot counts via `evaluated_at`), Telegram status, recovery testing (validated methods: `docker compose restart`/`host reboot`). `_validate_report()` validates shape; `_redact_dict()` handles strings in lists.
- `docs/runbooks/paper-trading-soak-test.md`: complete 10-section runbook covering setup, duration requirements, evidence collection, recovery testing, pass/fail criteria, 6 recovery paths, and safety statements.
- 70 new integration tests, 1341 total, 93% coverage. Branch: `feat/wi-51-24-7-paper-trading-soak-test-and-runbook`, merge commit on `develop`.
