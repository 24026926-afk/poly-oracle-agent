"""
src/agents/ingestion/ws_client.py

CLOB WebSocket client for streaming live orderbook events from Polymarket.

Connects to the CLOB WebSocket, validates incoming frames via
``MarketSnapshotSchema``, persists to the DB, and feeds an
``asyncio.Queue`` consumed by the Context Builder (Module 2).

WI-46: Hardened with jittered exponential backoff, typed health snapshots,
PONG timeout detection, and explicit market-closed handling.
"""

import asyncio
import json
import random
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import structlog
import websockets
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.config import AppConfig
from src.db.models import MarketSnapshot
from src.db.repositories.market_repo import MarketRepository
from src.observability.health import (
    MarketClosedSkipReason,
    MarketLifecycleState,
    WebSocketConnectionState,
    WebSocketHealthSnapshot,
    WebSocketReconnectConfig,
)
from src.schemas.market import MarketSnapshotSchema

logger = structlog.get_logger(__name__)

_VALID_EVENTS = {"book", "price_change", "last_trade_price"}
_HEARTBEAT_INTERVAL_S = 10


def _extract_asset_id(data: dict) -> str:
    """Return top-level or first nested price_change asset id."""
    asset_id = data.get("asset_id", "")
    if asset_id:
        return str(asset_id)
    price_changes = data.get("price_changes", [])
    if price_changes and isinstance(price_changes, list):
        first_change = price_changes[0]
        if isinstance(first_change, dict):
            nested_asset_id = first_change.get("asset_id", "")
            if nested_asset_id:
                return str(nested_asset_id)
    return ""


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
        self._condition_by_token: dict[str, str] = {}
        self._subscription_sent_at: float = 0.0
        self._aggregator_map: dict[str, Any] = {}
        self._ws: websockets.ClientConnection | None = None

        # WI-46: Reconnect config — defensive against mock configs in tests
        try:
            self._reconnect_cfg = WebSocketReconnectConfig(
                initial_backoff_seconds=config.ws_reconnect_initial_backoff_seconds,
                max_backoff_seconds=config.ws_reconnect_max_backoff_seconds,
                jitter_pct=config.ws_reconnect_jitter_pct,
                pong_timeout_seconds=config.ws_pong_timeout_seconds,
                consecutive_failure_degraded_threshold=config.ws_consecutive_failure_degraded_threshold,
            )
        except Exception:
            self._reconnect_cfg = WebSocketReconnectConfig()

        # WI-46: Health snapshot
        self._health = WebSocketHealthSnapshot()
        self._pong_event: asyncio.Event = asyncio.Event()
        self._pong_event.set()  # initially clear (no PONG pending)

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect, subscribe, and stream forever with jittered backoff."""
        backoff_s = float(self._reconnect_cfg.initial_backoff_seconds)
        max_backoff_s = float(self._reconnect_cfg.max_backoff_seconds)
        jitter_pct = float(self._reconnect_cfg.jitter_pct)

        self._health.reconnect_count = 0
        self._health.consecutive_failure_count = 0

        while True:
            try:
                self._health.connection_state = WebSocketConnectionState.CONNECTED
                await self._stream()
                # _stream only returns on clean shutdown
                self._health.last_connected_at_utc = datetime.now(timezone.utc)
                backoff_s = float(self._reconnect_cfg.initial_backoff_seconds)
                self._health.reconnect_count = 0
                self._health.consecutive_failure_count = 0
            except websockets.ConnectionClosed as exc:
                self._health.connection_state = WebSocketConnectionState.DISCONNECTED
                self._health.consecutive_failure_count += 1
                self._health.reconnect_count = self._health.consecutive_failure_count
                self._health.total_reconnect_count += 1
                self._health.last_error_reason = (
                    f"ConnectionClosed: code={exc.code} reason={exc.reason}"
                )
                self._health.last_connected_at_utc = None
                logger.warning(
                    "ws_client.disconnected",
                    code=exc.code,
                    reason=exc.reason,
                    reconnect_in=backoff_s,
                    backoff_s=backoff_s,
                    consecutive_failures=self._health.consecutive_failure_count,
                )
            except Exception as exc:
                self._health.connection_state = WebSocketConnectionState.DISCONNECTED
                self._health.consecutive_failure_count += 1
                self._health.reconnect_count = self._health.consecutive_failure_count
                self._health.total_reconnect_count += 1
                self._health.last_error_reason = type(exc).__name__
                self._health.last_connected_at_utc = None
                logger.error(
                    "ws_client.connection_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    reconnect_in=backoff_s,
                    backoff_s=backoff_s,
                    consecutive_failures=self._health.consecutive_failure_count,
                )

            # Degraded state when threshold exceeded
            if (
                self._health.consecutive_failure_count
                >= self._reconnect_cfg.consecutive_failure_degraded_threshold
            ):
                self._health.connection_state = WebSocketConnectionState.DEGRADED

            # Reconnect with jittered backoff
            self._health.connection_state = WebSocketConnectionState.RECONNECTING

            # Compute jitter: random factor in [1 - jitter_pct, 1 + jitter_pct]
            jitter_factor = 1.0 + random.uniform(-jitter_pct, jitter_pct)
            sleep_s = backoff_s * jitter_factor

            await asyncio.sleep(sleep_s)
            backoff_s = min(backoff_s * 2, max_backoff_s)

    def get_health_snapshot(self) -> WebSocketHealthSnapshot:
        """Return a copy of the current WebSocket health state."""
        return self._health.model_copy(deep=True)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_subscription_message(self) -> str:
        """Build the CLOB WebSocket subscription payload."""
        return json.dumps(
            {
                "type": "subscribe",
                "channel": "market",
                "assets_ids": self._assets_ids,
            }
        )

    def set_assets_ids(self, assets_ids: list[str]) -> None:
        """Update token IDs for subscription (e.g. after market rotation)."""
        self._assets_ids = assets_ids
        self._health.active_subscribed_asset_count = len(assets_ids)

    def set_token_id_mapping(self, mapping: dict[str, str]) -> None:
        """Set the token_id → yes_token_id mapping for snapshot enrichment."""
        self._token_id_mapping = mapping

    def set_condition_by_token(self, mapping: dict[str, str]) -> None:
        """Set the token_id → condition_id routing map for diagnostics.

        This is the authoritative setter — do not mutate _condition_by_token
        directly from outside the class.
        """
        self._condition_by_token = mapping

    def register_aggregator(self, asset_id: str, aggregator: Any) -> None:
        """Register a DataAggregator for frame routing."""
        self._aggregator_map[asset_id] = aggregator

    async def subscribe_batch(self, assets_ids: list[str]) -> None:
        """Subscribe to multiple assets via a single WebSocket connection.

        Polymarket CLOB supports multiplexed subscriptions — one connection,
        many assets. This is the concurrency primitive for WI-32.
        """
        if not assets_ids:
            logger.warning(
                "ws.subscribe_batch_empty",
                message="No assets to subscribe to",
            )
            return

        ws = self._ws
        if ws is None:
            logger.warning(
                "ws.subscribe_batch_no_connection",
                message="WebSocket not yet connected — deferring subscription",
            )
            return

        subscription_msg = json.dumps(
            {
                "type": "subscribe",
                "assets_ids": assets_ids,
                "event_types": ["book", "price_change", "last_trade_price"],
            }
        )

        await ws.send(subscription_msg)
        self._health.active_subscribed_asset_count = len(assets_ids)
        logger.info(
            "market_tracking.subscribed_batch",
            asset_count=len(assets_ids),
            assets_ids=assets_ids,
        )

    async def _stream(self) -> None:
        async with websockets.connect(self._url) as ws:
            self._ws = ws
            self._health.connection_state = WebSocketConnectionState.CONNECTED
            self._health.last_connected_at_utc = datetime.now(timezone.utc)
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
                self._ws = None

    async def _heartbeat(self, ws: websockets.ClientConnection) -> None:
        """Send a heartbeat ping every 10 seconds with PONG timeout detection.

        Polymarket CLOB expects the plain text string "PING" (not JSON).
        Server responds with "PONG" automatically.

        WI-46: If no PONG received within pong_timeout_seconds, close the
        connection to trigger reconnect.
        """
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)
            try:
                self._health.last_heartbeat_sent_at_utc = datetime.now(timezone.utc)
                self._pong_event.clear()
                logger.debug("ws_client.outbound_message", message_type="ping")
                await ws.send("PING")

                # Wait for PONG within timeout
                try:
                    await asyncio.wait_for(
                        self._pong_event.wait(),
                        timeout=float(self._reconnect_cfg.pong_timeout_seconds),
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "ws_client.pong_timeout",
                        timeout_seconds=str(self._reconnect_cfg.pong_timeout_seconds),
                    )
                    return  # trigger reconnect
            except websockets.ConnectionClosed:
                logger.warning("ws_client.heartbeat_connection_closed")
                return
            except Exception as exc:
                logger.warning(
                    "ws_client.heartbeat_error",
                    error=str(exc),
                )
                return

    async def _handle_message(self, raw_msg: str) -> None:
        """Parse, validate, persist, and enqueue a single WS frame.

        WI-32: Routes incoming frames to per-market aggregators via asset_id
        before the standard persistence/enqueue path.

        WI-46: Detects market-closed frames from metadata.
        """
        # Handle server PONG response (plain text, not JSON)
        if raw_msg.strip() == "PONG":
            self._health.last_pong_received_at_utc = datetime.now(timezone.utc)
            self._pong_event.set()
            logger.debug("ws_client.pong_received")
            return

        logger.debug("ws_client.raw_message", preview=raw_msg[:300])

        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            # Plain-text server errors (e.g. "INVALID OPERATION") are not JSON
            stripped = raw_msg.strip()
            if (
                stripped
                and not stripped.startswith("{")
                and not stripped.startswith("[")
            ):
                logger.warning("ws_client.server_error", response=stripped[:200])
            else:
                logger.warning("ws_client.invalid_json", preview=raw_msg[:100])
            return

        # WI-46: Market lifecycle detection
        if isinstance(data, dict):
            market_lifecycle = data.get("market_lifecycle") or data.get(
                "lifecycle_state"
            )
            if market_lifecycle is not None:
                lifecycle_str = str(market_lifecycle).upper()
                try:
                    state = MarketLifecycleState(lifecycle_str)
                except ValueError:
                    state = MarketLifecycleState.UNKNOWN

                if state in (
                    MarketLifecycleState.CLOSED,
                    MarketLifecycleState.INACTIVE,
                    MarketLifecycleState.EXPIRED,
                ):
                    condition_id = data.get("market") or data.get(
                        "condition_id", "unknown"
                    )
                    reason = MarketClosedSkipReason(
                        condition_id=condition_id,
                        reason=state,
                        detail=f"Market {state.value}: {raw_msg[:200]}",
                    )
                    logger.info(
                        "ws_client.market_closed",
                        condition_id=reason.condition_id,
                        lifecycle_state=state.value,
                    )
                    return  # do not treat as transport error

        # WI-32: Route to per-market aggregator via asset_id before standard processing
        asset_id = _extract_asset_id(data) if isinstance(data, dict) else None
        if asset_id and asset_id in self._aggregator_map:
            aggregator = self._aggregator_map[asset_id]
            aggregator.process_frame(data)
            aggregator.frame_count += 1
            aggregator.last_seen_utc = datetime.now(timezone.utc)
            return  # Routed — skip standard persistence path

        if asset_id is not None and asset_id not in self._aggregator_map:
            logger.warning(
                "ws.frame_unrouted",
                asset_id=asset_id,
                frame_type=data.get("event_type", "unknown")
                if isinstance(data, dict)
                else "unknown",
            )
        elif isinstance(data, dict) and "asset_id" not in data:
            logger.warning(
                "ws.frame_unrouted",
                asset_id=None,
                frame_type=data.get("event_type", "unknown"),
            )

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

        WI-32 fix: frames from stale (deactivated) tokens are dropped.
        """
        event_type = data.get("event_type") or data.get("event", "")
        if event_type not in _VALID_EVENTS:
            return

        condition_id = data.get("market", data.get("condition_id", ""))
        asset_id = _extract_asset_id(data)

        # WI-32: Drop frames from tokens not in any active market.
        # When _condition_by_token is populated, both condition_id and
        # asset_id must reference active markets.  A frame with a stale
        # asset_id but no condition_id must also be rejected.
        if self._condition_by_token:
            active_conditions = set(self._condition_by_token.values())
            active_tokens = set(self._condition_by_token.keys())
            # condition_id must be active (if present)
            if condition_id and condition_id not in active_conditions:
                logger.debug(
                    "ws_client.drop_stale_token",
                    asset_id=asset_id,
                    condition_id=condition_id,
                    event_type=event_type,
                    reason="stale_condition_id",
                )
                return
            # asset_id must be active (if present) — catches stale tokens
            # even when condition_id is missing from the frame
            if asset_id and asset_id not in active_tokens:
                logger.debug(
                    "ws_client.drop_stale_token",
                    asset_id=asset_id,
                    condition_id=condition_id,
                    event_type=event_type,
                    reason="stale_asset_id",
                )
                return
        elif asset_id and asset_id not in self._token_id_mapping:
            logger.debug(
                "ws_client.drop_unknown_token",
                asset_id=asset_id,
                event_type=event_type,
            )
            return

        # Resolve yes_token_id from the token_id mapping set by MarketDiscoveryEngine.
        # no_token_id is populated later from Gamma market metadata (clobTokenIds[1]).
        yes_token_id: str | None = None
        no_token_id: str | None = None

        known_token = asset_id in self._token_id_mapping if asset_id else False
        if asset_id and asset_id in self._token_id_mapping:
            yes_token_id = self._token_id_mapping[asset_id]
        elif condition_id and condition_id in self._token_id_mapping:
            yes_token_id = self._token_id_mapping[condition_id]

        logger.debug(
            "snapshot_route_debug",
            token_id=asset_id,
            condition_id=condition_id,
            known_token=known_token,
        )

        # Extract best_bid/best_ask based on frame type
        best_bid = 0.0
        best_ask = 0.0
        last_trade_price = None

        if event_type == "last_trade_price":
            last_trade_price = data.get("price", 0.0)
        elif event_type == "price_change":
            # Polymarket CLOB sends price_changes as an array; extract from first entry
            price_changes = data.get("price_changes", [])
            if price_changes and isinstance(price_changes, list):
                first_change = price_changes[0]
                if isinstance(first_change, dict):
                    best_bid = float(first_change.get("best_bid", 0.0))
                    best_ask = float(first_change.get("best_ask", 0.0))
            # Fall back to top-level fields if array is empty/missing
            if best_bid == 0.0:
                best_bid = data.get("best_bid", 0.0)
            if best_ask == 0.0:
                best_ask = data.get("best_ask", 0.0)
        elif event_type == "book":
            # Try to extract from bids[0]/asks[0] lists first
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if bids:
                try:
                    best_bid = float(
                        bids[0].get("price", 0.0)
                        if isinstance(bids[0], dict)
                        else bids[0]
                    )
                except (ValueError, IndexError, TypeError):
                    best_bid = 0.0
            if asks:
                try:
                    best_ask = float(
                        asks[0].get("price", 0.0)
                        if isinstance(asks[0], dict)
                        else asks[0]
                    )
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

        # last_trade_price frames lack a full orderbook — do not treat as
        # complete market snapshots for evaluation.  They are useful for
        # price tracking but should not produce midpoint=0.0 or malformed EV inputs.
        if event_type == "last_trade_price" and best_bid <= 0 and best_ask <= 0:
            logger.debug(
                "ws_client.skip_last_trade_no_book",
                event_type=event_type,
                condition_id=condition_id,
                last_trade_price=last_trade_price,
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
