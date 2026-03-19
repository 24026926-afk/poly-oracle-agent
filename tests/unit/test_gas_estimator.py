"""
tests/unit/test_gas_estimator.py

Async unit tests for the EIP-1559 GasEstimator.
"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from src.agents.execution.gas_estimator import GasEstimator
from src.core.exceptions import GasEstimatorError
from src.schemas.web3 import GasPrice

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
WEI_PER_GWEI = 1_000_000_000

# Reasonable Polygon baseline: 30 Gwei base, 2 Gwei tip
BASE_FEE_WEI = 30 * WEI_PER_GWEI
TIP_WEI = 2 * WEI_PER_GWEI


def _mock_w3(
    base_fee_wei: int = BASE_FEE_WEI,
    tip_wei: int = TIP_WEI,
) -> MagicMock:
    """Build an AsyncWeb3 mock with controllable gas values."""
    w3 = MagicMock()
    w3.eth = MagicMock()
    w3.eth.get_block = AsyncMock(
        return_value={"baseFeePerGas": base_fee_wei}
    )
    # max_priority_fee is an awaitable property on the real AsyncWeb3.
    # We model it as a coroutine that returns the value.
    w3.eth.max_priority_fee = AsyncMock(return_value=tip_wei)()
    return w3


def _broken_w3() -> MagicMock:
    """AsyncWeb3 mock whose RPC calls always raise."""
    w3 = MagicMock()
    w3.eth = MagicMock()
    w3.eth.get_block = AsyncMock(side_effect=ConnectionError("RPC down"))
    return w3


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_estimate_returns_gas_price_model():
    w3 = _mock_w3()
    est = GasEstimator(w3)

    result = await est.estimate()

    assert isinstance(result, GasPrice)
    assert result.is_fallback is False


@pytest.mark.asyncio
async def test_estimate_applies_priority_fee_multiplier():
    w3 = _mock_w3(base_fee_wei=BASE_FEE_WEI, tip_wei=TIP_WEI)
    est = GasEstimator(w3)

    result = await est.estimate()

    expected_priority = int(TIP_WEI * 1.15)
    assert result.priority_fee_wei == expected_priority


@pytest.mark.asyncio
async def test_estimate_max_fee_formula():
    w3 = _mock_w3(base_fee_wei=BASE_FEE_WEI, tip_wei=TIP_WEI)
    est = GasEstimator(w3)

    result = await est.estimate()

    expected_priority = int(TIP_WEI * 1.15)
    expected_max = (2 * BASE_FEE_WEI) + expected_priority
    assert result.max_fee_per_gas_wei == expected_max


@pytest.mark.asyncio
async def test_estimate_raises_on_ceiling_breach():
    # 300 Gwei base → max_fee = 600+ Gwei, exceeds 500 ceiling
    huge_base = 300 * WEI_PER_GWEI
    w3 = _mock_w3(base_fee_wei=huge_base, tip_wei=TIP_WEI)
    est = GasEstimator(w3)

    with pytest.raises(GasEstimatorError, match="exceeds ceiling"):
        await est.estimate()


@pytest.mark.asyncio
async def test_estimate_returns_fallback_on_rpc_error():
    w3 = _broken_w3()
    est = GasEstimator(w3)

    result = await est.estimate()

    assert result.is_fallback is True
    assert result.max_fee_per_gas_gwei == 50.0


@pytest.mark.asyncio
async def test_fallback_never_raises():
    w3 = _broken_w3()
    est = GasEstimator(w3)

    # Must always return, never raise
    result = await est.estimate()

    assert isinstance(result, GasPrice)
