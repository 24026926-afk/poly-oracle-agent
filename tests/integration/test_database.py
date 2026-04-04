"""
tests/integration/test_database.py

Integration tests for database persistence of MarketSnapshot records,
including yes_token_id and no_token_id column validation.
"""

from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.db.models import MarketSnapshot
from src.db.repositories.market_repo import MarketRepository


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_snapshot_payload(
    *,
    condition_id: str = "0x-test-condition-id",
    yes_token_id: str | None = "yes-token-abc123",
    no_token_id: str | None = "no-token-xyz789",
) -> dict:
    """Return keyword arguments suitable for MarketSnapshot construction."""
    return {
        "condition_id": condition_id,
        "question": "Will the system pass this test?",
        "best_bid": 0.45,
        "best_ask": 0.46,
        "last_trade_price": 0.455,
        "midpoint": 0.455,
        "outcome_token": "YES",
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
        "raw_ws_payload": json.dumps({
            "event": "book",
            "market": condition_id,
            "bids": [{"price": 0.45, "size": 100}],
            "asks": [{"price": 0.46, "size": 100}],
        }),
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_snapshot_with_token_ids(async_session: AsyncSession) -> None:
    """Verify that a MarketSnapshot with yes_token_id and no_token_id persists."""
    repo = MarketRepository(async_session)
    payload = _make_snapshot_payload()
    snapshot = MarketSnapshot(**payload)

    saved = await repo.insert_snapshot(snapshot)
    await async_session.commit()

    # Re-read from DB and verify
    assert saved.id is not None
    assert saved.yes_token_id == "yes-token-abc123"
    assert saved.no_token_id == "no-token-xyz789"


@pytest.mark.asyncio
async def test_query_snapshot_by_condition_id(async_engine) -> None:
    """Verify that snapshots can be retrieved by condition_id after insertion."""
    session_factory = async_sessionmaker(
        bind=async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )

    async with session_factory() as session:
        repo = MarketRepository(session)
        condition_id = f"0x-query-test-{uuid.uuid4().hex[:8]}"
        snapshot = MarketSnapshot(**_make_snapshot_payload(condition_id=condition_id))

        await repo.insert_snapshot(snapshot)
        await session.commit()

        fetched = await repo.get_latest_by_condition_id(condition_id)
        assert fetched is not None
        assert fetched.condition_id == condition_id
        assert fetched.yes_token_id == "yes-token-abc123"
        assert fetched.no_token_id == "no-token-xyz789"


@pytest.mark.asyncio
async def test_insert_snapshot_nullable_token_ids(async_session: AsyncSession) -> None:
    """Verify that snapshots with NULL token_ids are accepted (nullable columns)."""
    repo = MarketRepository(async_session)
    payload = _make_snapshot_payload(yes_token_id=None, no_token_id=None)
    snapshot = MarketSnapshot(**payload)

    saved = await repo.insert_snapshot(snapshot)
    await async_session.commit()

    assert saved.yes_token_id is None
    assert saved.no_token_id is None