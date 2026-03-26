# PRD v4.0 - Poly-Oracle-Agent Phase 4

Source inputs: `PRD-v3.0.md`, `STATE.md`, `docs/system_architecture.md`, `docs/risk_management.md`, and `docs/business_logic.md`.

## 1. Executive Summary

Phase 4 marks the transition from a linear evaluation flow to an advanced agentic trading workflow inside `poly-oracle-agent`. The objective is to add cognitive coordination layers that improve decision quality without weakening financial controls: market-aware routing, staged reasoning via prompt chaining, and internal reflection before final risk validation.

Phase 4 does not expand execution surface area. It strengthens decision formation quality upstream while preserving the same downstream safety boundary and execution contracts delivered in earlier phases.

## 2. Core Pillars

### 2.1 Dynamic Routing

Dynamic Routing introduces a market-classification step that tags incoming opportunities by regime (for example: liquidity quality, spread profile, resolution horizon, and volatility context). The router directs each market context into the most appropriate prompt path so the model receives task framing that matches the market type rather than a single universal prompt.

Expected outcomes:
- Better context specialization by market type
- Reduced prompt noise and irrelevant reasoning branches
- Deterministic route metadata persisted for auditability

### 2.2 Prompt Chaining

Prompt Chaining decouples two responsibilities that are currently entangled:
- Data extraction and normalization from raw market context
- Mathematical reasoning (probability, EV, Kelly, and action proposal)

Instead of one monolithic prompt, Phase 4 introduces chained stages with explicit handoff artifacts. This reduces hallucination risk, improves traceability of intermediate assumptions, and makes failure points easier to test and audit.

Expected outcomes:
- Clear intermediate artifacts for debugging and replay
- Stronger separation between factual extraction and quantitative inference
- More reliable downstream structured outputs

### 2.3 Reflection

Reflection adds an internal LLM risk-audit pass before Gatekeeper validation. The reflection step challenges the draft decision by checking for inconsistencies, overconfidence, unsupported assumptions, and risk-threshold blind spots. The output is a revised candidate decision plus a reflection audit note for persistence.

Expected outcomes:
- Fewer brittle approvals caused by single-pass reasoning
- Better self-critique before the hard validation boundary
- Richer audit trails for post-trade analysis

## 3. Work Items

### WI-11: Market Router

**Goal:** Implement a router that classifies each market snapshot/context and assigns it to a prompt lane.

**Scope:**
- Add a routing component in the cognitive path between context aggregation and prompt generation
- Define a typed routing artifact (market class, route id, reason codes, timestamp)
- Persist routing decisions for observability and replay

**Acceptance Criteria:**
1. Every evaluable context is assigned a deterministic route or a documented fallback route.
2. Route metadata is available to downstream prompt stages.
3. Routing runs fully async and does not block queue throughput.
4. Unit/integration tests verify classification behavior and fallback safety.

### WI-12: Chained Prompt Factory

**Goal:** Replace the single-step prompt with a staged prompt chain that separates extraction from quantitative reasoning.

**Scope:**
- Stage A: extract structured market facts from routed context
- Stage B: perform probabilistic and EV/Kelly reasoning from Stage A output
- Enforce typed handoff contracts between stages

**Acceptance Criteria:**
1. Stage outputs are schema-validated before being passed forward.
2. Reasoning stage consumes only structured extraction artifacts (no hidden raw-context dependency).
3. Pipeline remains deterministic under retry and malformed intermediate output scenarios.
4. Existing evaluation persistence/audit logging remains intact with added stage metadata.

### WI-13: Reflection Auditor

**Goal:** Add a reflection pass that audits draft reasoning and recommendation before Gatekeeper validation.

**Scope:**
- Introduce reflection prompt stage after draft reasoning and before `LLMEvaluationResponse` validation
- Emit reflection audit metadata (risk flags, contradictions, confidence critique)
- Pass revised decision candidate to existing Gatekeeper boundary

**Acceptance Criteria:**
1. Reflection always executes before Gatekeeper validation for non-error decision paths.
2. Reflection artifacts are persisted with decision logs for auditability.
3. Critical reflection flags must force conservative fallback behavior (default HOLD path) when unresolved.
4. Tests prove Gatekeeper remains the final authority and reflection cannot bypass validation.

## 4. Strict Constraints

The following constraints are mandatory and non-negotiable for all Phase 4 work:

1. **Pydantic Gatekeeper is immutable as execution boundary:**  
   `LLMEvaluationResponse` remains the sole validation contract that gates execution. New cognitive steps may enrich inputs, but they MUST NOT bypass, replace, or weaken Gatekeeper enforcement.

2. **Decimal financial math is immutable:**  
   All USDC, price, EV/Kelly-derived sizing, and exposure calculations must continue to use `Decimal` end-to-end under existing rules (`Decimal('1e6')` conversion for USDC micro-units; no `float` for money).

3. **Fully asynchronous pipeline is immutable:**  
   Routing, prompt chaining, and reflection must run within the existing async architecture (`asyncio`, queue handoffs, non-blocking I/O). No synchronous bottlenecks or side-channel execution paths may be introduced.
