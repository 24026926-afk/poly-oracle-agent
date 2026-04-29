# WI-41 Business Logic — Decision Audit Log

**Phase:** 12 — Command Center Dashboard  
**WI:** WI-41  
**Date:** 2026-04-15  
**Author:** Lead Architect  
**Status:** Pre-signoff specification

---

## 1. Objective

Render a read-only operator audit table showing the most recent LLM decisions, including full reasoning, for fast post-trade explainability checks.

---

## 2. Canonical Data Contract

The decision audit table must expose, at minimum, these fields:

1. `created_at`
2. `market_id`
3. `action`
4. `confidence`
5. `reasoning`
6. `kelly_fraction` (when available)

`reasoning` is mandatory for WI-41 signoff.

---

## 3. SQL Logic

### 3.1 Primary Query (Dashboard `decisions` table)

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

### 3.2 Compatibility Query (Legacy `agent_decision_logs` table)

If `decisions` is unavailable, the dashboard may read from `agent_decision_logs` and normalize names to the WI-41 table contract:

```sql
SELECT
    d.evaluated_at AS created_at,
    COALESCE(s.condition_id, d.snapshot_id) AS market_id,
    d.recommended_action AS action,
    d.confidence_score AS confidence,
    d.expected_value AS expected_value,
    d.reasoning_log AS reasoning
FROM agent_decision_logs d
LEFT JOIN market_snapshots s
    ON s.id = d.snapshot_id
ORDER BY d.evaluated_at DESC
LIMIT 20;
```

---

## 4. Streamlit Rendering Constraints

### 4.1 Section Header

```python
st.header("🧠 LLM Decision Audit Log")
```

### 4.2 DataFrame Rendering

The table must be rendered with stretch width:

```python
st.dataframe(..., width='stretch', ...)
```

Required display constraints:

1. `hide_index=True`
2. Fixed viewport height (recommended: `height=420`)
3. `reasoning` configured as `TextColumn` and kept readable (`width="large"`)
4. `reasoning` should remain left-aligned; text wrapping enabled when Streamlit build supports wrapped cells
5. Caption must show returned row count:

```python
st.caption(f"Showing last {len(df)} decisions")
```

---

## 5. Data Flow

1. Resolve available tables from SQLite metadata.
2. Execute primary query when `decisions` exists; otherwise execute compatibility query.
3. Normalize column names to the canonical WI-41 contract.
4. Coerce timestamp and numeric display columns for consistent formatting.
5. Render via `st.dataframe` with `width='stretch'`.

---

## 6. Empty/Failure Behavior

1. If no rows are returned: `st.info("No decisions logged yet.")`
2. If table lookup/query fails: return an empty DataFrame and surface the same empty-state info
3. No code path in WI-41 may perform `INSERT`, `UPDATE`, or `DELETE`

---

## 7. Definition of Done Translation

WI-41 is business-logic complete when all conditions are true:

1. Query returns max 20 rows sorted by newest decision first.
2. `reasoning` is present and human-readable in the UI.
3. Table render uses `width='stretch'`.
4. Row count caption reflects the displayed result set.
5. Empty state is graceful and non-throwing.
