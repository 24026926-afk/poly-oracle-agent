# STATE.md — Poly-Oracle-Agent Project State

**Last Updated:** 2026-03-26
**Version:** 0.4.0-draft
**Status:** Phase 4 Active — Cognitive Architecture

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
| Total tests | 92 (76 unit + 16 integration) |
| Coverage | 90% (target ≥ 80%) |
| Framework | `pytest` + `pytest-asyncio` |
| DB | `poly_oracle.db` (SQLite, 3 tables, Alembic-managed) |

---

## Phase 4: Cognitive Architecture

### Work Items

- [x] **WI-11 — Market Router** (completed 2026-03-26)
  - `MarketCategory` enum (`CRYPTO | POLITICS | SPORTS | GENERAL`) in `src/schemas/llm.py`
  - `ClaudeClient._route_market()` — async keyword/pattern classification, no extra LLM call
  - `PromptFactory.build_evaluation_prompt(category=...)` — injects domain-specific persona preamble
  - Gatekeeper (`LLMEvaluationResponse`) remains final validation gate regardless of route
  - Key files: `src/schemas/llm.py`, `src/agents/context/prompt_factory.py`, `src/agents/evaluation/claude_client.py`

- [ ] **WI-12 — Chained Prompt Factory**
  - Stage A: extract structured market facts from routed context (schema-validated output)
  - Stage B: probabilistic + EV/Kelly reasoning from Stage A artifacts only
  - Typed handoff contract between stages; existing Gatekeeper and audit logging preserved

- [ ] **WI-13 — Reflection Auditor**
  - Internal LLM reflection pass after Stage B, before Gatekeeper validation
  - Challenges draft decision for inconsistencies, overconfidence, unsupported assumptions
  - Output: revised candidate decision + reflection audit note persisted to `agent_decision_logs`

### Phase 4 Completion Gate

- [ ] WI-12 implemented, tests pass, no coverage regression
- [ ] WI-13 implemented, tests pass, no coverage regression
- [ ] `STATE.md` updated: version `0.4.0`, status `Phase 4 Complete`
- [ ] PRs merged to `develop`, then `develop → main`

---

## Active Constraints (always enforced)

1. **Decimal math** — all monetary values; no `float` in financial calculations
2. **Repository pattern** — `market_snapshots`, `agent_decision_logs`, `execution_txs` only through their respective repositories
3. **Pydantic Gatekeeper** — `LLMEvaluationResponse` is the final validation gate; no bypass
4. **No hardcoded `condition_id`** — market discovery via `MarketDiscoveryEngine` only
5. **`dry_run=True` blocks execution** — `OrderBroadcaster` enforces; always set in dev/test
6. **Async-only** — no blocking I/O in any agent task; `asyncio.Lock` for shared state

---

## Key File Map (Phase 4)

| File | Purpose |
|---|---|
| `src/schemas/llm.py` | `MarketCategory` enum + `LLMEvaluationResponse` Gatekeeper |
| `src/agents/context/prompt_factory.py` | `PromptFactory` — domain-aware prompt construction |
| `src/agents/evaluation/claude_client.py` | `ClaudeClient` — routing + evaluation + retry logic |
| `src/orchestrator.py` | Main entry point; spins up 5 async tasks |
| `docs/PRD-v4.0.md` | Phase 4 scope and acceptance criteria |
| `docs/archive/ARCHIVE_PHASES_1_TO_3.md` | Historical invariants and completed WI index |
| `AGENTS.md` | Agent rules, class name reference, hard constraints |
