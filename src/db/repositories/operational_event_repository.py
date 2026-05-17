"""
src/db/repositories/operational_event_repository.py

Append-only async repository for OperationalEvent persistence (WI-56).

Exposes append, batch_append, and read_window methods only.
No public update or delete methods are provided.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import structlog

from src.db.models import OperationalEvent
from src.schemas.ops import (
    OperationalEventBatchResult,
    OperationalEventCreate,
    OperationalEventPersistenceStatus,
    OperationalEventQuery,
    OperationalEventReadWindow,
    OperationalEventRecord,
)

logger = structlog.get_logger(__name__)


def _model_to_record(model: OperationalEvent) -> OperationalEventRecord:
    """Convert an ORM model to a frozen Pydantic record."""
    return OperationalEventRecord(
        id=model.id,
        event_type=model.event_type,  # type: ignore[arg-type]
        severity=model.severity,  # type: ignore[arg-type]
        source=model.source,  # type: ignore[arg-type]
        reason_code=model.reason_code,  # type: ignore[arg-type]
        payload_json=model.payload_json,
        persistence_status=model.persistence_status,  # type: ignore[arg-type]
        created_at_utc=model.created_at_utc,
        recorded_at_utc=model.recorded_at_utc,
    )


def _create_to_model(event: OperationalEventCreate) -> OperationalEvent:
    """Convert a Pydantic create schema to an ORM model, ready for insert."""
    import uuid

    return OperationalEvent(
        id=str(uuid.uuid4()),
        event_type=event.event_type.value,
        severity=event.severity.value,
        source=event.source.value,
        reason_code=event.reason_code.value,
        payload_json=event.payload.model_dump_json(exclude_none=True),
        persistence_status=OperationalEventPersistenceStatus.PERSISTED.value,
        created_at_utc=event.timestamp_utc,
        recorded_at_utc=datetime.now(timezone.utc),
    )


class OperationalEventRepository:
    """Append-only repository for operational event persistence.

    This is the ONLY allowed path for writing operational events.
    Agent code must not hold raw database sessions for event writes.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def append(self, event: OperationalEventCreate) -> OperationalEventRecord:
        """Persist a single operational event and return its record."""
        model = _create_to_model(event)
        self._session.add(model)
        await self._session.flush()
        logger.debug(
            "operational_event.appended",
            event_type=model.event_type,
            severity=model.severity,
        )
        return _model_to_record(model)

    async def batch_append(
        self, events: list[OperationalEventCreate]
    ) -> OperationalEventBatchResult:
        """Persist a batch of operational events.

        Each event is inserted individually; failures are caught per-event
        so one bad event does not block the remainder.
        """
        import uuid

        batch_id = str(uuid.uuid4())
        records: list[OperationalEventRecord] = []
        succeeded = 0
        failed = 0

        for event in events:
            try:
                record = await self.append(event)
                records.append(record)
                succeeded += 1
            except Exception:
                failed += 1
                logger.warning(
                    "operational_event.batch_append_failed",
                    event_type=event.event_type.value,
                    reason_code=event.reason_code.value,
                )

        return OperationalEventBatchResult(
            batch_id=batch_id,
            total=len(events),
            succeeded=succeeded,
            failed=failed,
            dropped=0,
            records=records,
        )

    async def read_window(
        self, query: OperationalEventQuery
    ) -> OperationalEventReadWindow:
        """Read operational events matching the query within the given time window.

        Returns a bounded result window; pagination is handled via the
        ``has_more`` flag.
        """
        conditions: list = []

        if query.event_types:
            conditions.append(
                OperationalEvent.event_type.in_([t.value for t in query.event_types])
            )
        if query.severities:
            conditions.append(
                OperationalEvent.severity.in_([s.value for s in query.severities])
            )
        if query.sources:
            conditions.append(
                OperationalEvent.source.in_([s.value for s in query.sources])
            )
        if query.reason_codes:
            conditions.append(
                OperationalEvent.reason_code.in_([r.value for r in query.reason_codes])
            )
        if query.start_time_utc:
            conditions.append(OperationalEvent.created_at_utc >= query.start_time_utc)
        if query.end_time_utc:
            conditions.append(OperationalEvent.created_at_utc <= query.end_time_utc)

        # Count total
        count_stmt = select(func.count(OperationalEvent.id))
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        count_result = await self._session.execute(count_stmt)
        total_count = count_result.scalar_one() or 0

        # Fetch page
        stmt = select(OperationalEvent).order_by(OperationalEvent.created_at_utc.desc())
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = stmt.offset(query.offset).limit(query.limit)
        result = await self._session.execute(stmt)
        models = list(result.scalars().all())

        events = [_model_to_record(m) for m in models]

        # has_more accounts for the offset cursor so callers paginating
        # through a large window terminate correctly on the last page.
        # Equivalent to the legacy ``total_count > limit`` semantics when
        # offset == 0.
        return OperationalEventReadWindow(
            events=events,
            start_time_utc=query.start_time_utc,
            end_time_utc=query.end_time_utc,
            total_count=total_count,
            has_more=(query.offset + len(events)) < total_count,
        )
