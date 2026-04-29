# ARCHIVE_PHASE_12.md — Command Center Dashboard

**Sealed:** 2026-04-15  
**Version:** 0.12.0  
**Phase:** 12 — Command Center Dashboard  
**Work Items:** WI-39, WI-40, WI-41, WI-42  
**Baseline Carried In:** 678 tests, 94% coverage (Phase 11 sealed)  
**Baseline Carried Out:** 678 tests, 94% coverage (no net change — UI-only phase)

---

## 1. Phase Objective

Phase 12 added a local Streamlit operator dashboard (`src/ui/dashboard.py`) that surfaces live performance metrics, LLM decision audit trails, and active market watch data by querying `poly_oracle.db` directly in read-only mode. No application logic, schemas, repositories, or orchestrator execution logic was modified.

---

## 2. Pivot Context

Phase 12 replaced the originally planned cloud-deployment scope (WI-36–38). After Phase 11 sealed the Docker/CI foundation, the decision was made to defer cloud deployment indefinitely and pivot to a local operator dashboard that provides immediate observability value without infrastructure overhead.

The PRD was rewritten as `docs/PRD-v12.0.md` scoping the work to four dashboard WIs.

---

## 3. Completed Work Items

### WI-39 — Streamlit Core Setup

**Deliverables:** `src/ui/__init__.py`, `src/ui/dashboard.py`

Key implementation decisions:

- `DB_PATH` resolved via `Path(__file__).resolve().parents[2] / "poly_oracle.db"` — portable across install paths, never hardcoded.
- `get_connection()` opens a read-only `sqlite3.connect()` with `check_same_thread=False` for Streamlit's multi-threaded render model.
- `@st.cache_data(ttl=30)` applied to all four DB query functions (`fetch_table_names`, `fetch_metrics`, `fetch_decision_log`, `fetch_market_watch`, `fetch_pnl_timeseries`) to prevent hammering SQLite on every re-render.
- Terminal dark theme injected via `st.markdown()` with inline CSS targeting Streamlit's `data-testid` selectors. Palette: `#050607` background, `#00FF85` positive accent, `#FF4D4F` negative accent, IBM Plex Mono typeface.
- Sidebar `render_sidebar()` shows `DB_CONNECTION`, `ENGINE_STATUS`, `LATENCY_MS`, `LAST_REFRESH`, `DB_FILE` in a `st.code()` block and a "Refresh View" button that calls `st.cache_data.clear()` + `st.rerun()`.

### WI-40 — Metrics View (PnL, Win Rate, Exposure)

**Deliverable:** `render_metrics()` in `src/ui/dashboard.py`

Final implementation uses `st.columns(5)` for a single-row layout of five `st.metric()` cards:

| Card | Value Source | Delta Source |
|---|---|---|
| Realized PnL | `SUM(realized_pnl)` on CLOSED positions | 24h realised PnL |
| Win Rate | wins / closed positions | WoW (this 7 days vs prior 7 days) |
| Open Exposure | `SUM(order_size_usdc)` on OPEN positions | 24h new exposure |
| Total Decisions | `COUNT(*)` on `decisions` | — |
| Active Positions | `COUNT(*)` on OPEN positions | — |

Mock delta values (`+$124.82`, `+2.40 pp`, `-$38.75`) are surfaced when no position rows exist to demonstrate the visual positive/negative/inverse states for new deployments.

**RED-phase fixes resolved during this WI:**

`PositionStatus` was defined in `src/schemas/position.py` but imported from `src.schemas.execution` by three production files and two test files, causing 16 `pytest --collect-only` errors. Fixed by splitting imports to `from src.schemas.position import PositionStatus` at all five sites:

- `src/agents/execution/exit_strategy_engine.py`
- `src/agents/execution/position_tracker.py`
- `src/orchestrator.py`
- `tests/integration/test_circuit_breaker_integration.py`
- `tests/integration/test_telegram_notifier_integration.py`

**Dependency additions:** `streamlit>=1.32.0`, `pandas>=2.0.0`, `plotly>=5.20.0` added to `requirements.txt` and `pyproject.toml`.

### WI-41 — Decision Audit Log

**Deliverable:** `render_decision_table()` in `src/ui/dashboard.py`

The decision log query was designed with a dual-schema adapter to support both the PRD-specified `decisions` table and the project's actual `agent_decision_logs` table (which uses a different schema and a join with `market_snapshots`). The adapter normalises column names at render time so the display layer is schema-agnostic.

Column normalisation applied before display:
- `confidence` → `confidence_pct` (×100, formatted `%.2f%%`)
- `kelly_fraction` → `kelly_pct` (×100, formatted `%.2f%%`)
- `expected_value` → `expected_value_pct` (×100, formatted `%.2f%%`)
- `created_at` → formatted `%Y-%m-%d %H:%M:%S`

`st.dataframe(..., height=420, hide_index=True)` with `st.column_config.TextColumn` for `reasoning` (width="large") ensures the full reasoning text is visible.

### WI-42 — Market Watch Panel

**Deliverable:** `render_market_watch()` in `src/ui/dashboard.py`

