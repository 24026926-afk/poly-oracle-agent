"""
src/agents/context/aggregator.py

Context Builder module.
Maintains an in-memory representation of the orderbook and pushes market
summaries to the LLM Evaluation Node based on time or price triggers.
"""

import asyncio
import uuid
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

import structlog

from src.schemas.market import CLOBMessage, PerMarketAggregatorState
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
        self._yes_token_id: Optional[str] = None

        # In-memory orderbook (simplified: keeping best bid/ask for now)
        self.best_bid = 0.0
        self.best_ask = 0.0
        
        # Configurable triggers
        self.TIME_INTERVAL_SEC = 30.0
        self.PRICE_CHANGE_THRESHOLD = 0.01  # 1%

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

        # Capture yes_token_id from the snapshot if present
        msg_yes_token_id = getattr(msg, "yes_token_id", None)
        if msg_yes_token_id is not None:
            self._yes_token_id = msg_yes_token_id

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
            "state": state,
            "yes_token_id": self._yes_token_id,
        }
        
        logger.debug("Emitting market state and CoT prompt.", midpoint=current_midpoint, spread=spread)
        await self.output_queue.put(output_payload)

    # ------------------------------------------------------------------
    # WI-32: Concurrent multi-market tracking
    # ------------------------------------------------------------------

    async def track_market(self, token_ids: List[str]) -> List[Dict[str, Any]]:
        """Track a single market concurrently (accepts list of token IDs for WI-32).

        Manages per-market subscription state via PerMarketAggregatorState.
        Produces MarketContext to shared prompt_queue.
        """
        state = PerMarketAggregatorState(token_ids=token_ids)

        # Register with WS client for frame routing
        ws_client = getattr(self, "ws_client", None)
        if ws_client is not None:
            for token_id in token_ids:
                ws_client.register_aggregator(token_id, self)

            # Subscribe via multiplexed batch
            await ws_client.subscribe_batch(token_ids)

        # Build market context
        market_contexts: List[Dict[str, Any]] = []
        try:
            context = self._build_market_context(token_ids, state)
            market_contexts.append(context)

            # Produce to shared prompt_queue
            prompt_queue = getattr(self, "prompt_queue", None)
            if prompt_queue is not None:
                await prompt_queue.put(context)
        except Exception as exc:
            logger.error("aggregator.track_market_error", error=str(exc))
            raise

        return market_contexts

    def _build_market_context(
        self,
        token_ids: List[str],
        state: PerMarketAggregatorState,
    ) -> Dict[str, Any]:
        """Build a MarketContext dictionary for the evaluation pipeline."""
        current_midpoint = (
            (self.best_bid + self.best_ask) / 2.0
            if self.best_bid > 0 and self.best_ask > 0
            else 0.0
        )
        spread = self.best_ask - self.best_bid

        state_dict = {
            "condition_id": self.condition_id,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "midpoint": current_midpoint,
            "spread": spread,
            "timestamp": time.time(),
            "token_ids": token_ids,
            "subscription_status": state.subscription_status,
            "frame_count": state.frame_count,
        }

        prompt = PromptFactory.build_evaluation_prompt(state_dict)

        return {
            "snapshot_id": str(uuid.uuid4()),
            "prompt": prompt,
            "state": state_dict,
            "yes_token_id": self._yes_token_id,
        }
