"""
tests/unit/test_gas_estimator.py

Unit tests for the WI-29 GasEstimator implementation.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.agents.execution.gas_estimator import GasEstimator
from src.schemas.web3 import GasPrice


def _config(**overrides) -> SimpleNamespace:
    defaults = {
        "dry_run": False,
        "polygon_rpc_url": "http://localhost:8545",
        "dry_run_gas_price_wei": Decimal("30000000000"),
        "fallback_gas_price_gwei": 50.0,
        "gas_ev_buffer_pct": Decimal("0.10"),
        "matic_usdc_price": Decimal("0.50"),
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _mock_httpx_client(
    *,
    post_result: MagicMock | None = None,
    post_side_effect: Exception | None = None,
) -> AsyncMock:
    client = AsyncMock()
    if post_side_effect is not None:
        client.post = AsyncMock(side_effect=post_side_effect)
    else:
        client.post = AsyncMock(return_value=post_result)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None
    return client


@pytest.mark.asyncio
async def test_estimate_returns_gas_price_model():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"result": "0x6FC23AC00"}  # 30 Gwei
    client = _mock_httpx_client(post_result=response)

    estimator = GasEstimator(_config())
    with patch(
        "src.agents.execution.gas_estimator.httpx.AsyncClient", return_value=client
    ):
        result = await estimator.estimate()

    assert isinstance(result, GasPrice)
    assert result.max_fee_per_gas_wei == 30000000000
    assert result.max_fee_per_gas_gwei == 30.0
    assert result.is_fallback is False


@pytest.mark.asyncio
async def test_estimate_uses_rpc_fallback_on_http_error():
    client = _mock_httpx_client(post_side_effect=httpx.HTTPError("rpc down"))
    estimator = GasEstimator(_config(fallback_gas_price_gwei=50.0))

    with patch(
        "src.agents.execution.gas_estimator.httpx.AsyncClient", return_value=client
    ):
        result = await estimator.estimate()

    assert isinstance(result, GasPrice)
    assert result.max_fee_per_gas_wei == 50000000000
    assert result.max_fee_per_gas_gwei == 50.0
    assert result.is_fallback is True


@pytest.mark.asyncio
async def test_estimate_uses_dry_run_value_without_http():
    estimator = GasEstimator(
        _config(
            dry_run=True,
            dry_run_gas_price_wei=Decimal("123000000000"),
        )
    )
    client = _mock_httpx_client()

    with patch(
        "src.agents.execution.gas_estimator.httpx.AsyncClient", return_value=client
    ):
        result = await estimator.estimate()

    client.post.assert_not_awaited()
    assert isinstance(result, GasPrice)
    assert result.max_fee_per_gas_wei == 123000000000
    assert result.max_fee_per_gas_gwei == 123.0
    assert result.is_fallback is True


@pytest.mark.asyncio
async def test_estimate_gas_price_wei_parses_hex_response():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"result": "0x6FC23AC00"}  # 30 Gwei
    client = _mock_httpx_client(post_result=response)

    estimator = GasEstimator(_config())
    with patch(
        "src.agents.execution.gas_estimator.httpx.AsyncClient", return_value=client
    ):
        gas_wei = await estimator.estimate_gas_price_wei()

    assert gas_wei == Decimal("30000000000")
    assert isinstance(gas_wei, Decimal)


@pytest.mark.asyncio
async def test_estimate_gas_price_wei_fallback_never_raises():
    client = _mock_httpx_client(post_side_effect=RuntimeError("bad rpc"))
    estimator = GasEstimator(_config())

    with patch(
        "src.agents.execution.gas_estimator.httpx.AsyncClient", return_value=client
    ):
        gas_wei = await estimator.estimate_gas_price_wei()

    assert gas_wei == Decimal("50000000000")
    assert isinstance(gas_wei, Decimal)
