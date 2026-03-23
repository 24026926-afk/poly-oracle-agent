# P6-WI-04-bankroll-portfolio-tracker.md
**WI:** WI-04  
**Agent:** Portfolio Specialist  
**Depends on:** P5  
**Risk:** HIGH  

## Context
`TransactionSigner.build_order_from_decision()` hardcodes 1000 USDC bankroll. No exposure tracking exists. PRD-v2.0.md WI-04 requires real capital awareness.

## Objective
Add BankrollPortfolioTracker service that computes available bankroll/exposure from DB and enforces caps before order creation.

## Exact Files to Touch
- `src/agents/execution/bankroll_tracker.py` (new)
- `src/agents/execution/signer.py` — replace hardcoded bankroll
- `src/agents/execution/broadcaster.py` — inject tracker

## Step-by-Step Task
1. Create `BankrollPortfolioTracker` using `ExecutionRepository.get_aggregate_exposure`.
2. Update `build_order_from_decision()` to use tracker.available_bankroll and apply `min(kelly, 0.03*bankroll)`.
3. Reject trade if proposed size > available or exceeds exposure.
4. Add restart-recovery test that reconstructs state from DB.

## Step 5b — Reflection Pass (NEW)
Tool: Codex Chat Panel (Antigravity)
Prompt: "Review the changes made in this session against:
  1. business_logic_wi04.md — did every rule get implemented?
  2. .agents/rules/db-engineer.md — any violations?
  3. PRD-v3.0 acceptance criteria — all met?
List any gaps before I approve the commit."

## Acceptance Criteria (must match PRD exactly)
- [ ] Order sizing no longer uses hardcoded 1000 USDC anywhere.
- [ ] Bankroll/portfolio service computes available bankroll and current exposure from persisted state.
- [ ] If trade would exceed limits, it is rejected before signing.
- [ ] Unit tests verify sizing, exposure aggregation, and rejection cases.

## Hard Constraints
- `Decimal` for ALL USDC math. Kelly multiplier exactly 0.25.
- Exposure includes pending + confirmed executions.

## Verification Command
```
python -m pytest tests/unit/test_broadcaster.py::TestBankrollTracker -q --tb=no
```
