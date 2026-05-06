"""Poly-Oracle Command Center dashboard (read-only)."""

import math
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from time import perf_counter

import pandas as pd
import plotly.graph_objects as go
import streamlit as st


st.set_page_config(
    page_title="Poly-Oracle Command Center",
    layout="wide",
    initial_sidebar_state="expanded",
)

DB_PATH = Path(__file__).resolve().parents[2] / "poly_oracle.db"

SURFACE_BASE = "#0D0D0D"
SURFACE_PANEL = "#111111"
SURFACE_RAISED = "#161616"
SURFACE_TRACK = "#1B1B1B"
TEXT_PRIMARY = "#F2EEE7"
TEXT_SECONDARY = "#AEA79B"
TEXT_MUTED = "#736D64"
LINE_COLOR = "rgba(255,255,255,0.06)"
GRID_COLOR = "rgba(255,255,255,0.05)"
ACCENT_AMBER = "#E8A020"
ACCENT_RED = "#C0392B"
ACCENT_GREEN = "#3E8A62"
ACCENT_NEUTRAL = "#8B877F"
ZERO = Decimal("0")

MOCK_PNL_DELTA_USDC = Decimal("124.82")
MOCK_WIN_RATE_DELTA_PCT = Decimal("2.40")
MOCK_EXPOSURE_DELTA_USDC = Decimal("-38.75")


