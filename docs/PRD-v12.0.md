# PRD-v12.0 — Phase 12: Command Center Dashboard

**Version:** 12.0  
**Status:** ACTIVE  
**Phase:** 12  
**Author:** Lead Full-Stack Engineer  
**Date:** 2026-04-15  
**Baseline:** Phase 11 sealed — 678 tests, 94% coverage, multi-stage Docker image, CI pipeline

---

## 1. Objective

Build a local Streamlit dashboard (`src/ui/dashboard.py`) that connects directly to `poly_oracle.db` and surfaces live performance metrics, LLM decision audit trails, and active market watch data for the operator.

Phase 12 is purely UI/observability. No application logic, schemas, repositories, or orchestrator execution logic will change.

---

## 2. Scope Boundaries

**In scope:**
- Streamlit dashboard with dark theme
- SQLite read-only queries against `poly_oracle.db`
- PnL, Win Rate, and Exposure metrics view
- LLM decision audit log (last 20 decisions with reasoning)
- Live tracked markets panel
- Sidebar system status indicator

**Out of scope:**
- Cloud deployment (deferred indefinitely)
- Writing to the database from the UI
- Authentication / access control
- Real-time WebSocket streaming into the UI (polling only)
- CI/CD pipeline modification (Phase 11 sealed)

---

## 3. Work Items

### WI-39 — Streamlit Core Setup

**Goal:** Bootstrap `src/ui/dashboard.py` with a working DB connection to `poly_oracle.db` and a dark-themed layout.

#### 3.1 File Structure

```
src/
└── ui/
    ├── __init__.py
    └── dashboard.py
```

#### 3.2 Core Requirements

- Import `streamlit`, `pandas`, and `sqlite3`.
- Locate `poly_oracle.db` relative to the project root (use `pathlib.Path`).
- Set page config: title `"Poly-Oracle Command Center"`, layout `"wide"`, dark theme via `config.toml` or inline.
- Sidebar shows:
  - System status (DB reachable: ✅ / ❌)
  - Last refresh timestamp
  - Manual refresh button (`st.button("Refresh")`)
- Use `st.cache_data(ttl=30)` on all DB query functions to avoid hammering SQLite on every re-render.

#### 3.3 DB Connection

```python
DB_PATH = Path(__file__).resolve().parents[2] / "poly_oracle.db"

def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, check_same_thread=False)
```

#### 3.4 Definition of Done — WI-39

- [ ] `streamlit run src/ui/dashboard.py` launches without error.
- [ ] Page title renders as `"Poly-Oracle Command Center"`.
- [ ] Sidebar displays DB reachable status.
- [ ] No writes to the database occur under any code path in the dashboard.

---

### WI-40 — Metrics View (PnL, Win Rate, Exposure)

**Goal:** Display aggregate performance metrics pulled from the `decisions` and `positions` tables.

#### 4.1 Metrics to Surface

| Metric | Source Table | Derivation |
|---|---|---|
| Total Realised PnL | `positions` | `SUM(realized_pnl)` on closed positions |
| Win Rate | `decisions` | `COUNT(action='BUY' AND outcome='WIN') / COUNT(action='BUY')` |
| Open Exposure (USDC) | `positions` | `SUM(size_usdc)` where `status='OPEN'` |
| Total Decisions | `decisions` | `COUNT(*)` |
| Active Positions | `positions` | `COUNT(*)` where `status='OPEN'` |

#### 4.2 Layout

- Use `st.columns(5)` to render each metric as an `st.metric()` card in a single row.
- Section header: `st.header("📊 Performance Metrics")`.
- If the table is empty or does not yet exist, display `st.info("No data yet.")` gracefully — never crash.

#### 4.3 Definition of Done — WI-40

- [ ] Five metric cards render in one row.
- [ ] Metrics query returns 0 / 0% gracefully on an empty DB.
- [ ] No unhandled exceptions on missing tables.

---

### WI-41 — Decision Audit Log

**Goal:** Show the last 20 LLM decisions including the full `reasoning` field in a scrollable table.

#### 5.1 Query

