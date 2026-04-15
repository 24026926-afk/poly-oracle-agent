"""
tests/integration/test_pnl_settlement_integration.py

RED-phase integration tests for WI-21 Realized PnL & Settlement.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import inspect, select

from src.db.models import Position
from src.orchestrator import Orchestrator
from src.schemas.execution import (
    ExitOrderAction,
    ExitOrderResult,
    ExitReason,
    ExitResult,
)
from src.schemas.position import PositionRecord, PositionStatus
from src.schemas.web3 import OrderData, OrderSide, SIGNATURE_TYPE_EOA, SignedOrder


PARENT_ROOT = Path(__file__).resolve().parents[2]
CALCULATOR_MODULE_NAME = "src.agents.execution.pnl_calculator"
CALCULATOR_MODULE_PATH = Path("src/agents/execution/pnl_calculator.py")
SCHEMA_MODULE_NAME = "src.schemas.execution"
REPO_MODULE_NAME = "src.db.repositories.position_repository"
MIGRATION_FILE = Path("migrations/versions/0003_add_pnl_columns.py")
FORBIDDEN_IMPORT_PREFIXES = (
    "src.agents.context",
    "src.agents.evaluation",
    "src.agents.ingestion",
)
FORBIDDEN_IMPORTS = {
    "src.agents.execution.polymarket_client",
    "src.agents.execution.bankroll_sync",
    "src.agents.execution.signer",
    "src.agents.execution.execution_router",
    "src.agents.execution.exit_order_router",
}


def _load_module(name: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError:
        pytest.fail(f"Expected module {name} to exist.", pytrace=False)
    except Exception as exc:
        pytest.fail(f"Module {name} import failed unexpectedly: {exc!r}", pytrace=False)


def _patch_heavy_deps():
    mock_w3 = MagicMock()
    mock_w3.eth = MagicMock()
    mock_w3.eth.get_transaction_count = AsyncMock(return_value=0)
    return {
        "AsyncWeb3": MagicMock(return_value=mock_w3),
        "AsyncHTTPProvider": MagicMock(),
        "AsyncSessionLocal": MagicMock(),
        "engine": MagicMock(dispose=AsyncMock()),
    }


def _build_orchestrator(test_config) -> Orchestrator:
    with patch.multiple("src.orchestrator", **_patch_heavy_deps()):
        return Orchestrator(test_config)


def _make_position_row(
    *,
    position_id: str = "pos-int-001",
    condition_id: str = "condition-int-001",
    token_id: str = "token-int-001",
    status: str = "CLOSED",
    entry_price: Decimal = Decimal("0.45"),
    order_size_usdc: Decimal = Decimal("25"),
):
    now = datetime.now(timezone.utc)
    return Position(
        id=position_id,
        condition_id=condition_id,
        token_id=token_id,
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
    )


def _make_position_record(
    *,
    position_id: str = "pos-int-001",
    condition_id: str = "condition-int-001",
    token_id: str = "token-int-001",
    entry_price: Decimal = Decimal("0.45"),
    order_size_usdc: Decimal = Decimal("25"),
) -> PositionRecord:
    now = datetime.now(timezone.utc)
    return PositionRecord(
        id=position_id,
        condition_id=condition_id,
        token_id=token_id,
        status=PositionStatus.CLOSED,
        side="BUY",
        entry_price=entry_price,
        order_size_usdc=order_size_usdc,
        kelly_fraction=Decimal("0.10"),
        best_ask_at_entry=entry_price + Decimal("0.01"),
        bankroll_usdc_at_entry=Decimal("1000"),
        execution_action=_load_module(SCHEMA_MODULE_NAME).ExecutionAction.EXECUTED,
        reason="integration-test",
        routed_at_utc=now,
        recorded_at_utc=now,
        realized_pnl=None,
        exit_price=None,
        closed_at_utc=None,
    )


def _make_exit_result(*, position_id: str, should_exit: bool) -> ExitResult:
    return ExitResult(
        position_id=position_id,
        condition_id=f"condition-{position_id}",
        should_exit=should_exit,
        exit_reason=ExitReason.STOP_LOSS if should_exit else ExitReason.NO_EDGE,
        entry_price=Decimal("0.45"),
        current_midpoint=Decimal("0.40"),
        current_best_bid=Decimal("0.39"),
        position_age_hours=Decimal("8"),
        unrealized_edge=Decimal("-0.05"),
        evaluated_at_utc=datetime.now(timezone.utc),
    )


def _make_signed_order(order: OrderData) -> SignedOrder:
    return SignedOrder(
        order=order,
        signature="0x" + "ab" * 65,
        owner="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    )


def _make_exit_order_result(
    *, position_id: str, action: ExitOrderAction
) -> ExitOrderResult:
    payload = OrderData(
        salt=1,
        maker="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        signer="0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        taker="0x0000000000000000000000000000000000000000",
        token_id=123,
        maker_amount=1_000_000,
        taker_amount=500_000,
        expiration=0,
        nonce=0,
        fee_rate_bps=0,
        side=OrderSide.SELL,
        signature_type=SIGNATURE_TYPE_EOA,
    )
    signed = (
        _make_signed_order(payload) if action == ExitOrderAction.SELL_ROUTED else None
    )
    return ExitOrderResult(
        position_id=position_id,
        condition_id=f"condition-{position_id}",
        action=action,
        reason=None,
        order_payload=payload,
        signed_order=signed,
        exit_price=Decimal("0.65"),
        order_size_usdc=Decimal("25"),
        routed_at_utc=datetime.now(timezone.utc),
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
async def test_record_settlement_writes_columns_and_round_trips(db_session_factory):
    repo_module = _load_module(REPO_MODULE_NAME)
    async with db_session_factory() as session:
        repo = repo_module.PositionRepository(session)
        row = _make_position_row(position_id="pos-int-101")
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
        fetched = await repo.get_by_id("pos-int-101")
    assert fetched is not None
    assert Decimal(str(fetched.realized_pnl)).quantize(Decimal("1e-12")) == Decimal(
        "11.111111111111"
    )
    assert Decimal(str(fetched.exit_price)).quantize(Decimal("1e-6")) == Decimal(
        "0.650000"
    )
    assert fetched.closed_at_utc is not None


@pytest.mark.asyncio
async def test_record_settlement_is_idempotent_and_does_not_overwrite(
    db_session_factory,
):
    repo_module = _load_module(REPO_MODULE_NAME)
    first_closed = datetime.now(timezone.utc)
    async with db_session_factory() as session:
        repo = repo_module.PositionRepository(session)
        row = _make_position_row(position_id="pos-int-102")
        await repo.insert_position(row)
        await repo.record_settlement(
            position_id=row.id,
            realized_pnl=Decimal("10"),
            exit_price=Decimal("0.60"),
            closed_at_utc=first_closed,
        )
        await session.commit()

    second_closed = datetime.now(timezone.utc)
    async with db_session_factory() as session:
        repo = repo_module.PositionRepository(session)
        settled = await repo.record_settlement(
            position_id="pos-int-102",
            realized_pnl=Decimal("999"),
            exit_price=Decimal("0.99"),
            closed_at_utc=second_closed,
        )
        await session.commit()

    assert settled is not None
    assert Decimal(str(settled.realized_pnl)).quantize(Decimal("1e-6")) == Decimal(
        "10.000000"
    )
    assert Decimal(str(settled.exit_price)).quantize(Decimal("1e-6")) == Decimal(
        "0.600000"
    )
    assert settled.closed_at_utc is not None
    assert settled.closed_at_utc.replace(tzinfo=timezone.utc) == first_closed


@pytest.mark.asyncio
async def test_record_settlement_returns_none_when_position_missing(async_session):
    repo_module = _load_module(REPO_MODULE_NAME)
    repo = repo_module.PositionRepository(async_session)

    result = await repo.record_settlement(
        position_id="missing",
        realized_pnl=Decimal("1"),
        exit_price=Decimal("0.2"),
        closed_at_utc=datetime.now(timezone.utc),
    )
    assert result is None


@pytest.mark.asyncio
async def test_end_to_end_dry_run_computes_record_and_writes_zero_db_rows(
    db_session_factory,
):
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    schema_module = _load_module(SCHEMA_MODULE_NAME)
    calculator = calculator_module.PnLCalculator(
        config=SimpleNamespace(dry_run=True),
        db_session_factory=db_session_factory,
    )
    position = _make_position_record(position_id="pos-int-201")

    record = await calculator.settle(position=position, exit_price=Decimal("0.65"))

    assert isinstance(record, schema_module.PnLRecord)
    async with db_session_factory() as session:
        rows = (await session.execute(select(Position))).scalars().all()
        assert rows == []


@pytest.mark.asyncio
async def test_end_to_end_live_mode_persists_settlement_values(
    db_session_factory,
):
    calculator_module = _load_module(CALCULATOR_MODULE_NAME)
    calculator = calculator_module.PnLCalculator(
        config=SimpleNamespace(dry_run=False),
        db_session_factory=db_session_factory,
    )
    position = _make_position_record(position_id="pos-int-202")

    async with db_session_factory() as session:
        session.add(_make_position_row(position_id="pos-int-202"))
        await session.commit()

    record = await calculator.settle(position=position, exit_price=Decimal("0.65"))

    async with db_session_factory() as session:
        row = await session.get(Position, "pos-int-202")
        assert row is not None
        assert Decimal(str(row.exit_price)).quantize(Decimal("1e-6")) == Decimal(
            "0.650000"
        )
        assert Decimal(str(row.realized_pnl)).quantize(Decimal("1e-12")) == (
            record.realized_pnl.quantize(Decimal("1e-12"))
        )
        assert row.closed_at_utc is not None


def test_pnl_calculator_module_has_no_forbidden_imports():
    if not CALCULATOR_MODULE_PATH.exists():
        pytest.fail(
            "Expected implementation file at src/agents/execution/pnl_calculator.py.",
            pytrace=False,
        )

    tree = ast.parse(CALCULATOR_MODULE_PATH.read_text())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    forbidden_prefix_matches = sorted(
        module_name
        for module_name in imported_modules
        if module_name.startswith(FORBIDDEN_IMPORT_PREFIXES)
    )
    forbidden_exact_matches = sorted(
        module_name
        for module_name in imported_modules
        if module_name in FORBIDDEN_IMPORTS
    )
    assert forbidden_prefix_matches == []
    assert forbidden_exact_matches == []


def test_migration_file_0003_exists_and_references_parent_0002():
    assert MIGRATION_FILE.exists()
    contents = MIGRATION_FILE.read_text()
    assert 'revision: str = "0003"' in contents
    assert 'down_revision: Union[str, Sequence[str], None] = "0002"' in contents


def test_migration_0003_upgrade_adds_expected_columns(tmp_path):
    db_file = tmp_path / "wi21_upgrade.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"
    sync_url = f"sqlite:///{db_file}"
    cfg = _alembic_cfg(async_url)

    command.upgrade(cfg, "head")
    columns = _list_columns(sync_url, "positions")
    assert {"realized_pnl", "exit_price", "closed_at_utc"}.issubset(columns)


def test_migration_0003_downgrade_removes_expected_columns(tmp_path):
    db_file = tmp_path / "wi21_downgrade.db"
    async_url = f"sqlite+aiosqlite:///{db_file}"
    sync_url = f"sqlite:///{db_file}"
    cfg = _alembic_cfg(async_url)

    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0002")
    columns = _list_columns(sync_url, "positions")
    assert "realized_pnl" not in columns
    assert "exit_price" not in columns
    assert "closed_at_utc" not in columns


def test_orchestrator_constructs_pnl_calculator_after_exit_order_router(test_config):
    with (
        patch.multiple("src.orchestrator", **_patch_heavy_deps()),
        patch("src.orchestrator.PnLCalculator") as mock_pnl_cls,
    ):
        orch = Orchestrator(test_config)

    assert orch.pnl_calculator is mock_pnl_cls.return_value
    mock_pnl_cls.assert_called_once_with(
        config=orch.config,
        db_session_factory=orch.position_tracker._db_session_factory,
    )


@pytest.mark.asyncio
async def test_exit_scan_loop_calls_pnl_settle_for_sell_routed_and_dry_run_actions(
    monkeypatch,
    test_config,
):
    orch = _build_orchestrator(test_config)
    object.__setattr__(orch.config, "exit_scan_interval_seconds", Decimal("1"))
    object.__setattr__(orch.config, "dry_run", True)

    exit_results = [
        _make_exit_result(position_id="pos-int-301", should_exit=True),
        _make_exit_result(position_id="pos-int-302", should_exit=True),
    ]
    positions = [
        _make_position_record(position_id="pos-int-301"),
        _make_position_record(position_id="pos-int-302"),
    ]

    orch.exit_strategy_engine.scan_open_positions = AsyncMock(return_value=exit_results)
    orch._fetch_position_record = AsyncMock(side_effect=positions)
    orch.exit_order_router = MagicMock()
    orch.exit_order_router.route_exit = AsyncMock(
        side_effect=[
            _make_exit_order_result(
                position_id="pos-int-301",
                action=ExitOrderAction.SELL_ROUTED,
            ),
            _make_exit_order_result(
                position_id="pos-int-302",
                action=ExitOrderAction.DRY_RUN,
            ),
        ]
    )
    orch.pnl_calculator = MagicMock()
    orch.pnl_calculator.settle = AsyncMock()
    orch.broadcaster = AsyncMock()

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orch._exit_scan_loop()

    assert orch.pnl_calculator.settle.await_count == 2


@pytest.mark.asyncio
async def test_exit_scan_loop_does_not_call_pnl_settle_when_route_exit_fails(
    monkeypatch,
    test_config,
):
    orch = _build_orchestrator(test_config)
    object.__setattr__(orch.config, "exit_scan_interval_seconds", Decimal("1"))
    object.__setattr__(orch.config, "dry_run", True)

    exit_result = _make_exit_result(position_id="pos-int-303", should_exit=True)
    orch.exit_strategy_engine.scan_open_positions = AsyncMock(
        return_value=[exit_result]
    )
    orch._fetch_position_record = AsyncMock(
        return_value=_make_position_record(position_id="pos-int-303")
    )
    orch.exit_order_router = MagicMock()
    orch.exit_order_router.route_exit = AsyncMock(
        return_value=_make_exit_order_result(
            position_id="pos-int-303",
            action=ExitOrderAction.FAILED,
        )
    )
    orch.pnl_calculator = MagicMock()
    orch.pnl_calculator.settle = AsyncMock()

    sleep_calls = 0

    async def _fake_sleep(seconds: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr("src.orchestrator.asyncio.sleep", _fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await orch._exit_scan_loop()

    orch.pnl_calculator.settle.assert_not_awaited()
