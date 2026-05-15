"""
src/agents/context/aggregator.py

Context Builder module.
Maintains an in-memory representation of the orderbook and pushes market
summaries to the LLM Evaluation Node based on time or price triggers.

WI-32 fix: Per-market state isolation — each active market maintains its own
best_bid, best_ask, yes_token_id, question, category, tags, and emit timers.
Frames from market B update market B's state and emit market B's context.
"""

import asyncio
import uuid
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, Optional, List

import structlog

from src.schemas.market import CLOBMessage, PerMarketAggregatorState
from src.schemas.market_eligibility import (
    MarketEvaluationFingerprint,
    MarketEvaluationDedupeDecision,
    MarketEvaluationDedupeReason,
)
from src.agents.context.prompt_factory import PromptFactory

logger = structlog.get_logger(__name__)


@dataclass
class _MarketState:
    """Per-market orderbook and emit tracking."""
    condition_id: str
    question: str = ""
    category: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    yes_token_id: Optional[str] = None
    best_bid: float = 0.0
    best_ask: float = 0.0
    last_emit_time: float = 0.0
    last_emitted_midpoint: Optional[float] = None


class DataAggregator:
    """
    Reads live CLOB messages, maintains per-market orderbook state, and
    emits a complete market summary either every 30 seconds or when a
    significant price change (> 1%) is detected.

    WI-32: Each active market has isolated state.  A frame from market B
    updates market B's bid/ask and emits market B's condition/question/category.
    """

    def __init__(
        self,
        input_queue: asyncio.Queue[CLOBMessage],
        output_queue: asyncio.Queue[Dict[str, Any]],
        condition_id: str,
        condition_by_token: Optional[Dict[str, str]] = None,
        metrics=None,  # Optional MetricsRegistry for WI-53 metrics
    ):
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.condition_id = condition_id
        self.condition_by_token: Dict[str, str] = condition_by_token or {}
        self._metrics = metrics

        # Per-market state isolation (WI-32 fix)
        self._markets: Dict[str, _MarketState] = {}

        # State
        self._running = False

        # Configurable triggers
        self.TIME_INTERVAL_SEC = 30.0
        self.PRICE_CHANGE_THRESHOLD = 0.01  # 1%

        # WI-32: Per-instance frame tracking (mutated by CLOBWebSocketClient routing)
        self.frame_count: int = 0
        self.last_seen_utc: Optional[datetime] = None

        # WI-53: Dedupe state — per-market fingerprints
        self._dedupe_enabled: bool = False
        self._dedupe_min_interval: float = 30.0
        self._dedupe_midpoint_delta: Decimal = Decimal("0.01")
        self._dedupe_spread_delta: Decimal = Decimal("0.005")
        # condition_id -> last fingerprint
        self._last_fingerprints: Dict[str, MarketEvaluationFingerprint] = {}
        # condition_id -> last emit timestamp for dedupe interval
        self._dedupe_last_emit: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Backward-compatible global properties (delegate to primary market)
    # ------------------------------------------------------------------

    @property
    def best_bid(self) -> float:
        ms = self._markets.get(self.condition_id)
        return ms.best_bid if ms else 0.0

    @best_bid.setter
    def best_bid(self, value: float) -> None:
        self._ensure_market(self.condition_id)
        self._markets[self.condition_id].best_bid = value

    @property
    def best_ask(self) -> float:
        ms = self._markets.get(self.condition_id)
        return ms.best_ask if ms else 0.0

    @best_ask.setter
    def best_ask(self, value: float) -> None:
        self._ensure_market(self.condition_id)
        self._markets[self.condition_id].best_ask = value

    @property
    def _yes_token_id(self) -> Optional[str]:
        ms = self._markets.get(self.condition_id)
        return ms.yes_token_id if ms else None

    @_yes_token_id.setter
    def _yes_token_id(self, value: Optional[str]) -> None:
        self._ensure_market(self.condition_id)
        self._markets[self.condition_id].yes_token_id = value

    @property
    def _market_category(self) -> Optional[str]:
        ms = self._markets.get(self.condition_id)
        return ms.category if ms else None

    @_market_category.setter
    def _market_category(self, value: Optional[str]) -> None:
        self._ensure_market(self.condition_id)
        self._markets[self.condition_id].category = value

    @property
    def _market_tags(self) -> list[str]:
        ms = self._markets.get(self.condition_id)
        return ms.tags if ms else []

    @_market_tags.setter
    def _market_tags(self, value: list[str]) -> None:
        self._ensure_market(self.condition_id)
        self._markets[self.condition_id].tags = list(value)

    @property
    def _market_question(self) -> str:
        ms = self._markets.get(self.condition_id)
        return ms.question if ms else ""

    @_market_question.setter
    def _market_question(self, value: str) -> None:
        self._ensure_market(self.condition_id)
        self._markets[self.condition_id].question = value

    @property
    def _last_emit_time(self) -> float:
        ms = self._markets.get(self.condition_id)
        return ms.last_emit_time if ms else 0.0

    @_last_emit_time.setter
    def _last_emit_time(self, value: float) -> None:
        self._ensure_market(self.condition_id)
        self._markets[self.condition_id].last_emit_time = value

    @property
    def _last_emitted_midpoint(self) -> Optional[float]:
        ms = self._markets.get(self.condition_id)
        return ms.last_emitted_midpoint if ms else None

    @_last_emitted_midpoint.setter
    def _last_emitted_midpoint(self, value: Optional[float]) -> None:
        self._ensure_market(self.condition_id)
        self._markets[self.condition_id].last_emitted_midpoint = value

    # ------------------------------------------------------------------
    # Per-market registration
    # ------------------------------------------------------------------

    def _ensure_market(self, condition_id: str) -> None:
        """Create per-market state if not already present."""
        if condition_id not in self._markets:
            self._markets[condition_id] = _MarketState(condition_id=condition_id)

    def register_market(
        self,
        condition_id: str,
        question: str = "",
        category: Optional[str] = None,
        tags: Optional[list[str]] = None,
        yes_token_id: Optional[str] = None,
    ) -> None:
        """Register a market's metadata for per-market emit context."""
        self._ensure_market(condition_id)
        ms = self._markets[condition_id]
        if question:
            ms.question = question
        if category is not None:
            ms.category = category
        if tags is not None:
            ms.tags = list(tags)
        if yes_token_id is not None:
            ms.yes_token_id = yes_token_id

    def deregister_market(self, condition_id: str) -> None:
        """Remove a market's state (e.g. when it becomes ineligible)."""
        self._markets.pop(condition_id, None)

    def clear_markets(self) -> None:
        """Remove all market state (e.g. on full deactivation)."""
        self._markets.clear()
        self._last_fingerprints.clear()
        self._dedupe_last_emit.clear()

    # ------------------------------------------------------------------
    # WI-53: Dedupe configuration
    # ------------------------------------------------------------------

    def configure_dedupe(
        self,
        enabled: bool = False,
        min_interval_sec: float = 30.0,
        midpoint_delta: Decimal = Decimal("0.01"),
        spread_delta: Decimal = Decimal("0.005"),
    ) -> None:
        """Configure dedupe parameters."""
        self._dedupe_enabled = enabled
        self._dedupe_min_interval = min_interval_sec
        self._dedupe_midpoint_delta = midpoint_delta
        self._dedupe_spread_delta = spread_delta

    # ------------------------------------------------------------------
    # WI-53: Dedupe check
    # ------------------------------------------------------------------

    def _check_dedupe(
        self,
        condition_id: str,
        midpoint: Decimal,
        spread: Decimal,
    ) -> MarketEvaluationDedupeDecision:
        """Determine whether to emit a new evaluation for this market.

        Returns a dedupe decision with emit=True/False and the reason.
        """
        if not self._dedupe_enabled:
            return MarketEvaluationDedupeDecision(
                condition_id=condition_id,
                emit=True,
                reason=MarketEvaluationDedupeReason.MIDPOINT_MOVED,
            )

        now = time.time()
        last_emit = self._dedupe_last_emit.get(condition_id, 0.0)
        elapsed = now - last_emit

        # Check minimum interval
        if elapsed < self._dedupe_min_interval:
            last_fp = self._last_fingerprints.get(condition_id)
            if last_fp is not None:
                midpoint_changed = abs(midpoint - last_fp.midpoint) >= self._dedupe_midpoint_delta
                spread_changed = abs(spread - last_fp.spread) >= self._dedupe_spread_delta

                if not midpoint_changed and not spread_changed:
                    return MarketEvaluationDedupeDecision(
                        condition_id=condition_id,
                        emit=False,
                        reason=MarketEvaluationDedupeReason.UNCHANGED_STATE,
                    )

                # Material movement — emit
                reason = (
                    MarketEvaluationDedupeReason.MIDPOINT_MOVED
                    if midpoint_changed
                    else MarketEvaluationDedupeReason.SPREAD_MOVED
                )
                return MarketEvaluationDedupeDecision(
                    condition_id=condition_id,
                    emit=True,
                    reason=reason,
                )

        # Either enough time elapsed, or no previous fingerprint
        return MarketEvaluationDedupeDecision(
            condition_id=condition_id,
            emit=True,
            reason=MarketEvaluationDedupeReason.MIDPOINT_MOVED,
        )

    def _record_fingerprint(
        self,
        condition_id: str,
        midpoint: Decimal,
        spread: Decimal,
    ) -> None:
        """Store the current fingerprint for a market."""
        self._last_fingerprints[condition_id] = MarketEvaluationFingerprint(
            condition_id=condition_id,
            midpoint=midpoint,
            spread=spread,
        )
        self._dedupe_last_emit[condition_id] = time.time()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Starts the aggregation loop processing incoming messages."""
        self._running = True
        logger.info("Starting DataAggregator...", condition_id=self.condition_id)

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

    # ------------------------------------------------------------------
    # Message processing — per-market isolation
    # ------------------------------------------------------------------

    async def _process_message(self, msg: object) -> None:
        """Updates per-market internal state based on a new market message.

        WI-32 fix: messages from any active market update that market's
        isolated state.  Frames from market B update market B's bid/ask
        and trigger market B's emit — never market A's context.
        """
        msg_cid = getattr(msg, "condition_id", None)
        if msg_cid is not None:
            active_conditions = set(self.condition_by_token.values())
            if active_conditions and msg_cid not in active_conditions:
                return
            if not active_conditions and msg_cid != self.condition_id:
                return
            target_cid = msg_cid
        else:
            target_cid = self.condition_id

        self._ensure_market(target_cid)
        ms = self._markets[target_cid]

        updated = False

        msg_yes_token_id = getattr(msg, "yes_token_id", None)
        if msg_yes_token_id is not None:
            ms.yes_token_id = msg_yes_token_id

        best_bid = getattr(msg, "best_bid", None)
        best_ask = getattr(msg, "best_ask", None)

        if best_bid is not None and best_bid > 0:
            ms.best_bid = float(best_bid)
            updated = True

        if best_ask is not None and best_ask > 0:
            ms.best_ask = float(best_ask)
            updated = True

        if updated:
            await self._check_triggers_for_market(target_cid)

    async def _timer_loop(self) -> None:
        """Background loop to force an emit every TIME_INTERVAL_SEC for each market."""
        while self._running:
            await asyncio.sleep(1.0)
            now = time.time()
            for cid, ms in list(self._markets.items()):
                if now - ms.last_emit_time >= self.TIME_INTERVAL_SEC:
                    if ms.best_bid > 0 and ms.best_ask > 0:
                        logger.debug("Time trigger activated.", condition_id=cid)
                        await self._emit_state_for_market(cid)

    async def _check_triggers_for_market(self, condition_id: str) -> None:
        """Evaluates if the price change warrants an immediate LLM evaluation.

        WI-53: Midpoint and spread computed as Decimal at the boundary.
        """
        ms = self._markets.get(condition_id)
        if ms is None or ms.best_bid <= 0 or ms.best_ask <= 0:
            return

        # WI-53: Convert to Decimal at the boundary
        bid_dec = Decimal(str(ms.best_bid))
        ask_dec = Decimal(str(ms.best_ask))
        current_midpoint_dec = (bid_dec + ask_dec) / Decimal("2")
        current_midpoint = float(current_midpoint_dec)
        now = time.time()

        time_elapsed = now - ms.last_emit_time >= self.TIME_INTERVAL_SEC

        price_moved = False
        if ms.last_emitted_midpoint is not None and ms.last_emitted_midpoint > 0:
            last_mid_dec = Decimal(str(ms.last_emitted_midpoint))
            change_dec = (
                abs(current_midpoint_dec - last_mid_dec) / last_mid_dec
            )
            threshold_dec = Decimal(str(self.PRICE_CHANGE_THRESHOLD))
            if change_dec >= threshold_dec:
                price_moved = True
                logger.debug(
                    "Volatility trigger activated.",
                    condition_id=condition_id,
                    price_change_pct=float(change_dec),
                )

        if time_elapsed or price_moved:
            await self._emit_state_for_market(condition_id, current_midpoint)

    async def _emit_state_for_market(
        self, condition_id: str, current_midpoint: Optional[float] = None,
    ) -> None:
        """Builds the market summary and pushes it to the output queue.

        WI-53: Runs dedupe check before emitting.  If dedupe suppresses the
        payload, no prompt is enqueued.
        """
        ms = self._markets.get(condition_id)
        if ms is None:
            return

        if current_midpoint is None:
            current_midpoint = (ms.best_bid + ms.best_ask) / 2.0

        spread = ms.best_ask - ms.best_bid

        # WI-53: Convert to Decimal at the boundary — no float in financial path
        bid_dec = Decimal(str(ms.best_bid))
        ask_dec = Decimal(str(ms.best_ask))
        mid_dec = (bid_dec + ask_dec) / Decimal("2")
        spr_dec = ask_dec - bid_dec
        dedupe_decision = self._check_dedupe(condition_id, mid_dec, spr_dec)

        if not dedupe_decision.emit:
            logger.debug(
                "dedupe.suppressed",
                reason=dedupe_decision.reason.value,
            )
            if self._metrics is not None:
                await self._metrics.record_deduped_context()
            return

        state = {
            "condition_id": condition_id,
            "title": ms.question,
            "question": ms.question,
            "category": ms.category,
            "tags": list(ms.tags),
            "best_bid": mid_dec - spr_dec / Decimal("2"),
            "best_ask": mid_dec + spr_dec / Decimal("2"),
            "midpoint": mid_dec,
            "spread": spr_dec,
            "timestamp": time.time(),
        }

        ms.last_emit_time = time.time()
        ms.last_emitted_midpoint = current_midpoint

        # WI-53: Record fingerprint after successful emit
        self._record_fingerprint(condition_id, mid_dec, spr_dec)

        prompt = PromptFactory.build_evaluation_prompt(state)

        output_payload = {
            "snapshot_id": str(uuid.uuid4()),
            "prompt": prompt,
            "state": state,
            "yes_token_id": ms.yes_token_id,
        }

        logger.debug(
            "Emitting market state and CoT prompt.",
            condition_id=condition_id,
            midpoint=current_midpoint,
            spread=spread,
        )
        if self._metrics is not None:
            await self._metrics.record_emitted_context()
        await self.output_queue.put(output_payload)

    # ------------------------------------------------------------------
    # Legacy emit (backward compat — emits primary market)
    # ------------------------------------------------------------------

    async def _emit_state(self, current_midpoint: Optional[float] = None) -> None:
        """Backward-compatible: emit primary market state."""
        await self._emit_state_for_market(self.condition_id, current_midpoint)

    async def _check_triggers(self) -> None:
        """Backward-compatible: check triggers for primary market."""
        await self._check_triggers_for_market(self.condition_id)

    # ------------------------------------------------------------------
    # WI-32: Concurrent multi-market tracking
    # ------------------------------------------------------------------

    def process_frame(self, data: dict) -> None:
        """Update per-market orderbook state from a raw WS frame dict.

        WI-32 fix: routes to per-market state via condition_by_token map.
        """
        event_type = data.get("event_type", "")
        asset_id = data.get("asset_id")
        market_id = data.get("market") or data.get("condition_id")

        condition_id = None
        if asset_id and asset_id in self.condition_by_token:
            condition_id = self.condition_by_token[asset_id]
        elif market_id and market_id in self.condition_by_token:
            condition_id = self.condition_by_token[market_id]
        elif market_id:
            condition_id = market_id

        if condition_id is None:
            condition_id = self.condition_id

        self._ensure_market(condition_id)
        ms = self._markets[condition_id]

        if event_type == "last_trade_price":
            ltp = data.get("last_trade_price") or data.get("price")
            if ltp is not None:
                val = float(ltp)
                if val > 0:
                    ms.best_bid = val
                    ms.best_ask = val
            return

        raw_bid = data.get("best_bid")
        raw_ask = data.get("best_ask")
        if raw_bid is not None:
            bid = float(raw_bid)
            if bid > 0:
                ms.best_bid = bid
        if raw_ask is not None:
            ask = float(raw_ask)
            if ask > 0:
                ms.best_ask = ask

    async def track_market(self, token_ids: List[str]) -> List[Dict[str, Any]]:
        """Track a single market concurrently.

        Manages per-market subscription state via PerMarketAggregatorState.
        Produces MarketContext to shared prompt_queue.
        """
        state = PerMarketAggregatorState(token_ids=token_ids)

        ws_client = getattr(self, "ws_client", None)
        if ws_client is not None:
            for token_id in token_ids:
                ws_client.register_aggregator(token_id, self)
            await ws_client.subscribe_batch(token_ids)

        market_contexts: List[Dict[str, Any]] = []
        try:
            context = self._build_market_context(token_ids, state)
            market_contexts.append(context)

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
        ms = self._markets.get(self.condition_id)
        best_bid = ms.best_bid if ms else 0.0
        best_ask = ms.best_ask if ms else 0.0
        current_midpoint = (
            (best_bid + best_ask) / 2.0
            if best_bid > 0 and best_ask > 0
            else 0.0
        )
        spread = best_ask - best_bid

        state_dict = {
            "condition_id": self.condition_id,
            "title": ms.question if ms else "",
            "question": ms.question if ms else "",
            "category": ms.category if ms else None,
            "tags": list(ms.tags) if ms else [],
            "best_bid": best_bid,
            "best_ask": best_ask,
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
            "yes_token_id": ms.yes_token_id if ms else None,
        }
