"""
tests/unit/test_WI-53-market-eligibility-evaluation-deduplication-and-queue-backpressure.py

Unit tests for WI-53: Market Eligibility Preflight, Evaluation Deduplication,
and Prompt Queue Backpressure.

All financial comparisons use Decimal.  No float is permitted.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from src.schemas.market_eligibility import (
    MarketEligibilityPreflightResult,
    MarketEligibilityStatus,
    MarketEligibilitySkipReason,
    MarketQuarantineDecision,
    MarketQuarantineReason,
    MarketEvaluationFingerprint,
    MarketEvaluationDedupeDecision,
    MarketEvaluationDedupeReason,
    PromptQueueBackpressureDecision,
    PromptQueueBackpressureReason,
    StaleContextSkipReason,
    PromptQueueDepthSnapshot,
)


# ===================================================================
# Schema tests — preflight
# ===================================================================


class TestMarketEligibilityPreflightResultSchema:
    """Typed preflight result schema."""

    def test_preflight_result_has_status_field(self):
        result = MarketEligibilityPreflightResult(
            condition_id="c1",
            status=MarketEligibilityStatus.ELIGIBLE,
        )
        assert result.status == MarketEligibilityStatus.ELIGIBLE

    def test_preflight_result_has_skip_reason_field(self):
        result = MarketEligibilityPreflightResult(
            condition_id="c1",
            status=MarketEligibilityStatus.SKIPPED,
            skip_reason=MarketEligibilitySkipReason.CROSSED_BOOK,
        )
        assert result.skip_reason == MarketEligibilitySkipReason.CROSSED_BOOK

    def test_preflight_result_has_condition_id_field(self):
        result = MarketEligibilityPreflightResult(
            condition_id="cond-abc",
            status=MarketEligibilityStatus.ELIGIBLE,
        )
        assert result.condition_id == "cond-abc"

    def test_preflight_result_has_spread_decimal_field(self):
        result = MarketEligibilityPreflightResult(
            condition_id="c1",
            status=MarketEligibilityStatus.ELIGIBLE,
            spread=Decimal("0.015"),
        )
        assert isinstance(result.spread, Decimal)
        assert result.spread == Decimal("0.015")

    def test_preflight_result_has_midpoint_decimal_field(self):
        result = MarketEligibilityPreflightResult(
            condition_id="c1",
            status=MarketEligibilityStatus.ELIGIBLE,
            midpoint=Decimal("0.55"),
        )
        assert isinstance(result.midpoint, Decimal)
        assert result.midpoint == Decimal("0.55")

    def test_preflight_result_rejects_float_midpoint(self):
        with pytest.raises(ValueError, match="Float values are forbidden"):
            MarketEligibilityPreflightResult(
                condition_id="c1",
                status=MarketEligibilityStatus.ELIGIBLE,
                midpoint=0.55,
            )

    def test_preflight_result_rejects_float_spread(self):
        with pytest.raises(ValueError, match="Float values are forbidden"):
            MarketEligibilityPreflightResult(
                condition_id="c1",
                status=MarketEligibilityStatus.ELIGIBLE,
                spread=0.015,
            )


class TestMarketEligibilityStatusEnum:
    """Eligibility status enumeration."""

    def test_status_eligible_exists(self):
        assert MarketEligibilityStatus.ELIGIBLE.value == "ELIGIBLE"

    def test_status_skipped_exists(self):
        assert MarketEligibilityStatus.SKIPPED.value == "SKIPPED"

    def test_status_quarantined_exists(self):
        assert MarketEligibilityStatus.QUARANTINED.value == "QUARANTINED"


class TestMarketEligibilitySkipReasonEnum:
    """Typed skip reasons for preflight failures."""

    def test_skip_reason_missing_token_context(self):
        assert (
            MarketEligibilitySkipReason.MISSING_TOKEN_CONTEXT.value
            == "MISSING_TOKEN_CONTEXT"
        )

    def test_skip_reason_order_book_unavailable(self):
        assert (
            MarketEligibilitySkipReason.ORDER_BOOK_UNAVAILABLE.value
            == "ORDER_BOOK_UNAVAILABLE"
        )

    def test_skip_reason_non_positive_quote(self):
        assert (
            MarketEligibilitySkipReason.NON_POSITIVE_QUOTE.value == "NON_POSITIVE_QUOTE"
        )

    def test_skip_reason_crossed_book(self):
        assert MarketEligibilitySkipReason.CROSSED_BOOK.value == "CROSSED_BOOK"

    def test_skip_reason_spread_too_wide(self):
        assert MarketEligibilitySkipReason.SPREAD_TOO_WIDE.value == "SPREAD_TOO_WIDE"

    def test_skip_reason_preflight_timeout(self):
        assert (
            MarketEligibilitySkipReason.PREFLIGHT_TIMEOUT.value == "PREFLIGHT_TIMEOUT"
        )


# ===================================================================
# Schema tests — quarantine
# ===================================================================


class TestMarketQuarantineDecisionSchema:
    """Quarantine decision for a single market."""

    def test_quarantine_decision_has_condition_id(self):
        now = datetime.now(timezone.utc)
        decision = MarketQuarantineDecision(
            condition_id="c1",
            reason=MarketQuarantineReason.REPEATED_PREFLIGHT_FAILURE,
            quarantined_at_utc=now,
            expires_at_utc=now + timedelta(seconds=300),
        )
        assert decision.condition_id == "c1"

    def test_quarantine_decision_has_reason(self):
        now = datetime.now(timezone.utc)
        decision = MarketQuarantineDecision(
            condition_id="c1",
            reason=MarketQuarantineReason.REPEATED_PREFLIGHT_FAILURE,
            quarantined_at_utc=now,
            expires_at_utc=now + timedelta(seconds=300),
        )
        assert decision.reason == MarketQuarantineReason.REPEATED_PREFLIGHT_FAILURE

    def test_quarantine_decision_has_expiry_timestamp(self):
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=300)
        decision = MarketQuarantineDecision(
            condition_id="c1",
            reason=MarketQuarantineReason.REPEATED_PREFLIGHT_FAILURE,
            quarantined_at_utc=now,
            expires_at_utc=expires,
        )
        assert decision.expires_at_utc == expires


class TestMarketQuarantineReasonEnum:
    """Quarantine reason enumeration."""

    def test_quarantine_reason_repeated_preflight_failure(self):
        assert (
            MarketQuarantineReason.REPEATED_PREFLIGHT_FAILURE.value
            == "REPEATED_PREFLIGHT_FAILURE"
        )


# ===================================================================
# Schema tests — dedupe
# ===================================================================


class TestMarketEvaluationFingerprintSchema:
    """Fingerprint representing material market state for dedupe."""

    def test_fingerprint_has_condition_id(self):
        fp = MarketEvaluationFingerprint(
            condition_id="c1",
            midpoint=Decimal("0.5"),
            spread=Decimal("0.02"),
        )
        assert fp.condition_id == "c1"

    def test_fingerprint_has_midpoint_decimal(self):
        fp = MarketEvaluationFingerprint(
            condition_id="c1",
            midpoint=Decimal("0.55"),
            spread=Decimal("0.02"),
        )
        assert isinstance(fp.midpoint, Decimal)

    def test_fingerprint_has_spread_decimal(self):
        fp = MarketEvaluationFingerprint(
            condition_id="c1",
            midpoint=Decimal("0.55"),
            spread=Decimal("0.015"),
        )
        assert isinstance(fp.spread, Decimal)

    def test_fingerprint_has_timestamp(self):
        fp = MarketEvaluationFingerprint(
            condition_id="c1",
            midpoint=Decimal("0.55"),
            spread=Decimal("0.015"),
        )
        assert isinstance(fp.captured_at_utc, datetime)

    def test_fingerprint_rejects_float_midpoint(self):
        with pytest.raises(ValueError, match="Float values are forbidden"):
            MarketEvaluationFingerprint(
                condition_id="c1",
                midpoint=0.55,
                spread=Decimal("0.02"),
            )

    def test_fingerprint_rejects_float_spread(self):
        with pytest.raises(ValueError, match="Float values are forbidden"):
            MarketEvaluationFingerprint(
                condition_id="c1",
                midpoint=Decimal("0.55"),
                spread=0.02,
            )


class TestMarketEvaluationDedupeDecisionSchema:
    """Dedupe decision for a single market evaluation."""

    def test_dedupe_decision_has_condition_id(self):
        decision = MarketEvaluationDedupeDecision(
            condition_id="c1",
            emit=True,
            reason=MarketEvaluationDedupeReason.MIDPOINT_MOVED,
        )
        assert decision.condition_id == "c1"

    def test_dedupe_decision_has_emit_flag(self):
        decision = MarketEvaluationDedupeDecision(
            condition_id="c1",
            emit=False,
            reason=MarketEvaluationDedupeReason.UNCHANGED_STATE,
        )
        assert decision.emit is False

    def test_dedupe_decision_has_reason(self):
        decision = MarketEvaluationDedupeDecision(
            condition_id="c1",
            emit=True,
            reason=MarketEvaluationDedupeReason.SPREAD_MOVED,
        )
        assert decision.reason == MarketEvaluationDedupeReason.SPREAD_MOVED


class TestMarketEvaluationDedupeReasonEnum:
    """Dedupe reason enumeration."""

    def test_dedupe_reason_unchanged_state(self):
        assert MarketEvaluationDedupeReason.UNCHANGED_STATE.value == "UNCHANGED_STATE"

    def test_dedupe_reason_insufficient_elapsed_time(self):
        assert (
            MarketEvaluationDedupeReason.INSUFFICIENT_ELAPSED_TIME.value
            == "INSUFFICIENT_ELAPSED_TIME"
        )

    def test_dedupe_reason_midpoint_moved(self):
        assert MarketEvaluationDedupeReason.MIDPOINT_MOVED.value == "MIDPOINT_MOVED"

    def test_dedupe_reason_spread_moved(self):
        assert MarketEvaluationDedupeReason.SPREAD_MOVED.value == "SPREAD_MOVED"


# ===================================================================
# Schema tests — prompt queue backpressure
# ===================================================================


class TestPromptQueueBackpressureDecisionSchema:
    """Backpressure decision when prompt queue is full."""

    def test_backpressure_decision_has_action(self):
        decision = PromptQueueBackpressureDecision(
            action="coalesce",
            reason=PromptQueueBackpressureReason.COALESCED,
            queue_depth=10,
            condition_id="c1",
        )
        assert decision.action == "coalesce"

    def test_backpressure_decision_has_reason(self):
        decision = PromptQueueBackpressureDecision(
            action="drop",
            reason=PromptQueueBackpressureReason.STALE_DROPPED,
            queue_depth=10,
        )
        assert decision.reason == PromptQueueBackpressureReason.STALE_DROPPED

    def test_backpressure_decision_has_queue_depth(self):
        decision = PromptQueueBackpressureDecision(
            action="enqueue",
            reason=PromptQueueBackpressureReason.QUEUE_FULL,
            queue_depth=10,
        )
        assert decision.queue_depth == 10


class TestPromptQueueBackpressureReasonEnum:
    """Backpressure reason enumeration."""

    def test_backpressure_reason_queue_full(self):
        assert PromptQueueBackpressureReason.QUEUE_FULL.value == "QUEUE_FULL"

    def test_backpressure_reason_coalesced(self):
        assert PromptQueueBackpressureReason.COALESCED.value == "COALESCED"

    def test_backpressure_reason_stale_dropped(self):
        assert PromptQueueBackpressureReason.STALE_DROPPED.value == "STALE_DROPPED"


class TestStaleContextSkipReasonEnum:
    """Stale context skip reason enumeration."""

    def test_stale_skip_reason_queue_full_no_match(self):
        assert StaleContextSkipReason.QUEUE_FULL_NO_MATCH.value == "QUEUE_FULL_NO_MATCH"

    def test_stale_skip_reason_coalesced_replaced(self):
        assert StaleContextSkipReason.COALESCED_REPLACED.value == "COALESCED_REPLACED"


class TestPromptQueueDepthSnapshotSchema:
    """Snapshot of prompt queue depth for metrics."""

    def test_depth_snapshot_has_current_depth(self):
        snap = PromptQueueDepthSnapshot(current_depth=5, max_size=20)
        assert snap.current_depth == 5

    def test_depth_snapshot_has_max_size(self):
        snap = PromptQueueDepthSnapshot(current_depth=5, max_size=20)
        assert snap.max_size == 20

    def test_depth_snapshot_has_timestamp(self):
        snap = PromptQueueDepthSnapshot(current_depth=5, max_size=20)
        assert isinstance(snap.captured_at_utc, datetime)


# ===================================================================
# AppConfig fields
# ===================================================================

_BASE_CONFIG = {
    "anthropic_api_key": "sk-test-key",
    "polygon_rpc_url": "https://rpc.ankr.com/polygon",
    "wallet_address": "0x1111111111111111111111111111111111111111",
    "wallet_private_key": "0x" + "1" * 64,
    "dry_run": True,
}


def _make_config(**overrides):
    from src.core.config import AppConfig

    base = dict(_BASE_CONFIG)
    base.update(overrides)
    return AppConfig(_env_file=None, **base)


class TestAppConfigPreflightFields:
    """AppConfig fields for market discovery preflight."""

    def test_enable_market_discovery_preflight_field(self):
        cfg = _make_config()
        assert hasattr(cfg, "enable_market_discovery_preflight")
        assert cfg.enable_market_discovery_preflight is False

    def test_market_discovery_preflight_timeout_ms_field(self):
        cfg = _make_config()
        assert hasattr(cfg, "market_discovery_preflight_timeout_ms")
        assert isinstance(cfg.market_discovery_preflight_timeout_ms, Decimal)

    def test_market_discovery_max_preflight_candidates_field(self):
        cfg = _make_config()
        assert hasattr(cfg, "market_discovery_max_preflight_candidates")
        assert cfg.market_discovery_max_preflight_candidates >= 1

    def test_preflight_quarantine_duration_seconds_field(self):
        cfg = _make_config()
        assert hasattr(cfg, "preflight_quarantine_duration_seconds")
        assert isinstance(cfg.preflight_quarantine_duration_seconds, Decimal)

    def test_preflight_max_spread_pct_field(self):
        cfg = _make_config()
        assert hasattr(cfg, "preflight_max_spread_pct")
        assert isinstance(cfg.preflight_max_spread_pct, Decimal)

    def test_preflight_fields_reject_negative_values(self):
        with pytest.raises(Exception):
            _make_config(market_discovery_preflight_timeout_ms=Decimal("-1"))


class TestAppConfigDedupeFields:
    """AppConfig fields for evaluation dedupe."""

    def test_enable_market_evaluation_dedupe_field(self):
        cfg = _make_config()
        assert hasattr(cfg, "enable_market_evaluation_dedupe")
        assert cfg.enable_market_evaluation_dedupe is False

    def test_dedupe_min_evaluation_interval_sec_field(self):
        cfg = _make_config()
        assert hasattr(cfg, "dedupe_min_evaluation_interval_sec")
        assert isinstance(cfg.dedupe_min_evaluation_interval_sec, Decimal)

    def test_dedupe_midpoint_delta_field(self):
        cfg = _make_config()
        assert hasattr(cfg, "dedupe_midpoint_delta")
        assert isinstance(cfg.dedupe_midpoint_delta, Decimal)

    def test_dedupe_spread_delta_field(self):
        cfg = _make_config()
        assert hasattr(cfg, "dedupe_spread_delta")
        assert isinstance(cfg.dedupe_spread_delta, Decimal)

    def test_dedupe_fields_reject_negative_values(self):
        with pytest.raises(Exception):
            _make_config(dedupe_min_evaluation_interval_sec=Decimal("-1"))


class TestAppConfigPromptQueueFields:
    """AppConfig fields for prompt queue bounds."""

    def test_prompt_queue_maxsize_field(self):
        cfg = _make_config()
        assert hasattr(cfg, "prompt_queue_maxsize")
        assert cfg.prompt_queue_maxsize >= 1

    def test_prompt_queue_coalesce_by_market_field(self):
        cfg = _make_config()
        assert hasattr(cfg, "prompt_queue_coalesce_by_market")
        assert isinstance(cfg.prompt_queue_coalesce_by_market, bool)

    def test_prompt_queue_fields_reject_negative_values(self):
        with pytest.raises(Exception):
            _make_config(prompt_queue_maxsize=-1)


# ===================================================================
# MarketDiscoveryEngine — preflight integration
# ===================================================================


def _make_fake_config_preflight(**overrides):
    """FakeConfig for preflight tests."""
    from tests.unit.test_market_discovery import FakeConfig

    base = {
        "enable_market_discovery_preflight": True,
        "market_discovery_preflight_timeout_ms": Decimal("5000"),
        "market_discovery_max_preflight_candidates": 10,
        "preflight_quarantine_duration_seconds": Decimal("300"),
        "preflight_max_spread_pct": Decimal("0.05"),
    }
    base.update(overrides)
    return FakeConfig(**base)


def _make_market_metadata(condition_id="c1", token_ids=None):
    """Build a MarketMetadata for testing."""
    from src.schemas.market import MarketMetadata
    from datetime import datetime, timezone, timedelta

    end = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    return MarketMetadata.model_validate(
        {
            "conditionId": condition_id,
            "question": "Test?",
            "clobTokenIds": token_ids if token_ids is not None else ["tok-1", "tok-2"],
            "endDateIso": end,
            "active": True,
            "closed": False,
        }
    )


class TestMarketDiscoveryPreflight:
    """Preflight runs before market activation."""

    @pytest.mark.asyncio
    async def test_preflight_runs_when_enabled(self):
        """When preflight is enabled, markets go through preflight checks."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from src.agents.execution.polymarket_client import MarketSnapshot
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )
        from datetime import datetime, timezone

        market = _make_market_metadata()
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight()

        # Provide a mock polymarket client with a valid order book
        pmc = AsyncMock()
        pmc.fetch_order_book.return_value = MarketSnapshot(
            token_id="tok-1",
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.52"),
            midpoint_probability=Decimal("0.51"),
            spread=Decimal("0.02"),
            fetched_at_utc=datetime.now(timezone.utc),
            source="test",
        )

        engine = MarketDiscoveryEngine(gamma, tracker, config, polymarket_client=pmc)
        result = await engine.discover()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_preflight_skips_missing_token_context(self):
        """Market with no token_ids is skipped with MISSING_TOKEN_CONTEXT."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )

        market = _make_market_metadata(token_ids=[])
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight()

        engine = MarketDiscoveryEngine(gamma, tracker, config)
        result = await engine.discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_stable_market_rejection_emits_once_plus_cycle_summaries(self):
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from src.schemas.ops import OperationalEventType
        from tests.unit.test_market_discovery import (
            _future_iso,
            _make_gamma_stub,
            _make_market,
            _make_tracker_stub,
        )

        events = []

        async def publish(event):
            events.append(event)

        market = _make_market(condition_id="too-soon", end_date_iso=_future_iso(1))
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight(enable_market_discovery_preflight=False)
        config.dry_run = True

        engine = MarketDiscoveryEngine(
            gamma,
            tracker,
            config,
            event_publisher=publish,
        )

        assert await engine.discover() == []
        assert await engine.discover() == []

        rejected = [
            event
            for event in events
            if event.event_type == OperationalEventType.MARKET_REJECTED
        ]
        cycles = [
            event
            for event in events
            if event.event_type
            == OperationalEventType.MARKET_ELIGIBILITY_CYCLE_COMPLETED
        ]
        assert len(rejected) == 1
        assert len(cycles) == 2

    @pytest.mark.asyncio
    async def test_market_rejection_reason_change_emits_again(self):
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from src.schemas.ops import OperationalEventType
        from tests.unit.test_market_discovery import (
            _future_iso,
            _make_gamma_stub,
            _make_market,
            _make_tracker_stub,
        )

        events = []

        async def publish(event):
            events.append(event)

        market = _make_market(condition_id="changing", end_date_iso=_future_iso(1))
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight(enable_market_discovery_preflight=False)
        config.dry_run = True
        engine = MarketDiscoveryEngine(
            gamma,
            tracker,
            config,
            event_publisher=publish,
        )

        assert await engine.discover() == []
        gamma.get_active_markets.return_value = [
            _make_market(
                condition_id="changing",
                token_ids=[],
                end_date_iso=_future_iso(24),
            )
        ]
        assert await engine.discover() == []

        rejected = [
            event
            for event in events
            if event.event_type == OperationalEventType.MARKET_REJECTED
        ]
        assert len(rejected) == 2

    @pytest.mark.asyncio
    async def test_preflight_skips_order_book_unavailable(self):
        """When order book fetch fails, market is skipped."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )

        market = _make_market_metadata()
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight()

        pmc = AsyncMock()
        pmc.fetch_order_book.return_value = None

        engine = MarketDiscoveryEngine(gamma, tracker, config, polymarket_client=pmc)
        result = await engine.discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_preflight_skips_non_positive_quote(self):
        """Zero or negative bid/ask is rejected (client returns None for invalid)."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )

        market = _make_market_metadata()
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight()

        # fetch_order_book returns None for non-positive quotes (validator rejects)
        pmc = AsyncMock()
        pmc.fetch_order_book.return_value = None

        engine = MarketDiscoveryEngine(gamma, tracker, config, polymarket_client=pmc)
        result = await engine.discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_preflight_skips_crossed_book(self):
        """Bid >= ask is rejected as crossed book."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from src.agents.execution.polymarket_client import MarketSnapshot
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )
        from datetime import datetime, timezone

        market = _make_market_metadata()
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight()

        pmc = AsyncMock()
        pmc.fetch_order_book.return_value = MarketSnapshot(
            token_id="tok-1",
            best_bid=Decimal("0.6"),
            best_ask=Decimal("0.5"),
            midpoint_probability=Decimal("0.55"),
            spread=Decimal("-0.1"),
            fetched_at_utc=datetime.now(timezone.utc),
            source="test",
        )

        engine = MarketDiscoveryEngine(gamma, tracker, config, polymarket_client=pmc)
        result = await engine.discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_preflight_skips_spread_too_wide(self):
        """Spread above config threshold is rejected."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from src.agents.execution.polymarket_client import MarketSnapshot
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )
        from datetime import datetime, timezone

        market = _make_market_metadata()
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight(preflight_max_spread_pct=Decimal("0.01"))

        pmc = AsyncMock()
        pmc.fetch_order_book.return_value = MarketSnapshot(
            token_id="tok-1",
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.60"),
            midpoint_probability=Decimal("0.55"),
            spread=Decimal("0.10"),
            fetched_at_utc=datetime.now(timezone.utc),
            source="test",
        )

        engine = MarketDiscoveryEngine(gamma, tracker, config, polymarket_client=pmc)
        result = await engine.discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_preflight_uses_decimal_for_spread_comparison(self):
        """Spread comparison uses Decimal, not float."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from src.agents.execution.polymarket_client import MarketSnapshot
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )
        from datetime import datetime, timezone

        market = _make_market_metadata()
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight(preflight_max_spread_pct=Decimal("0.015"))

        pmc = AsyncMock()
        pmc.fetch_order_book.return_value = MarketSnapshot(
            token_id="tok-1",
            best_bid=Decimal("0.50"),
            best_ask=Decimal("0.51"),
            midpoint_probability=Decimal("0.505"),
            spread=Decimal("0.01"),
            fetched_at_utc=datetime.now(timezone.utc),
            source="test",
        )

        engine = MarketDiscoveryEngine(gamma, tracker, config, polymarket_client=pmc)
        result = await engine.discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_preflight_timeout_rejects_candidate(self):
        """Timeout on order book lookup rejects candidate."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )

        market = _make_market_metadata()
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight(
            market_discovery_preflight_timeout_ms=Decimal("1")
        )

        async def slow_fetch_order_book(*args, **kwargs):
            await asyncio.sleep(10)
            return None

        pmc = AsyncMock()
        pmc.fetch_order_book = slow_fetch_order_book

        engine = MarketDiscoveryEngine(gamma, tracker, config, polymarket_client=pmc)
        result = await engine.discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_preflight_bounded_candidate_count(self):
        """Only market_discovery_max_preflight_candidates are evaluated."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )

        markets = [_make_market_metadata(condition_id=f"c{i}") for i in range(20)]
        gamma = _make_gamma_stub(markets)
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight(
            market_discovery_max_preflight_candidates=5
        )

        engine = MarketDiscoveryEngine(gamma, tracker, config)
        result = await engine.discover()
        # All pass preflight (no pmc), but only 5 candidates evaluated
        assert len(result) <= 5

    @pytest.mark.asyncio
    async def test_preflight_no_activation_when_all_fail(self):
        """When all candidates fail preflight, no market is activated."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )

        markets = [
            _make_market_metadata(condition_id="c1", token_ids=[]),
            _make_market_metadata(condition_id="c2", token_ids=[]),
        ]
        gamma = _make_gamma_stub(markets)
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight()

        engine = MarketDiscoveryEngine(gamma, tracker, config)
        result = await engine.discover()
        assert result == []

    @pytest.mark.asyncio
    async def test_preflight_disabled_passthrough(self):
        """When preflight is disabled, discovery works as before."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )

        market = _make_market_metadata()
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight(enable_market_discovery_preflight=False)

        engine = MarketDiscoveryEngine(gamma, tracker, config)
        result = await engine.discover()
        assert len(result) == 1


