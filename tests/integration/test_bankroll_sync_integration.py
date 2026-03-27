"""
tests/integration/test_bankroll_sync_integration.py

RED-phase integration tests for WI-18 bankroll sync wiring.
"""

from __future__ import annotations

import importlib
from decimal import Decimal
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from src.agents.execution.bankroll_tracker import BankrollPortfolioTracker
from src.orchestrator import Orchestrator


def _patch_heavy_deps():
    mock_w3 = MagicMock()
    mock_w3.eth = MagicMock()
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=0)

    return {
        "AsyncWeb3": MagicMock(return_value=mock_w3),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(),
    }


def _balance_fetch_error_type():
    exceptions = importlib.import_module("src.core.exceptions")
    error_type = getattr(exceptions, "BalanceFetchError", None)
    assert error_type is not None, "Expected BalanceFetchError in src.core.exceptions."
    return error_type


@pytest.mark.asyncio
async def test_tracker_get_total_bankroll_delegates_to_bankroll_sync(
    test_config,
    db_session_factory,
):
    tracker = BankrollPortfolioTracker(
        config=test_config,
        db_session_factory=db_session_factory,
    )
    tracker._bankroll_sync = MagicMock()
    tracker._bankroll_sync.fetch_balance = AsyncMock(return_value=Decimal("4321"))

    bankroll = await tracker.get_total_bankroll()

    tracker._bankroll_sync.fetch_balance.assert_awaited_once()
    assert bankroll == Decimal("4321")


@pytest.mark.asyncio
async def test_tracker_compute_position_size_propagates_balance_fetch_error(
    test_config,
    db_session_factory,
):
    error_type = _balance_fetch_error_type()
    tracker = BankrollPortfolioTracker(
        config=test_config,
        db_session_factory=db_session_factory,
    )
    tracker._bankroll_sync = MagicMock()
    tracker._bankroll_sync.fetch_balance = AsyncMock(
        side_effect=error_type("rpc timeout")
    )

    with pytest.raises(error_type):
        await tracker.compute_position_size(
            kelly_fraction_raw=Decimal("0.10"),
            condition_id="cond-wi18",
        )


@pytest.mark.asyncio
async def test_orchestrator_wires_bankroll_sync_provider_into_tracker(test_config):
    patches = _patch_heavy_deps()

    with patch.multiple("src.orchestrator", **patches), patch(
        "src.orchestrator.BankrollSyncProvider",
        create=True,
    ) as mock_sync_cls, patch(
        "src.orchestrator.BankrollPortfolioTracker",
    ) as mock_tracker_cls:
        Orchestrator(test_config)

    mock_sync_cls.assert_called_once_with(config=test_config)
    mock_tracker_cls.assert_called_once_with(
        config=test_config,
        db_session_factory=ANY,
        bankroll_sync=mock_sync_cls.return_value,
    )
