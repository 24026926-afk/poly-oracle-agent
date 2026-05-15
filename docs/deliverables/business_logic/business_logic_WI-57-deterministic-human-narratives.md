# Business Logic - WI-57 Deterministic Human Narratives

## Objective

Create a deterministic narrative layer that converts typed operational ledger events and decision outcomes into plain English summaries that a non-technical operator can read without seeing prompts, private reasoning, secrets, token identifiers, condition identifiers, wallet details, or other high-cardinality data.

This WI is a presentation and auditability layer on top of the WI-56 operational event ledger. It must not modify `LLMEvaluationResponse`, trading strategy, prompt construction, execution routing, live trading authorization, signing, or broadcasting. Narratives are deterministic renderings of typed data only; no LLM may generate runtime narrative text.

## Data Models

Pydantic schema names only:

- `OperationalNarrative`
- `DecisionNarrative`
- `RuntimeNarrative`
- `NarrativeRenderResult`
- `NarrativeRenderStatus`
- `NarrativeRenderFailureReason`
- `NarrativeTemplateKey`
- `NarrativeInspectionHint`
- `OperationalEventRecord`
- `OperationalEventType`
- `OperationalEventSeverity`
- `OperationalEventSource`
- `OperationalEventReasonCode`
- `OperationalEventPayload`
- `OperationalEventReadWindow`
- `LLMEvaluationResponse`

## Key Rules

1. Narrative schemas are presentation schemas. They must remain separate from cognitive, financial, and execution schemas.
2. `LLMEvaluationResponse` must not gain `human_summary`, `operator_summary`, narrative, presentation, or dashboard-only fields.
3. Narrative rendering uses deterministic mappings from `OperationalEventType`, `OperationalEventReasonCode`, `OperationalEventSeverity`, `OperationalEventSource`, and bounded payload fields.
4. Runtime narratives must never call Claude, DeepSeek, Grok, or any other LLM.
5. The same typed input must always produce the same narrative output.
6. Narratives must explain what happened, why it happened, whether the bot continued, skipped, degraded, or stopped, and what the operator should inspect next when applicable.
7. Supported event coverage must include budget blocks, cooldown blocks, provider failures, market rejection/quarantine, readiness changes, accepted/skipped decisions, dry-run execution, circuit breaker transitions, alerts, and recovery events.
8. Reason-code wording must be stable English and audit-friendly. It must not depend on raw payload text.
9. Unknown but valid event/reason combinations return conservative generic summaries that expose no raw payload values.
10. Invalid or unsafe inputs return typed render failures or fallback summaries. Narrative failures must not crash the trading loop.
11. Narrative output may include stable event type, severity, source component, reason code, timestamp, dry-run status, provider name, bounded counts, readiness state, and aggregate decision action.
12. Narrative output must not include raw prompts, private reasoning, raw provider responses, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, raw exception messages, or high-cardinality identifiers.
13. Secret and high-cardinality scanning applies to narrative text before it is returned to callers.
14. Narrative rendering must not write to the database. Persisted operational events remain append-only and repository-owned.
15. Repository changes, if any, are read-only helper methods over `OperationalEventRepository`.
16. Narrative consumers may read `OperationalEventRecord` objects or repository-backed windows. They must not hold raw database sessions in observability presentation code.
17. All money, spend, EV, PnL, price, sizing, exposure, and token-cost values used in narrative inputs must already be Decimal-safe at schema boundaries.
18. Narratives may format Decimal values for display, but must not perform trading calculations.
19. Metrics and logs emitted by the narrative layer must use low-cardinality labels only.
20. The narrative layer is reusable by future WI-58 incident replay, WI-59 dashboard feed, and WI-60 daily operations digest.

## Edge Cases

1. `BUDGET_BLOCK` with `BUDGET_DAILY`: render a deterministic summary that the model call was blocked because the daily LLM spend limit was reached.
2. `BUDGET_BLOCK` with `BUDGET_HOURLY`, `BUDGET_TOKEN`, `BUDGET_COST`, or `BUDGET_REFLECTION`: render the corresponding stable budget reason without exposing raw cost traces.
3. `COOLDOWN_BLOCK` with repeated hold or invalid-response reason: state that the market was temporarily skipped due to repeated low-value or invalid evaluations.
4. `PROVIDER_FAILURE` with malformed response or failed provider call: state that the evaluation provider failed and the bot did not treat the response as a valid trade decision.
5. Market rejection or quarantine: explain the market was skipped or quarantined because a typed eligibility or cooldown rule blocked evaluation.
6. Readiness changed to `DEGRADED` or `NOT_READY`: explain the bot is degraded or not ready and identify the stable component/reason category to inspect.
7. Decision accepted: describe the accepted aggregate decision action without raw reasoning or prompt content.
8. Decision skipped: describe the typed skip reason, such as low confidence, low EV, high spread, exposure limit, or TTR rule.
9. Dry-run execution: explicitly state that execution was simulated and no live signing or broadcasting occurred.
10. Circuit breaker opened: state that new BUY routing was blocked by the safety gate until the breaker closes or is overridden according to policy.
11. Alert sent or alert dispatch failed: state the alert outcome without Telegram token, chat ID, or secret payloads.
12. Error recovered: state that the runtime recovered from a bounded error category and continued or degraded according to the event severity.
13. Unknown valid event/reason combination: return a generic safe summary based only on event type, severity, source, and reason code.
14. Event payload contains a forbidden secret or high-cardinality value despite prior validation: narrative rendering returns a typed failure or redacted fallback and exposes no unsafe text.
15. Payload JSON is malformed in a persisted record: narrative rendering returns a typed failure or fallback summary and does not crash.
16. Timestamp is missing or timezone-naive: rendering normalizes to a safe UTC representation when possible, or omits the timestamp in a typed fallback.
17. Narrative template lookup is missing for a supported enum: tests catch the gap; runtime returns a conservative generic summary.
18. Multiple repeated events are rendered: each summary remains stable and does not depend on nondeterministic ordering outside the supplied sequence.

## Invariants

1. Narratives are deterministic and generated from typed fields only.
2. No runtime narrative is generated by an LLM.
3. `LLMEvaluationResponse` remains free of presentation fields.
4. The Gatekeeper remains the terminal schema before execution.
5. No live trading, signing, broadcasting, or `DRY_RUN=false` behavior changes are introduced.
6. Narrative text is secret-safe and high-cardinality-safe.
7. Unknown mappings fail safe with generic summaries rather than raw payload exposure.
8. Narrative failures return typed render results or fallback summaries and do not crash the trading loop.
9. Operational event persistence remains append-only and repository-owned.
10. The dashboard, incident replay, and digest features consume narratives later; this WI does not implement those surfaces.
11. Metrics and logs remain low-cardinality.
12. Decimal integrity is preserved for any money, EV, PnL, spend, price, sizing, exposure, or token-cost values that appear in typed narrative inputs.
13. Tests cover deterministic output, supported mappings, unknown mappings, no `LLMEvaluationResponse` pollution, secret/high-cardinality rejection, and non-crashing failure behavior.
