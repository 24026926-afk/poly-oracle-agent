# WI-42 Business Logic — Market Watch Panel

**Phase:** 12 — Command Center Dashboard  
**WI:** WI-42  
**Date:** 2026-04-15  
**Author:** Lead Architect  
**Status:** Pre-signoff specification

---

## 1. Objective

Render a read-only market watch table showing currently active tracked markets, ranked by 24h volume, so operators can prioritize liquidity-first monitoring.

---

## 2. Canonical Data Contract

The market watch panel must expose:

1. `market_id`
2. `question`
3. `yes_price`
4. `no_price`
5. `volume_24h`
6. `end_date`
7. `status`

Rows shown in WI-42 are active markets only, sorted by descending `volume_24h`.

---

## 3. SQL Logic

### 3.1 Primary Query (`markets` table)

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
ORDER BY COALESCE(volume_24h, 0) DESC;
```

### 3.2 Compatibility Query (`market_snapshots` table)

If `markets` is unavailable, derive active-market rows from the latest snapshot per condition:

```sql
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
ORDER BY COALESCE(volume_24h_usdc, 0) DESC;
```

---

## 4. Streamlit DataFrame Configuration

### 4.1 Section Header

```python
st.header("🌐 Market Watch")
```

### 4.2 Data Preparation

Before rendering:

1. Coerce `yes_price`, `no_price`, `volume_24h` with `pd.to_numeric(..., errors="coerce")`
2. Normalize `end_date` with `pd.to_datetime(..., errors="coerce")`

### 4.3 DataFrame Rendering

The table must be rendered with stretch width:

```python
st.dataframe(..., width='stretch', ...)
```

Required UI configuration:

1. `hide_index=True`
2. Fixed viewport height (recommended: `height=420`)
3. Column config must include `yes_price` as `NumberColumn(format="%.4f")`.
4. Column config must include `no_price` as `NumberColumn(format="%.4f")`.
5. Column config must include `volume_24h` as `NumberColumn(format="$%.2f")`.
6. Caption must show tracked row count:

```python
st.caption(f"{len(df)} markets tracked")
```

---

## 5. Data Flow

1. Detect available tables in SQLite metadata.
2. Run primary `markets` query when present; otherwise run compatibility CTE.
3. Restrict output to active markets.
4. Sort rows by 24h volume descending.
5. Apply numeric/date normalization for deterministic rendering.
6. Render via `st.dataframe` with `width='stretch'`.

---

## 6. Empty/Failure Behavior

1. If no active rows exist: `st.info("No markets ingested yet.")`
2. If query fails: return empty DataFrame and show the same empty-state message
3. WI-42 remains read-only: no `INSERT`, `UPDATE`, or `DELETE` SQL in UI code paths

---

## 7. Definition of Done Translation

WI-42 is business-logic complete when all conditions are true:

1. Only active tracked markets are shown.
2. Output is sorted by descending 24h volume.
3. Numeric columns render with mandated precision (`%.4f` prices, `$%.2f` volume).
4. DataFrame render uses `width='stretch'`.
5. Empty state is graceful and non-throwing.
