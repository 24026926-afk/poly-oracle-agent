"""
src/agents/ingestion/ws_client.py

CLOB WebSocket client for streaming live orderbook events from Polymarket.

Connects to the CLOB WebSocket, validates incoming frames via
``MarketSnapshotSchema``, persists to the DB, and feeds an
``asyncio.Queue`` consumed by the Context Builder (Module 2).
"""

import asyncio
import json

import structlog
import websockets
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import AppConfig
from src.db.models import MarketSnapshot
from src.schemas.market import MarketSnapshotSchema

logger = structlog.get_logger(__name__)

_VALID_EVENTS = {"book", "price_change", "last_trade_price"}
_HEARTBEAT_INTERVAL_S = 10


class CLOBWebSocketClient:
    """Streams live CLOB orderbook events and enqueues MarketSnapshots."""

    def __init__(
        self,
        config: AppConfig,
        queue: asyncio.Queue[MarketSnapshot],
        db_session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._url = config.clob_ws_url
        self._queue = queue
        self._db_factory = db_session_factory

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect, subscribe, and stream forever with backoff reconnect."""
        backoff_s = 1.0
        max_backoff_s = 60.0

        while True:
            try:
                await self._stream()
                # _stream only returns on clean shutdown
                backoff_s = 1.0
            except websockets.ConnectionClosed as exc:
                logger.warning(
                    "ws_client.disconnected",
                    code=exc.code,
                    reason=exc.reason,
                    reconnect_in=backoff_s,
                )
            except Exception as exc:
                logger.error(
                    "ws_client.connection_error",
                    error=str(exc),
                    reconnect_in=backoff_s,
                )

            await asyncio.sleep(backoff_s)
            backoff_s = min(backoff_s * 2, max_backoff_s)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _stream(self) -> None:
        async with websockets.connect(self._url) as ws:
            logger.info("ws_client.connected", url=self._url)

            # Subscribe to all active markets
            sub_msg = json.dumps({
                "type": "subscribe",
                "channel": "market",
                "market_ids": [],
            })
            await ws.send(sub_msg)

            # Start heartbeat task
            heartbeat_task = asyncio.create_task(self._heartbeat(ws))

            try:
                async for raw_msg in ws:
                    await self._handle_message(raw_msg)
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _heartbeat(self, ws: websockets.ClientConnection) -> None:
        """Send a heartbeat ping every 10 seconds."""
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            try:
                await ws.send(json.dumps({"type": "heartbeat"}))
            except Exception:
                return

    async def _handle_message(self, raw_msg: str) -> None:
        """Parse, validate, persist, and enqueue a single WS frame."""
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            logger.warning("ws_client.invalid_json", preview=raw_msg[:100])
            return

        event_type = data.get("event_type") or data.get("event", "")
        if event_type not in _VALID_EVENTS:
            return

        try:
            snapshot_schema = MarketSnapshotSchema(
                condition_id=data.get("market", data.get("condition_id", "")),
                question=data.get("question", ""),
                best_bid=data.get("best_bid", data.get("price", 0.0)),
                best_ask=data.get("best_ask", data.get("price", 0.0)),
                last_trade_price=data.get("last_trade_price"),
                outcome_token=data.get("outcome_token", "YES"),
                raw_ws_payload=raw_msg,
            )
        except ValidationError as exc:
            logger.warning(
                "ws_client.validation_error",
                errors=str(exc),
                market=data.get("market"),
            )
            return

        # Persist to DB
        row = MarketSnapshot(
            condition_id=snapshot_schema.condition_id,
            question=snapshot_schema.question,
            best_bid=snapshot_schema.best_bid,
            best_ask=snapshot_schema.best_ask,
            last_trade_price=snapshot_schema.last_trade_price,
            midpoint=snapshot_schema.midpoint,
            outcome_token=snapshot_schema.outcome_token,
            raw_ws_payload=snapshot_schema.raw_ws_payload,
        )

        async with self._db_factory() as session:
            session.add(row)
            await session.commit()

        await self._queue.put(row)

        logger.debug(
            "ws_client.snapshot_enqueued",
            condition_id=snapshot_schema.condition_id,
            midpoint=snapshot_schema.midpoint,
        )
