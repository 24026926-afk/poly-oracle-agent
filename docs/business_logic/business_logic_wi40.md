# Business Logic — WI-40 & Phase 12 Fixes

**Phase:** 12 — Command Center Dashboard  
**WI:** WI-40 (Metrics View), plus Phase 12 RED-phase fixes  
**Date:** 2026-04-15  
**Author:** Lead Architect  
**Status:** Pre-implementation specification

---

## 1. Overview

This document specifies the three distinct engineering concerns that must be resolved before WI-40 can be declared complete:

1. **RED PHASE — `PositionStatus` import collision** (16 pytest collection errors blocking the test suite)
2. **RED PHASE — Missing UI dependencies** (`streamlit`, `pandas`, `plotly` absent from package manifests)
3. **WI-40 — Metrics layout refactor** (`st.columns(3)` → `st.columns(5)` per PRD-v12.0 §4.2)

Each section below provides root cause analysis, the invariant violated, and the precise fix to apply.

---

## 2. RED PHASE — `PositionStatus` Import Collision

### 2.1 Root Cause

`PositionStatus` is defined exclusively in `src/schemas/position.py:20`:

```python
class PositionStatus(str, Enum):
    OPEN   = "OPEN"
    CLOSED = "CLOSED"
    FAILED = "FAILED"
```

`src/schemas/execution.py` imports `PositionRecord` from `position.py` (line 16) to resolve a forward reference, but **does not import or re-export `PositionStatus`**. Therefore `PositionStatus` is not an attribute of the `src.schemas.execution` module namespace.

Three production source files import `PositionStatus` from the wrong module:

| File | Import line | Symptom |
|---|---|---|
| `src/agents/execution/exit_strategy_engine.py` | L20–27 | `ImportError` on every module that imports `ExitStrategyEngine` |
| `src/agents/execution/position_tracker.py` | L22–23 | `ImportError` in all tracker-dependent tests |
| `src/orchestrator.py` | L55–56 | `ImportError` in 8+ integration suites |

Because `src/orchestrator.py` is imported transitively by a large fraction of the integration test suite, a single wrong import there cascades into **16 collection errors**.

### 2.2 Cascade Failure Map

```
src.schemas.execution  (PositionStatus NOT exported)
  └── imported by src.agents.execution.exit_strategy_engine  ← direct error
  └── imported by src.agents.execution.position_tracker      ← direct error
  └── imported by src.orchestrator                           ← direct error
        └── transitively imported by:
              tests/integration/test_alert_engine_integration.py
              tests/integration/test_bankroll_sync_integration.py
              tests/integration/test_circuit_breaker_integration.py
              tests/integration/test_exit_order_router_integration.py
              tests/integration/test_exit_scan_integration.py
              tests/integration/test_lifecycle_reporter_integration.py
              tests/integration/test_orchestrator.py
              tests/integration/test_pnl_settlement_integration.py
              tests/integration/test_portfolio_aggregator_integration.py
              tests/integration/test_telegram_notifier_integration.py
              tests/integration/test_wi29_live_fees_integration.py
              tests/integration/test_wi30_exposure_limits_integration.py
              tests/integration/test_wi31_live_balances_integration.py
              tests/unit/test_exit_scan_loop.py
              tests/unit/test_wi29_live_fees.py
              tests/unit/test_wi30_exposure_limits.py
```

Additionally, some integration test files contain direct wrong imports of `PositionStatus` from `src.schemas.execution`:

| Test file | Incorrect import line |
|---|---|
| `tests/integration/test_circuit_breaker_integration.py` | L20–28 |
| `tests/integration/test_telegram_notifier_integration.py` | L23–31 |

### 2.3 Invariant Violated

> **Invariant P-1 (Position Schema Ownership):** `PositionStatus` is owned by `src.schemas.position`. No other module re-exports it. All consumers must import it directly from `src.schemas.position`.

This invariant was established during WI-17 (position schema) and encoded in the original `position.py` docstring. The violation was introduced silently, likely via a refactor that moved `PositionStatus` out of `execution.py` into `position.py` without updating downstream consumers.

### 2.4 Fix Specification

**Rule:** Any `from src.schemas.execution import ..., PositionStatus, ...` must be split into two imports:

