"""
tests/unit/test_exit_order_router.py

RED-phase unit tests for WI-20 ExitOrderRouter.
"""

from __future__ import annotations

import importlib
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.config import AppConfig
from src.schemas.web3 import SignedOrder


ROUTER_MODULE_NAME = "src.agents.execution.exit_order_router"
SCHEMA_MODULE_NAME = "src.schemas.execution"
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


def _make_config(
    *,
    dry_run: bool,
    exit_min_bid_tolerance: Decimal = Decimal("0.01"),
):
    return SimpleNamespace(
        dry_run=dry_run,
        wallet_address=WALLET_ADDRESS,
        exit_min_bid_tolerance=exit_min_bid_tolerance,
    )


def _make_position_record(
    schema_module,
    *,
    position_id: str = "pos-unit-001",
    condition_id: str = "condition-unit-001",
    token_id: str = "12345",
    entry_price: Decimal = Decimal("0.60"),
    order_size_usdc: Decimal = Decimal("30"),
):
    now = datetime.now(timezone.utc)
    return schema_module.PositionRecord(
        id=position_id,
        condition_id=condition_id,
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
    position_id: str = "pos-unit-001",
    condition_id: str = "condition-unit-001",
    should_exit: bool = True,
    exit_reason: str = "STOP_LOSS",
):
    return schema_module.ExitResult(
        position_id=position_id,
        condition_id=condition_id,
        should_exit=should_exit,
        exit_reason=getattr(schema_module.ExitReason, exit_reason),
        entry_price=Decimal("0.60"),
        current_midpoint=Decimal("0.55"),
        current_best_bid=Decimal("0.54"),
        position_age_hours=Decimal("8"),
        unrealized_edge=Decimal("-0.05"),
        evaluated_at_utc=datetime.now(timezone.utc),
    )


def _make_signed_order(order) -> SignedOrder:
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
    signing_outcome=None,
    exit_min_bid_tolerance: Decimal = Decimal("0.01"),
):
    router_module, schema_module = _load_contracts()

    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock(return_value=snapshot)

    transaction_signer = signer
    if transaction_signer is DEFAULT_SIGNER:
        transaction_signer = MagicMock()

    if transaction_signer is not None:
        if signing_outcome is None:
            transaction_signer.sign_order.side_effect = lambda order: (
                _make_signed_order(order)
            )
        elif isinstance(signing_outcome, Exception):
            transaction_signer.sign_order.side_effect = signing_outcome
        else:
            transaction_signer.sign_order.side_effect = lambda order: signing_outcome

    router = router_module.ExitOrderRouter(
        config=_make_config(
            dry_run=dry_run,
            exit_min_bid_tolerance=exit_min_bid_tolerance,
        ),
        polymarket_client=polymarket_client,
        transaction_signer=transaction_signer,
    )
    return router, polymarket_client, transaction_signer, schema_module


def test_exit_order_router_contract_exists_and_has_one_public_async_method():
    router_module, _ = _load_contracts()

    router_cls = getattr(router_module, "ExitOrderRouter", None)
    assert router_cls is not None, "Expected ExitOrderRouter class."
    assert inspect.isclass(router_cls)
    assert inspect.iscoroutinefunction(router_cls.route_exit)

    init_params = list(inspect.signature(router_cls.__init__).parameters.keys())
    assert init_params == [
        "self",
        "config",
        "polymarket_client",
        "transaction_signer",
    ]

    public_methods = [
        name
        for name, member in inspect.getmembers(router_cls, predicate=inspect.isfunction)
        if not name.startswith("_")
    ]
    assert public_methods == ["route_exit"]


def test_exit_order_action_enum_exists_with_expected_values():
    _, schema_module = _load_contracts()
    action_cls = getattr(schema_module, "ExitOrderAction", None)
    assert action_cls is not None, "Expected ExitOrderAction enum."
    assert {member.value for member in action_cls} == {
        "SELL_ROUTED",
        "DRY_RUN",
        "FAILED",
        "SKIP",
    }


def test_exit_order_result_schema_rejects_float_financials():
    _, schema_module = _load_contracts()
    action_cls = getattr(schema_module, "ExitOrderAction", None)
    result_cls = getattr(schema_module, "ExitOrderResult", None)

    assert action_cls is not None, "Expected ExitOrderAction enum."
    assert result_cls is not None, "Expected ExitOrderResult schema."

    with pytest.raises(Exception):
        result_cls(
            position_id="pos-unit-001",
            condition_id="condition-unit-001",
            action=action_cls.FAILED,
            reason="float_money_forbidden",
            exit_price=0.45,
            order_size_usdc=10.25,
            routed_at_utc=datetime.now(timezone.utc),
        )


