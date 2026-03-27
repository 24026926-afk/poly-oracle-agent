"""
tests/unit/test_bankroll_tracker.py

Async unit tests for BankrollPortfolioTracker.

Validates:
    - Bankroll queries (total, exposure, available)
    - Quarter-Kelly position sizing with 3% cap
    - Pre-trade validation and rejection
    - All math uses Decimal (never float)
    - Restart recovery from persisted DB state
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.agents.execution.bankroll_tracker import BankrollPortfolioTracker
from src.core.exceptions import ExposureLimitError
from src.db.models import Base, ExecutionTx, TxStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(
    bankroll: Decimal = Decimal("1000"),
    kelly_frac: float = 0.25,
    max_exposure: float = 0.03,
) -> "FakeConfig":
    """Minimal config stub matching the fields tracker reads."""

    class FakeConfig:
        initial_bankroll_usdc = bankroll
        kelly_fraction = kelly_frac
        max_exposure_pct = max_exposure

    return FakeConfig()


def _make_execution_tx(
    condition_id: str,
    size_usdc: float,
    status: TxStatus = TxStatus.CONFIRMED,
    decision_id: str | None = None,
) -> ExecutionTx:
    """Create an ExecutionTx row for seeding the test DB."""
    import uuid

    return ExecutionTx(
        decision_id=decision_id or str(uuid.uuid4()),
        tx_hash=f"0x{uuid.uuid4().hex}",
        status=status,
        side="BUY",
        size_usdc=size_usdc,
        limit_price=0.60,
        condition_id=condition_id,
        outcome_token="YES",
    )


@pytest_asyncio.fixture()
async def db_factory():
    """In-memory async SQLite engine + session factory for tests."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    yield factory

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


async def _seed_executions(
    db_factory: async_sessionmaker[AsyncSession],
    rows: list[ExecutionTx],
) -> None:
    """Insert execution rows into test DB."""
    async with db_factory() as session:
        for row in rows:
            session.add(row)
        await session.commit()


# ---------------------------------------------------------------------------
# Tests — Bankroll queries
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_total_bankroll_returns_config_value(db_factory):
    config = _make_config(bankroll=Decimal("5000"))
    tracker = BankrollPortfolioTracker(config, db_factory)

    result = await tracker.get_total_bankroll()

    assert result == Decimal("5000")
    assert isinstance(result, Decimal)


@pytest.mark.asyncio
async def test_exposure_returns_zero_for_empty_db(db_factory):
    tracker = BankrollPortfolioTracker(_make_config(), db_factory)

    exposure = await tracker.get_exposure("cond-abc")

    assert exposure == Decimal("0")
    assert isinstance(exposure, Decimal)


@pytest.mark.asyncio
async def test_exposure_sums_pending_and_confirmed(db_factory):
    await _seed_executions(db_factory, [
        _make_execution_tx("cond-1", 10.0, TxStatus.CONFIRMED),
        _make_execution_tx("cond-1", 20.0, TxStatus.PENDING),
        _make_execution_tx("cond-1", 5.0, TxStatus.FAILED),  # excluded
        _make_execution_tx("cond-2", 100.0, TxStatus.CONFIRMED),  # diff market
    ])
    tracker = BankrollPortfolioTracker(_make_config(), db_factory)

    exposure = await tracker.get_exposure("cond-1")

    assert exposure == Decimal("30")


@pytest.mark.asyncio
async def test_available_bankroll_subtracts_exposure(db_factory):
    await _seed_executions(db_factory, [
        _make_execution_tx("cond-1", 200.0, TxStatus.CONFIRMED),
    ])
    config = _make_config(bankroll=Decimal("1000"))
    tracker = BankrollPortfolioTracker(config, db_factory)

    available = await tracker.get_available_bankroll("cond-1")

    assert available == Decimal("800")


@pytest.mark.asyncio
async def test_available_bankroll_floors_at_zero(db_factory):
    await _seed_executions(db_factory, [
        _make_execution_tx("cond-1", 1500.0, TxStatus.CONFIRMED),
    ])
    config = _make_config(bankroll=Decimal("1000"))
    tracker = BankrollPortfolioTracker(config, db_factory)

    available = await tracker.get_available_bankroll("cond-1")

    assert available == Decimal("0")


# ---------------------------------------------------------------------------
# Tests — Position sizing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_compute_position_size_quarter_kelly(db_factory):
    """f* = 0.10 → f_quarter = 0.25 × 0.10 = 0.025 → 25 USDC on 1000 bankroll."""
    config = _make_config(bankroll=Decimal("1000"))
    tracker = BankrollPortfolioTracker(config, db_factory)

    size = await tracker.compute_position_size(
        kelly_fraction_raw=Decimal("0.10"),
        condition_id="cond-1",
    )

    # 0.25 × 0.10 × 1000 = 25
    assert size == Decimal("25.0")
    assert isinstance(size, Decimal)


