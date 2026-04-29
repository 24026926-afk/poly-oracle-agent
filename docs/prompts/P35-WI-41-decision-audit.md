# P35-WI-41 — Decision Audit Log Verification & Signoff

## Execution Target

- Primary: Claude Code implementation agent ("Maker")
- Branch discipline: work directly on `develop` — Phase 12 UI-only, no feature branch required
- MAAP: not required for Phase 12 (PRD-v12.0 §4)

---

## Role

You are performing a spec-compliance audit and fix pass for WI-41 of Phase 12. The `render_decision_table()` function in `src/ui/dashboard.py` is mostly correct but contains one known spec deviation identified during Lead Architect review. Your job is to:

1. Read and internalize the business logic specification.
2. Audit the implementation against it.
3. Fix the identified deviation (and any others you discover).
4. Run the regression suite to confirm no breakage.

Phase 12 is **read-only UI/observability**. No application logic (`src/agents/`, `src/core/`, `src/db/`, `src/schemas/`) may change during this pass.

---

## Mandatory Context Hydration

Read these files before any edits:

1. `docs/business_logic/business_logic_wi41.md` — **primary specification**
2. `docs/PRD-v12.0.md` §5 (WI-41 section)
3. `src/ui/dashboard.py` — full file; understand both `fetch_decision_log()` and `render_decision_table()`

Do not proceed until all three are loaded.

---

## Known Deviation (Lead Architect Pre-Audit)

The following deviation was confirmed during pre-audit. Fix it as Step 1.

### D-41-1 — Section Header Missing Emoji

**Location:** `src/ui/dashboard.py`, `render_decision_table()`, first line of function body.

**Current:**
```python
st.header("LLM Decision Audit Log")
```

**Required (PRD-v12.0 §5.2 and business_logic_wi41.md §4.1):**
```python
st.header("🧠 LLM Decision Audit Log")
```

The emoji is part of the spec literal. This is the only confirmed deviation.

---

## Audit Checklist

After fixing D-41-1, verify each item below against the current implementation. For any item that does not match the spec, apply the minimum fix required and document what you changed.

### Fetch Layer (`fetch_decision_log()`)

| # | Check | Spec Reference |
|---|---|---|
| F-1 | Primary query selects from `decisions` when table exists | BL §3.1 |
| F-2 | Primary query is `ORDER BY created_at DESC LIMIT 20` | BL §3.1 |
| F-3 | Compatibility query joins `agent_decision_logs` with `market_snapshots` on `snapshot_id` | BL §3.2 |
| F-4 | Compatibility query selects `reasoning_log` and aliases to `reasoning` | BL §3.2 |
| F-5 | `@st.cache_data(ttl=30)` applied | PRD §3.2 |
| F-6 | Returns empty `pd.DataFrame()` on any exception | BL §6 |

### Render Layer (`render_decision_table()`)

| # | Check | Spec Reference |
|---|---|---|
| R-1 | Header is `st.header("🧠 LLM Decision Audit Log")` — emoji included | BL §4.1 |
| R-2 | Empty state renders `st.info("No decisions logged yet.")` and returns | BL §6 |
| R-3 | `st.dataframe(...)` is called with `width="stretch"` | BL §4.2 |
| R-4 | `hide_index=True` | BL §4.2 |
| R-5 | Fixed `height` set (420px or equivalent) | BL §4.2 |
| R-6 | `reasoning` configured as `st.column_config.TextColumn` with `width="large"` | BL §4.2 |
| R-7 | Caption renders `st.caption(f"Showing last {len(df)} decisions")` | BL §4.2, §5 |
| R-8 | `reasoning` column is present in `display_columns` list — never excluded | BL §2 |
| R-9 | Column normalisation handles the legacy `agent_decision_logs` field names | BL §3.2 |

For each item: mark **PASS** or **FAIL** with a one-line note. Fix any FAILs.

---

## Execution Steps

### Step 1 — Apply D-41-1 Fix

In `src/ui/dashboard.py`, locate `render_decision_table()` and change:

```python
st.header("LLM Decision Audit Log")
```

to:

```python
st.header("🧠 LLM Decision Audit Log")
```

### Step 2 — Run Audit Checklist

Work through every item in the checklist above. Apply fixes where needed.

### Step 3 — Regression Gate

Run the full test suite:

```bash
python -m pytest tests/ -q --asyncio-mode=auto
```

**Gate:** ≥ 678 passed, 0 failures. Fix any regression before proceeding.

### Step 4 — Dashboard Launch Check

Confirm the dashboard starts without error:

```bash
streamlit run src/ui/dashboard.py --server.headless true &
sleep 4 && kill %1
```

**Gate:** No `ImportError`, `AttributeError`, or `TypeError` in output.

### Step 5 — Memory Consolidation

Update `STATE.md` to reflect WI-41 formally signed off. Add an entry under the Phase 12 work items block confirming the audit pass and the D-41-1 fix.

---

## Definition of Done — WI-41

WI-41 signoff is complete when ALL of the following are true:

- [ ] `st.header("🧠 LLM Decision Audit Log")` — exact literal, emoji present
- [ ] Audit checklist F-1 through F-6 and R-1 through R-9 all PASS
- [ ] `python -m pytest tests/ -q --asyncio-mode=auto` → ≥ 678 passed, 0 failures
- [ ] `streamlit run src/ui/dashboard.py` starts without error
- [ ] `STATE.md` updated with WI-41 audit signoff note

---

## Files in Scope

| File | Permitted Change |
|---|---|
| `src/ui/dashboard.py` | Fix D-41-1 (header emoji) and any other spec deviations found during audit |
| `STATE.md` | WI-41 signoff note |

**No other files may be modified.**
