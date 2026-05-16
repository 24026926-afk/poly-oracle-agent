# Business Logic - WI-60 Daily Operations Digest

## Objective

Automatically generate a deterministic daily bot operations digest for non-technical review at `03_Daily/YYYY-MM-DD-bot.md` without overwriting manual coding notes at `03_Daily/YYYY-MM-DD.md`.

This WI consumes the WI-56 append-only operational event ledger, the WI-57 deterministic narrative layer, the WI-58 replay summary patterns, and the WI-59 dashboard current-state interpretation. It may optionally include repository-backed paper PnL and a short Telegram summary when explicitly configured. It must not mutate operational events, invent missing runtime state, expose prompts or identifiers, change trading behavior, enable live trading, or weaken `LLMEvaluationResponse` as the terminal Gatekeeper before execution.

## Data Models

Pydantic schema names only:

- `OperationalEventRecord`
- `OperationalEventReadWindow`
- `OperationalEventQuery`
- `OperationalEventType`
- `OperationalEventSeverity`
- `OperationalEventSource`
- `OperationalEventReasonCode`
- `OperationalEventPayload`
- `OperationalNarrative`
- `RuntimeNarrative`
- `NarrativeRenderResult`
- `NarrativeRenderStatus`
- `IncidentReplayLine`
- `IncidentReplaySummary`
- `DashboardCurrentState`
- `DailyOpsDigestStatus`
- `DailyOpsDigestFailureReason`
- `DailyOpsDigestRequest`
- `DailyOpsDigestWindow`
- `DailyOpsDigestRunSummary`
- `DailyOpsDigestDecisionSummary`
- `DailyOpsDigestLLMSummary`
- `DailyOpsDigestPnLSummary`
- `DailyOpsDigestEventHighlight`
- `DailyOpsDigestOperatorCheck`
- `DailyOpsDigestTelegramSummary`
- `DailyOpsDigestTelegramResult`
- `DailyOpsDigestWriteResult`
- `DailyOpsDigestReport`

## Key Rules

1. The daily digest is an operator reporting surface. It must never append, update, delete, backfill, or repair operational events.
2. All event reads must route through `OperationalEventRepository` or a repository-backed read service.
3. Position-derived paper PnL reads must route through `PositionRepository` or a repository-backed read service.
4. The digest file path must be constrained to `03_Daily/YYYY-MM-DD-bot.md`.
5. The manual coding note path `03_Daily/YYYY-MM-DD.md` must never be created, overwritten, truncated, appended, renamed, or deleted by digest generation.
6. Re-running the digest for the same date and same persisted data must produce the same digest text.
7. Re-running the digest may replace only the matching `YYYY-MM-DD-bot.md` file after path validation so cron/systemd execution remains idempotent.
8. The digest window must be explicit and timezone-aware. Default CLI behavior may derive a UTC daily window from the requested digest date.
9. All event timestamps must be normalized to UTC before aggregation and rendering.
10. Run start and stop times must be derived only from typed lifecycle events.
11. Uptime must be computed from typed start/stop events. If shutdown is missing, the digest must mark the run as partial or still open rather than invent a stop time.
12. Active provider must be derived only from typed event payloads that safely expose a low-cardinality provider name.
13. Dry-run status must be derived only from typed config, lifecycle, or execution events. Unknown dry-run status must remain unknown.
14. Readiness status must use the latest typed readiness event in the digest window.
15. Market counts must be derived from typed market-discovery, rejection, and quarantine events without printing raw market names, token IDs, condition IDs, or market identifiers.
16. Decisions by type must use typed reason codes or aggregate actions only.
17. Skips by reason must use stable `OperationalEventReasonCode` values only.
18. LLM calls, budget blocks, cooldown blocks, and provider failures must be counted from typed operational event fields.
19. Estimated LLM spend must use `Decimal` end to end. Missing cost fields are valid and must render as unavailable, not zero unless the persisted data supports zero.
20. Paper PnL must use `Decimal` end to end and may be omitted or marked unavailable when repository-backed position data is absent.
21. Digest summaries may format `Decimal` values for display, but must not use raw `float` for spend, PnL, EV, price, sizing, exposure, or token-cost math.
22. Top operational events must be selected deterministically from typed severity, event type, reason code, timestamp, and stable persisted event id.
23. Unresolved warnings/errors must be derived from recent typed warning/error/critical events and recovery events. The digest must not claim resolution without a typed recovery or normalizing event.
24. Recommended next operator checks must be generated from typed state only, using deterministic rules.
25. Digest prose must be deterministic, bounded, and generated without an LLM.
26. Digest text must never include raw prompts, private reasoning, raw provider responses, API keys, wallet keys, Telegram tokens, token IDs, condition IDs, wallet addresses, raw exception messages, connection strings, SQL text, or high-cardinality identifiers.
27. All output sections must pass the existing secret/high-cardinality scan before writing to disk or sending to Telegram.
28. A missing `operational_events` table or empty event window must produce a valid no-run digest.
29. Partial-run days must produce a valid digest with explicit unknown or unavailable fields where typed evidence is missing.
30. Telegram delivery is optional and must be disabled unless both Telegram alerts are enabled and digest Telegram delivery is explicitly configured.
31. Telegram summary text must be shorter than the file digest, bounded, deterministic, secret-safe, and timeout-protected through existing Telegram notifier patterns.
32. Telegram failures must return typed delivery failures and must not fail the file digest write when the file digest itself is valid.
33. Logs and metrics, if added, must use low-cardinality labels only.
34. The CLI entrypoint must be suitable for manual, cron, or systemd timer execution.
35. The CLI must return safe non-zero results for invalid dates, invalid output paths, repository failures, database failures, or forbidden content.
36. The digest must not call Claude, DeepSeek, Grok, or any other LLM.
37. The digest must not import or invoke execution routing, transaction signing, order broadcasting, order placement, or live wallet mutation paths.
38. The digest must not modify `LLMEvaluationResponse` or add presentation fields to cognitive, financial, or Gatekeeper schemas.