```sql
SELECT
    created_at,
    market_id,
    action,
    confidence,
    reasoning,
    kelly_fraction
FROM decisions
ORDER BY created_at DESC
LIMIT 20;
```

#### 5.2 Layout

- Section header: `st.header("🧠 LLM Decision Audit Log")`.
- Render with `st.dataframe(df, use_container_width=True)`.
- `reasoning` column should be left-aligned and wrap text (configure via `st.dataframe` column config if Streamlit version supports it).
- Display row count: `st.caption(f"Showing last {len(df)} decisions")`.

#### 5.3 Definition of Done — WI-41

- [ ] Table renders last 20 decisions when data exists.
- [ ] `reasoning` column is visible and readable (not truncated to ellipsis at display level).
- [ ] Empty state shows `st.info("No decisions logged yet.")`.

---

### WI-42 — Market Watch Panel

**Goal:** Display all currently tracked markets with their latest ingested price and status.

#### 6.1 Query

```sql
SELECT
    market_id,
    question,
    yes_price,
    no_price,
    volume_24h,
    end_date,
    status
FROM markets
ORDER BY volume_24h DESC;
```

#### 6.2 Layout

- Section header: `st.header("🌐 Market Watch")`.
- Render with `st.dataframe(df, use_container_width=True)`.
- Show market count: `st.caption(f"{len(df)} markets tracked")`.
- If no markets, show `st.info("No markets ingested yet.")`.

#### 6.3 Definition of Done — WI-42

- [ ] All tracked markets display sorted by 24h volume descending.
- [ ] Columns `yes_price`, `no_price`, `volume_24h` are numeric and formatted to 4 decimal places where applicable.
- [ ] Empty state handled gracefully.

---

## 4. Phase 12 Definition of Done

Phase 12 is complete when **all four WI DoDs are satisfied** and the following global gate passes:

1. **Launch gate:** `streamlit run src/ui/dashboard.py` starts and displays all four sections without error on a populated OR empty DB.
2. **Read-only gate:** No `INSERT`, `UPDATE`, or `DELETE` SQL appears anywhere in `src/ui/`.
3. **Graceful empty state gate:** All sections handle an empty or missing DB without raising an unhandled exception.
4. **No regression gate:** Existing test suite (`pytest`) continues to pass at ≥ 94% coverage after dashboard files are added.

---

## 5. Constraints & Non-Negotiables

1. Dashboard is **read-only**. No writes to SQLite from any UI code path.
2. No application code (`src/agents/`, `src/core/`, `src/db/`, `src/schemas/`) may be modified during Phase 12.
3. New dependencies (`streamlit`, `pandas` if not already present) must be added to `requirements.txt` and `pyproject.toml`.
4. MAAP audit is not required for Phase 12 (UI/observability scope only).

---

## 6. Dependencies to Add

| Package | Minimum Version | Purpose |
|---|---|---|
| `streamlit` | `>=1.32.0` | Dashboard framework |
| `pandas` | `>=2.0.0` | DataFrame manipulation for display |

`pandas` may already be present as a transitive dependency; verify before adding.

---

## 7. Deliverables Summary

| WI | Deliverable |
|---|---|
| WI-39 | `src/ui/__init__.py`, `src/ui/dashboard.py` — core layout and DB connection |
| WI-40 | Metrics section in dashboard — PnL, Win Rate, Exposure cards |
| WI-41 | Decision audit log section — last 20 LLM decisions with reasoning |
| WI-42 | Market watch section — all tracked markets sorted by volume |

---

## 8. State & Documentation Updates on Phase Completion

On Phase 12 completion:
1. `STATE.md` version bumped to `0.12.0`, status updated to "Phase 12 — COMPLETE".
2. `CLAUDE.md` current WI set updated to reflect WI-39 through WI-42 complete.
3. `README.md` updated with a "Dashboard" section: how to run `streamlit run src/ui/dashboard.py`.
4. `docs/archive/ARCHIVE_PHASE_12.md` generated (per AGENTS.md PHASE COMPLETION AUTOMATION rule).
