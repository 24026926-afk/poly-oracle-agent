"""
tests/unit/test_wi29_live_fees.py

RED-phase unit tests for WI-29 Live Fee Injection.
These tests define the target behavior for:
1) GasEstimator (httpx + Decimal-only math + fail-open fallback)
2) MaticPriceProvider (live fetch + fail-open fallback)
3) Orchestrator gas EV gate wiring
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


def _make_eval_response(condition_id: str = "cond-wi29-001") -> MagicMock:
    eval_resp = MagicMock()
    eval_resp.market_context.condition_id = condition_id
    eval_resp.market_context.yes_token_id = "yes-token-wi29"
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


async def _run_exit_scan_once(orch: Orchestrator) -> None:
    task = asyncio.create_task(orch._exit_scan_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


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
        id="pos-wi29-001",
        condition_id="cond-wi29-001",
        token_id="token-wi29-001",
        status=PositionStatus.OPEN,
        side="BUY",
        entry_price=Decimal("0.45"),
        order_size_usdc=Decimal("25"),
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=Decimal("0.46"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action=ExecutionAction.DRY_RUN,
        reason="wi29-test",
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
        position_id="pos-wi29-001",
        condition_id="cond-wi29-001",
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
        position_id="pos-wi29-001",
        condition_id="cond-wi29-001",
        action=ExitOrderAction.DRY_RUN,
        reason=None,
        order_payload=payload,
        signed_order=None,
        exit_price=Decimal("0.41"),
        order_size_usdc=Decimal("25"),
        routed_at_utc=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_gas_estimator_estimate_gas_price_wei_success_hex_to_decimal():
    module = _load_module("src.agents.execution.gas_estimator")
    estimator = module.GasEstimator(_wi29_config(dry_run=False))

    assert hasattr(module, "httpx"), "WI-29 requires httpx-based GasEstimator."
    assert hasattr(estimator, "estimate_gas_price_wei"), (
        "WI-29 GasEstimator must expose estimate_gas_price_wei()."
    )

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"result": "0x6FC23AC00"}  # 30 Gwei

    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with patch.object(module.httpx, "AsyncClient", return_value=client):
        result = await estimator.estimate_gas_price_wei()

    assert result == Decimal("30000000000")
    assert isinstance(result, Decimal)


@pytest.mark.asyncio
async def test_gas_estimator_estimate_gas_price_wei_fallback_on_http_error():
    module = _load_module("src.agents.execution.gas_estimator")
    estimator = module.GasEstimator(_wi29_config(dry_run=False))

    assert hasattr(module, "httpx"), "WI-29 requires httpx-based GasEstimator."
    assert hasattr(estimator, "estimate_gas_price_wei"), (
        "WI-29 GasEstimator must expose estimate_gas_price_wei()."
    )

    client = AsyncMock()
    client.post = AsyncMock(side_effect=httpx.HTTPError("rpc down"))
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with patch.object(module.httpx, "AsyncClient", return_value=client):
        result = await estimator.estimate_gas_price_wei()

    assert result == Decimal("30000000000")
    assert isinstance(result, Decimal)


@pytest.mark.asyncio
async def test_gas_estimator_estimate_gas_price_wei_dry_run_skips_http():
    module = _load_module("src.agents.execution.gas_estimator")
    estimator = module.GasEstimator(_wi29_config(dry_run=True))

    assert hasattr(module, "httpx"), "WI-29 requires httpx-based GasEstimator."
    assert hasattr(estimator, "estimate_gas_price_wei"), (
        "WI-29 GasEstimator must expose estimate_gas_price_wei()."
    )

    client = AsyncMock()
    client.post = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with patch.object(module.httpx, "AsyncClient", return_value=client):
        result = await estimator.estimate_gas_price_wei()

    assert result == Decimal("30000000000")
    client.post.assert_not_awaited()


def test_gas_estimator_estimate_gas_cost_usdc_formula():
    module = _load_module("src.agents.execution.gas_estimator")
    estimator = module.GasEstimator(_wi29_config())

    assert hasattr(estimator, "estimate_gas_cost_usdc"), (
        "WI-29 GasEstimator must expose estimate_gas_cost_usdc()."
    )

    result = estimator.estimate_gas_cost_usdc(
        gas_units=21000,
        gas_price_wei=Decimal("30000000000"),
        matic_usdc_price=Decimal("0.50"),
    )
    expected = (
        Decimal("21000")
        * Decimal("30000000000")
        / Decimal("1000000000000000000")
        * Decimal("0.50")
    )
    assert result == expected
    assert isinstance(result, Decimal)


def test_gas_estimator_estimate_gas_cost_usdc_zero_gas_is_zero():
    module = _load_module("src.agents.execution.gas_estimator")
    estimator = module.GasEstimator(_wi29_config())

    assert hasattr(estimator, "estimate_gas_cost_usdc"), (
        "WI-29 GasEstimator must expose estimate_gas_cost_usdc()."
    )

    result = estimator.estimate_gas_cost_usdc(
        gas_units=21000,
        gas_price_wei=Decimal("0"),
        matic_usdc_price=Decimal("0.50"),
    )
    assert result == Decimal("0")
    assert isinstance(result, Decimal)


def test_gas_estimator_pre_evaluate_gas_check_boundary_and_pass_fail():
    module = _load_module("src.agents.execution.gas_estimator")
    estimator = module.GasEstimator(_wi29_config(gas_ev_buffer_pct=Decimal("0.10")))

    assert hasattr(estimator, "pre_evaluate_gas_check"), (
        "WI-29 GasEstimator must expose pre_evaluate_gas_check()."
    )

    assert (
        estimator.pre_evaluate_gas_check(
            expected_value_usdc=Decimal("0.10"),
            gas_cost_usdc=Decimal("0.05"),
        )
        is True
    )
    assert (
        estimator.pre_evaluate_gas_check(
            expected_value_usdc=Decimal("0.055"),
            gas_cost_usdc=Decimal("0.05"),
        )
        is False
    )
    assert (
        estimator.pre_evaluate_gas_check(
            expected_value_usdc=Decimal("0.03"),
            gas_cost_usdc=Decimal("0.05"),
        )
        is False
    )


@pytest.mark.asyncio
async def test_matic_price_provider_success_fetch_returns_decimal():
    module = _load_module("src.agents.execution.matic_price_provider")
    provider = module.MaticPriceProvider(_wi29_config(dry_run=False))

    assert hasattr(module, "httpx"), "WI-29 MaticPriceProvider requires httpx."
    assert hasattr(provider, "get_matic_usdc"), (
        "WI-29 MaticPriceProvider must expose get_matic_usdc()."
    )

    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"MATIC": "0.62"}

    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with patch.object(module.httpx, "AsyncClient", return_value=client):
        result = await provider.get_matic_usdc()

    assert result == Decimal("0.62")
    assert isinstance(result, Decimal)


@pytest.mark.asyncio
async def test_matic_price_provider_fallback_on_fetch_error():
    module = _load_module("src.agents.execution.matic_price_provider")
    provider = module.MaticPriceProvider(_wi29_config(dry_run=False))

    assert hasattr(module, "httpx"), "WI-29 MaticPriceProvider requires httpx."
    assert hasattr(provider, "get_matic_usdc"), (
        "WI-29 MaticPriceProvider must expose get_matic_usdc()."
    )

    client = AsyncMock()
    client.get = AsyncMock(side_effect=RuntimeError("feed down"))
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with patch.object(module.httpx, "AsyncClient", return_value=client):
        result = await provider.get_matic_usdc()

    assert result == Decimal("0.50")
    assert isinstance(result, Decimal)


@pytest.mark.asyncio
async def test_matic_price_provider_dry_run_skips_http():
    module = _load_module("src.agents.execution.matic_price_provider")
    provider = module.MaticPriceProvider(_wi29_config(dry_run=True))

    assert hasattr(module, "httpx"), "WI-29 MaticPriceProvider requires httpx."
    assert hasattr(provider, "get_matic_usdc"), (
        "WI-29 MaticPriceProvider must expose get_matic_usdc()."
    )

    client = AsyncMock()
    client.get = AsyncMock()
    client.__aenter__.return_value = client
    client.__aexit__.return_value = None

    with patch.object(module.httpx, "AsyncClient", return_value=client):
        result = await provider.get_matic_usdc()

    assert result == Decimal("0.50")
    client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_orchestrator_builds_wi29_components_when_gas_check_enabled(test_config):
    orch = _build_orchestrator(test_config, gas_check_enabled=True)
    assert hasattr(orch, "_gas_estimator")
    assert hasattr(orch, "_matic_price_provider")
    assert orch._gas_estimator is not None
    assert orch._matic_price_provider is not None


@pytest.mark.asyncio
async def test_orchestrator_no_wi29_components_when_gas_check_disabled(test_config):
    orch = _build_orchestrator(test_config, gas_check_enabled=False)
    assert hasattr(orch, "_gas_estimator")
    assert hasattr(orch, "_matic_price_provider")
    assert orch._gas_estimator is None
    assert orch._matic_price_provider is None


@pytest.mark.asyncio
async def test_orchestrator_gas_gate_fail_skips_execution_router(test_config):
    orch = _build_orchestrator(test_config, gas_check_enabled=True)
    orch.broadcaster = AsyncMock()
    orch.position_tracker.record_execution = AsyncMock(return_value=None)
    orch.execution_router.route = AsyncMock(
        return_value=ExecutionResult(action=ExecutionAction.DRY_RUN)
    )

    gas_estimator = MagicMock()
    gas_estimator.estimate_gas_price_wei = AsyncMock(
        return_value=Decimal("100000000000")
    )
    gas_estimator.estimate_gas_cost_usdc = MagicMock(return_value=Decimal("1.00"))
    gas_estimator.pre_evaluate_gas_check = MagicMock(return_value=False)
    price_provider = MagicMock()
    price_provider.get_matic_usdc = AsyncMock(return_value=Decimal("1.00"))

    object.__setattr__(orch, "_gas_estimator", gas_estimator)
    object.__setattr__(orch, "_matic_price_provider", price_provider)

    item = {
        "evaluation": _make_eval_response(),
        "expected_value_usdc": Decimal("0.10"),
        "snapshot_id": "snap-wi29-001",
        "yes_token_id": "token-wi29-001",
    }

    await _run_execution_consumer_once(orch, item)

    orch.execution_router.route.assert_not_awaited()
    assert "execution_result" in item
    assert item["execution_result"].action == ExecutionAction.SKIP
    assert item["execution_result"].reason == "gas_cost_exceeds_ev"


@pytest.mark.asyncio
async def test_orchestrator_gas_gate_pass_calls_execution_router(test_config):
    orch = _build_orchestrator(test_config, gas_check_enabled=True)
    orch.broadcaster = AsyncMock()
    orch.position_tracker.record_execution = AsyncMock(return_value=None)
    orch.execution_router.route = AsyncMock(
        return_value=ExecutionResult(action=ExecutionAction.DRY_RUN)
    )

    gas_estimator = MagicMock()
    gas_estimator.estimate_gas_price_wei = AsyncMock(
        return_value=Decimal("30000000000")
    )
    gas_estimator.estimate_gas_cost_usdc = MagicMock(return_value=Decimal("0.02"))
    gas_estimator.pre_evaluate_gas_check = MagicMock(return_value=True)
    price_provider = MagicMock()
    price_provider.get_matic_usdc = AsyncMock(return_value=Decimal("0.50"))

    object.__setattr__(orch, "_gas_estimator", gas_estimator)
    object.__setattr__(orch, "_matic_price_provider", price_provider)

    item = {
        "evaluation": _make_eval_response(),
        "expected_value_usdc": Decimal("5.00"),
        "snapshot_id": "snap-wi29-002",
        "yes_token_id": "token-wi29-001",
    }

    await _run_execution_consumer_once(orch, item)

    orch.execution_router.route.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_exit_scan_passes_gas_cost_into_settle(test_config):
    orch = _build_orchestrator(test_config, gas_check_enabled=True)
    orch.broadcaster = AsyncMock()
    orch._fetch_position_record = AsyncMock(return_value=_make_position_record())
    orch.exit_strategy_engine.scan_open_positions = AsyncMock(
        return_value=[_make_exit_result()]
    )
    orch.exit_order_router.route_exit = AsyncMock(
        return_value=_make_exit_order_result()
    )
    orch.pnl_calculator.settle = AsyncMock(return_value=MagicMock())
    orch.telegram_notifier = None

    gas_estimator = MagicMock()
    gas_estimator.estimate_gas_price_wei = AsyncMock(
        return_value=Decimal("100000000000")
    )
    gas_estimator.estimate_gas_cost_usdc = MagicMock(return_value=Decimal("0.77"))
    price_provider = MagicMock()
    price_provider.get_matic_usdc = AsyncMock(return_value=Decimal("1.00"))

    object.__setattr__(orch, "_gas_estimator", gas_estimator)
    object.__setattr__(orch, "_matic_price_provider", price_provider)

    await _run_exit_scan_once(orch)

    assert orch.pnl_calculator.settle.await_count >= 1
    _, kwargs = orch.pnl_calculator.settle.await_args
    assert "gas_cost_usdc" in kwargs
    assert kwargs["gas_cost_usdc"] == Decimal("0.77")
