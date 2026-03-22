# P1-WI-01-orchestrator-class-name-mismatch.md
**WI:** WI-01  
**Agent:** Orchestrator Specialist  
**Depends on:** None  
**Risk:** HIGH  

## Context
`src/orchestrator.py` is marked ✅ IMPLEMENTED in STATE.md section 7 but still references legacy class names `AsyncWebSocketClient` and `TxBroadcaster` from the outdated Mermaid diagram in `docs/system_architecture.md`. This triggers ImportError on any startup attempt (explicitly called out in STATE.md "Architecture Gaps"). PRD-v2.0.md WI-01 lists this as the Phase 2 BLOCKER — no queue wiring, no event loop, no downstream work possible.

## Objective
Make `src/orchestrator.py` import and instantiate ONLY the implemented runtime classes so the asyncio event loop starts cleanly.

## Exact Files to Touch
- `src/orchestrator.py` — fix imports, instantiations, queue wiring; remove hardcoded condition_id (placeholder comment for WI-03)

## Step-by-Step Task
1. Replace all imports with exact names from AGENTS.md table: `CLOBWebSocketClient`, `GammaRESTClient`, `DataAggregator`, `PromptFactory`, `ClaudeClient`, `OrderBroadcaster`.
2. Wire the three queues exactly as described in system_architecture.md sequence diagram: market_queue (ingestion → context), prompt_queue (context → evaluation), execution_queue (evaluation → broadcaster).
3. Update `__init__` and `.start()` to use `asyncio.create_task` + `asyncio.gather` for the four layers.
4. Keep graceful shutdown (`CancelledError` + `.stop()` + engine dispose) unchanged.
5. Add comment `# TODO: replace with dynamic market selection from WI-03` where hardcoded asset appears.

## Acceptance Criteria (must match PRD exactly)
- [ ] `src/orchestrator.py` imports and instantiates the implemented runtime classes only; no references to `AsyncWebSocketClient` or `TxBroadcaster` remain.
- [ ] A startup smoke test instantiates the orchestrator, creates the three queues, and reaches task creation without `ImportError`, `NameError`, or `AttributeError`.
- [ ] A shutdown smoke test proves the orchestrator still calls `.stop()` on all managed components and disposes the async database engine without hanging.

## Hard Constraints
- Use ONLY class names from AGENTS.md reference table — never rename or alias.
- No `float` anywhere; `Decimal` for all USDC/price math (even in comments).
- `dry_run` flag from `AppConfig` must remain untouched here (enforced in WI-05).

## Verification Command
```
python -c "
import asyncio
from src/orchestrator import Orchestrator
from src.core.config import get_config
config = get_config()
o = Orchestrator(config)
print('Orchestrator instantiated with correct classes')
asyncio.run(asyncio.sleep(0.1))
print('Smoke test passed')
"
```
