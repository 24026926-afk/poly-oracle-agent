# PRD-v16.0 - Phase 16: Operator Clarity and Runtime Audit Trail

**Version:** 16.0
**Status:** READY FOR IMPLEMENTATION
**Phase:** 16
**Author:** Staff Architect / Quantitative Systems Engineer
**Date:** 2026-05-14
**Baseline:** Phase 15 complete - 1824 tests, 93% coverage, DigitalOcean dry-run paper-trading deployment available, LLM budget/cooldown controls and DeepSeek provider optionality implemented

---

## 1. Objective

Make every autonomous dry-run server run understandable, resumable, and reconstructable by a non-technical operator through a durable operational event ledger, deterministic human-readable narratives, incident replay, dashboard timeline visibility, and an automatic daily operations digest.

Phase 16 is an operational auditability phase. It adds a persistent, secret-safe account of runtime behavior from Phase 16 implementation onward. It must not enable live trading, change `DRY_RUN=false` behavior, add live signing or broadcasting, or weaken `LLMEvaluationResponse` as the terminal Gatekeeper before execution.

---

## 2. Scope Boundaries

**In scope:**
- SQLite-backed `operational_events` table managed by Alembic.
- Typed Pydantic V2 operational event schemas with stable enums for event type, severity, source component, and reason code.
- Append-only operational event repository with no public update/delete methods.
- Async event queue and bounded batch flush so persistence does not block WebSocket, evaluation, or execution hot paths.
- Runtime event emission for startup, shutdown, config load, market discovery, market rejection/quarantine, WebSocket health, readiness changes, LLM budget/cooldown/provider blocks, decisions, dry-run execution, circuit breaker transitions, alerts, recoveries, and bounded errors.
- Deterministic narrative layer that converts typed events and decisions into plain English summaries for operators.
- Incident replay CLI that reconstructs a bounded time window from persisted operational events.
- Streamlit dashboard activity timeline and recent runtime state panel backed by the operational ledger.
- Daily bot operations digest written to `03_Daily/YYYY-MM-DD-bot.md` without overwriting manual coding notes.
- Optional Telegram summary for the daily operations digest when Telegram alerts are enabled.
- Secret-safe logs, reports, dashboard feeds, and metrics labels.
- Decimal-only arithmetic for estimated spend, PnL, EV, price, sizing, exposure, and token-cost math.

**Out of scope:**
- Live trading.
- `DRY_RUN=false` changes.
- Live order signing or broadcasting changes.
- Any execution path that bypasses `LLMEvaluationResponse`.
- Adding presentation fields such as `human_summary` to `LLMEvaluationResponse`.
- LLM-generated runtime narratives.
- Raw prompt, private reasoning, API key, wallet key, Telegram token, token ID, condition ID, wallet address, or other high-cardinality identifier exposure in human-facing logs, reports, metrics labels, or dashboard feeds.
- Strategy optimization.
- Kelly optimization.
- DeepSeek/Claude prompt redesign.
- Historical backfill from old Docker logs.
- Full log retention or rsync hardening.
- Cryptographic hash-chain ledger.
- PostgreSQL migration.
- Public dashboard exposure.
- Generating WI business-logic or implementation-prompt deliverables during PRD creation. Those are generated one at a time via `/wi-start`.

---

## 3. Work Items

### WI-56 - Operational Event Ledger

**Goal:** Add a durable, append-only operational event ledger with typed schemas, Alembic migration, repository-only persistence, and non-blocking runtime event ingestion.

#### 3.1 File Structure

```text
src/
├── core/
│   └── config.py
├── db/
│   ├── models.py
│   └── repositories/
│       └── operational_event_repo.py
├── observability/
│   ├── operational_event_bus.py
│   └── metrics.py
├── schemas/
│   └── ops.py
└── orchestrator.py

migrations/
└── versions/
    └── 0006_add_operational_events.py

docs/
└── runbooks/
    └── operational-event-ledger.md

tests/
├── unit/
│   └── test_WI-56-operational-event-ledger.py
└── integration/
    └── test_WI-56-operational-event-ledger.py
```

#### 3.2 Core Requirements