## Edge Cases

1. Manual note exists at `03_Daily/YYYY-MM-DD.md`: digest writes only `03_Daily/YYYY-MM-DD-bot.md`.
2. Manual note does not exist: digest still writes only the bot digest file.
3. Bot digest already exists: generation replaces only the validated bot digest file with deterministic output.
4. Output path is outside `03_Daily/` due to traversal or symlink behavior: fail closed with a typed path failure.
5. Database file does not exist: return a valid no-run or database-unavailable result according to the configured source path.
6. Database exists but `operational_events` is missing: render a no-run digest or typed unavailable status without crashing.
7. `operational_events` exists but contains no rows for the date: write a zero-event no-run digest.
8. Events exist before `START`: include them in counts but mark run start unavailable unless a typed start event exists.
9. Multiple start/stop cycles exist in one date window: compute deterministic run spans and total uptime from typed lifecycle pairs.
10. Start event has no matching shutdown: mark the latest span as partial and compute uptime only to the digest window end or mark unavailable by typed rule.
11. Latest readiness event is `DEGRADED` or `NOT_READY`: digest reflects the state and adds a deterministic operator check.
12. Provider changes during the day: digest reports the latest typed provider and may include a bounded provider-change count.
13. LLM cost payloads are missing: estimated spend is unavailable rather than fabricated.
14. LLM cost payloads include Decimal-compatible strings: parse and sum with `Decimal`.
15. Position table is absent or has no settled rows: paper PnL is unavailable, not zero.
16. Settled positions exist with gas and fees: paper PnL uses repository-backed Decimal values including available gas/fee fields.
17. Persisted payload JSON is malformed: skip payload-derived fields for that event and include a typed parsing warning without raw payload text.
18. Persisted payload contains forbidden content despite earlier validation: redact or drop that event from human-facing digest sections.
19. Warning/error is followed by a typed recovery event: unresolved status uses deterministic latest-event semantics.
20. Warning/error has no recovery: include it in unresolved warnings/errors using typed event metadata only.
21. Telegram is disabled: file digest succeeds and Telegram result is disabled/not applicable.
22. Telegram is enabled but digest delivery config is disabled: file digest succeeds and no Telegram send occurs.
23. Telegram send times out or fails: digest reports a typed Telegram failure without raw exception text.
24. Filtered or bounded top-event list omits lower-priority events: aggregate counts still include all valid events in the digest window.
25. CLI receives a malformed date: fail non-zero with a bounded error message.

## Invariants

1. Daily digest generation is deterministic for the same input events, positions, request, and config.
2. Manual coding daily notes are never overwritten.
3. The only file write target is the validated bot digest path under `03_Daily/`.
4. Operational event reads are repository-backed.
5. Position/PnL reads are repository-backed.
6. Operational events remain append-only.
7. The digest is secret-safe and high-cardinality-safe before file or Telegram output.
8. No digest text is generated by an LLM.
9. Unknown runtime facts remain unknown or unavailable; they are never invented.
10. `LLMEvaluationResponse` remains the Gatekeeper and is not modified.
11. No live trading, signing, broadcasting, or `DRY_RUN=false` behavior is added or changed.
12. No raw database sessions are used in digest business logic outside repository boundaries.
13. No `Base.metadata.create_all()` is introduced in runtime, CLI, or digest paths.
14. All spend, PnL, EV, price, sizing, exposure, and token-cost math remains Decimal-native.
15. Telegram summary delivery is optional, bounded, timeout-protected, and secret-safe.
16. Tests cover digest generation, no-overwrite behavior, empty/no-run days, partial runs, unresolved warnings/errors, Decimal spend/PnL formatting, Telegram disabled/enabled/failure paths, path constraints, deterministic output, and redaction.
