"""
src/agents/ingestion/ws_client.py

CLOB WebSocket client for streaming live orderbook events from Polymarket.

Connects to the CLOB WebSocket, validates incoming frames via
``MarketSnapshotSchema``, persists to the DB, and feeds an
``asyncio.Queue`` consumed by the Context Builder (Module 2).
"""

import asyncio
import json
from collections.abc import Callable

import structlog
import websockets
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import AppConfig
from src.db.models import MarketSnapshot
from src.db.repositories.market_repo import MarketRepository
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
        market_repo_factory: Callable[
            [AsyncSession], MarketRepository
        ] = MarketRepository,
        assets_ids: list[str] | None = None,
        token_id_to_yes_token_id: dict[str, str] | None = None,
    ) -> None:
        self._url = config.clob_ws_url
        self._queue = queue
        self._db_factory = db_session_factory
        self._market_repo_factory = market_repo_factory
        self._assets_ids: list[str] = assets_ids or []
        self._token_id_mapping: dict[str, str] = token_id_to_yes_token_id or {}
        self._subscription_sent_at: float = 0.0

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

    def _build_subscription_message(self) -> str:
        """Build the CLOB WebSocket subscription payload."""
        return json.dumps({
            "type": "subscribe",
            "channel": "market",
            "assets_ids": self._assets_ids,
        })

    def set_assets_ids(self, assets_ids: list[str]) -> None:
        """Update token IDs for subscription (e.g. after market rotation)."""
        self._assets_ids = assets_ids

    def set_token_id_mapping(self, mapping: dict[str, str]) -> None:
        """Set the token_id → yes_token_id mapping for snapshot enrichment."""
        self._token_id_mapping = mapping

    async def _stream(self) -> None:
        async with websockets.connect(self._url) as ws:
            logger.info("ws_client.connected", url=self._url)

            sub_msg = self._build_subscription_message()
            logger.debug(
                "ws_client.subscribing",
                payload=sub_msg,
                assets_count=len(self._assets_ids),
            )
            logger.debug(
                "ws_client.outbound_message",
                message_type="subscribe",
                assets_ids_count=len(self._assets_ids),
            )
            logger.info(
                "ws_client.subscription_audit",
                assets_ids_count=len(self._assets_ids),
                token_mapping_count=len(self._token_id_mapping),
            )
            self._subscription_sent_at = asyncio.get_event_loop().time()
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
                hb_msg = json.dumps({"type": "heartbeat"})
                logger.debug(
                    "ws_client.outbound_message",
                    message_type="heartbeat",
                )
                await ws.send(hb_msg)
            except Exception:
                return

    async def _handle_message(self, raw_msg: str) -> None:
        """Parse, validate, persist, and enqueue a single WS frame."""
        logger.debug("ws_client.raw_message", preview=raw_msg[:300])

        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            # Plain-text server errors (e.g. "INVALID OPERATION") are not JSON
            stripped = raw_msg.strip()
            if stripped and not stripped.startswith("{") and not stripped.startswith("["):
                logger.warning("ws_client.server_error", response=stripped[:200])
            else:
                logger.warning("ws_client.invalid_json", preview=raw_msg[:100])
            return

        # The CLOB WS may send list-wrapped messages (batches or ack frames).
        # Normalise to a list of dicts and process each individually.
        if isinstance(data, list):
            items: list[dict] = data
        else:
            items = [data]

        for item in items:
            if not isinstance(item, dict):
                continue
            await self._process_event(item, raw_msg)

    async def _process_event(self, data: dict, raw_msg: str) -> None:
        """Process a single event dict from the WS stream.

        Handles three frame types:
        - last_trade_price: midpoint from last_trade_price
        - price_change: midpoint from best_bid/best_ask
        - book: midpoint from bids[0]/asks[0] lists
        """
        event_type = data.get("event_type") or data.get("event", "")
        if event_type not in _VALID_EVENTS:
            return

        condition_id = data.get("market", data.get("condition_id", ""))
        asset_id = data.get("asset_id", "")

        # Resolve yes_token_id from the token_id mapping set by MarketDiscoveryEngine.
        # no_token_id is populated later from Gamma market metadata (clobTokenIds[1]).
        yes_token_id: str | None = None
        no_token_id: str | None = None

        if asset_id and asset_id in self._token_id_mapping:
            yes_token_id = self._token_id_mapping[asset_id]
        elif condition_id and condition_id in self._token_id_mapping:
            yes_token_id = self._token_id_mapping[condition_id]

        # Extract best_bid/best_ask based on frame type
        best_bid = 0.0
        best_ask = 0.0
        last_trade_price = None

        if event_type == "last_trade_price":
            last_trade_price = data.get("price", 0.0)
        elif event_type == "price_change":
            best_bid = data.get("best_bid", 0.0)
            best_ask = data.get("best_ask", 0.0)
        elif event_type == "book":
            # Try to extract from bids[0]/asks[0] lists first
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids:
                try:
                    best_bid = float(bids[0].get("price", 0.0) if isinstance(bids[0], dict) else bids[0])
                except (ValueError, IndexError, TypeError):
                    best_bid = 0.0
            if asks:
                try:
                    best_ask = float(asks[0].get("price", 0.0) if isinstance(asks[0], dict) else asks[0])
                except (ValueError, IndexError, TypeError):
                    best_ask = 0.0
            # Fall back to top-level best_bid/best_ask if lists are empty
            if best_bid == 0.0:
                best_bid = data.get("best_bid", 0.0)
            if best_ask == 0.0:
                best_ask = data.get("best_ask", 0.0)

        # Guard: do not emit snapshot with midpoint=0 for frames that
        # should carry spread data.  Preserves last known midpoint downstream.
        if event_type in ("price_change", "book") and (best_bid <= 0 or best_ask <= 0):
            logger.debug(
                "ws_client.skip_no_spread",
                event_type=event_type,
                condition_id=condition_id,
                best_bid=best_bid,
                best_ask=best_ask,
            )
            return

        try:
            snapshot_schema = MarketSnapshotSchema(
                condition_id=condition_id,
                question=data.get("question", ""),
                best_bid=best_bid,
                best_ask=best_ask,
                last_trade_price=last_trade_price,
                outcome_token=data.get("outcome_token", "YES"),
                raw_ws_payload=raw_msg,
                yes_token_id=yes_token_id,
                no_token_id=no_token_id,
            )
        except ValidationError as exc:
            logger.warning(
                "ws_client.validation_error",
                errors=str(exc),
                market=condition_id,
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
            yes_token_id=snapshot_schema.yes_token_id,
            no_token_id=snapshot_schema.no_token_id,
        )

        async with self._db_factory() as session:
            repo = self._market_repo_factory(session)
            await repo.insert_snapshot(row)
            await session.commit()

        await self._queue.put(row)

        logger.debug(
            "ws_client.snapshot_enqueued",
            condition_id=snapshot_schema.condition_id,
            midpoint=snapshot_schema.midpoint,
            yes_token_id=snapshot_schema.yes_token_id,
            no_token_id=snapshot_schema.no_token_id,
        )