- Create an `operational_events` table through Alembic only.
- Add Pydantic V2 schemas for operational event creation, persisted records, event metadata, batch append results, queue state, and query filters.
- Add stable enums for:
  - event type
  - severity
  - source component
  - reason code
  - event persistence status
- Initial event type coverage must include:
  - `START`
  - `SHUTDOWN`
  - `CONFIG_LOADED`
  - `MARKET_DISCOVERED`
  - `MARKET_REJECTED`
  - `MARKET_QUARANTINE`
  - `WS_CONNECTED`
  - `WS_RECONNECT`
  - `WS_PONG_STALE`
  - `READY_STATE_CHANGED`
  - `LLM_CALL_STARTED`
  - `LLM_CALL_BLOCKED`
  - `BUDGET_BLOCK`
  - `COOLDOWN_BLOCK`
  - `PROVIDER_FAILURE`
  - `DECISION_ACCEPTED`
  - `DECISION_SKIPPED`
  - `EXECUTION_DRY_RUN`
  - `CIRCUIT_BREAKER_OPEN`
  - `CIRCUIT_BREAKER_CLOSED`
  - `ALERT_SENT`
  - `ERROR_RECOVERED`
- Runtime code may append events only. It must not update or delete operational events.
- `OperationalEventRepository` must expose append/read methods only. It must not expose public update/delete methods.
- Runtime persistence must route through the repository. Agent logic must not hold raw database sessions for operational event writes.
- Use an async event bus or publisher backed by a bounded `asyncio.Queue`.
- Flush events in bounded batches with configurable batch size and flush interval.
- Queue overflow behavior must be deterministic, typed, and logged. Critical lifecycle/audit events must be prioritized over noisy diagnostic events.
- Event payloads must be bounded and structured. They must not include raw prompts, private reasoning, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, or other high-cardinality identifiers.
- Add schema boundary validators that reject forbidden secret-like and high-cardinality values in human-facing event fields.
- Metrics/log labels must remain low-cardinality: event type, severity, component, reason code, and persistence status only.
- Audit integrity failures must fail closed where the event is part of a safety-critical transition. Non-critical narrative or formatting failures must not crash the trading loop.
- Add a runbook explaining event categories, retention expectations, failure handling, and how operators should interpret the ledger.

#### 3.3 Definition of Done - WI-56

- [ ] `operational_events` exists after `alembic upgrade head`.
- [ ] Operational events are persisted only through `OperationalEventRepository`.
- [ ] The repository has no public update/delete methods.
- [ ] Event writes are append-only in runtime behavior.
- [ ] The async event queue is bounded and batch flushing is test-covered.
- [ ] Secret/high-cardinality values are rejected or redacted at schema boundaries before human-facing persistence.
- [ ] Runtime hooks emit representative lifecycle, discovery, WS, readiness, budget/cooldown, decision, execution dry-run, circuit-breaker, alert, and recovery events.
- [ ] Metrics use only low-cardinality labels.
- [ ] Tests cover migration shape, append behavior, read-window query, queue flush, queue overflow, secret rejection, reason-code validation, and disabled-event behavior.

---

### WI-57 - Deterministic Human Narratives

**Goal:** Create a separate narrative layer that converts typed operational events and decisions into deterministic plain English summaries for operators without polluting Gatekeeper or financial schemas.

#### 4.1 File Structure

```text
src/
├── observability/
│   └── operational_narratives.py
├── schemas/
│   └── ops.py
└── db/
    └── repositories/
        └── operational_event_repo.py

tests/
├── unit/
│   └── test_WI-57-deterministic-human-narratives.py
└── integration/
    └── test_WI-57-deterministic-human-narratives.py
```

#### 4.2 Core Requirements

- Do not add narrative fields to `LLMEvaluationResponse`.
- Keep cognitive/financial schemas separate from presentation/narrative schemas.
- Add presentation schemas such as:
  - `OperationalNarrative`
  - `DecisionNarrative`
  - `RuntimeNarrative`
  - `NarrativeRenderResult`
