# P34-WI-40 — Metrics View, PositionStatus Import Fix & UI Dependency Injection

## Execution Target

- Primary: Claude Code implementation agent ("Maker")
- Branch discipline: work directly on `develop`; this WI is Phase 12 UI/observability only — no feature branch required per PRD-v12.0 §2
- MAAP: not required for Phase 12 (PRD-v12.0 §4)

## Active Agents

- `.agents/rules/test-engineer.md`
- `.agents/rules/security-auditor.md`

---

## Role

You are executing a two-part task for Phase 12 of the Poly-Oracle-Agent project:

1. **RED PHASE FIXES** — Repair 16 pytest collection errors caused by a `PositionStatus` import collision and add missing UI package declarations. These are pre-conditions; the test suite must collect cleanly before any WI-40 feature work.
2. **WI-40 FEATURE** — Refactor `render_metrics()` in `src/ui/dashboard.py` from `st.columns(3)` to `st.columns(5)` with all five PRD-mandated metric cards.

Phase 12 is **read-only UI/observability**. No application logic (`src/agents/`, `src/core/`, `src/db/`, `src/schemas/`) may change as part of WI-40. The import collision fixes are the single exception — those are correcting a pre-existing bug in production source files.

---

## Mandatory Context Hydration

Read all of the following before any edits:

1. `docs/business_logic/business_logic_wi40.md` — **primary specification; governs every decision in this prompt**
2. `docs/PRD-v12.0.md` — Phase 12 scope and WI-40 DoD
3. `STATE.md` — current test baseline (678 passing, 94% coverage)
4. `src/ui/dashboard.py` — dashboard under modification
5. `src/schemas/position.py` — authoritative home of `PositionStatus`
6. `src/schemas/execution.py` — confirm `PositionStatus` is NOT exported here
7. `src/agents/execution/exit_strategy_engine.py` — import fix target
8. `src/agents/execution/position_tracker.py` — import fix target
9. `src/orchestrator.py` — import fix target
10. `tests/integration/test_circuit_breaker_integration.py` — test import fix target
11. `tests/integration/test_telegram_notifier_integration.py` — test import fix target
12. `requirements.txt` and `pyproject.toml` — dependency declaration targets

Do not proceed until all twelve files are loaded.

---

## Critical Invariants

### I-1: `PositionStatus` Ownership

`PositionStatus` is defined in and owned by `src/schemas/position.py`. It is never re-exported from `src/schemas/execution.py`. Every consumer must import it with:

```python
from src.schemas.position import PositionStatus
```

Leaving any `from src.schemas.execution import ..., PositionStatus, ...` in place after this WI is a bug.

### I-2: Zero Application Logic Changes

No file under `src/agents/`, `src/core/`, `src/db/`, or `src/schemas/` may change for any reason related to WI-40 UI features. The only permitted changes to those namespaces are the import collision fixes (I-1 above).

### I-3: Dashboard is Read-Only

No `INSERT`, `UPDATE`, or `DELETE` SQL may appear anywhere in `src/ui/`. This invariant is already met by the existing dashboard. Do not introduce any writes.

### I-4: Strict `st.columns(5)` Layout

`render_metrics()` must call `st.columns(5)` — not `st.columns(3)`, not `st.columns(4)`, not two separate `st.columns()` calls. A single five-element destructuring is the only permitted layout.

### I-5: No New Queries in `fetch_metrics()`

`total_decisions` and `active_positions` are already returned by `fetch_metrics()`. Do not add or modify any SQL query. The data is available; only the render layer changes.

### I-6: Coverage Floor

The existing test suite must continue to pass at ≥ 94% coverage. Phase 12 adds no new test files (UI observability scope), so regression must be verified against the existing baseline.

---

## Execution Sequence

Execute the steps below in strict order. Do not skip ahead. Report the outcome of each verification gate before moving to the next step.

---

## Step 1 — Read the Business Logic Document

Open and read `docs/business_logic/business_logic_wi40.md` in full. Confirm you have internalized:
- The cascade failure map (§2.2)
- The five files requiring import fixes (§2.4)
- The three dependency entries to add (§3.3)
- The exact `render_metrics()` target signature (§4.3)

Do not proceed to Step 2 until this is complete.

---

## Step 2 — Fix `PositionStatus` Imports in Production Source Files

Fix three production source files. In each, remove `PositionStatus` from the `from src.schemas.execution import (...)` block and add a new import line for it from the correct module.

### 2.1 — `src/agents/execution/exit_strategy_engine.py`

Current import block (lines 20–27):

