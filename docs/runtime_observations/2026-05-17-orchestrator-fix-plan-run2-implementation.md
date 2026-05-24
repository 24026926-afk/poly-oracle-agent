# Orchestrator Fix Plan Run 2 — Implementation Notes

**Date:** 2026-05-17
**Agent:** Codex
**Scope:** Implementation pass for `2026-05-17-orchestrator-fix-plan-run2.md`

## Implemented Corrections

- Stopped the active Run 2 orchestrator/dashboard processes and archived runtime logs before changing source.
- Raised documented dry-run LLM budget tuning to:
  - `LLM_HOURLY_CALL_LIMIT=240`
  - `LLM_REFLECTION_HOURLY_CALL_LIMIT=240`
  - `LLM_DAILY_CALL_LIMIT=2000`
  - `LLM_MARKET_HOURLY_CALL_LIMIT=60`
- Corrected the plan's budget-window assumption by adding explicit per-market budget expiry in `LLMBudgetGuard`; per-market limits no longer rely on the global hourly reset.
- Throttled budget-block audit emission while preserving per-occurrence metrics.
- Added budget-quarantine feedback from evaluation into the prompt queue so markets blocked by per-market budget stop churning the queue until their budget window reopens.
- Changed market-discovery rejection events to state-transition emission and added `MARKET_ELIGIBILITY_CYCLE_COMPLETED` summary events.
- Enabled SQLite WAL/busy-timeout behavior for runtime engine connections and added `busy_timeout=5000` to dashboard read connections.
- Resolved activation-time market categories with the same category resolver used by evaluation.
- Suppressed unchanged WebSocket subscription-summary logs.

## Deviations From Original Plan

- F1 used `60` per-market calls/hour instead of `30` because per-market accounting counts primary plus reflection calls.
- F1 also raised primary/reflection/global daily caps; raising only the per-market cap would have moved the same idle state to the global primary cap.
- F3 was treated as configuration/log semantics, not a code defect: the operational alert bridge logs `operational_alerts.dispatched` after Telegram send success; there is no `telegram.dispatched` log name.
- F5 was implemented in `src/db/engine.py`, not `src/db/session.py`; the latter does not exist.
- F8 was implemented in `src/orchestrator.py`, where `ws_subscribe_summary` is emitted.
- WAL pragmas were not applied inside Alembic migrations after migration smoke tests showed that changing journal mode during migration could confuse verification connections.

## Validation

- Focused WI-52/WI-53/WI-56/WI-57/WI-59/orchestrator/WS/provider/sentiment/migration tests passed.
- Ruff check passed on touched Python files.
- Ruff format check passed on touched Python files.
- `git diff --check` passed.
- Full regression passed outside the sandbox: `2312 passed`.
- Coverage-backed regression passed: `2312 passed`, coverage `93%`.

## MAAP Follow-Up

- Removed the unused `MarketQuarantineReason.BUDGET_EXHAUSTED` enum value and schema-only assertion; budget exhaustion remains modeled as `PromptQueueBackpressureReason.BUDGET_QUARANTINED`, which is the production path that drops queued prompts until the budget window reopens.
- Added operator documentation for SQLite WAL plus `synchronous=NORMAL` durability tradeoffs.
- Documented the `LLMBudgetGuard` metrics callback lock contract in the WI-52 runbook.
- Hoisted activation category resolution so aggregator registration and activation logging share the same resolved category.

## MAAP Correction Pass

- Fixed `LLMBudgetGuard.peek_budget()` to remain read-only for budget windows: it now evaluates expired hourly/daily/per-market windows as effective state without calling the mutating refresh helpers or initializing/resetting per-market counters.
- Extended prompt-queue budget quarantine to drain already queued snapshots for the quarantined market and adjust unfinished-task accounting so `join()` still reflects only remaining work.
- Added regression tests for non-mutating per-market budget peeks and queue-drain quarantine behavior.
- Focused WI-52/WI-53 correction tests passed: `220 passed`.
- Final wi-done full regression passed outside the sandbox: `2314 passed`.
- Ruff check, Ruff format check, and `git diff --check` passed.