- Use deterministic mappings from typed fields to English summary text.
- Do not call any LLM to generate runtime narratives.
- Use stable English `reason_code` values for auditability.
- Human summaries must be English, deterministic, and generated from typed fields only.
- Narratives must explain:
  - what happened
  - why it happened
  - whether the bot continued, skipped, degraded, or stopped
  - what the operator should inspect next, when applicable
- Narrative rendering must be secret-safe. It must never include raw prompts, private reasoning, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, or high-cardinality identifiers.
- Example mapping:
  - `event_type: BUDGET_BLOCK`
  - `reason_code: LLM_DAILY_COST_LIMIT_EXCEEDED`
  - `human_summary: The model call was blocked because the daily LLM spending limit was already reached.`
- Unknown but valid event/reason combinations must produce conservative generic summaries that expose no raw payload values.
- Narrative failures must return typed render failures or fallback summaries. They must not crash the trading loop.

#### 4.3 Definition of Done - WI-57

- [ ] Narrative schemas exist separately from `LLMEvaluationResponse`.
- [ ] No `human_summary` or presentation field is added to `LLMEvaluationResponse`.
- [ ] Supported event types and reason codes render deterministic English summaries.
- [ ] Narrative output is stable across repeated runs with the same input.
- [ ] Unknown event/reason combinations return safe generic summaries.
- [ ] Secret/high-cardinality values never appear in narrative output.
- [ ] Tests cover budget blocks, cooldown blocks, provider failures, market rejection/quarantine, readiness changes, decisions, execution dry-run, circuit breaker transitions, alerts, recovery events, unknown mappings, and secret rejection.

---

### WI-58 - Incident Replay CLI

**Goal:** Add a CLI tool that reconstructs a time window from the operational event ledger so an operator can answer what happened during an incident.

#### 5.1 File Structure

```text
scripts/
└── ops/
    └── replay.py

src/
├── observability/
│   └── operational_narratives.py
├── db/
│   └── repositories/
│       └── operational_event_repo.py
└── schemas/
    └── ops.py

docs/
└── runbooks/
    └── incident-replay.md

tests/
├── unit/
│   └── test_WI-58-incident-replay-cli.py
└── integration/
    └── test_WI-58-incident-replay-cli.py
```

#### 5.2 Core Requirements

- Add a CLI command shaped like:

```bash
python scripts/ops/replay.py --from "2026-05-12T14:00:00Z" --to "2026-05-12T15:00:00Z"
```

- Read only from the operational event repository or a repository-backed read service.
- Output chronological human-readable replay lines.
- Support filters by:
  - severity
  - component
  - event type
  - reason code
- Include summary counts:
  - total events
  - warnings/errors
  - markets seen
  - decisions by action
  - skips by reason
  - LLM calls
  - budget blocks
  - cooldown blocks
  - provider failures
  - readiness changes
- Query timestamps must be timezone-aware and normalized to UTC.
- The CLI must fail with a clear non-zero result for invalid time windows.
- The CLI must return a valid empty-window report when no events are present.
- Output must not expose secrets, prompts, private reasoning, token IDs, condition IDs, wallet details, or high-cardinality identifiers.
- Add a runbook explaining incident replay usage after server incidents.

#### 5.3 Definition of Done - WI-58

- [ ] CLI accepts `--from` and `--to` ISO-8601 timestamps.
- [ ] CLI filters work independently and in combination.
- [ ] Replay output is chronological and human-readable.
- [ ] Summary counts are accurate and test-covered.
- [ ] Invalid timestamp windows return non-zero with safe error text.
- [ ] Empty windows produce a valid zero-event report.
- [ ] Output is secret-safe and high-cardinality-safe.
- [ ] Tests cover query filtering, sort order, summaries, invalid input, empty windows, and redaction.

---

### WI-59 - Dashboard Activity Feed

**Goal:** Add a read-only Streamlit activity timeline and current-state panel backed by the operational event ledger.

#### 6.1 File Structure

```text
src/
├── ui/
│   └── dashboard.py
├── observability/
│   └── operational_narratives.py
└── schemas/
    └── ops.py

tests/
├── unit/
│   └── test_WI-59-dashboard-activity-feed.py
└── integration/
    └── test_WI-59-dashboard-activity-feed.py
```