```python
# WRONG
from src.schemas.execution import PositionRecord, PositionStatus, ...

# CORRECT
from src.schemas.execution import PositionRecord, ...        # keep execution-owned symbols here
from src.schemas.position import PositionStatus              # PositionStatus comes from position.py
```

**Files requiring the fix (production source):**

1. **`src/agents/execution/exit_strategy_engine.py`** — Replace `PositionStatus` in the `from src.schemas.execution import (...)` block with a new line `from src.schemas.position import PositionStatus`.

2. **`src/agents/execution/position_tracker.py`** — Same split: remove `PositionStatus` from the execution import; add `from src.schemas.position import PositionStatus`.

3. **`src/orchestrator.py`** — Same split at L55–56.

**Files requiring the fix (tests):**

4. **`tests/integration/test_circuit_breaker_integration.py`** — Remove `PositionStatus` from the `src.schemas.execution` import block; add `from src.schemas.position import PositionStatus`.

5. **`tests/integration/test_telegram_notifier_integration.py`** — Same.

> **Note:** `src/agents/execution/pnl_calculator.py` imports `PositionRecord` from `src.schemas.execution`. This is safe because `execution.py` re-exports `PositionRecord` as a module-level name (it is imported at module scope on line 16 of `execution.py`). No change needed there.

### 2.5 Verification Gate

After applying the fixes, `python -m pytest --collect-only` must report **0 collection errors** from the `tests/` directory. The `scripts/test_ws.py` `AsyncWebSocketClient` error is a separate pre-existing issue unrelated to this WI.

---

## 3. RED PHASE — Missing UI Dependencies

### 3.1 Root Cause

`src/ui/dashboard.py` imports three packages at the top level:

```python
import pandas as pd          # line 10
import plotly.express as px  # line 11
import streamlit as st        # line 12
```

Neither `requirements.txt` nor the `[project] dependencies` block in `pyproject.toml` lists these packages. Any clean-environment install (`pip install -e .` or `uv sync`) will produce an `ImportError` or `ModuleNotFoundError` when launching the dashboard, and CI containers will fail identically.

### 3.2 Invariant Violated

> **Invariant D-1 (Dependency Completeness):** Every importable package used by production source files under `src/` must be declared in both `requirements.txt` and `pyproject.toml [project] dependencies`. This is a hard gate enforced during containerization (Phase 11, WI-34).

### 3.3 Fix Specification

**`requirements.txt`** — append the following three lines:

```
streamlit>=1.32.0
pandas>=2.0.0
plotly>=5.20.0
```

`pandas>=2.0.0` is the minimum specified in PRD-v12.0 §6. `plotly>=5.20.0` aligns with the `plotly.express` API used in `render_chart()` (`px.line`, `fig.update_traces`, dark template).

**`pyproject.toml`** — append the same three entries to the `dependencies` list:

```toml
dependencies = [
    ...existing entries...,
    "streamlit>=1.32.0",
    "pandas>=2.0.0",
    "plotly>=5.20.0",
]
```

> **Verification note:** Run `pip show pandas` before adding to confirm whether `pandas` is already installed as a transitive dependency. Even if it is, it must still be declared explicitly per Invariant D-1.

### 3.4 Verification Gate

`python -c "import streamlit; import pandas; import plotly"` must succeed in a fresh virtual environment after `pip install -e .`.

---

## 4. WI-40 — Metrics Layout Refactor: `st.columns(3)` → `st.columns(5)`

### 4.1 Current State

`render_metrics()` in `src/ui/dashboard.py` (line 461) uses:

```python
col_pnl, col_win, col_exp = st.columns(3)
```

Only three of the five required metric cards are rendered:
- Realized PnL
- Win Rate
- Open Exposure

The two remaining cards mandated by PRD-v12.0 §4.1 are missing:
- **Total Decisions** (source: `decisions` table, `COUNT(*)`)
- **Active Positions** (source: `positions` table, `COUNT(*)` where `status='OPEN'`)

Both values are already fetched and present in the `metrics` dict returned by `fetch_metrics()` under keys `"total_decisions"` and `"active_positions"`.

### 4.2 PRD Mandate (PRD-v12.0 §4.2)

> Use `st.columns(5)` to render each metric as an `st.metric()` card in a single row.

### 4.3 Fix Specification

