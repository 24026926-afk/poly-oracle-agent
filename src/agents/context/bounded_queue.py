"""
src/agents/context/bounded_queue.py

Bounded prompt queue with coalescing and stale-drop fallback for WI-53.

Uses a deque-based design so coalescing can replace items in-place without
draining/re-enqueueing (which would break asyncio.Queue task accounting).
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
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
        self._budget_quarantine_until: dict[str, datetime] = {}

    async def put(
        self,
        item: Dict[str, Any],
    ) -> PromptQueueBackpressureDecision:
        """Attempt to enqueue a prompt payload."""
        condition_id = item.get("state", {}).get("condition_id")
        if condition_id and self._is_budget_quarantined(condition_id):
            logger.debug(
                "queue.budget_quarantined_drop",
                reason=PromptQueueBackpressureReason.BUDGET_QUARANTINED.value,
                queue_depth=len(self._deque),
            )
            return PromptQueueBackpressureDecision(
                action="drop",
                reason=PromptQueueBackpressureReason.BUDGET_QUARANTINED,
                queue_depth=len(self._deque),
                condition_id=condition_id,
            )

        # Resolve the decision under the lock without holding it across any await.
        # _coalesce_sync and _drop_stale_sync are fully synchronous so the lock
        # is never held across I/O — eliminates starvation of get() callers.
        record_coalesced = False
        record_dropped = False

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

            # Queue is full — resolve synchronously inside the lock
            if self._coalescing and condition_id:
                decision, record_coalesced, record_dropped = self._coalesce_sync(
                    item, condition_id
                )
            else:
                decision, record_dropped = self._drop_stale_sync(item, condition_id)

        # Metrics are recorded OUTSIDE the lock so no await contends with get()
        if self._metrics is not None:
            if record_coalesced:
                await self._metrics.record_coalesced_context()
            if record_dropped:
                await self._metrics.record_dropped_stale_context()
            self._update_queue_depth_metric()

        return decision

    def _coalesce_sync(
        self,
        item: Dict[str, Any],
        condition_id: str,
    ) -> tuple[PromptQueueBackpressureDecision, bool, bool]:
        """Replace the stale payload for the same market in-place (sync, lock held).

        Returns (decision, record_coalesced_flag, record_dropped_flag).
        """
        for i, existing in enumerate(self._deque):
            existing_cid = existing.get("state", {}).get("condition_id")
            if existing_cid == condition_id:
                self._deque[i] = item
                logger.debug(
                    "queue.coalesced",
                    reason=PromptQueueBackpressureReason.COALESCED.value,
                    queue_depth=len(self._deque),
                )
                return (
                    PromptQueueBackpressureDecision(
                        action="coalesce",
                        reason=PromptQueueBackpressureReason.COALESCED,
                        queue_depth=len(self._deque),
                        condition_id=condition_id,
                    ),
                    True,
                    False,
                )

        # No matching market — fall through to drop-stale
        decision, record_dropped = self._drop_stale_sync(item, condition_id)
        return decision, False, record_dropped

    def _drop_stale_sync(
        self,
        item: Dict[str, Any],
        condition_id: Optional[str],
    ) -> tuple[PromptQueueBackpressureDecision, bool]:
        """Drop the oldest item and enqueue the new one (sync, lock held).

        WI-53: Discards the stalest payload and enqueues fresh context so the
        queue always holds the most recent market data.

        Returns (decision, record_dropped_flag).
        """
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

        self._deque.append(item)
        self._unfinished_tasks += 1
        self._all_done.clear()
        self._not_empty.set()

        return (
            PromptQueueBackpressureDecision(
                action="drop",
                reason=PromptQueueBackpressureReason.STALE_DROPPED,
                queue_depth=len(self._deque),
                condition_id=condition_id,
            ),
            True,
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
                loop.create_task(
                    self._metrics.set_evaluation_queue_depth(len(self._deque))
                )
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

    async def quarantine_market_until(
        self,
        condition_id: str,
        until_utc: datetime | None,
    ) -> None:
        """Temporarily drop queued snapshots for a market blocked by LLM budget."""
        if not condition_id or until_utc is None:
            return
        if until_utc.tzinfo is None:
            until_utc = until_utc.replace(tzinfo=timezone.utc)
        async with self._lock:
            current = self._budget_quarantine_until.get(condition_id)
            if current is None or until_utc > current:
                self._budget_quarantine_until[condition_id] = until_utc
            retained: deque[Dict[str, Any]] = deque(maxlen=self._max_size)
            removed = 0
            for item in self._deque:
                existing_cid = item.get("state", {}).get("condition_id")
                if existing_cid == condition_id:
                    removed += 1
                    continue
                retained.append(item)
            if removed == 0:
                return

            self._deque = retained
            self._unfinished_tasks = max(0, self._unfinished_tasks - removed)
            if self._deque:
                self._not_empty.set()
            else:
                self._not_empty.clear()
            if self._unfinished_tasks == 0:
                self._all_done.set()
            self._update_queue_depth_metric()
            logger.info(
                "queue.budget_quarantine_drained",
                reason=PromptQueueBackpressureReason.BUDGET_QUARANTINED.value,
                queue_depth=len(self._deque),
                dropped_count=removed,
            )

    def _is_budget_quarantined(self, condition_id: str) -> bool:
        until_utc = self._budget_quarantine_until.get(condition_id)
        if until_utc is None:
            return False
        now = datetime.now(timezone.utc)
        if now >= until_utc:
            self._budget_quarantine_until.pop(condition_id, None)
            return False
        return True