#### 6.2 Core Requirements

- Use the operational event ledger as the activity feed data source.
- Keep the dashboard read-only.
- Preserve existing read-only SQLite URI behavior for dashboard access.
- Add an "Activity Timeline" or "Runtime Timeline" section.
- Show chronological or reverse-chronological recent events in operator-friendly English.
- Include:
  - timestamp
  - severity
  - component
  - event type
  - reason code
  - human summary
- Add a "What is the bot doing right now?" panel based on recent operational events.
- Use Streamlit polling/cache TTL or auto-refresh behavior compatible with the existing architecture. SSE is not required.
- Provide graceful empty-state behavior when the ledger table does not exist yet or contains no events.
- Do not expose raw prompts, private reasoning, token IDs, condition IDs, secrets, wallet addresses, or wallet details.
- Avoid direct writes from dashboard code. Dashboard queries must be read-only.
- Keep dashboard labels and filtering low-cardinality.

#### 6.3 Definition of Done - WI-59

- [ ] Dashboard renders an operator-friendly runtime timeline from `operational_events`.
- [ ] Dashboard renders a current-state panel derived from recent events.
- [ ] Dashboard remains read-only at the SQLite connection level.
- [ ] Dashboard handles missing/empty event tables gracefully.
- [ ] Timeline output uses deterministic narratives from WI-57.
- [ ] Timeline output never exposes raw prompts, reasoning, secrets, token IDs, condition IDs, wallet addresses, or wallet details.
- [ ] Tests cover read-only query behavior, empty state, event formatting, current-state derivation, and redaction.

---

### WI-60 - Daily Operations Digest

**Goal:** Automatically generate a deterministic daily bot operations digest for non-technical review without overwriting manual coding daily notes.

#### 7.1 File Structure

```text
scripts/
└── ops/
    └── generate_daily_ops_digest.py

src/
├── observability/
│   ├── daily_ops_digest.py
│   └── operational_narratives.py
├── db/
│   └── repositories/
│       ├── operational_event_repo.py
│       └── position_repository.py
└── schemas/
    └── ops.py

docs/
└── runbooks/
    └── daily-operations-digest.md

tests/
├── unit/
│   └── test_WI-60-daily-operations-digest.py
└── integration/
    └── test_WI-60-daily-operations-digest.py
```

#### 7.2 Core Requirements

- Generate a daily digest file at `03_Daily/YYYY-MM-DD-bot.md`.
- The bot digest must not overwrite manual coding daily notes at `03_Daily/YYYY-MM-DD.md`.
- Digest generation must be deterministic and based on persisted events.
- Include:
  - run start/stop times
  - uptime
  - active provider
  - dry-run status
  - readiness status
  - number of markets seen
  - number of markets rejected/quarantined
  - decisions by type
  - skips by reason
  - LLM calls
  - budget blocks
  - cooldown blocks
  - provider failures
  - estimated LLM spend
  - paper PnL if available
  - top operational events
  - unresolved warnings/errors
  - recommended next operator checks
- All spend, PnL, EV, price, sizing, exposure, and token-cost math must use `Decimal`.
- Paper PnL may be omitted or marked unavailable when no repository-backed position data exists for the period.
- Send a short Telegram summary when Telegram alerts are enabled and digest Telegram delivery is configured.
- Telegram summary must use existing Telegram notifier patterns and explicit timeout behavior.
- Digest text must not include secrets, prompts, reasoning text, token IDs, condition IDs, wallet addresses, wallet keys, or high-cardinality identifiers.
- Provide a CLI entrypoint so the digest can be generated by cron/systemd timer or manually after an incident.
- Add a runbook documenting how and when the digest is generated.

#### 7.3 Definition of Done - WI-60

