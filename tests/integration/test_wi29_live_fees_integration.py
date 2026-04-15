"""
tests/integration/test_wi29_live_fees_integration.py

RED-phase integration tests for WI-29 Live Fee Injection.
These tests define end-to-end expectations for:
1) entry-path gas EV gating
2) RPC fallback chain behavior
3) settlement-time gas cost injection
4) dry-run mock-value behavior
5) exit-path independence from entry gas gate
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal
import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.orchestrator import Orchestrator
from src.schemas.execution import (
    ExecutionAction,
    ExecutionResult,
    ExitOrderAction,
    ExitOrderResult,
    ExitReason,
    ExitResult,
)
from src.schemas.position import PositionRecord, PositionStatus
from src.schemas.web3 import OrderData, OrderSide, SIGNATURE_TYPE_EOA


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except Exception as exc:  # pragma: no cover - RED diagnostics
        pytest.fail(f"Failed to import {module_name}: {exc!r}", pytrace=False)


def _wi29_config(**overrides):
    defaults = {
        "dry_run": False,
        "polygon_rpc_url": "http://localhost:8545",
        "dry_run_gas_price_wei": Decimal("30000000000"),
        "gas_ev_buffer_pct": Decimal("0.10"),
        "matic_usdc_price": Decimal("0.50"),
        "gas_check_enabled": True,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


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


def _make_eval_response(condition_id: str = "cond-wi29-int-001") -> MagicMock:
    eval_resp = MagicMock()
    eval_resp.market_context.condition_id = condition_id
    eval_resp.market_context.yes_token_id = "yes-token-wi29-int"
    eval_resp.recommended_action.value = "BUY"
    eval_resp.position_size_pct = Decimal("0.02")
    return eval_resp


def _build_orchestrator(test_config, *, gas_check_enabled: bool) -> Orchestrator:
    cfg = test_config.model_copy(deep=True)
    object.__setattr__(cfg, "gas_check_enabled", gas_check_enabled)
    object.__setattr__(cfg, "dry_run_gas_price_wei", Decimal("30000000000"))
    object.__setattr__(cfg, "gas_ev_buffer_pct", Decimal("0.10"))
    object.__setattr__(cfg, "matic_usdc_price", Decimal("0.50"))
    object.__setattr__(cfg, "exit_scan_interval_seconds", Decimal("0"))

    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        orch = Orchestrator(cfg)
    return orch


def _make_position_record() -> PositionRecord:
    now = datetime.now(timezone.utc)
    return PositionRecord(
        id="pos-wi29-int-001",
        condition_id="cond-wi29-int-001",
        token_id="token-wi29-int-001",
        status=PositionStatus.OPEN,
        side="BUY",
        entry_price=Decimal("0.45"),
        order_size_usdc=Decimal("25"),
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=Decimal("0.46"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action=ExecutionAction.DRY_RUN,
        reason="wi29-int",
        routed_at_utc=now,
        recorded_at_utc=now,
        realized_pnl=None,
        exit_price=None,
        closed_at_utc=None,
        gas_cost_usdc=Decimal("0"),
        fees_usdc=Decimal("0"),
    )


def _make_exit_result() -> ExitResult:
    return ExitResult(
        position_id="pos-wi29-int-001",
        condition_id="cond-wi29-int-001",
        should_exit=True,
        exit_reason=ExitReason.STOP_LOSS,
        entry_price=Decimal("0.45"),
        current_midpoint=Decimal("0.40"),
        current_best_bid=Decimal("0.39"),
        position_age_hours=Decimal("9"),
        unrealized_edge=Decimal("-0.05"),
        evaluated_at_utc=datetime.now(timezone.utc),
    )


def _make_exit_order_result() -> ExitOrderResult:
    payload = OrderData(
        salt=1,
        maker="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        signer="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        taker="0x0000000000000000000000000000000000000000",
        token_id=123,
        maker_amount=1_000_000,
        taker_amount=500_000,
        expiration=0,
        nonce=0,
        fee_rate_bps=0,
        side=OrderSide.SELL,
        signature_type=SIGNATURE_TYPE_EOA,
    )
    return ExitOrderResult(
        position_id="pos-wi29-int-001",
        condition_id="cond-wi29-int-001",
        action=ExitOrderAction.DRY_RUN,
        reason=None,
        order_payload=payload,
        signed_order=None,
        exit_price=Decimal("0.41"),
        order_size_usdc=Decimal("25"),
        routed_at_utc=datetime.now(timezone.utc),
    )


async def _run_execution_consumer_once(orch: Orchestrator, item: dict) -> None:
    await orch.execution_queue.put(item)
    task = asyncio.create_task(orch._execution_consumer_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _run_exit_scan_once(orch: Orchestrator) -> None:
    task = asyncio.create_task(orch._exit_scan_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_wi29_integration_gas_gate_pass_calls_execution_router(test_config):
    orch = _build_orchestrator(test_config, gas_check_enabled=True)
    orch.broadcaster = AsyncMock()
    orch.position_tracker.record_execution = AsyncMock(return_value=None)
    orch.execution_router.route = AsyncMock(
        return_value=ExecutionResult(action=ExecutionAction.DRY_RUN)
    )

    gas_estimator = MagicMock()
    gas_estimator.estimate_gas_price_wei = AsyncMock(return_value=Decimal("30000000000"))
    gas_estimator.estimate_gas_cost_usdc = MagicMock(return_value=Decimal("0.02"))
    gas_estimator.pre_evaluate_gas_check = MagicMock(return_value=True)
    price_provider = MagicMock()
    price_provider.get_matic_usdc = AsyncMock(return_value=Decimal("0.50"))

    object.__setattr__(orch, "_gas_estimator", gas_estimator)
    object.__setattr__(orch, "_matic_price_provider", price_provider)

    item = {
        "evaluation": _make_eval_response(),
        "expected_value_usdc": Decimal("5.00"),
        "snapshot_id": "snap-wi29-int-001",
        "yes_token_id": "token-wi29-int-001",
    }

    await _run_execution_consumer_once(orch, item)
    orch.execution_router.route.assert_awaited_once()


@pytest.mark.asyncio
async def test_wi29_integration_gas_gate_fail_emits_skip_and_bypasses_router(test_config):
    orch = _build_orchestrator(test_config, gas_check_enabled=True)
    orch.broadcaster = AsyncMock()
    orch.position_tracker.record_execution = AsyncMock(return_value=None)
    orch.execution_router.route = AsyncMock(
        return_value=ExecutionResult(action=ExecutionAction.DRY_RUN)
    )

    gas_estimator = MagicMock()
    gas_estimator.estimate_gas_price_wei = AsyncMock(return_value=Decimal("100000000000"))
    gas_estimator.estimate_gas_cost_usdc = MagicMock(return_value=Decimal("2.00"))
    gas_estimator.pre_evaluate_gas_check = MagicMock(return_value=False)
    price_provider = MagicMock()
    price_provider.get_matic_usdc = AsyncMock(return_value=Decimal("1.00"))

    object.__setattr__(orch, "_gas_estimator", gas_estimator)
    object.__setattr__(orch, "_matic_price_provider", price_provider)

    item = {
        "evaluation": _make_eval_response(),
        "expected_value_usdc": Decimal("0.10"),
        "snapshot_id": "snap-wi29-int-002",
        "yes_token_id": "token-wi29-int-001",
    }

    await _run_execution_consumer_once(orch, item)

    orch.execution_router.route.assert_not_awaited()
    assert "execution_result" in item
    assert item["execution_result"].action == ExecutionAction.SKIP
    assert item["execution_result"].reason == "gas_cost_exceeds_ev"


@pytest.mark.asyncio
async def test_wi29_integration_rpc_fallback_chain_uses_mock_value():
    gas_module = _load_module("src.agents.execution.gas_estimator")
    estimator = gas_module.GasEstimator(_wi29_config(dry_run=False))

    assert hasattr(gas_module, "httpx"), "WI-29 requires httpx-based GasEstimator."
    assert hasattr(
        estimator, "estimate_gas_price_wei"
    ), "WI-29 GasEstimator must expose estimate_gas_price_wei()."
    assert hasattr(
        estimator, "estimate_gas_cost_usdc"
    ), "WI-29 GasEstimator must expose estimate_gas_cost_usdc()."
    assert hasattr(
        estimator, "pre_evaluate_gas_check"
    ), "WI-29 GasEstimator must expose pre_evaluate_gas_check()."

    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.HTTPError("rpc unavailable"))
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with patch.object(gas_module.httpx, "AsyncClient", return_value=client):
        gas_price_wei = await estimator.estimate_gas_price_wei()

    assert gas_price_wei == Decimal("30000000000")

    gas_cost = estimator.estimate_gas_cost_usdc(
        gas_units=21000,
        gas_price_wei=gas_price_wei,
        matic_usdc_price=Decimal("0.50"),
    )
    assert isinstance(gas_cost, Decimal)
    assert estimator.pre_evaluate_gas_check(
        expected_value_usdc=Decimal("5.00"),
        gas_cost_usdc=gas_cost,
    ) is True


@pytest.mark.asyncio
async def test_wi29_integration_settlement_gas_is_injected_into_pnl_settle(test_config):
    orch = _build_orchestrator(test_config, gas_check_enabled=True)
    orch.broadcaster = AsyncMock()
    orch._fetch_position_record = AsyncMock(return_value=_make_position_record())
    orch.exit_strategy_engine.scan_open_positions = AsyncMock(return_value=[_make_exit_result()])
    orch.exit_order_router.route_exit = AsyncMock(return_value=_make_exit_order_result())
    orch.pnl_calculator.settle = AsyncMock(return_value=MagicMock())
    orch.telegram_notifier = None

    gas_estimator = MagicMock()
    gas_estimator.estimate_gas_price_wei = AsyncMock(return_value=Decimal("30000000000"))
    gas_estimator.estimate_gas_cost_usdc = MagicMock(return_value=Decimal("0.33"))
    price_provider = MagicMock()
    price_provider.get_matic_usdc = AsyncMock(return_value=Decimal("0.50"))

    object.__setattr__(orch, "_gas_estimator", gas_estimator)
    object.__setattr__(orch, "_matic_price_provider", price_provider)

    await _run_exit_scan_once(orch)

    assert orch.pnl_calculator.settle.await_count >= 1
    _, kwargs = orch.pnl_calculator.settle.await_args
    assert kwargs.get("gas_cost_usdc") == Decimal("0.33")


@pytest.mark.asyncio
async def test_wi29_integration_dry_run_uses_mock_values_without_http():
    gas_module = _load_module("src.agents.execution.gas_estimator")
    matic_module = _load_module("src.agents.execution.matic_price_provider")

    gas_estimator = gas_module.GasEstimator(_wi29_config(dry_run=True))
    price_provider = matic_module.MaticPriceProvider(_wi29_config(dry_run=True))

    assert hasattr(gas_module, "httpx"), "WI-29 requires httpx-based GasEstimator."
    assert hasattr(matic_module, "httpx"), "WI-29 requires httpx-based MaticPriceProvider."
    assert hasattr(gas_estimator, "estimate_gas_price_wei")
    assert hasattr(price_provider, "get_matic_usdc")

    gas_client = AsyncMock()
    gas_client.post = AsyncMock()
    gas_client.__aenter__.return_value = gas_client
    gas_client.__aexit__.return_value = None

    matic_client = AsyncMock()
    matic_client.get = AsyncMock()
    matic_client.__aenter__.return_value = matic_client
    matic_client.__aexit__.return_value = None

    with patch.object(gas_module.httpx, "AsyncClient", return_value=gas_client):
        gas_price_wei = await gas_estimator.estimate_gas_price_wei()
    with patch.object(matic_module.httpx, "AsyncClient", return_value=matic_client):
        matic_price = await price_provider.get_matic_usdc()

    assert gas_price_wei == Decimal("30000000000")
    assert matic_price == Decimal("0.50")
    gas_client.post.assert_not_awaited()
    matic_client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_wi29_integration_exit_path_not_blocked_by_high_gas(test_config):
    orch = _build_orchestrator(test_config, gas_check_enabled=True)
    orch.broadcaster = AsyncMock()
    orch._fetch_position_record = AsyncMock(return_value=_make_position_record())
    orch.exit_strategy_engine.scan_open_positions = AsyncMock(return_value=[_make_exit_result()])
    orch.exit_order_router.route_exit = AsyncMock(return_value=_make_exit_order_result())
    orch.pnl_calculator.settle = AsyncMock(return_value=MagicMock())
    orch.telegram_notifier = None

    gas_estimator = MagicMock()
    gas_estimator.estimate_gas_price_wei = AsyncMock(return_value=Decimal("250000000000"))
    gas_estimator.estimate_gas_cost_usdc = MagicMock(return_value=Decimal("4.00"))
    price_provider = MagicMock()
    price_provider.get_matic_usdc = AsyncMock(return_value=Decimal("1.20"))

    object.__setattr__(orch, "_gas_estimator", gas_estimator)
    object.__setattr__(orch, "_matic_price_provider", price_provider)

    await _run_exit_scan_once(orch)

    orch.exit_order_router.route_exit.assert_awaited()
    orch.pnl_calculator.settle.assert_awaited()

