"""
src/agents/execution/broadcaster.py

Web3 Execution Node.
Reads approved trades from the LLM, signs them via TransactionSigner,
and simulates POSTing the order to the Polymarket CLOB.
Records the final transaction status persistently.
"""

import asyncio
from typing import Dict, Any

import httpx
import structlog
from sqlalchemy import select

from src.agents.execution.signer import TransactionSigner
from src.db.engine import get_db_session
from src.db.models import AgentDecisionLog, ExecutionTx, TxStatus

logger = structlog.get_logger(__name__)

class TxBroadcaster:
    """
    Consumes approved decisions from the Context Node out_queue, handles signing,
    simulates network dispatch, and persists ExecutionTx audit logs.
    """

    def __init__(self, in_queue: asyncio.Queue[Dict[str, Any]], signer: TransactionSigner):
        self.in_queue = in_queue
        self.signer = signer
        self._running = False
        self.polymarket_endpoint = "https://clob.polymarket.com/order"

    async def start(self) -> None:
        """Starts the broadcaster loop."""
        self._running = True
        logger.info("Starting TxBroadcaster Node...")
        
        task = asyncio.create_task(self._consume_queue())
        try:
            while self._running:
                await asyncio.sleep(1)
        finally:
            task.cancel()

    async def stop(self) -> None:
        """Stops the broadcaster node."""
        logger.info("Stopping TxBroadcaster Node...")
        self._running = False

    async def _consume_queue(self) -> None:
        """Continuously pulls decisions and processes them asynchronously."""
        while self._running:
            try:
                decision = await self.in_queue.get()
                await self._process_decision(decision)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Unexpected error in broadcaster loop.", error=str(e))
            finally:
                self.in_queue.task_done()

    async def _process_decision(self, decision: Dict[str, Any]) -> None:
        """Handles the end-to-end execution of a single confirmed trade."""
        snapshot_id = decision.get("snapshot_id")
        
        if not snapshot_id:
            logger.error("Decision missing snapshot_id. Aborting execution.")
            return
            
        # 1. Sign the order
        signed_payload = await self.signer.sign_order(decision)
        
        # 2. Simulate dispatch to Polymarket REST API
        await self._simulate_post(signed_payload)
        
        # 3. Persist transaction to DB
        await self._persist_transaction(snapshot_id, signed_payload)

    async def _simulate_post(self, signed_payload: Dict[str, Any]) -> None:
        """Simulates network dispatch to the real Polymarket endpoint."""
        logger.info(
            "simulated_order_posted",
            endpoint=self.polymarket_endpoint,
            condition_id=signed_payload.get("condition_id"),
            side=signed_payload.get("side"),
            size=signed_payload.get("size_usdc"),
            price=signed_payload.get("limit_price")
        )
        # We perform a tiny async sleep to mock network latency
        await asyncio.sleep(0.1)

    async def _persist_transaction(self, snapshot_id: str, signed_payload: Dict[str, Any]) -> None:
        """Logs the CONFIRMED execution permanently into the database."""
        try:
            async for session in get_db_session():
                # Locate the parent AgentDecisionLog record created by ClaudeClient
                query = select(AgentDecisionLog.id).where(AgentDecisionLog.snapshot_id == snapshot_id).order_by(AgentDecisionLog.evaluated_at.desc()).limit(1)
                result = await session.execute(query)
                decision_id = result.scalar_one_or_none()
                
                if not decision_id:
                    logger.error("Parent AgentDecisionLog not found for execution phase.", snapshot_id=snapshot_id)
                    return
                
                # Build ExecutionTx
                tx = ExecutionTx(
                    decision_id=decision_id,
                    tx_hash=f"0x_mock_tx_hash_{snapshot_id[-8:]}",
                    status=TxStatus.CONFIRMED,
                    side=signed_payload["side"],
                    size_usdc=signed_payload["size_usdc"],
                    limit_price=signed_payload["limit_price"],
                    condition_id=signed_payload["condition_id"],
                    outcome_token=signed_payload["outcome_token"],
                )
                
                session.add(tx)
                await session.commit()
                
                logger.info(
                    "Execution recorded persistently.",
                    tx_status="CONFIRMED",
                    decision_id=decision_id
                )
                break
        except Exception as e:
            logger.error("Database failure while saving ExecutionTx.", error=str(e), snapshot_id=snapshot_id)
