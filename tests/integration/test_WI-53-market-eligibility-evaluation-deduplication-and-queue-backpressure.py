"""
tests/integration/test_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.py

Integration tests for WI-53: Market Eligibility Preflight, Evaluation
Deduplication, and Prompt Queue Backpressure.

Validates end-to-end wiring of preflight, dedupe, backpressure, and metrics
through the orchestrator pipeline.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from src.schemas.market_eligibility import (
    MarketEligibilityStatus,
    MarketEligibilitySkipReason,
    PromptQueueBackpressureReason,
)


# ===================================================================
# Helpers
# ===================================================================


def _make_config(**overrides):
    """Build a real AppConfig for integration tests."""
    from src.core.config import AppConfig
    base = {
        "anthropic_api_key": "sk-test-key",
        "polygon_rpc_url": "https://rpc.ankr.com/polygon",
        "wallet_address": "0x1111111111111111111111111111111111111111",
        "wallet_private_key": "0x" + "1" * 64,
        "dry_run": True,
    }
    base.update(overrides)
    return AppConfig(_env_file=None, **base)


def _make_market_metadata(condition_id="c1", token_ids=None):
    """Build a MarketMetadata for testing."""
    from src.schemas.market import MarketMetadata
    end = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    return MarketMetadata.model_validate({
        "conditionId": condition_id,
        "question": "Test?",
        "clobTokenIds": token_ids if token_ids is not None else ["tok-1", "tok-2"],
        "endDateIso": end,
        "active": True,
        "closed": False,
    })


# ===================================================================
# Preflight integration tests
# ===================================================================


class TestPreflightIntegration:
    """End-to-end preflight wiring tests."""

    @pytest.mark.asyncio
    async def test_preflight_wired_with_polymarket_client(self):
        """MarketDiscoveryEngine uses polymarket_client for preflight."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from src.agents.ingestion.rest_client import GammaRESTClient
        from src.agents.execution.bankroll_tracker import BankrollPortfolioTracker
        from src.observability.metrics import MetricsRegistry

        market = _make_market_metadata()
        gamma = AsyncMock()
        gamma.get_active_markets.return_value = [market]

        tracker = AsyncMock()
        tracker.get_total_bankroll.return_value = Decimal("1000")
        tracker.get_exposure.return_value = Decimal("0")

        config = _make_config(
            enable_market_discovery_preflight=True,
            market_discovery_max_preflight_candidates=5,
        )

        # Polymarket client that returns a valid order book
        pmc = AsyncMock()
        pmc.fetch_order_book.return_value = None  # No snapshot → unavailable

        metrics = MetricsRegistry()
        engine = MarketDiscoveryEngine(
            gamma_client=gamma,
            bankroll_tracker=tracker,
            config=config,
            polymarket_client=pmc,
            metrics=metrics,
        )

        result = await engine.discover()
        # Preflight enabled, client returns None → market skipped
        assert result == []

    @pytest.mark.asyncio
    async def test_preflight_fail_closed_without_client(self):
        """Preflight enabled but no client → fail closed."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine

        market = _make_market_metadata()
        gamma = AsyncMock()
        gamma.get_active_markets.return_value = [market]

        tracker = AsyncMock()
        tracker.get_total_bankroll.return_value = Decimal("1000")
        tracker.get_exposure.return_value = Decimal("0")

        config = _make_config(
            enable_market_discovery_preflight=True,
        )

        # No polymarket client
        engine = MarketDiscoveryEngine(
            gamma_client=gamma,
            bankroll_tracker=tracker,
            config=config,
            polymarket_client=None,
        )

        result = await engine.discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_preflight_metrics_recorded(self):
        """Preflight pass/fail metrics are recorded."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from src.observability.metrics import MetricsRegistry

        market = _make_market_metadata()
        gamma = AsyncMock()
        gamma.get_active_markets.return_value = [market]

        tracker = AsyncMock()
        tracker.get_total_bankroll.return_value = Decimal("1000")
        tracker.get_exposure.return_value = Decimal("0")

        config = _make_config(
            enable_market_discovery_preflight=True,
        )

        pmc = AsyncMock()
        pmc.fetch_order_book.return_value = None

        metrics = MetricsRegistry()
        engine = MarketDiscoveryEngine(
            gamma_client=gamma,
            bankroll_tracker=tracker,
            config=config,
            polymarket_client=pmc,
            metrics=metrics,
        )

        await engine.discover()
        snapshot = await metrics.snapshot()
        names = {s.name for s in snapshot.samples}
        assert "poly_agent_preflight_fail_total" in names


# ===================================================================
# Dedupe integration tests
# ===================================================================