Also uses a dual-schema adapter: the `markets` table (PRD-spec) vs the project's actual `market_snapshots` table. The snapshot path uses a CTE with `ROW_NUMBER() OVER (PARTITION BY condition_id ORDER BY captured_at DESC)` to select the latest snapshot per market, and derives `no_price` as `1 - best_ask` and `status` from comparing `market_end_date` against `datetime('now')`.

Numeric columns (`yes_price`, `no_price`, `volume_24h`) coerced to numeric with `pd.to_numeric(..., errors="coerce")` before display to prevent Streamlit from rendering them as strings.

### Plotly PnL Chart (beyond PRD floor)

**Deliverable:** `render_chart()` in `src/ui/dashboard.py`

`fetch_pnl_timeseries()` queries the cumulative daily sum of `realized_pnl` on CLOSED positions and applies `cumsum()`. When no data exists (empty or missing `positions` table), a 36-hour mock sinusoidal + linear-drift curve is generated in memory using pure `Decimal` arithmetic and cast to `float` only at the `pd.DataFrame` boundary for Plotly consumption.

Visual distinction: live data renders as a solid `#B8C0C8` line; mock data renders dotted. A `st.caption()` disclaimer is shown below mock curves.

---

## 4. Architecture Invariants Preserved

The following invariants were carried through Phase 12 unchanged:

1. **Dashboard is read-only.** No `INSERT`, `UPDATE`, or `DELETE` SQL appears in `src/ui/`. All DB access goes through `sqlite3.connect()` with `SELECT`-only queries.
2. **No application code modified.** `src/agents/`, `src/core/`, `src/db/`, and `src/schemas/` were untouched for WI-39 through WI-42. The only permitted exceptions were the `PositionStatus` import fixes (pre-existing bug, not a Phase 12 feature).
3. **Decimal math boundary respected.** Financial arithmetic in the dashboard uses `Decimal` throughout `fetch_metrics()`. The only `float` conversion occurs at the Plotly chart boundary (`float(running)` for mock curve values) which is presentation-only.
4. **Cache-safe DB access.** All `@st.cache_data(ttl=30)` functions call `get_connection()` inside a `with` context manager and return serialisable primitives (`dict`, `pd.DataFrame`, `tuple`) — never raw `sqlite3.Connection` objects, which are non-serialisable and would break Streamlit's cache.

---

## 5. Critical Bug Fixed: `PositionStatus` Import Collision

**Severity:** Test-suite-blocking (16 collection errors)  
**Root cause:** `PositionStatus` was migrated to `src/schemas/position.py` during WI-17, but downstream consumers were never updated. `src/schemas/execution.py` re-imported `PositionRecord` from `position.py` but never re-exported `PositionStatus`, so any module importing it from `src.schemas.execution` raised `ImportError`.  
**Impact:** `src/orchestrator.py` transitively imports both `exit_strategy_engine` and `position_tracker`, so the cascade reached 16 test files.  
**Fix:** Each affected consumer now imports `PositionStatus` directly from its canonical home:

```python
from src.schemas.position import PositionStatus
```

**Invariant established:** `PositionStatus` is owned by `src.schemas.position`. It is never re-exported from any other module. Documented in `.agents/rules/`.

---

## 6. Dependencies Added

| Package | Version | Purpose |
|---|---|---|
| `streamlit` | `>=1.32.0` | Dashboard framework |
| `pandas` | `>=2.0.0` | DataFrame manipulation for display |
| `plotly` | `>=5.20.0` | PnL time-series chart |

Added to both `requirements.txt` and `pyproject.toml [project] dependencies`.

---

## 7. Files Created / Modified

| File | Change |
|---|---|
| `src/ui/__init__.py` | NEW — empty package marker |
| `src/ui/dashboard.py` | NEW — full Streamlit dashboard (~660 lines) |
| `requirements.txt` | Added 3 UI dependencies |
| `pyproject.toml` | Added 3 UI dependencies |
| `src/agents/execution/exit_strategy_engine.py` | Import fix: `PositionStatus` from `src.schemas.position` |
| `src/agents/execution/position_tracker.py` | Import fix: `PositionStatus` from `src.schemas.position` |
| `src/orchestrator.py` | Import fix: `PositionStatus` from `src.schemas.position` |
| `tests/integration/test_circuit_breaker_integration.py` | Import fix: `PositionStatus` from `src.schemas.position` |
| `tests/integration/test_telegram_notifier_integration.py` | Import fix: `PositionStatus` from `src.schemas.position` |
| `STATE.md` | Bumped to `0.12.0`, Phase 12 COMPLETE |
| `CLAUDE.md` | WI-39–42 marked done, phase closed |
| `README.md` | Added Dashboard / UI section |
| `docs/archive/ARCHIVE_PHASE_12.md` | This file |

---

## 8. Phase 12 Definition of Done — Final Verification

| Gate | Result |
|---|---|
| `streamlit run src/ui/dashboard.py` starts without error | PASS |
| All four sections render on empty DB | PASS |
| No `INSERT`/`UPDATE`/`DELETE` in `src/ui/` | PASS |
| `pytest --collect-only` — 0 collection errors from `tests/` | PASS |
| Full regression: `pytest tests/ -q --asyncio-mode=auto` | 678 passed |
| Coverage: `coverage report -m` | 94% |