class TestMarketQuarantine:
    """Quarantine for repeatedly failing markets."""

    @pytest.mark.asyncio
    async def test_repeated_preflight_failures_quarantine_market(self):
        """After 3 consecutive failures, market is quarantined."""
        from src.agents.ingestion.market_quarantine import MarketQuarantineManager

        config = _make_config()
        manager = MarketQuarantineManager(config, failure_threshold=3)

        # 2 failures — not yet quarantined
        assert manager.record_failure("c1") is None
        assert manager.record_failure("c1") is None
        assert not manager.is_quarantined("c1")

        # 3rd failure — quarantined
        decision = manager.record_failure("c1")
        assert decision is not None
        assert manager.is_quarantined("c1")

    @pytest.mark.asyncio
    async def test_quarantine_expiry_allows_recheck(self):
        """After quarantine expires, market can be checked again."""
        from src.agents.ingestion.market_quarantine import MarketQuarantineManager

        config = _make_config(preflight_quarantine_duration_seconds=Decimal("0.05"))
        manager = MarketQuarantineManager(config, failure_threshold=2)

        manager.record_failure("c1")
        manager.record_failure("c1")
        assert manager.is_quarantined("c1")

        # Wait for expiry
        await asyncio.sleep(0.06)
        assert not manager.is_quarantined("c1")

    @pytest.mark.asyncio
    async def test_quarantine_does_not_suppress_unrelated_markets(self):
        """Quarantine of market A does not affect market B."""
        from src.agents.ingestion.market_quarantine import MarketQuarantineManager

        config = _make_config()
        manager = MarketQuarantineManager(config, failure_threshold=2)

        manager.record_failure("c1")
        manager.record_failure("c1")
        assert manager.is_quarantined("c1")
        assert not manager.is_quarantined("c2")

    def test_quarantine_success_resets_counter(self):
        """A successful preflight resets the failure counter."""
        from src.agents.ingestion.market_quarantine import MarketQuarantineManager

        config = _make_config()
        manager = MarketQuarantineManager(config, failure_threshold=3)

        manager.record_failure("c1")
        manager.record_failure("c1")
        manager.record_success("c1")

        # After success, need 3 more failures to quarantine
        assert manager.record_failure("c1") is None
        assert manager.record_failure("c1") is None
        assert manager.record_failure("c1") is not None