def inject_terminal_theme() -> int:
    css_rules = [
        """
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');
        """,
        f"""
        :root {{
            --surface-base: {SURFACE_BASE};
            --surface-panel: {SURFACE_PANEL};
            --surface-raised: {SURFACE_RAISED};
            --surface-track: {SURFACE_TRACK};
            --text-primary: {TEXT_PRIMARY};
            --text-secondary: {TEXT_SECONDARY};
            --text-muted: {TEXT_MUTED};
            --line-color: {LINE_COLOR};
            --grid-color: {GRID_COLOR};
            --accent-amber: {ACCENT_AMBER};
            --accent-red: {ACCENT_RED};
            --accent-green: {ACCENT_GREEN};
            --accent-neutral: {ACCENT_NEUTRAL};
        }}
        """,
        """
        html, body, [class*="css"] {
            font-family: "Inter", system-ui, sans-serif;
        }
        """,
        """
        [data-testid="stAppViewContainer"] {
            background:
                radial-gradient(circle at top right, rgba(232, 160, 32, 0.06), transparent 28%),
                linear-gradient(180deg, #101010 0%, #0d0d0d 100%);
            color: var(--text-primary);
        }
        """,
        """
        [data-testid="stAppViewContainer"]::before {
            content: "";
            position: fixed;
            inset: 0;
            pointer-events: none;
            background-image:
                linear-gradient(rgba(255, 255, 255, 0.015) 1px, transparent 1px),
                linear-gradient(90deg, rgba(255, 255, 255, 0.015) 1px, transparent 1px);
            background-size: 96px 96px;
            opacity: 0.18;
            mix-blend-mode: soft-light;
            z-index: 0;
        }
        """,
        """
        [data-testid="stHeader"] {
            background: transparent;
        }
        """,
        """
        #MainMenu, footer {
            visibility: hidden;
        }
        """,
        """
        section.main > div {
            max-width: 100% !important;
        }
        """,
        """
        .block-container {
            max-width: 100% !important;
            padding-left: 1rem !important;
            padding-right: 1rem !important;
            padding-top: 1rem !important;
            padding-bottom: 2rem;
            position: relative;
            z-index: 1;
        }
        """,
        """
        [data-testid="stSidebar"] {
            background:
                linear-gradient(180deg, rgba(255, 255, 255, 0.02), rgba(255, 255, 255, 0)),
                #0f0f0f;
            border-right: 1px solid var(--line-color);
        }
        """,
        """
        [data-testid="stSidebar"] {
            background-color: #0d0d0d !important;
            border-right: 1px solid rgba(255,255,255,0.06) !important;
            padding: 0 !important;
        }
        """,
        """
        [data-testid="stSidebar"] > div:first-child {
            padding: 1.5rem 1rem !important;
        }
        """,
        """
        [data-testid="collapsedControl"] { display: none !important; }
        """,
        """
        [data-testid="stSidebarNav"] { display: none !important; }
        """,
        """
        section[data-testid="stSidebar"] + div {
            background-color: #111111 !important;
        }
        """,
        """
        [data-testid="stSidebar"] * {
            font-family: "JetBrains Mono", "Fira Code", monospace;
        }
        """,
        """
        [data-testid="stSidebar"] .block-container {
            padding-top: 1rem;
            padding-bottom: 1rem;
        }
        """,
        """
        .sidebar-shell,
        .terminal-shell,
        .section-shell,
        .empty-shell {
            border: 1px solid var(--line-color);
            border-radius: 4px;
            background:
                linear-gradient(180deg, rgba(255, 255, 255, 0.015), rgba(255, 255, 255, 0)),
                var(--surface-panel);
        }
        """,
        """
        .terminal-shell {
            padding: 1.2rem 1.25rem 1.35rem;
            margin-bottom: 1rem;
        }
        """,
        """
        .terminal-topline {
            display: flex;
            justify-content: space-between;
            gap: 1rem;
            border-bottom: 1px solid var(--line-color);
            padding-bottom: 0.65rem;
            margin-bottom: 1rem;
            font-size: 0.72rem;
            letter-spacing: 0.08em;
            color: var(--text-muted);
            text-transform: uppercase;
        }
        """,
        """
        .hero-grid {
            display: grid;
            grid-template-columns: minmax(0, 1.9fr) minmax(280px, 1fr);
            gap: 1rem;
            align-items: start;
        }
        """,
        """
        .hero-kicker {
            font-size: 0.76rem;
            color: var(--text-muted);
            letter-spacing: 0.1em;
            text-transform: uppercase;
            margin-bottom: 0.55rem;
        }
        """,
        """
        .section-stack {
            display: flex;
            flex-direction: column;
            gap: 0;
            width: 100%;
        }
        """,
        """
        .section-kicker {
            font-size: 0.76rem;
            color: var(--text-muted);
            letter-spacing: 0.1em;
            text-transform: uppercase;
            margin: 0;
            padding: 0 0 0.45rem 0;
        }
        """,
        """
        .hero-title {
            margin: 0;
            max-width: 18ch;
            font-size: clamp(2.7rem, 4.4vw, 4.8rem);
            line-height: 0.94;
            letter-spacing: -0.05em;
            font-weight: 600;
            text-wrap: balance;
            color: var(--text-primary);
        }
        """,
        """
        .hero-deck {
            max-width: 62ch;
            margin: 0.95rem 0 0;
            color: var(--text-secondary);
            line-height: 1.65;
            font-size: 0.97rem;
        }
        """,
        """
        .hero-ribbon {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin-top: 1rem;
        }
        """,
        """
        .ribbon-chip,
        .delta-tag,
        .status-chip {
            display: inline-flex;
            align-items: center;
            gap: 0.4rem;
            padding: 0.28rem 0.52rem;
            border: 1px solid var(--line-color);
            border-radius: 3px;
            font-family: "JetBrains Mono", "Fira Code", monospace;
            font-size: 0.72rem;
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        """,
        """
        .hero-rail {
            display: grid;
            gap: 0.65rem;
            padding: 0.15rem 0 0;
        }
        """,
        """
        .rail-row,
        .vital-row {
            display: grid;
            grid-template-columns: 112px 14px minmax(0, 1fr);
            align-items: center;
            gap: 0.25rem;
            font-family: "JetBrains Mono", "Fira Code", monospace;
            font-size: 0.78rem;
            font-variant-numeric: tabular-nums;
        }
        """,
        """
        .rail-key,
        .vital-key {
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.07em;
        }
        """,
        """
        .rail-value,
        .vital-value {
            color: var(--text-primary);
            text-align: right;
        }
        """,
        """
        tr {
            transition: background 120ms ease;
        }
        """,
        """
        tr:hover td {
            background: rgba(232,160,32,0.04) !important;
        }
        """,
        """
        @keyframes value-flash {
            0% { opacity: 0.4; }
            50% { opacity: 1; }
            100% { opacity: 1; }
        }
        """,
        """
        .kpi-value {
            animation: value-flash 400ms ease forwards;
        }
        """,
        """
        @keyframes dot-pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.3; }
        }
        """,
        """
        .status-dot-online {
            display: inline-block;
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: #27ae60;
            animation: dot-pulse 2s ease-in-out infinite;
            margin-right: 6px;
            vertical-align: middle;
        }
        """,
        """
        .status-dot-error {
            display: inline-block;
            width: 6px;
            height: 6px;
            border-radius: 50%;
            background: #c0392b;
            margin-right: 6px;
            vertical-align: middle;
        }
        """,
        """
        .sidebar-shell {
            padding: 0.85rem 0.9rem 1rem;
        }
        """,
        """
        .sidebar-title {
            font-size: 0.78rem;
            color: var(--text-muted);
            letter-spacing: 0.1em;
            text-transform: uppercase;
            margin-bottom: 0.75rem;
        }
        """,
        """
        .sidebar-divider {
            height: 1px;
            background: var(--line-color);
            margin: 0.75rem 0 0.85rem;
        }
        """,
        """
        .sidebar-note {
            margin-top: 0.85rem;
            color: var(--text-muted);
            font-size: 0.72rem;
            line-height: 1.65;
        }
        """,
        """
        .section-shell {
            padding: 1rem 1rem 0.4rem;
        }
        """,
        """
        .section-head {
            display: flex;
            justify-content: space-between;
            align-items: end;
            gap: 1rem;
            margin-bottom: 0.85rem;
        }
        """,
        """
        .section-title {
            margin: 0;
            font-size: 1.15rem;
            font-weight: 600;
            letter-spacing: -0.02em;
            color: var(--text-primary);
        }
        """,
        """
        .section-caption {
            margin: 0.2rem 0 0;
            color: var(--text-secondary);
            font-size: 0.88rem;
        }
        """,
        """
        .section-meta {
            font-family: "JetBrains Mono", "Fira Code", monospace;
            font-size: 0.74rem;
            color: var(--text-muted);
            font-variant-numeric: tabular-nums;
            white-space: nowrap;
        }
        """,
        """
        .metrics-grid {
            display: grid;
            grid-template-columns: repeat(5, minmax(140px, 1fr));
            gap: 0.75rem;
        }
        """,
        """
        .metric-card {
            min-height: 136px;
            padding: 0.9rem 0.95rem 0.95rem;
            border: 1px solid var(--line-color);
            border-radius: 4px;
            background:
                linear-gradient(180deg, rgba(255, 255, 255, 0.015), rgba(255, 255, 255, 0)),
                var(--surface-raised);
            transition:
                transform 220ms ease,
                border-color 220ms ease,
                background-color 220ms ease;
        }
        """,
        """
        .metric-card:hover {
            transform: translateY(-1px);
            border-color: rgba(232, 160, 32, 0.14);
            background-color: #191919;
        }
        """,
        """
        .metric-card.metric-pnl {
            min-height: 136px;
        }
        """,
        """
        .metric-card.metric-win {
        }
        """,
        """
        .metric-card.metric-exposure {
        }
        """,
        """
        .metric-card.metric-decisions {
        }
        """,
        """
        .metric-card.metric-positions {
        }
        """,
        """
        .metric-label {
            color: var(--text-muted);
            font-size: 0.72rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }
        """,
        """
        .metric-value,
        .metric-subvalue {
            margin-top: 0.75rem;
            font-family: "JetBrains Mono", "Fira Code", monospace;
            font-variant-numeric: tabular-nums;
            color: var(--text-primary);
            line-height: 1;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            min-width: 120px;
        }
        """,
        """
        .metric-value {
            font-size: clamp(1.6rem, 2vw, 2.75rem);
        }
        """,
        """
        .metric-card.metric-pnl .metric-value {
            font-size: clamp(2.55rem, 3.8vw, 4rem);
            margin-top: 1.05rem;
        }
        """,
        """
        .metric-hint {
            margin-top: 0.95rem;
            color: var(--text-secondary);
            font-size: 0.84rem;
            line-height: 1.6;
        }
        """,
        """
        .metric-meta {
            margin-top: 0.8rem;
            font-family: "JetBrains Mono", "Fira Code", monospace;
            font-size: 0.74rem;
            color: var(--text-muted);
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }
        """,
        """
        .tone-positive {
            color: var(--accent-amber);
        }
        """,
        """
        .tone-negative {
            color: var(--accent-red);
        }
        """,
        """
        .tone-neutral {
            color: var(--text-primary);
        }
        """,
        """
        .tag-positive {
            color: var(--accent-amber);
            background: rgba(232, 160, 32, 0.11);
        }
        """,
        """
        .tag-negative {
            color: var(--accent-red);
            background: rgba(192, 57, 43, 0.12);
        }
        """,
        """
        .tag-neutral {
            color: var(--text-secondary);
            background: rgba(255, 255, 255, 0.04);
        }
        """,
        """
        .chart-note,
        .metrics-note {
            margin-top: 0.65rem;
            font-family: "JetBrains Mono", "Fira Code", monospace;
            font-size: 0.72rem;
            color: var(--text-muted);
            font-variant-numeric: tabular-nums;
        }
        """,
        """
        div[data-testid="stPlotlyChart"] {
            border: 1px solid var(--line-color);
            border-radius: 4px;
            background:
                linear-gradient(180deg, rgba(255, 255, 255, 0.015), rgba(255, 255, 255, 0)),
                var(--surface-panel);
            padding: 0.2rem 0.2rem 0.1rem;
        }
        """,
        """
        .table-shell {
            border: 1px solid var(--line-color);
            border-radius: 4px;
            background:
                linear-gradient(180deg, rgba(255, 255, 255, 0.015), rgba(255, 255, 255, 0)),
                var(--surface-panel);
            overflow: hidden;
        }
        """,
        """
        .table-scroll {
            width: 100%;
            overflow-x: auto;
        }
        """,
        """
        .stButton > button {
            width: 100%;
            border: 1px solid var(--line-color);
            border-radius: 4px;
            background: #141414;
            color: var(--text-primary);
            padding: 0.58rem 0.75rem;
            font-family: "JetBrains Mono", "Fira Code", monospace;
            font-size: 0.74rem;
            text-transform: uppercase;
            letter-spacing: 0.09em;
            transition:
                transform 180ms ease,
                background-color 180ms ease,
                border-color 180ms ease;
        }
        """,
        """
        .stButton > button:hover {
            transform: translateY(-1px);
            border-color: rgba(232, 160, 32, 0.16);
            background: #1a1712;
            color: var(--text-primary);
        }
        """,
        """
        .stButton > button:active {
            transform: translateY(1px) scale(0.99);
        }
        """,
        """
        .stButton > button:focus-visible {
            outline: 2px solid rgba(232, 160, 32, 0.45);
            outline-offset: 2px;
        }
        """,
        """
        .empty-shell {
            padding: 1rem;
        }
        """,
        """
        .empty-title {
            margin: 0;
            font-size: 0.92rem;
            color: var(--text-primary);
        }
        """,
        """
        .empty-copy {
            margin: 0.45rem 0 0;
            color: var(--text-secondary);
            line-height: 1.6;
            font-size: 0.9rem;
            max-width: 52ch;
        }
        """,
        """
        @media (max-width: 1180px) {
            .hero-grid {
                grid-template-columns: 1fr;
            }
            .metrics-grid {
                grid-template-columns: repeat(2, minmax(140px, 1fr));
            }
            .metric-card {
                min-height: 180px;
            }
            .terminal-topline,
            .section-head {
                flex-direction: column;
                align-items: flex-start;
            }
        }
        """,
        """
        @media (max-width: 760px) {
            .metrics-grid {
                grid-template-columns: 1fr;
            }
        }
        """,
    ]
    st.markdown(f"<style>{''.join(css_rules)}</style>", unsafe_allow_html=True)
    return len(css_rules)


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


