# P2-WI-05-dry-run-flag-enforcement.md
**WI:** WI-05  
**Agent:** Execution Specialist  
**Depends on:** None  
**Risk:** HIGH  

## Context
`DRY_RUN` exists in `src/core/config.py` (AppConfig) and is loaded in `orchestrator.py` but is never checked in Layer 4. PRD-v2.0.md WI-05 calls this the highest-risk operational safety gap — real orders can be broadcast even when `dry_run=true`.

## Objective
Enforce `dry_run` at the top of the execution path so no signing, nonce, CLOB submission or receipt polling ever occurs when flag is True.

## Exact Files to Touch
- `src/agents/execution/broadcaster.py` — add check in `broadcast()` and `OrderBroadcaster.__init__`
- `src/agents/execution/signer.py` — add dry_run guard in `sign_order()`
- `src/agents/execution/nonce_manager.py` — add dry_run guard in `get_next_nonce()`

## Step-by-Step Task
1. Inject `self.config = get_config()` in `OrderBroadcaster`.
2. At the very top of `broadcast()`: `if self.config.dry_run: log structured entry with condition_id/action/size and return`.
3. Propagate same guard into `TransactionSigner.sign_order()` and `NonceManager.get_next_nonce()`.
4. Keep full audit trail (DB insert of decision) and upstream layers untouched.
5. Emit structlog entry exactly: `dry_run=true`, `condition_id`, `proposed_action`, `would_be_size_usdc`.

## Step 5b — Reflection Pass (NEW)
Tool: Codex Chat Panel (Antigravity)
Prompt: "Review the changes made in this session against:
  1. business_logic_wi05.md — did every rule get implemented?
  2. .agents/rules/db-engineer.md — any violations?
  3. PRD-v3.0 acceptance criteria — all met?
List any gaps before I approve the commit."

## Acceptance Criteria (must match PRD exactly)
- [ ] When `DRY_RUN=true`, the execution path does not call `TransactionSigner.sign_order()`, `NonceManager.get_next_nonce()`, CLOB order submission, or Polygon receipt polling.
- [ ] When `DRY_RUN=true`, upstream ingestion, context building, evaluation, and decision persistence continue to run normally.
- [ ] Each prevented execution emits a structured log entry containing `dry_run=true`, `condition_id`, proposed action, and would-be order sizing data.
- [ ] An integration test verifies that an approved trade in dry-run mode produces zero external execution side effects and does not advance nonce state.

## Hard Constraints
- `dry_run` check MUST be first statement in Layer 4 before any Web3 call.
- Use `structlog` only — no print().
- Never commit real orders when flag=True.

## Verification Command
```
DRY_RUN=true python -m pytest tests/unit/test_broadcaster.py::TestOrderBroadcaster::test_dry_run_prevents_all_execution -q --tb=no
```
