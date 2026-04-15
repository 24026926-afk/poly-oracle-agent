"""
tests/integration/test_wi31_live_balances_integration.py

RED-phase integration tests for WI-31 Live Wallet Balance Checks.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator import Orchestrator
from src.schemas.execution import ExecutionAction, ExecutionResult


def _patch_heavy_deps():
    mock_w3 = MagicMock()
    mock_w3.eth = MagicMock()
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=0)
    return {
        "AsyncWeb3": MagicMock(return_value=mock_w3),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }


def _make_eval_response(condition_id: str = "cond-wi31-int-001") -> MagicMock:
    eval_resp = MagicMock()
    eval_resp.market_context.condition_id = condition_id
    eval_resp.market_context.yes_token_id = "yes-token-wi31-int"
    eval_resp.recommended_action.value = "BUY"
    eval_resp.position_size_pct = Decimal("0.02")
    return eval_resp


async def _run_execution_consumer_once(orch: Orchestrator, item: dict) -> None:
    await orch.execution_queue.put(item)
    task = asyncio.create_task(orch._execution_consumer_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_execution_consumer_skips_when_wallet_balances_are_insufficient(
    test_config,
):
    cfg = test_config.model_copy(deep=True)
    object.__setattr__(cfg, "enable_wallet_balance_check", True)
    object.__setattr__(cfg, "enable_exposure_validator", False)
    object.__setattr__(cfg, "gas_check_enabled", False)

    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(cfg)

    orch.broadcaster = AsyncMock()
    orch.position_tracker.record_execution = AsyncMock(return_value=None)
    orch.execution_router.route = AsyncMock(
        return_value=ExecutionResult(action=ExecutionAction.DRY_RUN)
    )

    insufficient_balance_result = SimpleNamespace(
        check_passed=False,
        fallback_used=False,
        matic_balance_wei=Decimal("1"),
        usdc_balance_usdc=Decimal("1"),
        matic_sufficient=False,
        usdc_sufficient=False,
    )
    wallet_provider = MagicMock()
    wallet_provider.check_balances = AsyncMock(return_value=insufficient_balance_result)
    object.__setattr__(orch, "_wallet_balance_provider", wallet_provider)

    item = {
        "evaluation": _make_eval_response(),
        "snapshot_id": "snap-wi31-int-001",
        "yes_token_id": "token-wi31-int-001",
    }

    await _run_execution_consumer_once(orch, item)

    wallet_provider.check_balances.assert_awaited_once()
    assert "execution_result" in item
    assert item["execution_result"].action == ExecutionAction.SKIP
    assert item["execution_result"].reason == "insufficient_wallet_balance"
    orch.execution_router.route.assert_not_awaited()
