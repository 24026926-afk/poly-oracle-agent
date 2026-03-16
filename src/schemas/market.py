"""
src/schemas/market.py

Pydantic V2 schemas for validating Polymarket CLOB WebSocket data.
"""

from typing import List, Optional
from pydantic import BaseModel, Field

class CLOBTick(BaseModel):
    """
    Represents a single price level in the orderbook.
    Polymarket CLOB sends these as strings, so Pydantic will coerce them to floats.
    """
    price: float = Field(..., description="The limit price of the order")
    size: float = Field(..., description="The quantity available at this price")

    model_config = {
        "frozen": True,
    }

class CLOBMessage(BaseModel):
    """
    Message structure received from the Polymarket CLOB WebSocket.
    """
    event: str = Field(..., description="Event type, e.g., 'book' or 'price_change'")
    market: str = Field(..., description="The condition_id representing the market")
    bids: Optional[List[CLOBTick]] = Field(default=None, description="List of bid price levels")
    asks: Optional[List[CLOBTick]] = Field(default=None, description="List of ask price levels")

    model_config = {
        "frozen": True,
        # Allow extra fields since the WebSocket may send additional metadata
        # like timestamps or sequence numbers that we might not strictly need right now.
        "extra": "ignore",
    }