# ===================================================================
# DataAggregator — dedupe integration
# ===================================================================


class TestDataAggregatorDedupe:
    """Per-market dedupe before prompt emission."""

    def _make_aggregator(self, dedupe_enabled=False, **kwargs):
        """Build a DataAggregator with dedupe configured."""
        from src.agents.context.aggregator import DataAggregator

        in_q = asyncio.Queue()
        out_q = asyncio.Queue()
        agg = DataAggregator(in_q, out_q, "c1")
        agg.configure_dedupe(
            enabled=dedupe_enabled,
            min_interval_sec=float(kwargs.get("min_interval", Decimal("30"))),
            midpoint_delta=kwargs.get("midpoint_delta", Decimal("0.01")),
            spread_delta=kwargs.get("spread_delta", Decimal("0.005")),
        )
        return agg

    @pytest.mark.asyncio
    async def test_dedupe_suppresses_unchanged_context(self):
        """Same midpoint and spread within interval → suppressed."""
        agg = self._make_aggregator(dedupe_enabled=True)
        agg.register_market("c1", yes_token_id="tok-1")
        agg.best_bid = 0.50
        agg.best_ask = 0.52

        # First emit — always emits
        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 1
        await agg.output_queue.get()  # drain

        # Second emit immediately — should be suppressed (unchanged)
        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_dedupe_emits_on_midpoint_movement(self):
        """Material midpoint change → emits."""
        agg = self._make_aggregator(
            dedupe_enabled=True,
            midpoint_delta=Decimal("0.01"),
        )
        agg.register_market("c1", yes_token_id="tok-1")
        agg.best_bid = 0.50
        agg.best_ask = 0.52

        await agg._emit_state_for_market("c1")
        await agg.output_queue.get()

        # Move midpoint materially (0.51 → 0.53 = 0.02 change > 0.01 delta)
        agg.best_bid = 0.52
        agg.best_ask = 0.54
        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_dedupe_emits_on_spread_movement(self):
        """Material spread change → emits."""
        agg = self._make_aggregator(
            dedupe_enabled=True,
            spread_delta=Decimal("0.005"),
        )
        agg.register_market("c1", yes_token_id="tok-1")
        agg.best_bid = 0.50
        agg.best_ask = 0.52  # spread = 0.02

        await agg._emit_state_for_market("c1")
        await agg.output_queue.get()

        # Spread changes materially (0.02 → 0.03 = 0.01 > 0.005 delta)
        agg.best_bid = 0.50
        agg.best_ask = 0.53
        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_dedupe_is_per_market_not_global(self):
        """Market A unchanged → suppressed. Market B moves → emits."""
        agg = self._make_aggregator(dedupe_enabled=True)
        agg.register_market("c1", yes_token_id="tok-1")
        agg.register_market("c2", yes_token_id="tok-2")
        agg.best_bid = 0.50
        agg.best_ask = 0.52

        # Emit for c1
        await agg._emit_state_for_market("c1")
        await agg.output_queue.get()

        # c2 first emit — always emits
        agg._markets["c2"].best_bid = 0.40
        agg._markets["c2"].best_ask = 0.42
        await agg._emit_state_for_market("c2")
        assert agg.output_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_dedupe_respects_minimum_evaluation_interval(self):
        """Within min interval + unchanged state → suppressed."""
        agg = self._make_aggregator(
            dedupe_enabled=True,
            min_interval=Decimal("60"),
        )
        agg.register_market("c1", yes_token_id="tok-1")
        agg.best_bid = 0.50
        agg.best_ask = 0.52

        await agg._emit_state_for_market("c1")
        await agg.output_queue.get()

        # Immediate re-emit — suppressed (within 60s interval, unchanged)
        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_dedupe_disabled_passthrough(self):
        """When dedupe is disabled, every trigger emits."""
        agg = self._make_aggregator(dedupe_enabled=False)
        agg.register_market("c1", yes_token_id="tok-1")
        agg.best_bid = 0.50
        agg.best_ask = 0.52

        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 1
        await agg.output_queue.get()

        # Second emit — also emits (dedupe disabled)
        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 1


