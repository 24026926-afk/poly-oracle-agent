# P3-WI-06-http-library-migration.md
**WI:** WI-06  
**Agent:** Ingestion Specialist  
**Depends on:** None  
**Risk:** MEDIUM  

## Context
`src/agents/ingestion/rest_client.py` still uses `aiohttp` while `pyproject.toml` declares only `httpx` and PRD-v2.0.md WI-06 requires single async HTTP client.

## Objective
Migrate `GammaRESTClient` to `httpx.AsyncClient` exclusively while preserving exact behavior.

## Exact Files to Touch
- `src/agents/ingestion/rest_client.py` — replace aiohttp with httpx

## Step-by-Step Task
1. Remove any aiohttp import/usage.
2. Use `httpx.AsyncClient(timeout=10)` with context manager in `get_active_markets()` and `get_market_by_condition_id()`.
3. Keep 60s in-memory cache, stale-cache-on-failure, 5xx → `RESTClientError`, and all Pydantic validation.
4. Update unit tests to mock `httpx` responses.

## Step 5b — Reflection Pass (NEW)
Tool: Codex Chat Panel (Antigravity)
Prompt: "Review the changes made in this session against:
  1. business_logic_wi06.md — did every rule get implemented?
  2. .agents/rules/db-engineer.md — any violations?
  3. PRD-v3.0 acceptance criteria — all met?
List any gaps before I approve the commit."

## Acceptance Criteria (must match PRD exactly)
- [ ] `src/agents/ingestion/rest_client.py` uses `httpx.AsyncClient` exclusively; no `aiohttp` import remains.
- [ ] Existing behavior is preserved for `get_active_markets()`, `get_market_by_condition_id()`, cache TTL, stale-cache fallback, and error handling.
- [ ] Unit tests verify behavior parity under `httpx`-based mocks.
- [ ] The project has a single supported async HTTP client for the REST layer.

## Hard Constraints
- Use only `httpx` — never re-introduce aiohttp.
- All calls must remain async.

## Verification Command
```
python -m pytest tests/unit/test_ingestion.py -q --tb=no
```