Replace the 3-column destructuring with a 5-column destructuring and render all five cards:

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

### 4.4 Key Design Decisions

| Decision | Rationale |
|---|---|
| `total_decisions` and `active_positions` have no `delta=` argument | No meaningful delta is defined for count metrics in the PRD. Omitting `delta` is cleaner than surfacing `None` or `0`. |
| `str(total_decisions)` for value | `st.metric()` accepts a string value. Using `str()` prevents Streamlit from auto-formatting integers with commas, which is visually inconsistent with the monospace terminal theme. |
| Header literal includes `📊` emoji | PRD-v12.0 §4.2 specifies `st.header("📊 Performance Metrics")`. The existing code used `"Performance Metrics"` without the emoji — this is a spec deviation to correct. |
| Column order: PnL → Win Rate → Exposure → Decisions → Positions | Descending financial impact. PnL and win rate are primary operator concerns; counts are secondary. |

### 4.5 Data Availability

Both `"total_decisions"` and `"active_positions"` are populated by `fetch_metrics()` which already runs the following queries:

```sql
-- total_decisions (decisions table)
SELECT COUNT(*) FROM decisions

-- active_positions (positions table)
SELECT COALESCE(SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END), 0) AS active_positions
FROM positions
```

No changes to `fetch_metrics()` are required. The data keys are available; only the render layer needs updating.

### 4.6 Empty / Missing Table Handling

PRD-v12.0 §4.3 requires graceful handling when tables are empty or absent. `fetch_metrics()` already handles this:
- Returns `total_decisions = 0` and `active_positions = 0` when the tables are empty or missing.
- `render_metrics()` must not crash on zero values — `str(0)` is safe.

### 4.7 Definition of Done — WI-40

- [ ] `st.columns(5)` is the only column call in `render_metrics()`.
- [ ] All five `st.metric()` cards render in a single row: Realized PnL, Win Rate, Open Exposure, Total Decisions, Active Positions.
- [ ] `st.header("📊 Performance Metrics")` used (emoji included).
- [ ] On an empty DB all five metrics show `$0.00`, `0.00%`, `$0.00`, `0`, `0` respectively without raising any exception.
- [ ] `streamlit run src/ui/dashboard.py` starts cleanly with the 5-column row visible.

---

## 5. Execution Sequence

Apply fixes in this order to avoid compounding errors:

1. **Fix `PositionStatus` imports** in source files (`exit_strategy_engine.py`, `position_tracker.py`, `orchestrator.py`).
2. **Fix `PositionStatus` imports** in test files (`test_circuit_breaker_integration.py`, `test_telegram_notifier_integration.py`).
3. **Run `pytest --collect-only`** — verify 0 collection errors from `tests/`.
4. **Add UI dependencies** to `requirements.txt` and `pyproject.toml`.
5. **Refactor `render_metrics()`** to `st.columns(5)` with all five cards.
6. **Run full test suite** — gate: existing ≥94% coverage must not regress.
7. **Launch dashboard** — `streamlit run src/ui/dashboard.py` — verify 5-column metrics row.

---

## 6. Files Modified Summary

| File | Change Type | Scope |
|---|---|---|
| `src/agents/execution/exit_strategy_engine.py` | Import fix | Split `PositionStatus` to `src.schemas.position` |
| `src/agents/execution/position_tracker.py` | Import fix | Split `PositionStatus` to `src.schemas.position` |
| `src/orchestrator.py` | Import fix | Split `PositionStatus` to `src.schemas.position` |
| `tests/integration/test_circuit_breaker_integration.py` | Import fix | Split `PositionStatus` to `src.schemas.position` |
| `tests/integration/test_telegram_notifier_integration.py` | Import fix | Split `PositionStatus` to `src.schemas.position` |
| `requirements.txt` | Dependency add | `streamlit>=1.32.0`, `pandas>=2.0.0`, `plotly>=5.20.0` |
| `pyproject.toml` | Dependency add | Same three packages |
| `src/ui/dashboard.py` | WI-40 feature | `st.columns(5)`, two new metric cards, emoji header |

**No changes to:** application logic, DB schemas, repositories, orchestrator business logic, or any file outside `src/ui/` for WI-40 (per PRD-v12.0 §2 out-of-scope constraint).
