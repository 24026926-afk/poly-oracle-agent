"""Poly-Oracle Command Center dashboard (read-only)."""

import math
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from time import perf_counter

import pandas as pd
import plotly.express as px
import streamlit as st


st.set_page_config(
    page_title="Poly-Oracle Command Center",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = Path(__file__).resolve().parents[2] / "poly_oracle.db"

BG_MAIN = "#050607"
BG_PANEL = "#0B0D10"
BORDER = "#1C2127"
TEXT_PRIMARY = "#E8EBEE"
TEXT_MUTED = "#9EA8B3"
ACCENT_POSITIVE = "#00FF85"
ACCENT_NEGATIVE = "#FF4D4F"

ZERO = Decimal("0")

MOCK_PNL_DELTA_USDC = Decimal("124.82")
MOCK_WIN_RATE_DELTA_PCT = Decimal("2.40")
MOCK_EXPOSURE_DELTA_USDC = Decimal("-38.75")


def inject_terminal_theme() -> None:
    st.markdown(
        f"""
        <style>
            :root {{
                --terminal-bg: {BG_MAIN};
                --panel-bg: {BG_PANEL};
                --border-color: {BORDER};
                --text-primary: {TEXT_PRIMARY};
                --text-muted: {TEXT_MUTED};
                --positive: {ACCENT_POSITIVE};
                --negative: {ACCENT_NEGATIVE};
            }}
            [data-testid="stAppViewContainer"] {{
                background: var(--terminal-bg);
            }}
            [data-testid="stSidebar"] {{
                background: #090B0E;
                border-right: 1px solid var(--border-color);
            }}
            .block-container {{
                padding-top: 1rem;
                padding-bottom: 1rem;
                max-width: 100%;
            }}
            h1, h2, h3 {{
                color: var(--text-primary);
                letter-spacing: 0.015em;
            }}
            p, span, label, div {{
                color: var(--text-primary);
            }}
            div[data-testid="stMetric"] {{
                background: var(--panel-bg);
                border: 1px solid var(--border-color);
                border-radius: 6px;
                padding: 0.5rem 0.75rem;
            }}
            div[data-testid="stMetricLabel"] {{
                font-size: 0.72rem;
                color: var(--text-muted);
                letter-spacing: 0.08em;
                text-transform: uppercase;
            }}
            div[data-testid="stMetricValue"] {{
                font-family: "IBM Plex Mono", "SFMono-Regular", Menlo, Consolas, monospace;
            }}
            div[data-testid="stMetricDelta"] {{
                font-family: "IBM Plex Mono", "SFMono-Regular", Menlo, Consolas, monospace;
            }}
            [data-testid="stDataFrame"] {{
                border: 1px solid var(--border-color);
                border-radius: 6px;
            }}
            hr {{
                border-color: var(--border-color);
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def get_connection() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def to_decimal(value: object) -> Decimal:
    if value is None:
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return ZERO


def format_usdc(value: Decimal) -> str:
    return f"${value:,.2f}"


def format_signed_usdc(value: Decimal) -> str:
    return f"{value:+,.2f} USDC"


def format_signed_pct(value: Decimal) -> str:
    return f"{value:+.2f} pp"


def get_system_vitals() -> dict[str, object]:
    start = perf_counter()
    reachable = False
    latency_ms: float | None = None

    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")
            reachable = True
    except Exception:
        reachable = False
    finally:
        elapsed_ms = (perf_counter() - start) * 1000
        latency_ms = round(elapsed_ms, 2)

    return {
        "db_connection": "ONLINE" if reachable else "OFFLINE",
        "engine_status": "ACTIVE" if reachable else "DEGRADED",
        "latency_ms": latency_ms,
    }


@st.cache_data(ttl=30)
def fetch_table_names() -> tuple[str, ...]:
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        return tuple(row[0] for row in rows if row and row[0])
    except Exception:
        return tuple()


@st.cache_data(ttl=30)
def fetch_metrics() -> dict[str, object]:
    metrics = {
        "total_pnl": ZERO,
        "win_rate": ZERO,
        "open_exposure": ZERO,
        "total_decisions": 0,
        "active_positions": 0,
        "pnl_delta": MOCK_PNL_DELTA_USDC,
        "win_rate_delta_pct": MOCK_WIN_RATE_DELTA_PCT,
        "exposure_delta": MOCK_EXPOSURE_DELTA_USDC,
        "using_mock_deltas": True,
    }
    tables = set(fetch_table_names())

    try:
        with get_connection() as conn:
            if "positions" in tables:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total_rows,
                        COALESCE(SUM(CASE WHEN status='CLOSED' THEN COALESCE(realized_pnl, 0) ELSE 0 END), 0) AS total_pnl,
                        COALESCE(SUM(CASE WHEN status='OPEN' THEN COALESCE(order_size_usdc, 0) ELSE 0 END), 0) AS open_exposure,
                        COALESCE(SUM(CASE WHEN status='OPEN' THEN 1 ELSE 0 END), 0) AS active_positions,
                        COALESCE(SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END), 0) AS closed_count,
                        COALESCE(SUM(CASE WHEN status='CLOSED' AND COALESCE(realized_pnl, 0) > 0 THEN 1 ELSE 0 END), 0) AS wins_count
                    FROM positions
                    """
                ).fetchone()
                if row:
                    closed_count = int(row[4] or 0)
                    wins_count = int(row[5] or 0)
                    metrics["total_pnl"] = to_decimal(row[1])
                    metrics["open_exposure"] = to_decimal(row[2])
                    metrics["active_positions"] = int(row[3] or 0)
                    metrics["win_rate"] = (
                        (Decimal(wins_count) / Decimal(closed_count))
                        if closed_count > 0
                        else ZERO
                    )
                    metrics["using_mock_deltas"] = int(row[0] or 0) == 0

                    if not metrics["using_mock_deltas"]:
                        pnl_delta = conn.execute(
                            """
                            SELECT COALESCE(SUM(COALESCE(realized_pnl, 0)), 0)
                            FROM positions
                            WHERE status='CLOSED'
                              AND COALESCE(closed_at_utc, recorded_at_utc) >= datetime('now', '-1 day')
                            """
                        ).fetchone()
                        metrics["pnl_delta"] = to_decimal(pnl_delta[0] if pnl_delta else ZERO)

                        exposure_delta = conn.execute(
                            """
                            SELECT COALESCE(SUM(COALESCE(order_size_usdc, 0)), 0)
                            FROM positions
                            WHERE status='OPEN'
                              AND recorded_at_utc >= datetime('now', '-1 day')
                            """
                        ).fetchone()
                        metrics["exposure_delta"] = to_decimal(
                            exposure_delta[0] if exposure_delta else ZERO
                        )

                        this_week = conn.execute(
                            """
                            SELECT
                                COALESCE(SUM(CASE WHEN COALESCE(realized_pnl, 0) > 0 THEN 1 ELSE 0 END), 0) AS wins,
                                COUNT(*) AS total
                            FROM positions
                            WHERE status='CLOSED'
                              AND COALESCE(closed_at_utc, recorded_at_utc) >= datetime('now', '-7 day')
                            """
                        ).fetchone()
                        prev_week = conn.execute(
                            """
                            SELECT
                                COALESCE(SUM(CASE WHEN COALESCE(realized_pnl, 0) > 0 THEN 1 ELSE 0 END), 0) AS wins,
                                COUNT(*) AS total
                            FROM positions
                            WHERE status='CLOSED'
                              AND COALESCE(closed_at_utc, recorded_at_utc) < datetime('now', '-7 day')
                              AND COALESCE(closed_at_utc, recorded_at_utc) >= datetime('now', '-14 day')
                            """
                        ).fetchone()
                        this_week_rate = (
                            Decimal(int(this_week[0])) / Decimal(int(this_week[1]))
                            if this_week and int(this_week[1]) > 0
                            else ZERO
                        )
                        prev_week_rate = (
                            Decimal(int(prev_week[0])) / Decimal(int(prev_week[1]))
                            if prev_week and int(prev_week[1]) > 0
                            else ZERO
                        )
                        metrics["win_rate_delta_pct"] = (
                            this_week_rate - prev_week_rate
                        ) * Decimal("100")

            if "decisions" in tables:
                row = conn.execute("SELECT COUNT(*) FROM decisions").fetchone()
                metrics["total_decisions"] = int(row[0] or 0) if row else 0
            elif "agent_decision_logs" in tables:
                row = conn.execute("SELECT COUNT(*) FROM agent_decision_logs").fetchone()
                metrics["total_decisions"] = int(row[0] or 0) if row else 0
    except Exception:
        pass

    return metrics


@st.cache_data(ttl=30)
def fetch_decision_log() -> pd.DataFrame:
    tables = set(fetch_table_names())
    if not tables:
        return pd.DataFrame()

    try:
        with get_connection() as conn:
            if "decisions" in tables:
                return pd.read_sql_query(
                    """
                    SELECT
                        created_at,
                        market_id,
                        action,
                        confidence,
                        reasoning,
                        kelly_fraction
                    FROM decisions
                    ORDER BY created_at DESC
                    LIMIT 20
                    """,
                    conn,
                )

            if "agent_decision_logs" in tables:
                return pd.read_sql_query(
                    """
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
                    LIMIT 20
                    """,
                    conn,
                )
    except Exception:
        return pd.DataFrame()

    return pd.DataFrame()


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
                    ORDER BY volume_24h DESC
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
                        CASE
                            WHEN market_end_date IS NOT NULL
                                 AND datetime(market_end_date) < datetime('now')
                            THEN 'CLOSED'
                            ELSE 'ACTIVE'
                        END AS status
                    FROM latest
                    WHERE row_num = 1
                    ORDER BY COALESCE(volume_24h_usdc, 0) DESC
                    """,
                    conn,
                )
    except Exception:
        return pd.DataFrame()

    return pd.DataFrame()


@st.cache_data(ttl=30)
def fetch_pnl_timeseries() -> tuple[pd.DataFrame, bool]:
    tables = set(fetch_table_names())
    if "positions" in tables:
        try:
            with get_connection() as conn:
                pnl_df = pd.read_sql_query(
                    """
                    SELECT
                        date(COALESCE(closed_at_utc, recorded_at_utc)) AS timestamp,
                        COALESCE(SUM(COALESCE(realized_pnl, 0)), 0) AS pnl_usdc
                    FROM positions
                    WHERE status='CLOSED'
                    GROUP BY date(COALESCE(closed_at_utc, recorded_at_utc))
                    ORDER BY timestamp ASC
                    """,
                    conn,
                )
            if not pnl_df.empty:
                pnl_df["timestamp"] = pd.to_datetime(pnl_df["timestamp"], errors="coerce")
                pnl_df["pnl_usdc"] = pd.to_numeric(pnl_df["pnl_usdc"], errors="coerce").fillna(0)
                pnl_df["pnl_usdc"] = pnl_df["pnl_usdc"].cumsum()
                return pnl_df.dropna(subset=["timestamp"]), False
        except Exception:
            pass

    base_time = pd.Timestamp.utcnow().floor("h")
    timestamps = pd.date_range(end=base_time, periods=36, freq="h")
    values: list[float] = []
    running = Decimal("0")
    for idx in range(len(timestamps)):
        wave = Decimal(str(math.sin(idx / 4) * 2.15))
        drift = Decimal(str(idx)) * Decimal("0.22")
        running = wave + drift
        values.append(float(running))

    mock_df = pd.DataFrame({"timestamp": timestamps, "pnl_usdc": values})
    return mock_df, True


def render_sidebar() -> None:
    vitals = get_system_vitals()
    latency = vitals["latency_ms"]
    latency_label = f"{latency:.2f} ms" if isinstance(latency, float) else "n/a"

    with st.sidebar:
        st.markdown("### System Vitals")
        st.code(
            "\n".join(
                [
                    f"DB_CONNECTION : {vitals['db_connection']}",
                    f"ENGINE_STATUS : {vitals['engine_status']}",
                    f"LATENCY_MS    : {latency_label}",
                    f"LAST_REFRESH  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                    f"DB_FILE       : {DB_PATH.name}",
                ]
            ),
            language="text",
        )
        if st.button("Refresh View", use_container_width=True):
            st.cache_data.clear()
            st.rerun()


def render_metrics(metrics: dict[str, object]) -> None:
    st.header("Performance Metrics")
    col_pnl, col_win, col_exp = st.columns(3)

    total_pnl = to_decimal(metrics.get("total_pnl", ZERO))
    win_rate_pct = to_decimal(metrics.get("win_rate", ZERO)) * Decimal("100")
    exposure = to_decimal(metrics.get("open_exposure", ZERO))

    pnl_delta = to_decimal(metrics.get("pnl_delta", ZERO))
    win_rate_delta_pct = to_decimal(metrics.get("win_rate_delta_pct", ZERO))
    exposure_delta = to_decimal(metrics.get("exposure_delta", ZERO))

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

    if bool(metrics.get("using_mock_deltas", False)):
        st.caption(
            "No position rows detected. Delta indicators are mock values to preview "
            "positive and negative states."
        )


def render_chart() -> None:
    st.header("PnL Over Time")
    pnl_df, is_mock = fetch_pnl_timeseries()

    fig = px.line(
        pnl_df,
        x="timestamp",
        y="pnl_usdc",
        template="plotly_dark",
    )
    fig.update_traces(
        line={
            "color": "#B8C0C8",
            "width": 2.25,
            "dash": "dot" if is_mock else "solid",
        },
        hovertemplate="%{x}<br>PnL: %{y:,.2f} USDC<extra></extra>",
    )
    fig.update_layout(
        showlegend=False,
        margin={"l": 20, "r": 20, "t": 15, "b": 15},
        height=320,
        paper_bgcolor=BG_MAIN,
        plot_bgcolor=BG_PANEL,
        font={
            "color": TEXT_PRIMARY,
            "family": "IBM Plex Mono, SFMono-Regular, Menlo, Consolas, monospace",
            "size": 12,
        },
    )
    fig.update_xaxes(showgrid=False, zeroline=False, title="")
    fig.update_yaxes(showgrid=False, zeroline=False, title="", ticksuffix=" USDC")

    st.plotly_chart(
        fig,
        width="stretch",
        config={"displayModeBar": False},
    )
    if is_mock:
        st.caption("Placeholder curve rendered because realized PnL history is not available yet.")


def render_decision_table() -> None:
    st.header("LLM Decision Audit Log")
    decisions_df = fetch_decision_log()

    if decisions_df.empty:
        st.info("No decisions logged yet.")
        return

    normalized_df = decisions_df.rename(
        columns={
            "reasoning_log": "reasoning",
            "confidence_score": "confidence",
            "recommended_action": "action",
            "evaluated_at": "created_at",
            "condition_id": "market_id",
        }
    ).copy()

    normalized_df["created_at"] = pd.to_datetime(
        normalized_df.get("created_at"), errors="coerce"
    ).dt.strftime("%Y-%m-%d %H:%M:%S")
    normalized_df["confidence_pct"] = (
        pd.to_numeric(normalized_df.get("confidence"), errors="coerce").fillna(0) * 100
    )

    if "expected_value" in normalized_df.columns:
        normalized_df["expected_value_pct"] = (
            pd.to_numeric(normalized_df["expected_value"], errors="coerce").fillna(0) * 100
        )
    else:
        normalized_df["expected_value_pct"] = 0.0

    if "kelly_fraction" in normalized_df.columns:
        normalized_df["kelly_pct"] = (
            pd.to_numeric(normalized_df["kelly_fraction"], errors="coerce").fillna(0) * 100
        )
    else:
        normalized_df["kelly_pct"] = 0.0

    display_columns = [
        "created_at",
        "market_id",
        "action",
        "confidence_pct",
        "expected_value_pct",
        "kelly_pct",
        "reasoning",
    ]
    for col in display_columns:
        if col not in normalized_df.columns:
            normalized_df[col] = ""

    st.dataframe(
        normalized_df[display_columns],
        width="stretch",
        hide_index=True,
        height=420,
        column_config={
            "created_at": st.column_config.TextColumn("Timestamp", width="medium"),
            "market_id": st.column_config.TextColumn("Market", width="medium"),
            "action": st.column_config.TextColumn("Action", width="small"),
            "confidence_pct": st.column_config.NumberColumn("Confidence", format="%.2f%%"),
            "expected_value_pct": st.column_config.NumberColumn("EV", format="%.2f%%"),
            "kelly_pct": st.column_config.NumberColumn("Kelly", format="%.2f%%"),
            "reasoning": st.column_config.TextColumn("Reasoning", width="large"),
        },
    )
    st.caption(f"Showing last {len(normalized_df)} decisions")


def render_market_watch() -> None:
    st.header("Market Watch")
    markets_df = fetch_market_watch()

    if markets_df.empty:
        st.info("No markets ingested yet.")
        return

    display_df = markets_df.copy()
    for col in ("yes_price", "no_price", "volume_24h"):
        if col in display_df.columns:
            display_df[col] = pd.to_numeric(display_df[col], errors="coerce")
    if "end_date" in display_df.columns:
        display_df["end_date"] = pd.to_datetime(display_df["end_date"], errors="coerce").dt.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    st.dataframe(
        display_df,
        width="stretch",
        hide_index=True,
        height=420,
        column_config={
            "market_id": st.column_config.TextColumn("Market", width="medium"),
            "question": st.column_config.TextColumn("Question", width="large"),
            "yes_price": st.column_config.NumberColumn("Yes", format="%.4f"),
            "no_price": st.column_config.NumberColumn("No", format="%.4f"),
            "volume_24h": st.column_config.NumberColumn("24h Volume", format="$%.2f"),
            "end_date": st.column_config.TextColumn("End Date", width="medium"),
            "status": st.column_config.TextColumn("Status", width="small"),
        },
    )
    st.caption(f"{len(display_df)} markets tracked")


inject_terminal_theme()
render_sidebar()

st.title("Poly-Oracle Command Center")
st.caption(
    "Institutional dashboard for performance metrics, decision audit trails, and market surveillance."
)
st.markdown("---")

dashboard_metrics = fetch_metrics()
render_metrics(dashboard_metrics)

st.markdown("---")
render_chart()

st.markdown("---")
render_decision_table()

st.markdown("---")
render_market_watch()
