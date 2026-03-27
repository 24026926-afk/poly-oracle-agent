# WI-15 Business Logic — Wallet Signer (Secure Polygon Signing Surface)

## Active Agents + Constraints

- `.agents/rules/async-architect.md` — signing path remains async and preserves queue order (`market_queue -> prompt_queue -> execution_queue`).
- `.agents/rules/web3-specialist.md` — EIP-712 domain stays on Polygon (`chain_id=137`) with canonical exchange addresses only.
- `.agents/rules/security-auditor.md` — private key never appears in plaintext/logs/env vars; `dry_run=True` bypasses signer creation and key loading.
- `.agents/rules/risk-auditor.md` — all transaction amount math is Decimal-safe; USDC conversion uses `Decimal("1e6")` only.
- `.agents/rules/test-engineer.md` — WI-15 signing behavior requires unit + integration coverage; full suite remains >= 80%.

## 1. Objective

Introduce a hardened wallet-signing capability for Polygon EIP-712 order signatures with strict key-custody controls and fail-closed behavior.

WI-15 is the first signing-surface work item. It adds signing only. It does not add order routing or broadcast execution paths (that is WI-16).

## 2. Scope Boundaries

### In Scope

1. Secure key-custody boundary for `TransactionSigner` (vault or encrypted keystore only).
2. Async, typed signing interface for EIP-712 payload signing on Polygon.
3. Decimal-safe amount handling at signer boundary (`Decimal` end-to-end in amount derivation paths).
4. Layer 4 `dry_run` bypass semantics that prevent signer instantiation and key loading.
5. Deterministic structured logging and error taxonomy for signer outcomes (without exposing secrets).

### Out of Scope

1. Order routing changes, execution queue redesign, or broadcast orchestration changes (WI-16 scope).
2. Market data fetch logic, prompt construction, or evaluation policy changes.
3. Any modification to `LLMEvaluationResponse` Gatekeeper formulas/thresholds.
4. Key storage in plaintext `.env` / env vars / source code / logs.
5. Settlement, cancellation, or receipt polling behavior changes.

## 3. Target Component Architecture + Data Contracts

### 3.1 Signing Component (Existing Canonical Class)

- **Module:** `src/agents/execution/signer.py`
- **Class Name:** `TransactionSigner` (exact)
- **Responsibility:** sign validated order payloads via EIP-712 for Polygon only.

Isolation rule:
- `TransactionSigner` must remain execution-layer only. It must not depend on market-data ingestion, prompt logic, routing logic, or evaluation logic modules.

### 3.2 Key Custody Boundary (Required)

WI-15 introduces a strict signer key-source boundary:

1. Allowed key sources:
   - secure remote vault secret reference
   - encrypted local keystore (ciphertext at rest, runtime decrypt only)
2. Forbidden key sources:
   - plaintext env vars (including `.env`)
   - hardcoded literals in code/tests
   - persisted plaintext in DB or files
3. Key material lifecycle:
   - loaded just-in-time for signing
   - never emitted in logs/exceptions
   - released/zeroized best-effort immediately after signing attempt

### 3.3 Data Contracts (Required)

Signer boundary must use typed contracts (Pydantic at boundary is required). Minimum contracts:

1. `SignRequest`
   - `order`: validated order payload object
   - `chain_id`: must equal `137`
   - `neg_risk`: `bool`
   - `key_ref`: opaque secret reference (vault path or keystore id), never raw private key
   - `maker_amount_usdc`: `Decimal`
   - `taker_amount_usdc`: `Decimal`
2. `SignedOrder` (or equivalent typed response)
   - `order`
   - `signature` (hex string)
   - `owner` (checksummed signer address)
   - `signed_at_utc`
   - `key_source_type` (`vault` or `encrypted_keystore`)

Hard rule:
- If on-chain micro-units are derived in signer scope, conversion is `int(amount_decimal * Decimal("1e6"))` only.

## 4. Core Method Contracts (async, typed)

### 4.1 Async Sign Entry Point

Required public method:

- `sign_order(request: SignRequest)` (async)

Behavior requirements:

1. First check is `dry_run`; when true, return conservative no-op/error outcome and exit immediately.
2. `dry_run=True` path MUST NOT instantiate `TransactionSigner` internals that require key material.
3. Key retrieval happens only through the secure key-provider boundary using `key_ref`.
4. Derived signer address must match configured wallet identity before signature is accepted.
5. Signature output is deterministic typed output; no raw dict return.
6. Any key/decrypt/sign failure is fail-closed (no execution-eligible artifact).

