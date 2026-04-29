# P36-WI-42 — Market Watch Panel Verification & Signoff

## Execution Target

- Primary: Claude Code implementation agent ("Maker")
- Branch discipline: work directly on `develop` — Phase 12 UI-only, no feature branch required
- MAAP: not required for Phase 12 (PRD-v12.0 §4)

---

## Role

You are performing a spec-compliance audit and fix pass for WI-42 of Phase 12. The `render_market_watch()` function and its backing `fetch_market_watch()` query in `src/ui/dashboard.py` contain three known spec deviations identified during Lead Architect review. Your job is to:

1. Read and internalize the business logic specification.
2. Audit the implementation against it.
3. Fix all three identified deviations (and any others you discover).
4. Run the regression suite to confirm no breakage.

Phase 12 is **read-only UI/observability**. No application logic (`src/agents/`, `src/core/`, `src/db/`, `src/schemas/`) may change during this pass.

---

## Mandatory Context Hydration

Read these files before any edits:

1. `docs/business_logic/business_logic_wi42.md` — **primary specification**
2. `docs/PRD-v12.0.md` §6 (WI-42 section)
3. `src/ui/dashboard.py` — full file; understand both `fetch_market_watch()` and `render_market_watch()`

Do not proceed until all three are loaded.

---

## Known Deviations (Lead Architect Pre-Audit)

Three deviations were confirmed during pre-audit. Fix them in the order listed.

### D-42-1 — Section Header Missing Emoji

**Location:** `src/ui/dashboard.py`, `render_market_watch()`, first line of function body.

**Current:**
```python
st.header("Market Watch")
```

**Required (PRD-v12.0 §6.2 and business_logic_wi42.md §4.1):**
```python
st.header("🌐 Market Watch")
```

---

### D-42-2 — Primary `markets` Query Does Not Filter to Active Markets

**Location:** `src/ui/dashboard.py`, `fetch_market_watch()`, the `if "markets" in tables:` branch.

**Current query:**
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
ORDER BY volume_24h DESC
```

**Required (business_logic_wi42.md §3.1):**
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
WHERE UPPER(COALESCE(status, 'ACTIVE')) = 'ACTIVE'
ORDER BY COALESCE(volume_24h, 0) DESC
```

**Why:** The WI-42 spec restricts the market watch to active markets only. Without the `WHERE` clause, closed markets appear in the panel, violating the data contract in BL §2.

**`COALESCE(status, 'ACTIVE')`** ensures rows where `status` is `NULL` (newly ingested, not yet classified) are treated as active and included rather than silently dropped.

---

### D-42-3 — Compatibility `market_snapshots` Query Does Not Filter Expired Markets

**Location:** `src/ui/dashboard.py`, `fetch_market_watch()`, the `if "market_snapshots" in tables:` branch.

**Current query (relevant WHERE clause):**
```sql
FROM latest
WHERE row_num = 1
ORDER BY COALESCE(volume_24h_usdc, 0) DESC
```

The computed `status` column (`ACTIVE` vs `CLOSED`) is derived but never used as a filter — expired markets appear in the result set.

**Required (business_logic_wi42.md §3.2):**
```sql
FROM latest
WHERE row_num = 1
  AND (
        market_end_date IS NULL
        OR datetime(market_end_date) >= datetime('now')
      )
ORDER BY COALESCE(volume_24h_usdc, 0) DESC
```

Additionally, the `status` derived column should be hardcoded to `'ACTIVE'` for these rows (they are all active by construction after the filter):

```sql
'ACTIVE' AS status
```

Replace the current dynamic `CASE ... THEN 'CLOSED' ELSE 'ACTIVE' END AS status` expression with the literal `'ACTIVE' AS status` — the filter ensures only active markets reach the output, so the `CASE` is redundant.

**Why:** The spec states WI-42 shows active tracked markets only. Expired `market_snapshots` rows represent markets that have already resolved and are not relevant to live operator monitoring.

---

## Full Fixed `fetch_market_watch()` Implementation

Apply all three SQL fixes together. Replace the entire `fetch_market_watch()` function body with the following:

```python
@st.cache_data(ttl=30)
def fetch_market_watch() -> pd.DataFrame:
    tables = set(fetch_table_names())
    if not tables:
        return pd.DataFrame()

    try:
        with get_connection() as conn:
            if "markets" in tables:
                return pd.read_sql_query(
                    """
                    SELECT
                        market_id,
                        question,
                        yes_price,
                        no_price,
                        volume_24h,
                        end_date,
                        status
                    FROM markets
                    WHERE UPPER(COALESCE(status, 'ACTIVE')) = 'ACTIVE'
                    ORDER BY COALESCE(volume_24h, 0) DESC
                    """,
                    conn,
                )

            if "market_snapshots" in tables:
                return pd.read_sql_query(
                    """
                    WITH latest AS (
                        SELECT
                            condition_id,
                            question,
                            best_bid,
                            best_ask,
                            volume_24h_usdc,
                            market_end_date,
                            captured_at,
                            ROW_NUMBER() OVER (
                                PARTITION BY condition_id
                                ORDER BY captured_at DESC
                            ) AS row_num
                        FROM market_snapshots
                    )
                    SELECT
                        condition_id AS market_id,
                        question,
                        best_bid AS yes_price,
                        CASE
                            WHEN best_ask IS NULL THEN NULL
                            ELSE (1 - best_ask)
                        END AS no_price,
                        volume_24h_usdc AS volume_24h,
                        market_end_date AS end_date,
                        'ACTIVE' AS status
                    FROM latest
                    WHERE row_num = 1
                      AND (
                            market_end_date IS NULL
                            OR datetime(market_end_date) >= datetime('now')
                          )
                    ORDER BY COALESCE(volume_24h_usdc, 0) DESC
                    """,
                    conn,
                )
    except Exception:
        return pd.DataFrame()

    return pd.DataFrame()
```

