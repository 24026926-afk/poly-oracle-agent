"""
src/agents/ingestion/market_discovery.py

Autonomous market discovery using the Gamma REST API.

Fetches active markets, applies eligibility filters (metadata presence,
time-to-resolution, exposure limits), and returns condition_ids ordered
by priority.  All monetary comparisons use ``Decimal``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog

from src.agents.execution.bankroll_tracker import BankrollPortfolioTracker
from src.agents.ingestion.rest_client import GammaRESTClient
from src.core.config import AppConfig
from src.schemas.market import MarketMetadata

logger = structlog.get_logger(__name__)


class MarketDiscoveryEngine:
    """Selects eligible markets from the Gamma API for pipeline consumption.

    Filters applied in order:
        1. Required metadata (condition_id + token_ids present)
        2. Time-to-resolution >= ``config.min_ttr_hours``
        3. Current exposure < ``config.max_exposure_pct`` × bankroll
    """

    def __init__(
        self,
        gamma_client: GammaRESTClient,
        bankroll_tracker: BankrollPortfolioTracker,
        config: AppConfig,
    ) -> None:
        self._gamma_client = gamma_client
        self._bankroll_tracker = bankroll_tracker
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def discover(self) -> list[str]:
        """Return eligible condition_ids, best candidates first.

        Returns an empty list (with a warning log) when no market passes
        all filters.  Never falls back to a hardcoded condition_id.
        """
        markets = await self._gamma_client.get_active_markets()
        if not markets:
            logger.warning("market_discovery.no_markets_from_gamma")
            return []

        bankroll = await self._bankroll_tracker.get_total_bankroll()
        exposure_cap = Decimal(str(self._config.max_exposure_pct)) * bankroll

        eligible: list[str] = []
        stats = {
            "total": len(markets),
            "no_metadata": 0,
            "ttr_fail": 0,
            "exposure_fail": 0,
        }

        for market in markets:
            if not self._has_required_metadata(market):
                stats["no_metadata"] += 1
                continue

            if not self._meets_ttr_requirement(market):
                stats["ttr_fail"] += 1
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
                continue

            eligible.append(market.condition_id)

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
