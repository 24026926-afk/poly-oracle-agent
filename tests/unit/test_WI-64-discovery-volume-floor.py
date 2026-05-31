"""
tests/unit/test_WI-64-discovery-volume-floor.py

WI-64: Discovery volume floor.

``MarketDiscoveryEngine.discover`` must prune markets whose 24h volume is below
``AppConfig.min_market_volume_24h_usdc`` (or missing) before any order-book
preflight fetch or LLM evaluation. The filter is opt-in: a default threshold of
``Decimal("0")`` is a complete no-op. Volume is compared with ``Decimal`` via a
single ``Decimal(str(volume_24h))`` conversion — no float arithmetic.

Exclusion tests fail against the pre-WI-64 code (no volume filter exists);
inclusion / no-op tests are regression guards.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
from src.schemas.market import MarketMetadata


def _future_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


def _make_market(
    condition_id: str = "cond-abc",
    token_ids: list[str] | None = None,
    volume_24h: float | None = 100.0,
) -> MarketMetadata:
    return MarketMetadata.model_validate(
        {
            "conditionId": condition_id,
            "question": "Will X happen?",
            "clobTokenIds": token_ids if token_ids is not None else ["tok-1", "tok-2"],
            "endDateIso": _future_iso(24),
            "active": True,
            "closed": False,
            "volume24hr": volume_24h,
        }
    )


class FakeConfig:
    """Minimal config stub including the WI-64 volume floor field."""

    def __init__(
        self,
        min_market_volume_24h_usdc: Decimal = Decimal("0"),
        min_ttr_hours: float = 4.0,
        max_exposure_pct: float = 0.03,
    ) -> None:
        self.min_market_volume_24h_usdc = min_market_volume_24h_usdc
        self.min_ttr_hours = min_ttr_hours
        self.max_exposure_pct = max_exposure_pct
        self.initial_bankroll_usdc = Decimal("1000")
        self.enable_market_discovery_preflight = False
        self.market_discovery_preflight_timeout_ms = Decimal("5000")
        self.market_discovery_max_preflight_candidates = 50
        self.preflight_quarantine_duration_seconds = Decimal("300")
        self.preflight_max_spread_pct = Decimal("0.05")
        self.dry_run = True


def _make_tracker() -> AsyncMock:
    stub = AsyncMock()
    stub.get_total_bankroll.return_value = Decimal("1000")
    stub.get_exposure.side_effect = lambda cid: Decimal("0")
    return stub


def _build_engine(markets, threshold: Decimal, event_publisher=None):
    gamma = AsyncMock()
    gamma.get_active_markets.return_value = markets
    config = FakeConfig(min_market_volume_24h_usdc=threshold)
    return MarketDiscoveryEngine(
        gamma, _make_tracker(), config, event_publisher=event_publisher
    )


async def _ids(markets, threshold, **kw):
    engine = _build_engine(markets, threshold, **kw)
    result = await engine.discover()
    return [m.condition_id for m in result]


# ---------------------------------------------------------------------------
# Exclusion behavior (RED against pre-WI-64 code)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_low_volume_market_excluded():
    markets = [_make_market("low", volume_24h=100.0)]
    assert await _ids(markets, Decimal("1000")) == []


@pytest.mark.asyncio
async def test_high_volume_market_passes_when_active():
    markets = [_make_market("high", volume_24h=5000.0)]
    assert await _ids(markets, Decimal("1000")) == ["high"]


@pytest.mark.asyncio
async def test_mixed_volumes_only_above_threshold_pass():
    markets = [
        _make_market("a", volume_24h=50.0),
        _make_market("b", volume_24h=500.0),
        _make_market("c", volume_24h=1500.0),
    ]
    assert await _ids(markets, Decimal("1000")) == ["c"]


@pytest.mark.asyncio
async def test_none_volume_excluded_when_active():
    markets = [_make_market("unknown", volume_24h=None)]
    assert await _ids(markets, Decimal("1000")) == []


@pytest.mark.asyncio
async def test_decimal_comparison_below_threshold_excluded():
    # 100.4 < 100.5 with exact Decimal comparison.
    markets = [_make_market("just_below", volume_24h=100.4)]
    assert await _ids(markets, Decimal("100.5")) == []


@pytest.mark.asyncio
async def test_decimal_comparison_above_threshold_included():
    markets = [_make_market("just_above", volume_24h=100.6)]
    assert await _ids(markets, Decimal("100.5")) == ["just_above"]


@pytest.mark.asyncio
async def test_low_volume_emits_rejection_event():
    events = []

    async def _capture(evt):
        events.append(evt)

    markets = [_make_market("low", volume_24h=10.0)]
    engine = _build_engine(markets, Decimal("1000"), event_publisher=_capture)
    await engine.discover()
    messages = [getattr(getattr(e, "payload", None), "message", "") for e in events]
    assert any("volume" in m.lower() for m in messages)


@pytest.mark.asyncio
async def test_all_excluded_returns_empty_no_crash():
    markets = [
        _make_market("a", volume_24h=1.0),
        _make_market("b", volume_24h=None),
    ]
    assert await _ids(markets, Decimal("1000000")) == []


# ---------------------------------------------------------------------------
# No-op / inclusion (regression guards: green now and after)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_by_default_passes_low_volume():
    markets = [_make_market("low", volume_24h=1.0)]
    assert await _ids(markets, Decimal("0")) == ["low"]


@pytest.mark.asyncio
async def test_disabled_passes_none_volume():
    markets = [_make_market("unknown", volume_24h=None)]
    assert await _ids(markets, Decimal("0")) == ["unknown"]


@pytest.mark.asyncio
async def test_negative_threshold_treated_as_disabled():
    markets = [_make_market("low", volume_24h=1.0)]
    assert await _ids(markets, Decimal("-1")) == ["low"]


@pytest.mark.asyncio
async def test_volume_equal_threshold_included():
    markets = [_make_market("equal", volume_24h=100.0)]
    assert await _ids(markets, Decimal("100")) == ["equal"]
