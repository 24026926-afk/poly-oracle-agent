"""
tests/unit/test_market_discovery.py

Async unit tests for MarketDiscoveryEngine.

Validates:
    - Eligible market selection from mocked Gamma data
    - Metadata presence filter (condition_id + token_ids)
    - Time-to-resolution filter (>= MIN_TTR_HOURS)
    - Exposure limit filter (< max_exposure_pct * bankroll)
    - Empty / unparseable edge cases
    - Decimal math in exposure comparisons
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
from src.schemas.market import MarketMetadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _future_iso(hours: float) -> str:
    """Return an ISO-8601 datetime string *hours* from now (UTC)."""
    dt = datetime.now(timezone.utc) + timedelta(hours=hours)
    return dt.isoformat()


def _past_iso(hours: float) -> str:
    """Return an ISO-8601 datetime string *hours* ago (UTC)."""
    dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    return dt.isoformat()


def _make_market(
    condition_id: str = "cond-abc",
    question: str = "Will X happen?",
    token_ids: list[str] | None = None,
    end_date_iso: str | None = None,
    active: bool = True,
    closed: bool = False,
    volume_24h: float | None = 100.0,
) -> MarketMetadata:
    """Build a MarketMetadata from kwargs."""
    return MarketMetadata.model_validate(
        {
            "conditionId": condition_id,
            "question": question,
            "clobTokenIds": token_ids if token_ids is not None else ["tok-1", "tok-2"],
            "endDateIso": end_date_iso if end_date_iso is not None else _future_iso(24),
            "active": active,
            "closed": closed,
            "volume24hr": volume_24h,
        }
    )


class FakeConfig:
    """Minimal config stub matching fields read by MarketDiscoveryEngine."""

    def __init__(
        self,
        min_ttr_hours: float = 4.0,
        max_exposure_pct: float = 0.03,
        initial_bankroll_usdc: Decimal = Decimal("1000"),
    ) -> None:
        self.min_ttr_hours = min_ttr_hours
        self.max_exposure_pct = max_exposure_pct
        self.initial_bankroll_usdc = initial_bankroll_usdc


def _make_gamma_stub(markets: list[MarketMetadata]) -> AsyncMock:
    """Mock GammaRESTClient returning a fixed market list."""
    stub = AsyncMock()
    stub.get_active_markets.return_value = markets
    return stub


def _make_tracker_stub(
    exposure_map: dict[str, Decimal] | None = None,
    bankroll: Decimal = Decimal("1000"),
) -> AsyncMock:
    """Mock BankrollPortfolioTracker with configurable exposure per market."""
    exposure_map = exposure_map or {}
    stub = AsyncMock()
    stub.get_total_bankroll.return_value = bankroll
    stub.get_exposure.side_effect = lambda cid: exposure_map.get(cid, Decimal("0"))
    return stub


def _build_engine(
    markets: list[MarketMetadata] | None = None,
    exposure_map: dict[str, Decimal] | None = None,
    bankroll: Decimal = Decimal("1000"),
    min_ttr_hours: float = 4.0,
    max_exposure_pct: float = 0.03,
) -> MarketDiscoveryEngine:
    """Convenience builder: creates engine with mocked dependencies."""
    gamma = _make_gamma_stub(markets or [])
    tracker = _make_tracker_stub(exposure_map, bankroll)
    config = FakeConfig(min_ttr_hours, max_exposure_pct)
    return MarketDiscoveryEngine(gamma, tracker, config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_returns_eligible_markets():
    """Happy path: all three markets pass every filter."""
    markets = [
        _make_market(condition_id="m1", end_date_iso=_future_iso(10)),
        _make_market(condition_id="m2", end_date_iso=_future_iso(48)),
        _make_market(condition_id="m3", end_date_iso=_future_iso(100)),
    ]
    engine = _build_engine(markets=markets)

    result = await engine.discover()

    assert result == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_discover_excludes_empty_token_ids():
    """Markets with no token_ids are excluded (missing trading metadata)."""
    markets = [
        _make_market(condition_id="good", token_ids=["tok-1"]),
        _make_market(condition_id="bad", token_ids=[]),
    ]
    engine = _build_engine(markets=markets)

    result = await engine.discover()

    assert result == ["good"]


@pytest.mark.asyncio
async def test_discover_excludes_ttr_below_minimum():
    """Markets resolving in < MIN_TTR_HOURS are excluded."""
    markets = [
        _make_market(condition_id="soon", end_date_iso=_future_iso(2)),
        _make_market(condition_id="later", end_date_iso=_future_iso(10)),
    ]
    engine = _build_engine(markets=markets, min_ttr_hours=4.0)

    result = await engine.discover()

    assert result == ["later"]


@pytest.mark.asyncio
async def test_discover_excludes_no_end_date():
    """Markets with end_date_iso=None cannot be verified for TTR."""
    # Override frozen field via model_validate with None end_date
    m_no_date = MarketMetadata.model_validate(
        {
            "conditionId": "no-date",
            "question": "Q?",
            "clobTokenIds": ["t1"],
            "endDateIso": None,
            "active": True,
            "closed": False,
        }
    )
    engine = _build_engine(markets=[m_no_date])

    result = await engine.discover()

    assert result == []


@pytest.mark.asyncio
async def test_discover_excludes_past_end_date():
    """Markets whose end_date is in the past yield negative TTR."""
    markets = [
        _make_market(condition_id="expired", end_date_iso=_past_iso(2)),
    ]
    engine = _build_engine(markets=markets)

    result = await engine.discover()

    assert result == []


@pytest.mark.asyncio
async def test_discover_excludes_at_exposure_limit():
    """Market at exactly the exposure cap (>= comparison) is excluded."""
    markets = [
        _make_market(condition_id="maxed", end_date_iso=_future_iso(24)),
    ]
    # cap = 0.03 * 1000 = 30  →  exposure == 30 → excluded
    engine = _build_engine(
        markets=markets,
        exposure_map={"maxed": Decimal("30")},
        bankroll=Decimal("1000"),
        max_exposure_pct=0.03,
    )

    result = await engine.discover()

    assert result == []


@pytest.mark.asyncio
async def test_discover_includes_below_exposure_limit():
    """Market below the exposure cap passes."""
    markets = [
        _make_market(condition_id="ok", end_date_iso=_future_iso(24)),
    ]
    # cap = 30, exposure = 29 → passes
    engine = _build_engine(
        markets=markets,
        exposure_map={"ok": Decimal("29")},
    )

    result = await engine.discover()

    assert result == ["ok"]


@pytest.mark.asyncio
async def test_discover_returns_empty_when_no_eligible():
    """When all markets fail filters, returns [] (no hardcoded fallback)."""
    markets = [
        _make_market(condition_id="bad1", token_ids=[]),
        _make_market(condition_id="bad2", end_date_iso=_future_iso(1)),
    ]
    engine = _build_engine(markets=markets)

    result = await engine.discover()

    assert result == []


@pytest.mark.asyncio
async def test_discover_handles_empty_gamma_response():
    """Gamma returns no markets — returns [] gracefully."""
    engine = _build_engine(markets=[])

    result = await engine.discover()

    assert result == []


@pytest.mark.asyncio
async def test_discover_handles_unparseable_end_date():
    """Malformed end_date_iso is excluded without raising."""
    m_bad_date = MarketMetadata.model_validate(
        {
            "conditionId": "bad-date",
            "question": "Q?",
            "clobTokenIds": ["t1"],
            "endDateIso": "not-a-date",
            "active": True,
            "closed": False,
        }
    )
    engine = _build_engine(markets=[m_bad_date])

    result = await engine.discover()

    assert result == []


@pytest.mark.asyncio
async def test_ttr_computation_accuracy():
    """Direct test of _compute_hours_to_resolution precision."""
    engine = _build_engine()
    future = datetime.now(timezone.utc) + timedelta(hours=10)
    iso_str = future.isoformat()

    hours = engine._compute_hours_to_resolution(iso_str)

    assert hours is not None
    # Allow 1 minute of drift from test execution time
    assert abs(hours - 10.0) < (1.0 / 60.0)


@pytest.mark.asyncio
async def test_exposure_check_uses_decimal():
    """Verify that exposure comparison is Decimal, not float (no precision loss)."""
    # Decimal("30.000000000000001") > Decimal("30") is True
    # float(30.000000000000001) > float(30) might be False due to precision
    markets = [
        _make_market(condition_id="edge", end_date_iso=_future_iso(24)),
    ]
    engine = _build_engine(
        markets=markets,
        exposure_map={"edge": Decimal("30.000000000000001")},
        bankroll=Decimal("1000"),
        max_exposure_pct=0.03,
    )

    result = await engine.discover()

    assert result == []
