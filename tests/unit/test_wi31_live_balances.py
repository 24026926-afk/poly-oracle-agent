"""
tests/unit/test_wi31_live_balances.py

RED-phase unit tests for WI-31 Live Wallet Balance Checks.
"""

from __future__ import annotations

from decimal import Decimal
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # pragma: no cover - RED diagnostics
        pytest.fail(f"Failed to import {module_name}: {exc!r}", pytrace=False)


def _wi31_config(**overrides):
    defaults = {
        "dry_run": False,
        "polygon_rpc_url": "http://localhost:8545",
        "wallet_address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        "min_matic_balance_wei": Decimal("100000000000000000"),
        "min_usdc_balance_usdc": Decimal("10"),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _status_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://localhost:8545")
    response = httpx.Response(503, request=request)
    return httpx.HTTPStatusError(
        "503 Service Unavailable",
        request=request,
        response=response,
    )


@pytest.mark.asyncio
async def test_get_matic_balance_wei_parses_hex_to_decimal():
    module = _load_module("src.agents.execution.wallet_balance_provider")
    cfg = _wi31_config()

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "jsonrpc": "2.0",
        "result": "0x1bc16d674ec80000",
    }  # 2 MATIC in WEI

    client = AsyncMock()
    client.post = AsyncMock(return_value=response)

    provider = module.WalletBalanceProvider(config=cfg, http_client=client)
    balance = await provider.get_matic_balance_wei(cfg.wallet_address)

    assert balance == Decimal("2000000000000000000")
    assert isinstance(balance, Decimal)


@pytest.mark.asyncio
async def test_get_usdc_balance_usdc_parses_eth_call_six_decimals():
    module = _load_module("src.agents.execution.wallet_balance_provider")
    cfg = _wi31_config()

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "jsonrpc": "2.0",
        "result": "0x5f5e100",
    }  # 100,000,000 raw units -> 100 USDC (6 decimals)

    client = AsyncMock()
    client.post = AsyncMock(return_value=response)

    provider = module.WalletBalanceProvider(config=cfg, http_client=client)
    balance = await provider.get_usdc_balance_usdc(cfg.wallet_address)

    assert balance == Decimal("100")
    assert isinstance(balance, Decimal)

    _, kwargs = client.post.await_args
    payload = kwargs["json"]
    call_target = payload["params"][0]["to"]
    call_data = payload["params"][0]["data"]

    assert payload["method"] == "eth_call"
    assert call_target.lower() == "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
    assert "70a08231" in call_data.lower()
    assert cfg.wallet_address[2:].lower().zfill(64) in call_data.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_factory",
    [
        lambda: httpx.TimeoutException("rpc timeout"),
        _status_error,
    ],
    ids=["timeout", "http_status_error"],
)
async def test_check_balances_is_fail_open_on_rpc_errors(exc_factory):
    module = _load_module("src.agents.execution.wallet_balance_provider")
    cfg = _wi31_config()
    provider = module.WalletBalanceProvider(config=cfg, http_client=AsyncMock())

    provider.get_matic_balance_wei = AsyncMock(side_effect=exc_factory())
    provider.get_usdc_balance_usdc = AsyncMock(return_value=Decimal("500"))

    result = await provider.check_balances()

    assert result.check_passed is True
    assert result.fallback_used is True
