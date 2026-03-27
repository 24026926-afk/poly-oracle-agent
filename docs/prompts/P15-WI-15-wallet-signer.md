# P15-WI-15 — Wallet Signer Implementation Prompt

## Execution Target
- Primary: Claude Code implementation agent ("Maker")
- Checker: Gemini 2.5 Pro / GPT-5.4 ("Checker") under MAAP
- Branch discipline: create and work on `feat/wi15-wallet-signer` (branched from `develop`), atomic commits only, PR back to `develop`

## Active Agents
- `.agents/rules/async-architect.md`
- `.agents/rules/web3-specialist.md`
- `.agents/rules/risk-auditor.md`
- `.agents/rules/test-engineer.md`
- `.agents/rules/security-auditor.md`

## Role

You are implementing WI-15 for Phase 5: a secure wallet signer surface for Polygon EIP-712 transaction signing.

This WI is signing-surface-only. It must harden private-key custody and signing boundaries without adding send/broadcast capabilities.

---

## Mandatory Context Hydration (Read Before Any Edits)

1. `AGENTS.md`
2. `STATE.md`
3. `docs/business_logic/business_logic_wi15.md`
4. `docs/PRD-v5.0.md` (Phase 5 / WI-15 section)  
   If `PRD-v5.0.md` is not present, read the current Phase 5 PRD section from:
   - `docs/archive/ARCHIVE_PHASE_4.md` (`## Next Phase (Phase 5)`)
5. `docs/system_architecture.md`
6. `docs/risk_management.md`
7. `docs/business_logic.md`
8. `src/agents/execution/signer.py`
9. `src/agents/execution/polymarket_client.py` (context boundary: signer must remain isolated from market-data modules)
10. `src/orchestrator.py`
11. `src/core/config.py`
12. Existing tests:
    - `tests/unit/test_signer.py`
    - `tests/integration/test_orchestrator.py`
    - `tests/integration/test_pipeline_e2e.py`

Do not proceed if this context is not loaded.

---

## CRITICAL: Agentic TDD Mandate (RED Phase First)

You MUST start with failing tests before editing production code.

### RED Phase Requirements

1. Create/extend WI-15 test files first:
   - `tests/unit/test_wallet_signer.py`
   - `tests/integration/test_wallet_signer_integration.py` (or equivalent integration target)
2. Write failing tests for all required behaviors:
   - `WalletSigner` exists and exposes only one public signing API for WI-15: `sign_transaction()`.
   - Private key is loaded only via encrypted keystore/vault provider; no `os.environ` or plaintext key reads.
   - Private key material is never logged (including exception paths).
   - `dry_run=True` path does not instantiate `WalletSigner` and does not touch key files/providers.
   - `WalletSigner` has zero imports from evaluation modules or market data modules.
   - Gas/amount math in signer path uses strict `Decimal` handling (no float assertion path).
   - `sign_transaction()` signs and returns typed signed artifact only (no send/broadcast side effect).
3. Run RED tests:
   - `pytest tests/unit/test_wallet_signer.py -v`
   - `pytest tests/integration/test_wallet_signer_integration.py -v`
4. Capture and summarize expected failures.

Hard stop rule:
- Do NOT modify any `src/` implementation files until RED tests fail for the expected reasons.

---

## GREEN Phase — Atomic Execution Steps

### Step 1 — Create WI-15 Wallet Signer Module

Target:
- `src/agents/execution/wallet_signer.py` (new)

Requirements:
1. Add async `WalletSigner` class.
2. `WalletSigner` is signing-only and isolated from market/evaluation logic.
3. WI-15 public API surface is only `sign_transaction(...)`.
4. Add typed request/response contracts (Pydantic) for unsigned input and signed output artifacts.

### Step 2 — Implement Secure Key Provider Boundary

Target:
- `src/agents/execution/wallet_signer.py`
- supporting key-provider module(s) as needed
- config wiring files as needed (`src/core/config.py`)

Requirements:
1. Private key source must be encrypted keystore or secure vault only.
2. Never read signing key from `os.environ`, `.env`, or plaintext literals.
3. Never log key material, passphrases, decrypted bytes, or full key refs.
4. Fail closed on vault/keystore failure (no insecure fallback source).
5. Structured logging only (`structlog`), no `print()`.

### Step 3 — Implement `sign_transaction()` Async Contract

Target:
- `src/agents/execution/wallet_signer.py`

Requirements:
1. Implement async method `sign_transaction(...)` as the only public signing method for WI-15.
2. Enforce Polygon/EIP-712 signing domain requirements (`chain_id=137`).
3. Enforce Decimal-only handling for all gas/amount math in signer scope.
4. Return typed signed artifact only; do not send, broadcast, poll receipts, or mutate execution status.
5. Reject invalid/non-positive amount fields before signing.