---

## Audit Checklist

After applying all three fixes, verify each item below. For any item that does not match the spec, apply the minimum fix and document what you changed.

### Fetch Layer (`fetch_market_watch()`)

| # | Check | Spec Reference |
|---|---|---|
| F-1 | `@st.cache_data(ttl=30)` applied | PRD §3.2 |
| F-2 | Primary query: `WHERE UPPER(COALESCE(status, 'ACTIVE')) = 'ACTIVE'` | BL §3.1 |
| F-3 | Primary query: `ORDER BY COALESCE(volume_24h, 0) DESC` | BL §3.1 |
| F-4 | Compatibility query: expiry filter `datetime(market_end_date) >= datetime('now')` | BL §3.2 |
| F-5 | Compatibility query: `ROW_NUMBER() OVER (PARTITION BY condition_id ORDER BY captured_at DESC)` | BL §3.2 |
| F-6 | Compatibility query: `no_price` derived as `1 - best_ask` | BL §3.2 |
| F-7 | Returns empty `pd.DataFrame()` on any exception | BL §6 |

### Render Layer (`render_market_watch()`)

| # | Check | Spec Reference |
|---|---|---|
| R-1 | Header is `st.header("🌐 Market Watch")` — emoji present | BL §4.1 |
| R-2 | Empty state renders `st.info("No markets ingested yet.")` and returns | BL §6 |
| R-3 | `yes_price`, `no_price`, `volume_24h` coerced with `pd.to_numeric(..., errors="coerce")` | BL §4.2 |
| R-4 | `st.dataframe(...)` called with `width="stretch"` | BL §4.3 |
| R-5 | `hide_index=True` | BL §4.3 |
| R-6 | Fixed `height` set (420px or equivalent) | BL §4.3 |
| R-7 | `yes_price` → `NumberColumn(format="%.4f")` | BL §4.3, PRD §6.3 |
| R-8 | `no_price` → `NumberColumn(format="%.4f")` | BL §4.3, PRD §6.3 |
| R-9 | `volume_24h` → `NumberColumn(format="$%.2f")` | BL §4.3, PRD §6.3 |
| R-10 | Caption renders `st.caption(f"{len(df)} markets tracked")` | BL §4.3 |

---

## Execution Steps

### Step 1 — Apply D-42-1 (Header Emoji)

In `render_market_watch()`, change:

```python
st.header("Market Watch")
```

to:

```python
st.header("🌐 Market Watch")
```

### Step 2 — Apply D-42-2 and D-42-3 (SQL Query Fixes)

Replace the entire `fetch_market_watch()` function with the implementation provided in the **Full Fixed Implementation** section above. Do not change any other function.

### Step 3 — Run Audit Checklist

Work through every item in the checklist above. Apply fixes where needed.

### Step 4 — Regression Gate

Run the full test suite:

```bash
python -m pytest tests/ -q --asyncio-mode=auto
```

**Gate:** ≥ 678 passed, 0 failures. Fix any regression before proceeding.

Note: the SQL changes only affect `@st.cache_data`-decorated functions that are never called by the test suite — they execute only when Streamlit renders. Zero test failures are expected from these changes. If any test fails, investigate before proceeding.

### Step 5 — Dashboard Launch Check

Confirm the dashboard starts without error:

```bash
streamlit run src/ui/dashboard.py --server.headless true &
sleep 4 && kill %1
```

**Gate:** No `ImportError`, `AttributeError`, or `TypeError` in output.

### Step 6 — Memory Consolidation

Update `STATE.md` to reflect WI-42 formally signed off. Add an entry under the Phase 12 work items block confirming the audit pass, the three deviations found and fixed (D-42-1, D-42-2, D-42-3), and the final regression count.

---

## Definition of Done — WI-42

WI-42 signoff is complete when ALL of the following are true:

- [ ] `st.header("🌐 Market Watch")` — exact literal, emoji present
- [ ] `markets` query includes `WHERE UPPER(COALESCE(status, 'ACTIVE')) = 'ACTIVE'`
- [ ] `market_snapshots` compatibility query filters out expired markets via `datetime(market_end_date) >= datetime('now')`
- [ ] Audit checklist F-1 through F-7 and R-1 through R-10 all PASS
- [ ] `python -m pytest tests/ -q --asyncio-mode=auto` → ≥ 678 passed, 0 failures
- [ ] `streamlit run src/ui/dashboard.py` starts without error
- [ ] `STATE.md` updated with WI-42 audit signoff note

---

## Files in Scope

| File | Permitted Change |
|---|---|
| `src/ui/dashboard.py` | Fix D-42-1 (header emoji), D-42-2 (primary query filter), D-42-3 (compatibility query expiry filter) |
| `STATE.md` | WI-42 signoff note |

**No other files may be modified.**

---

## Deviation Summary (Reference)

| ID | Location | Deviation | Fix |
|---|---|---|---|
| D-42-1 | `render_market_watch()` L1 | Missing `🌐` emoji in header | Add emoji to header literal |
| D-42-2 | `fetch_market_watch()` `markets` branch | No active-status filter on primary query | Add `WHERE UPPER(COALESCE(status, 'ACTIVE')) = 'ACTIVE'` |
| D-42-3 | `fetch_market_watch()` `market_snapshots` branch | No expiry filter; shows resolved markets | Add `AND (market_end_date IS NULL OR datetime(market_end_date) >= datetime('now'))` |
