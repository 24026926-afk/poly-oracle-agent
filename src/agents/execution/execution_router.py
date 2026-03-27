"""
src/agents/execution/execution_router.py

WI-16 execution router that turns a validated BUY decision into an
unsigned or signed Polymarket order payload.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import secrets
from typing import Any

import structlog

from src.agents.execution.bankroll_sync import BankrollSyncProvider
from src.agents.execution.polymarket_client import PolymarketClient
from src.agents.execution.signer import TransactionSigner
from src.core.config import AppConfig
from src.core.exceptions import (
    BalanceFetchError,
    RoutingAbortedError,
    RoutingRejectedError,
    SlippageExceededError,
)
from src.schemas.execution import ExecutionAction, ExecutionResult
from src.schemas.llm import LLMEvaluationResponse, MarketContext, RecommendedAction
from src.schemas.web3 import OrderData, OrderSide, SIGNATURE_TYPE_EOA

logger = structlog.get_logger(__name__)

_USDC_SCALE = Decimal("1e6")
_ONE = Decimal("1")
_ZERO = Decimal("0")


class ExecutionRouter:
    """Async orchestrator for WI-16 execution routing."""

    def __init__(
        self,
        config: AppConfig,
        polymarket_client: PolymarketClient,
        bankroll_provider: BankrollSyncProvider,
        transaction_signer: TransactionSigner | None,
    ) -> None:
        self._config = config
        self._polymarket_client = polymarket_client
        self._bankroll_provider = bankroll_provider
        self._transaction_signer = transaction_signer

    async def route(
        self,
        response: LLMEvaluationResponse,
        market_context: MarketContext,
    ) -> ExecutionResult:
        """Convert a validated BUY decision into a routed execution result."""
        routed_at_utc = datetime.now(timezone.utc)
        token_id = str(market_context.condition_id)
        action = response.recommended_action

        if action != RecommendedAction.BUY:
            rejected = RoutingRejectedError(
                reason=f"action_is_{action.value}",
                token_id=token_id,
                action=action.value,
            )
            logger.info(
                "execution_router.skipped_non_buy",
                token_id=token_id,
                reason=rejected.reason,
            )
            return self._result(
                action=ExecutionAction.SKIP,
                reason=rejected.reason,
                routed_at_utc=routed_at_utc,
            )

        if response.confidence_score < self._config.min_confidence:
            rejected = RoutingRejectedError(
                reason="confidence_below_threshold",
                token_id=token_id,
                confidence_score=response.confidence_score,
                min_confidence=self._config.min_confidence,
            )
            logger.info(
                "execution_router.skipped_low_confidence",
                token_id=token_id,
                confidence_score=response.confidence_score,
                min_confidence=self._config.min_confidence,
            )
            return self._result(
                action=ExecutionAction.SKIP,
                reason=rejected.reason,
                routed_at_utc=routed_at_utc,
            )

        snapshot = await self._polymarket_client.fetch_order_book(token_id)
        if snapshot is None:
            aborted = RoutingAbortedError(
                reason="order_book_unavailable",
                token_id=token_id,
            )
            logger.warning(
                "execution_router.order_book_unavailable",
                token_id=token_id,
            )
            return self._result(
                action=ExecutionAction.FAILED,
                reason=aborted.reason,
                routed_at_utc=routed_at_utc,
            )

        midpoint = Decimal(str(snapshot.midpoint_probability))
        best_ask = Decimal(str(snapshot.best_ask))
        slippage_tolerance = Decimal(str(self._config.max_slippage_tolerance))
        slippage_limit = midpoint + slippage_tolerance
        if best_ask > slippage_limit:
            slippage_error = SlippageExceededError(
                reason="slippage_exceeded",
                token_id=token_id,
                best_ask=str(best_ask),
                midpoint=str(midpoint),
                tolerance=str(slippage_tolerance),
            )
            logger.warning(
                "execution_router.slippage_exceeded",
                token_id=token_id,
                reason=slippage_error.reason,
                best_ask=str(best_ask),
                midpoint=str(midpoint),
                tolerance=str(slippage_tolerance),
            )
            return self._result(
                action=ExecutionAction.FAILED,
                reason=slippage_error.reason,
                midpoint_probability=midpoint,
                best_ask=best_ask,
                routed_at_utc=routed_at_utc,
            )

        try:
            balance_result = await self._bankroll_provider.fetch_balance()
        except BalanceFetchError as exc:
            aborted = RoutingAbortedError(
                reason="balance_fetch_error",
                token_id=token_id,
                cause=exc,
            )
            logger.error(
                "execution_router.balance_fetch_error",
                token_id=token_id,
                reason=aborted.reason,
                error=str(exc),
            )
            return self._result(
                action=ExecutionAction.FAILED,
                reason=aborted.reason,
                midpoint_probability=midpoint,
                best_ask=best_ask,
                routed_at_utc=routed_at_utc,
            )

        bankroll_usdc = self._extract_balance_usdc(balance_result)

        if midpoint <= _ZERO or midpoint >= _ONE:
            rejected = RoutingRejectedError(
                reason="degenerate_midpoint",
                token_id=token_id,
                midpoint=str(midpoint),
            )
            logger.warning(
                "execution_router.degenerate_midpoint",
                token_id=token_id,
                midpoint=str(midpoint),
            )
            return self._result(
                action=ExecutionAction.FAILED,
                reason=rejected.reason,
                midpoint_probability=midpoint,
                best_ask=best_ask,
                bankroll_usdc=bankroll_usdc,
                routed_at_utc=routed_at_utc,
            )

        threshold = Decimal(str(self._config.min_ev_threshold))
        edge = midpoint - threshold
        if edge <= _ZERO:
            rejected = RoutingRejectedError(
                reason="no_positive_edge",
                token_id=token_id,
                midpoint=str(midpoint),
                threshold=str(threshold),
            )
            logger.warning(
                "execution_router.no_positive_edge",
                token_id=token_id,
                midpoint=str(midpoint),
                threshold=str(threshold),
            )
            return self._result(
                action=ExecutionAction.FAILED,
                reason=rejected.reason,
                midpoint_probability=midpoint,
                best_ask=best_ask,
                bankroll_usdc=bankroll_usdc,
                routed_at_utc=routed_at_utc,
            )

        odds = (_ONE - midpoint) / midpoint
        kelly_raw = edge / odds
        kelly_scaled = kelly_raw * Decimal(str(self._config.kelly_fraction))
        order_size = min(
            kelly_scaled * bankroll_usdc,
            Decimal(str(self._config.max_order_usdc)),
        )

        if order_size <= _ZERO:
            rejected = RoutingRejectedError(
                reason="non_positive_order_size",
                token_id=token_id,
                order_size=str(order_size),
            )
            logger.warning(
                "execution_router.non_positive_order_size",
                token_id=token_id,
                order_size=str(order_size),
            )
            return self._result(
                action=ExecutionAction.FAILED,
                reason=rejected.reason,
                kelly_fraction=kelly_scaled,
                order_size_usdc=order_size,
                midpoint_probability=midpoint,
                best_ask=best_ask,
                bankroll_usdc=bankroll_usdc,
                routed_at_utc=routed_at_utc,
            )

        order_data = self._build_order_data(
            market_context=market_context,
            midpoint=midpoint,
            order_size=order_size,
        )

        if self._config.dry_run:
            logger.info(
                "execution_router.dry_run_order_built",
                dry_run=True,
                token_id=token_id,
                side=order_data.side.name,
                maker_amount=order_data.maker_amount,
                taker_amount=order_data.taker_amount,
                order_size_usdc=str(order_size),
                kelly_fraction=str(kelly_scaled),
            )
            return self._result(
                action=ExecutionAction.DRY_RUN,
                order_payload=order_data,
                kelly_fraction=kelly_scaled,
                order_size_usdc=order_size,
                midpoint_probability=midpoint,
                best_ask=best_ask,
                bankroll_usdc=bankroll_usdc,
                routed_at_utc=routed_at_utc,
            )

        if self._transaction_signer is None:
            aborted = RoutingAbortedError(
                reason="signer_unavailable",
                token_id=token_id,
            )
            logger.error(
                "execution_router.signer_unavailable",
                token_id=token_id,
            )
            return self._result(
                action=ExecutionAction.FAILED,
                reason=aborted.reason,
                order_payload=order_data,
                kelly_fraction=kelly_scaled,
                order_size_usdc=order_size,
                midpoint_probability=midpoint,
                best_ask=best_ask,
                bankroll_usdc=bankroll_usdc,
                routed_at_utc=routed_at_utc,
            )

        try:
            signed_order = self._transaction_signer.sign_order(order_data)
        except Exception as exc:
            aborted = RoutingAbortedError(
                reason="signing_error",
                token_id=token_id,
                cause=exc,
            )
            logger.error(
                "execution_router.signing_error",
                token_id=token_id,
                error=str(exc),
            )
            return self._result(
                action=ExecutionAction.FAILED,
                reason=aborted.reason,
                order_payload=order_data,
                kelly_fraction=kelly_scaled,
                order_size_usdc=order_size,
                midpoint_probability=midpoint,
                best_ask=best_ask,
                bankroll_usdc=bankroll_usdc,
                routed_at_utc=routed_at_utc,
            )

        logger.info(
            "execution_router.executed",
            token_id=token_id,
            side=order_data.side.name,
            maker_amount=order_data.maker_amount,
            taker_amount=order_data.taker_amount,
            order_size_usdc=str(order_size),
            kelly_fraction=str(kelly_scaled),
        )
        return self._result(
            action=ExecutionAction.EXECUTED,
            order_payload=order_data,
            signed_order=signed_order,
            kelly_fraction=kelly_scaled,
            order_size_usdc=order_size,
            midpoint_probability=midpoint,
            best_ask=best_ask,
            bankroll_usdc=bankroll_usdc,
            routed_at_utc=routed_at_utc,
        )

    def _build_order_data(
        self,
        *,
        market_context: MarketContext,
        midpoint: Decimal,
        order_size: Decimal,
    ) -> OrderData:
        maker_amount = int(order_size * _USDC_SCALE)
        taker_amount = 0
        if midpoint > _ZERO:
            taker_amount = int((order_size / midpoint) * _USDC_SCALE)

        return OrderData(
            salt=secrets.randbits(256),
            maker=self._config.wallet_address,
            signer=self._config.wallet_address,
            taker="0x0000000000000000000000000000000000000000",
            token_id=int(str(market_context.condition_id), 0),
            maker_amount=maker_amount,
            taker_amount=taker_amount,
            expiration=0,
            nonce=0,
            fee_rate_bps=0,
            side=OrderSide.BUY,
            signature_type=SIGNATURE_TYPE_EOA,
        )

    @staticmethod
    def _extract_balance_usdc(balance_result: Any) -> Decimal:
        if isinstance(balance_result, Decimal):
            return balance_result
        if hasattr(balance_result, "balance_usdc"):
            balance_usdc = getattr(balance_result, "balance_usdc")
            if isinstance(balance_usdc, Decimal):
                return balance_usdc
            return Decimal(str(balance_usdc))
        raise TypeError("Unsupported bankroll balance result")

    @staticmethod
    def _result(
        *,
        action: ExecutionAction,
        routed_at_utc: datetime,
        reason: str | None = None,
        order_payload: OrderData | None = None,
        signed_order: Any | None = None,
        kelly_fraction: Decimal | None = None,
        order_size_usdc: Decimal | None = None,
        midpoint_probability: Decimal | None = None,
        best_ask: Decimal | None = None,
        bankroll_usdc: Decimal | None = None,
    ) -> ExecutionResult:
        return ExecutionResult(
            action=action,
            reason=reason,
            order_payload=order_payload,
            signed_order=signed_order,
            kelly_fraction=kelly_fraction,
            order_size_usdc=order_size_usdc,
            midpoint_probability=midpoint_probability,
            best_ask=best_ask,
            bankroll_usdc=bankroll_usdc,
            routed_at_utc=routed_at_utc,
        )
