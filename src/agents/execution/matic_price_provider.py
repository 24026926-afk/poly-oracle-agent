"""
src/agents/execution/matic_price_provider.py

WI-29 live MATIC/USDC price provider.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import structlog

from src.core.config import AppConfig

logger = structlog.get_logger(__name__)

_REQUEST_TIMEOUT_SECONDS = 2.0
_GAMMA_MATIC_URL = "https://gamma-api.polymarket.com/prices?ids=MATIC"


class MaticPriceProvider:
    """Fetch MATIC/USDC and fail-open to a configured Decimal fallback."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._log = logger.bind(component="MaticPriceProvider")

    def _fallback_price(self) -> Decimal:
        return Decimal(str(self._config.matic_usdc_price))

    @staticmethod
    def _extract_price(payload: Any) -> Decimal:
        if isinstance(payload, dict):
            if "MATIC" in payload:
                return Decimal(str(payload["MATIC"]))
            if "price" in payload:
                return Decimal(str(payload["price"]))
            for key in ("data", "result"):
                nested = payload.get(key)
                if isinstance(nested, dict):
                    if "MATIC" in nested:
                        return Decimal(str(nested["MATIC"]))
                    if "price" in nested:
                        return Decimal(str(nested["price"]))
        raise ValueError("MATIC price not found in response payload")

    async def get_matic_usdc(self) -> Decimal:
        """Return live MATIC/USDC price or fail-open fallback."""
        if self._config.dry_run:
            return self._fallback_price()

        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.get(_GAMMA_MATIC_URL)
                response.raise_for_status()
                price = self._extract_price(response.json())
                self._log.info("matic_price.fetched", matic_usdc_price=str(price))
                return price
        except Exception as exc:
            self._log.error("matic_price.fetch_failed", error=str(exc))
            return self._fallback_price()
