"""
tests/unit/test_bankroll_sync.py

RED-phase tests for WI-18 bankroll sync.

These tests intentionally codify the expected read-only Polygon USDC
balance contract before the implementation exists.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


MODULE_NAME = "src.agents.execution.bankroll_sync"
MODULE_PATH = Path("src/agents/execution/bankroll_sync.py")
CANONICAL_USDC_PROXY = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
FORBIDDEN_IMPORTS = (
    "src.agents.execution.signer",
    "src.agents.execution.polymarket_client",
    "src.agents.evaluation",
    "src.agents.context",
    "src.agents.ingestion",
)


def _load_bankroll_sync_module():
    try:
        return importlib.import_module(MODULE_NAME)
    except ModuleNotFoundError:
        pytest.fail(
            "Expected WI-18 module src.agents.execution.bankroll_sync to exist.",
            pytrace=False,
        )


def _make_request(module, test_config, *, dry_run: bool, timeout_ms: int = 500):
    request_cls = getattr(module, "BalanceReadRequest", None)
    if request_cls is None:
        return None
    return request_cls(
        wallet_address=test_config.wallet_address,
        token_contract=getattr(module, "POLYGON_USDC_PROXY", CANONICAL_USDC_PROXY),
        chain_id=137,
        timeout_ms=timeout_ms,
        dry_run=dry_run,
    )


async def _invoke_fetch_balance(module, provider, test_config, *, dry_run: bool):
    signature = inspect.signature(provider.fetch_balance)
    if len(signature.parameters) == 0:
        return await provider.fetch_balance()

    request = _make_request(module, test_config, dry_run=dry_run)
    assert request is not None, (
        "BalanceReadRequest must exist when fetch_balance expects a request."
    )
    return await provider.fetch_balance(request)


def _extract_balance(result) -> Decimal:
    if isinstance(result, Decimal):
        return result
    if hasattr(result, "balance_usdc"):
        return result.balance_usdc
    pytest.fail(
        "fetch_balance() must return Decimal or a typed result with balance_usdc.",
        pytrace=False,
    )


def _extract_is_mock(result) -> bool | None:
    return getattr(result, "is_mock", None)


def _balance_fetch_error_type():
    exceptions = importlib.import_module("src.core.exceptions")
    error_type = getattr(exceptions, "BalanceFetchError", None)
    assert error_type is not None, "Expected BalanceFetchError in src.core.exceptions."
    return error_type


def test_bankroll_sync_provider_contract_exists():
    module = _load_bankroll_sync_module()

    provider_cls = getattr(module, "BankrollSyncProvider", None)
    assert provider_cls is not None, "Expected BankrollSyncProvider class."
    assert inspect.isclass(provider_cls)
    assert inspect.iscoroutinefunction(provider_cls.fetch_balance)

    public_methods = [
        name
        for name, member in inspect.getmembers(
            provider_cls, predicate=inspect.isfunction
        )
        if not name.startswith("_")
    ]
    assert public_methods == ["fetch_balance"]

    assert getattr(module, "POLYGON_USDC_PROXY", None) == CANONICAL_USDC_PROXY


def test_balance_contract_models_exist_and_reject_float_balance(test_config):
    module = _load_bankroll_sync_module()

    request_cls = getattr(module, "BalanceReadRequest", None)
    result_cls = getattr(module, "BalanceReadResult", None)

    assert request_cls is not None, "Expected BalanceReadRequest model."
    assert result_cls is not None, "Expected BalanceReadResult model."

    request = request_cls(
        wallet_address=test_config.wallet_address,
        token_contract=getattr(module, "POLYGON_USDC_PROXY", CANONICAL_USDC_PROXY),
        chain_id=137,
        timeout_ms=500,
        dry_run=False,
    )
    assert request.chain_id == 137

    with pytest.raises(Exception):
        result_cls(
            balance_usdc=1.5,
            raw_balance_uint256=1_500_000,
            wallet_address=test_config.wallet_address,
            block_number=123,
            fetched_at_utc=datetime.now(timezone.utc),
            is_mock=False,
        )


@pytest.mark.asyncio
async def test_fetch_balance_dry_run_returns_mock_balance_without_web3(test_config):
    module = _load_bankroll_sync_module()
    provider = module.BankrollSyncProvider(test_config)

    with (
        patch.object(
            module,
            "Web3",
            side_effect=AssertionError("Web3 must not be constructed in dry_run."),
            create=True,
        ) as mock_module_web3,
        patch(
            "web3.Web3",
            side_effect=AssertionError(
                "Global Web3 must not be constructed in dry_run."
            ),
        ) as mock_global_web3,
    ):
        result = await _invoke_fetch_balance(
            module,
            provider,
            test_config,
            dry_run=True,
        )

    balance = _extract_balance(result)
    assert balance == test_config.initial_bankroll_usdc
    assert isinstance(balance, Decimal)
    if _extract_is_mock(result) is not None:
        assert _extract_is_mock(result) is True
    mock_module_web3.assert_not_called()
    mock_global_web3.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_balance_live_read_converts_uint256_exactly(test_config):
    module = _load_bankroll_sync_module()
    provider = module.BankrollSyncProvider(test_config)

    raw_balance = 1_500_000_000
    mock_w3 = MagicMock()

    with (
        patch.object(module, "Web3", create=True) as mock_web3_cls,
        patch.object(
            module.asyncio,
            "wait_for",
            new=AsyncMock(return_value=raw_balance),
        ) as mock_wait_for,
    ):
        mock_web3_cls.HTTPProvider.return_value = MagicMock()
        mock_web3_cls.return_value = mock_w3
        mock_web3_cls.to_checksum_address.side_effect = lambda value: value

        result = await _invoke_fetch_balance(
            module,
            provider,
            test_config,
            dry_run=False,
        )

    balance = _extract_balance(result)
    assert balance == Decimal("1500")
    assert isinstance(balance, Decimal)
    if hasattr(result, "raw_balance_uint256"):
        assert result.raw_balance_uint256 == raw_balance

    mock_wait_for.assert_awaited_once()
    assert mock_wait_for.await_args.kwargs["timeout"] == 0.5


@pytest.mark.asyncio
async def test_fetch_balance_timeout_raises_typed_error(test_config):
    module = _load_bankroll_sync_module()
    provider = module.BankrollSyncProvider(test_config)
    error_type = _balance_fetch_error_type()

    with (
        patch.object(module, "Web3", create=True) as mock_web3_cls,
        patch.object(
            module.asyncio,
            "wait_for",
            new=AsyncMock(side_effect=asyncio.TimeoutError),
        ),
    ):
        mock_web3_cls.HTTPProvider.return_value = MagicMock()
        mock_web3_cls.to_checksum_address.side_effect = lambda value: value
        mock_web3_cls.return_value = MagicMock()

        with pytest.raises(error_type):
            await _invoke_fetch_balance(
                module,
                provider,
                test_config,
                dry_run=False,
            )


@pytest.mark.asyncio
async def test_fetch_balance_malformed_response_raises_typed_error(test_config):
    module = _load_bankroll_sync_module()
    provider = module.BankrollSyncProvider(test_config)
    error_type = _balance_fetch_error_type()

    with (
        patch.object(module, "Web3", create=True) as mock_web3_cls,
        patch.object(
            module.asyncio,
            "wait_for",
            new=AsyncMock(return_value="not-a-uint256"),
        ),
    ):
        mock_web3_cls.HTTPProvider.return_value = MagicMock()
        mock_web3_cls.to_checksum_address.side_effect = lambda value: value
        mock_web3_cls.return_value = MagicMock()

        with pytest.raises(error_type):
            await _invoke_fetch_balance(
                module,
                provider,
                test_config,
                dry_run=False,
            )


@pytest.mark.asyncio
async def test_fetch_balance_zero_balance_is_valid(test_config):
    module = _load_bankroll_sync_module()
    provider = module.BankrollSyncProvider(test_config)

    with (
        patch.object(module, "Web3", create=True) as mock_web3_cls,
        patch.object(
            module.asyncio,
            "wait_for",
            new=AsyncMock(return_value=0),
        ),
    ):
        mock_web3_cls.HTTPProvider.return_value = MagicMock()
        mock_web3_cls.to_checksum_address.side_effect = lambda value: value
        mock_web3_cls.return_value = MagicMock()

        result = await _invoke_fetch_balance(
            module,
            provider,
            test_config,
            dry_run=False,
        )

    balance = _extract_balance(result)
    assert balance == Decimal("0")
    assert isinstance(balance, Decimal)


def test_bankroll_sync_module_has_no_forbidden_imports():
    assert MODULE_PATH.exists(), (
        "Expected src/agents/execution/bankroll_sync.py to exist."
    )

    module_ast = ast.parse(MODULE_PATH.read_text())
    imported_modules: set[str] = set()

    for node in ast.walk(module_ast):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    for forbidden_prefix in FORBIDDEN_IMPORTS:
        assert all(
            not name.startswith(forbidden_prefix) for name in imported_modules
        ), f"Forbidden dependency detected: {forbidden_prefix}"
