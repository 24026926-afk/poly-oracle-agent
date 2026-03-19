"""
src/agents/execution/gas_estimator.py

EIP-1559 gas price estimator for Polygon PoS.

Queries the Polygon RPC for the latest baseFee and priority tip,
applies a safety buffer, and returns a ``GasPrice`` model.  If the
RPC is unreachable, falls back to a hard-coded default so the
broadcaster always receives a value to attempt a transaction.
"""

import structlog
from web3 import AsyncWeb3

from src.core.exceptions import GasEstimatorError
from src.schemas.web3 import GasPrice

logger = structlog.get_logger(__name__)

# Wei-per-Gwei constant
_WEI_PER_GWEI: int = 1_000_000_000


class GasEstimator:
    """
    Fetches fresh EIP-1559 gas pricing from Polygon on every call.

    Polygon blocks are ~2 s — prices are volatile, so we never cache.
    """

    PRIORITY_FEE_MULTIPLIER: float = 1.15   # 15 % buffer over base tip
    MAX_GAS_PRICE_GWEI: float = 500.0       # hard safety ceiling
    FALLBACK_GAS_PRICE_GWEI: float = 50.0   # used when RPC fails

    def __init__(self, w3: AsyncWeb3) -> None:
        self._w3 = w3

    async def estimate(self) -> GasPrice:
        """Return a fresh ``GasPrice`` for the current Polygon block.

        Raises:
            GasEstimatorError: If the computed maxFeePerGas exceeds
                ``MAX_GAS_PRICE_GWEI`` (safety ceiling).
        """
        try:
            return await self._estimate_from_rpc()
        except GasEstimatorError:
            # Ceiling breach — propagate, do NOT mask.
            raise
        except Exception as exc:
            logger.warning(
                "gas_estimator.rpc_failed",
                error=str(exc),
                fallback_gwei=self.FALLBACK_GAS_PRICE_GWEI,
            )
            return self._build_fallback()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _estimate_from_rpc(self) -> GasPrice:
        block = await self._w3.eth.get_block("latest")
        base_fee_wei: int = block["baseFeePerGas"]

        tip_wei: int = await self._w3.eth.max_priority_fee
        priority_fee_wei: int = int(tip_wei * self.PRIORITY_FEE_MULTIPLIER)

        max_fee_per_gas_wei: int = (2 * base_fee_wei) + priority_fee_wei
        max_fee_per_gas_gwei: float = max_fee_per_gas_wei / _WEI_PER_GWEI

        if max_fee_per_gas_gwei > self.MAX_GAS_PRICE_GWEI:
            raise GasEstimatorError(
                f"Gas price {max_fee_per_gas_gwei:.2f} Gwei exceeds "
                f"ceiling of {self.MAX_GAS_PRICE_GWEI} Gwei"
            )

        logger.debug(
            "gas_estimator.estimated",
            base_fee_gwei=round(base_fee_wei / _WEI_PER_GWEI, 4),
            priority_gwei=round(priority_fee_wei / _WEI_PER_GWEI, 4),
            max_fee_gwei=round(max_fee_per_gas_gwei, 4),
        )

        return GasPrice(
            base_fee_wei=base_fee_wei,
            priority_fee_wei=priority_fee_wei,
            max_fee_per_gas_wei=max_fee_per_gas_wei,
            max_fee_per_gas_gwei=round(max_fee_per_gas_gwei, 4),
        )

    def _build_fallback(self) -> GasPrice:
        fallback_wei = int(self.FALLBACK_GAS_PRICE_GWEI * _WEI_PER_GWEI)
        return GasPrice(
            base_fee_wei=fallback_wei,
            priority_fee_wei=0,
            max_fee_per_gas_wei=fallback_wei,
            max_fee_per_gas_gwei=self.FALLBACK_GAS_PRICE_GWEI,
            is_fallback=True,
        )