```python
from src.schemas.execution import (
    ExecutionAction,
    ExitReason,
    ExitResult,
    ExitSignal,
    PositionRecord,
    PositionStatus,
)
```

Required change — split into two imports:

```python
from src.schemas.execution import (
    ExecutionAction,
    ExitReason,
    ExitResult,
    ExitSignal,
    PositionRecord,
)
from src.schemas.position import PositionStatus
```

### 2.2 — `src/agents/execution/position_tracker.py`

Locate the import of `PositionStatus` from `src.schemas.execution`. Apply the same split: remove `PositionStatus` from that block; add `from src.schemas.position import PositionStatus` as a separate import.

### 2.3 — `src/orchestrator.py`

Locate the import of `PositionStatus` from `src.schemas.execution` (around line 55–56). Apply the same split.

---

## Step 3 — Fix `PositionStatus` Imports in Test Files

Fix two integration test files using the identical split pattern.

### 3.1 — `tests/integration/test_circuit_breaker_integration.py`

Find the `from src.schemas.execution import (...)` block that includes `PositionStatus`. Remove `PositionStatus` from that block. Add `from src.schemas.position import PositionStatus` as a new import line.

### 3.2 — `tests/integration/test_telegram_notifier_integration.py`

Apply the same fix.

---

## Step 4 — Verify: Zero Collection Errors

Run the collection check:

```bash
python -m pytest --collect-only 2>&1 | grep "ERROR collecting tests/"
```

**Gate:** The output must be empty (zero lines). If any collection error from `tests/` remains, do not proceed — diagnose and fix it.

Also confirm total collected item count is at or above the baseline:

```bash
python -m pytest --collect-only -q 2>&1 | tail -5
```

---

## Step 5 — Add Missing UI Dependencies

### 5.1 — `requirements.txt`

Append these three lines at the end of the file:

```
streamlit>=1.32.0
pandas>=2.0.0
plotly>=5.20.0
```

### 5.2 — `pyproject.toml`

In the `[project] dependencies` list, append the same three entries:

```toml
"streamlit>=1.32.0",
"pandas>=2.0.0",
"plotly>=5.20.0",
```

### 5.3 — Verify

Confirm both files now contain all three entries:

```bash
grep -E "streamlit|pandas|plotly" requirements.txt pyproject.toml
```

**Gate:** All six lines (3 per file) must be present.

---

## Step 6 — Run the Full Regression Suite

Before touching the UI, confirm the import fixes and dependency additions have not broken anything:

```bash
python -m pytest tests/ -q --asyncio-mode=auto
```

**Gate:** Must report `678 passed` (or higher if any tests were previously failing due to the import collision). Zero failures permitted. Fix any regression before proceeding to Step 7.

---

## Step 7 — Refactor `render_metrics()` to `st.columns(5)`

Open `src/ui/dashboard.py`. Locate `render_metrics()` (currently around line 459).

Replace the entire function body with the following implementation. Preserve the function signature `def render_metrics(metrics: dict[str, object]) -> None:` unchanged.

```python
def render_metrics(metrics: dict[str, object]) -> None:
    st.header("📊 Performance Metrics")
    col_pnl, col_win, col_exp, col_decisions, col_positions = st.columns(5)

    total_pnl        = to_decimal(metrics.get("total_pnl", ZERO))
    win_rate_pct     = to_decimal(metrics.get("win_rate", ZERO)) * Decimal("100")
    exposure         = to_decimal(metrics.get("open_exposure", ZERO))
    total_decisions  = int(metrics.get("total_decisions", 0))
    active_positions = int(metrics.get("active_positions", 0))

    pnl_delta          = to_decimal(metrics.get("pnl_delta", ZERO))
    win_rate_delta_pct = to_decimal(metrics.get("win_rate_delta_pct", ZERO))
    exposure_delta     = to_decimal(metrics.get("exposure_delta", ZERO))

    col_pnl.metric(
        label="Realized PnL",
        value=format_usdc(total_pnl),
        delta=format_signed_usdc(pnl_delta),
    )
    col_win.metric(
        label="Win Rate",
        value=f"{win_rate_pct:.2f}%",
        delta=format_signed_pct(win_rate_delta_pct),
    )
    col_exp.metric(
        label="Open Exposure",
        value=format_usdc(exposure),
        delta=format_signed_usdc(exposure_delta),
        delta_color="inverse",
    )
    col_decisions.metric(
        label="Total Decisions",
        value=str(total_decisions),
    )
    col_positions.metric(
        label="Active Positions",
        value=str(active_positions),
    )

    if bool(metrics.get("using_mock_deltas", False)):
        st.caption(
            "No position rows detected. Delta indicators are mock values to preview "
            "positive and negative states."
        )
```