def test_exit_order_result_schema_is_frozen():
    _, schema_module = _load_contracts()
    action_cls = getattr(schema_module, "ExitOrderAction", None)
    result_cls = getattr(schema_module, "ExitOrderResult", None)

    assert action_cls is not None, "Expected ExitOrderAction enum."
    assert result_cls is not None, "Expected ExitOrderResult schema."

    result = result_cls(
        position_id="pos-unit-001",
        condition_id="condition-unit-001",
        action=action_cls.SKIP,
        reason="should_exit_is_false",
        routed_at_utc=datetime.now(timezone.utc),
    )
    with pytest.raises(Exception):
        result.reason = "mutated"


def test_app_config_includes_exit_min_bid_tolerance_decimal_default():
    fields = AppConfig.model_fields
    assert "exit_min_bid_tolerance" in fields
    field = fields["exit_min_bid_tolerance"]
    assert field.annotation is Decimal
    assert field.default == Decimal("0.01")


@pytest.mark.asyncio
async def test_should_exit_false_skips_without_upstream_calls():
    router, polymarket_client, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=SimpleNamespace(best_bid=Decimal("0.55")),
    )
    position = _make_position_record(schema_module)
    exit_result = _make_exit_result(
        schema_module,
        should_exit=False,
        exit_reason="NO_EDGE",
    )

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.SKIP
    assert result.reason == "should_exit_is_false"
    polymarket_client.fetch_order_book.assert_not_awaited()
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_exit_reason_error_skips_without_upstream_calls():
    router, polymarket_client, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=SimpleNamespace(best_bid=Decimal("0.55")),
    )
    position = _make_position_record(schema_module)
    exit_result = _make_exit_result(
        schema_module,
        should_exit=True,
        exit_reason="ERROR",
    )

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.SKIP
    assert result.reason == "exit_reason_is_error"
    polymarket_client.fetch_order_book.assert_not_awaited()
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_order_book_none_returns_failed():
    router, polymarket_client, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=None,
    )
    position = _make_position_record(
        schema_module,
        token_id="token-id-from-position",
    )
    exit_result = _make_exit_result(schema_module)

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.FAILED
    assert result.reason == "order_book_unavailable"
    polymarket_client.fetch_order_book.assert_awaited_once_with(
        "token-id-from-position"
    )
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_best_bid_below_tolerance_returns_failed():
    router, _, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=SimpleNamespace(best_bid=Decimal("0.009")),
        exit_min_bid_tolerance=Decimal("0.01"),
    )
    position = _make_position_record(schema_module)
    exit_result = _make_exit_result(schema_module)

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.FAILED
    assert result.reason == "exit_bid_below_tolerance"
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_entry_price_non_positive_returns_failed():
    router, _, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=SimpleNamespace(best_bid=Decimal("0.55")),
    )
    position = _make_position_record(schema_module, entry_price=Decimal("0"))
    exit_result = _make_exit_result(schema_module)

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.FAILED
    assert result.reason == "degenerate_entry_price"
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_builds_sell_order_and_computes_amounts_without_signing():
    router, _, signer, schema_module = _build_router(
        dry_run=True,
        snapshot=SimpleNamespace(best_bid=Decimal("0.55")),
    )
    position = _make_position_record(
        schema_module,
        entry_price=Decimal("0.60"),
        order_size_usdc=Decimal("30"),
        token_id="789",
    )
    exit_result = _make_exit_result(schema_module)

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.DRY_RUN
    assert result.signed_order is None
    assert result.order_payload is not None
    assert result.order_payload.side.name == "SELL"
    assert result.order_payload.maker_amount == 50_000_000
    assert result.order_payload.taker_amount == 27_500_000
    assert isinstance(result.exit_price, Decimal)
    assert isinstance(result.order_size_usdc, Decimal)
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_live_mode_signs_and_returns_sell_routed():
    router, _, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=SimpleNamespace(best_bid=Decimal("0.55")),
    )
    position = _make_position_record(
        schema_module,
        entry_price=Decimal("0.60"),
        order_size_usdc=Decimal("30"),
        token_id="456",
    )
    exit_result = _make_exit_result(schema_module)

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.SELL_ROUTED
    assert result.signed_order is not None
    signer.sign_order.assert_called_once()


@pytest.mark.asyncio
async def test_signer_none_in_live_mode_returns_failed():
    router, _, _, schema_module = _build_router(
        dry_run=False,
        snapshot=SimpleNamespace(best_bid=Decimal("0.55")),
        signer=None,
    )
    position = _make_position_record(schema_module, token_id="111")
    exit_result = _make_exit_result(schema_module)

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.FAILED
    assert result.reason == "signer_unavailable"
    assert result.signed_order is None


@pytest.mark.asyncio
async def test_signing_exception_returns_failed_does_not_propagate():
    router, _, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=SimpleNamespace(best_bid=Decimal("0.55")),
        signing_outcome=RuntimeError("boom"),
    )
    position = _make_position_record(schema_module, token_id="222")
    exit_result = _make_exit_result(schema_module)

    result = await router.route_exit(exit_result, position)

    assert result.action == schema_module.ExitOrderAction.FAILED
    assert result.reason == "signing_error"
    assert result.signed_order is None
    signer.sign_order.assert_called_once()
