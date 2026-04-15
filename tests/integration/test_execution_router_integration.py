"""
tests/integration/test_execution_router_integration.py

RED-phase integration tests for WI-16 execution routing.

These tests define the async routing behavior before any src/
implementation changes are made.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.execution.bankroll_sync import BalanceReadResult
from src.agents.execution.polymarket_client import MarketSnapshot
from src.core.exceptions import BalanceFetchError
from src.schemas.llm import (
    LLMEvaluationResponse,
    MarketContext,
    OutcomeLabel,
    ProbabilisticEstimate,
    RecommendedAction,
    RiskAssessment,
)
from src.schemas.web3 import OrderData, OrderSide, SIGNATURE_TYPE_EOA, SignedOrder


ROUTER_MODULE_NAME = "src.agents.execution.execution_router"
SCHEMA_MODULE_NAME = "src.schemas.execution"
WALLET_ADDRESS = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
TOKEN_ID = (
    "71321045649585302271083621547358078199379994963399676385373543929026897356791"
)
DEFAULT_SIGNER = object()


def _load_contracts():
    try:
        router_module = importlib.import_module(ROUTER_MODULE_NAME)
    except ModuleNotFoundError:
        pytest.fail(
            "Expected WI-16 module src.agents.execution.execution_router to exist.",
            pytrace=False,
        )
    except Exception as exc:
        pytest.fail(
            f"Execution router module import failed unexpectedly: {exc!r}",
            pytrace=False,
        )

    try:
        schema_module = importlib.import_module(SCHEMA_MODULE_NAME)
    except ModuleNotFoundError:
        pytest.fail(
            "Expected WI-16 schema module src.schemas.execution to exist.",
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
    min_confidence: float = 0.75,
    min_ev_threshold: float = 0.02,
    kelly_fraction: float = 0.25,
    max_order_usdc: Decimal = Decimal("50"),
    max_slippage_tolerance: Decimal = Decimal("0.02"),
):
    return SimpleNamespace(
        dry_run=dry_run,
        wallet_address=WALLET_ADDRESS,
        min_confidence=min_confidence,
        min_ev_threshold=min_ev_threshold,
        kelly_fraction=kelly_fraction,
        max_order_usdc=max_order_usdc,
        max_slippage_tolerance=max_slippage_tolerance,
    )


def _base_response() -> LLMEvaluationResponse:
    market_context = MarketContext(
        condition_id=TOKEN_ID,
        outcome_evaluated=OutcomeLabel.YES,
        best_bid=0.64,
        best_ask=0.645,
        midpoint=0.6425,
        market_end_date=datetime.now(timezone.utc) + timedelta(days=10),
    )
    return LLMEvaluationResponse(
        market_context=market_context,
        probabilistic_estimate=ProbabilisticEstimate(p_true=0.80, p_market=0.45),
        risk_assessment=RiskAssessment(
            liquidity_risk_score=0.1,
            resolution_risk_score=0.1,
            information_asymmetry_flag=False,
            risk_notes=(
                "Liquidity is stable, resolution criteria are clear, and the "
                "market supports conservative execution routing."
            ),
        ),
        confidence_score=0.90,
        decision_boolean=True,
        recommended_action=RecommendedAction.BUY,
        reasoning_log=(
            "This market shows a strong positive edge, acceptable liquidity, "
            "and enough confidence to justify a carefully sized BUY order."
        ),
    )


def _make_response(**updates) -> LLMEvaluationResponse:
    return _base_response().model_copy(update=updates)


def _make_snapshot(
    *,
    best_bid: Decimal = Decimal("0.64"),
    best_ask: Decimal = Decimal("0.66"),
    midpoint: Decimal = Decimal("0.65"),
) -> MarketSnapshot:
    return MarketSnapshot(
        token_id=TOKEN_ID,
        best_bid=best_bid,
        best_ask=best_ask,
        midpoint_probability=midpoint,
        spread=best_ask - best_bid,
        fetched_at_utc=datetime.now(timezone.utc),
        source="test_order_book",
    )


def _make_balance(balance_usdc: Decimal = Decimal("1000")) -> BalanceReadResult:
    return BalanceReadResult(
        balance_usdc=balance_usdc,
        raw_balance_uint256=int(balance_usdc * Decimal("1e6")),
        wallet_address=WALLET_ADDRESS,
        block_number=123,
        fetched_at_utc=datetime.now(timezone.utc),
        is_mock=False,
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
    snapshot: MarketSnapshot | None = None,
    balance: BalanceReadResult | Exception | None = None,
    signed_order: SignedOrder | Exception | None = None,
    signer=DEFAULT_SIGNER,
    max_order_usdc: Decimal = Decimal("50"),
    max_slippage_tolerance: Decimal = Decimal("0.02"),
):
    router_module, schema_module = _load_contracts()
    config = _make_config(
        dry_run=dry_run,
        max_order_usdc=max_order_usdc,
        max_slippage_tolerance=max_slippage_tolerance,
    )

    polymarket_client = MagicMock()
    polymarket_client.fetch_order_book = AsyncMock(return_value=snapshot)

    bankroll_provider = MagicMock()
    if isinstance(balance, Exception):
        bankroll_provider.fetch_balance = AsyncMock(side_effect=balance)
    else:
        bankroll_provider.fetch_balance = AsyncMock(
            return_value=balance or _make_balance()
        )

    transaction_signer = signer
    if transaction_signer is DEFAULT_SIGNER:
        transaction_signer = MagicMock()

    if transaction_signer is not None and not hasattr(transaction_signer, "sign_order"):
        transaction_signer.sign_order = MagicMock()

    if transaction_signer is not None and signed_order is not None:
        if isinstance(signed_order, Exception):
            transaction_signer.sign_order.side_effect = signed_order
        else:
            transaction_signer.sign_order.side_effect = lambda order: signed_order

    router = router_module.ExecutionRouter(
        config=config,
        polymarket_client=polymarket_client,
        bankroll_provider=bankroll_provider,
        transaction_signer=transaction_signer,
    )
    return (
        router,
        polymarket_client,
        bankroll_provider,
        transaction_signer,
        schema_module,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "decision_boolean"),
    [
        (RecommendedAction.HOLD, False),
        (RecommendedAction.SELL, True),
    ],
)
async def test_non_buy_actions_skip_without_upstream_calls(action, decision_boolean):
    router, polymarket_client, bankroll_provider, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=_make_snapshot(),
    )
    response = _make_response(
        recommended_action=action,
        decision_boolean=decision_boolean,
    )

    result = await router.route(response, response.market_context)

    assert result.action == schema_module.ExecutionAction.SKIP
    assert result.reason == f"action_is_{action.value}"
    polymarket_client.fetch_order_book.assert_not_awaited()
    bankroll_provider.fetch_balance.assert_not_awaited()
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_buy_below_confidence_threshold_skips_without_upstream_calls():
    router, polymarket_client, bankroll_provider, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=_make_snapshot(),
    )
    response = _make_response(
        recommended_action=RecommendedAction.BUY,
        confidence_score=0.60,
    )

    result = await router.route(response, response.market_context)

    assert result.action == schema_module.ExecutionAction.SKIP
    assert result.reason == "confidence_below_threshold"
    polymarket_client.fetch_order_book.assert_not_awaited()
    bankroll_provider.fetch_balance.assert_not_awaited()
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_slippage_guard_returns_failed():
    router, _, bankroll_provider, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=_make_snapshot(best_bid=Decimal("0.64"), best_ask=Decimal("0.69")),
    )

    result = await router.route(_base_response(), _base_response().market_context)

    assert result.action == schema_module.ExecutionAction.FAILED
    assert result.reason == "slippage_exceeded"
    bankroll_provider.fetch_balance.assert_not_awaited()
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_kelly_math_uses_decimal_and_order_size_is_kelly_limited():
    router, _, _, signer, schema_module = _build_router(
        dry_run=True,
        snapshot=_make_snapshot(),
        balance=_make_balance(Decimal("100")),
        max_order_usdc=Decimal("500"),
    )

    result = await router.route(_base_response(), _base_response().market_context)

    assert result.action == schema_module.ExecutionAction.DRY_RUN
    assert result.kelly_fraction == Decimal("0.2925")
    assert result.order_size_usdc == Decimal("29.25")
    assert result.order_payload is not None
    assert result.order_payload.maker_amount == 29_250_000
    assert isinstance(result.kelly_fraction, Decimal)
    assert isinstance(result.order_size_usdc, Decimal)
    assert isinstance(result.midpoint_probability, Decimal)
    assert isinstance(result.best_ask, Decimal)
    assert isinstance(result.bankroll_usdc, Decimal)
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_kelly_without_positive_edge_returns_failed():
    router, _, bankroll_provider, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=_make_snapshot(
            best_bid=Decimal("0.01"),
            best_ask=Decimal("0.02"),
            midpoint=Decimal("0.02"),
        ),
    )

    result = await router.route(_base_response(), _base_response().market_context)

    assert result.action == schema_module.ExecutionAction.FAILED
    assert result.reason == "no_positive_edge"
    bankroll_provider.fetch_balance.assert_awaited_once()
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "midpoint",
    [Decimal("0"), Decimal("1")],
)
async def test_degenerate_midpoint_returns_failed(midpoint):
    degenerate_snapshot = SimpleNamespace(
        token_id=TOKEN_ID,
        best_bid=Decimal("0.01"),
        best_ask=Decimal("0.01"),
        midpoint_probability=midpoint,
        spread=Decimal("0"),
        fetched_at_utc=datetime.now(timezone.utc),
        source="degenerate_test_order_book",
    )
    router, _, bankroll_provider, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=degenerate_snapshot,
    )

    result = await router.route(_base_response(), _base_response().market_context)

    assert result.action == schema_module.ExecutionAction.FAILED
    assert result.reason == "degenerate_midpoint"
    bankroll_provider.fetch_balance.assert_awaited_once()
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_order_size_cap_is_applied():
    router, _, _, signer, schema_module = _build_router(
        dry_run=True,
        snapshot=_make_snapshot(),
        balance=_make_balance(Decimal("1000")),
        max_order_usdc=Decimal("50"),
    )

    result = await router.route(_base_response(), _base_response().market_context)

    assert result.action == schema_module.ExecutionAction.DRY_RUN
    assert result.order_size_usdc == Decimal("50")
    assert result.order_payload is not None
    assert result.order_payload.maker_amount == 50_000_000
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_builds_payload_without_signing():
    router, _, _, signer, schema_module = _build_router(
        dry_run=True,
        snapshot=_make_snapshot(),
    )

    result = await router.route(_base_response(), _base_response().market_context)

    assert result.action == schema_module.ExecutionAction.DRY_RUN
    assert result.order_payload is not None
    assert result.signed_order is None
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_happy_path_signs_and_returns_executed():
    unsigned_order = OrderData(
        salt=12345,
        maker=WALLET_ADDRESS,
        signer=WALLET_ADDRESS,
        taker="0x0000000000000000000000000000000000000000",
        token_id=int(TOKEN_ID),
        maker_amount=50_000_000,
        taker_amount=76_923_076,
        expiration=0,
        nonce=0,
        fee_rate_bps=0,
        side=OrderSide.BUY,
        signature_type=SIGNATURE_TYPE_EOA,
    )
    signed_order = _make_signed_order(unsigned_order)
    router, _, _, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=_make_snapshot(),
        signed_order=signed_order,
    )

    result = await router.route(_base_response(), _base_response().market_context)

    assert result.action == schema_module.ExecutionAction.EXECUTED
    assert result.signed_order == signed_order
    assert result.order_payload is not None
    signer.sign_order.assert_called_once()


@pytest.mark.asyncio
async def test_order_book_unavailable_returns_failed():
    router, _, bankroll_provider, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=None,
    )

    result = await router.route(_base_response(), _base_response().market_context)

    assert result.action == schema_module.ExecutionAction.FAILED
    assert result.reason == "order_book_unavailable"
    bankroll_provider.fetch_balance.assert_not_awaited()
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_balance_fetch_error_returns_failed():
    router, _, _, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=_make_snapshot(),
        balance=BalanceFetchError("rpc timeout", wallet_address=WALLET_ADDRESS),
    )

    result = await router.route(_base_response(), _base_response().market_context)

    assert result.action == schema_module.ExecutionAction.FAILED
    assert result.reason == "balance_fetch_error"
    signer.sign_order.assert_not_called()


@pytest.mark.asyncio
async def test_signing_error_returns_failed():
    router, _, _, signer, schema_module = _build_router(
        dry_run=False,
        snapshot=_make_snapshot(),
        signed_order=RuntimeError("signing failed"),
    )

    result = await router.route(_base_response(), _base_response().market_context)

    assert result.action == schema_module.ExecutionAction.FAILED
    assert result.reason == "signing_error"
    signer.sign_order.assert_called_once()


@pytest.mark.asyncio
async def test_signer_none_is_safe_in_dry_run_but_fails_live():
    response = _base_response()

    dry_router, _, _, _, schema_module = _build_router(
        dry_run=True,
        snapshot=_make_snapshot(),
        signer=None,
    )
    dry_result = await dry_router.route(response, response.market_context)
    assert dry_result.action == schema_module.ExecutionAction.DRY_RUN

    live_router, _, _, _, schema_module = _build_router(
        dry_run=False,
        snapshot=_make_snapshot(),
        signer=None,
    )
    live_result = await live_router.route(response, response.market_context)
    assert live_result.action == schema_module.ExecutionAction.FAILED
    assert live_result.reason == "signer_unavailable"
