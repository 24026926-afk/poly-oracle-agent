"""
src/agents/ingestion/ws_client.py

Asynchronous WebSocket client for connecting to the Polymarket CLOB.
Ingests live orderbook updates and enqueues them for the Context Builder to process.
"""

import asyncio
import json
from typing import Any

import structlog
import websockets
from pydantic import ValidationError

from src.schemas.market import CLOBMessage

# Configure structlog for this module
logger = structlog.get_logger(__name__)

# Official Polymarket CLOB WebSocket endpoint
POLY_CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

class AsyncWebSocketClient:
    """
    Connects to the Polymarket CLOB WebSocket, subscribes to a specific market,
    parses incoming messages safely, and enqueues them for downstream processing.
    """

    def __init__(self, queue: asyncio.Queue[Any], condition_id: str):
        self.queue = queue
        self.condition_id = condition_id
        self._running = False
        self._ws: websockets.WebSocketClientProtocol | None = None

    async def start(self) -> None:
        """
        Connects to the WebSocket, sends the subscription message,
        and starts the infinite loop to receive and process messages.
        """
        self._running = True
        
        while self._running:
            try:
                logger.info("Connecting to Polymarket CLOB WebSocket...", url=POLY_CLOB_WS_URL)
                
                async with websockets.connect(POLY_CLOB_WS_URL) as ws:
                    self._ws = ws
                    logger.info("Connection established. Sending subscription.", condition_id=self.condition_id)
                    
                    # Send subscription payload
                    sub_message = {
                        "assets_ids": [self.condition_id],
                        "type": "market"
                    }
                    await ws.send(json.dumps(sub_message))
                    
                    # Process incoming messages
                    await self._receive_loop(ws)
                    
            except websockets.ConnectionClosed as e:
                logger.warning("WebSocket connection closed. Attempting reconnect...", code=e.code, reason=e.reason)
            except Exception as e:
                logger.error("Unexpected WebSocket error. Retrying in 5 seconds...", error=str(e))
                await asyncio.sleep(5)
            
            # Brief pause before reconnecting
            if self._running:
                await asyncio.sleep(2)

    async def stop(self) -> None:
        """Gracefully stops the WebSocket client."""
        logger.info("Stopping WebSocket client...")
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _receive_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """
        Infinite loop to receive messages from the open socket.
        """
        async for raw_msg in ws:
            if not self._running:
                break
                
            try:
                logger.debug("Raw message received", payload_preview=raw_msg[:200])
                
                # 1. Parse JSON
                data = json.loads(raw_msg)
                
                # Polymarket sometimes sends purely informational or error frames.
                # Skip if it doesn't look like a market data event.
                if "event" not in data or "market" not in data:
                    continue
                    
                # 2. Validate via Pydantic
                clob_msg = CLOBMessage.model_validate(data)
                
                # 3. Log significant data (e.g., best bid/ask changes)
                best_bid = clob_msg.bids[0].price if clob_msg.bids else None
                best_ask = clob_msg.asks[0].price if clob_msg.asks else None
                
                logger.debug("Received CLOB payload", 
                             event=clob_msg.event, 
                             best_bid=best_bid, 
                             best_ask=best_ask)
                
                # 4. Enqueue for downstream consumption
                # Downstream will map this CLOBMessage to a complete MarketSnapshot
                await self.queue.put(clob_msg)
                
            except json.JSONDecodeError:
                logger.error("Received malformed JSON payload.", payload=raw_msg[:100])
            except ValidationError as e:
                logger.warning("Message failed Pydantic validation.", error=str(e), payload=raw_msg[:100])
            except Exception as e:
                logger.error("Error processing message.", error=str(e))
