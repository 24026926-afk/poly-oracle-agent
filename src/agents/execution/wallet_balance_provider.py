"""
src/agents/execution/wallet_balance_provider.py

WI-31 live wallet balance gate:
- fetch MATIC via eth_getBalance
- fetch USDC via eth_call(balanceOf)
- evaluate threshold sufficiency before evaluation routing

Fail-open behavior:
- RPC/network failures return fallback result with check_passed=True
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import structlog

from src.core.config import AppConfig
from src.schemas.web3 import BalanceCheckResult

logger = structlog.get_logger(__name__)

_POLYGON_USDC_PROXY = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
_BALANCE_OF_SELECTOR = "70a08231"
_WEI_PER_MATIC = Decimal("1000000000000000000")
_USDC_SCALE = Decimal("1000000")
_REQUEST_TIMEOUT_SECONDS = 3.0


class WalletBalanceProvider:
    """Async wallet balance gate for WI-31."""

    def __init__(
        self,
        config: AppConfig,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = http_client
        self.log = logger.bind(component="WalletBalanceProvider")

    async def get_matic_balance_wei(self, address: str) -> Decimal:
        """Fetch native MATIC balance in WEI via eth_getBalance."""
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBalance",
            "params": [address, "latest"],
            "id": 1,
        }
        response = await self._post_rpc(payload)
        response.raise_for_status()
        result_hex = response.json()["result"]
        return Decimal(str(int(str(result_hex), 16)))

    async def get_usdc_balance_usdc(self, address: str) -> Decimal:
        """Fetch USDC balance via eth_call(balanceOf) and scale from 6 decimals."""
        padded_address = address[2:].lower().zfill(64)
        call_data = f"0x{_BALANCE_OF_SELECTOR}{padded_address}"
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": _POLYGON_USDC_PROXY, "data": call_data}, "latest"],
            "id": 2,
        }
        response = await self._post_rpc(payload)
        response.raise_for_status()
        result_hex = response.json()["result"]
        raw_uint256 = Decimal(str(int(str(result_hex), 16)))
        return raw_uint256 / _USDC_SCALE

    async def check_balances(
        self,
        trade_size_usdc: Decimal | None = None,
        estimated_gas_matic: Decimal | None = None,
    ) -> BalanceCheckResult:
        """
        Run both balance RPC calls concurrently and evaluate thresholds.

        Optional parameters are accepted for future audit payload enrichment.
        """
        _ = trade_size_usdc
        _ = estimated_gas_matic

        if self._config.dry_run:
            return self._build_mock_result()

        try:
            matic_balance_wei, usdc_balance_usdc = await asyncio.gather(
                self.get_matic_balance_wei(self._config.wallet_address),
                self.get_usdc_balance_usdc(self._config.wallet_address),
            )
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            self.log.warning(
                "wallet.balance_fallback_used",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return self._build_fallback_result()
        except Exception as exc:
            self.log.warning(
                "wallet.balance_fallback_used",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return self._build_fallback_result()

        min_matic = Decimal(str(self._config.min_matic_balance_wei))
        min_usdc = Decimal(str(self._config.min_usdc_balance_usdc))
        matic_sufficient = matic_balance_wei >= min_matic
        usdc_sufficient = usdc_balance_usdc >= min_usdc
        check_passed = matic_sufficient and usdc_sufficient

        result = BalanceCheckResult(
            wallet_address=self._config.wallet_address,
            matic_balance_wei=matic_balance_wei,
            matic_balance_matic=matic_balance_wei / _WEI_PER_MATIC,
            usdc_balance_usdc=usdc_balance_usdc,
            min_matic_balance_wei=min_matic,
            min_usdc_balance_usdc=min_usdc,
            matic_sufficient=matic_sufficient,
            usdc_sufficient=usdc_sufficient,
            check_passed=check_passed,
            fallback_used=False,
            is_mock=False,
            checked_at_utc=datetime.now(timezone.utc),
        )
        return result

    async def _post_rpc(self, payload: dict[str, object]) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(
                self._config.polygon_rpc_url,
                json=payload,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
            return await client.post(self._config.polygon_rpc_url, json=payload)

    def _build_fallback_result(self) -> BalanceCheckResult:
        min_matic = Decimal(str(self._config.min_matic_balance_wei))
        min_usdc = Decimal(str(self._config.min_usdc_balance_usdc))
        return BalanceCheckResult(
            wallet_address=self._config.wallet_address,
            matic_balance_wei=min_matic,
            matic_balance_matic=min_matic / _WEI_PER_MATIC,
            usdc_balance_usdc=min_usdc,
            min_matic_balance_wei=min_matic,
            min_usdc_balance_usdc=min_usdc,
            matic_sufficient=True,
            usdc_sufficient=True,
            check_passed=True,
            fallback_used=True,
            is_mock=False,
            checked_at_utc=datetime.now(timezone.utc),
        )

    def _build_mock_result(self) -> BalanceCheckResult:
        min_matic = Decimal(str(self._config.min_matic_balance_wei))
        min_usdc = Decimal(str(self._config.min_usdc_balance_usdc))
        mock_matic = min_matic * Decimal("10")
        mock_usdc = min_usdc * Decimal("10")
        return BalanceCheckResult(
            wallet_address=self._config.wallet_address,
            matic_balance_wei=mock_matic,
            matic_balance_matic=mock_matic / _WEI_PER_MATIC,
            usdc_balance_usdc=mock_usdc,
            min_matic_balance_wei=min_matic,
            min_usdc_balance_usdc=min_usdc,
            matic_sufficient=True,
            usdc_sufficient=True,
            check_passed=True,
            fallback_used=False,
            is_mock=True,
            checked_at_utc=datetime.now(timezone.utc),
        )
