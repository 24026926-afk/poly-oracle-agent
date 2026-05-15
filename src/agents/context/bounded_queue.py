"""
src/agents/context/bounded_queue.py

Bounded prompt queue with coalescing and stale-drop fallback for WI-53.

Uses a deque-based design so coalescing can replace items in-place without
draining/re-enqueueing (which would break asyncio.Queue task accounting).
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any, Dict, Optional

import structlog

from src.schemas.market_eligibility import (
    PromptQueueBackpressureDecision,
    PromptQueueBackpressureReason,
    StaleContextSkipReason,
    PromptQueueDepthSnapshot,
)

logger = structlog.get_logger(__name__)


class BoundedPromptQueue:
    """Bounded prompt queue with coalescing support.

    When the queue is full:
    - Coalescing mode: replace the stale payload for the same condition_id
    - Non-coalescing mode: drop the new payload with a typed reason

    Uses a deque + asyncio.Event for proper task accounting.
    """

    def __init__(
        self,
        max_size: int = 50,
        coalescing: bool = True,
        metrics=None,
    ) -> None:
        self._deque: deque[Dict[str, Any]] = deque(maxlen=max_size)
        self._max_size = max_size
        self._coalescing = coalescing
        self._metrics = metrics
        self._not_empty = asyncio.Event()
        self._lock = asyncio.Lock()
        # Track unfinished tasks for join()
        self._unfinished_tasks = 0
        self._all_done = asyncio.Event()
        self._all_done.set()  # Initially empty = all done

    async def put(
        self,
        item: Dict[str, Any],
    ) -> PromptQueueBackpressureDecision:
        """Attempt to enqueue a prompt payload."""
        condition_id = item.get("state", {}).get("condition_id")

        async with self._lock:
            if len(self._deque) < self._max_size:
                self._deque.append(item)
                self._unfinished_tasks += 1
                self._all_done.clear()
                self._not_empty.set()
                self._update_queue_depth_metric()
                return PromptQueueBackpressureDecision(
                    action="enqueue",
                    reason=PromptQueueBackpressureReason.QUEUE_FULL,
                    queue_depth=len(self._deque),
                    condition_id=condition_id,
                )

            # Queue is full
            if self._coalescing and condition_id:
                return await self._coalesce(item, condition_id)
            else:
                return await self._drop_stale(item, condition_id)

    async def _coalesce(
        self,
        item: Dict[str, Any],
        condition_id: str,
    ) -> PromptQueueBackpressureDecision:
        """Replace the stale payload for the same market in-place."""
        for i, existing in enumerate(self._deque):
            existing_cid = existing.get("state", {}).get("condition_id")
            if existing_cid == condition_id:
                self._deque[i] = item  # In-place replacement
                logger.debug(
                    "queue.coalesced",
                    reason=PromptQueueBackpressureReason.COALESCED.value,
                    queue_depth=len(self._deque),
                )
                if self._metrics is not None:
                    await self._metrics.record_coalesced_context()
                    self._update_queue_depth_metric()
                return PromptQueueBackpressureDecision(
                    action="coalesce",
                    reason=PromptQueueBackpressureReason.COALESCED,
                    queue_depth=len(self._deque),
                    condition_id=condition_id,
                )

        # No matching market — drop stale
        return await self._drop_stale(item, condition_id)

    async def _drop_stale(
        self,
        item: Dict[str, Any],
        condition_id: Optional[str],
    ) -> PromptQueueBackpressureDecision:
        """Drop the oldest item and enqueue the new one to preserve latest context.

        WI-53: When the queue is full and no matching market exists, we
        discard the oldest (stalest) payload and enqueue the new context
        so the queue always holds the freshest market data.
        """
        # Drop the oldest item — mark its task as done since it will never be processed
        self._deque.popleft()
        self._unfinished_tasks -= 1
        if self._unfinished_tasks <= 0:
            self._unfinished_tasks = 0
            self._all_done.set()

        logger.warning(
            "queue.stale_dropped",
            reason=StaleContextSkipReason.QUEUE_FULL_NO_MATCH.value,
            queue_depth=len(self._deque),
        )
        if self._metrics is not None:
            await self._metrics.record_dropped_stale_context()

        # Enqueue the new item
        self._deque.append(item)
        self._unfinished_tasks += 1
        self._all_done.clear()
        self._not_empty.set()
        self._update_queue_depth_metric()

        return PromptQueueBackpressureDecision(
            action="drop",
            reason=PromptQueueBackpressureReason.STALE_DROPPED,
            queue_depth=len(self._deque),
            condition_id=condition_id,
        )

    async def get(self) -> Dict[str, Any]:
        """Get the next item from the queue, blocking if empty."""
        while True:
            async with self._lock:
                if self._deque:
                    item = self._deque.popleft()
                    if not self._deque:
                        self._not_empty.clear()
                    self._update_queue_depth_metric()
                    return item
            self._not_empty.clear()
            await self._not_empty.wait()

    def task_done(self) -> None:
        """Mark a task as done."""
        self._unfinished_tasks -= 1
        if self._unfinished_tasks <= 0:
            self._unfinished_tasks = 0
            self._all_done.set()
        self._update_queue_depth_metric()

    def _update_queue_depth_metric(self) -> None:
        """Update the queue depth gauge if metrics are available."""
        if self._metrics is not None:
            import asyncio as _asyncio
            try:
                loop = _asyncio.get_running_loop()
                loop.create_task(self._metrics.set_evaluation_queue_depth(len(self._deque)))
            except RuntimeError:
                pass  # No running loop (e.g. in sync context)

    async def join(self) -> None:
        """Block until all items have been processed."""
        await self._all_done.wait()

    def qsize(self) -> int:
        """Return the current queue size."""
        return len(self._deque)

    def empty(self) -> bool:
        """Return True if the queue is empty."""
        return len(self._deque) == 0

    def full(self) -> bool:
        """Return True if the queue is full."""
        return len(self._deque) >= self._max_size

    def snapshot(self) -> PromptQueueDepthSnapshot:
        """Return a snapshot of the current queue depth."""
        return PromptQueueDepthSnapshot(
            current_depth=len(self._deque),
            max_size=self._max_size,
        )

    @property
    def max_size(self) -> int:
        return self._max_size

    @property
    def coalescing(self) -> bool:
        return self._coalescing
