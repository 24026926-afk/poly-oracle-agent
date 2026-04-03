"""
src/agents/context/aggregator.py

Context Builder module.
Maintains an in-memory representation of the orderbook and pushes market
summaries to the LLM Evaluation Node based on time or price triggers.
"""

import asyncio
import uuid
import time
from typing import Dict, Any, Optional

import structlog

from src.schemas.market import CLOBMessage
from src.agents.context.prompt_factory import PromptFactory

logger = structlog.get_logger(__name__)

class DataAggregator:
    """
    Reads live CLOB messages, maintains the current orderbook state, and
    emits a complete market summary either every 10 seconds or when a
    significant price change (> 2%) is detected.
    """
    
    def __init__(
        self, 
        input_queue: asyncio.Queue[CLOBMessage], 
        output_queue: asyncio.Queue[Dict[str, Any]],
        condition_id: str
    ):
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.condition_id = condition_id
        
        # State
        self._running = False
        self._last_emit_time = 0.0
        self._last_emitted_midpoint: Optional[float] = None
        
        # In-memory orderbook (simplified: keeping best bid/ask for now)
        self.best_bid = 0.0
        self.best_ask = 0.0
        
        # Configurable triggers
        self.TIME_INTERVAL_SEC = 10.0
        self.PRICE_CHANGE_THRESHOLD = 0.02  # 2%

    async def start(self) -> None:
        """Starts the aggregation loop processing incoming messages."""
        self._running = True
        logger.info("Starting DataAggregator...", condition_id=self.condition_id)
        
        # Start a background task to enforce the 10-second time trigger
        # in case no messages arrive from the WebSocket.
        timer_task = asyncio.create_task(self._timer_loop())
        
        try:
            while self._running:
                msg = await self.input_queue.get()
                await self._process_message(msg)
                self.input_queue.task_done()
        finally:
            timer_task.cancel()

    async def stop(self) -> None:
        """Stops the aggregator."""
        self._running = False
        logger.info("Stopping DataAggregator...")

    async def _process_message(self, msg: object) -> None:
        """Updates internal state based on a new market message.

        Accepts both ``MarketSnapshot`` ORM objects (from ``ws_client``)
        and legacy ``CLOBMessage`` Pydantic models.
        """
        msg_cid = getattr(msg, "condition_id", None)
        if msg_cid is not None and msg_cid != self.condition_id:
            return

        updated = False

        # MarketSnapshot ORM objects carry best_bid/best_ask directly.
        # Legacy CLOBMessage objects carry bids/asks lists.
        best_bid = getattr(msg, "best_bid", None)
        best_ask = getattr(msg, "best_ask", None)

        if best_bid is not None and best_bid > 0:
            self.best_bid = float(best_bid)
            updated = True

        if best_ask is not None and best_ask > 0:
            self.best_ask = float(best_ask)
            updated = True

        if updated:
            await self._check_triggers()

    async def _timer_loop(self) -> None:
        """Background loop to force an emit every TIME_INTERVAL_SEC."""
        while self._running:
            await asyncio.sleep(1.0)
            now = time.time()
            if now - self._last_emit_time >= self.TIME_INTERVAL_SEC:
                # Only emit if we actually have some data
                if self.best_bid > 0 and self.best_ask > 0:
                    logger.debug("Time trigger activated.")
                    await self._emit_state()

    async def _check_triggers(self) -> None:
        """Evaluates if the price change warrants an immediate LLM evaluation."""
        if self.best_bid <= 0 or self.best_ask <= 0:
            return
            
        current_midpoint = (self.best_bid + self.best_ask) / 2.0
        now = time.time()
        
        # Trigger 1: Time elapsed > 10s
        time_elapsed = (now - self._last_emit_time >= self.TIME_INTERVAL_SEC)
        
        # Trigger 2: Price changed > 2% since last emit
        price_moved = False
        if self._last_emitted_midpoint is not None and self._last_emitted_midpoint > 0:
            change = abs(current_midpoint - self._last_emitted_midpoint) / self._last_emitted_midpoint
            if change >= self.PRICE_CHANGE_THRESHOLD:
                price_moved = True
                logger.debug("Volatility trigger activated.", price_change_pct=change)
                
        if time_elapsed or price_moved:
            await self._emit_state(current_midpoint)

    async def _emit_state(self, current_midpoint: Optional[float] = None) -> None:
        """Builds the market summary dictionary and pushes it to the output queue."""
        if current_midpoint is None:
            current_midpoint = (self.best_bid + self.best_ask) / 2.0
            
        spread = self.best_ask - self.best_bid
        
        state = {
            "condition_id": self.condition_id,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "midpoint": current_midpoint,
            "spread": spread,
            "timestamp": time.time()
        }
        
        self._last_emit_time = time.time()
        self._last_emitted_midpoint = current_midpoint
        
        prompt = PromptFactory.build_evaluation_prompt(state)
        
        output_payload = {
            "snapshot_id": str(uuid.uuid4()),
            "prompt": prompt,
            "state": state
        }
        
        logger.info("Emitting market state and CoT prompt.", midpoint=current_midpoint, spread=spread)
        await self.output_queue.put(output_payload)
