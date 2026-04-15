"""
tests/unit/test_wi30_exposure_limits.py

RED-phase unit tests for WI-30 Global Portfolio Exposure Limits.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from decimal import Decimal
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator import Orchestrator
from src.schemas.execution import ExecutionAction, ExecutionResult
from src.schemas.llm import MarketCategory


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # pragma: no cover - RED diagnostics
        pytest.fail(f"Failed to import {module_name}: {exc!r}", pytrace=False)


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


def _make_eval_response(condition_id: str = "cond-wi30-unit-001") -> MagicMock:
    eval_resp = MagicMock()
    eval_resp.market_context.condition_id = condition_id
    eval_resp.market_context.yes_token_id = "yes-token-wi30-unit"
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
async def test_exposure_validator_breaches_when_new_trade_plus_open_exposure_exceeds_limit():
    module = _load_module("src.agents.execution.exposure_validator")
    validator = module.ExposureValidator(
        config=SimpleNamespace(
            max_exposure_pct=Decimal("0.03"),
            max_category_exposure_pct=Decimal("0.015"),
        ),
        position_repo=MagicMock(),
    )

    open_positions = [
        SimpleNamespace(order_size_usdc=Decimal("10"), category="CRYPTO"),
        SimpleNamespace(order_size_usdc=Decimal("18"), category="CRYPTO"),
    ]
    passed, summary = validator.validate_entry(
        bankroll_usdc=Decimal("1000"),
        proposed_size_usdc=Decimal("5"),
        category=MarketCategory.CRYPTO,
        open_positions=open_positions,
    )

    assert passed is False
    assert summary.aggregate_exposure_usdc == Decimal("28")
    assert summary.global_limit_usdc == Decimal("30")
    assert summary.validation_passed is False


@pytest.mark.asyncio
async def test_exposure_validator_allows_entry_at_exact_global_limit_boundary():
    module = _load_module("src.agents.execution.exposure_validator")
    validator = module.ExposureValidator(
        config=SimpleNamespace(
            max_exposure_pct=Decimal("0.03"),
            max_category_exposure_pct=Decimal("0.015"),
        ),
        position_repo=MagicMock(),
    )

    open_positions = [
        SimpleNamespace(order_size_usdc=Decimal("10"), category="GENERAL"),
        SimpleNamespace(order_size_usdc=Decimal("15"), category="GENERAL"),
    ]
    passed, summary = validator.validate_entry(
        bankroll_usdc=Decimal("1000"),
        proposed_size_usdc=Decimal("5"),
        category=MarketCategory.GENERAL,
        open_positions=open_positions,
    )

    assert passed is True
    assert summary.aggregate_exposure_usdc == Decimal("25")
    assert summary.global_limit_usdc == Decimal("30")
    assert summary.validation_passed is True


@pytest.mark.asyncio
async def test_execution_consumer_skips_with_exposure_limit_exceeded_result(test_config):
    cfg = test_config.model_copy(deep=True)
    object.__setattr__(cfg, "enable_exposure_validator", True)
    object.__setattr__(cfg, "gas_check_enabled", False)

    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(cfg)

    orch.broadcaster = AsyncMock()
    orch.position_tracker.record_execution = AsyncMock(return_value=None)
    orch.execution_router.route = AsyncMock(
        return_value=ExecutionResult(action=ExecutionAction.DRY_RUN)
    )
    orch._position_repo = MagicMock()
    orch._position_repo.get_open_positions = AsyncMock(
        return_value=[SimpleNamespace(order_size_usdc=Decimal("28"))]
    )

    summary = MagicMock()
    summary.aggregate_exposure_usdc = Decimal("28")
    summary.available_headroom_usdc = Decimal("2")
    summary.model_dump.return_value = {}
    orch._exposure_validator = MagicMock()
    orch._exposure_validator.validate_entry.return_value = (False, summary)

    item = {
        "evaluation": _make_eval_response(),
        "snapshot_id": "snap-wi30-unit-001",
        "yes_token_id": "token-wi30-unit-001",
        "proposed_size_usdc": Decimal("5"),
        "category": MarketCategory.CRYPTO,
    }

    await _run_execution_consumer_once(orch, item)

    assert "execution_result" in item
    assert item["execution_result"].action == ExecutionAction.SKIP
    assert item["execution_result"].reason == "exposure_limit_exceeded"
    orch.execution_router.route.assert_not_awaited()
    orch._position_repo.get_open_positions.assert_awaited_once()
    orch._exposure_validator.validate_entry.assert_called_once()
