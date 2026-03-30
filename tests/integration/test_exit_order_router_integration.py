"""
tests/integration/test_exit_order_router_integration.py

RED-phase integration tests for WI-20 exit order routing and orchestrator wiring.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator import Orchestrator
from src.schemas.web3 import OrderData, OrderSide, SIGNATURE_TYPE_EOA, SignedOrder


ROUTER_MODULE_NAME = "src.agents.execution.exit_order_router"
SCHEMA_MODULE_NAME = "src.schemas.execution"
ROUTER_MODULE_PATH = Path("src/agents/execution/exit_order_router.py")
FORBIDDEN_IMPORT_PREFIXES = (
    "src.agents.context",
    "src.agents.evaluation",
    "src.agents.ingestion",
    "src.db",
)
WALLET_ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
DEFAULT_SIGNER = object()


def _load_contracts():
    try:
        router_module = importlib.import_module(ROUTER_MODULE_NAME)
    except ModuleNotFoundError:
        pytest.fail(
            "Expected WI-20 module src.agents.execution.exit_order_router to exist.",
            pytrace=False,
        )
    except Exception as exc:
        pytest.fail(
            f"Exit order router module import failed unexpectedly: {exc!r}",
            pytrace=False,
        )

    try:
        schema_module = importlib.import_module(SCHEMA_MODULE_NAME)
    except ModuleNotFoundError:
        pytest.fail(
            "Expected schema module src.schemas.execution to exist.",
            pytrace=False,
        )
    except Exception as exc:
        pytest.fail(
            f"Execution schema module import failed unexpectedly: {exc!r}",
            pytrace=False,
        )

    return router_module, schema_module


def _make_router_config(*, dry_run: bool):
    return SimpleNamespace(
        dry_run=dry_run,
        wallet_address=WALLET_ADDRESS,
        exit_min_bid_tolerance=Decimal("0.01"),
    )


def _make_position_record(
    schema_module,
    *,
    position_id: str,
    token_id: str,
    entry_price: Decimal = Decimal("0.60"),
    order_size_usdc: Decimal = Decimal("30"),
):
    now = datetime.now(timezone.utc)
    return schema_module.PositionRecord(
        id=position_id,
        condition_id=f"condition-{position_id}",
        token_id=token_id,
        status=schema_module.PositionStatus.CLOSED,
        side="BUY",
        entry_price=entry_price,
        order_size_usdc=order_size_usdc,
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=entry_price + Decimal("0.01"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action=schema_module.ExecutionAction.EXECUTED,
        reason=None,
        routed_at_utc=now,
        recorded_at_utc=now,
    )


def _make_exit_result(
    schema_module,
    *,
    position_id: str,
    should_exit: bool = True,
    exit_reason: str = "STOP_LOSS",
):
    return schema_module.ExitResult(
        position_id=position_id,
        condition_id=f"condition-{position_id}",
        should_exit=should_exit,
        exit_reason=getattr(schema_module.ExitReason, exit_reason),
        entry_price=Decimal("0.60"),
        current_midpoint=Decimal("0.55"),
        current_best_bid=Decimal("0.54"),
        position_age_hours=Decimal("8"),
        unrealized_edge=Decimal("-0.05"),
        evaluated_at_utc=datetime.now(timezone.utc),
    )


def _make_signed_order(order: OrderData) -> SignedOrder:
    return SignedOrder(
        order=order,
        signature="0x" + "ab" * 65,
        owner=WALLET_ADDRESS,
    )


def _build_router(
    *,
    dry_run: bool,
    snapshot=None,
    signer=DEFAULT_SIGNER,
    signing_error: Exception | None = None,
):
    router_module, schema_module = _load_contracts()
    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock(return_value=snapshot)

    transaction_signer = signer
    if transaction_signer is DEFAULT_SIGNER:
        transaction_signer = MagicMock()
    if transaction_signer is not None:
        if signing_error is not None:
            transaction_signer.sign_order.side_effect = signing_error
        else:
            transaction_signer.sign_order.side_effect = (
                lambda order: _make_signed_order(order)
            )

    router = router_module.ExitOrderRouter(
        config=_make_router_config(dry_run=dry_run),
        polymarket_client=polymarket_client,
        transaction_signer=transaction_signer,
    )
    return router, polymarket_client, transaction_signer, schema_module


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


def _build_orchestrator(test_config) -> Orchestrator:
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        return Orchestrator(test_config)


def _make_exit_order_result(schema_module, *, position_id: str, action: str):
    order = OrderData(
        salt=1,
        maker=WALLET_ADDRESS,
        signer=WALLET_ADDRESS,
        taker="0x0000000000000000000000000000000000000000",
        token_id=123,
        maker_amount=50_000_000,
        taker_amount=27_500_000,
        expiration=0,
        nonce=0,
        fee_rate_bps=0,
        side=OrderSide.SELL,
        signature_type=SIGNATURE_TYPE_EOA,
    )
    signed_order = _make_signed_order(order) if action == "SELL_ROUTED" else None
    return schema_module.ExitOrderResult(
        position_id=position_id,
        condition_id=f"condition-{position_id}",
        action=getattr(schema_module.ExitOrderAction, action),
        reason=None,
        order_payload=order,
        signed_order=signed_order,
        exit_price=Decimal("0.55"),
        order_size_usdc=Decimal("30"),
        routed_at_utc=datetime.now(timezone.utc),
    )


def test_exit_order_router_module_has_no_forbidden_imports():
    if not ROUTER_MODULE_PATH.exists():
        pytest.fail(
            "Expected router implementation file at "
            "src/agents/execution/exit_order_router.py.",
            pytrace=False,
        )

    tree = ast.parse(ROUTER_MODULE_PATH.read_text())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    forbidden = sorted(
        module_name
        for module_name in imported_modules
        if module_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
    )
    assert forbidden == []


def test_route_exit_contract_returns_typed_result():
    router_module, schema_module = _load_contracts()
    router_cls = getattr(router_module, "ExitOrderRouter", None)
    result_cls = getattr(schema_module, "ExitOrderResult", None)
    assert router_cls is not None
    assert result_cls is not None
    assert inspect.iscoroutinefunction(router_cls.route_exit)
    assert "route_exit" in router_cls.__dict__


@pytest.mark.asyncio
async def test_end_to_end_dry_run_computes_full_payload_without_signing():
    router, _, signer, schema_module = _build_router(
        dry_run=True,
        snapshot=SimpleNamespace(best_bid=Decimal("0.55")),
    )
    position = _make_position_record(
        schema_module,
        position_id="pos-int-001",
        token_id="123",
    )
    exit_result = _make_exit_result(schema_module, position_id="pos-int-001")

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.DRY_RUN
    assert result.order_payload is not None
    assert result.order_payload.side == OrderSide.SELL
    assert result.order_payload.maker_amount == 50_000_000
    assert result.order_payload.taker_amount == 27_500_000
    assert result.signed_order is None
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_end_to_end_live_mode_returns_signed_sell_order():
    router, _, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=SimpleNamespace(best_bid=Decimal("0.55")),
    )
    position = _make_position_record(
        schema_module,
        position_id="pos-int-002",
        token_id="456",
    )
    exit_result = _make_exit_result(schema_module, position_id="pos-int-002")

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.SELL_ROUTED
    assert result.signed_order is not None
    assert result.order_payload is not None
    assert result.order_payload.side == OrderSide.SELL
    signer.sign_order.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("snapshot", "signer", "signing_error", "expected_reason"),
    [
        (None, DEFAULT_SIGNER, None, "order_book_unavailable"),
        (SimpleNamespace(best_bid=Decimal("0.55")), None, None, "signer_unavailable"),
        (
            SimpleNamespace(best_bid=Decimal("0.55")),
            DEFAULT_SIGNER,
            RuntimeError("sign failed"),
            "signing_error",
        ),
    ],
)
async def test_upstream_failure_cascade_returns_failed_actions(
    snapshot,
    signer,
    signing_error,
    expected_reason,
):
    router, _, _, schema_module = _build_router(
        dry_run=False,
        snapshot=snapshot,
        signer=signer,
        signing_error=signing_error,
    )
    position = _make_position_record(
        schema_module,
        position_id="pos-int-003",
        token_id="789",
    )
    exit_result = _make_exit_result(schema_module, position_id="pos-int-003")

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.FAILED
    assert result.reason == expected_reason


def test_orchestrator_constructs_exit_order_router_with_expected_dependencies(
    test_config,
):
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()), patch(
        "src.orchestrator.ExitOrderRouter"
    ) as mock_exit_router_cls:
        orch = Orchestrator(test_config)

    assert orch.exit_order_router is mock_exit_router_cls.return_value
    mock_exit_router_cls.assert_called_once_with(
        config=orch.config,
        polymarket_client=orch.polymarket_client,
        transaction_signer=orch.signer,
    )


@pytest.mark.asyncio
async def test_exit_scan_loop_continues_when_one_routing_call_fails(
    monkeypatch, test_config
):
    _, schema_module = _load_contracts()
    orchestrator = _build_orchestrator(test_config)
    object.__setattr__(orchestrator.config, "dry_run", False)
    object.__setattr__(orchestrator.config, "exit_scan_interval_seconds", Decimal("1"))

    exit_results = [
        _make_exit_result(schema_module, position_id="pos-int-101"),
        _make_exit_result(schema_module, position_id="pos-int-102"),
        _make_exit_result(schema_module, position_id="pos-int-103"),
    ]
    positions = [
        _make_position_record(
            schema_module,
            position_id="pos-int-101",
            token_id="1001",
        ),
        _make_position_record(
            schema_module,
            position_id="pos-int-102",
            token_id="1002",
        ),
        _make_position_record(
            schema_module,
            position_id="pos-int-103",
            token_id="1003",
        ),
    ]

    orchestrator.exit_strategy_engine.scan_open_positions = AsyncMock(
        return_value=exit_results
    )
    orchestrator._fetch_position_record = AsyncMock(side_effect=positions)
    orchestrator.exit_order_router = MagicMock()
    orchestrator.exit_order_router.route_exit = AsyncMock(
        side_effect=[
            RuntimeError("route crashed"),
            _make_exit_order_result(
                schema_module,
                position_id="pos-int-102",
                action="FAILED",
            ),
            _make_exit_order_result(
                schema_module,
                position_id="pos-int-103",
                action="SELL_ROUTED",
            ),
        ]
    )

    orchestrator.broadcaster = AsyncMock()
    orchestrator.broadcaster.broadcast = AsyncMock()

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    mock_logger = MagicMock()
    monkeypatch.setattr("src.orchestrator.logger", mock_logger)
    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orchestrator._exit_scan_loop()

    assert orchestrator.exit_order_router.route_exit.await_count == 3
    orchestrator.broadcaster.broadcast.assert_awaited_once()
    mock_logger.error.assert_any_call(
        "exit_scan.routing_error",
        position_id="pos-int-101",
        error="route crashed",
    )

