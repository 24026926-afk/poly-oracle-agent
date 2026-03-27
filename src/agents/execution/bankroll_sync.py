"""
src/agents/execution/bankroll_sync.py

Live USDC bankroll sync provider for Polygon PoS (WI-18).

Strictly read-only:
    - single ERC-20 balanceOf call
    - no approvals, transfers, or state mutation
    - Decimal-only balance conversion
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog
from eth_utils import is_address, to_checksum_address
from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from src.core.config import AppConfig
from src.core.exceptions import BalanceFetchError

logger = structlog.get_logger(__name__)

POLYGON_USDC_PROXY = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
USDC_DECIMALS = 6
BALANCE_OF_SELECTOR = "0x70a08231"
_USDC_SCALE = Decimal("1e6")
_DEFAULT_TIMEOUT_MS = 500
_BALANCE_OF_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


class BalanceReadRequest(BaseModel):
    """Typed request contract for a read-only Polygon USDC balance fetch."""

    wallet_address: str
    token_contract: str = Field(default=POLYGON_USDC_PROXY)
    chain_id: int = Field(default=137)
    timeout_ms: int = Field(default=_DEFAULT_TIMEOUT_MS, gt=0)
    dry_run: bool = Field(default=False)

    @field_validator("wallet_address", "token_contract")
    @classmethod
    def _validate_checksum_address(cls, value: str) -> str:
        if not is_address(value):
            raise ValueError(f"Invalid EIP-55 address: {value}")
        return to_checksum_address(value)

    @field_validator("token_contract")
    @classmethod
    def _validate_canonical_usdc_contract(cls, value: str) -> str:
        if value.lower() != POLYGON_USDC_PROXY.lower():
            raise ValueError("token_contract must be the canonical Polygon USDC proxy")
        return value

    @field_validator("chain_id")
    @classmethod
    def _validate_polygon_chain(cls, value: int) -> int:
        if value != 137:
            raise ValueError("chain_id must be 137 (Polygon PoS)")
        return value

    model_config = {"frozen": True}


class BalanceReadResult(BaseModel):
    """Typed result contract for a Polygon USDC balance fetch."""

    balance_usdc: Decimal
    raw_balance_uint256: int
    wallet_address: str
    block_number: int | None
    fetched_at_utc: datetime
    is_mock: bool

    @field_validator("balance_usdc", mode="before")
    @classmethod
    def _reject_float_balance(cls, value: Any) -> Any:
        if isinstance(value, float):
            raise ValueError("Float balances are forbidden; use Decimal")
        return value

    @field_validator("balance_usdc")
    @classmethod
    def _validate_non_negative_balance(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("Balance cannot be negative")
        return value

    @field_validator("raw_balance_uint256", mode="before")
    @classmethod
    def _validate_raw_balance_type(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("raw_balance_uint256 must be an integer")
        return value

    model_config = {"frozen": True}


class BankrollSyncProvider:
    """Read-only USDC balance reader for Polygon PoS."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    async def fetch_balance(
        self,
        request: BalanceReadRequest | None = None,
    ) -> BalanceReadResult:
        """Fetch the wallet's current USDC balance with fail-closed semantics."""
        dry_run = request.dry_run if request is not None else getattr(
            self._config, "dry_run", True
        )
        if dry_run:
            return self._build_mock_result(request)

        effective_request = request or self._build_request_from_config()
        started_at = time.monotonic()
        w3 = Web3(Web3.HTTPProvider(self._config.polygon_rpc_url))
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(effective_request.token_contract),
            abi=_BALANCE_OF_ABI,
        )
        balance_call = contract.functions.balanceOf(
            effective_request.wallet_address
        ).call

        try:
            raw_balance = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, balance_call),
                timeout=effective_request.timeout_ms / 1000,
            )
        except asyncio.TimeoutError as exc:
            self._log_failure(
                wallet_address=effective_request.wallet_address,
                reason="rpc_timeout",
                latency_ms=self._elapsed_ms(started_at),
            )
            raise BalanceFetchError(
                reason=f"RPC timeout after {effective_request.timeout_ms}ms",
                wallet_address=effective_request.wallet_address,
                cause=exc,
            ) from exc
        except Exception as exc:
            self._log_failure(
                wallet_address=effective_request.wallet_address,
                reason=f"rpc_error:{type(exc).__name__}",
                latency_ms=self._elapsed_ms(started_at),
            )
            raise BalanceFetchError(
                reason=f"RPC balance fetch failed: {type(exc).__name__}",
                wallet_address=effective_request.wallet_address,
                cause=exc,
            ) from exc

        if isinstance(raw_balance, bool) or not isinstance(raw_balance, int):
            self._log_failure(
                wallet_address=effective_request.wallet_address,
                reason="malformed_balance_response",
                latency_ms=self._elapsed_ms(started_at),
            )
            raise BalanceFetchError(
                reason="Malformed balance response from Polygon RPC",
                wallet_address=effective_request.wallet_address,
            )

        balance_usdc = Decimal(raw_balance) / _USDC_SCALE
        assert balance_usdc >= 0, "On-chain USDC balance cannot be negative"

        result = BalanceReadResult(
            balance_usdc=balance_usdc,
            raw_balance_uint256=raw_balance,
            wallet_address=effective_request.wallet_address,
            block_number=None,
            fetched_at_utc=datetime.now(timezone.utc),
            is_mock=False,
        )
        logger.info(
            "bankroll_sync.balance_fetched",
            wallet_address=result.wallet_address,
            balance_usdc=str(result.balance_usdc),
            block_number=result.block_number,
            latency_ms=self._elapsed_ms(started_at),
        )
        return result

    def _build_request_from_config(self) -> BalanceReadRequest:
        return BalanceReadRequest(
            wallet_address=self._config.wallet_address,
            token_contract=POLYGON_USDC_PROXY,
            chain_id=137,
            timeout_ms=_DEFAULT_TIMEOUT_MS,
            dry_run=bool(getattr(self._config, "dry_run", False)),
        )

    def _build_mock_result(
        self,
        request: BalanceReadRequest | None,
    ) -> BalanceReadResult:
        balance_usdc = self._coerce_decimal_balance(
            getattr(self._config, "initial_bankroll_usdc")
        )
        raw_balance_uint256 = int(balance_usdc * _USDC_SCALE)
        wallet_address = (
            request.wallet_address
            if request is not None
            else str(getattr(self._config, "wallet_address", ""))
        )
        result = BalanceReadResult(
            balance_usdc=balance_usdc,
            raw_balance_uint256=raw_balance_uint256,
            wallet_address=wallet_address,
            block_number=None,
            fetched_at_utc=datetime.now(timezone.utc),
            is_mock=True,
        )
        logger.info(
            "bankroll_sync.mock_balance_returned",
            wallet_address=wallet_address or None,
            balance_usdc=str(result.balance_usdc),
            is_mock=True,
        )
        return result

    @staticmethod
    def _coerce_decimal_balance(value: Any) -> Decimal:
        if isinstance(value, float):
            raise ValueError("Float balances are forbidden; use Decimal")
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @staticmethod
    def _elapsed_ms(started_at: float) -> int:
        return int((time.monotonic() - started_at) * 1000)

    def _log_failure(
        self,
        *,
        wallet_address: str,
        reason: str,
        latency_ms: int,
    ) -> None:
        logger.error(
            "bankroll_sync.fetch_failed",
            wallet_address=wallet_address,
            reason=reason,
            latency_ms=latency_ms,
        )
