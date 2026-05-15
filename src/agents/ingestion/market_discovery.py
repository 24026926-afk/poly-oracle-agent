"""
src/agents/ingestion/market_discovery.py

Autonomous market discovery using the Gamma REST API.

Fetches active markets, applies eligibility filters (metadata presence,
time-to-resolution, exposure limits), and returns market metadata ordered
by priority.  All monetary comparisons use ``Decimal``.

WI-53: Adds read-only preflight checks before market activation, including
order book validation, spread checks, and quarantine for repeated failures.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from src.agents.execution.bankroll_tracker import BankrollPortfolioTracker
from src.agents.ingestion.rest_client import GammaRESTClient
from src.agents.ingestion.market_quarantine import MarketQuarantineManager
from src.core.config import AppConfig
from src.schemas.market import MarketMetadata
from src.schemas.market_eligibility import (
    MarketEligibilityPreflightResult,
    MarketEligibilityStatus,
    MarketEligibilitySkipReason,
)
from src.schemas.ops import (
    OperationalEventCreate,
    OperationalEventPayload,
    OperationalEventReasonCode,
    OperationalEventSeverity,
    OperationalEventSource,
    OperationalEventType,
)

logger = structlog.get_logger(__name__)


class MarketDiscoveryEngine:
    """Selects eligible markets from the Gamma API for pipeline consumption.

    Filters applied in order:
        1. Required metadata (condition_id + token_ids present)
        2. Time-to-resolution >= ``config.min_ttr_hours``
        3. Current exposure < ``config.max_exposure_pct`` × bankroll
        4. WI-53: Preflight checks (order book, spread, quarantine)
    """

    def __init__(
        self,
        gamma_client: GammaRESTClient,
        bankroll_tracker: BankrollPortfolioTracker,
        config: AppConfig,
        polymarket_client=None,  # Optional, for preflight order book lookups
        metrics=None,  # Optional MetricsRegistry for WI-53 metrics
        event_publisher: Callable[[OperationalEventCreate], Awaitable[Any]] | None = None,
    ) -> None:
        self._gamma_client = gamma_client
        self._bankroll_tracker = bankroll_tracker
        self._config = config
        self._polymarket_client = polymarket_client
        self._quarantine_manager = MarketQuarantineManager(config)
        self._metrics = metrics
        self._event_publisher = event_publisher

    async def _publish_event(
        self,
        *,
        event_type: OperationalEventType,
        severity: OperationalEventSeverity,
        reason_code: OperationalEventReasonCode,
        message: str,
    ) -> None:
        if self._event_publisher is None:
            return
        try:
            payload = OperationalEventPayload(
                message=message,
                reason_code=reason_code.value,
                dry_run=self._config.dry_run,
            )
            await self._event_publisher(
                OperationalEventCreate(
                    event_type=event_type,
                    severity=severity,
                    source=OperationalEventSource.INGESTION,
                    reason_code=reason_code,
                    payload=payload,
                )
            )
        except Exception:
            logger.debug("market_discovery.operational_event_skipped")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def discover(self) -> list[MarketMetadata]:
        """Return eligible markets, best candidates first.

        Returns an empty list (with a warning log) when no market passes
        all filters.  Never falls back to a hardcoded condition_id.

        WI-53: Preflight uses bounded concurrency so slow candidates
        cannot stall the discovery loop.
        """
        markets = await self._gamma_client.get_active_markets()
        if not markets:
            logger.warning("market_discovery.no_markets_from_gamma")
            return []

        bankroll = await self._bankroll_tracker.get_total_bankroll()
        exposure_cap = Decimal(str(self._config.max_exposure_pct)) * bankroll

        # WI-53: Bound candidate count only when preflight is enabled
        if self._config.enable_market_discovery_preflight:
            max_candidates = self._config.market_discovery_max_preflight_candidates
            candidates = markets[:max_candidates]
        else:
            candidates = markets

        # Run cheap filters sequentially
        pre_preflight: list[MarketMetadata] = []
        stats = {
            "total": len(markets),
            "no_metadata": 0,
            "ttr_fail": 0,
            "exposure_fail": 0,
            "preflight_skip": 0,
            "quarantine_skip": 0,
        }

        for market in candidates:
            if not self._has_required_metadata(market):
                stats["no_metadata"] += 1
                await self._publish_event(
                    event_type=OperationalEventType.MARKET_REJECTED,
                    severity=OperationalEventSeverity.WARNING,
                    reason_code=OperationalEventReasonCode.MARKET_INELIGIBLE,
                    message="Market rejected because required metadata was missing",
                )
                continue

            if not self._meets_ttr_requirement(market):
                stats["ttr_fail"] += 1
                await self._publish_event(
                    event_type=OperationalEventType.MARKET_REJECTED,
                    severity=OperationalEventSeverity.INFO,
                    reason_code=OperationalEventReasonCode.MARKET_INELIGIBLE,
                    message="Market rejected because time-to-resolution was below threshold",
                )
                continue

            exposure = await self._bankroll_tracker.get_exposure(market.condition_id)
            if exposure >= exposure_cap:
                stats["exposure_fail"] += 1
                logger.debug(
                    "market_discovery.exposure_limit_reached",
                    condition_id=market.condition_id,
                    exposure_usdc=str(exposure),
                    cap_usdc=str(exposure_cap),
                )
                await self._publish_event(
                    event_type=OperationalEventType.MARKET_REJECTED,
                    severity=OperationalEventSeverity.WARNING,
                    reason_code=OperationalEventReasonCode.DECISION_SKIP_EXPOSURE,
                    message="Market rejected because exposure cap was reached",
                )
                continue

            pre_preflight.append(market)

        # WI-53: Bounded concurrency for preflight
        if self._config.enable_market_discovery_preflight:
            eligible = await self._run_preflight_concurrent(pre_preflight, stats)
        else:
            eligible = pre_preflight

        if not eligible:
            logger.warning(
                "market_discovery.no_eligible_markets",
                **stats,
            )
        else:
            logger.info(
                "market_discovery.eligible_markets_found",
                eligible_count=len(eligible),
                **stats,
            )

        return eligible

    async def _run_preflight_concurrent(
        self,
        candidates: list[MarketMetadata],
        stats: dict,
    ) -> list[MarketMetadata]:
        """Run preflight with bounded concurrency.

        Uses a semaphore to limit concurrent order-book lookups so a
        pathological market cannot stall the entire discovery loop.
        """
        # Bounded concurrency: at most 3 concurrent preflight calls
        semaphore = asyncio.Semaphore(3)
        timeout = float(self._config.market_discovery_preflight_timeout_ms) / 1000.0

        async def _preflight_one(market: MarketMetadata):
            async with semaphore:
                # Check quarantine first
                if self._quarantine_manager.is_quarantined(market.condition_id):
                    stats["quarantine_skip"] += 1
                    await self._publish_event(
                        event_type=OperationalEventType.MARKET_QUARANTINE,
                        severity=OperationalEventSeverity.WARNING,
                        reason_code=OperationalEventReasonCode.MARKET_QUARANTINED,
                        message="Market skipped because quarantine is active",
                    )
                    return None

                result = await asyncio.wait_for(
                    self._run_preflight(market),
                    timeout=timeout,
                )
                return (market, result)

        results = await asyncio.gather(
            *[_preflight_one(m) for m in candidates],
            return_exceptions=True,
        )

        eligible: list[MarketMetadata] = []
        for result in results:
            if isinstance(result, Exception):
                stats["preflight_skip"] += 1
                if self._metrics is not None:
                    await self._metrics.record_preflight_fail(reason="TIMEOUT")
                continue
            if result is None:
                continue
            market, preflight_result = result
            if preflight_result.status != MarketEligibilityStatus.ELIGIBLE:
                stats["preflight_skip"] += 1
                decision = self._quarantine_manager.record_failure(market.condition_id)
                if self._metrics is not None:
                    reason = preflight_result.skip_reason.value if preflight_result.skip_reason else "UNKNOWN"
                    await self._metrics.record_preflight_fail(reason=reason)
                    if decision is not None:
                        await self._metrics.record_quarantine(reason=decision.reason.value)
                logger.info(
                    "market_discovery.preflight_failed",
                    skip_reason=preflight_result.skip_reason.value if preflight_result.skip_reason else None,
                )
                await self._publish_event(
                    event_type=OperationalEventType.MARKET_REJECTED,
                    severity=OperationalEventSeverity.WARNING,
                    reason_code=OperationalEventReasonCode.MARKET_INELIGIBLE,
                    message="Market rejected by bounded preflight checks",
                )
                if decision is not None:
                    await self._publish_event(
                        event_type=OperationalEventType.MARKET_QUARANTINE,
                        severity=OperationalEventSeverity.WARNING,
                        reason_code=OperationalEventReasonCode.MARKET_QUARANTINED,
                        message="Market placed into quarantine after repeated preflight failures",
                    )
            else:
                self._quarantine_manager.record_success(market.condition_id)
                if self._metrics is not None:
                    await self._metrics.record_preflight_pass()
                eligible.append(market)

        return eligible

    # ------------------------------------------------------------------
    # WI-53: Preflight checks
    # ------------------------------------------------------------------

    async def _run_preflight(self, market: MarketMetadata) -> MarketEligibilityPreflightResult:
        """Run read-only preflight checks on a market candidate.

        Validates:
        - Token context presence
        - Order book availability
        - Non-positive quotes
        - Crossed books
        - Spread threshold
        """
        condition_id = market.condition_id

        # Check token context
        if not market.token_ids or len(market.token_ids) == 0:
            return MarketEligibilityPreflightResult(
                condition_id=condition_id,
                status=MarketEligibilityStatus.SKIPPED,
                skip_reason=MarketEligibilitySkipReason.MISSING_TOKEN_CONTEXT,
            )

        # Get order book data
        if self._polymarket_client is None:
            # Fail closed: preflight enabled but no market data client
            return MarketEligibilityPreflightResult(
                condition_id=condition_id,
                status=MarketEligibilityStatus.SKIPPED,
                skip_reason=MarketEligibilitySkipReason.ORDER_BOOK_UNAVAILABLE,
            )

        try:
            timeout = float(self._config.market_discovery_preflight_timeout_ms) / 1000.0
            best_bid, best_ask = await asyncio.wait_for(
                self._get_order_book_quotes(market),
                timeout=timeout,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            reason = (
                MarketEligibilitySkipReason.PREFLIGHT_TIMEOUT
                if isinstance(exc, asyncio.TimeoutError)
                else MarketEligibilitySkipReason.ORDER_BOOK_UNAVAILABLE
            )
            logger.warning(
                "preflight.order_book_error",
                reason=reason.value,
            )
            return MarketEligibilityPreflightResult(
                condition_id=condition_id,
                status=MarketEligibilityStatus.SKIPPED,
                skip_reason=reason,
            )

        if best_bid is None or best_ask is None:
            return MarketEligibilityPreflightResult(
                condition_id=condition_id,
                status=MarketEligibilityStatus.SKIPPED,
                skip_reason=MarketEligibilitySkipReason.ORDER_BOOK_UNAVAILABLE,
            )

        # best_bid/best_ask are already Decimal from the I/O boundary
        bid: Decimal = best_bid
        ask: Decimal = best_ask
        midpoint = (bid + ask) / Decimal("2")
        spread = ask - bid

        # Non-positive quote check
        if bid <= 0 or ask <= 0:
            return MarketEligibilityPreflightResult(
                condition_id=condition_id,
                status=MarketEligibilityStatus.SKIPPED,
                skip_reason=MarketEligibilitySkipReason.NON_POSITIVE_QUOTE,
                midpoint=midpoint,
                spread=spread,
            )

        # Crossed book check (bid >= ask)
        if bid >= ask:
            return MarketEligibilityPreflightResult(
                condition_id=condition_id,
                status=MarketEligibilityStatus.SKIPPED,
                skip_reason=MarketEligibilitySkipReason.CROSSED_BOOK,
                midpoint=midpoint,
                spread=spread,
            )

        # Spread threshold check
        max_spread = Decimal(str(self._config.preflight_max_spread_pct))
        if ask > 0 and (spread / ask) > max_spread:
            return MarketEligibilityPreflightResult(
                condition_id=condition_id,
                status=MarketEligibilityStatus.SKIPPED,
                skip_reason=MarketEligibilitySkipReason.SPREAD_TOO_WIDE,
                midpoint=midpoint,
                spread=spread,
            )

        return MarketEligibilityPreflightResult(
            condition_id=condition_id,
            status=MarketEligibilityStatus.ELIGIBLE,
            midpoint=midpoint,
            spread=spread,
        )

    async def _get_order_book_quotes(self, market: MarketMetadata):
        """Fetch best bid and ask from the order book.

        Returns (best_bid, best_ask) as Decimal, or (None, None) on failure.
        No float is used in the financial path.
        """
        yes_token_id = market.token_ids[0] if market.token_ids else None
        if yes_token_id is None:
            return None, None

        snapshot = await self._polymarket_client.fetch_order_book(yes_token_id)
        if snapshot is None:
            return None, None

        return snapshot.best_bid, snapshot.best_ask

    # ------------------------------------------------------------------
    # Filter helpers
    # ------------------------------------------------------------------

    def _has_required_metadata(self, market: MarketMetadata) -> bool:
        """True when condition_id is present and token_ids is non-empty."""
        return bool(market.condition_id) and len(market.token_ids) > 0

    def _compute_hours_to_resolution(self, end_date_iso: str | None) -> float | None:
        """Parse ISO-8601 *end_date_iso* and return hours until resolution.

        Returns ``None`` when the string is missing or unparseable.
        """
        if end_date_iso is None:
            return None
        try:
            end_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return (end_dt - now).total_seconds() / 3600.0
        except (ValueError, TypeError):
            return None

    def _meets_ttr_requirement(self, market: MarketMetadata) -> bool:
        """True when hours-to-resolution >= ``min_ttr_hours``.

        Markets with no parseable end date are excluded.
        """
        hours = self._compute_hours_to_resolution(market.end_date_iso)
        if hours is None:
            return False
        return hours >= self._config.min_ttr_hours

    @property
    def quarantine_manager(self) -> MarketQuarantineManager:
        """Expose the quarantine manager for testing."""
        return self._quarantine_manager
