"""
src/agents/execution/portfolio_aggregator.py

WI-23 portfolio-level read-only analytics aggregator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.agents.execution.polymarket_client import PolymarketClient
from src.core.config import AppConfig
from src.db.repositories.position_repository import PositionRepository
from src.schemas.risk import PortfolioSnapshot

logger = structlog.get_logger(__name__)

_ZERO = Decimal("0")


class PortfolioAggregator:
    """Compute aggregate exposure metrics over all open positions."""

    def __init__(
        self,
        config: AppConfig,
        polymarket_client: PolymarketClient,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._config = config
        self._polymarket_client = polymarket_client
        self._db_session_factory = db_session_factory

    async def compute_snapshot(self) -> PortfolioSnapshot:
        """Compute the current read-only portfolio snapshot."""
        snapshot_at_utc = datetime.now(timezone.utc)

        async with self._db_session_factory() as session:
            repo = PositionRepository(session)
            open_positions = await repo.get_open_positions()

        if not open_positions:
            empty_snapshot = PortfolioSnapshot(
                snapshot_at_utc=snapshot_at_utc,
                position_count=0,
                total_notional_usdc=_ZERO,
                total_unrealized_pnl=_ZERO,
                total_locked_collateral_usdc=_ZERO,
                positions_with_stale_price=0,
                dry_run=self._config.dry_run,
            )
            logger.info(
                "portfolio.snapshot_computed",
                position_count=empty_snapshot.position_count,
                total_notional_usdc=str(empty_snapshot.total_notional_usdc),
                total_unrealized_pnl=str(empty_snapshot.total_unrealized_pnl),
                total_locked_collateral_usdc=str(
                    empty_snapshot.total_locked_collateral_usdc
                ),
                positions_with_stale_price=empty_snapshot.positions_with_stale_price,
                dry_run=empty_snapshot.dry_run,
            )
            return empty_snapshot

        total_notional_usdc = _ZERO
        total_unrealized_pnl = _ZERO
        total_locked_collateral_usdc = _ZERO
        position_count = 0
        positions_with_stale_price = 0

        for position in open_positions:
            entry_price_d = Decimal(str(position.entry_price))
            order_size_usdc_d = Decimal(str(position.order_size_usdc))

            market_snapshot = await self._polymarket_client.fetch_order_book(
                str(position.token_id)
            )
            if market_snapshot is None:
                current_price = entry_price_d
                positions_with_stale_price += 1
                logger.warning(
                    "portfolio.price_fetch_failed",
                    position_id=str(position.id),
                    token_id=str(position.token_id),
                    fallback="entry_price",
                )
            else:
                current_price = Decimal(str(market_snapshot.midpoint_probability))

            if entry_price_d == _ZERO:
                position_size_tokens = _ZERO
            else:
                position_size_tokens = order_size_usdc_d / entry_price_d

            current_notional = current_price * position_size_tokens
            unrealized_pnl = (current_price - entry_price_d) * position_size_tokens

            total_notional_usdc += current_notional
            total_unrealized_pnl += unrealized_pnl
            total_locked_collateral_usdc += order_size_usdc_d
            position_count += 1

        snapshot = PortfolioSnapshot(
            snapshot_at_utc=snapshot_at_utc,
            position_count=position_count,
            total_notional_usdc=total_notional_usdc,
            total_unrealized_pnl=total_unrealized_pnl,
            total_locked_collateral_usdc=total_locked_collateral_usdc,
            positions_with_stale_price=positions_with_stale_price,
            dry_run=self._config.dry_run,
        )
        logger.info(
            "portfolio.snapshot_computed",
            position_count=snapshot.position_count,
            total_notional_usdc=str(snapshot.total_notional_usdc),
            total_unrealized_pnl=str(snapshot.total_unrealized_pnl),
            total_locked_collateral_usdc=str(snapshot.total_locked_collateral_usdc),
            positions_with_stale_price=snapshot.positions_with_stale_price,
            dry_run=snapshot.dry_run,
        )
        return snapshot