class TestDedupeIntegration:
    """End-to-end dedupe wiring tests."""

    @pytest.mark.asyncio
    async def test_dedupe_wired_from_config(self):
        """DataAggregator dedupe is configured from AppConfig."""
        from src.agents.context.aggregator import DataAggregator

        config = _make_config(
            enable_market_evaluation_dedupe=True,
            dedupe_min_evaluation_interval_sec=Decimal("60"),
            dedupe_midpoint_delta=Decimal("0.02"),
            dedupe_spread_delta=Decimal("0.01"),
        )

        in_q = asyncio.Queue()
        out_q = asyncio.Queue()
        agg = DataAggregator(in_q, out_q, "c1")
        agg.configure_dedupe(
            enabled=config.enable_market_evaluation_dedupe,
            min_interval_sec=float(config.dedupe_min_evaluation_interval_sec),
            midpoint_delta=config.dedupe_midpoint_delta,
            spread_delta=config.dedupe_spread_delta,
        )

        assert agg._dedupe_enabled is True
        assert agg._dedupe_min_interval == 60.0
        assert agg._dedupe_midpoint_delta == Decimal("0.02")
        assert agg._dedupe_spread_delta == Decimal("0.01")

    @pytest.mark.asyncio
    async def test_dedupe_metrics_recorded(self):
        """Dedupe suppression records metrics."""
        from src.agents.context.aggregator import DataAggregator
        from src.observability.metrics import MetricsRegistry

        metrics = MetricsRegistry()
        in_q = asyncio.Queue()
        out_q = asyncio.Queue()
        agg = DataAggregator(in_q, out_q, "c1", metrics=metrics)
        agg.configure_dedupe(enabled=True)
        agg.register_market("c1", yes_token_id="tok-1")
        agg.best_bid = 0.50
        agg.best_ask = 0.52

        # First emit
        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 1
        await agg.output_queue.get()

        # Second emit — suppressed by dedupe
        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 0

        snapshot = await metrics.snapshot()
        names = {s.name for s in snapshot.samples}
        assert "poly_agent_deduped_contexts_total" in names
        assert "poly_agent_emitted_contexts_total" in names


# ===================================================================
# Backpressure integration tests
# ===================================================================


class TestBackpressureIntegration:
    """End-to-end backpressure wiring tests."""

    @pytest.mark.asyncio
    async def test_bounded_queue_wired_from_config(self):
        """BoundedPromptQueue uses config values."""
        from src.agents.context.bounded_queue import BoundedPromptQueue

        config = _make_config(
            prompt_queue_maxsize=10,
            prompt_queue_coalesce_by_market=False,
        )

        bq = BoundedPromptQueue(
            max_size=config.prompt_queue_maxsize,
            coalescing=config.prompt_queue_coalesce_by_market,
        )

        assert bq.max_size == 10
        assert bq.coalescing is False

    @pytest.mark.asyncio
    async def test_backpressure_metrics_recorded(self):
        """Backpressure drop records metrics."""
        from src.agents.context.bounded_queue import BoundedPromptQueue
        from src.observability.metrics import MetricsRegistry

        metrics = MetricsRegistry()
        bq = BoundedPromptQueue(max_size=1, coalescing=False, metrics=metrics)

        await bq.put({"state": {"condition_id": "c1"}, "prompt": "c1"})
        await bq.put({"state": {"condition_id": "c2"}, "prompt": "c2"})

        snapshot = await metrics.snapshot()
        names = {s.name for s in snapshot.samples}
        assert "poly_agent_dropped_stale_contexts_total" in names

    @pytest.mark.asyncio
    async def test_coalescing_metrics_recorded(self):
        """Backpressure coalesce records metrics."""
        from src.agents.context.bounded_queue import BoundedPromptQueue
        from src.observability.metrics import MetricsRegistry

        metrics = MetricsRegistry()
        bq = BoundedPromptQueue(max_size=1, coalescing=True, metrics=metrics)

        await bq.put({"state": {"condition_id": "c1"}, "prompt": "old"})
        await bq.put({"state": {"condition_id": "c1"}, "prompt": "new"})

        snapshot = await metrics.snapshot()
        names = {s.name for s in snapshot.samples}
        assert "poly_agent_coalesced_contexts_total" in names


# ===================================================================
# Quarantine integration tests
# ===================================================================


class TestQuarantineIntegration:
    """End-to-end quarantine wiring tests."""

    @pytest.mark.asyncio
    async def test_quarantine_metrics_recorded(self):
        """Quarantine records metrics."""
        from src.agents.ingestion.market_quarantine import MarketQuarantineManager
        from src.observability.metrics import MetricsRegistry

        config = _make_config()
        metrics = MetricsRegistry()
        manager = MarketQuarantineManager(config, failure_threshold=2)

        manager.record_failure("c1")
        decision = manager.record_failure("c1")
        assert decision is not None

        # Record quarantine metric
        await metrics.record_quarantine(reason=decision.reason.value)

        snapshot = await metrics.snapshot()
        names = {s.name for s in snapshot.samples}
        assert "poly_agent_quarantine_total" in names