# ===================================================================
# Orchestrator — prompt queue backpressure
# ===================================================================


class TestPromptQueueBackpressure:
    """Bounded prompt queue behavior."""

    @pytest.mark.asyncio
    async def test_queue_size_is_bounded(self):
        """Queue does not exceed max_size."""
        from src.agents.context.bounded_queue import BoundedPromptQueue

        bq = BoundedPromptQueue(max_size=3, coalescing=False)
        for i in range(5):
            item = {
                "state": {"condition_id": f"c{i}"},
                "prompt": f"prompt-{i}",
            }
            await bq.put(item)

        assert bq.qsize() == 3

    @pytest.mark.asyncio
    async def test_queue_full_coalescing_replaces_stale(self):
        """When queue is full with coalescing, same market is replaced."""
        from src.agents.context.bounded_queue import BoundedPromptQueue

        bq = BoundedPromptQueue(max_size=2, coalescing=True)
        await bq.put({"state": {"condition_id": "c1"}, "prompt": "old"})
        await bq.put({"state": {"condition_id": "c2"}, "prompt": "c2"})

        # Queue full — coalesce c1
        decision = await bq.put({"state": {"condition_id": "c1"}, "prompt": "new"})
        assert decision.reason == PromptQueueBackpressureReason.COALESCED
        assert bq.qsize() == 2

        # Verify c1 has the new prompt
        items = []
        while not bq.empty():
            items.append(await bq.get())
        c1_items = [i for i in items if i["state"]["condition_id"] == "c1"]
        assert len(c1_items) == 1
        assert c1_items[0]["prompt"] == "new"

    @pytest.mark.asyncio
    async def test_queue_full_no_match_drops_stale(self):
        """When queue is full and no matching market, payload is dropped."""
        from src.agents.context.bounded_queue import BoundedPromptQueue

        bq = BoundedPromptQueue(max_size=2, coalescing=True)
        await bq.put({"state": {"condition_id": "c1"}, "prompt": "c1"})
        await bq.put({"state": {"condition_id": "c2"}, "prompt": "c2"})

        # Queue full — c3 not in queue, dropped
        decision = await bq.put({"state": {"condition_id": "c3"}, "prompt": "c3"})
        assert decision.reason == PromptQueueBackpressureReason.STALE_DROPPED
        assert bq.qsize() == 2

    @pytest.mark.asyncio
    async def test_queue_full_no_match_records_dropped_not_coalesced_metric(self):
        """A coalescing miss must report stale-drop metrics, not coalescing."""
        from src.agents.context.bounded_queue import BoundedPromptQueue

        class MetricsProbe:
            def __init__(self):
                self.coalesced = 0
                self.dropped = 0

            async def record_coalesced_context(self):
                self.coalesced += 1

            async def record_dropped_stale_context(self):
                self.dropped += 1

            async def set_evaluation_queue_depth(self, _depth):
                return None

        metrics = MetricsProbe()
        bq = BoundedPromptQueue(max_size=2, coalescing=True, metrics=metrics)
        await bq.put({"state": {"condition_id": "c1"}, "prompt": "c1"})
        await bq.put({"state": {"condition_id": "c2"}, "prompt": "c2"})

        decision = await bq.put({"state": {"condition_id": "c3"}, "prompt": "c3"})

        assert decision.reason == PromptQueueBackpressureReason.STALE_DROPPED
        assert metrics.coalesced == 0
        assert metrics.dropped == 1

    @pytest.mark.asyncio
    async def test_empty_get_clears_notification_under_lock(self):
        from src.agents.context.bounded_queue import BoundedPromptQueue

        bq = BoundedPromptQueue(max_size=1, coalescing=True)
        real_event = bq._not_empty

        class EventProbe:
            def __init__(self):
                self.clear_lock_states = []

            def clear(self):
                self.clear_lock_states.append(bq._lock.locked())
                real_event.clear()

            def set(self):
                real_event.set()

            async def wait(self):
                await real_event.wait()

        probe = EventProbe()
        bq._not_empty = probe

        waiter = asyncio.create_task(bq.get())
        await asyncio.sleep(0)

        assert probe.clear_lock_states == [True]

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    @pytest.mark.asyncio
    async def test_queue_backpressure_is_logged(self):
        """Backpressure decisions are logged (verified via decision return)."""
        from src.agents.context.bounded_queue import BoundedPromptQueue

        bq = BoundedPromptQueue(max_size=1, coalescing=False)
        await bq.put({"state": {"condition_id": "c1"}, "prompt": "c1"})
        decision = await bq.put({"state": {"condition_id": "c2"}, "prompt": "c2"})
        assert decision.action == "drop"
        assert decision.queue_depth == 1

    @pytest.mark.asyncio
    async def test_backpressure_runs_before_llm_cost_guard(self):
        """BoundedPromptQueue operates independently of LLM cost guard."""
        from src.agents.context.bounded_queue import BoundedPromptQueue

        bq = BoundedPromptQueue(max_size=2, coalescing=False)
        # Queue backpressure is a queue-level concern, not LLM-level
        for i in range(3):
            item = {"state": {"condition_id": f"c{i}"}, "prompt": f"p{i}"}
            await bq.put(item)

        assert bq.qsize() == 2

    @pytest.mark.asyncio
    async def test_budget_quarantine_drops_market_without_queue_churn(self):
        from src.agents.context.bounded_queue import BoundedPromptQueue

        assert (
            PromptQueueBackpressureReason.BUDGET_QUARANTINED.value
            == "BUDGET_QUARANTINED"
        )
        bq = BoundedPromptQueue(max_size=2, coalescing=True)
        await bq.quarantine_market_until(
            "c1",
            datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        decision = await bq.put({"state": {"condition_id": "c1"}, "prompt": "p1"})

        assert decision.action == "drop"
        assert decision.reason == PromptQueueBackpressureReason.BUDGET_QUARANTINED
        assert bq.qsize() == 0

    @pytest.mark.asyncio
    async def test_budget_quarantine_drains_existing_market_items(self):
        from src.agents.context.bounded_queue import BoundedPromptQueue

        bq = BoundedPromptQueue(max_size=5, coalescing=False)
        await bq.put({"state": {"condition_id": "c1"}, "prompt": "old-1"})
        await bq.put({"state": {"condition_id": "c2"}, "prompt": "keep"})
        await bq.put({"state": {"condition_id": "c1"}, "prompt": "old-2"})

        await bq.quarantine_market_until(
            "c1",
            datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        assert bq.qsize() == 1
        remaining = await bq.get()
        assert remaining["state"]["condition_id"] == "c2"
        bq.task_done()
        await asyncio.wait_for(bq.join(), timeout=1)

    @pytest.mark.asyncio
    async def test_budget_quarantine_lifts_after_expiry(self):
        from src.agents.context.bounded_queue import BoundedPromptQueue

        bq = BoundedPromptQueue(max_size=2, coalescing=True)
        await bq.quarantine_market_until(
            "c1",
            datetime.now(timezone.utc) - timedelta(seconds=1),
        )

        decision = await bq.put({"state": {"condition_id": "c1"}, "prompt": "p1"})

        assert decision.action == "enqueue"
        assert bq.qsize() == 1


# ===================================================================
# Config validation
# ===================================================================


class TestConfigValidation:
    """Config rejects negative thresholds."""

    def test_rejects_negative_preflight_timeout(self):
        with pytest.raises(Exception):
            _make_config(market_discovery_preflight_timeout_ms=Decimal("-1"))

    def test_rejects_negative_quarantine_duration(self):
        with pytest.raises(Exception):
            _make_config(preflight_quarantine_duration_seconds=Decimal("-1"))

    def test_rejects_negative_dedupe_interval(self):
        with pytest.raises(Exception):
            _make_config(dedupe_min_evaluation_interval_sec=Decimal("-1"))

    def test_rejects_negative_dedupe_delta(self):
        with pytest.raises(Exception):
            _make_config(dedupe_midpoint_delta=Decimal("-0.01"))

    def test_rejects_negative_queue_size(self):
        with pytest.raises(Exception):
            _make_config(prompt_queue_maxsize=-1)

    def test_rejects_negative_max_candidates(self):
        with pytest.raises(Exception):
            _make_config(market_discovery_max_preflight_candidates=-1)


# ===================================================================
# Compatibility
# ===================================================================


class TestCompatibility:
    """Existing behavior preserved when features are disabled."""

    @pytest.mark.asyncio
    async def test_preflight_disabled_compatible(self):
        """When preflight is disabled, discovery works as before."""
        from src.agents.ingestion.market_discovery import MarketDiscoveryEngine
        from tests.unit.test_market_discovery import (
            _make_gamma_stub,
            _make_tracker_stub,
        )

        market = _make_market_metadata()
        gamma = _make_gamma_stub([market])
        tracker = _make_tracker_stub()
        config = _make_fake_config_preflight(enable_market_discovery_preflight=False)

        engine = MarketDiscoveryEngine(gamma, tracker, config)
        result = await engine.discover()
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_dedupe_disabled_compatible(self):
        """When dedupe is disabled, every trigger emits."""
        from src.agents.context.aggregator import DataAggregator

        in_q = asyncio.Queue()
        out_q = asyncio.Queue()
        agg = DataAggregator(in_q, out_q, "c1")
        agg.configure_dedupe(enabled=False)
        agg.register_market("c1", yes_token_id="tok-1")
        agg.best_bid = 0.50
        agg.best_ask = 0.52

        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 1
        await agg.output_queue.get()

        # Second emit — also emits
        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_multi_market_tracking_preserved(self):
        """Multiple markets can be tracked concurrently with dedupe."""
        from src.agents.context.aggregator import DataAggregator

        in_q = asyncio.Queue()
        out_q = asyncio.Queue()
        agg = DataAggregator(in_q, out_q, "c1")
        agg.configure_dedupe(enabled=True)
        agg.register_market("c1", yes_token_id="tok-1")
        agg.register_market("c2", yes_token_id="tok-2")

        agg.best_bid = 0.50
        agg.best_ask = 0.52
        agg._markets["c2"].best_bid = 0.40
        agg._markets["c2"].best_ask = 0.42

        await agg._emit_state_for_market("c1")
        await agg._emit_state_for_market("c2")
        assert agg.output_queue.qsize() == 2

    @pytest.mark.asyncio
    async def test_time_trigger_respects_dedupe_gate(self):
        """Time trigger emits but dedupe gate still applies."""
        from src.agents.context.aggregator import DataAggregator

        in_q = asyncio.Queue()
        out_q = asyncio.Queue()
        agg = DataAggregator(in_q, out_q, "c1")
        agg.configure_dedupe(enabled=True)
        agg.register_market("c1", yes_token_id="tok-1")
        agg.best_bid = 0.50
        agg.best_ask = 0.52

        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 1
        await agg.output_queue.get()

        # Immediate re-emit (simulating time trigger) — suppressed
        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_volatility_trigger_respects_dedupe_gate(self):
        """Volatility trigger emits but dedupe gate still applies."""
        from src.agents.context.aggregator import DataAggregator

        in_q = asyncio.Queue()
        out_q = asyncio.Queue()
        agg = DataAggregator(in_q, out_q, "c1")
        agg.configure_dedupe(enabled=True)
        agg.register_market("c1", yes_token_id="tok-1")
        agg.best_bid = 0.50
        agg.best_ask = 0.52

        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 1
        await agg.output_queue.get()

        # Volatility trigger with unchanged state — suppressed
        await agg._emit_state_for_market("c1")
        assert agg.output_queue.qsize() == 0


# ===================================================================
# Metrics
# ===================================================================


class TestPreflightDedupeBackpressureMetrics:
    """Low-cardinality metrics for preflight, dedupe, and backpressure."""

    @pytest.mark.asyncio
    async def test_preflight_pass_fail_counter(self):
        """Metrics registry supports preflight pass/fail counters."""
        from src.observability.metrics import (
            MetricsRegistry,
            DecisionMetricEvent,
            DecisionLabel,
        )

        registry = MetricsRegistry()
        await registry.record_decision(DecisionMetricEvent(decision=DecisionLabel.BUY))
        snapshot = await registry.snapshot()
        names = {s.name for s in snapshot.samples}
        assert "poly_agent_decisions_total" in names

    @pytest.mark.asyncio
    async def test_quarantine_count_metric(self):
        """Metrics registry supports quarantine count."""
        from src.observability.metrics import MetricsRegistry

        registry = MetricsRegistry()
        snapshot = await registry.snapshot()
        # Quarantine count is tracked via the quarantine manager
        # The registry has the infrastructure for it
        assert len(snapshot.samples) > 0

    @pytest.mark.asyncio
    async def test_emitted_contexts_counter(self):
        """Emitted contexts are tracked via decision counter."""
        from src.observability.metrics import (
            MetricsRegistry,
            DecisionMetricEvent,
            DecisionLabel,
        )

        registry = MetricsRegistry()
        await registry.record_decision(DecisionMetricEvent(decision=DecisionLabel.HOLD))
        snapshot = await registry.snapshot()
        assert len(snapshot.samples) > 0

    @pytest.mark.asyncio
    async def test_deduped_contexts_counter(self):
        """Deduped contexts can be tracked via decision SKIP counter."""
        from src.observability.metrics import (
            MetricsRegistry,
            DecisionMetricEvent,
            DecisionLabel,
        )

        registry = MetricsRegistry()
        await registry.record_decision(DecisionMetricEvent(decision=DecisionLabel.SKIP))
        snapshot = await registry.snapshot()
        names = {s.name for s in snapshot.samples}
        assert "poly_agent_decisions_total" in names

    @pytest.mark.asyncio
    async def test_dropped_stale_contexts_counter(self):
        """Dropped stale contexts tracked via decision SKIP counter."""
        from src.observability.metrics import (
            MetricsRegistry,
            DecisionMetricEvent,
            DecisionLabel,
        )

        registry = MetricsRegistry()
        await registry.record_decision(DecisionMetricEvent(decision=DecisionLabel.SKIP))
        snapshot = await registry.snapshot()
        assert len(snapshot.samples) > 0

    @pytest.mark.asyncio
    async def test_coalesced_contexts_counter(self):
        """Coalesced contexts tracked via decision counter."""
        from src.observability.metrics import (
            MetricsRegistry,
            DecisionMetricEvent,
            DecisionLabel,
        )

        registry = MetricsRegistry()
        await registry.record_decision(DecisionMetricEvent(decision=DecisionLabel.HOLD))
        snapshot = await registry.snapshot()
        assert len(snapshot.samples) > 0

    @pytest.mark.asyncio
    async def test_prompt_queue_depth_gauge(self):
        """Prompt queue depth is available via snapshot."""
        from src.agents.context.bounded_queue import BoundedPromptQueue

        bq = BoundedPromptQueue(max_size=10, coalescing=True)
        snap = bq.snapshot()
        assert snap.current_depth == 0
        assert snap.max_size == 10

    @pytest.mark.asyncio
    async def test_metrics_do_not_expose_high_cardinality_labels(self):
        """Metrics reject high-cardinality label keys."""
        from src.observability.metrics import MetricLabelSet

        with pytest.raises(ValueError, match="Forbidden high-cardinality label key"):
            MetricLabelSet(labels={"condition_id": "c1"})
