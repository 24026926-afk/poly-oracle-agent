"""
src/agents/execution/gas_estimator.py

WI-29 live fee estimator:
- fetches Polygon gas price via JSON-RPC `eth_gasPrice` (httpx)
- converts gas usage to USDC using Decimal-only arithmetic
- enforces pre-evaluation EV viability gate

Fail-open behavior:
- any RPC failure returns a configured fallback gas price
- never raises into caller on estimate failures
"""

from __future__ import annotations

from decimal import Decimal

import httpx
import structlog

from src.core.config import AppConfig
from src.schemas.web3 import GasPrice

logger = structlog.get_logger(__name__)

_REQUEST_TIMEOUT_SECONDS = 2.0
_WEI_PER_GWEI = Decimal("1000000000")
_WEI_PER_MATIC = Decimal("1000000000000000000")


class GasEstimator:
    """WI-29 gas estimator and EV gate helper."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._log = logger.bind(component="GasEstimator")
        self._last_is_fallback = False

    def _dry_run_gas_price_wei(self) -> Decimal:
        return Decimal(str(self._config.dry_run_gas_price_wei))

    def _rpc_fallback_gas_price_wei(self) -> Decimal:
        fallback_gwei = getattr(self._config, "fallback_gas_price_gwei", None)
        if fallback_gwei is not None:
            return (
                Decimal(str(fallback_gwei)) * _WEI_PER_GWEI
            ).quantize(Decimal("1"))
        return self._dry_run_gas_price_wei()

    async def estimate_gas_price_wei(self) -> Decimal:
        """Return Polygon gas price in WEI via eth_gasPrice."""
        if self._config.dry_run:
            self._last_is_fallback = True
            return self._dry_run_gas_price_wei()

        payload = {"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1}

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    self._config.polygon_rpc_url,
                    json=payload,
                )
                response.raise_for_status()
                result_hex = response.json()["result"]
                gas_price_wei = Decimal(str(int(str(result_hex), 16)))
                self._last_is_fallback = False
                self._log.info("gas.estimated", gas_price_wei=str(gas_price_wei))
                return gas_price_wei
        except Exception as exc:
            fallback = self._rpc_fallback_gas_price_wei()
            self._last_is_fallback = True
            self._log.error(
                "gas.rpc_failed",
                error=str(exc),
                fallback_gas_price_wei=str(fallback),
            )
            return fallback

    def estimate_gas_cost_usdc(
        self,
        gas_units: int,
        gas_price_wei: Decimal,
        matic_usdc_price: Decimal,
    ) -> Decimal:
        """Convert gas usage into USDC with Decimal-only arithmetic."""
        gas_cost_matic = Decimal(str(gas_units)) * gas_price_wei / _WEI_PER_MATIC
        gas_cost_usdc = gas_cost_matic * matic_usdc_price
        self._log.info(
            "gas.settlement_computed",
            gas_units=gas_units,
            gas_price_wei=str(gas_price_wei),
            matic_usdc_price=str(matic_usdc_price),
            gas_cost_usdc=str(gas_cost_usdc),
        )
        return gas_cost_usdc

    def pre_evaluate_gas_check(
        self,
        expected_value_usdc: Decimal,
        gas_cost_usdc: Decimal,
    ) -> bool:
        """Require EV to exceed gas cost by configured buffer percentage."""
        buffer_pct = Decimal(str(getattr(self._config, "gas_ev_buffer_pct", Decimal("0.10"))))
        buffered_threshold = gas_cost_usdc * (Decimal("1") + buffer_pct)
        passes = expected_value_usdc > buffered_threshold
        self._log.info(
            "gas.check_passed" if passes else "gas.check_failed",
            expected_value_usdc=str(expected_value_usdc),
            gas_cost_usdc=str(gas_cost_usdc),
            buffered_threshold=str(buffered_threshold),
        )
        return passes

    async def estimate(self) -> GasPrice:
        """Legacy adapter for broadcaster, mapped to WI-29 gas-price source."""
        gas_price_wei = await self.estimate_gas_price_wei()
        gwei = float((gas_price_wei / _WEI_PER_GWEI).quantize(Decimal("0.0001")))
        gas_price_int = int(gas_price_wei)
        return GasPrice(
            base_fee_wei=gas_price_int,
            priority_fee_wei=0,
            max_fee_per_gas_wei=gas_price_int,
            max_fee_per_gas_gwei=gwei,
            is_fallback=self._last_is_fallback,
        )