### 4.2 Async Key Provider Contract

Required provider methods (name may vary, behavior may not):

- `load_private_key(key_ref: str)` (async)
- `source_type()` -> `"vault"` or `"encrypted_keystore"`

Hard constraints:

1. No fallback to env var private key if provider fails.
2. Provider failures are explicit typed errors (no silent retries with alternate insecure source).
3. Provider logs only metadata (`key_ref_hash`, source type, latency); never key contents/passphrase.

### 4.3 Decimal Transaction Amount Integrity

1. Monetary fields entering signer boundary are `Decimal`.
2. `float` inputs in amount fields are rejected at schema boundary.
3. USDC decimal conversion uses `Decimal("1e6")` only.
4. Negative/zero/NaN-equivalent amounts are rejected before signing.

## 5. Pipeline Integration Design

WI-15 integration point is Layer 4 execution entry and is strictly conservative:

1. Gatekeeper-approved decisions enter `execution_queue` unchanged.
2. Layer 4 worker checks `AppConfig.dry_run` before any signer construction.
3. If `dry_run=True`: log skip and return; do not load key, do not instantiate signer, do not sign.
4. If `dry_run=False`: instantiate signer with secure key-provider reference and produce signed payload.
5. WI-15 ends at signed payload generation contract; no new broadcast routing capability is introduced in this WI.

### 5.1 Signer Isolation Rule

The signer module must not:

1. fetch market data,
2. call LLM/evaluation code,
3. perform decision routing.

Signer input is already-approved, typed execution payload only.

### 5.2 Failure Semantics (Fail Closed)

On key access error, decrypt error, address mismatch, schema violation, or signing failure:

1. emit structured error log (non-sensitive),
2. produce non-execution outcome,
3. do not enqueue/forward to broadcast path.

## 6. Invariants Preserved

1. `LLMEvaluationResponse` remains the terminal pre-execution Gatekeeper.
2. Kelly formula and safety filters remain unchanged (`KELLY_FRACTION=0.25`, 3% cap still intact).
3. Decimal financial-integrity rules remain mandatory for all transaction amount paths.
4. Async 4-layer queue topology remains unchanged.
5. `dry_run=True` continues to block all Layer 4 side effects.
6. Repository pattern and DB boundaries remain unchanged.

## 7. Strict Acceptance Criteria (Maker Agent)

1. `TransactionSigner` remains the canonical signer class in `src/agents/execution/signer.py`.
2. Signer key retrieval supports only secure vault/encrypted keystore sources.
3. No signer path reads private key plaintext from env vars or `.env`.
4. No logs/exceptions include private key, passphrase, or decrypted key bytes.
5. `sign_order(...)` signing entrypoint is async and typed.
6. `dry_run=True` bypass occurs before signer/key initialization and before any sign attempt.
7. Signing enforces Polygon `chain_id=137` and canonical exchange-domain addresses.
8. Transaction amount math uses `Decimal` only; no float amount path allowed.
9. Amount-to-micro conversion uses `Decimal("1e6")` only where needed.
10. Signer remains isolated from market-data/evaluation/routing concerns.
11. WI-15 introduces no order-routing/broadcast capability expansion (explicitly deferred to WI-16).
12. Full regression remains green (`pytest --asyncio-mode=auto tests/`) with coverage >= 80%.

## 8. Verification Checklist

1. Unit test: `dry_run=True` path does not instantiate signer and does not call key provider.
2. Unit test: vault/keystore provider success path signs and returns typed `SignedOrder`.
3. Unit test: provider failure returns fail-closed non-execution outcome.
4. Unit test: address mismatch between derived key and configured wallet is rejected.
5. Unit test: float amount input is rejected at schema boundary.
6. Unit test: Decimal -> micro conversion uses `Decimal("1e6")` and expected integer output.
7. Integration test: execution worker with `dry_run=True` performs zero signing operations.
8. Integration test: signer module has no dependency on context/evaluation modules (import boundary check).
9. Integration test: WI-15 path ends at signed payload artifact and does not trigger broadcast routing additions.
10. Full suite:
    - `pytest --asyncio-mode=auto tests/`
    - `coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m`