- [ ] Daily digest writes to `03_Daily/YYYY-MM-DD-bot.md`.
- [ ] Manual daily notes are never overwritten.
- [ ] Digest content is deterministic for the same persisted event set.
- [ ] Digest includes required operational counts and status summaries.
- [ ] Decimal arithmetic is used for estimated spend and paper PnL calculations.
- [ ] Telegram summary is optional, bounded, timeout-protected, and secret-safe.
- [ ] Digest handles no-run and partial-run days gracefully.
- [ ] Tests cover digest generation, no-overwrite behavior, empty days, partial runs, unresolved warnings/errors, Decimal spend/PnL formatting, Telegram disabled/enabled paths, and redaction.

---

## 4. Phase Definition of Done

- [ ] WI-56 through WI-60 are implemented and merged through the normal Work Item flow.
- [ ] Full test suite passes with coverage remaining at or above 80%.
- [ ] No live trading path is enabled or modified.
- [ ] `DRY_RUN=false` behavior is not changed.
- [ ] `LLMEvaluationResponse` remains the terminal Gatekeeper before execution.
- [ ] No presentation fields are added to `LLMEvaluationResponse`.
- [ ] All runtime persistence from agent logic goes through repositories.
- [ ] Alembic remains the only schema management path.
- [ ] Operational events, narratives, replay output, dashboard feed, metrics labels, Telegram summaries, and daily digest are secret-safe.
- [ ] No raw prompts, private reasoning, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, or high-cardinality identifiers appear in operator-facing surfaces.
- [ ] All money, pricing, EV, PnL, spend, sizing, exposure, and token-cost math uses `Decimal`.
- [ ] MAAP is run before commits touching `src/agents/`, `src/schemas/`, `src/db/`, `src/orchestrator.py`, or other core logic.

---

## 5. Constraints & Non-Negotiables

- Follow `AGENTS.md` hard constraints.
- Do not enable live trading.
- Do not change `DRY_RUN=false` behavior.
- Do not add live signing or broadcasting.
- Do not bypass `LLMEvaluationResponse`.
- Do not put `human_summary` or any presentation field inside `LLMEvaluationResponse`.
- Do not use an LLM to generate runtime narratives.
- All operator-facing summaries must be deterministic and derived from typed fields.
- All events must be secret-safe.
- Runtime persistence must go through repository classes, not direct DB access from agent logic.
- Metrics labels must remain low-cardinality.
- WebSocket, RPC, HTTP, and Telegram paths must use explicit timeout or bounded retry behavior.
- Source code must use Pydantic V2 at boundaries, SQLAlchemy 2.0 Async repositories, `asyncio`, and `structlog`.
- All work stays on `develop`; never commit directly to `main`.

---

## 6. Dependencies to Add

No new package dependencies are expected for Phase 16.

The implementation should use existing project dependencies:
- Pydantic V2
- SQLAlchemy 2.0 Async
- `aiosqlite`
- Alembic
- Streamlit
- Pandas
- `structlog`
- existing Telegram notifier infrastructure

---

## 7. Deliverables Summary

Per `AGENTS.md`, `/prd` creates only this phase PRD and updates `STATE.md`. WI-specific business-logic and implementation-prompt deliverables are generated one at a time only when `/wi-start {WI}` is explicitly called.

| Item | File created or updated |
|---|---|
| Phase 16 PRD | `docs/PRD-v16.0.md` |
| Project state | `STATE.md` |
| WI-56 deliverables | Deferred to `/wi-start WI-56` |
| WI-57 deliverables | Deferred to `/wi-start WI-57` |
| WI-58 deliverables | Deferred to `/wi-start WI-58` |
| WI-59 deliverables | Deferred to `/wi-start WI-59` |
| WI-60 deliverables | Deferred to `/wi-start WI-60` |

---

## 8. State & Documentation Updates on Phase Completion

When Phase 16 is complete:
- Update `STATE.md` with completed WI summaries, final test count, final coverage, merge commits, and any operational caveats.
- Update `docs/system_architecture.md` to include the operational event ledger, narrative layer, replay CLI, dashboard timeline, and daily digest.
- Add or update runbooks for:
  - operational event ledger
  - incident replay
  - dashboard runtime timeline
  - daily operations digest
- Archive Phase 16 completion records under `04_Archive/poly-oracle-agent/Phase-16/`.
- Open the required PR from `develop` to `main` after Work Item completion flow is finished.
