# STATE.md — Poly-Oracle-Agent Project State

**Last Updated:** 2026-03-26
**Version:** 0.4.0
**Status:** Phase 4 Complete — Cognitive Architecture (WI-11–WI-13 Complete)
**Next Task:** Phase 5 Preparation (WI-14)

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
| Total tests | 119 |
| Coverage | 90%+ (target ≥ 80%) |
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
| `src/schemas/llm.py` | `MarketCategory` enum + `SentimentResponse` + `LLMEvaluationResponse` Gatekeeper |
| `src/agents/context/prompt_factory.py` | `PromptFactory` — domain-aware + sentiment oracle injection |
| `src/agents/evaluation/claude_client.py` | `ClaudeClient` — routing + sentiment fetch + evaluation + retry logic |
| `src/agents/evaluation/grok_client.py` | `GrokClient` — async sentiment oracle (mock-first, 2.0s timeout) |
| `src/core/config.py` | `AppConfig` — Grok fields (api_key, base_url, model, mocked) |
| `src/orchestrator.py` | Main entry point; spins up 5 async tasks |
| `docs/PRD-v4.0.md` | Phase 4 scope and acceptance criteria |
| `docs/archive/ARCHIVE_PHASES_1_TO_3.md` | Historical invariants and completed WI index |
| `AGENTS.md` | Agent rules, class name reference, hard constraints |
