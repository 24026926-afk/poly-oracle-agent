"""
src/agents/execution/bankroll_tracker.py

Bankroll and portfolio exposure tracking service.

Computes available bankroll and enforces position-size caps
using persisted ExecutionTx state from the database.  All monetary
math uses ``Decimal`` — never ``float``.

Risk-auditor compliance:
    - Kelly fraction: 0.25 (Quarter-Kelly) via ``config.kelly_fraction``
    - Exposure cap: min(kelly_size, 0.03 × bankroll)
    - Exposure aggregation includes PENDING + CONFIRMED rows
"""

from __future__ import annotations

from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import AppConfig
from src.core.exceptions import ExposureLimitError
from src.db.repositories.execution_repo import ExecutionRepository

logger = structlog.get_logger(__name__)


class BankrollPortfolioTracker:
    """Real-time bankroll awareness and position-size enforcement.

    Replaces the hardcoded ``1000 USDC`` fallback previously used in
    ``TransactionSigner.build_order_from_decision()``.
    """

    def __init__(
        self,
        config: AppConfig,
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._config = config
        self._db_factory = db_session_factory

    # ------------------------------------------------------------------
    # Bankroll queries
    # ------------------------------------------------------------------

    async def get_total_bankroll(self) -> Decimal:
        """Return the configured seed bankroll (USDC)."""
        return self._config.initial_bankroll_usdc

    async def get_exposure(self, condition_id: str) -> Decimal:
        """Sum of PENDING + CONFIRMED execution sizes for *condition_id*."""
        async with self._db_factory() as session:
            repo = ExecutionRepository(session)
            exposure = await repo.get_aggregate_exposure(condition_id)
        logger.debug(
            "bankroll.exposure_queried",
            condition_id=condition_id,
            exposure_usdc=str(exposure),
        )
        return exposure

    async def get_available_bankroll(self, condition_id: str) -> Decimal:
        """Total bankroll minus current exposure, floored at zero."""
        total = await self.get_total_bankroll()
        exposure = await self.get_exposure(condition_id)
        available = max(total - exposure, Decimal("0"))
        logger.debug(
            "bankroll.available_computed",
            total_usdc=str(total),
            exposure_usdc=str(exposure),
            available_usdc=str(available),
        )
        return available

    # ------------------------------------------------------------------
    # Position sizing (risk_management.md §2.2 + §3)
    # ------------------------------------------------------------------

    async def compute_position_size(
        self,
        kelly_fraction_raw: Decimal,
        condition_id: str,
    ) -> Decimal:
        """Apply Quarter-Kelly and 3% exposure cap.

        Args:
            kelly_fraction_raw: Full Kelly fraction f* from the EV calc.
            condition_id: Market to check exposure against.

        Returns:
            Position size in USDC (``Decimal``), already capped.
        """
        bankroll = await self.get_total_bankroll()

        # Quarter-Kelly: f_quarter = KELLY_FRAC × f*
        kelly_frac = Decimal(str(self._config.kelly_fraction))
        kelly_size = kelly_frac * kelly_fraction_raw * bankroll

        # Exposure cap: 3% of bankroll
        exposure_pct = Decimal(str(self._config.max_exposure_pct))
        exposure_cap = exposure_pct * bankroll

        # Final size = min(kelly_size, exposure_cap)
        position_size = min(kelly_size, exposure_cap)

        # Never negative
        position_size = max(position_size, Decimal("0"))

        logger.info(
            "bankroll.position_sized",
            kelly_fraction_raw=str(kelly_fraction_raw),
            kelly_size_usdc=str(kelly_size),
            exposure_cap_usdc=str(exposure_cap),
            final_size_usdc=str(position_size),
        )
        return position_size

    # ------------------------------------------------------------------
    # Pre-trade validation
    # ------------------------------------------------------------------

    async def validate_trade(
        self,
        size_usdc: Decimal,
        condition_id: str,
    ) -> None:
        """Reject the trade if it would exceed bankroll or exposure limits.

        Raises:
            ExposureLimitError: When ``size_usdc`` exceeds available
                bankroll or the per-trade exposure cap.
        """
        bankroll = await self.get_total_bankroll()
        exposure_pct = Decimal(str(self._config.max_exposure_pct))
        exposure_cap = exposure_pct * bankroll

        if size_usdc > exposure_cap:
            raise ExposureLimitError(
                f"Position {size_usdc} USDC exceeds "
                f"{exposure_pct:.0%} exposure cap ({exposure_cap} USDC)"
            )

        available = await self.get_available_bankroll(condition_id)
        if size_usdc > available:
            raise ExposureLimitError(
                f"Position {size_usdc} USDC exceeds "
                f"available bankroll ({available} USDC) "
                f"for condition_id={condition_id}"
            )

        logger.debug(
            "bankroll.trade_validated",
            size_usdc=str(size_usdc),
            available_usdc=str(available),
            exposure_cap_usdc=str(exposure_cap),
        )