@pytest.mark.asyncio
async def test_compute_position_size_capped_at_3pct(db_factory):
    """f* = 0.50 → f_quarter = 0.125 → 125 USDC, but cap = 0.03 × 1000 = 30."""
    config = _make_config(bankroll=Decimal("1000"))
    tracker = BankrollPortfolioTracker(config, db_factory)

    size = await tracker.compute_position_size(
        kelly_fraction_raw=Decimal("0.50"),
        condition_id="cond-1",
    )

    # min(125, 30) = 30
    assert size == Decimal("30.0")


@pytest.mark.asyncio
async def test_compute_position_size_never_negative(db_factory):
    """Negative Kelly fraction should floor at zero."""
    config = _make_config(bankroll=Decimal("1000"))
    tracker = BankrollPortfolioTracker(config, db_factory)

    size = await tracker.compute_position_size(
        kelly_fraction_raw=Decimal("-0.05"),
        condition_id="cond-1",
    )

    assert size == Decimal("0")


# ---------------------------------------------------------------------------
# Tests — Trade validation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_validate_trade_passes_within_limits(db_factory):
    config = _make_config(bankroll=Decimal("1000"))
    tracker = BankrollPortfolioTracker(config, db_factory)

    # 20 USDC is below the 30 USDC cap and within available bankroll
    await tracker.validate_trade(Decimal("20"), "cond-1")  # no error


@pytest.mark.asyncio
async def test_validate_trade_raises_on_exposure_cap(db_factory):
    config = _make_config(bankroll=Decimal("1000"))
    tracker = BankrollPortfolioTracker(config, db_factory)

    # 50 USDC exceeds the 30 USDC cap (0.03 × 1000)
    with pytest.raises(ExposureLimitError, match="exposure cap"):
        await tracker.validate_trade(Decimal("50"), "cond-1")


@pytest.mark.asyncio
async def test_validate_trade_raises_on_insufficient_bankroll(db_factory):
    # Seed 990 USDC of existing exposure
    await _seed_executions(db_factory, [
        _make_execution_tx("cond-1", 990.0, TxStatus.CONFIRMED),
    ])
    config = _make_config(bankroll=Decimal("1000"))
    tracker = BankrollPortfolioTracker(config, db_factory)

    # 20 USDC exceeds available 10 USDC
    with pytest.raises(ExposureLimitError, match="available bankroll"):
        await tracker.validate_trade(Decimal("20"), "cond-1")


# ---------------------------------------------------------------------------
# Tests — Type safety
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_math_uses_decimal(db_factory):
    """Every return value must be Decimal, never float."""
    await _seed_executions(db_factory, [
        _make_execution_tx("cond-1", 50.0, TxStatus.CONFIRMED),
    ])
    config = _make_config(bankroll=Decimal("1000"))
    tracker = BankrollPortfolioTracker(config, db_factory)

    assert isinstance(await tracker.get_total_bankroll(), Decimal)
    assert isinstance(await tracker.get_exposure("cond-1"), Decimal)
    assert isinstance(await tracker.get_available_bankroll("cond-1"), Decimal)
    assert isinstance(
        await tracker.compute_position_size(Decimal("0.10"), "cond-1"),
        Decimal,
    )


# ---------------------------------------------------------------------------
# Tests — Restart recovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_recovery_from_db(db_factory):
    """Tracker reconstructs exposure state from persisted ExecutionTx rows."""
    # Simulate prior run: seed 3 executions spread across 2 markets
    await _seed_executions(db_factory, [
        _make_execution_tx("cond-A", 100.0, TxStatus.CONFIRMED),
        _make_execution_tx("cond-A", 50.0, TxStatus.PENDING),
        _make_execution_tx("cond-B", 200.0, TxStatus.CONFIRMED),
    ])

    # New tracker instance — simulates fresh startup
    config = _make_config(bankroll=Decimal("1000"))
    tracker = BankrollPortfolioTracker(config, db_factory)

    # Must pick up existing exposure without any warm-up step
    assert await tracker.get_exposure("cond-A") == Decimal("150")
    assert await tracker.get_exposure("cond-B") == Decimal("200")
    assert await tracker.get_available_bankroll("cond-A") == Decimal("850")
    assert await tracker.get_available_bankroll("cond-B") == Decimal("800")
