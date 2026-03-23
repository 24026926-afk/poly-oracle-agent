"""
src/agents/execution/broadcaster.py

Order broadcaster for the Polymarket CLOB.

Orchestrates the full order lifecycle:
    SignedOrder → POST /order (CLOB REST) → poll Polygon RPC → TxReceipt

Depends on the three completed execution modules:
    - signer.py       (produces SignedOrder)
    - nonce_manager.py (dispenses sequential nonces under lock)
    - gas_estimator.py (fresh EIP-1559 gas pricing)
"""

import asyncio
from datetime import datetime, timezone

import aiohttp
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from web3 import AsyncWeb3

from src.agents.execution.gas_estimator import GasEstimator
from src.agents.execution.nonce_manager import NonceManager
from src.core.config import AppConfig
from src.core.exceptions import BroadcastError
from src.db.models import ExecutionTx, TxStatus
from src.schemas.web3 import GasPrice, SignedOrder, TxReceiptSchema

logger = structlog.get_logger(__name__)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


class OrderBroadcaster:
    """
    Submits signed orders to the Polymarket CLOB REST API, polls for
    on-chain confirmation, and persists every attempt to the DB.
    """

    def __init__(
        self,
        w3: AsyncWeb3,
        nonce_manager: NonceManager,
        gas_estimator: GasEstimator,
        http_session: aiohttp.ClientSession,
        db_session_factory: async_sessionmaker[AsyncSession],
        clob_rest_url: str,
        config: AppConfig | None = None,
        poll_max_attempts: int = 30,
        poll_delay_s: float = 2.0,
    ) -> None:
        self._w3 = w3
        self._nonce_manager = nonce_manager
        self._gas_estimator = gas_estimator
        self._http = http_session
        self._db_factory = db_session_factory
        self._clob_url = clob_rest_url.rstrip("/")
        self._config = config
        self._poll_max_attempts = poll_max_attempts
        self._poll_delay_s = poll_delay_s

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def broadcast(
        self,
        signed_order: SignedOrder,
        decision_id: str,
    ) -> TxReceiptSchema:
        """Submit *signed_order* to the CLOB and wait for confirmation."""
        if self._config is not None and self._config.dry_run:
            order = signed_order.order
            logger.info(
                "broadcaster.dry_run_skip",
                dry_run=True,
                condition_id=str(order.token_id),
                proposed_action="BUY" if order.side.value == 0 else "SELL",
                would_be_size_usdc=order.maker_amount / 1_000_000,
                decision_id=decision_id,
            )
            return TxReceiptSchema(
                order_id="dry-run",
                status="DRY_RUN",
            )

        gas: GasPrice = await self._gas_estimator.estimate()
        nonce: int = await self._nonce_manager.get_next_nonce()

        order_id = await self._submit_to_clob(signed_order, nonce, gas)

        receipt = await self._poll_receipt_safe(
            order_id, decision_id, signed_order, nonce, gas,
        )
        return receipt

    # ------------------------------------------------------------------
    # CLOB REST submission
    # ------------------------------------------------------------------

    async def _submit_to_clob(
        self,
        signed_order: SignedOrder,
        nonce: int,
        gas: GasPrice,
    ) -> str:
        """POST the order payload; return the CLOB ``orderID``."""
        url = f"{self._clob_url}/order"
        payload = signed_order.to_api_payload()

        async with self._http.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT,
        ) as resp:
            body = await resp.text()

            if resp.status >= 500:
                logger.error(
                    "broadcaster.clob_error",
                    status=resp.status,
                    body=body,
                )
                raise BroadcastError(
                    f"CLOB server error: {resp.status}",
                    status_code=resp.status,
                )

            if resp.status >= 400:
                logger.error(
                    "broadcaster.clob_error",
                    status=resp.status,
                    body=body,
                )
                await self._nonce_manager.sync()
                raise BroadcastError(
                    f"CLOB client error: {resp.status}",
                    status_code=resp.status,
                )

            data = await resp.json()
            order_id: str = data.get("orderID", "")

        logger.info(
            "broadcaster.order_submitted",
            order_id=order_id,
            nonce=nonce,
            gas_gwei=gas.max_fee_per_gas_gwei,
        )
        return order_id

    # ------------------------------------------------------------------
    # Receipt polling
    # ------------------------------------------------------------------

    async def _poll_receipt(
        self,
        order_hash: str,
        max_attempts: int = 30,
        delay_s: float = 2.0,
    ) -> TxReceiptSchema:
        """Poll Polygon RPC until the tx receipt appears or timeout."""
        for attempt in range(1, max_attempts + 1):
            try:
                receipt = await self._w3.eth.get_transaction_receipt(order_hash)
            except Exception:
                receipt = None

            if receipt is not None:
                status = "CONFIRMED" if receipt["status"] == 1 else "REVERTED"
                logger.info(
                    "broadcaster.receipt_confirmed",
                    tx_hash=order_hash,
                    block=receipt["blockNumber"],
                    gas_used=receipt["gasUsed"],
                )
                return TxReceiptSchema(
                    order_id=order_hash,
                    tx_hash=order_hash,
                    status=status,
                    gas_used=receipt["gasUsed"],
                    block_number=receipt["blockNumber"],
                )

            await asyncio.sleep(delay_s)

        logger.warning(
            "broadcaster.receipt_timeout",
            order_hash=order_hash,
            attempts=max_attempts,
        )
        raise BroadcastError(
            f"Receipt timeout after {max_attempts * delay_s:.0f}s"
        )

    async def _poll_receipt_safe(
        self,
        order_id: str,
        decision_id: str,
        signed_order: SignedOrder,
        nonce: int,
        gas: GasPrice,
    ) -> TxReceiptSchema:
        """Wrap _poll_receipt so we always persist an ExecutionTx row."""
        order = signed_order.order
        try:
            receipt = await self._poll_receipt(
                order_id,
                max_attempts=self._poll_max_attempts,
                delay_s=self._poll_delay_s,
            )
            await self._persist_tx(
                decision_id=decision_id,
                signed_order=signed_order,
                nonce=nonce,
                gas=gas,
                tx_hash=receipt.tx_hash,
                status=TxStatus.CONFIRMED if receipt.status == "CONFIRMED" else TxStatus.REVERTED,
                gas_used=receipt.gas_used,
                block_number=receipt.block_number,
            )
            return receipt
        except BroadcastError:
            # Timeout — persist as PENDING, then re-raise.
            await self._persist_tx(
                decision_id=decision_id,
                signed_order=signed_order,
                nonce=nonce,
                gas=gas,
                tx_hash=None,
                status=TxStatus.PENDING,
                gas_used=None,
                block_number=None,
                error_message="Receipt polling timed out",
            )
            raise

    # ------------------------------------------------------------------
    # DB persistence
    # ------------------------------------------------------------------

    async def _persist_tx(
        self,
        decision_id: str,
        signed_order: SignedOrder,
        nonce: int,
        gas: GasPrice,
        tx_hash: str | None,
        status: TxStatus,
        gas_used: int | None,
        block_number: int | None,
        error_message: str | None = None,
    ) -> None:
        order = signed_order.order
        row = ExecutionTx(
            decision_id=decision_id,
            tx_hash=tx_hash,
            status=status,
            side="BUY" if order.side.value == 0 else "SELL",
            size_usdc=order.maker_amount / 1_000_000,
            limit_price=0.0,  # CLOB manages price matching
            condition_id=str(order.token_id),
            outcome_token="YES",
            gas_limit=None,
            gas_price_gwei=gas.max_fee_per_gas_gwei,
            gas_used=gas_used,
            nonce=nonce,
            block_number=block_number,
            error_message=error_message,
            confirmed_at=datetime.now(timezone.utc) if status == TxStatus.CONFIRMED else None,
        )

        async with self._db_factory() as session:
            session.add(row)
            await session.commit()
