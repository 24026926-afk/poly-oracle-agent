"""
tests/conftest.py

Shared async test fixtures for poly-oracle-agent.
Provides an in-memory SQLite database with per-test rollback isolation,
plus integration-level fixtures for config, mocked externals, and queues.
"""

from __future__ import annotations

# Set env vars BEFORE any src imports to satisfy AppConfig at collection time.
import os as _os

_os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake-key-000")
_os.environ.setdefault("POLYGON_RPC_URL", "http://localhost:8545")
_os.environ.setdefault("WALLET_ADDRESS", "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045")
_os.environ.setdefault("WALLET_PRIVATE_KEY", "0x" + "a1" * 32)

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import AppConfig
from src.db.models import Base
from src.schemas.market import MarketMetadata


# ---------------------------------------------------------------------------
# Session / backend
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


# ---------------------------------------------------------------------------
# Database fixtures (unit + integration)
# ---------------------------------------------------------------------------

@pytest.fixture()
async def async_engine():
    """Create an in-memory async SQLite engine and provision all tables."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        echo=False,
        future=True,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture()
async def async_session(async_engine):
    """Yield an AsyncSession that rolls back after each test."""
    session_factory = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with session_factory() as session:
        async with session.begin():
            yield session
            # Rollback on exit to keep tests isolated
            await session.rollback()


@pytest.fixture()
def db_session_factory(async_engine):
    """Return an async_sessionmaker bound to the in-memory test engine."""
    return async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


# ---------------------------------------------------------------------------
# Config fixture (integration)
# ---------------------------------------------------------------------------

@pytest.fixture()
def test_config() -> AppConfig:
    """AppConfig with safe test values.  dry_run=True, no .env dependency."""
    return AppConfig.model_construct(
        anthropic_api_key=SecretStr("sk-ant-test-fake-key-000"),
        anthropic_model="claude-3-5-sonnet-20241022",
        anthropic_max_tokens=4096,
        anthropic_max_retries=2,
        polygon_rpc_url="http://localhost:8545",
        wallet_address="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        wallet_private_key=SecretStr("0x" + "a1" * 32),
        clob_rest_url="http://localhost:9999",
        clob_ws_url="ws://localhost:9998",
        gamma_api_url="http://localhost:9997",
        kelly_fraction=0.25,
        min_confidence=0.75,
        max_spread_pct=0.015,
        max_exposure_pct=0.03,
        min_ev_threshold=0.02,
        min_ttr_hours=4.0,
        initial_bankroll_usdc=Decimal("10000"),
        max_gas_price_gwei=500.0,
        fallback_gas_price_gwei=50.0,
        grok_api_key=SecretStr("grok-test-fake-key-000"),
        grok_base_url="http://localhost:9996",
        grok_model="grok-3",
        grok_mocked=True,
        database_url="sqlite+aiosqlite://",
        log_level="DEBUG",
        dry_run=True,
    )


# ---------------------------------------------------------------------------
# Queue fixtures (integration)
# ---------------------------------------------------------------------------

@pytest.fixture()
def pipeline_queues():
    """Return (market_queue, prompt_queue, execution_queue)."""
    return asyncio.Queue(), asyncio.Queue(), asyncio.Queue()


# ---------------------------------------------------------------------------
# Mock Gamma market data (integration)
# ---------------------------------------------------------------------------

def _future_iso(hours: float) -> str:
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.isoformat()


@pytest.fixture()
def mock_gamma_markets() -> list[MarketMetadata]:
    """Three markets: two eligible, one ineligible (past end date)."""
    return [
        MarketMetadata.model_validate({
            "conditionId": "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
            "question": "Will ETH exceed $5000 by June?",
            "clobTokenIds": ["tok-a1", "tok-a2"],
            "endDateIso": _future_iso(72),
            "active": True,
            "closed": False,
        }),
        MarketMetadata.model_validate({
            "conditionId": "0xbbbb2222cccc3333dddd4444eeee5555ffff6666",
            "question": "Will BTC exceed $100k by July?",
            "clobTokenIds": ["tok-b1", "tok-b2"],
            "endDateIso": _future_iso(120),
            "active": True,
            "closed": False,
        }),
        MarketMetadata.model_validate({
            "conditionId": "0xcccc3333dddd4444eeee5555ffff6666aaaa7777",
            "question": "Will SOL exceed $500 by April?",
            "clobTokenIds": ["tok-c1", "tok-c2"],
            "endDateIso": _future_iso(-2),  # expired
            "active": True,
            "closed": False,
        }),
    ]


# ---------------------------------------------------------------------------
# Mock Anthropic LLM response (integration)
# ---------------------------------------------------------------------------

def _build_llm_response_json(
    *,
    decision: bool = True,
    action: str = "BUY",
    confidence: float = 0.85,
    p_true: float = 0.65,
    p_market: float = 0.45,
    condition_id: str = "0xaaaa1111bbbb2222cccc3333dddd4444eeee5555",
) -> str:
    """Build a raw JSON string that passes LLMEvaluationResponse validation."""
    end_date = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    payload = {
        "market_context": {
            "condition_id": condition_id,
            "outcome_evaluated": "YES",
            "best_bid": 0.45,
            "best_ask": 0.455,
            "midpoint": 0.4525,
            "market_end_date": end_date,
        },
        "probabilistic_estimate": {
            "p_true": p_true,
            "p_market": p_market,
        },
        "risk_assessment": {
            "liquidity_risk_score": 0.2,
            "resolution_risk_score": 0.1,
            "information_asymmetry_flag": False,
            "risk_notes": (
                "Low risk market with adequate liquidity and clear "
                "resolution criteria established by oracle."
            ),
        },
        "confidence_score": confidence,
        "decision_boolean": decision,
        "recommended_action": action,
        "reasoning_log": (
            "Based on thorough analysis of the market data and external "
            "signals, the true probability is estimated at 65% while the "
            "market implies approximately 45%. This creates a significant "
            "positive expected value opportunity with adequate confidence."
        ),
    }
    return json.dumps(payload)


@pytest.fixture()
def mock_anthropic_buy_json() -> str:
    """Canned Anthropic response JSON that passes all gatekeeper filters (BUY)."""
    return _build_llm_response_json(decision=True, action="BUY")


@pytest.fixture()
def mock_anthropic_hold_json() -> str:
    """Canned Anthropic response JSON that fails confidence filter (HOLD)."""
    return _build_llm_response_json(
        decision=True,
        action="BUY",
        confidence=0.50,  # below 0.75 → gatekeeper overrides to HOLD
    )
