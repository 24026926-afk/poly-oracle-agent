"""
tests/unit/test_repositories.py

Unit tests for MarketRepository, DecisionRepository, ExecutionRepository.
All tests run against an async in-memory SQLite database.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.db.models import (
    AgentDecisionLog,
    DecisionAction,
    ExecutionTx,
    MarketSnapshot,
    TxStatus,
)
from src.db.repositories import (
    DecisionRepository,
    ExecutionRepository,
    MarketRepository,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    condition_id: str = "cond_abc",
    captured_at: datetime | None = None,
) -> MarketSnapshot:
    """Build a MarketSnapshot with sensible defaults."""
    return MarketSnapshot(
        id=str(uuid.uuid4()),
        condition_id=condition_id,
        question="Will it rain tomorrow?",
        best_bid=0.45,
        best_ask=0.46,
        last_trade_price=0.455,
        midpoint=0.455,
        bid_liquidity_usdc=1000.0,
        ask_liquidity_usdc=1200.0,
        outcome_token="YES",
        raw_ws_payload='{"type":"book"}',
        captured_at=captured_at or datetime.now(timezone.utc),
    )


def _make_decision(
    snapshot_id: str,
    evaluated_at: datetime | None = None,
) -> AgentDecisionLog:
    """Build an AgentDecisionLog with sensible defaults."""
    return AgentDecisionLog(
        id=str(uuid.uuid4()),
        snapshot_id=snapshot_id,
        confidence_score=0.85,
        expected_value=0.10,
        decision_boolean=True,
        recommended_action=DecisionAction.BUY,
        implied_probability=0.70,
        reasoning_log="CoT reasoning text",
        prompt_version="v1.0.0",
        llm_model_id="claude-3-5-sonnet-20241022",
        input_tokens=500,
        output_tokens=200,
        evaluated_at=evaluated_at or datetime.now(timezone.utc),
    )


def _make_execution(
    decision_id: str,
    condition_id: str = "cond_abc",
    size_usdc: float = 10.0,
    status: TxStatus = TxStatus.PENDING,
) -> ExecutionTx:
    """Build an ExecutionTx with sensible defaults."""
    return ExecutionTx(
        id=str(uuid.uuid4()),
        decision_id=decision_id,
        side="BUY",
        size_usdc=size_usdc,
        limit_price=0.46,
        condition_id=condition_id,
        outcome_token="YES",
        status=status,
    )


# ---------------------------------------------------------------------------
# MarketRepository
# ---------------------------------------------------------------------------


class TestMarketRepository:
    @pytest.mark.asyncio
    async def test_insert_and_get_latest_snapshot(self, async_session):
        repo = MarketRepository(async_session)

        old = _make_snapshot(
            captured_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        new = _make_snapshot(
            captured_at=datetime.now(timezone.utc),
        )
        await repo.insert_snapshot(old)
        await repo.insert_snapshot(new)

        latest = await repo.get_latest_by_condition_id("cond_abc")
        assert latest is not None
        assert latest.id == new.id

    @pytest.mark.asyncio
    async def test_get_latest_returns_none(self, async_session):
        repo = MarketRepository(async_session)
        result = await repo.get_latest_by_condition_id("nonexistent")
        assert result is None


# ---------------------------------------------------------------------------
# DecisionRepository
# ---------------------------------------------------------------------------


class TestDecisionRepository:
    @pytest.mark.asyncio
    async def test_insert_and_get_recent_decisions(self, async_session):
        market_repo = MarketRepository(async_session)
        decision_repo = DecisionRepository(async_session)

        snap = _make_snapshot()
        await market_repo.insert_snapshot(snap)

        decisions = []
        for i in range(3):
            d = _make_decision(
                snapshot_id=snap.id,
                evaluated_at=datetime.now(timezone.utc) + timedelta(seconds=i),
            )
            await decision_repo.insert_decision(d)
            decisions.append(d)

        recent = await decision_repo.get_recent_by_market("cond_abc", limit=2)
        assert len(recent) == 2
        # Newest first
        assert recent[0].id == decisions[2].id
        assert recent[1].id == decisions[1].id

    @pytest.mark.asyncio
    async def test_get_recent_decisions_filters_by_market(self, async_session):
        market_repo = MarketRepository(async_session)
        decision_repo = DecisionRepository(async_session)

        snap_a = _make_snapshot(condition_id="market_a")
        snap_b = _make_snapshot(condition_id="market_b")
        await market_repo.insert_snapshot(snap_a)
        await market_repo.insert_snapshot(snap_b)

        dec_a = _make_decision(snapshot_id=snap_a.id)
        dec_b = _make_decision(snapshot_id=snap_b.id)
        await decision_repo.insert_decision(dec_a)
        await decision_repo.insert_decision(dec_b)

        results = await decision_repo.get_recent_by_market("market_a")
        assert len(results) == 1
        assert results[0].id == dec_a.id


# ---------------------------------------------------------------------------
# ExecutionRepository
# ---------------------------------------------------------------------------


class TestExecutionRepository:
    @pytest.mark.asyncio
    async def test_insert_and_get_execution_by_decision(self, async_session):
        market_repo = MarketRepository(async_session)
        decision_repo = DecisionRepository(async_session)
        exec_repo = ExecutionRepository(async_session)

        snap = _make_snapshot()
        await market_repo.insert_snapshot(snap)

        dec = _make_decision(snapshot_id=snap.id)
        await decision_repo.insert_decision(dec)

        tx = _make_execution(decision_id=dec.id)
        await exec_repo.insert_execution(tx)

        found = await exec_repo.get_by_decision_id(dec.id)
        assert found is not None
        assert found.id == tx.id

    @pytest.mark.asyncio
    async def test_get_by_decision_id_returns_none(self, async_session):
        exec_repo = ExecutionRepository(async_session)
        result = await exec_repo.get_by_decision_id("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_aggregate_exposure_sums_pending_confirmed(self, async_session):
        market_repo = MarketRepository(async_session)
        decision_repo = DecisionRepository(async_session)
        exec_repo = ExecutionRepository(async_session)

        snap = _make_snapshot(condition_id="cond_xyz")
        await market_repo.insert_snapshot(snap)

        # Create 3 decisions with 3 execution statuses
        dec1 = _make_decision(snapshot_id=snap.id)
        dec2 = _make_decision(snapshot_id=snap.id)
        dec3 = _make_decision(snapshot_id=snap.id)
        for d in (dec1, dec2, dec3):
            await decision_repo.insert_decision(d)

        await exec_repo.insert_execution(
            _make_execution(dec1.id, "cond_xyz", 15.0, TxStatus.PENDING)
        )
        await exec_repo.insert_execution(
            _make_execution(dec2.id, "cond_xyz", 25.0, TxStatus.CONFIRMED)
        )
        # FAILED should be excluded
        await exec_repo.insert_execution(
            _make_execution(dec3.id, "cond_xyz", 100.0, TxStatus.FAILED)
        )

        exposure = await exec_repo.get_aggregate_exposure("cond_xyz")
        assert isinstance(exposure, Decimal)
        assert exposure == Decimal("40.0")

    @pytest.mark.asyncio
    async def test_aggregate_exposure_returns_zero_on_empty(self, async_session):
        exec_repo = ExecutionRepository(async_session)
        exposure = await exec_repo.get_aggregate_exposure("no_rows")
        assert exposure == Decimal("0")
        assert isinstance(exposure, Decimal)