Key changes from the prior implementation:
- `st.columns(3)` → `st.columns(5)` with a five-element destructure
- Two new `st.metric()` cards: `Total Decisions` and `Active Positions`
- Header now includes the `📊` emoji as specified in PRD-v12.0 §4.2
- `total_decisions` and `active_positions` are cast with `int()` — already present in `metrics` dict from `fetch_metrics()`, no query changes required

---

## Step 8 — Verify the Dashboard Launches

Run the Streamlit dashboard and confirm it starts without error:

```bash
streamlit run src/ui/dashboard.py --server.headless true &
sleep 3 && kill %1
```

If Streamlit is not installed in the active environment, install the UI dependencies first:

```bash
pip install streamlit>=1.32.0 pandas>=2.0.0 plotly>=5.20.0
streamlit run src/ui/dashboard.py --server.headless true &
sleep 3 && kill %1
```

**Gate:** No Python `ImportError`, `AttributeError`, or `TypeError` in stdout/stderr.

---

## Step 9 — Final Regression Gate

Run the complete test suite one final time:

```bash
python -m pytest tests/ -q --asyncio-mode=auto
```

**Gate:** ≥ 678 passed, 0 failures.

Run coverage:

```bash
coverage run -m pytest tests/ --asyncio-mode=auto && coverage report -m
```

**Gate:** ≥ 94% total coverage.

---

## Step 10 — Memory Consolidation (Mandatory per CLAUDE.md)

After all gates pass, execute the mandatory Memory Consolidation:

1. Update `STATE.md`:
   - Change active WI from `WI-40` to `WI-41`
   - Record the import fix as a resolved bug entry
   - Confirm test count and coverage numbers

2. Add an entry to the appropriate `.agents/rules/` file documenting:
   - **Invariant P-1 (Position Schema Ownership):** `PositionStatus` is defined in `src/schemas/position.py` and must never be imported from `src/schemas/execution.py`. This violation caused 16 collection errors in Phase 12.

3. Print the `🧠 Memory Consolidation Complete` summary.

---

## Definition of Done — WI-40

WI-40 is complete when ALL of the following are true:

- [ ] `python -m pytest --collect-only 2>&1 | grep "ERROR collecting tests/"` returns empty output
- [ ] `requirements.txt` and `pyproject.toml` each declare `streamlit>=1.32.0`, `pandas>=2.0.0`, `plotly>=5.20.0`
- [ ] `render_metrics()` calls `st.columns(5)` as its sole column call
- [ ] All five metric cards render: Realized PnL, Win Rate, Open Exposure, Total Decisions, Active Positions
- [ ] `st.header("📊 Performance Metrics")` is used (emoji included)
- [ ] `streamlit run src/ui/dashboard.py` starts without error on an empty DB
- [ ] Full regression: ≥ 678 tests passing
- [ ] Coverage: ≥ 94%
- [ ] `STATE.md` updated
- [ ] Memory Consolidation executed

---

## Files Modified Summary

| File | Change |
|---|---|
| `src/agents/execution/exit_strategy_engine.py` | Import fix: split `PositionStatus` to `src.schemas.position` |
| `src/agents/execution/position_tracker.py` | Import fix: split `PositionStatus` to `src.schemas.position` |
| `src/orchestrator.py` | Import fix: split `PositionStatus` to `src.schemas.position` |
| `tests/integration/test_circuit_breaker_integration.py` | Import fix: split `PositionStatus` to `src.schemas.position` |
| `tests/integration/test_telegram_notifier_integration.py` | Import fix: split `PositionStatus` to `src.schemas.position` |
| `requirements.txt` | Add `streamlit>=1.32.0`, `pandas>=2.0.0`, `plotly>=5.20.0` |
| `pyproject.toml` | Add same three dependencies |
| `src/ui/dashboard.py` | `render_metrics()` refactor: `st.columns(5)`, two new metric cards, emoji header |

## Files NOT Modified

| File | Reason |
|---|---|
| `src/schemas/position.py` | `PositionStatus` definition correct; no change needed |
| `src/schemas/execution.py` | No re-export of `PositionStatus` to add; consumers fix their own imports |
| `src/ui/dashboard.py` (all other functions) | Only `render_metrics()` changes; `fetch_metrics()` and all other render functions are unchanged |
| `src/db/` | Read-only phase; zero DB changes |
| `migrations/` | Zero migrations |
| Any file under `src/agents/` not listed above | Out of WI-40 scope |
