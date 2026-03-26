# P13-WI-13 — Reflection Auditor Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 3.1 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: `develop` only, atomic commits only

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/test-engineer.md`

## Role

You are implementing WI-13 Reflection Auditor in the Phase 4 cognitive chain.
This is a safety-critical self-correction stage between primary evaluation and Gatekeeper validation.

Your implementation must improve decision robustness without weakening financial or execution safeguards.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/PRD-v4.0.md`
4. `docs/archive/ARCHIVE_PHASES_1_TO_3.md`
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `docs/business_logic/business_logic_wi13.md`
9. `src/schemas/llm.py`
10. `src/agents/context/prompt_factory.py`
11. `src/agents/evaluation/claude_client.py`
12. `tests/integration/test_claude_client.py`
13. `tests/integration/test_sentiment_chain.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (Test-Driven Reflection)

You MUST begin with the **RED phase**.

### RED Phase Requirements

1. Create: `tests/integration/test_reflection_chain.py`
2. Implement four failing integration scenarios:
   - `APPROVED`: reflection passes candidate through and Gatekeeper remains terminal.
   - `REJECTED` via bias: reflection flags bias/contradiction and forces non-execution HOLD path.
   - `ADJUSTED` via math fix: reflection returns corrected candidate that then passes Gatekeeper.
   - `TIMEOUT` via 2.0s shared budget exhaustion: reflection returns conservative reject/hold behavior.
3. Run:
   - `pytest tests/integration/test_reflection_chain.py -v`
4. Confirm tests fail for expected reasons and document failures briefly.

**Hard stop rule:** Do NOT modify any file in `src/` until these tests fail as expected.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Add Reflection Schema Contracts

Target:
- `src/schemas/llm.py`
- `src/schemas/__init__.py` (export)

Requirements:
1. Add `ReflectionResponse` Pydantic model with strict verdict typing (`APPROVED | ADJUSTED | REJECTED`).
2. Include typed audit fields (`bias_flags`, `consistency_flags`, `risk_flags`, `audit_note`, optional correction payload).
3. Enforce Decimal-safe parsing for any risk adjustment fields (no float money adjustments).
4. Keep `LLMEvaluationResponse` unchanged as terminal Gatekeeper contract.

### Step 2 — Add Reflection Prompt Builder

Target:
- `src/agents/context/prompt_factory.py`

Requirements:
1. Add adversarial reflection prompt block/system instruction.
2. Prompt input must include: market state, sentiment artifact, primary candidate JSON, and fixed risk constants.
3. Prompt output contract must require strict JSON matching `ReflectionResponse`.
4. Preserve existing WI-11/WI-12 prompt behavior for primary evaluation path.

### Step 3 — Implement Reflection Execution in ClaudeClient

Target:
- `src/agents/evaluation/claude_client.py`

Requirements:
1. Implement `_run_reflection_audit(...)` async method.
2. Implement `_apply_reflection_verdict(...)` helper.
3. Enforce verdict behavior:
   - `APPROVED` -> pass original candidate.
   - `ADJUSTED` -> use corrected candidate (single pass only).
   - `REJECTED` -> force HOLD candidate; never enqueue trade.
4. Keep exactly one terminal validation boundary:
   - `LLMEvaluationResponse.model_validate_json(final_candidate_json)`
5. Reflection must execute before terminal Gatekeeper on all non-crash paths.

### Step 4 — Shared Latency Budget + Persistence Audit Envelope

Target:
- `src/agents/evaluation/claude_client.py`

Requirements:
1. Implement one shared 2.0s wall-clock budget across the chain segment:
   Router -> Sentiment -> Primary Evaluation -> Reflection.
2. If reflection budget is exhausted/timeout occurs, default to conservative reject/hold path.
3. Persist reflection artifacts in decision audit trail with machine-parseable envelope:
   - `[REFLECTION_AUDIT]{...}[/REFLECTION_AUDIT]`
4. Emit structured log fields:
   - `reflection_verdict`, `reflection_flags`, `reflection_reason`, `reflection_latency_ms`, `snapshot_id`.

---

## Invariants & Safety Gates (Non-Negotiable)

1. `LLMEvaluationResponse` is the absolute terminal gate before execution.
2. Reflection cannot bypass, replace, or weaken Gatekeeper validation.
3. `REJECTED` verdicts must force a HOLD candidate and must not reach execution queue.
4. No float-based money math in any reflection-driven adjustment path; Decimal invariants remain mandatory.
5. Async architecture and queue ordering remain unchanged.
6. `dry_run` behavior in Layer 4 remains untouched.

---

## Required Test Matrix

At minimum, `tests/integration/test_reflection_chain.py` must assert:
1. `APPROVED` verdict path reaches terminal Gatekeeper and routes according to final validated decision.
2. `REJECTED` verdict path yields HOLD and does not enqueue execution.
3. `ADJUSTED` verdict path uses corrected candidate and still passes terminal Gatekeeper.
4. Reflection timeout/shared-budget exhaustion yields conservative non-execution behavior with audit artifact persisted/logged.

Also run regression:
```bash
pytest tests/integration/test_reflection_chain.py -v
pytest tests/integration/test_claude_client.py -v
pytest tests/integration/test_sentiment_chain.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Deliverables

1. Failing RED test output summary.
2. GREEN implementation summary by file.
3. Passing test output summary (targeted + full suite).
4. `git diff` of staged changes for MAAP checker.

---

## MAAP Reflection Pass (Checker Prompt for Gemini 3.1 Pro)

Use the following exact prompt with the final staged `git diff`:

```text
You are the MAAP Checker for WI-13 (Reflection Auditor) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi13.md
2) docs/PRD-v4.0.md (WI-13 acceptance criteria)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Decimal violations (any float usage for monetary/risk adjustment math)
- Gatekeeper bypasses (any path to execution without terminal LLMEvaluationResponse validation)
- Business logic drift (Kelly fraction, 5 safety filters, exposure cap, or HOLD override semantics)
- Reflection policy violations (missing mandatory reflection trigger, incorrect verdict semantics, missing conservative timeout behavior)

Additional required checks:
- REJECTED verdict always forces HOLD and blocks execution enqueue
- ADJUSTED path is bounded (single-pass) and cannot recurse indefinitely
- Shared 2.0s latency budget is enforced conservatively
- Reflection artifacts are logged and persisted in a machine-parseable audit trail

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-13/invariants
4) Explicit statement on each MAAP critical category:
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Business logic drift: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```

