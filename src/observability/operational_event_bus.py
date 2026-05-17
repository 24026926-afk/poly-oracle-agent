"""
src/observability/operational_event_bus.py

Bounded async operational event bus for WI-56.

Provides a non-blocking, bounded ``asyncio.Queue``-backed event publisher
that buffers operational events and flushes them to the persistence layer
in bounded batches on a configurable interval.

Safety-critical (CRITICAL/ERROR) events are prioritized over diagnostic
(INFO) events during queue pressure.  Overflow is deterministic, typed,
and logged.  Critical persistence failures invoke the optional
``on_critical_failure`` callback for fail-closed readiness signaling.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Callable, Optional

import structlog

from src.schemas.ops import (
    OperationalEventAppendResult,
    OperationalEventCreate,
    OperationalEventFlushResult,
    OperationalEventQueueState,
    OperationalEventSeverity,
)

logger = structlog.get_logger(__name__)

# Critical severities that are never dropped during overflow
_CRITICAL_SEVERITIES = frozenset(
    {OperationalEventSeverity.CRITICAL.value, OperationalEventSeverity.ERROR.value}
)

# Diagnostic severities that can be dropped first
_DIAGNOSTIC_SEVERITIES = frozenset({OperationalEventSeverity.INFO.value})


class OperationalEventBus:
    """Bounded async operational event bus with batch flushing.

    Events are submitted via ``publish()``, which returns immediately
    with a typed ``OperationalEventAppendResult``.  A background task
    periodically flushes buffered events to the provided
    ``OperationalEventRepository`` in bounded batches.

    Parameters
    ----------
    repository_factory:
        Async callable that returns a ``(OperationalEventRepository, AsyncSession)``
        tuple.  The bus commits the session after successful persistence and closes
        it in a finally block to prevent session leaks.
    config:
        AppConfig or compatible object with ``event_ledger_*`` fields.
    metrics:
        Optional MetricsRegistry for low-cardinality counters/gauges.
    on_critical_failure:
        Optional async callback invoked when a critical event persistence
        failure occurs.  Used for fail-closed readiness signaling.
    """

    def __init__(
        self,
        repository_factory,
        config,
        metrics=None,
        on_critical_failure: Optional[Callable[[], None]] = None,
    ) -> None:
        self._repository_factory = repository_factory
        self._config = config
        self._metrics = metrics
        self._on_critical_failure = on_critical_failure

        self._queue: asyncio.Queue[OperationalEventCreate] = asyncio.Queue(
            maxsize=config.event_ledger_queue_size
        )
        self._dropped_total: int = 0
        self._overflowed: bool = False
        self._last_overflow_at_utc: Optional[datetime] = None
        self._diagnostic_count: int = 0

        self._flush_task: Optional[asyncio.Task] = None
        self._running: bool = False

    # ── Public API ───────────────────────────────────────────────────────

    async def publish(
        self, event: OperationalEventCreate
    ) -> OperationalEventAppendResult:
        """Submit an operational event to the bounded queue.

        Records an append-attempt metric for every call.
        """
        self._record_append_attempt(event.event_type.value)
        try:
            self._queue.put_nowait(event)
            if event.severity.value in _DIAGNOSTIC_SEVERITIES:
                self._diagnostic_count += 1
            self._maybe_update_depth_metric()
            return OperationalEventAppendResult(
                accepted=True,
                queue_depth=self._queue.qsize(),
            )
        except asyncio.QueueFull:
            return self._handle_overflow(event)

    async def start(self) -> None:
        """Start the background flush loop."""
        if self._running:
            return
        self._running = True
        self._flush_task = asyncio.create_task(
            self._flush_loop(), name="OperationalEventBus-FlushLoop"
        )
        logger.info(
            "operational_event_bus.started",
            queue_maxsize=self._config.event_ledger_queue_size,
        )

    async def stop(self) -> None:
        """Stop the bus, drain remaining events with a timeout."""
        if not self._running:
            return
        self._running = False

        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        timeout = float(self._config.event_ledger_shutdown_flush_timeout_sec)
        try:
            await asyncio.wait_for(self._flush_pending(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "operational_event_bus.shutdown_flush_timeout",
                remaining=self._queue.qsize(),
                timeout_seconds=timeout,
            )

        queue_state = self.queue_state()
        logger.info(
            "operational_event_bus.stopped",
            final_depth=queue_state.current_depth,
            dropped_total=queue_state.dropped_total,
        )

    def queue_state(self) -> OperationalEventQueueState:
        """Return a snapshot of the current queue state."""
        return OperationalEventQueueState(
            current_depth=self._queue.qsize(),
            max_capacity=self._config.event_ledger_queue_size,
            dropped_total=self._dropped_total,
            overflow=self._overflowed,
            last_overflow_at_utc=self._last_overflow_at_utc,
        )

    # ── Internal: Overflow handling ──────────────────────────────────────

    def _is_critical(self, event: OperationalEventCreate) -> bool:
        return event.severity.value in _CRITICAL_SEVERITIES

    def _signal_critical_failure(self) -> None:
        if self._on_critical_failure is None:
            return
        try:
            self._on_critical_failure()
        except Exception:
            pass

    def _mark_overflow(self, severity: str) -> None:
        self._overflowed = True
        self._last_overflow_at_utc = datetime.now(timezone.utc)
        self._record_overflow_metric(severity)

    def _handle_overflow(
        self, event: OperationalEventCreate
    ) -> OperationalEventAppendResult:
        """Apply the configured overflow policy.

        Never drops critical events.  For ``drop_oldest``, the oldest
        *non-critical* event is found and removed.  If every item in the
        queue is critical, the incoming event is rejected.
        """
        policy = self._config.event_ledger_overflow_policy
        event_sev = event.severity.value

        # Try to make room by removing a diagnostic (INFO) event first
        if self._pop_diagnostic():
            try:
                self._queue.put_nowait(event)
                self._mark_overflow(event_sev)
                return OperationalEventAppendResult(
                    accepted=True,
                    queue_depth=self._queue.qsize(),
                )
            except asyncio.QueueFull:
                pass

        if policy == "drop_oldest":
            return self._overflow_drop_oldest_safe(event, event_sev)
        elif policy == "drop_newest":
            return self._overflow_drop_newest(event, event_sev)
        elif self._is_critical(event):
            return self._overflow_drop_oldest_safe(event, event_sev)
        return self._overflow_reject(event, event_sev)

    def _overflow_drop_oldest_safe(
        self, event: OperationalEventCreate, event_sev: str
    ) -> OperationalEventAppendResult:
        """Remove the oldest NON-CRITICAL event to make room.

        If the queue contains only critical events, reject the incoming
        event (fix #2: never drop critical queued events).
        """
        drained: list[OperationalEventCreate] = []
        removed = False

        while not self._queue.empty():
            try:
                oldest = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not removed and oldest.severity.value not in _CRITICAL_SEVERITIES:
                self._queue.task_done()
                self._dropped_total += 1
                if oldest.severity.value in _DIAGNOSTIC_SEVERITIES:
                    self._diagnostic_count -= 1
                removed = True
            else:
                drained.append(oldest)

        # Re-enqueue all except the removed one
        for item in drained:
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                self._dropped_total += 1

        if not removed:
            # Queue is all critical — reject the incoming event
            self._record_dropped_metric("queue_full_all_critical")
            self._mark_overflow(event_sev)
            if self._is_critical(event):
                self._signal_critical_failure()
            return OperationalEventAppendResult(
                accepted=False,
                reason="queue_full",
                queue_depth=self._queue.qsize(),
            )

        # Now try to enqueue the incoming event
        try:
            self._queue.put_nowait(event)
            if event.severity.value in _DIAGNOSTIC_SEVERITIES:
                self._diagnostic_count += 1
            self._mark_overflow(event_sev)
            return OperationalEventAppendResult(
                accepted=True,
                queue_depth=self._queue.qsize(),
            )
        except asyncio.QueueFull:
            self._record_dropped_metric("queue_full")
            self._mark_overflow(event_sev)
            if self._is_critical(event):
                self._signal_critical_failure()
            return OperationalEventAppendResult(
                accepted=False,
                reason="queue_full",
                queue_depth=self._queue.qsize(),
            )

    def _overflow_drop_newest(
        self, event: OperationalEventCreate, event_sev: str
    ) -> OperationalEventAppendResult:
        """Reject the incoming event — drop_newest policy.

        However, incoming CRITICAL/ERROR events are never rejected.
        Instead, try to make room by dropping the oldest non-critical
        queued event.  If the queue is entirely critical, reject.
        """
        if event_sev in _CRITICAL_SEVERITIES:
            return self._overflow_drop_oldest_safe(event, event_sev)

        self._dropped_total += 1
        self._mark_overflow(event_sev)
        logger.warning(
            "operational_event_bus.overflow_drop_newest",
            event_type=event.event_type.value,
            severity=event_sev,
        )
        self._record_dropped_metric("queue_full")
        return OperationalEventAppendResult(
            accepted=False,
            reason="queue_full",
            queue_depth=self._queue.qsize(),
        )

    def _overflow_reject(
        self, event: OperationalEventCreate, event_sev: str
    ) -> OperationalEventAppendResult:
        """Reject the incoming event (used by drop_diagnostic after pop fails)."""
        self._dropped_total += 1
        self._mark_overflow(event_sev)
        self._record_dropped_metric("queue_full")
        if self._is_critical(event):
            self._signal_critical_failure()
        return OperationalEventAppendResult(
            accepted=False,
            reason="queue_full",
            queue_depth=self._queue.qsize(),
        )

    def _pop_diagnostic(self) -> bool:
        """Remove one diagnostic (INFO) event from the queue to make room.

        Drains the queue looking for a diagnostic event, re-enqueuing
        non-diagnostic events.  O(n) but only runs during overflow (rare).

        Returns True if a diagnostic event was removed.
        """
        if self._diagnostic_count <= 0:
            return False

        buffer: list[OperationalEventCreate] = []
        removed = False

        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not removed and item.severity.value in _DIAGNOSTIC_SEVERITIES:
                self._queue.task_done()
                self._dropped_total += 1
                self._diagnostic_count -= 1
                removed = True
            else:
                buffer.append(item)

        for item in buffer:
            try:
                self._queue.put_nowait(item)
            except asyncio.QueueFull:
                self._dropped_total += 1

        return removed

    # ── Internal: Flush loop ─────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        """Periodic background task that flushes events in bounded batches."""
        while self._running:
            try:
                interval = float(self._config.event_ledger_flush_interval_sec)
                await asyncio.sleep(interval)
                if not self._running:
                    break
                await self._flush_batch()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("operational_event_bus.flush_loop_error")

    async def _flush_batch(self) -> OperationalEventFlushResult:
        """Drain and persist a batch, committing and closing the session."""
        batch_size = self._config.event_ledger_batch_size
        batch: list[OperationalEventCreate] = []
        info_count = 0

        while len(batch) < batch_size:
            try:
                event = self._queue.get_nowait()
                batch.append(event)
                if event.severity.value in _DIAGNOSTIC_SEVERITIES:
                    info_count += 1
            except asyncio.QueueEmpty:
                break

        if not batch:
            return OperationalEventFlushResult(
                batch_id="",
                persisted=0,
                dropped=0,
                failed=0,
            )

        batch_count = len(batch)
        start = asyncio.get_event_loop().time()
        session = None

        try:
            # Factory returns (repository, session) tuple for proper lifecycle
            factory_result = await self._repository_factory()
            if isinstance(factory_result, tuple):
                repository, session = factory_result
            else:
                repository = factory_result

            result = await repository.batch_append(batch)

            # Commit + close for durability and no session leaks
            if session is not None:
                await session.commit()
                await session.close()

            persisted = result.succeeded
            failed = result.failed
            has_critical = any(e.severity.value in _CRITICAL_SEVERITIES for e in batch)

            for _ in range(batch_count):
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

            self._diagnostic_count = max(0, self._diagnostic_count - info_count)

            if self._metrics is not None:
                for event in batch:
                    if any(
                        r.event_type.value == event.event_type.value
                        and r.reason_code.value == event.reason_code.value
                        for r in result.records
                    ):
                        try:
                            await self._metrics.record_event_persisted(
                                event_type=event.event_type.value,
                                severity=event.severity.value,
                            )
                        except Exception:
                            pass
                if failed > 0:
                    try:
                        await self._metrics.record_event_flush_failure(
                            reason="partial_persist_failure"
                        )
                    except Exception:
                        pass

            if failed > 0 and has_critical:
                self._signal_critical_failure()

            elapsed = (asyncio.get_event_loop().time() - start) * 1000
            logger.debug(
                "operational_event_bus.flush_batch",
                persisted=persisted,
                failed=failed,
                batch_size=batch_count,
                duration_ms=round(elapsed, 2),
            )

            return OperationalEventFlushResult(
                batch_id="",
                persisted=persisted,
                dropped=0,
                failed=failed,
                flush_duration_ms=elapsed,
            )
        except Exception:
            failed_count = batch_count
            for _ in range(failed_count):
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

            # Close session on failure too
            if session is not None:
                try:
                    await session.close()
                except Exception:
                    pass

            has_critical = any(e.severity.value in _CRITICAL_SEVERITIES for e in batch)

            if self._metrics is not None:
                try:
                    await self._metrics.record_event_flush_failure(
                        reason="persist_exception"
                    )
                except Exception:
                    pass

            logger.error(
                "operational_event_bus.flush_failed",
                event_count=len(batch),
                has_critical=has_critical,
            )

            # Fail-closed: invoke callback for readiness degradation
            if has_critical and self._on_critical_failure is not None:
                try:
                    self._on_critical_failure()
                except Exception:
                    pass

            return OperationalEventFlushResult(
                batch_id="",
                persisted=0,
                dropped=0,
                failed=failed_count,
            )

    async def _flush_pending(self) -> None:
        """Drain all remaining events during shutdown."""
        while not self._queue.empty():
            await self._flush_batch()

    # ── Metric helpers ───────────────────────────────────────────────────

    def _record_append_attempt(self, event_type: str) -> None:
        if self._metrics is not None:
            try:
                asyncio.ensure_future(
                    self._metrics.record_event_append_attempt(event_type=event_type)
                )
            except Exception:
                pass

    def _record_overflow_metric(self, severity: str) -> None:
        if self._metrics is not None:
            try:
                asyncio.ensure_future(
                    self._metrics.record_event_queue_overflow(severity=severity)
                )
            except Exception:
                pass

    def _record_dropped_metric(self, reason: str) -> None:
        if self._metrics is not None:
            try:
                asyncio.ensure_future(self._metrics.record_event_dropped(reason=reason))
            except Exception:
                pass

    def _maybe_update_depth_metric(self) -> None:
        if self._metrics is not None:
            try:
                asyncio.ensure_future(
                    self._metrics.set_event_queue_depth(self._queue.qsize())
                )
            except Exception:
                pass