def get_system_vitals() -> dict[str, object]:
    start = perf_counter()
    reachable = False

    try:
        with get_connection() as conn:
            conn.execute("SELECT 1")
            reachable = True
    except Exception:
        reachable = False

    elapsed_ms = round((perf_counter() - start) * 1000, 2)
    return {
        "db_connection": "ONLINE" if reachable else "OFFLINE",
        "engine_status": "ACTIVE" if reachable else "DEGRADED",
        "latency_ms": elapsed_ms,
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
                        Decimal(wins_count) / Decimal(closed_count)
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


def format_delta_tag(value: Decimal, kind: str, tone_override: str | None = None) -> str:
    if kind == "currency":
        text = f"{value:+,.2f} USDC"
    elif kind == "percentage_points":
        text = f"{value:+.2f} pp"
    else:
        text = f"{value:+,.2f}"

    tone = tone_override
    if tone is None:
        if value > 0:
            tone = "positive"
        elif value < 0:
            tone = "negative"
        else:
            tone = "neutral"

    return f'<span class="delta-tag tag-{tone}">{escape(text)}</span>'


def build_metric_card(
    *,
    area_class: str,
    label: str,
    value: str,
    value_tone: str,
    delta_html: str,
    hint: str,
    meta: str,
) -> str:
    return f"""
    <article class="metric-card {area_class}">
        <div class="metric-label">{escape(label)}</div>
        <div class="metric-value kpi-value tone-{value_tone}" style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:120px;">{escape(value)}</div>
        <div class="metric-hint">{escape(hint)}</div>
        <div class="metric-meta">{delta_html} <span style="margin-left:0.55rem;">{escape(meta)}</span></div>
    </article>
    """


def render_terminal_header(vitals: dict[str, object], refreshed_at: datetime) -> None:
    status_value = str(vitals.get("db_connection", "OFFLINE"))
    status_class = "status-dot-online" if status_value == "ONLINE" else "status-dot-error"
    latency_ms = vitals.get("latency_ms")
    latency_label = f"{latency_ms:.2f} ms" if isinstance(latency_ms, float) else "n/a"

    st.markdown(
        f"""
        <section class="terminal-shell">
            <div class="terminal-topline">
                <span>poly-oracle / operator surface</span>
                <span>read only / sqlite mirror / cache ttl 30s</span>
            </div>
            <div class="hero-grid">
                <div>
                    <div class="hero-kicker">Institutional command layer</div>
                    <h1 class="hero-title">Poly-Oracle command center</h1>
                    <p class="hero-deck">
                        Read-only execution telemetry for realised performance, model behaviour,
                        and market coverage. The surface stays dense, typed, and audit-first.
                    </p>
                    <div class="hero-ribbon">
                        <span class="ribbon-chip">overview</span>
                        <span class="ribbon-chip">decision audit</span>
                        <span class="ribbon-chip">market watch</span>
                    </div>
                </div>
                <div class="hero-rail">
                    <div class="rail-row">
                        <span class="rail-key">DB status</span>
                        <span>:</span>
                        <span class="rail-value"><span class="{status_class}"></span>{escape(status_value)}</span>
                    </div>
                    <div class="rail-row">
                        <span class="rail-key">Engine</span>
                        <span>:</span>
                        <span class="rail-value">{escape(str(vitals.get("engine_status", "UNKNOWN")))}</span>
                    </div>
                    <div class="rail-row">
                        <span class="rail-key">Latency</span>
                        <span>:</span>
                        <span class="rail-value">{escape(latency_label)}</span>
                    </div>
                    <div class="rail-row">
                        <span class="rail-key">Refreshed</span>
                        <span>:</span>
                        <span class="rail-value">{escape(refreshed_at.strftime('%Y-%m-%d %H:%M:%S'))}</span>
                    </div>
                    <div class="rail-row">
                        <span class="rail-key">Database</span>
                        <span>:</span>
                        <span class="rail-value">{escape(DB_PATH.name)}</span>
                    </div>
                </div>
            </div>
        </section>
        """,
        unsafe_allow_html=True,
    )


def render_sidebar(vitals: dict[str, object], refreshed_at: datetime) -> None:
    db_state = str(vitals.get("db_connection", "OFFLINE"))
    db_class = "status-dot-online" if db_state == "ONLINE" else "status-dot-error"
    latency_ms = vitals.get("latency_ms")
    latency_label = f"{latency_ms:.2f} ms" if isinstance(latency_ms, float) else "n/a"

    with st.sidebar:
        st.markdown(
            f"""
            <aside class="sidebar-shell">
                <div class="sidebar-title">System vitals</div>
                <div class="vital-row">
                    <span class="vital-key">db</span>
                    <span>:</span>
                    <span class="vital-value"><span class="{db_class}"></span>{escape(db_state)}</span>
                </div>
                <div class="vital-row">
                    <span class="vital-key">engine</span>
                    <span>:</span>
                    <span class="vital-value">{escape(str(vitals.get("engine_status", "UNKNOWN")))}</span>
                </div>
                <div class="vital-row">
                    <span class="vital-key">latency</span>
                    <span>:</span>
                    <span class="vital-value">{escape(latency_label)}</span>
                </div>
                <div class="vital-row">
                    <span class="vital-key">refresh</span>
                    <span>:</span>
                    <span class="vital-value">{escape(refreshed_at.strftime('%H:%M:%S'))}</span>
                </div>
                <div class="vital-row">
                    <span class="vital-key">db file</span>
                    <span>:</span>
                    <span class="vital-value">{escape(DB_PATH.name)}</span>
                </div>
                <div class="sidebar-divider"></div>
                <div class="sidebar-note">
                    Dashboard queries stay read-only. All cached fetches clear on manual refresh.
                </div>
            </aside>
            """,
            unsafe_allow_html=True,
        )
        if st.button("Refresh cache", use_container_width=True):
            st.cache_data.clear()
            st.rerun()


def render_metrics(metrics: dict[str, object]) -> None:
    total_pnl = to_decimal(metrics.get("total_pnl", ZERO))
    win_rate_pct = to_decimal(metrics.get("win_rate", ZERO)) * Decimal("100")
    exposure = to_decimal(metrics.get("open_exposure", ZERO))
    total_decisions = int(metrics.get("total_decisions", 0))
    active_positions = int(metrics.get("active_positions", 0))

    pnl_delta = to_decimal(metrics.get("pnl_delta", ZERO))
    win_rate_delta_pct = to_decimal(metrics.get("win_rate_delta_pct", ZERO))
    exposure_delta = to_decimal(metrics.get("exposure_delta", ZERO))

    pnl_tone = "positive" if total_pnl >= ZERO else "negative"

    metrics_html = "".join(
        [
            build_metric_card(
                area_class="metric-pnl",
                label="realized pnl",
                value=format_usdc(total_pnl),
                value_tone=pnl_tone,
                delta_html=format_delta_tag(pnl_delta, "currency"),
                hint="Closed position settlement, aggregated from the positions ledger.",
                meta="24h settlement drift",
            ),
            build_metric_card(
                area_class="metric-win",
                label="win rate",
                value=f"{win_rate_pct:.2f}%",
                value_tone="neutral",
                delta_html=format_delta_tag(win_rate_delta_pct, "percentage_points"),
                hint="Closed-position hit rate, expressed as a terminal gate for quality.",
                meta="7d vs prior 7d",
            ),
            build_metric_card(
                area_class="metric-exposure",
                label="open exposure",
                value=format_usdc(exposure),
                value_tone="neutral",
                delta_html=format_delta_tag(exposure_delta, "currency", tone_override="neutral"),
                hint="Live capital currently deployed across open positions.",
                meta="fresh open flow",
            ),
            build_metric_card(
                area_class="metric-decisions",
                label="total decisions",
                value=f"{total_decisions:,}",
                value_tone="neutral",
                delta_html='<span class="delta-tag tag-neutral">audit depth</span>',
                hint="Persisted evaluation rows available for operator review.",
                meta="llm event count",
            ),
            build_metric_card(
                area_class="metric-positions",
                label="active positions",
                value=f"{active_positions:,}",
                value_tone="neutral",
                delta_html='<span class="delta-tag tag-neutral">open book</span>',
                hint="Open inventory still waiting on exit logic or resolution.",
                meta="position ledger",
            ),
        ]
    )

    st.markdown(
        f"""
        <div class="section-stack">
            <div class="section-kicker">Performance surface</div>
            <section class="section-shell">
                <div class="section-head">
                    <div>
                        <h2 class="section-title">KPI matrix</h2>
                        <p class="section-caption">Custom HTML blocks replace default Streamlit metrics.</p>
                    </div>
                    <div class="section-meta">5 cells / fixed KPI grid / tabular numerics</div>
                </div>
                <div class="metrics-grid">{metrics_html}</div>
            </section>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if bool(metrics.get("using_mock_deltas", False)):
        st.markdown(
            '<div class="metrics-note">Mock deltas are shown because the positions table does not yet have live history.</div>',
            unsafe_allow_html=True,
        )


def render_chart() -> None:
    pnl_df, is_mock = fetch_pnl_timeseries()

    st.markdown(
        """
        <div class="section-stack">
            <div class="section-kicker">PnL telemetry</div>
            <section class="section-shell">
                <div class="section-head">
                    <div>
                        <h2 class="section-title">Cumulative realised curve</h2>
                        <p class="section-caption">Dark terminal Plotly treatment with restrained gridlines and no legend chrome.</p>
                    </div>
                    <div class="section-meta">plotly / read only / monotone amber trace</div>
                </div>
            </section>
        </div>
        """,
        unsafe_allow_html=True,
    )

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=pnl_df["timestamp"],
            y=pnl_df["pnl_usdc"],
            mode="lines",
            line={
                "color": ACCENT_AMBER,
                "width": 2.35,
                "dash": "dot" if is_mock else "solid",
            },
            hovertemplate="%{x|%Y-%m-%d %H:%M}<br>PnL %{y:,.2f} USDC<extra></extra>",
        )
    )
    fig.update_layout(
        showlegend=False,
        margin={"l": 30, "r": 18, "t": 14, "b": 28},
        height=420,
        paper_bgcolor=SURFACE_PANEL,
        plot_bgcolor=SURFACE_RAISED,
        hovermode="x unified",
        font={
            "color": TEXT_PRIMARY,
            "family": "JetBrains Mono, Fira Code, monospace",
            "size": 11,
        },
        xaxis={
            "showgrid": True,
            "gridcolor": GRID_COLOR,
            "zeroline": False,
            "title": "",
            "tickfont": {"family": "JetBrains Mono, Fira Code, monospace", "size": 10},
        },
        yaxis={
            "showgrid": True,
            "gridcolor": GRID_COLOR,
            "zeroline": True,
            "zerolinecolor": "rgba(255,255,255,0.08)",
            "title": "",
            "tickfont": {"family": "JetBrains Mono, Fira Code, monospace", "size": 10},
            "ticksuffix": " USDC",
        },
    )

    st.plotly_chart(
        fig,
        width="stretch",
        config={"displayModeBar": False, "responsive": True},
    )
    if is_mock:
        st.markdown(
            '<div class="chart-note">Mock curve rendered because no closed-position PnL history is available yet.</div>',
            unsafe_allow_html=True,
        )


def render_empty_state(title: str, body: str) -> None:
    st.markdown(
        f"""
        <section class="empty-shell">
            <p class="empty-title">{escape(title)}</p>
            <p class="empty-copy">{escape(body)}</p>
        </section>
        """,
        unsafe_allow_html=True,
    )


def escape_cell(value: object) -> str:
    if value is None:
        return ""
    return escape(str(value))


def render_audit_table(df: pd.DataFrame) -> None:
    action_tag = {
        "HOLD": (
            '<span style="color:#666;font-size:11px;font-family:monospace;'
            'background:rgba(255,255,255,0.04);padding:2px 6px;border-radius:2px">HOLD</span>'
        ),
        "BUY": (
            '<span style="color:#e8a020;font-size:11px;font-family:monospace;'
            'background:rgba(232,160,32,0.1);padding:2px 6px;border-radius:2px">BUY</span>'
        ),
        "SELL": (
            '<span style="color:#c0392b;font-size:11px;font-family:monospace;'
            'background:rgba(192,57,43,0.1);padding:2px 6px;border-radius:2px">SELL</span>'
        ),
    }
    rows = ""
    for i, row in df.iterrows():
        bg = "rgba(255,255,255,0.015)" if i % 2 == 0 else "transparent"
        action_key = str(row.get("action", "")).upper()
        action_html = action_tag.get(action_key, escape_cell(row.get("action", "")))
        rows += f"""
        <tr style="background:{bg}">
            <td style="padding:10px 12px">{escape_cell(row.get("timestamp", ""))}</td>
            <td style="padding:10px 12px;font-size:11px;color:#888;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                {escape_cell(row.get("market", ""))}</td>
            <td style="padding:10px 12px">{action_html}</td>
            <td style="padding:10px 12px;color:#e8a020">{escape_cell(row.get("confidence", ""))}</td>
            <td style="padding:10px 12px">{escape_cell(row.get("ev", ""))}</td>
            <td style="padding:10px 12px">{escape_cell(row.get("kelly", ""))}</td>
            <td style="padding:10px 12px;color:#666;font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                {escape_cell(row.get("reasoning", ""))}</td>
        </tr>"""

    html = f"""
    <div class="table-shell">
        <div class="table-scroll">
            <table style="width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:12px;color:#ccc;font-variant-numeric:tabular-nums">
                <thead>
                    <tr style="border-bottom:1px solid rgba(255,255,255,0.08);color:#555;font-size:10px;text-transform:uppercase;letter-spacing:0.08em">
                        <th style="padding:8px 12px;text-align:left;font-weight:400">Timestamp</th>
                        <th style="padding:8px 12px;text-align:left;font-weight:400">Market</th>
                        <th style="padding:8px 12px;text-align:left;font-weight:400">Action</th>
                        <th style="padding:8px 12px;text-align:left;font-weight:400">Confidence</th>
                        <th style="padding:8px 12px;text-align:left;font-weight:400">EV</th>
                        <th style="padding:8px 12px;text-align:left;font-weight:400">Kelly</th>
                        <th style="padding:8px 12px;text-align:left;font-weight:400">Reasoning</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>"""
    st.markdown(html, unsafe_allow_html=True)


def render_market_table(df: pd.DataFrame) -> None:
    rows = ""
    for i, row in df.iterrows():
        bg = "rgba(255,255,255,0.015)" if i % 2 == 0 else "transparent"
        yes = float(row.get("yes", 0) or 0)
        no = float(row.get("no", 0) or 0)
        yes_pct = round(yes * 100, 1)
        no_pct = round(no * 100, 1)
        yes_bar = f"""
            <div style="display:flex;align-items:center;gap:6px">
                <div style="width:80px;height:4px;background:rgba(255,255,255,0.06);border-radius:2px;overflow:hidden">
                    <div style="width:{yes_pct}%;height:100%;background:#e8a020;border-radius:2px"></div>
                </div>
                <span style="color:#e8a020">{yes_pct}%</span>
            </div>"""
        no_bar = f"""
            <div style="display:flex;align-items:center;gap:6px">
                <div style="width:80px;height:4px;background:rgba(255,255,255,0.06);border-radius:2px;overflow:hidden">
                    <div style="width:{no_pct}%;height:100%;background:#c0392b;border-radius:2px"></div>
                </div>
                <span style="color:#c0392b">{no_pct}%</span>
            </div>"""
        rows += f"""
        <tr style="background:{bg}">
            <td style="font-size:11px;color:#888;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding:10px 12px">{escape_cell(row.get("market", ""))}</td>
            <td style="padding:10px 12px;color:#999;font-size:12px;max-width:300px">{escape_cell(row.get("question", ""))}</td>
            <td style="padding:10px 12px">{yes_bar}</td>
            <td style="padding:10px 12px">{no_bar}</td>
            <td style="padding:10px 12px;color:#666;font-size:11px">{escape_cell(row.get("volume_24h", "N/A"))}</td>
        </tr>"""

    html = f"""
    <div class="table-shell">
        <div class="table-scroll">
            <table style="width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:12px;color:#ccc;font-variant-numeric:tabular-nums">
                <thead>
                    <tr style="border-bottom:1px solid rgba(255,255,255,0.08);color:#555;font-size:10px;text-transform:uppercase;letter-spacing:0.08em">
                        <th style="padding:8px 12px;text-align:left;font-weight:400">Market</th>
                        <th style="padding:8px 12px;text-align:left;font-weight:400">Question</th>
                        <th style="padding:8px 12px;text-align:left;font-weight:400">Yes</th>
                        <th style="padding:8px 12px;text-align:left;font-weight:400">No</th>
                        <th style="padding:8px 12px;text-align:left;font-weight:400">24h Vol</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
            </table>
        </div>
    </div>"""
    st.markdown(html, unsafe_allow_html=True)


def render_decision_table() -> None:
    decisions_df = fetch_decision_log()

    st.markdown(
        """
        <div class="section-stack">
            <div class="section-kicker">Model governance</div>
            <section class="section-shell">
                <div class="section-head">
                    <div>
                        <h2 class="section-title">Decision audit log</h2>
                        <p class="section-caption">Latest gatekeeper output with confidence, EV, Kelly, and reasoning context.</p>
                    </div>
                    <div class="section-meta">custom html table / action state tags / hover feedback</div>
                </div>
            </section>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if decisions_df.empty:
        render_empty_state(
            "Decision log is empty.",
            "The evaluation layer has not persisted recent rows yet. Run the orchestrator or a backtest to populate the audit surface.",
        )
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
    normalized_df["action"] = (
        normalized_df.get("action", pd.Series(dtype="object"))
        .fillna("HOLD")
        .astype(str)
        .str.upper()
    )
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

    display_df = normalized_df[
        [
            "created_at",
            "market_id",
            "action",
            "confidence_pct",
            "expected_value_pct",
            "kelly_pct",
            "reasoning",
        ]
    ].rename(
        columns={
            "created_at": "timestamp",
            "market_id": "market",
            "action": "action",
            "confidence_pct": "confidence",
            "expected_value_pct": "ev",
            "kelly_pct": "kelly",
            "reasoning": "reasoning",
        }
    )
    display_df["confidence"] = display_df["confidence"].map(lambda value: f"{value:.2f}%")
    display_df["ev"] = display_df["ev"].map(lambda value: f"{value:.2f}%")
    display_df["kelly"] = display_df["kelly"].map(lambda value: f"{value:.2f}%")

    render_audit_table(display_df)
    st.markdown(
        f'<div class="chart-note">Showing last {len(display_df)} decisions.</div>',
        unsafe_allow_html=True,
    )


def render_market_watch() -> None:
    markets_df = fetch_market_watch()

    st.markdown(
        """
        <div class="section-stack">
            <div class="section-kicker">Coverage map</div>
            <section class="section-shell">
                <div class="section-head">
                    <div>
                        <h2 class="section-title">Market watch</h2>
                        <p class="section-caption">Tracked markets sorted by liquidity with probability bars and lifecycle status.</p>
                    </div>
                    <div class="section-meta">custom html table / probability bars / hover feedback</div>
                </div>
            </section>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if markets_df.empty:
        render_empty_state(
            "Market watch is empty.",
            "No tracked markets have been ingested into the current SQLite snapshot yet.",
        )
        return

    display_df = markets_df.copy()
    display_df["yes_price"] = pd.to_numeric(display_df.get("yes_price"), errors="coerce").fillna(0)
    display_df["no_price"] = pd.to_numeric(display_df.get("no_price"), errors="coerce").fillna(0)
    display_df["volume_24h"] = pd.to_numeric(display_df.get("volume_24h"), errors="coerce").fillna(0)
    display_df["end_date"] = pd.to_datetime(display_df.get("end_date"), errors="coerce").dt.strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    display_df["status"] = display_df.get("status", pd.Series(dtype="object")).fillna("UNKNOWN").astype(str).str.upper()

    watch_df = display_df[
        [
            "market_id",
            "question",
            "yes_price",
            "no_price",
            "volume_24h",
        ]
    ].rename(
        columns={
            "market_id": "market",
            "question": "question",
            "yes_price": "yes",
            "no_price": "no",
            "volume_24h": "volume_24h",
        }
    )
    watch_df["volume_24h"] = watch_df["volume_24h"].map(lambda value: f"${value:,.2f}")

    render_market_table(watch_df)
    st.markdown(
        f'<div class="chart-note">{len(watch_df)} markets tracked.</div>',
        unsafe_allow_html=True,
    )


THEME_RULE_COUNT = inject_terminal_theme()
REFRESHED_AT = datetime.now()
SYSTEM_VITALS = get_system_vitals()

render_sidebar(SYSTEM_VITALS, REFRESHED_AT)
render_terminal_header(SYSTEM_VITALS, REFRESHED_AT)

left_col, right_col = st.columns([1.3, 1], gap="large")

with left_col:
    render_chart()

with right_col:
    render_metrics(fetch_metrics())

lower_left, lower_right = st.columns([1.08, 0.92], gap="large")

with lower_left:
    render_decision_table()

with lower_right:
    render_market_watch()