### Step 4 — Enforce `dry_run` Pre-Instantiation Guard

Target:
- Layer 4 execution entrypoint(s), including `src/orchestrator.py` and related execution wiring

Requirements:
1. Ensure `dry_run=True` check occurs before any `WalletSigner` instantiation.
2. Ensure `dry_run=True` path does not load vault/keystore and does not touch key files.
3. Emit structured skip log with execution metadata only (non-sensitive).

### Step 5 — Enforce Isolation + No Broadcast Expansion

Target:
- `src/agents/execution/wallet_signer.py`
- related execution wiring/tests as needed

Requirements:
1. `WalletSigner` must have zero imports from:
   - `src/agents/evaluation/*`
   - market-data modules (including `polymarket_client.py`)
2. WI-15 must not add any send/broadcast method to signer.
3. Preserve existing Gatekeeper boundary (`LLMEvaluationResponse`) and queue topology.

### Step 6 — GREEN Validation

Run:
```bash
pytest tests/unit/test_wallet_signer.py -v
pytest tests/integration/test_wallet_signer_integration.py -v
pytest tests/integration/test_orchestrator.py -v
pytest --asyncio-mode=auto tests/ -q
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

Coverage must remain >= 80%.

---

## Invariants & Safety Gates (Non-Negotiable)

1. Private key loaded ONLY from encrypted keystore or vault — never `os.environ`, never plaintext logs.
2. `WalletSigner` is isolated — zero imports from evaluation or market data modules.
3. `dry_run=True` must bypass signer completely: no signer instantiation, no key file/provider touch.
4. All gas/amount math in WI-15 signer paths is `Decimal` only (no float money math).
5. `sign_transaction()` is the only public method in WI-15 signer scope — no send/broadcast methods.
6. No bypass of `LLMEvaluationResponse` terminal Gatekeeper.
7. No queue topology changes; preserve async pipeline order.

---

## Required Test Matrix

At minimum, WI-15 tests must prove:
1. `WalletSigner` private key retrieval is vault/keystore-only and rejects insecure sources.
2. `WalletSigner` never logs key material in success/failure paths.
3. `sign_transaction()` is async, typed, and returns signed artifact without side effects.
4. Decimal correctness for gas/amount handling under precision-sensitive fixtures.
5. `dry_run=True` path performs zero signer instantiation and zero keystore/vault access.
6. Import-boundary test confirms no evaluation/market-data module dependencies in `WalletSigner`.
7. No WI-15 code path introduces send/broadcast capability.

---

## Deliverables

1. RED-phase failing test summary.
2. GREEN implementation summary by file.
3. Passing targeted test summary + full regression summary.
4. Final staged `git diff` for MAAP checker review.

---

## MAAP Reflection Pass (Checker Prompt for Gemini 2.5 Pro)

Use the following exact prompt with the final staged `git diff`:

```text
You are the MAAP Checker for WI-15 (Wallet Signer) in poly-oracle-agent.

Review the following git diff against:
1) docs/business_logic/business_logic_wi15.md
2) Phase 5 PRD section (docs/PRD-v5.0.md WI-15 section, or ARCHIVE_PHASE_4.md Next Phase section if PRD-v5.0 is unavailable)
3) docs/archive/ARCHIVE_PHASES_1_TO_3.md invariants
4) AGENTS.md hard constraints

Your review MUST explicitly clear or flag these categories:
- Decimal violations (any float usage in gas/amount money-path logic)
- Gatekeeper bypasses (any execution-eligible path without terminal LLMEvaluationResponse validation)
- Business logic drift (deviation from WI-15 signer-only scope and dry_run rules)
- Key custody violations (any key load from os.environ/plaintext source or key material leakage in logs)
- Isolation violations (WalletSigner importing evaluation or market-data modules)

Additional required checks:
- WalletSigner class exists and is signing-only
- sign_transaction() exists, is async, and is the only public method in WI-15 signer scope
- key loading is vault/keystore-only with fail-closed behavior
- dry_run=True path bypasses signer instantiation and key access
- WI-15 introduces no send/broadcast capability

Output format:
1) VERDICT: PASS or FAIL
2) Findings by severity (Critical, High, Medium, Low)
3) For each finding: file path + line reference + why it violates WI-15/invariants
4) Explicit statement on each MAAP critical category:
   - Decimal violations: CLEARED/FLAGGED
   - Gatekeeper bypasses: CLEARED/FLAGGED
   - Business logic drift: CLEARED/FLAGGED
   - Key custody violations: CLEARED/FLAGGED
   - Isolation violations: CLEARED/FLAGGED
5) Minimal fix list required before commit approval

If no issues are found, state "MAAP CLEARANCE GRANTED".
```
