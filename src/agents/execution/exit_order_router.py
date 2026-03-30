"""
src/agents/execution/exit_order_router.py

WI-20 exit order router that converts actionable ExitResult entries into
SELL-side order payloads for Polymarket CLOB submission.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import secrets

import structlog

from src.agents.execution.polymarket_client import PolymarketClient
from src.agents.execution.signer import TransactionSigner
from src.core.config import AppConfig
from src.core.exceptions import ExitRoutingError
from src.schemas.execution import (
    ExitOrderAction,
    ExitOrderResult,
    ExitReason,
    ExitResult,
)
from src.schemas.position import PositionRecord
from src.schemas.web3 import (
    OrderData,
    OrderSide,
    SIGNATURE_TYPE_EOA,
    SignedOrder,
)

logger = structlog.get_logger(__name__)

_USDC_SCALE = Decimal("1e6")
_ZERO = Decimal("0")


class ExitOrderRouter:
    """Async orchestrator for WI-20 exit-side order routing."""

    def __init__(
        self,
        config: AppConfig,
        polymarket_client: PolymarketClient,
        transaction_signer: TransactionSigner | None,
    ) -> None:
        self._config = config
        self._polymarket_client = polymarket_client
        self._transaction_signer = transaction_signer

    async def route_exit(
        self,
        exit_result: ExitResult,
        position: PositionRecord,
    ) -> ExitOrderResult:
        """Route an actionable exit decision into a SELL-side order payload."""
        routed_at_utc = datetime.now(timezone.utc)

        if not exit_result.should_exit:
            logger.info(
                "exit_order_router.skipped",
                position_id=position.id,
                reason="should_exit_is_false",
            )
            return ExitOrderResult(
                position_id=position.id,
                condition_id=position.condition_id,
                action=ExitOrderAction.SKIP,
                reason="should_exit_is_false",
                routed_at_utc=routed_at_utc,
            )

        if exit_result.exit_reason == ExitReason.ERROR:
            logger.info(
                "exit_order_router.skipped",
                position_id=position.id,
                reason="exit_reason_is_error",
            )
            return ExitOrderResult(
                position_id=position.id,
                condition_id=position.condition_id,
                action=ExitOrderAction.SKIP,
                reason="exit_reason_is_error",
                routed_at_utc=routed_at_utc,
            )

        snapshot = await self._polymarket_client.fetch_order_book(position.token_id)
        if snapshot is None:
            unavailable_error = ExitRoutingError(
                reason="order_book_unavailable",
                position_id=position.id,
                condition_id=position.condition_id,
            )
            logger.warning(
                "exit_order_router.order_book_unavailable",
                position_id=position.id,
                token_id=position.token_id,
            )
            return ExitOrderResult(
                position_id=position.id,
                condition_id=position.condition_id,
                action=ExitOrderAction.FAILED,
                reason=unavailable_error.reason,
                routed_at_utc=routed_at_utc,
            )

        best_bid = Decimal(str(snapshot.best_bid))
        min_bid = Decimal(str(self._config.exit_min_bid_tolerance))
        if best_bid < min_bid:
            tolerance_error = ExitRoutingError(
                reason="exit_bid_below_tolerance",
                position_id=position.id,
                condition_id=position.condition_id,
                best_bid=str(best_bid),
                min_bid=str(min_bid),
            )
            logger.warning(
                "exit_order_router.exit_bid_below_tolerance",
                position_id=position.id,
                best_bid=str(best_bid),
                tolerance=str(min_bid),
            )
            return ExitOrderResult(
                position_id=position.id,
                condition_id=position.condition_id,
                action=ExitOrderAction.FAILED,
                reason=tolerance_error.reason,
                exit_price=best_bid,
                routed_at_utc=routed_at_utc,
            )

        order_size_usdc = Decimal(str(position.order_size_usdc))
        entry_price = Decimal(str(position.entry_price))
        if entry_price <= _ZERO:
            degenerate_error = ExitRoutingError(
                reason="degenerate_entry_price",
                position_id=position.id,
                condition_id=position.condition_id,
                entry_price=str(entry_price),
            )
            logger.warning(
                "exit_order_router.degenerate_entry_price",
                position_id=position.id,
                entry_price=str(entry_price),
            )
            return ExitOrderResult(
                position_id=position.id,
                condition_id=position.condition_id,
                action=ExitOrderAction.FAILED,
                reason=degenerate_error.reason,
                exit_price=best_bid,
                order_size_usdc=order_size_usdc,
                routed_at_utc=routed_at_utc,
            )

        token_quantity = order_size_usdc / entry_price
        maker_amount = int(token_quantity * _USDC_SCALE)
        taker_amount = int((token_quantity * best_bid) * _USDC_SCALE)

        order_data = OrderData(
            salt=secrets.randbits(256),
            maker=self._config.wallet_address,
            signer=self._config.wallet_address,
            taker="0x0000000000000000000000000000000000000000",
            token_id=int(position.token_id),
            maker_amount=maker_amount,
            taker_amount=taker_amount,
            expiration=0,
            nonce=0,
            fee_rate_bps=0,
            side=OrderSide.SELL,
            signature_type=SIGNATURE_TYPE_EOA,
        )

        if self._config.dry_run:
            logger.info(
                "exit_order_router.dry_run_order_built",
                dry_run=True,
                position_id=position.id,
                condition_id=position.condition_id,
                side=order_data.side.name,
                maker_amount=order_data.maker_amount,
                taker_amount=order_data.taker_amount,
                exit_price=str(best_bid),
                order_size_usdc=str(order_size_usdc),
            )
            return ExitOrderResult(
                position_id=position.id,
                condition_id=position.condition_id,
                action=ExitOrderAction.DRY_RUN,
                order_payload=order_data,
                signed_order=None,
                exit_price=best_bid,
                order_size_usdc=order_size_usdc,
                routed_at_utc=routed_at_utc,
            )

        if self._transaction_signer is None:
            unavailable_signer = ExitRoutingError(
                reason="signer_unavailable",
                position_id=position.id,
                condition_id=position.condition_id,
            )
            logger.error(
                "exit_order_router.signer_unavailable",
                position_id=position.id,
                condition_id=position.condition_id,
            )
            return ExitOrderResult(
                position_id=position.id,
                condition_id=position.condition_id,
                action=ExitOrderAction.FAILED,
                reason=unavailable_signer.reason,
                order_payload=order_data,
                exit_price=best_bid,
                order_size_usdc=order_size_usdc,
                routed_at_utc=routed_at_utc,
            )

        try:
            signed_order: SignedOrder = self._transaction_signer.sign_order(order_data)
        except Exception as exc:
            signing_error = ExitRoutingError(
                reason="signing_error",
                position_id=position.id,
                condition_id=position.condition_id,
                cause=exc,
            )
            logger.error(
                "exit_order_router.signing_error",
                position_id=position.id,
                condition_id=position.condition_id,
                error=str(exc),
            )
            return ExitOrderResult(
                position_id=position.id,
                condition_id=position.condition_id,
                action=ExitOrderAction.FAILED,
                reason=signing_error.reason,
                order_payload=order_data,
                exit_price=best_bid,
                order_size_usdc=order_size_usdc,
                routed_at_utc=routed_at_utc,
            )

        logger.info(
            "exit_order_router.sell_routed",
            position_id=position.id,
            condition_id=position.condition_id,
            side=order_data.side.name,
            maker_amount=order_data.maker_amount,
            taker_amount=order_data.taker_amount,
            exit_price=str(best_bid),
            order_size_usdc=str(order_size_usdc),
        )
        return ExitOrderResult(
            position_id=position.id,
            condition_id=position.condition_id,
            action=ExitOrderAction.SELL_ROUTED,
            order_payload=order_data,
            signed_order=signed_order,
            exit_price=best_bid,
            order_size_usdc=order_size_usdc,
            routed_at_utc=routed_at_utc,
        )
