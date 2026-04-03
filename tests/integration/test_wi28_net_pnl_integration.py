"""
tests/integration/test_wi28_net_pnl_integration.py

RED-phase integration tests for WI-28 Net PnL & Fee Accounting.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import importlib
from pathlib import Path
from types import SimpleNamespace

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect

from src.db.models import Position


PARENT_ROOT = Path(__file__).resolve().parents[2]
CALCULATOR_MODULE_NAME = "src.agents.execution.pnl_calculator"
REPO_MODULE_NAME = "src.db.repositories.position_repository"
REPORTER_MODULE_NAME = "src.agents.execution.lifecycle_reporter"
POSITION_SCHEMA_MODULE_NAME = "src.schemas.position"
EXECUTION_SCHEMA_MODULE_NAME = "src.schemas.execution"
MIGRATION_FILE = Path("migrations/versions/0004_add_fee_columns.py")


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:
        pytest.fail(f"Module {name} import failed unexpectedly: {exc!r}", pytrace=False)


def _make_position_row(
    *,
    position_id: str,
    status: str,
    entry_price: Decimal = Decimal("0.45"),
    order_size_usdc: Decimal = Decimal("25"),
) -> Position:
    now = datetime.now(timezone.utc)
    return Position(
        id=position_id,
        condition_id=f"condition-{position_id}",
        token_id=f"token-{position_id}",
        status=status,
        side="BUY",
        entry_price=entry_price,
        order_size_usdc=order_size_usdc,
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=entry_price + Decimal("0.01"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action="EXECUTED",
        reason="integration-test",
        routed_at_utc=now,
        recorded_at_utc=now,
        realized_pnl=None,
        exit_price=None,
        closed_at_utc=None,
    )


def _make_position_record(
    *,
    position_id: str,
    status: str,
    entry_price: Decimal = Decimal("0.45"),
    order_size_usdc: Decimal = Decimal("25"),
):
    position_schema_module = _load_module(POSITION_SCHEMA_MODULE_NAME)
    execution_schema_module = _load_module(EXECUTION_SCHEMA_MODULE_NAME)
    now = datetime.now(timezone.utc)
    return position_schema_module.PositionRecord(
        id=position_id,
        condition_id=f"condition-{position_id}",
        token_id=f"token-{position_id}",
        status=getattr(position_schema_module.PositionStatus, status),
        side="BUY",
        entry_price=entry_price,
        order_size_usdc=order_size_usdc,
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=entry_price + Decimal("0.01"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action=execution_schema_module.ExecutionAction.EXECUTED,
        reason="integration-test",
        routed_at_utc=now,
        recorded_at_utc=now,
        realized_pnl=None,
        exit_price=None,
        closed_at_utc=None,
    )


def _alembic_cfg(database_url: str) -> Config:
    cfg = Config(str(PARENT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PARENT_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _list_columns(sync_database_url: str, table_name: str) -> set[str]:
    from sqlalchemy import create_engine

    engine = create_engine(sync_database_url)
    try:
        with engine.connect() as conn:
            inspector = inspect(conn)
            return {col["name"] for col in inspector.get_columns(table_name)}
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_record_settlement_persists_explicit_gas_and_fee_columns(db_session_factory):
    repo_module = _load_module(REPO_MODULE_NAME)
    async with db_session_factory() as session:
        repo = repo_module.PositionRepository(session)
        row = _make_position_row(position_id="pos-wi28-int-101", status="OPEN")
        await repo.insert_position(row)
        await repo.record_settlement(
            position_id=row.id,
            realized_pnl=Decimal("11.111111111111111111"),
            exit_price=Decimal("0.65"),
            closed_at_utc=datetime.now(timezone.utc),
            gas_cost_usdc=Decimal("0.50"),
            fees_usdc=Decimal("0.25"),
        )
        await session.commit()

    async with db_session_factory() as session:
        repo = repo_module.PositionRepository(session)
        fetched = await repo.get_by_id("pos-wi28-int-101")

    assert fetched is not None
    assert fetched.gas_cost_usdc == Decimal("0.50")
    assert fetched.fees_usdc == Decimal("0.25")


@pytest.mark.asyncio
async def test_record_settlement_normalizes_legacy_missing_fee_values_to_zero(
    db_session_factory,
):
    repo_module = _load_module(REPO_MODULE_NAME)
    async with db_session_factory() as session:
        repo = repo_module.PositionRepository(session)
        row = _make_position_row(position_id="pos-wi28-int-102", status="OPEN")
        await repo.insert_position(row)
        await repo.record_settlement(
            position_id=row.id,
            realized_pnl=Decimal("11.111111111111111111"),
            exit_price=Decimal("0.65"),
            closed_at_utc=datetime.now(timezone.utc),
        )
        await session.commit()

    async with db_session_factory() as session:
        repo = repo_module.PositionRepository(session)
        fetched = await repo.get_by_id("pos-wi28-int-102")

    assert fetched is not None
    assert fetched.gas_cost_usdc == Decimal("0")
    assert fetched.fees_usdc == Decimal("0")


@pytest.mark.asyncio
async def test_lifecycle_report_treats_legacy_null_fee_columns_as_zero(db_session_factory):
    reporter_module = _load_module(REPORTER_MODULE_NAME)
    async with db_session_factory() as session:
        row = _make_position_row(position_id="pos-wi28-int-103", status="CLOSED")
        row.realized_pnl = Decimal("3.0")
        row.exit_price = Decimal("0.60")
        row.closed_at_utc = datetime.now(timezone.utc)
        session.add(row)
        await session.commit()

    reporter = reporter_module.PositionLifecycleReporter(
        config=SimpleNamespace(dry_run=True),
        db_session_factory=db_session_factory,
    )
    report = await reporter.generate_report()

    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.gas_cost_usdc == Decimal("0")
    assert entry.fees_usdc == Decimal("0")
    assert entry.net_realized_pnl == Decimal("3.0")


@pytest.mark.asyncio
async def test_full_settlement_and_report_round_trip_surfaces_net_pnl_totals(
    db_session_factory,
):
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    reporter_module = _load_module(REPORTER_MODULE_NAME)

    async with db_session_factory() as session:
        session.add(_make_position_row(position_id="pos-wi28-int-104", status="CLOSED"))
        await session.commit()

    calculator = calculator_module.PnLCalculator(
        config=SimpleNamespace(dry_run=False),
        db_session_factory=db_session_factory,
    )
    position = _make_position_record(position_id="pos-wi28-int-104", status="CLOSED")
    record = await calculator.settle(
        position=position,
        exit_price=Decimal("0.70"),
        gas_cost_usdc=Decimal("0.50"),
        fees_usdc=Decimal("0.25"),
    )

    reporter = reporter_module.PositionLifecycleReporter(
        config=SimpleNamespace(dry_run=False),
        db_session_factory=db_session_factory,
    )
    report = await reporter.generate_report()

    assert len(report.entries) == 1
    entry = report.entries[0]
    assert entry.gas_cost_usdc == Decimal("0.50")
    assert entry.fees_usdc == Decimal("0.25")
    assert entry.net_realized_pnl == record.net_realized_pnl
    assert report.total_gas_cost_usdc == Decimal("0.50")
    assert report.total_fees_usdc == Decimal("0.25")
    assert report.total_net_realized_pnl == record.net_realized_pnl


@pytest.mark.asyncio
async def test_dry_run_settle_does_not_persist_fee_columns(db_session_factory):
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)

    async with db_session_factory() as session:
        session.add(_make_position_row(position_id="pos-wi28-int-105", status="OPEN"))
        await session.commit()

    calculator = calculator_module.PnLCalculator(
        config=SimpleNamespace(dry_run=True),
        db_session_factory=db_session_factory,
    )
    position = _make_position_record(position_id="pos-wi28-int-105", status="OPEN")
    record = await calculator.settle(
        position=position,
        exit_price=Decimal("0.70"),
        gas_cost_usdc=Decimal("0.50"),
        fees_usdc=Decimal("0.25"),
    )

    async with db_session_factory() as session:
        fetched = await session.get(Position, "pos-wi28-int-105")

    assert record.gas_cost_usdc == Decimal("0.50")
    assert record.fees_usdc == Decimal("0.25")
    assert fetched is not None
    assert fetched.gas_cost_usdc is None
    assert fetched.fees_usdc is None


def test_migration_0004_round_trip_adds_and_removes_fee_columns(tmp_path):
    assert MIGRATION_FILE.exists()

    db_file = tmp_path / "wi28_round_trip.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"
    sync_url = f"sqlite:///{db_file}"
    cfg = _alembic_cfg(async_url)

    command.upgrade(cfg, "head")
    upgraded_columns = _list_columns(sync_url, "positions")
    assert {"gas_cost_usdc", "fees_usdc"}.issubset(upgraded_columns)

    command.downgrade(cfg, "0003")
    downgraded_columns = _list_columns(sync_url, "positions")
    assert "gas_cost_usdc" not in downgraded_columns
    assert "fees_usdc" not in downgraded_columns
